"""L2 — vocabulary & enums.

Every enum-ish field in seed/cards.json, checked against WHAT THE APP ENGINE
ACTUALLY ACCEPTS — not against what the value sounds like it should mean.

Two sources of truth, both read directly rather than assumed:

  lib/core/engine/recommendation_engine.dart
      the rule_type switch (no default branch), the channel matcher, the
      base-lane channel tests, the cap-period key builder, the exclusion switch.
  lib/shared/models/credit_card.dart
      the reward_type maths, cap_kind, tier display, network normalisation,
      and every `as String?` cast that decides whether a card loads at all.

Severity policy for this layer, per the brief:
  ERROR  the value makes the engine drop the rule (or the whole card), so a
         real user can never receive it.
  WARN   the value is merely ignored or silently coerced — nothing is dropped,
         but the field is not doing what a human reading the data thinks it does.
  INFO   the vocabulary inventory: every distinct value of every enum field,
         with what the engine does with it, so the founder sees the whole
         vocabulary and not only the violations.

No grandfathering. No baseline. No suppression.
"""
from __future__ import annotations

import collections

from .base import Ctx, Finding, ERROR, WARN, INFO, num, trunc, iso_ok, card_base_pct, reach_scaled

LAYER = "L2 vocabulary & enums"

_EV = 2000  # inventory evidence budget — keep the full vocabulary readable


# --------------------------------------------------------------------------- #
# What the engine accepts, and what it does with each value.
# --------------------------------------------------------------------------- #

# recommendation_engine.dart — init() switch on rule.ruleType. No default branch.
RULE_TYPE_FATE = {
    "base_rate": "indexed as a general base rule",
    "channel_specific": "indexed as a base rule (only channel online/upi/empty can ever fire)",
    "promotional": "indexed as a base rule",
    "merchant_specific": "indexed under its merchant — needs merchant_ref, else not indexed",
    "category_bonus": "indexed under its category — needs category_id, else dropped",
    "conditional": "indexed by merchant, else by category, else as a base rule",
    "threshold_tier": "indexed by category if category_id is set, else as a base rule",
    "portal_bonus": "SKIPPED at index time — can never be recommended",
    "milestone": "SKIPPED at index time — display only, can never be recommended",
}
RULE_TYPE_OK = set(RULE_TYPE_FATE)

# credit_card.dart — rateForRule() / usedAgainstCap() switch on rewardType.
REWARD_TYPE_FATE = {
    "cashback_pct": "rate x 100 = percent back; cap counts rupees of cashback",
    "multiplier": "rate x base_reward_rate x point value; cap counts points",
    "points_per_spend": "rate / reward_unit_spend x point value; cap counts points",
}
REWARD_TYPE_OK = set(REWARD_TYPE_FATE)

# Two different, inconsistent channel matchers.
#   _channelMatches()  — merchant-lane and category-lane rules only.
#   base-lane phases   — literal equality against 'online', then 'upi', then null.
CHANNEL_MATCHER_FATE = {
    "online": "fires only at merchants flagged online",
    "offline": "fires only at merchants NOT flagged online",
    "upi": "fires only on cards with has_rupay_upi = 1",
    "app": "treated exactly like 'online'",
    "portal": "hard-coded to FALSE — this rule can never fire in the merchant or category lane",
}
CHANNEL_MATCHER_OK = set(CHANNEL_MATCHER_FATE)
CHANNEL_BASE_OK = {"online", "upi"}  # plus empty, which means "any channel"

# recommendation_engine.dart — _getSpentForRule() / _getTotalSpendForPeriod().
# Only 'quarter' and 'year' get their own key set; everything else is one month.
PERIOD_FATE = {
    "month": "the current calendar month",
    "cycle": "the current calendar MONTH — a statement cycle that straddles two calendar months is not modelled",
    "quarter": "the three months of the current calendar quarter",
    "year": "the twelve months of the current calendar year",
}
PERIOD_OK = set(PERIOD_FATE)

# credit_card.dart — usedAgainstCap() tests only 'spend'; everything else is 'reward'.
CAP_KIND_FATE = {
    "spend": "cap_amount is a ceiling on eligible SPEND in rupees",
    "reward": "cap_amount is a ceiling on the REWARD earned — rupees for cashback rules, points otherwise",
}
CAP_KIND_OK = set(CAP_KIND_FATE)

# recommendation_engine.dart — _isExcluded() switch. No default branch.
EXCLUSION_TYPE_FATE = {
    "mcc": "compared against the merchant's MCC — this exclusion works",
    "category": "compared against the merchant's category — this exclusion works",
}
EXCLUSION_TYPE_OK = set(EXCLUSION_TYPE_FATE)

# credit_card.dart — _normalizeNetwork(), lower-cased switch, default returns raw.
NETWORK_OK = {"visa", "mastercard", "rupay", "amex", "diners"}

# credit_card.dart — _tierToDisplay() switch, default title-cases the raw string.
CARD_TIER_OK = {
    "super_premium", "premium", "lifestyle", "cashback", "travel",
    "co_branded", "fuel", "entry_level", "business",
}

# smart_redemption_screen.dart — icon and label switches, both default to catalog.
REDEMPTION_CHANNEL_OK = {
    "cashback", "travel", "transfer", "voucher", "statement_credit", "catalog",
}

# credit_card.dart — isCashbackCard uses startsWith('cashback');
# isTravelCard tests intermiles / miles / air_miles exactly.
_POINTS_WORDS = ("point", "coin", "mile", "reward", "cash_point", "_rp", "avios")

# Fields the Dart parser reads with `as String?`. A non-string, non-null value
# there throws a TypeError inside fromOtaJson, which utils.dart catches PER CARD
# — so the entire card silently disappears from the app.
_STRING_CAST_FIELDS = {
    "card": ("network", "card_tier", "reward_currency", "point_currency", "issuer", "card_name"),
    "reward_rules": ("rule_type", "reward_type", "channel", "cap_period", "cap_kind",
                     "threshold_period", "point_currency", "confidence", "merchant_ref",
                     "portal_name", "rule_name"),
    "exclusion_rules": ("exclusion_type", "exclusion_value"),
    "milestone_rules": ("period", "bonus_type", "reward_type", "milestone_name",
                        "rule_name", "bonus_description", "reward_description"),
    "redemption_rules": ("channel_type", "rule_name", "channel_description"),
}


