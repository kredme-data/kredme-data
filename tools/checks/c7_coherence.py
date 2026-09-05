"""L7 — cross-card coherence.

"This card is fine. The catalogue is not."

Every other layer looks at one row, or at one card, and asks whether it is
well-formed, in-vocabulary, resolvable or numerically plausible. A file can
pass all of that and still be incoherent as a *set*:

  * one bank filed under two spellings, so it appears twice in the picker
  * one product filed twice under two ids, at two different rates
  * two rules on one card sharing a name, and therefore sharing one cap
  * an issuer's single pooled cap copied onto five rules, so the app lets a
    user earn it five times over
  * a card that ships no reward information at all, yet sits in the picker
  * one card whose rate is nothing like anything else that bank issues

None of those are visible from a single row. All of them are visible to a
user: a duplicated bank in the bank list, a card that can never be
recommended, a cap that is five times too generous.

Authority for the app-side consequences quoted below:
    lib/core/engine/recommendation_engine.dart   (nous/master, 917 lines)
    lib/shared/models/credit_card.dart           (nous/master, 1021 lines)
Specifically: the cap spend bucket is keyed on `rule_name` alone
(`"${ruleName}|YYYY_MM"`, engine ~:819/:826/:832), so two rules that share a
name share a cap, and two rules that should share a cap but have different
names get one bucket each.

Nothing here is grandfathered, nothing reads tools/rate_baseline.json, and
nothing is suppressed. Where this layer would repeat a defect another layer
already owns (a rate outside the 0.1%–10% band is L4's; a UPI rule on a
non-RuPay card is L6's) it deliberately stays inside that band and says so.
"""
from __future__ import annotations

import collections
import re

from .base import Ctx, Finding, ERROR, WARN, INFO, num, trunc, iso_ok, card_base_pct

LAYER = "L7 cross-card coherence"

# --------------------------------------------------------------------------- #
# Tunables — every one of them is a judgement call, so they live in one place.
# --------------------------------------------------------------------------- #
APP_PV_DEFAULT = 0.25          # credit_card.dart sanePointValue fallback
APP_PV_CLAMP_MAX = 1.5         # anything above this is clamped to the default

# L4 owns anything outside this band. This layer only compares cards to their
# own issuer's norm *inside* it, so the same rate is never reported twice.
L4_FLOOR_PCT = 0.1
L4_CEILING_PCT = 10.0

ISSUER_MIN_CARDS_FOR_OUTLIER = 5      # below this an "issuer norm" is noise
ISSUER_MIN_RULES_FOR_OUTLIER = 20
OUTLIER_MULTIPLE = 6.0                # x the issuer's median, both directions
TIER_SPREAD_MIN_CARDS = 3
TIER_SPREAD_RATIO = 10.0              # an order of magnitude

POOLED_NAME_OVERLAP = 0.65            # token overlap that means "same rule text"
MATERIAL_RATE_RATIO = 1.25            # same product, rates this far apart = stale
MATERIAL_RATE_GAP_PP = 0.25           # ...or this many percentage points apart

# A network field holding one of these is unfilled, not wrong. It cannot
# contradict the RuPay flag, so 6a leaves it alone.
PLACEHOLDER_NETWORKS = frozenset({"unknown", "n/a", "na", "none", "-", "tbd",
                                  "not available", "null"})

# Words that are corporate wrapping, not the bank's identity. Stripping these
# is what merges 'AU Bank' with 'AU Small Finance Bank' — and, importantly, is
# what does NOT merge 'HDFC Bank' with 'IDFC Bank'. Edit distance would.
ISSUER_STOPWORDS = frozenset({
    "bank", "banks", "limited", "ltd", "pvt", "private", "plc", "inc",
    "corporation", "corp", "co", "company", "the", "of", "and",
    "india", "indian", "card", "cards", "small", "finance", "financial",
    "services", "technologies", "technology",
})

# An issuer string carrying one of these is a co-brand sentence, not a bank
# name. Never fold it into another issuer automatically.
CO_BRAND_MARKERS = ("/", " with ", "partnership", "co-brand", "co brand",
                    "cobrand", "in association")

# Noise words inside a card name. 'card' only matches as a whole word, so
# 'BOBCARD' survives.
CARD_NAME_NOISE = re.compile(r"\b(credit\s+cards?|cards?|the)\b", re.I)

# A trailing "[grocery]" / "(dining)" disambiguator bolted onto an otherwise
# identical rule name. Stripping it is how a split pooled cap is spotted.
TRAILING_BRACKET = re.compile(r"\s*[\[\(][^\[\]\(\)]*[\]\)]\s*$")

POOLED_WORDING = re.compile(
    r"\b(combined|shared|pool(ed)?|across all|overall cap|total cap|"
    r"clubbed|together with)\b", re.I)

WORD = re.compile(r"[a-z0-9]+")

# Tier labels ranked by what the catalogue itself charges for them; the ranking
# is computed from the data, this is only the set we are willing to rank.
RANKABLE_TIERS = frozenset({
    "entry", "entry_level", "mid_range", "premium", "super_premium",
    "ultra_premium",
})


# --------------------------------------------------------------------------- #
# Helpers. None of these may raise, whatever they are handed.
# --------------------------------------------------------------------------- #
def _s(v):
    return v if isinstance(v, str) else None


def _txt(v):
    """Any value as trimmed display text, or ''."""
    return v.strip() if isinstance(v, str) else ""


def _rows(entry, block):
    v = entry.get(block) if isinstance(entry, dict) else None
    return v if isinstance(v, list) else []


def _words(v):
    return WORD.findall(v.lower()) if isinstance(v, str) else []


def _issuer_tokens(v):
    toks = _words(v)
    kept = [t for t in toks if t not in ISSUER_STOPWORDS]
    return tuple(kept or toks)


def _issuer_key(v):
    return "".join(_issuer_tokens(v))


def _is_co_brand(v):
    low = (v or "").lower() if isinstance(v, str) else ""
    return any(m in low for m in CO_BRAND_MARKERS)


def _card_name_key(v):
    if not isinstance(v, str):
        return ""
    return "".join(WORD.findall(CARD_NAME_NOISE.sub(" ", v).lower()))


def _card_name_tokens(v):
    if not isinstance(v, str):
        return frozenset()
    return frozenset(t for t in WORD.findall(CARD_NAME_NOISE.sub(" ", v).lower())
                     if t not in ("bank",))


def _stem(v):
    """Rule name with every trailing bracketed suffix peeled off."""
    s = _txt(v)
    prev = None
    while s != prev:
        prev = s
        s = TRAILING_BRACKET.sub("", s).strip()
    return re.sub(r"\s+", " ", s).lower()


def _jaccard(sets):
    try:
        inter = set.intersection(*sets)
        union = set.union(*sets)
        return len(inter) / len(union) if union else 0.0
    except Exception:
        return 0.0


def _num(v):
    """base.num, with NaN and infinity treated as 'not a number'.

    A NaN that reaches a median or a ratio produces a comparison that is
    neither true nor false and a message that reads 'nan%'. Better to call it
    missing here and let the schema layer report the value itself.
    """
    n = num(v)
    if n is None or n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def _sane_pv(v):
    p = _num(v)
    if p is None or p <= 0 or p > APP_PV_CLAMP_MAX:
        return APP_PV_DEFAULT
    return p


def _base_pct(inner):
    """The percent-of-spend the app actually prints for this card's base rate.

    Where the card ships a point value the app is willing to use, this defers
    to the shared card_base_pct so this layer cannot drift from L4. Where the
    card ships none, or ships one the app refuses (sanePointValue rejects
    anything <= 0 or > 1.5 and substitutes Rs 0.25), it follows the app —
    card_base_pct does not clamp, and an unclamped number would make every
    comparison below argue about a figure no user is shown.

    Returns None when there is no usable base rate at all. The app renders
    those as 'Rate not published'; that is L4's finding, not this layer's.
    """
    b = _num(inner.get("base_reward_rate"))
    if b is None or b <= 0:
        return None
    pv = _num(inner.get("rp_value_standard"))
    if pv is not None and 0 < pv <= APP_PV_CLAMP_MAX:
        try:
            p = card_base_pct(inner)
        except Exception:
            p = None
        if isinstance(p, (int, float)) and p > 0:
            return float(p)
    return b * APP_PV_DEFAULT * 100.0


def _rule_pct(inner, row):
    """What the app renders for one rule, replicating rateForRule exactly."""
    rate = _num(row.get("reward_rate"))
    if rate is None:
        return None
    base = _num(inner.get("base_reward_rate"))
    base = None if (base is None or base <= 0) else base
    card_pv = _sane_pv(inner.get("rp_value_standard"))
    # credit_card.dart:668 — `rule.point_value ?? card.rp_value_standard`, and a
    # point_value the Dart parser cannot read is a null, not a zero.
    rule_pv = _num(row.get("point_value"))
    pv = _sane_pv(rule_pv) if rule_pv is not None else card_pv
    rt = _s(row.get("reward_type")) or "points_per_spend"
    if rt == "cashback_pct":
        return rate * 100.0
    if rt == "multiplier":
        return None if base is None else rate * base * pv * 100.0
    if rt == "points_per_spend":
        unit = _num(row.get("reward_unit_spend"))
        if unit is None or unit <= 0:
            return None if base is None else base * card_pv * 100.0
        return (rate / unit) * pv * 100.0
    return None if base is None else base * card_pv * 100.0


def _median(vals):
    v = sorted(vals)
    n = len(v)
    if not n:
        return None
    m = n // 2
    return v[m] if n % 2 else (v[m - 1] + v[m]) / 2.0


def _house_name(recs):
    """issuer key -> the spelling most cards use, for group-level sentences."""
    counts = collections.defaultdict(collections.Counter)
    for r in recs:
        if r.get("issuer_key") and r.get("issuer_raw"):
            counts[r["issuer_key"]][r["issuer_raw"]] += 1
    return {k: v.most_common(1)[0][0] for k, v in counts.items() if v}


def _sample(items, n=8):
    items = list(items)
    head = ", ".join(str(x) for x in items[:n])
    return head + (f" (+{len(items) - n} more)" if len(items) > n else "")


def _money(v):
    try:
        return f"Rs {float(v):,.0f}"
    except Exception:
        return str(v)


def _cap_enforceable(row):
    """The engine needs BOTH a numeric amount and a period (engine:779)."""
    return _num(row.get("cap_amount")) is not None and bool(_txt(row.get("cap_period")))