# --------------------------------------------------------------------------- #
# Small local helpers
# --------------------------------------------------------------------------- #
def _rows(ctx: Ctx, block: str):
    """(card_id, row_index, row_dict) for every well-formed row in `block`.

    The pseudo-block 'card' yields each card's own inner object once.
    Malformed rows (strings, nulls, lists) are skipped here on purpose —
    the structural layer owns those.
    """
    try:
        if block == "card":
            for _i, _e, inner, cid in ctx.entries():
                if isinstance(inner, dict):
                    yield cid, 0, inner
            return
        for cid, _inner, j, r in ctx.rules(block):
            if isinstance(r, dict):
                yield cid, j, r
    except Exception:  # a malformed top-level file must not kill the layer
        return


def _sample(ids, n=12) -> str:
    ids = sorted({str(i) for i in ids if i})
    if len(ids) <= n:
        return ", ".join(ids)
    return ", ".join(ids[:n]) + f", and {len(ids) - n} more"


def _norm(v):
    """The value the engine would have matched if someone had been tidy."""
    return v.strip().lower() if isinstance(v, str) else v


def _near_miss(v, accepted) -> str:
    """A note when a value fails only on case or stray whitespace."""
    n = _norm(v)
    if isinstance(v, str) and v != n and n in accepted:
        return f" It would be recognised if it were written exactly '{n}' — it fails on capitalisation or stray spaces alone."
    return ""


def _label(v) -> str:
    if v is None:
        return "(empty)"
    if isinstance(v, str):
        return v if v.strip() else "(blank string)"
    return f"{type(v).__name__}: {trunc(v, 40)}"


def _count(ctx, block, field):
    """Counter of value-label -> rows, plus card ids per label, plus totals."""
    counts = collections.Counter()
    cards = collections.defaultdict(set)
    total = missing = 0
    for cid, _j, row in _rows(ctx, block):
        try:
            total += 1
            if field not in row:
                missing += 1
                continue
            lab = _label(row.get(field))
            counts[lab] += 1
            cards[lab].add(cid)
        except Exception:
            continue
    return counts, cards, total, missing


# --------------------------------------------------------------------------- #
# 1. The inventory — every distinct value of every enum field, INFO
# --------------------------------------------------------------------------- #
_INVENTORY = [
    # block, field, fate table or None, one-line note on what the engine does
    ("card", "network", None,
     "The engine title-cases visa/mastercard/rupay/amex/diners and prints anything else exactly as written. Network has no effect on which card is recommended."),
    ("card", "card_tier", None,
     "Nine tiers get a hand-written label; every other value is title-cased with underscores stripped. Tier has no effect on ranking."),
    ("card", "reward_currency", None,
     "The engine only asks whether this starts with 'cashback', and separately whether it is exactly intermiles/miles/air_miles. Everything else is a label."),
    ("card", "point_currency", None,
     "The card parser reads a card-level point_currency, but no card in this file ships one."),
    ("reward_rules", "rule_type", RULE_TYPE_FATE,
     "This routes the rule into one of three engine indexes. A value the switch does not name is dropped with no log at all."),
    ("reward_rules", "reward_type", REWARD_TYPE_FATE,
     "This decides the reward maths and what unit the cap is counted in. An unnamed value quietly falls back to the card's base rate."),
    ("reward_rules", "channel", None,
     "Two different matchers: merchant and category rules go through a matcher that knows online/offline/upi/app/portal; base-lane rules are compared literally against 'online', then 'upi', then empty."),
    ("reward_rules", "cap_period", PERIOD_FATE,
     "Only 'quarter' and 'year' get their own spend window. Every other value, known or not, is treated as the current calendar month."),
    ("reward_rules", "cap_kind", CAP_KIND_FATE,
     "Only 'spend' is tested; every other value behaves as 'reward'. Absent means 'reward'."),
    ("reward_rules", "cap_unit", None,
     "Nothing in the app reads cap_unit. The unit of a cap is inferred from cap_kind plus reward_type instead."),
    ("reward_rules", "threshold_period", PERIOD_FATE,
     "Same window logic as cap_period: only 'quarter' and 'year' are special, everything else is the current calendar month. Absent means month."),
    ("reward_rules", "confidence", None,
     "Nothing in the app reads confidence. A missing confidence is filled in as 'high', so an unverified rule looks as confident as a verified one."),
    ("reward_rules", "point_currency", None,
     "Rule-level point_currency is shown on the card detail screen and is not used in any calculation."),
    ("exclusion_rules", "exclusion_type", EXCLUSION_TYPE_FATE,
     "The engine only handles 'mcc' and 'category'. Any other type falls out of the switch and excludes nothing."),
    ("milestone_rules", "period", None,
     "Milestones never affect ranking and the app never works out a milestone window, so this string is only shown to the user as-is. There is no accepted vocabulary."),
    ("milestone_rules", "bonus_type", None,
     "Stored and displayed verbatim — there is no vocabulary at all. Absent means the milestone is labelled 'points'."),
    ("milestone_rules", "benefit_type", None,
     "Not a name the milestone parser knows. It reads bonus_type, then reward_type, then gives up and says 'points'."),
    ("redemption_rules", "channel_type", None,
     "The redemption screen has icons and labels for cashback/travel/transfer/voucher/statement_credit/catalog and shows everything else as 'Catalog'."),
    ("redemption_rules", "confidence", None,
     "Nothing in the app reads this, but unlike reward rules it is filled in on every row — proof the convention works when it is used."),
]


def _check_inventory(ctx: Ctx) -> list[Finding]:
    out = []
    for block, field, fate, note in _INVENTORY:
        try:
            counts, _cards, total, missing = _count(ctx, block, field)
            if total == 0:
                continue
            present = sum(counts.values())
            if present == 0:
                out.append(Finding(
                    severity=INFO, code="L2.VOCAB_ABSENT",
                    message=f"No row in {block} carries a '{field}' at all "
                            f"({total:,} rows checked). {note}",
                    block=block, field=field,
                ))
                continue
            parts = []
            for lab, n in counts.most_common():
                if fate is not None:
                    if lab == "(empty)":
                        tail = " -> not set"
                    elif lab in fate:
                        tail = f" -> {fate[lab]}"
                    else:
                        tail = " -> NOT RECOGNISED"
                    parts.append(f"{lab} x{n}{tail}")
                else:
                    parts.append(f"{lab} x{n}")
            distinct = len([k for k in counts if k != "(empty)"])
            blanks = counts.get("(empty)", 0) + missing
            out.append(Finding(
                severity=INFO, code="L2.VOCAB",
                message=f"{block}.{field}: {distinct} distinct value(s) across "
                        f"{present - counts.get('(empty)', 0):,} of {total:,} rows "
                        f"({blanks:,} rows leave it empty). {note}",
                block=block, field=field,
                evidence=trunc(" | ".join(parts), _EV),
            ))
        except Exception as exc:
            out.append(Finding(
                severity=WARN, code="L2.CHECK_INCOMPLETE",
                message=f"Could not inventory {block}.{field}; that part of the "
                        f"vocabulary report is missing.",
                block=block, field=field, evidence=trunc(repr(exc), 120),
                fix="Report this to whoever maintains the validator.",
            ))
    return out