# --------------------------------------------------------------------------- #
# Index — one defensive pass over the file, so every check below reads
# ordinary Python and can never trip over a malformed row.
# --------------------------------------------------------------------------- #
def _index(ctx, add):
    out = []
    for i, entry, inner, cid in ctx.entries():
        try:
            if not isinstance(inner, dict):
                continue
            rec = {
                "i": i,
                "id": cid if isinstance(cid, str) and cid else f"(entry {i}, no id)",
                "issuer_raw": _txt(inner.get("issuer")),
                "name_raw": _txt(inner.get("card_name")),
                "tier": _txt(inner.get("card_tier")).lower(),
                "fee": _num(inner.get("annual_fee")),
                "network": _txt(inner.get("network")),
                "rupay": inner.get("has_rupay_upi"),
                "currency": _txt(inner.get("reward_currency")).lower(),
                "base_pct": _base_pct(inner),
                "rules": [],
                "rule_rows": len(_rows(entry, "reward_rules")),
            }
            rec["issuer_key"] = _issuer_key(rec["issuer_raw"])
            rec["issuer_tokens"] = frozenset(_issuer_tokens(rec["issuer_raw"]))
            rec["name_key"] = _card_name_key(rec["name_raw"])
            rec["name_tokens"] = _card_name_tokens(rec["name_raw"])
            for j, row in enumerate(_rows(entry, "reward_rules")):
                if not isinstance(row, dict):
                    continue
                try:
                    rec["rules"].append({
                        "j": j,
                        "name": _txt(row.get("rule_name")),
                        "type": _txt(row.get("rule_type")).lower(),
                        "cap": _num(row.get("cap_amount")),
                        "period": _txt(row.get("cap_period")).lower(),
                        "enforceable": _cap_enforceable(row),
                        "pct": _rule_pct(inner, row),
                        "eff": row.get("effective_date"),
                        # Scope vs maths. A rule may set merchant_ref OR
                        # category_id, never both, so the pair identifies WHERE
                        # a rule applies while "maths" is WHAT it pays. Rules
                        # that share a name, agree on the maths and differ only
                        # in scope are one pooled cap, not a collision — see
                        # _rule_name_checks.
                        "scope": (_txt(row.get("merchant_ref")) or None,
                                  _txt(row.get("category_id")) or None),
                        "maths": (_txt(row.get("rule_type")).lower(),
                                  _txt(row.get("reward_type")).lower(),
                                  _num(row.get("reward_rate")),
                                  _num(row.get("reward_unit_spend")),
                                  _num(row.get("cap_amount")),
                                  _txt(row.get("cap_period")).lower(),
                                  _txt(row.get("channel")).lower() or None),
                    })
                except Exception:
                    continue
            out.append(rec)
        except Exception as exc:
            add(Finding(
                severity=WARN,
                code="L7.CARD_NOT_INDEXED",
                message=(f"Card at position {i} could not be read for the "
                         f"catalogue-wide checks, so it is missing from every "
                         f"count in this section."),
                card_id=cid if isinstance(cid, str) else None,
                evidence=trunc(f"{type(exc).__name__}: {exc}"),
                impact="Any duplicate bank, duplicate product or shared cap on this "
                       "card is unreported.",
                fix="Fix whatever L1 says about this card first, then re-run.",
            ))
    return out


# --------------------------------------------------------------------------- #
# 1. Issuer-name drift — one bank, several spellings, two rows in the picker.
# --------------------------------------------------------------------------- #
def _issuer_checks(recs, ctx, add):
    by_key = collections.defaultdict(collections.Counter)
    tokens_of = {}
    ids_of = collections.defaultdict(list)      # exact spelling -> card ids
    for r in recs:
        if not r["issuer_raw"]:
            continue
        by_key[r["issuer_key"]][r["issuer_raw"]] += 1
        tokens_of.setdefault(r["issuer_key"], set()).update(r["issuer_tokens"])
        ids_of[r["issuer_raw"]].append(r["id"])

    # -- 1a. exact same bank, different spelling ------------------------- #
    for key, spellings in sorted(by_key.items(),
                                 key=lambda kv: -sum(kv[1].values())):
        if len(spellings) < 2:
            continue
        n = sum(spellings.values())
        # Name the cards under the minority spellings — those are the rows a
        # human has to edit, and there are few enough of them to print.
        parts = []
        for name, c in spellings.most_common():
            part = f"'{name}' ({c} card{'s' if c != 1 else ''}"
            if c <= 6:
                part += f": {_sample(ids_of.get(name, []), 6)}"
            parts.append(part + ")")
        listed = " / ".join(parts)
        add(Finding(
            severity=WARN,
            code="L7.ISSUER_SPELLING_SPLIT",
            message=(f"One bank is spelled {len(spellings)} different ways across "
                     f"{n} cards, so it shows up as {len(spellings)} separate banks "
                     f"in the app."),
            block="card", field="issuer",
            evidence=trunc(listed, 600),
            impact=(f"A user looking for this bank finds {len(spellings)} entries and "
                    f"has to guess which one holds their card. Cards filed under the "
                    f"less obvious spelling are effectively hidden."),
            fix=("Pick one spelling and rewrite the issuer field on all "
                 f"{n} cards. This is a text change in seed/cards.json only — the "
                 "bank picker is built from the data at runtime, so it needs no app "
                 "release."),
        ))

    # -- 1b. probably the same bank, but only a human can say ------------ #
    keys = sorted(by_key)
    seen_pairs = set()
    for a in keys:
        for b in keys:
            if a >= b or (a, b) in seen_pairs:
                continue
            ta, tb = tokens_of.get(a, set()), tokens_of.get(b, set())
            if not ta or not tb or not (ta < tb or tb < ta):
                continue
            long_key = b if ta < tb else a
            long_names = list(by_key[long_key])
            if any(_is_co_brand(x) for x in long_names):
                continue          # reported as a co-brand string instead
            seen_pairs.add((a, b))
            na, nb = sum(by_key[a].values()), sum(by_key[b].values())
            add(Finding(
                severity=INFO,
                code="L7.ISSUER_MAYBE_SAME_BANK",
                message=("Two issuer names look like the same bank written short "
                         "and long, which would split it in the picker — but the "
                         "data cannot prove it, so a human has to decide."),
                block="card", field="issuer",
                evidence=trunc(
                    f"{_sample(by_key[a], 3)} ({na} cards)  vs  "
                    f"{_sample(by_key[b], 3)} ({nb} cards)", 300),
                impact="If they are the same bank, one of the two groups is sitting "
                       "in a picker entry nobody looks for.",
                fix=("Confirm against the issuer, then merge by hand. Never merge "
                     "issuer names on string similarity alone — 'HDFC Bank' and "
                     "'IDFC Bank' are 89% similar and are different banks."),
            ))

    # -- 1c. an issuer field holding a sentence -------------------------- #
    for key, spellings in by_key.items():
        for name, n in spellings.items():
            if not _is_co_brand(name):
                continue
            add(Finding(
                severity=WARN,
                code="L7.ISSUER_NAME_IS_A_SENTENCE",
                message=(f"The issuer field on {n} card{'s' if n != 1 else ''} holds "
                         f"a description of a partnership rather than a bank name, "
                         f"so the app lists that whole sentence as if it were a bank."),
                block="card", field="issuer",
                evidence=trunc(f"{name} — {_sample(ids_of.get(name, []), 6)}", 300),
                impact="The bank picker shows an entry that reads like a paragraph, "
                       "and the real issuer's own entry is missing these cards.",
                fix=("Put the issuing bank's plain name in `issuer` and keep the "
                     "co-brand in the card name, where it belongs."),
            ))


# --------------------------------------------------------------------------- #
# 2. The same product, filed twice.
# --------------------------------------------------------------------------- #
def _material_gap(a, b):
    if a is None or b is None:
        return False
    lo, hi = sorted((a, b))
    if lo <= 0:
        return hi > 0
    return (hi / lo) >= MATERIAL_RATE_RATIO or (hi - lo) >= MATERIAL_RATE_GAP_PP


def _card_name_checks(recs, ctx, add):
    by_name = collections.defaultdict(list)
    for r in recs:
        if r["name_key"]:
            by_name[r["name_key"]].append(r)

    for key, group in by_name.items():
        if len(group) < 2:
            continue
        one_bank = len({r["issuer_key"] for r in group}) == 1
        split_spelling = one_bank and len({r["issuer_raw"] for r in group}) > 1
        pcts = [r["base_pct"] for r in group]
        material = any(_material_gap(pcts[i], pcts[k])
                       for i in range(len(pcts)) for k in range(i + 1, len(pcts)))
        # An effective_date on either side says which copy is the newer one.
        dates = sorted({d for r in group for x in r["rules"]
                        if iso_ok(x.get("eff")) for d in [x["eff"]]})
        detail = "; ".join(
            f"{r['id']} = '{r['name_raw']}' ({r['issuer_raw'] or 'no issuer'}, "
            f"{'base ' + format(r['base_pct'], '.2f') + '%' if r['base_pct'] is not None else 'no base rate'}, "
            f"fee {_money(r['fee']) if r['fee'] is not None else 'unknown'}, "
            f"{len(r['rules'])} rule{'s' if len(r['rules']) != 1 else ''})"
            for r in group)
        if dates:
            detail += f"; effective dates seen: {_sample(dates, 4)}"
        if one_bank:
            head = (f"{len(group)} cards from the same bank are the same product name "
                    f"once punctuation and the words 'credit card' are ignored")
            if split_spelling:
                head += ", and they sit under two different spellings of that bank"
        else:
            head = (f"{len(group)} cards from different banks share one product name "
                    f"once punctuation and the words 'credit card' are ignored")
        if material:
            head += ", and they quote different rates"
        for r in group:
            add(Finding(
                severity=WARN,
                code="L7.SAME_PRODUCT_TWICE",
                message=head + ".",
                card_id=r["id"], block="card", field="card_name",
                evidence=trunc(detail, 500),
                impact=("The user sees two near-identical rows in the card list and "
                        "cannot tell which one is theirs"
                        + (". Because the rates differ, one of them is quoting money "
                           "the issuer no longer pays — that is what a pre/post-"
                           "devaluation duplicate looks like." if material else ".")),
                fix=("Check the issuer's page. If it is one product, delete the stale "
                     "id and keep the current rate — and remember a rename wipes cap "
                     "progress. If they are genuinely two products, make the names "
                     "say so."),
            ))

    # Near-duplicates that also straddle two spellings of one bank: a much
    # narrower net than name similarity, which on this file is ~98% wrong.
    by_issuer = collections.defaultdict(list)
    for r in recs:
        if r["issuer_key"] and r["name_tokens"]:
            by_issuer[r["issuer_key"]].append(r)
    for key, group in by_issuer.items():
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                A, B = group[a], group[b]
                if A["issuer_raw"] == B["issuer_raw"]:
                    continue          # same spelling: ordinary product family
                if A["name_key"] == B["name_key"]:
                    continue          # already reported above
                sa, sb = A["name_tokens"], B["name_tokens"]
                if not ((sa < sb and len(sb - sa) == 1) or (sb < sa and len(sa - sb) == 1)):
                    continue
                extra = _sample(sorted((sb - sa) or (sa - sb)), 3)
                add(Finding(
                    severity=INFO,
                    code="L7.NEAR_DUPLICATE_ACROSS_SPELLINGS",
                    message=("Two cards from the same bank differ by a single word in "
                             "the name and are filed under two different spellings of "
                             "that bank — worth checking they are really two products."),
                    card_id=A["id"], block="card", field="card_name",
                    evidence=trunc(
                        f"'{A['name_raw']}' ({A['issuer_raw']}) vs "
                        f"'{B['name_raw']}' ({B['issuer_raw']}); differs by: {extra}",
                        300),
                    impact="If they are one product, one of the two is stale data "
                           "sitting in the picker under a bank name that looks wrong.",
                    fix="Confirm at the issuer. Most one-word differences (Plus, Gold, "
                        "XL) are genuinely different cards — do not merge on the name.",
                ))