# --------------------------------------------------------------------------- #
# 2. A non-string where the app casts to String — the whole card disappears
# --------------------------------------------------------------------------- #
def _check_string_casts(ctx: Ctx) -> list[Finding]:
    hits = collections.defaultdict(list)  # card_id -> [(block, idx, field, val)]
    for block, fields in _STRING_CAST_FIELDS.items():
        for cid, j, row in _rows(ctx, block):
            for f in fields:
                try:
                    v = row.get(f, None)
                    if v is None or isinstance(v, str):
                        continue
                    hits[cid].append((block, j, f, v))
                except Exception:
                    continue
    out = []
    for cid, items in hits.items():
        blocks = sorted({b for b, _j, _f, _v in items})
        ev = "; ".join(f"{b}[{j}].{f} = {trunc(v, 40)}" for b, j, f, v in items[:6])
        out.append(Finding(
            severity=ERROR, code="L2.ENUM_NOT_A_STRING",
            message=f"{cid}: {len(items)} field(s) that the app reads as text hold "
                    f"a number, list or object instead. The app cannot read this card "
                    f"and skips the whole thing.",
            card_id=cid, block=", ".join(blocks), evidence=trunc(ev, 300),
            impact="This card vanishes from the app entirely — it never appears in "
                   "the picker and can never be recommended to anyone.",
            fix="Store the value as a quoted string, or remove the key.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 3. reward_rules.rule_type
# --------------------------------------------------------------------------- #
def _check_rule_type(ctx: Ctx) -> list[Finding]:
    bad = collections.defaultdict(list)
    missing = collections.defaultdict(list)
    for cid, j, row in _rows(ctx, "reward_rules"):
        try:
            v = row.get("rule_type", None)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing[cid].append((j, row.get("rule_name")))
            elif isinstance(v, str) and v not in RULE_TYPE_OK:
                bad[cid].append((j, v, row.get("rule_name")))
        except Exception:
            continue
    out = []
    for cid, items in bad.items():
        vals = sorted({v for _j, v, _n in items})
        note = "".join(_near_miss(v, RULE_TYPE_OK) for v in vals)
        out.append(Finding(
            severity=ERROR, code="L2.RULE_TYPE_UNKNOWN",
            message=f"{cid}: {len(items)} reward rule(s) use a rule_type the app has "
                    f"never heard of ({', '.join(vals)}). The app loads the rule, shows "
                    f"it on the card detail page, and then files it nowhere — so it can "
                    f"never win a recommendation." + note,
            card_id=cid, block="reward_rules", field="rule_type",
            index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {v} — {n}" for j, v, n in items[:5]), 260),
            impact="The reward looks real in the data and on the card page, but no user "
                   "is ever told to use this card for it. Nothing is logged either.",
            fix="Change it to one of: " + ", ".join(sorted(RULE_TYPE_OK)) + ".",
        ))
    for cid, items in missing.items():
        out.append(Finding(
            severity=WARN, code="L2.RULE_TYPE_MISSING",
            message=f"{cid}: {len(items)} reward rule(s) have no rule_type. The app "
                    f"assumes 'base_rate', which means the rule competes as the card's "
                    f"everyday rate rather than as a bonus.",
            card_id=cid, block="reward_rules", field="rule_type", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {n}" for j, n in items[:5]), 260),
            impact="A category or merchant bonus silently becomes an all-purchases rate.",
            fix="Set rule_type explicitly on every reward rule.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 4. reward_rules.reward_type
# --------------------------------------------------------------------------- #
def _check_reward_type(ctx: Ctx) -> list[Finding]:
    bad = collections.defaultdict(list)
    missing = collections.defaultdict(list)
    for cid, j, row in _rows(ctx, "reward_rules"):
        try:
            v = row.get("reward_type", None)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing[cid].append((j, row.get("rule_name")))
            elif isinstance(v, str) and v not in REWARD_TYPE_OK:
                bad[cid].append((j, v, row.get("rule_name")))
        except Exception:
            continue
    out = []
    for cid, items in bad.items():
        vals = sorted({v for _j, v, _n in items})
        note = "".join(_near_miss(v, REWARD_TYPE_OK) for v in vals)
        out.append(Finding(
            severity=WARN, code="L2.REWARD_TYPE_UNKNOWN",
            message=f"{cid}: {len(items)} reward rule(s) use a reward_type the app does "
                    f"not know ({', '.join(vals)}). The app ignores the rule's own rate "
                    f"and quietly shows the card's ordinary base rate instead." + note,
            card_id=cid, block="reward_rules", field="reward_type", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {v} — {n}" for j, v, n in items[:5]), 260),
            impact="The user is shown a number that has nothing to do with this offer, "
                   "and it looks perfectly normal.",
            fix="Use one of: " + ", ".join(sorted(REWARD_TYPE_OK)) + ".",
        ))
    for cid, items in missing.items():
        out.append(Finding(
            severity=WARN, code="L2.REWARD_TYPE_MISSING",
            message=f"{cid}: {len(items)} reward rule(s) have no reward_type. The app "
                    f"assumes 'points_per_spend' and divides the rate by reward_unit_spend, "
                    f"which is almost certainly not what a cashback rule means.",
            card_id=cid, block="reward_rules", field="reward_type", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {n}" for j, n in items[:5]), 260),
            impact="A cashback percentage can be shown a hundred times too small.",
            fix="Set reward_type explicitly on every reward rule.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 5. reward_rules.channel — the two-matcher trap
# --------------------------------------------------------------------------- #
def _lane(rule_type, row) -> str:
    """Which of the engine's two channel matchers this rule will meet."""
    rt = rule_type if isinstance(rule_type, str) else "base_rate"
    if rt in ("portal_bonus", "milestone"):
        return "dropped"          # never indexed at all — not this check's business
    if rt == "merchant_specific":
        return "matcher"
    if rt == "category_bonus":
        if row.get("category_id") is None and row.get("conditions_json") is not None:
            return "base"          # demoted into the base lane
        return "matcher"
    if rt == "conditional":
        return "matcher" if (row.get("merchant_ref") is not None
                             or row.get("category_id") is not None) else "base"
    if rt == "threshold_tier":
        return "matcher" if row.get("category_id") is not None else "base"
    if rt in ("base_rate", "channel_specific", "promotional"):
        return "base"
    return "unknown"               # unknown rule_type — already reported above


def _check_channel(ctx: Ctx) -> list[Finding]:
    novocab = collections.defaultdict(list)   # value the engine knows nowhere
    wronglane = collections.defaultdict(list) # value known, wrong lane for this rule_type
    portal = collections.defaultdict(list)    # 'portal' in the matcher lane: always false
    for cid, j, row in _rows(ctx, "reward_rules"):
        try:
            ch = row.get("channel", None)
            if ch is None or not isinstance(ch, str) or not ch.strip():
                continue
            rt = row.get("rule_type")
            lane = _lane(rt, row)
            if lane in ("dropped", "unknown"):
                continue
            name = row.get("rule_name")
            if lane == "matcher":
                if ch == "portal":
                    portal[cid].append((j, ch, rt, name))
                elif ch not in CHANNEL_MATCHER_OK:
                    novocab[cid].append((j, ch, rt, name))
            else:  # base lane — literal equality only
                if ch in CHANNEL_BASE_OK:
                    continue
                if ch in CHANNEL_MATCHER_OK:
                    wronglane[cid].append((j, ch, rt, name))
                else:
                    novocab[cid].append((j, ch, rt, name))
        except Exception:
            continue

    out = []
    for cid, items in novocab.items():
        vals = sorted({c for _j, c, _r, _n in items})
        out.append(Finding(
            severity=ERROR, code="L2.CHANNEL_NOT_IN_VOCAB",
            message=f"{cid}: {len(items)} reward rule(s) are restricted to a channel the "
                    f"app has no idea about ({', '.join(vals)}). The check for that channel "
                    f"always comes back false, so these rules can never fire for anyone.",
            card_id=cid, block="reward_rules", field="channel", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {c} ({r}) — {n}" for j, c, r, n in items[:5]), 300),
            impact="The card is quietly under-rated: a real accelerated rate exists in the "
                   "data and the user is never shown it. 'international' is the common case — "
                   "the app has no international channel; it handles foreign spend only by "
                   "subtracting the card's forex markup.",
            fix="For 'international', delete the channel and let forex_markup_pct do the work, "
                "or record the offer as a normal category rule. Otherwise use one of: "
                "online, offline, upi, app.",
        ))
    for cid, items in wronglane.items():
        vals = sorted({c for _j, c, _r, _n in items})
        out.append(Finding(
            severity=ERROR, code="L2.CHANNEL_WRONG_LANE",
            message=f"{cid}: {len(items)} reward rule(s) use channel '{', '.join(vals)}', "
                    f"which the app does understand — but not for this kind of rule. Rules of "
                    f"type base_rate / channel_specific / promotional are only ever tested "
                    f"against 'online', 'upi' or no channel at all, so these never fire.",
            card_id=cid, block="reward_rules", field="channel", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {c} ({r}) — {n}" for j, c, r, n in items[:5]), 300),
            impact="The rate exists in the data and on the card page but never reaches a "
                   "recommendation, so this card looks worse than it is.",
            fix="Either re-file the rule as a category_bonus or merchant_specific rule, where "
                "'offline' is honoured, or drop the channel restriction.",
        ))
    for cid, items in portal.items():
        out.append(Finding(
            severity=ERROR, code="L2.CHANNEL_PORTAL_ALWAYS_FALSE",
            message=f"{cid}: {len(items)} reward rule(s) use channel 'portal'. The app's "
                    f"channel check for 'portal' is hard-coded to return false, so the rule "
                    f"is loaded and then never fires.",
            card_id=cid, block="reward_rules", field="channel", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {c} ({r}) — {n}" for j, c, r, n in items[:5]), 300),
            impact="Shopping-portal offers are shown on the card page but never in a "
                   "recommendation.",
            fix="Portal offers need app work before they can be recommended; until then do "
                "not rely on this rule reaching a user.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 6. cap_period / threshold_period / cap_kind / cap_unit
# --------------------------------------------------------------------------- #
def _check_periods(ctx: Ctx) -> list[Finding]:
    cap = collections.defaultdict(list)
    thr = collections.defaultdict(list)
    for cid, j, row in _rows(ctx, "reward_rules"):
        try:
            v = row.get("cap_period", None)
            if isinstance(v, str) and v.strip() and v not in PERIOD_OK:
                cap[cid].append((j, v, row.get("rule_name")))
            t = row.get("threshold_period", None)
            if isinstance(t, str) and t.strip() and t not in PERIOD_OK:
                thr[cid].append((j, t, row.get("rule_name")))
        except Exception:
            continue
    out = []
    for cid, items in cap.items():
        vals = sorted({v for _j, v, _n in items})
        out.append(Finding(
            severity=WARN, code="L2.CAP_PERIOD_COERCED",
            message=f"{cid}: {len(items)} cap(s) are written for a window the app cannot "
                    f"count ({', '.join(vals)}). The app silently counts them over the "
                    f"current calendar month instead.",
            card_id=cid, block="reward_rules", field="cap_period", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {v} — {n}" for j, v, n in items[:5]), 300),
            impact="A per-transaction or per-day ceiling becomes a whole-month ceiling, so "
                   "the app tells the user the bonus is used up when it is not — or the other "
                   "way round.",
            fix="Only month, cycle, quarter and year are counted. Re-express the cap in one "
                "of those, or leave cap_amount off if the real limit cannot be expressed.",
        ))
    for cid, items in thr.items():
        vals = sorted({v for _j, v, _n in items})
        out.append(Finding(
            severity=WARN, code="L2.THRESHOLD_PERIOD_COERCED",
            message=f"{cid}: {len(items)} spend-threshold window(s) use a period the app "
                    f"cannot count ({', '.join(vals)}); it falls back to the current "
                    f"calendar month.",
            card_id=cid, block="reward_rules", field="threshold_period", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {v} — {n}" for j, v, n in items[:5]), 300),
            impact="A tiered card switches to its higher rate at the wrong moment.",
            fix="Use month, quarter or year.",
        ))
    return out


def _check_cap_kind_and_unit(ctx: Ctx) -> list[Finding]:
    bad = collections.defaultdict(list)
    unit = collections.defaultdict(list)
    for cid, j, row in _rows(ctx, "reward_rules"):
        try:
            v = row.get("cap_kind", None)
            if isinstance(v, str) and v.strip() and v not in CAP_KIND_OK:
                bad[cid].append((j, v, row.get("rule_name")))
            u = row.get("cap_unit", None)
            if u is not None:
                unit[cid].append((j, u, row.get("cap_kind"), row.get("reward_type"),
                                  row.get("rule_name")))
        except Exception:
            continue
    out = []
    for cid, items in bad.items():
        vals = sorted({v for _j, v, _n in items})
        out.append(Finding(
            severity=WARN, code="L2.CAP_KIND_UNKNOWN",
            message=f"{cid}: {len(items)} rule(s) use a cap_kind the app does not test for "
                    f"({', '.join(vals)}). Only 'spend' is recognised; everything else is "
                    f"treated as a cap on the reward earned.",
            card_id=cid, block="reward_rules", field="cap_kind", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {v} — {n}" for j, v, n in items[:5]), 300),
            impact="A ceiling meant to limit spending is applied to the reward instead, so "
                   "the cap bites at completely the wrong point.",
            fix="Use 'spend' or 'reward'.",
        ))
    for cid, items in unit.items():
        detail = []
        for j, u, kind, rtype, name in items:
            k = kind if isinstance(kind, str) else "reward (default)"
            if k == "spend":
                inferred = "rupees of spend"
            elif rtype == "cashback_pct":
                inferred = "rupees of cashback"
            elif rtype in ("points_per_spend", "multiplier"):
                inferred = "points"
            else:
                inferred = "rupees (fallback)"
            agrees = (str(u).strip().lower() in ("inr", "rupees", "rs", "₹")) == \
                     inferred.startswith("rupees")
            detail.append(f"[{j}] says '{u}', app assumes {inferred}"
                          f"{'' if agrees else ' — THESE DISAGREE'} ({trunc(name, 40)})")
        disagree = any("DISAGREE" in d for d in detail)
        out.append(Finding(
            severity=WARN, code="L2.CAP_UNIT_NOT_READ",
            message=f"{cid}: {len(items)} rule(s) declare a cap_unit. Nothing in the app "
                    f"reads that field — the unit of a cap is worked out from cap_kind plus "
                    f"reward_type instead."
                    + (" On this card the declared unit and the app's assumption disagree."
                       if disagree else " Here the two happen to agree, so nothing is wrong today."),
            card_id=cid, block="reward_rules", field="cap_unit", index=items[0][0],
            evidence=trunc("; ".join(detail[:5]), 300),
            impact="Anyone reading the data trusts cap_unit; the app never does. Where they "
                   "disagree, the cap is counted in the wrong currency.",
            fix="Do not rely on cap_unit. Make cap_kind and reward_type say the truth, and "
                "remember caps are stored in the issuer's own unit — points for a points card.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 7. exclusion_rules.exclusion_type — the inert-exclusion problem
# --------------------------------------------------------------------------- #
def _check_exclusion_type(ctx: Ctx) -> list[Finding]:
    inert = collections.defaultdict(collections.Counter)
    inert_idx = {}
    inert_ev = collections.defaultdict(list)
    empty = collections.defaultdict(list)
    totals = collections.Counter()
    for cid, j, row in _rows(ctx, "exclusion_rules"):
        try:
            totals[cid] += 1
            v = row.get("exclusion_type", None)
            if not isinstance(v, str):
                if v is None:
                    empty[cid].append((j, row.get("exclusion_value")))
                continue          # non-string: reported once, as L2.ENUM_NOT_A_STRING
            if not v.strip():
                empty[cid].append((j, row.get("exclusion_value")))
                continue
            if v in EXCLUSION_TYPE_OK:
                continue
            inert[cid][v] += 1
            inert_idx.setdefault(cid, j)
            inert_ev[cid].append(f"[{j}] {v}: {trunc(row.get('exclusion_value'), 45)}")
        except Exception:
            continue
    out = []
    for cid, kinds in inert.items():
        n = sum(kinds.values())
        breakdown = ", ".join(f"{k} x{c}" for k, c in kinds.most_common())
        note = "".join(_near_miss(k, EXCLUSION_TYPE_OK) for k in kinds)
        out.append(Finding(
            severity=ERROR, code="L2.EXCLUSION_TYPE_INERT",
            message=f"{cid}: {n} of {totals[cid]} exclusions use a type the app never checks "
                    f"({breakdown}). The app only handles 'mcc' and 'category', so these "
                    f"exclusions exclude nothing." + note,
            card_id=cid, block="exclusion_rules", field="exclusion_type",
            index=inert_idx.get(cid),
            evidence=trunc("; ".join(inert_ev[cid][:5]), 320),
            impact="The user is told to use this card on a purchase the issuer earns nothing "
                   "on — rent, fuel, wallet loads, government payments. They swipe, and get "
                   "no reward at all.",
            fix="Re-express each one as an 'mcc' exclusion (the merchant category code) or a "
                "'category' exclusion (a slug from the app's categories.json). If neither fits, "
                "the app cannot enforce it and it should not be recorded as an exclusion.",
        ))
    for cid, items in empty.items():
        out.append(Finding(
            severity=ERROR, code="L2.EXCLUSION_TYPE_EMPTY",
            message=f"{cid}: {len(items)} exclusion(s) have no exclusion_type. The app reads "
                    f"that as an empty string, which matches nothing, so the exclusion is dead.",
            card_id=cid, block="exclusion_rules", field="exclusion_type", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] value={trunc(v, 40)}" for j, v in items[:5]), 300),
            impact="Same as above: the user is recommended a card for a purchase it excludes.",
            fix="Set exclusion_type to 'mcc' or 'category'.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 8. card.network — special-cased: 'unknown' counted separately
# --------------------------------------------------------------------------- #
def _check_network(ctx: Ctx) -> list[Finding]:
    unknown, missing = [], []
    odd = collections.defaultdict(list)
    for cid, _j, card in _rows(ctx, "card"):
        try:
            v = card.get("network", None)
            if v is None or (isinstance(v, str) and not v.strip()):
                missing.append(cid)
                continue
            if not isinstance(v, str):
                continue                       # reported by L2.ENUM_NOT_A_STRING
            low = v.strip().lower()
            if low == "unknown":
                unknown.append(cid)
            elif low not in NETWORK_OK:
                odd[v].append(cid)
        except Exception:
            continue
    out = []
    total = sum(1 for _ in _rows(ctx, "card"))
    if unknown:
        pct = (len(unknown) / total * 100) if total else 0
        out.append(Finding(
            severity=WARN, code="L2.NETWORK_UNKNOWN",
            message=f"{len(unknown)} of {total} cards ({pct:.0f}%) have the word 'unknown' "
                    f"where the card network should be. The app prints it exactly as written, "
                    f"so the user reads the word 'unknown' on the card.",
            block="card", field="network",
            evidence=trunc(_sample(unknown, 15), 400),
            impact="Looks unfinished on more than half the catalogue, and nobody can filter by "
                   "Visa or RuPay. It does not change which card is recommended — RuPay UPI "
                   "eligibility comes from has_rupay_upi, not from this field.",
            fix="Fill in visa, mastercard, rupay, amex or diners from the issuer's page. Where "
                "a card genuinely ships on several networks, decide one house style first — "
                "the app has no vocabulary for a combined value.",
        ))
    if missing:
        out.append(Finding(
            severity=WARN, code="L2.NETWORK_MISSING",
            message=f"{len(missing)} card(s) have no network at all. The app fills in 'Visa' "
                    f"by default, which may simply be untrue.",
            block="card", field="network", evidence=trunc(_sample(missing, 15), 400),
            impact="The user is shown a network the card may not be on.",
            fix="Set network explicitly on every card.",
        ))
    for v, cids in sorted(odd.items(), key=lambda kv: -len(kv[1])):
        out.append(Finding(
            severity=WARN, code="L2.NETWORK_UNRECOGNISED",
            message=f"network '{trunc(v, 60)}' on {len(cids)} card(s) is not one of the five "
                    f"the app knows (visa, mastercard, rupay, amex, diners), so it is printed "
                    f"raw — punctuation, capitals, prose and all." + _near_miss(v, NETWORK_OK),
            block="card", field="network", card_id=cids[0] if len(cids) == 1 else None,
            evidence=trunc(_sample(cids, 15), 300),
            impact="The user sees a machine-looking string like 'visa; rupay' or a whole "
                   "sentence where a single network name belongs.",
            fix="Pick the one network this card is actually issued on, or agree a house style "
                "for multi-network cards and teach the app to display it.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 9. card.card_tier — special-cased, reported by value
# --------------------------------------------------------------------------- #
def _check_card_tier(ctx: Ctx) -> list[Finding]:
    odd = collections.defaultdict(list)
    empty = []
    for cid, _j, card in _rows(ctx, "card"):
        try:
            v = card.get("card_tier", None)
            if v is None or (isinstance(v, str) and not v.strip()):
                empty.append(cid)
            elif isinstance(v, str) and v not in CARD_TIER_OK:
                odd[v].append(cid)
        except Exception:
            continue
    out = []
    total = sum(1 for _ in _rows(ctx, "card"))
    for v, cids in sorted(odd.items(), key=lambda kv: -len(kv[1])):
        shown = v[0].upper() + v[1:].replace("_", " ") if v else "Other"
        out.append(Finding(
            severity=WARN, code="L2.CARD_TIER_UNRECOGNISED",
            message=f"card_tier '{v}' is used by {len(cids)} of {total} cards but is not one "
                    f"of the nine tiers the app has a label for. The app falls back to "
                    f"tidying up the raw text, so the user sees \"{shown}\"."
                    + _near_miss(v, CARD_TIER_OK),
            block="card", field="card_tier", card_id=cids[0] if len(cids) == 1 else None,
            evidence=trunc(_sample(cids, 15), 400),
            impact="Cosmetic on the card itself — tier never changes which card is "
                   "recommended. But the app also has two shortcuts that key off this field "
                   "(tier 'cashback' and tier 'travel'), and neither can ever fire while the "
                   "data uses names the app does not recognise.",
            fix="Either map the data onto the app's nine tiers (entry -> entry_level, "
                "ultra_premium -> super_premium, and decide where mid_range belongs), or add "
                "these names to the app's tier list. Do one or the other, not both.",
        ))
    if empty:
        out.append(Finding(
            severity=WARN, code="L2.CARD_TIER_EMPTY",
            message=f"{len(empty)} card(s) have no card_tier; the app labels them 'Other'.",
            block="card", field="card_tier", evidence=trunc(_sample(empty, 15), 300),
            impact="The card shows a meaningless tier label.",
            fix="Set card_tier on every card.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 10. card.reward_currency — special-cased
# --------------------------------------------------------------------------- #
def _classify_currency(v: str) -> str:
    low = v.strip().lower()
    if low.startswith("cashback") or low in ("cash_back", "cash back", "inr"):
        return "cashback"
    if any(w in low for w in _POINTS_WORDS):
        return "points"
    return "neither"


def _check_reward_currency(ctx: Ctx) -> list[Finding]:
    kinds = collections.defaultdict(lambda: collections.defaultdict(list))
    empty = []
    for cid, _j, card in _rows(ctx, "card"):
        try:
            v = card.get("reward_currency", None)
            if v is None or (isinstance(v, str) and not v.strip()):
                empty.append(cid)
                continue
            if not isinstance(v, str):
                continue
            kinds[_classify_currency(v)][v].append(cid)
        except Exception:
            continue
    out = []
    total = sum(1 for _ in _rows(ctx, "card"))

    # 10a. neither a cashback variant nor a points variant — the brief's special case
    for v, cids in sorted(kinds["neither"].items(), key=lambda kv: -len(kv[1])):
        out.append(Finding(
            severity=WARN, code="L2.REWARD_CURRENCY_UNCLASSIFIED",
            message=f"reward_currency '{v}' on {len(cids)} card(s) is neither a cashback "
                    f"variant nor a points variant, so the app cannot tell what this card "
                    f"pays out in.",
            block="card", field="reward_currency",
            card_id=cids[0] if len(cids) == 1 else None,
            evidence=trunc(_sample(cids, 15), 300),
            impact="The app decides 'is this a cashback card' by checking whether this word "
                   "starts with 'cashback'. A word it cannot classify is treated as points, "
                   "and the reward is then shown with an invented rupee value per point.",
            fix="Rename it so it either starts with 'cashback' or clearly names a point, coin "
                "or mile currency.",
        ))

    # 10b. the exact-match trap: 'cashback_inr' passes the ranking test but fails the
    #      redemption screen's `== 'cashback'` test.
    for v, cids in sorted(kinds["cashback"].items(), key=lambda kv: -len(kv[1])):
        if v == "cashback":
            continue
        out.append(Finding(
            severity=WARN, code="L2.REWARD_CURRENCY_CASHBACK_ALIAS",
            message=f"{len(cids)} card(s) use reward_currency '{v}'. The recommendation "
                    f"engine accepts it (it only checks the word starts with 'cashback'), but "
                    f"the redemption adviser tests for exactly 'cashback' and so does not "
                    f"recognise these as cashback cards.",
            block="card", field="reward_currency", evidence=trunc(_sample(cids, 15), 400),
            impact="On the redemption screen a plain cashback card is given invented "
                   "points-redemption options instead of being told the cash is already cash.",
            fix="Either settle on one spelling across the app and the data, or make the "
                "redemption adviser use the same 'starts with cashback' test the engine uses. "
                "Changing the data alone risks breaking the engine's own test — check both.",
        ))
    if empty:
        out.append(Finding(
            severity=WARN, code="L2.REWARD_CURRENCY_EMPTY",
            message=f"{len(empty)} card(s) have no reward_currency; the app assumes "
                    f"'reward_points'.",
            block="card", field="reward_currency", evidence=trunc(_sample(empty, 15), 300),
            impact="A cashback card can be presented as if it paid points.",
            fix="Set reward_currency on every card.",
        ))
    # 10c. the inventory of how each value classifies, so the split is visible
    if kinds:
        parts = []
        for k in ("cashback", "points", "neither"):
            for v, cids in sorted(kinds[k].items(), key=lambda kv: -len(kv[1])):
                parts.append(f"{v} x{len(cids)} = {k}")
        out.append(Finding(
            severity=INFO, code="L2.REWARD_CURRENCY_SPLIT",
            message=f"Of {total} cards, {sum(len(c) for c in kinds['cashback'].values())} pay "
                    f"cashback, {sum(len(c) for c in kinds['points'].values())} pay some kind "
                    f"of point, coin or mile, and "
                    f"{sum(len(c) for c in kinds['neither'].values())} use a word that is "
                    f"neither.",
            block="card", field="reward_currency",
            evidence=trunc(" | ".join(parts), _EV),
        ))
    return out


# --------------------------------------------------------------------------- #
# 11. reward_rules.confidence + point_currency — parsed, never read
# --------------------------------------------------------------------------- #
def _check_ignored_rule_fields(ctx: Ctx) -> list[Finding]:
    out = []
    conf = collections.Counter()
    conf_cards = set()
    pc = collections.defaultdict(list)
    total = 0
    for cid, j, row in _rows(ctx, "reward_rules"):
        try:
            total += 1
            if "confidence" in row:
                conf[_label(row.get("confidence"))] += 1
                conf_cards.add(cid)
            v = row.get("point_currency", None)
            if isinstance(v, str) and v.strip():
                pc[cid].append((j, v, row.get("rule_name")))
        except Exception:
            continue
    if total:
        present = sum(conf.values())
        out.append(Finding(
            severity=WARN, code="L2.CONFIDENCE_NOT_READ",
            message=f"confidence is set on {present:,} of {total:,} reward rules "
                    f"(values: {', '.join(f'{k} x{v}' for k, v in conf.most_common()) or 'none'}) "
                    f"and nothing in the app ever reads it. A missing confidence is filled in "
                    f"as 'high', so the {total - present:,} rules that say nothing about their "
                    f"own reliability are presented as the most reliable of all.",
            block="reward_rules", field="confidence",
            evidence=trunc("carried on: " + _sample(conf_cards, 12), 300),
            impact="Nobody — user or founder — can tell a rate someone verified at the issuer "
                   "from a rate nobody has ever checked.",
            fix="Two separate jobs: fill confidence in on every rule so the data is honest, "
                "and give the app something to do with it (hide, badge or down-rank low "
                "confidence). Filling it in alone changes nothing a user sees.",
        ))
    for cid, items in pc.items():
        vals = sorted({v for _j, v, _n in items})
        out.append(Finding(
            severity=WARN, code="L2.POINT_CURRENCY_DISPLAY_ONLY",
            message=f"{cid}: {len(items)} rule(s) name a point currency ({', '.join(vals)}). "
                    f"This is printed on the card detail screen exactly as typed and is used "
                    f"in no calculation; the card-level point_currency the app also reads is "
                    f"not set on any card in this file.",
            card_id=cid, block="reward_rules", field="point_currency", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {v} — {n}" for j, v, n in items[:5]), 300),
            impact="Free text with capitals and spaces sits next to machine-style names like "
                   "reward_points, so the card page reads inconsistently.",
            fix="Agree one style. If the currency name matters to the user, set it on the card, "
                "not on individual rules.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 12. milestone period / bonus_type / benefit_type
# --------------------------------------------------------------------------- #
def _check_milestones(ctx: Ctx) -> list[Finding]:
    freeperiod = collections.defaultdict(list)
    benefit = collections.defaultdict(list)
    spell = collections.defaultdict(lambda: collections.defaultdict(list))
    for cid, j, row in _rows(ctx, "milestone_rules"):
        try:
            p = row.get("period", None)
            if isinstance(p, str) and p.strip() and p not in PERIOD_OK:
                freeperiod[cid].append((j, p, row.get("milestone_name")))
            bt = row.get("bonus_type", None)
            rt = row.get("reward_type", None)
            if bt is None and rt is None and ("benefit_type" in row or "benefit_value" in row):
                benefit[cid].append((j, row.get("benefit_type"), row.get("benefit_value"),
                                     row.get("milestone_name")))
            if isinstance(bt, str) and bt.strip():
                key = " ".join(bt.lower().replace("_", " ").replace("-", " ").split())
                spell[key][bt].append(cid)
        except Exception:
            continue
    out = []
    for cid, items in freeperiod.items():
        out.append(Finding(
            severity=WARN, code="L2.MILESTONE_PERIOD_FREETEXT",
            message=f"{cid}: {len(items)} milestone(s) describe their period in free text "
                    f"rather than month/cycle/quarter/year. The app shows the text as typed "
                    f"and cannot work out when the milestone resets.",
            card_id=cid, block="milestone_rules", field="period", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {trunc(p, 45)}" for j, p, _n in items[:5]), 320),
            impact="Milestones never affect which card is recommended, so nothing is "
                   "mis-ranked — but the user reads a sentence where a period should be, and "
                   "no progress can ever be tracked against it.",
            fix="Put the window in period (month / cycle / quarter / year) and keep the "
                "wording in the milestone's description.",
        ))
    for cid, items in benefit.items():
        out.append(Finding(
            severity=WARN, code="L2.MILESTONE_BENEFIT_TYPE_NOT_READ",
            message=f"{cid}: {len(items)} milestone(s) describe their reward under "
                    f"benefit_type / benefit_value. The app only looks for bonus_type or "
                    f"reward_type, so the amount is thrown away and the milestone is labelled "
                    f"'points' whatever it really is.",
            card_id=cid, block="milestone_rules", field="benefit_type", index=items[0][0],
            evidence=trunc("; ".join(f"[{j}] {bt} = {bv} — {trunc(n, 35)}"
                                     for j, bt, bv, n in items[:5]), 320),
            impact="The user sees a milestone with no amount attached where a real benefit "
                   "exists, so a spend target looks pointless.",
            fix="Rename benefit_type to bonus_type and benefit_value to bonus_value. Same "
                "rows, same content, names the app already reads.",
        ))
    for _key, spellings in spell.items():
        if len(spellings) < 2:
            continue
        allcards = sorted({c for cids in spellings.values() for c in cids})
        detail = ", ".join(f"'{s}' x{len(c)}" for s, c in
                           sorted(spellings.items(), key=lambda kv: -len(kv[1])))
        out.append(Finding(
            severity=WARN, code="L2.MILESTONE_BONUS_TYPE_SPELLING_SPLIT",
            message=f"The same milestone reward is spelt {len(spellings)} different ways "
                    f"({detail}) across {len(allcards)} card(s). bonus_type has no accepted "
                    f"vocabulary at all — whatever is typed is shown to the user verbatim.",
            block="milestone_rules", field="bonus_type",
            evidence=trunc(_sample(allcards, 15), 400),
            impact="The same benefit reads differently on two cards side by side, and nothing "
                   "can ever be counted or grouped by benefit type.",
            fix="Pick one spelling per benefit and use it everywhere. Only underscore-versus-"
                "space differences are reported here; wordier variants of the same benefit "
                "need a human eye, not a matching rule.",
        ))
    return out


# --------------------------------------------------------------------------- #
# 13. redemption_rules.channel_type
# --------------------------------------------------------------------------- #
def _check_redemption_channel(ctx: Ctx) -> list[Finding]:
    odd = collections.defaultdict(list)
    empty = collections.defaultdict(list)
    total = 0
    for cid, j, row in _rows(ctx, "redemption_rules"):
        try:
            total += 1
            v = row.get("channel_type", None)
            if v is None or (isinstance(v, str) and not v.strip()):
                empty[cid].append(j)
            elif isinstance(v, str) and v not in REDEMPTION_CHANNEL_OK:
                odd[v].append(cid)
        except Exception:
            continue
    out = []
    # Both of these describe what the redemption SCREEN shows. It shows none of
    # it: the app builds that screen from `redemption_channels`, and this file
    # writes `redemption_rules`, so no row here reaches it (see
    # L6.REDEMPTION_BLOCK_NEVER_READ). Severity follows reach — the check is
    # unchanged and still finds every unmapped channel, but it stops claiming a
    # user is being shown the wrong icon today.
    reaches = ctx.block_reaches_app("redemption_rules")
    for v, cids in sorted(odd.items(), key=lambda kv: -len(kv[1])):
        out.append(reach_scaled(
            reaches, WARN, INFO,
            code="L2.REDEMPTION_CHANNEL_UNMAPPED",
            message=f"channel_type '{v}' appears on {len(cids)} redemption row(s) across "
                    f"{len(set(cids))} card(s) and is not one of the six the redemption "
                    f"screen has an icon and a label for. It is shown as 'Catalog' with a "
                    f"shopping-bag icon." + _near_miss(v, REDEMPTION_CHANNEL_OK),
            block="redemption_rules", field="channel_type",
            evidence=trunc(_sample(set(cids), 15), 400),
            live_impact="A transfer to an airline partner and a merchandise catalogue look "
                        "identical on screen, which is exactly the choice the redemption screen "
                        "exists to help the user make. Redemption data never affects ranking, so "
                        "no recommendation is wrong because of this.",
            dead_impact="Nobody sees this today — no redemption row reaches the app at all. "
                        "Worth fixing at the same time as the key mismatch, not before it.",
            fix="Either map these onto the app's six (partner_transfer -> transfer, "
                "gift_card and voucher_catalog -> voucher, merchandise and other -> catalog), "
                "or add the missing labels and icons to the redemption screen.",
        ))
    for cid, idxs in empty.items():
        out.append(reach_scaled(
            reaches, WARN, INFO,
            code="L2.REDEMPTION_CHANNEL_EMPTY",
            message=f"{cid}: {len(idxs)} redemption row(s) have no channel_type; the app "
                    f"labels them 'Catalog' by default.",
            card_id=cid, block="redemption_rules", field="channel_type", index=idxs[0],
            live_impact="The user cannot tell what kind of redemption this is.",
            dead_impact="Nobody sees this today — no redemption row reaches the app at all.",
            fix="Set channel_type on every redemption row.",
        ))
    return out


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
_CHECKS = (
    ("inventory", _check_inventory),
    ("string casts", _check_string_casts),
    ("rule_type", _check_rule_type),
    ("reward_type", _check_reward_type),
    ("channel", _check_channel),
    ("cap/threshold period", _check_periods),
    ("cap_kind and cap_unit", _check_cap_kind_and_unit),
    ("exclusion_type", _check_exclusion_type),
    ("network", _check_network),
    ("card_tier", _check_card_tier),
    ("reward_currency", _check_reward_currency),
    ("ignored rule fields", _check_ignored_rule_fields),
    ("milestones", _check_milestones),
    ("redemption channel_type", _check_redemption_channel),
)


def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    for name, fn in _CHECKS:
        try:
            out.extend(fn(ctx))
        except Exception as exc:                     # never let one check kill the layer
            out.append(Finding(
                severity=WARN, code="L2.CHECK_INCOMPLETE",
                message=f"The '{name}' vocabulary check could not finish, so anything it "
                        f"would have found is missing from this report.",
                evidence=trunc(f"{type(exc).__name__}: {exc}", 200),
                impact="This report is incomplete — treat a clean result for that field as "
                       "unproven, not as a pass.",
                fix="Report this to whoever maintains the validator.",
            ))
    return out