# --------------------------------------------------------------------------- #
# 3. Rule names that collide — and caps that should collide but do not.
# --------------------------------------------------------------------------- #
def _rule_name_checks(recs, ctx, add):
    for r in recs:
        names = collections.Counter(x["name"] for x in r["rules"])
        dups = {n: c for n, c in names.items() if c > 1 and n}
        blank = names.get("", 0)
        # An issuer that pools ONE cap across a list of brands or categories
        # ("5% on Amazon, Flipkart, Myntra ... capped at 1,000 a cycle") is
        # written as one row per merchant_ref or per category_id, all carrying
        # the SAME rule_name deliberately, because the app's cap bucket key is
        # `ruleName|periodKey`. Giving those rows distinct names would hand the
        # user one full cap per brand instead of one across all of them, which
        # overstates the card — so the shared name is the correct encoding, not
        # a defect. Every instance of this finding on the current file is that
        # construct: Millennia's ten merchants, SmartEarn's 10X brand list, both
        # PhonePe HDFC cards, Regalia Gold, and IndianOil Kotak's dining+grocery
        # pair whose own rule_name says they share one 800-point cap.
        #
        # It is only a real collision when the rows disagree on what they pay,
        # or when two of them cover the SAME scope — then which one the user
        # gets is undefined. Those still fire at full severity.
        pooled = set()
        for n in dups:
            rows = [x for x in r["rules"] if x["name"] == n]
            scopes = [x["scope"] for x in rows]
            if (len({x["maths"] for x in rows}) == 1
                    and all(sc != (None, None) for sc in scopes)
                    and len(set(scopes)) == len(scopes)):
                pooled.add(n)
        dups = {n: c for n, c in dups.items() if n not in pooled}

        if dups:
            capped = [x for x in r["rules"] if x["name"] in dups and x["enforceable"]]
            caps = {(x["cap"], x["period"]) for x in capped}
            sev = ERROR if len(capped) > 1 else WARN
            detail = "; ".join(
                f"'{trunc(n, 70)}' x{c}" for n, c in
                sorted(dups.items(), key=lambda kv: -kv[1]))
            add(Finding(
                severity=sev,
                code="L7.DUPLICATE_RULE_NAME",
                message=(f"{sum(dups.values())} of this card's {len(r['rules'])} "
                         f"reward rules reuse {len(dups)} rule "
                         f"name{'s' if len(dups) != 1 else ''} between them. The app "
                         f"tracks spending against the rule name, so rules that share "
                         f"a name are treated as one rule."
                         + (f" {len(capped)} of them carry a cap, so they all draw "
                            f"from a single ceiling." if len(capped) > 1 else "")),
                card_id=r["id"], block="reward_rules",
                index=next((x["j"] for x in r["rules"] if x["name"] in dups), None),
                field="rule_name",
                evidence=trunc(detail + (f"; shared cap(s): {sorted(caps)}" if caps else ""), 400),
                impact=("A user hits the cap after one category's worth of spending "
                        "and silently stops earning on all the others."
                        if len(capped) > 1 else
                        "Only one of the identically named rules will ever be shown, "
                        "so the others are dead weight on the card's page."),
                fix=("Give every rule on a card its own name — put the category or "
                     "merchant in the name. If the issuer really does pool these, "
                     "keep one rule and let it cover the lot."),
            ))
        if blank > 1:
            add(Finding(
                severity=ERROR,
                code="L7.RULES_WITH_NO_NAME",
                message=(f"{blank} reward rules on this card have no name at all. The "
                         f"app keys cap tracking on the name, so every unnamed rule on "
                         f"a card shares one bucket."),
                card_id=r["id"], block="reward_rules",
                index=next((x["j"] for x in r["rules"] if not x["name"]), None),
                field="rule_name",
                evidence=f"{blank} of {len(r['rules'])} rules have an empty rule_name",
                impact="Caps on these rules interfere with each other, and the card's "
                       "page shows blank rows.",
                fix="Name every rule after what it pays and where.",
            ))


def _name_clusters(rules):
    """Greedy clusters of rules whose names are, in substance, the same text.

    Two names cluster when one is the other plus a bracketed label, or when
    they share POOLED_NAME_OVERLAP of their words. The first rule in a cluster
    stays its yardstick, so a chain of loosely related names cannot drift into
    one big cluster.
    """
    clusters = []
    for x in rules:
        toks = set(_words(x["name"]))
        stem = _stem(x["name"])
        for c in clusters:
            same_stem = bool(stem) and stem == c["stem"]
            near = bool(toks and c["tokens"]) and _jaccard([toks, c["tokens"]]) >= POOLED_NAME_OVERLAP
            if same_stem or near:
                c["rules"].append(x)
                c["same_stem"] = c["same_stem"] and same_stem
                break
        else:
            clusters.append({"stem": stem, "tokens": toks, "rules": [x],
                             "same_stem": True})
    return clusters


def _cap_pool_checks(recs, ctx, add):
    """One issuer cap, copied onto several rules — so the app grants it several times.

    The app keeps one spend bucket per rule NAME. An issuer that publishes a
    single pooled ceiling ("Rs 2,000 a month across dining, travel and
    international") therefore gets one bucket per category here, and the user
    is quietly promised the cap several times over.

    Two signals, both deliberately conservative:
      * a family of rules whose names are the same text apart from a label,
        ALL carrying the identical cap and period; if the caps inside the
        family differ, the issuer clearly does differentiate and nothing is
        reported;
      * a rule whose own text says the cap is combined or shared with another.
    """
    for r in recs:
        # Rules that share a name already share one bucket, and
        # L7.DUPLICATE_RULE_NAME has said so. Leave them out entirely so the
        # same collision is never reported twice under two codes.
        seen = collections.Counter(x["name"] for x in r["rules"])
        capped = [x for x in r["rules"]
                  if x["cap"] is not None and x["cap"] > 0 and x["period"]
                  and seen[x["name"]] == 1]
        if len(capped) < 2:
            continue

        # (cap, period) -> the rules that look like they share it, and why.
        hits = {}

        def _claim(key, rules, why):
            h = hits.setdefault(key, {"rules": {}, "why": []})
            h["rules"].update({x["j"]: x for x in rules})
            if why not in h["why"]:
                h["why"].append(why)

        # -- signal 1: same rule text, same cap, several times -------------- #
        for c in _name_clusters(capped):
            rules = c["rules"]
            if len(rules) < 2:
                continue
            caps = {(x["cap"], x["period"]) for x in rules}
            if len(caps) != 1:
                continue          # different caps = the issuer differentiates
            _claim(caps.pop(), rules,
                   "identical rule text apart from a bracketed label"
                   if c["same_stem"] else
                   "the rule text is the same but for the category it names")

        # -- signal 2: the rule text says the cap is shared ----------------- #
        groups = collections.defaultdict(list)
        for x in capped:
            groups[(x["cap"], x["period"])].append(x)
        for key, rules in groups.items():
            if len(rules) < 2:
                continue
            if not any(POOLED_WORDING.search(x["name"] or "") for x in rules):
                continue
            _claim(key, rules, "the rule text itself says the cap is shared")

        hits = [(cap, period, sorted(h["rules"].values(), key=lambda x: x["j"]),
                 " and ".join(h["why"]))
                for (cap, period), h in hits.items()]
        hits.sort(key=lambda h: (-len(h[2]), h[0]))

        if not hits:
            continue
        detail = "; ".join(
            f"{len(rules)} rules capped at {cap:g} per {period} ({why}) — "
            f"e.g. '{trunc(rules[0]['name'], 60)}'"
            for cap, period, rules, why in hits)
        worst = max(hits, key=lambda h: len(h[2]))
        add(Finding(
            severity=WARN,
            code="L7.POOLED_CAP_SPLIT",
            message=(f"This card has {len(hits)} cap{'s' if len(hits) != 1 else ''} "
                     f"written onto several rules that look like one issuer cap split "
                     f"up. The app counts each rule separately, so it will let the "
                     f"user earn that cap up to {len(worst[2])} times over."),
            card_id=r["id"], block="reward_rules", index=worst[2][0]["j"],
            field="cap_amount",
            evidence=trunc(detail, 500),
            impact=(f"The card's page promises {worst[0]:g} per {worst[1]} on each of "
                    f"{len(worst[2])} separate rules. If the issuer pools them the "
                    f"real ceiling is {worst[0]:g} in total, and the user earns a "
                    f"third of what we told them and blames us for it."),
            fix=("Check the issuer's terms. If the cap is pooled, collapse those rules "
                 "into one rule carrying one cap — a shared cap cannot be written as "
                 "several rules, because the app buckets spending by rule name. If the "
                 "caps really are separate, say so in the rule names."),
        ))


# --------------------------------------------------------------------------- #
# 4. Rates that make no sense next to the same bank's other cards.
# --------------------------------------------------------------------------- #
def _rate_coherence_checks(recs, ctx, add):
    named_in_spread = set()
    house = _house_name(recs)

    # -- 4a. same issuer, same tier, an order of magnitude apart --------- #
    groups = collections.defaultdict(list)
    for r in recs:
        if r["base_pct"] is not None and r["issuer_key"] and r["tier"]:
            groups[(r["issuer_key"], r["tier"])].append(r)
    for (issuer, tier), group in sorted(groups.items()):
        if len(group) < TIER_SPREAD_MIN_CARDS:
            continue
        lo = min(group, key=lambda r: r["base_pct"])
        hi = max(group, key=lambda r: r["base_pct"])
        if lo["base_pct"] <= 0 or hi["base_pct"] / lo["base_pct"] < TIER_SPREAD_RATIO:
            continue
        named_in_spread.add(lo["id"])
        named_in_spread.add(hi["id"])
        label = house.get(issuer) or issuer
        for r in (lo, hi):
            add(Finding(
                severity=WARN,
                code="L7.ISSUER_TIER_RATE_SPREAD",
                message=(f"{label}'s {len(group)} '{tier}' cards claim base rates that "
                         f"are {hi['base_pct'] / lo['base_pct']:.0f} times apart. Cards "
                         f"in one tier at one bank should not differ by that much — one "
                         f"end of the range is almost certainly a unit mistake."),
                card_id=r["id"], block="card", field="base_reward_rate",
                evidence=trunc(
                    f"{lo['id']} = {lo['base_pct']:.3f}% .. {hi['id']} = "
                    f"{hi['base_pct']:.3f}%; group median "
                    f"{_median([x['base_pct'] for x in group]):.3f}%", 300),
                impact="The card at the low end is ranked below everything it should "
                       "beat; the card at the high end is recommended when it should "
                       "not be.",
                fix=("Read both cards' base_reward_rate against the issuer's page. A "
                     "rate stored as a display percentage where points-per-rupee was "
                     "meant is the usual cause, and the giveaway is a ratio equal to "
                     "1 / rp_value_standard."),
            ))

    # -- 4b. one card out of line with its own bank --------------------- #
    by_issuer = collections.defaultdict(list)
    for r in recs:
        if r["base_pct"] is not None and r["issuer_key"]:
            by_issuer[r["issuer_key"]].append(r)
    for issuer, group in sorted(by_issuer.items()):
        if len(group) < ISSUER_MIN_CARDS_FOR_OUTLIER:
            continue
        med = _median([r["base_pct"] for r in group])
        if not med or med <= 0:
            continue
        for r in group:
            p = r["base_pct"]
            if r["id"] in named_in_spread:
                continue                      # already said, under another code
            if not (L4_FLOOR_PCT <= p <= L4_CEILING_PCT):
                continue                      # outside this band is L4's finding
            if not (p >= OUTLIER_MULTIPLE * med or p * OUTLIER_MULTIPLE <= med):
                continue
            add(Finding(
                severity=WARN,
                code="L7.BASE_RATE_OUTLIER_FOR_ISSUER",
                message=(f"This card's base rate of {p:.2f}% is "
                         f"{max(p / med, med / p):.0f} times "
                         f"{'above' if p > med else 'below'} the typical "
                         f"{med:.2f}% across {house.get(issuer) or issuer}'s "
                         f"{len(group)} cards."),
                card_id=r["id"], block="card", field="base_reward_rate",
                evidence=trunc(
                    f"{p:.3f}% vs a median of {med:.3f}% over "
                    f"{len(group)} {house.get(issuer) or issuer} cards "
                    f"(range {min(x['base_pct'] for x in group):.3f}%-"
                    f"{max(x['base_pct'] for x in group):.3f}%)", 250),
                impact="If the number is wrong the card is ranked in the wrong place "
                       "on every recommendation, not just on its own page.",
                fix=("Verify against the issuer's page. It is inside the band the "
                     "numeric layer accepts, so nothing else will catch it — this is "
                     "a comparison against the bank's own other cards."),
            ))

    # -- 4c. a single rule out of line with the same bank's other rules -- #
    rules_by_issuer = collections.defaultdict(list)
    for r in recs:
        if not r["issuer_key"]:
            continue
        for x in r["rules"]:
            if x["pct"] is not None and x["pct"] > 0:
                rules_by_issuer[r["issuer_key"]].append((x["pct"], r, x))
    for issuer, rows in sorted(rules_by_issuer.items()):
        if len(rows) < ISSUER_MIN_RULES_FOR_OUTLIER:
            continue
        med = _median([p for p, _, _ in rows])
        if not med or med <= 0:
            continue
        per_card = collections.defaultdict(list)
        for p, r, x in rows:
            if p > OUTLIER_MULTIPLE * med and p <= L4_CEILING_PCT:
                per_card[r["id"]].append((p, r, x))
        for cid, hits in sorted(per_card.items()):
            r = hits[0][1]
            detail = "; ".join(f"{p:.2f}% — '{trunc(x['name'], 60)}'" for p, _, x in hits)
            add(Finding(
                severity=INFO,
                code="L7.RULE_RATE_OUTLIER_FOR_ISSUER",
                message=(f"{len(hits)} rule{'s' if len(hits) != 1 else ''} on this card "
                         f"{'pay' if len(hits) != 1 else 'pays'} more than "
                         f"{OUTLIER_MULTIPLE:.0f} times the typical "
                         f"{med:.2f}% across {house.get(issuer) or issuer}'s other rules. "
                         f"Unusual is not wrong — but it is where unit mistakes hide."),
                card_id=cid, block="reward_rules", index=hits[0][2]["j"],
                field="reward_rate",
                evidence=trunc(detail, 400),
                impact="If one of these is a unit mistake, the app recommends this card "
                       "ahead of better cards for that spend.",
                fix=("Check each against the issuer's own wording. Anything above "
                     f"{L4_CEILING_PCT:.0f}% is already reported by the numeric layer; "
                     "these sit below that line and would otherwise pass unnoticed."),
            ))


# --------------------------------------------------------------------------- #
# 5. Cards that cannot earn anything.
# --------------------------------------------------------------------------- #
def _earning_shape_checks(recs, ctx, add):
    for r in recs:
        rules = r["rules"]
        if not rules:
            add(Finding(
                severity=ERROR if r["base_pct"] is None else WARN,
                code="L7.CARD_HAS_NO_RULES",
                message=("This card ships no reward rules at all"
                         + (" and no usable base rate either, so the app has nothing "
                            "to show for it and nothing to rank it on."
                            if r["base_pct"] is None else
                            ", so it can only ever be recommended at its flat base rate.")),
                card_id=r["id"], block="reward_rules",
                evidence=trunc(
                    f"{r['rule_rows']} row(s) in reward_rules, "
                    f"base_reward_rate renders as "
                    f"{'nothing' if r['base_pct'] is None else format(r['base_pct'], '.2f') + '%'}",
                    200),
                impact=("The card appears in the bank picker, a user adds it, and the "
                        "app tells them 'Rate not published' for every purchase they "
                        "ever make. It also ranks below every card that has a number."
                        if r["base_pct"] is None else
                        "The card can never win a category recommendation, however good "
                        "its real offers are."),
                fix=("Add the issuer's earn table, or take the card out of the "
                     "catalogue until someone can. A card with no numbers is worse "
                     "than a missing card — the user thinks we checked."),
            ))
            continue

        if all(x["type"] in ("base_rate", "") for x in rules):
            add(Finding(
                severity=INFO,
                code="L7.CARD_HAS_ONLY_A_BASE_RULE",
                message=("This card has a base rate and nothing else — no category, "
                         "merchant or channel bonus of any kind."),
                card_id=r["id"], block="reward_rules",
                evidence=trunc(f"{len(rules)} rule(s), all of type base_rate", 120),
                impact="The card is only ever recommended when it happens to have the "
                       "best flat rate, so it looks weaker than it may be.",
                fix=("Fine if the card genuinely pays one flat rate. Otherwise its "
                     "accelerated categories have never been entered."),
            ))

        enforceable = [x for x in rules if x["enforceable"]]
        if r["base_pct"] is None and len(enforceable) == len(rules):
            caps = _sample([f"{x['cap']:g}/{x['period']}" for x in rules], 5)
            add(Finding(
                severity=ERROR,
                code="L7.EARNS_NOTHING_AFTER_CAP",
                message=((f"This card's only reward rule is capped"
                          if len(rules) == 1 else
                          f"Every one of this card's {len(rules)} reward rules is capped")
                         + ", and the card has no working base rate, so once the cap is "
                           "used up the card earns the user nothing at all."),
                card_id=r["id"], block="reward_rules", index=rules[0]["j"],
                evidence=trunc(f"caps: {caps}; base_reward_rate is missing or zero", 300),
                impact="Mid-month the app quietly stops recommending this card and "
                       "shows 'Rate not published' — with no explanation the user can "
                       "act on.",
                fix=("Enter the card's uncapped base earn rate. Almost every card pays "
                     "something after the accelerated cap, and that number is what the "
                     "app falls back to."),
            ))


# --------------------------------------------------------------------------- #
# 6. Labels that contradict each other across the catalogue.
# --------------------------------------------------------------------------- #
def _label_checks(recs, ctx, add):
    # -- 6a. the RuPay flag and the network field disagree --------------- #
    for r in recs:
        net = r["network"].lower()
        flag = r["rupay"] in (1, True, "1")
        says_rupay = "rupay" in net
        # An empty or placeholder network cannot contradict anything; that it is
        # blank at all is already the vocabulary layer's finding, not this one's.
        if not net or net in PLACEHOLDER_NETWORKS or flag == says_rupay:
            continue
        add(Finding(
            severity=WARN,
            code="L7.RUPAY_FLAG_VS_NETWORK",
            message=("This card's network says "
                     + (f"'{trunc(r['network'], 40)}' while the RuPay-UPI flag is on."
                        if flag else
                        f"'{trunc(r['network'], 40)}' while the RuPay-UPI flag is off.")),
            card_id=r["id"], block="card", field="has_rupay_upi",
            evidence=trunc(f"network={r['network']!r}, has_rupay_upi={r['rupay']!r}", 200),
            impact=("Only the flag decides whether the app will pay UPI rates on this "
                    "card; the network text is what the user reads. One of the two is "
                    "lying to somebody."),
            fix=("Check the card at the issuer. If it is a RuPay card, write 'rupay' "
                 "in network and set has_rupay_upi to 1; if it is not, set the flag "
                 "to 0."),
        ))

    # -- 6b. a tier label its own price contradicts ---------------------- #
    fees_by_tier = collections.defaultdict(list)
    for r in recs:
        if r["fee"] is not None and r["tier"] in RANKABLE_TIERS:
            fees_by_tier[r["tier"]].append(r["fee"])
    medians = {t: _median(v) for t, v in fees_by_tier.items() if len(v) >= 3}
    order = sorted(medians, key=lambda t: medians[t])
    if len(order) >= 3:
        for r in recs:
            if r["fee"] is None or r["tier"] not in medians:
                continue
            rank = order.index(r["tier"])
            beneath = [t for t in order[:max(0, rank - 1)]]
            if not beneath or not all(r["fee"] < medians[t] for t in beneath):
                continue
            add(Finding(
                severity=WARN,
                code="L7.TIER_CONTRADICTS_FEE",
                message=(f"This card is labelled '{r['tier']}' but costs "
                         f"{_money(r['fee'])} a year — less than the typical card in "
                         f"{'tiers' if len(beneath) > 1 else 'the tier'} the catalogue "
                         f"treats as cheaper ({', '.join(t + ' ' + _money(medians[t]) for t in beneath)})."),
                card_id=r["id"], block="card", field="card_tier",
                evidence=trunc(
                    f"tier={r['tier']}, annual_fee={r['fee']:g}; tier medians: "
                    + ", ".join(f"{t}={medians[t]:g}" for t in order), 300),
                impact="Tier is what a user reads to judge whether a card is in their "
                       "league. A free card badged as a top-tier card reads as a "
                       "mistake and costs trust.",
                fix=("Set the tier from the card's real position in the issuer's "
                     "line-up. Note the app does not recognise 'entry', 'mid_range' "
                     "or 'ultra_premium' at all and simply title-cases them."),
            ))


# --------------------------------------------------------------------------- #
# 7. One headline the founder can read without opening anything else.
# --------------------------------------------------------------------------- #
def _summary(recs, ctx, add):
    spellings = collections.Counter(r["issuer_raw"] for r in recs if r["issuer_raw"])
    keys = collections.defaultdict(collections.Counter)
    for r in recs:
        if r["issuer_raw"]:
            keys[r["issuer_key"]][r["issuer_raw"]] += 1
    split_groups = {k: v for k, v in keys.items() if len(v) > 1}
    split_cards = sum(sum(v.values()) for v in split_groups.values())
    no_rules = sum(1 for r in recs if not r["rules"])
    add(Finding(
        severity=INFO,
        code="L7.CATALOGUE_SHAPE",
        message=(f"{len(recs)} cards from {len(keys)} banks, written as "
                 f"{len(spellings)} different issuer names. "
                 f"{split_cards} card{'s' if split_cards != 1 else ''} "
                 f"({(split_cards / len(recs) * 100) if recs else 0:.0f}%) sit under a "
                 f"bank that is spelled more than one way, and {no_rules} card"
                 f"{'s' if no_rules != 1 else ''} carry no reward rules at all."),
        block="card",
        evidence=trunc(
            f"{len(split_groups)} banks with more than one spelling; "
            f"{no_rules} cards with an empty reward_rules block", 250),
        impact="Two numbers a founder can quote: how many cards are hard to find in "
               "the picker, and how many are in the picker but can never be "
               "recommended.",
        fix="Both are text-and-data fixes in seed/cards.json. Neither needs an app "
            "release.",
    ))


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    add = out.append

    if not isinstance(ctx.cards, list) or not ctx.cards:
        add(Finding(
            severity=WARN,
            code="L7.CHECK_NOT_RUN",
            message="The card file is not a list of cards, so nothing could be "
                    "compared across the catalogue.",
            evidence=trunc(type(ctx.cards).__name__),
            impact="Every duplicate bank, duplicate product and shared cap in this "
                   "file is unreported.",
            fix="Fix the file's shape first — the schema layer says what is wrong.",
        ))
        return out

    recs = _index(ctx, add)
    if not recs:
        add(Finding(
            severity=WARN,
            code="L7.CHECK_NOT_RUN",
            message=(f"None of the {len(ctx.cards)} entries in the card file could be "
                     f"read as a card, so nothing could be compared across the "
                     f"catalogue."),
            evidence=trunc(f"first entry is a {type(ctx.cards[0]).__name__}"),
            impact="Every duplicate bank, duplicate product and shared cap in this "
                   "file is unreported.",
            fix="Fix the file's shape first — the schema layer says what is wrong.",
        ))
        return out

    for fn in (_issuer_checks, _card_name_checks, _rule_name_checks,
               _cap_pool_checks, _rate_coherence_checks, _earning_shape_checks,
               _label_checks, _summary):
        try:
            fn(recs, ctx, add)
        except Exception as exc:
            add(Finding(
                severity=WARN,
                code="L7.CHECK_ABORTED",
                message=(f"One of the catalogue-wide checks "
                         f"({fn.__name__.strip('_')}) stopped early, so its findings "
                         f"are missing from this run."),
                evidence=trunc(f"{type(exc).__name__}: {exc}"),
                impact="That whole class of cross-card defect is unreported for every "
                       "card in the file.",
                fix="Send this message to whoever maintains the validator.",
            ))
    return out
