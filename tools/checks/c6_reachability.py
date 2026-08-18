"""L6 — engine reachability.

"Is this rule dead code?"

Every other layer asks whether a row is well-formed or whether its number is
plausible. This one asks the only question a user actually feels:

    WILL THE APP'S RECOMMENDATION ENGINE EVER FIRE THIS ROW?

A row can be perfectly valid JSON, carry a perfectly sane number, pass every
other gate — and still never execute, because the engine either never indexes
it, never matches its channel, always picks a different rule first, or reads a
different key name than the one the row is stored under.

Authority for every claim below is the shipping engine on `nous/master`:
    lib/core/engine/recommendation_engine.dart   (917 lines)
    lib/shared/models/credit_card.dart           (1021 lines)
    lib/core/utils.dart                          (load + per-card try/catch)

Line references in the comments are against that branch, not the detached
`db0f6de` checkout that happens to be on disk.

Nothing here is grandfathered and nothing reads tools/rate_baseline.json.
"""
from __future__ import annotations

import datetime

from .base import (Ctx, Finding, Skipped, ERROR, WARN, INFO, num, trunc, iso_ok,
                   card_base_pct)

LAYER = "L6 engine reachability"

# --------------------------------------------------------------------------- #
# The engine's own vocabularies, transcribed from master.
# --------------------------------------------------------------------------- #

# _isExcluded, recommendation_engine.dart:542-557 — a switch with two cases and
# NO default. Anything else falls straight out and excludes nothing.
LIVE_EXCLUSION_TYPES = ("mcc", "category")

# RecommendationEngine.init switch, recommendation_engine.dart:143-200.
# 'portal_bonus' and 'milestone' hit an explicit `continue` at :139-141.
NEVER_INDEXED_RULE_TYPES = ("portal_bonus", "milestone")
KNOWN_RULE_TYPES = (
    "merchant_specific", "conditional", "category_bonus", "threshold_tier",
    "base_rate", "channel_specific", "promotional",
) + NEVER_INDEXED_RULE_TYPES

# Keys each parser actually reads. Anything else in a row is carried in the
# file, shipped to every handset, and read by nothing.
READ_KEYS = {
    "reward_rules": {
        "rule_name", "rule_type", "category_id", "channel", "merchant_ref",
        "portal_name", "conditions_json", "reward_type", "reward_rate",
        "reward_unit_spend", "cap_amount", "cap_period", "cap_kind",
        "min_txn_amount", "spend_threshold_min", "spend_threshold_max",
        "threshold_period", "priority", "point_value", "point_currency",
        "confidence", "source_conflict", "source_quote",
    },
    "exclusion_rules": {
        "exclusion_type", "exclusion_value", "also_excludes_from_threshold",
    },
    "milestone_rules": {
        "milestone_name", "rule_name", "spend_target", "period",
        "bonus_description", "reward_description", "bonus_type", "reward_type",
        "bonus_value", "reward_value",
    },
    "fuel_surcharge_rules": {"min_txn_amount", "max_txn_amount"},
    "redemption_rules": {
        "channel_type", "partner", "rule_name", "ratio", "value_per_point_inr",
        "point_value_inr", "bonus_pct", "bonus_until", "min_points", "notes",
        "channel_description",
    },
}

# Keys the parser DOES assign to a field but which no code anywhere in lib/
# then reads. Parsed and thrown away is the same as not parsed at all.
PARSED_BUT_UNREAD = {
    "reward_rules": {
        "min_txn_amount": "a minimum transaction amount",
        "confidence": "a confidence grade",
        "source_conflict": "a source-conflict flag",
        "source_quote": "the quote the rate came from",
        "portal_name": "a portal name",
        "point_currency": "a point-currency label",
    },
    "exclusion_rules": {
        "also_excludes_from_threshold": "an also-excludes-from-threshold flag",
    },
}

# conditions_json fields _passesConditions understands (engine:592-681).
KNOWN_CONDITION_FIELDS = (
    "txn.category", "txn.merchant", "txn.is_online", "calendar.quarter",
    "calendar.month", "calendar.day_of_week", "user.is_prime_member",
    "user.has_amazon_pay_balance", "user.has_swiggy_one", "user.quarterly_spend",
    "user.selected_categories",
)
# ...and the one that is recognised only to be refused (engine:675-676).
ALWAYS_FALSE_CONDITION_FIELD = "user.selected_categories"

# Schema drift: (keys the parser tries, in order) <- (keys seen in the file that
# it does NOT try). When a row uses only the right-hand spelling, that row's
# payload is dropped on the floor at parse time.
WRITE_ALIASES = {
    "milestone_rules": (
        (("spend_target",), ("spend_threshold", "target_spend", "milestone_spend")),
        (("bonus_type", "reward_type"), ("benefit_type",)),
        (("bonus_value", "reward_value"), ("benefit_value",)),
        (("period",), ("cap_period", "threshold_period")),
    ),
    "redemption_rules": (
        (("channel_type",), ("type", "redemption_type")),
        (("value_per_point_inr", "point_value_inr"),
         ("point_value", "value_per_point", "rp_value")),
        (("min_points",), ("min_redemption_points", "redemption_min_points")),
        (("partner", "rule_name"), ("partner_name", "channel_name")),
        (("notes", "channel_description"), ("description",)),
    ),
    "reward_rules": (
        (("cap_kind",), ("cap_unit",)),
        (("merchant_ref",), ("merchant", "merchant_id", "merchant_slug")),
    ),
    "fuel_surcharge_rules": (
        (("min_txn_amount",), ("min_transaction_amount",)),
        (("max_txn_amount",), ("max_transaction_amount",)),
    ),
}

# Owned by the dedicated date findings below; kept out of the field census so
# one defect is never reported twice.
CENSUS_SKIP = {("reward_rules", "expiry_date"), ("reward_rules", "effective_date")}

BLOCKS = ("reward_rules", "exclusion_rules", "milestone_rules",
          "fuel_surcharge_rules", "redemption_rules")

# Blocks the recommendation engine never consults at all.
DISPLAY_ONLY_BLOCKS = ("milestone_rules", "redemption_rules")


# --------------------------------------------------------------------------- #
# Small local helpers. None of these may raise.
# --------------------------------------------------------------------------- #
def _s(v):
    """The value as the Dart `as String?` cast would see it, or None."""
    return v if isinstance(v, str) else None


def _d(v):
    return v if isinstance(v, dict) else {}


def _rows(entry, block):
    v = entry.get(block)
    return v if isinstance(v, list) else []


def _has(row, key):
    """Key present AND carrying something a user could have benefited from.
    Null, empty, 0 and false all mean 'nothing was lost here'."""
    if not isinstance(row, dict) or key not in row:
        return False
    v = row[key]
    if v is None or v is False or v == 0:
        return False
    if isinstance(v, (str, list, dict)) and len(v) == 0:
        return False
    return True


def _today(ctx):
    t = _d(ctx.config).get("today")
    if isinstance(t, str) and len(t) >= 10:
        try:
            return datetime.date.fromisoformat(t[:10])
        except Exception:
            pass
    return datetime.date.today()


def _as_date(v):
    if not iso_ok(v):
        return None
    try:
        return datetime.date.fromisoformat(v[:10])
    except Exception:
        return None


def _synth_gate(rule_name):
    """_inferUserPrefGate, credit_card.dart:390-431 — a gate conjured out of the
    rule's NAME when conditions_json is absent."""
    n = (rule_name or "").lower()
    if not n:
        return None
    negated = any(w in n for w in
                  ("non-prime", "non prime", "without prime", "not a prime"))
    if not negated and any(w in n for w in ("prime member", "for prime", "(prime)")):
        return "user.is_prime_member"
    if "swiggy one" in n or "with swiggy" in n:
        return "user.has_swiggy_one"
    if "amazon pay balance" in n or "amazon pay wallet" in n:
        return "user.has_amazon_pay_balance"
    return None


def _match_all(row):
    c = row.get("conditions_json")
    if not isinstance(c, dict):
        return None
    ma = c.get("match_all")
    return ma if isinstance(ma, list) and ma else None


def _effective_category(row, slug_to_id):
    """What RewardRule.fromJson:438-442 ends up with, plus why."""
    v = row.get("category_id")
    if isinstance(v, bool):
        return None, "bad_type"
    if isinstance(v, int):
        return v, "int"
    if isinstance(v, str):
        cid = slug_to_id.get(v)
        return (cid, "slug") if cid is not None else (None, "unresolved")
    return None, "absent"


def _lane(rule_type, merchant_ref, cat_id, has_conditions):
    """Which of the engine's three index maps this rule lands in.

    Returns (lane, key) where lane is 'merchant' | 'category' | 'base',
    or ('drop', reason) when init throws it away.
    """
    if rule_type in NEVER_INDEXED_RULE_TYPES:
        return "drop", "rule_type_skipped"
    if rule_type == "merchant_specific":
        return ("merchant", merchant_ref) if merchant_ref else ("drop", "no_merchant_ref")
    if rule_type == "conditional":
        if merchant_ref:
            return "merchant", merchant_ref
        if cat_id is not None:
            return "category", cat_id
        return "base", None
    if rule_type == "category_bonus":
        if cat_id is not None:
            return "category", cat_id
        return ("base", None) if has_conditions else ("drop", "category_bonus_dropped")
    if rule_type == "threshold_tier":
        return ("category", cat_id) if cat_id is not None else ("base", None)
    if rule_type in ("base_rate", "channel_specific", "promotional"):
        return "base", None
    return "drop", "rule_type_unknown"


def _channel_domain(channel, lane, has_upi):
    """The set of merchants this rule can ever match.

    'ALL' / 'ONLINE' / 'OFFLINE', or None meaning the rule can never match
    anything at all. Two different matchers, deliberately kept apart:
      merchant + category lanes -> _channelMatches (engine:560-573)
      base lane                 -> literal equality in three phases
                                   (engine:441 'online', :471 'upi', :500 null)
    """
    if channel is not None and not isinstance(channel, str):
        return None
    if lane == "base":
        if channel is None:
            return "ALL"
        if channel == "online":
            return "ONLINE"
        if channel == "upi":
            return "ALL" if has_upi else None
        return None
    if channel is None:
        return "ALL"
    if channel == "online" or channel == "app":
        return "ONLINE"
    if channel == "offline":
        return "OFFLINE"
    if channel == "upi":
        return "ALL" if has_upi else None
    return None


def _dominates(a, b):
    """Does channel-domain a cover everything domain b covers?"""
    if a is None or b is None:
        return False
    return a == "ALL" or a == b


def _cap_is_enforceable(row):
    """engine:779 — BOTH fields required, and cap_amount must survive _numOf."""
    return num(row.get("cap_amount")) is not None and bool(_s(row.get("cap_period")))


def _is_gated(row, rule_name):
    """True when the engine can ever decline this rule and move to the next one.
    An ungated rule that sorts first wins its phase every single time."""
    if _match_all(row) is not None:
        return True
    if _synth_gate(rule_name):
        return True
    if row.get("spend_threshold_min") is not None:
        return True
    if row.get("spend_threshold_max") is not None:
        return True
    return _cap_is_enforceable(row)


# --------------------------------------------------------------------------- #
# Dart casts that throw. A throw inside fromOtaJson is caught per-card at
# utils.dart:222 — the WHOLE card is dropped, silently, and reaches no user.
# --------------------------------------------------------------------------- #
_STR_CAST = {
    "card": ("issuer", "card_tier", "reward_currency", "network", "point_currency"),
    "reward_rules": ("rule_name", "rule_type", "channel", "merchant_ref",
                     "portal_name", "cap_period", "cap_kind", "threshold_period",
                     "point_currency", "confidence", "source_quote"),
    "exclusion_rules": ("exclusion_type", "exclusion_value"),
    "milestone_rules": ("milestone_name", "rule_name", "period",
                        "bonus_description", "reward_description",
                        "bonus_type", "reward_type"),
    "redemption_rules": ("channel_type", "partner", "rule_name", "bonus_until",
                         "notes", "channel_description"),
    "fuel_surcharge_rules": (),
}


def _cast_faults(entry, inner):
    """Every value that would throw a TypeError inside CreditCardData.fromOtaJson.
    Returns a list of (block, index, field, value) — empty means the card loads."""
    out = []
    if not isinstance(inner, dict):
        return [("card", None, "card", entry.get("card"))]
    for k in _STR_CAST["card"]:
        if k in inner and inner[k] is not None and not isinstance(inner[k], str):
            out.append(("card", None, k, inner[k]))
    if "redemption" in inner and inner["redemption"] is not None \
            and not isinstance(inner["redemption"], dict):
        out.append(("card", None, "redemption", inner["redemption"]))

    for block in BLOCKS:
        raw = entry.get(block)
        if raw is None:
            continue
        if not isinstance(raw, list):
            # `as List<dynamic>?` throws on a string/number/object.
            out.append((block, None, block, raw))
            continue
        for j, row in enumerate(raw):
            if not isinstance(row, dict):
                # `e as Map<String, dynamic>` throws on the first non-object.
                out.append((block, j, block, row))
                continue
            for k in _STR_CAST.get(block, ()):
                if k in row and row[k] is not None and not isinstance(row[k], str):
                    out.append((block, j, k, row[k]))
            if block == "reward_rules":
                cj = row.get("conditions_json")
                if cj is not None and not isinstance(cj, dict):
                    out.append((block, j, "conditions_json", cj))
                sc = row.get("source_conflict")
                if sc is not None and not isinstance(sc, bool):
                    out.append((block, j, "source_conflict", sc))
            if block == "exclusion_rules":
                # `as int?` — a JSON true/false or "1" here throws.
                f = row.get("also_excludes_from_threshold")
                if f is not None and (isinstance(f, bool) or not isinstance(f, int)):
                    out.append((block, j, "also_excludes_from_threshold", f))
            if block == "redemption_rules" and row.get("partner") is None:
                rn = row.get("rule_name")
                if rn is not None and not isinstance(rn, str):
                    out.append((block, j, "rule_name", rn))
    return out


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    add = out.append
    today = _today(ctx)

    if not isinstance(ctx.cards, list):
        return [Finding(
            severity=ERROR,
            code="L6.CARDS_FILE_NOT_A_LIST",
            message="cards.json is not a list of cards, so nothing could be checked "
                    "for reachability.",
            evidence=trunc(type(ctx.cards).__name__),
            impact="The app expects a list and would load no cards at all.",
            fix="Restore cards.json to a JSON list of card entries.",
        )]

    # categories.json: slug -> id, and the parent chain the category phase walks.
    slug_to_id, parent_of = {}, {}
    for c in (ctx.app_categories or []):
        if not isinstance(c, dict):
            continue
        cid, nm = c.get("id"), c.get("category_name")
        if isinstance(cid, int) and isinstance(nm, str):
            slug_to_id[nm] = cid
            p = c.get("parent_id")
            parent_of[cid] = p if isinstance(p, int) else None
    have_categories = bool(slug_to_id)

    # merchant_name -> row, for the self-excluded-bonus check below. Keyed the
    # way reward_rules.merchant_ref actually points (seed/merchants.json rows
    # carry 'merchant_name'; their 'id' is an integer nothing references).
    merchant_index = {}
    _mrows = ctx.merchants.get("merchants") if isinstance(ctx.merchants, dict) else ctx.merchants
    for _m in (_mrows or []):
        if isinstance(_m, dict):
            _nm = _s(_m.get("merchant_name")) or _s(_m.get("slug")) or ""
            if _nm:
                merchant_index[_nm.strip().lower()] = _m

    def ancestors(cid):
        chain, seen, cur = [], set(), parent_of.get(cid)
        while isinstance(cur, int) and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = parent_of.get(cur)
        return chain

    # ---- portfolio tallies for the headline ------------------------------- #
    rows_total = 0
    rows_dead = 0
    per_block_total = {b: 0 for b in BLOCKS}
    per_block_dead = {b: 0 for b in BLOCKS}
    dead_cards = 0
    inert_excl_total = 0
    excl_total = 0
    cards_with_inert_excl = 0
    milestone_rows = 0
    redemption_rows = 0
    fuel_rows = 0
    unread_key_rows = {}     # (block, field) -> [row count, {card ids}]
    shadowed_total = 0

    for pos, entry, inner, cid in ctx.entries():
        cid = cid if isinstance(cid, str) and cid else f"(entry #{pos}, no card id)"
        try:
            _one_card(ctx, entry, inner, cid, today, slug_to_id, parent_of,
                      ancestors, have_categories, add, merchant_index)
        except Exception as exc:      # a bad card must never kill the check
            add(Finding(
                severity=WARN,
                code="L6.CHECK_ABORTED_ON_CARD",
                message=f"Could not finish the reachability check for {cid}; "
                        f"its rows were skipped.",
                card_id=cid,
                evidence=trunc(f"{type(exc).__name__}: {exc}"),
                impact="This card's dead rules are unaccounted for in the totals below.",
                fix="Send this card's JSON to whoever maintains the validator.",
            ))

        # ---- counting for the headline, done defensively -------------------- #
        try:
            faults = _cast_faults(entry, inner)
            card_dead = bool(faults)
            if card_dead:
                dead_cards += 1
            for block in BLOCKS:
                raw = entry.get(block)
                n = len(raw) if isinstance(raw, list) else (0 if raw is None else 1)
                rows_total += n
                per_block_total[block] += n
                if block == "milestone_rules":
                    milestone_rows += n
                elif block == "redemption_rules":
                    redemption_rows += n
                elif block == "fuel_surcharge_rules":
                    fuel_rows += n
                if card_dead:
                    rows_dead += n
                    per_block_dead[block] += n

            if not card_dead:
                # exclusions
                inert_here = 0
                for row in _rows(entry, "exclusion_rules"):
                    if not isinstance(row, dict):
                        continue
                    excl_total += 1
                    if _s(row.get("exclusion_type")) not in LIVE_EXCLUSION_TYPES:
                        inert_here += 1
                        rows_dead += 1
                        per_block_dead["exclusion_rules"] += 1
                inert_excl_total += inert_here
                if inert_here:
                    cards_with_inert_excl += 1
                # display-only blocks never reach the engine
                for block in DISPLAY_ONLY_BLOCKS:
                    n = len(_rows(entry, block))
                    rows_dead += n
                    per_block_dead[block] += n
                # fuel: only [0] is read, and only as a yes/no
                nf = len(_rows(entry, "fuel_surcharge_rules"))
                if nf > 1:
                    rows_dead += nf - 1
                    per_block_dead["fuel_surcharge_rules"] += nf - 1
                # reward rules
                dead_rr, shadow_rr = _reward_row_verdicts(
                    entry, inner, slug_to_id, ancestors)
                rows_dead += dead_rr
                per_block_dead["reward_rules"] += dead_rr
                shadowed_total += shadow_rr

            for block, keys in READ_KEYS.items():
                unread_here = PARSED_BUT_UNREAD.get(block, {})
                for row in _rows(entry, block):
                    if not isinstance(row, dict):
                        continue
                    for k in row:
                        if (k in keys and k not in unread_here) or not _has(row, k):
                            continue
                        if (block, k) in CENSUS_SKIP:
                            continue
                        slot = unread_key_rows.setdefault((block, k), [0, set()])
                        slot[0] += 1
                        slot[1].add(cid)
        except Exception:
            pass

    # ---- portfolio-level findings ----------------------------------------- #
    if excl_total:
        pct = 100.0 * inert_excl_total / excl_total
        add(Finding(
            severity=INFO,
            code="L6.EXCLUSIONS_INERT_TOTAL",
            message=(f"Across the whole file {inert_excl_total} of {excl_total} "
                     f"exclusions ({pct:.1f}%) are never enforced, spread over "
                     f"{cards_with_inert_excl} cards."),
            block="exclusion_rules",
            evidence=f"live types are only 'mcc' and 'category'; everything else is ignored",
            impact=("Two thirds of every exclusion we publish is decoration. The app "
                    "will happily recommend a card for spends the issuer does not reward."),
            fix=("Re-type each inert exclusion as 'mcc' or 'category' — the same sweep "
                 "that was done for fuel in Aug — or delete it so we stop claiming it."),
        ))

    if not have_categories:
        # This was an ERROR, and it was the wrong shape twice over. An error is
        # a statement about the DATA, and "I could not read the app's category
        # list" is a statement about the RUN — the file on disk is the same file
        # either way. Worse, it did not stop the category phase from running
        # anyway on an empty vocabulary, so it was the quiet first line of a
        # 307-error cascade underneath it. Now it stops that phase and says so.
        add(Skipped(
            code="L6.CATEGORY_REACHABILITY",
            what="No category rule was checked for whether the app can file it under a "
                 "category and reach it at all.",
            reason="This run has no category vocabulary: there was no app checkout and "
                   "the vendored mirror at tools/app_mirror/categories.json could not be "
                   "read either.",
            impact="read the absence of these findings as proof that every category "
                   "rule fires. With no vocabulary this phase cannot tell a valid slug "
                   "from an invalid one — allowed to run blind it called 191 healthy "
                   "bonus rules dead and 7 valid slugs unresolvable. Rule shadowing goes "
                   "with it: an unresolved category rule is pooled into the base lane, "
                   "so a blind run both misses real clashes and invents ones the engine "
                   "would never have.",
            codes=("L6.CATEGORY_BONUS_DROPPED", "L6.CATEGORY_ID_UNRESOLVABLE",
                   "L6.RULE_SHADOWED", "L6.RULE_SHADOWED_EQUAL"),
            restore="Restore tools/app_mirror/categories.json (python3 "
                    "tools/app_mirror/refresh.py --app-root ../KredMe-main), or pass "
                    "--app-root pointing at a KredMe-main checkout.",
        ))

    if milestone_rows:
        add(Finding(
            severity=INFO,
            code="L6.MILESTONES_NEVER_RANK",
            message=(f"All {milestone_rows} milestone rows are display-only — the "
                     f"engine skips them before ranking and never uses their spend "
                     f"targets or bonus values."),
            block="milestone_rules",
            impact=("A card with a big milestone bonus is never recommended any higher "
                    "for it. The user only sees the milestone if they open the card."),
            fix="No data fix. If milestones should influence the pick, that is app work.",
        ))
    if redemption_rows:
        add(Finding(
            severity=INFO,
            code="L6.REDEMPTION_NEVER_RANKS",
            message=(f"All {redemption_rows} redemption rows are display-only — the "
                     f"recommendation engine never reads them. Only the Redemption "
                     f"tab does."),
            block="redemption_rules",
            impact=("A card whose points are worth far more through one channel is "
                    "still ranked on rp_value_standard alone."),
            fix="No data fix here; rp_value_standard is the field that drives ranking.",
        ))
        # The old finding here claimed the redemption key-name mismatch was
        # FIXED and that these rows "do reach the Redemption tab". That was
        # false, and 147 findings across four layers were resting on it. What
        # was actually verified was the FIELD aliases INSIDE
        # RedemptionChannel.fromJson — never the BLOCK key that feeds it. The
        # app builds that list from `redemption_channels`; this file writes
        # `redemption_rules`; the two never meet, so fromJson is never called
        # with one of our rows and its aliases are irrelevant. Re-scoped to the
        # field aliases only, and stated as conditional on the block arriving.
        block_read = ctx.block_reaches_app("redemption_rules")
        reader_key = "redemption_channels"
        reader_read = ctx.app_reads_json_key(reader_key)

        if block_read is False:
            add(Finding(
                severity=ERROR,
                code="L6.REDEMPTION_BLOCK_NEVER_READ",
                message=(f"Every one of the {redemption_rows} redemption rows in this "
                         f"file is invisible to the app. We write them under the key "
                         f"'redemption_rules'; that string does not appear anywhere in "
                         f"the app's lib/. The app builds its redemption list from "
                         f"'{reader_key}'"
                         + (", which no card in this file ships."
                            if reader_read else ", which it also never receives.")),
                block="redemption_rules",
                evidence=(f"searched every .dart under {ctx.app_root}/lib: "
                          f"'redemption_rules' not found; "
                          f"'{reader_key}' {'found' if reader_read else 'not found'}"),
                impact=("Nobody sees any of it. Not the channels, not the point "
                        "values, not the descriptions. Every other finding about a "
                        "redemption row is therefore about content no user can "
                        "reach today, and is reported as a note for that reason — "
                        "close this key mismatch and they all become real."),
                fix=(f"Decide which name wins and change ONE side. Either rename this "
                     f"block to '{reader_key}' and reshape the rows to what "
                     f"RedemptionChannel.fromJson expects, or raise app work to read "
                     f"'redemption_rules'. Until then do not spend another hour "
                     f"curating these rows."),
            ))
        elif block_read is None:
            add(Skipped(
                code="L6.REDEMPTION_BLOCK_REACH",
                what=f"Whether the app reads the 'redemption_rules' block at all was "
                     f"not established, so none of the {redemption_rows} redemption "
                     f"rows could be judged reachable or dead.",
                reason="There is no app checkout to search, and tools/app_mirror/"
                       "app_json_keys.json — which records the answer for exactly this "
                       "case — is missing, unreadable, or does not name that key.",
                impact="assume either answer. Four checks scale their severity by this "
                       "one fact, so on this run every redemption finding is reported at "
                       "its loud severity by default; that is a fallback, not a "
                       "measurement.",
                codes=("L6.REDEMPTION_BLOCK_NEVER_READ", "L6.REDEMPTION_ALIASES_NOW_READ"),
                restore="Restore tools/app_mirror/app_json_keys.json (python3 "
                        "tools/app_mirror/refresh.py --app-root ../KredMe-main), or pass "
                        "--app-root pointing at a KredMe-main checkout.",
            ))
        else:
            add(Finding(
                severity=INFO,
                code="L6.REDEMPTION_ALIASES_NOW_READ",
                message=("Inside RedemptionChannel.fromJson the field aliases are "
                         "handled: rule_name, point_value_inr and channel_description "
                         "are each read under our name as well as the app's own. This "
                         "says nothing about whether the rows arrive — that is "
                         "L6.REDEMPTION_BLOCK_NEVER_READ's question."),
                block="redemption_rules",
                evidence="credit_card.dart RedemptionChannel.fromJson reads partner||rule_name, "
                         "value_per_point_inr||point_value_inr, notes||channel_description",
                impact="None — this records a field-level fact so nobody 're-fixes' it.",
                fix="Nothing. Do not rename these fields back.",
            ))

    if fuel_rows:
        add(Finding(
            severity=INFO,
            code="L6.FUEL_IS_A_YES_NO_FLAG",
            message=(f"All {fuel_rows} fuel-surcharge rows are read as nothing more "
                     f"than 'this card has a fuel waiver, yes or no'. The waiver "
                     f"percentage and the monthly cap are thrown away."),
            block="fuel_surcharge_rules",
            impact=("A card that waives 1% and a card that waives the full surcharge "
                    "look identical to the user, and a monthly waiver cap is never "
                    "enforced."),
            fix="Correcting waiver_pct changes nothing today; it needs app work first.",
        ))

    # One finding per dead FIELD, not per card: the field is a single decision
    # (teach the app to read it, or stop writing it). The affected cards are
    # named in the evidence so nobody has to go and count them.
    for (block, field), (n, cids) in sorted(
            unread_key_rows.items(), key=lambda kv: (-kv[1][0], kv[0])):
        why = PARSED_BUT_UNREAD.get(block, {}).get(field)
        how = ("the app reads it into memory and then never looks at it again"
               if why else "no code in the app reads this key at all")
        sample = ", ".join(sorted(cids)[:3]) + (f" (+{len(cids) - 3} more cards)"
                                                if len(cids) > 3 else "")
        add(Finding(
            severity=INFO,
            code="L6.FIELD_NEVER_REACHES_A_USER",
            message=(f"'{field}' carries real content on {n} {block.replace('_', ' ')} "
                     f"row(s) across {len(cids)} card(s), and {how}."),
            block=block, field=field,
            evidence=trunc(f"{n} row(s), {len(cids)} card(s): {sample}", 220),
            impact=("Whatever we curated into this field never reaches a single user. "
                    "Time spent filling it in buys nothing today."),
            fix=("Either raise app work to read it, or stop writing it — it is "
                 "currently downloaded onto every handset and ignored."),
        ))

    # ---- the headline ------------------------------------------------------ #
    reachable = rows_total - rows_dead
    pct = (100.0 * reachable / rows_total) if rows_total else 0.0
    breakdown = ", ".join(
        f"{b}: {per_block_total[b] - per_block_dead[b]}/{per_block_total[b]}"
        for b in BLOCKS if per_block_total[b])
    add(Finding(
        severity=INFO,
        code="L6.REACHABLE_FRACTION",
        message=(f"{reachable} of {rows_total} rows in the card file ({pct:.1f}%) can "
                 f"ever change what the recommendation engine tells a user. The other "
                 f"{rows_dead} are carried in every app install and never fire."),
        evidence=trunc(
            f"{breakdown} | {dead_cards} card(s) fail to load at all | "
            f"{inert_excl_total} inert exclusions | {shadowed_total} shadowed reward "
            f"rules | milestone+redemption rows are display-only by design", 400),
        impact=("Two thirds of the file is freight. Effort spent curating it buys the "
                "user nothing until the app learns to read it."),
        fix=("Read this number as a ceiling on how much any data-quality push can "
             "possibly improve, and fix the biggest dead block first."),
    ))
    return out


# --------------------------------------------------------------------------- #
# Per-card work
# --------------------------------------------------------------------------- #
def _reward_row_verdicts(entry, inner, slug_to_id, ancestors):
    """(dead_row_count, shadowed_count) for one card's reward_rules."""
    try:
        verdicts = _classify_reward_rules(entry, inner, slug_to_id, ancestors)
    except Exception:
        return 0, 0
    dead = sum(1 for v in verdicts if v["dead"])
    shadow = sum(1 for v in verdicts if v["shadowed_by"] is not None)
    return dead, shadow


def _classify_reward_rules(entry, inner, slug_to_id, ancestors):
    """One verdict dict per reward rule: where it is indexed, whether it can ever
    match a merchant, and whether another rule on the same card always beats it."""
    has_upi = inner.get("has_rupay_upi") in (1, True, "1")
    verdicts = []
    for j, row in enumerate(_rows(entry, "reward_rules")):
        v = {"i": j, "row": row, "dead": False, "why": None, "shadowed_by": None,
             "lane": None, "key": None, "domain": None, "cat": None,
             "upi_swallow": False}
        verdicts.append(v)
        if not isinstance(row, dict):
            v["dead"] = True
            v["why"] = "not_an_object"
            continue
        rt = _s(row.get("rule_type")) or "base_rate"
        mref = _s(row.get("merchant_ref"))
        cat, cat_how = _effective_category(row, slug_to_id)
        v["cat"], v["cat_how"] = cat, cat_how
        # conditionsJson != null after _inferUserPrefGate: an explicit map, or
        # a gate synthesised from the rule's name.
        has_cond = isinstance(row.get("conditions_json"), dict) or \
            bool(_synth_gate(_s(row.get("rule_name"))))
        lane, key = _lane(rt, mref, cat, has_cond)
        v["lane"], v["key"], v["rule_type"] = lane, key, rt
        if lane == "drop":
            v["dead"], v["why"] = True, key
            continue
        dom = _channel_domain(row.get("channel"), lane, has_upi)
        v["domain"] = dom
        if dom is None:
            v["dead"], v["why"] = True, "channel_never_matches"
            continue
        if cat_how == "unresolved":
            v["dead"], v["why"] = True, "category_unresolved"
            continue
        ma = _match_all(row)
        if ma and any(isinstance(c, dict) and c.get("field") == ALWAYS_FALSE_CONDITION_FIELD
                      for c in ma):
            v["dead"], v["why"] = True, "condition_always_false"
            continue

    def _renders_same(win_row, lose_row) -> bool:
        """Do these two rules put the SAME percentage on the screen?

        If they do there is no user-visible defect to fix: whichever one the
        engine reaches, the user is quoted and paid the identical number. The
        shadowed row is still duplicate data worth merging, but the finding may
        not go on saying "the user is being shown — and paid — the worse rate of
        the two", because there is no worse rate. 7 of 37 shadowed rows are this
        case.

        Compared on every input the app's rateForRule reads, not just the rate,
        so 2 points per Rs 100 and 2 points per Rs 200 are correctly NOT equal.
        """
        if not (isinstance(win_row, dict) and isinstance(lose_row, dict)):
            return False
        def shape(r):
            return (r.get("reward_type"), num(r.get("reward_rate")),
                    num(r.get("reward_unit_spend")), num(r.get("point_value")))
        return shape(win_row) == shape(lose_row)

    # ---- shadowing: who always wins the same phase, on this card ---------- #
    live = [v for v in verdicts if not v["dead"] and v["lane"] in
            ("merchant", "category", "base")]

    def sortkey(v):
        row = v["row"]
        p = num(row.get("priority")) or 0.0
        r = num(row.get("reward_rate")) or 0.0
        return (-p, -r, v["i"])

    # merchant lane: one pool per merchant, sorted priority then rate, first match wins
    pools = {}
    for v in live:
        if v["lane"] == "merchant":
            pools.setdefault(v["key"], []).append(v)
    for members in pools.values():
        members.sort(key=sortkey)
        for a_i, a in enumerate(members):
            if _is_gated(a["row"], _s(a["row"].get("rule_name"))):
                continue
            for b in members[a_i + 1:]:
                if b["shadowed_by"] is None and _dominates(a["domain"], b["domain"]):
                    b["shadowed_by"] = a

    # base lane: NOT one pool. The engine runs three separate loops in a fixed
    # order and returns from the first that matches — Phase 3a channel=='online'
    # (engine:441), Phase 3b channel=='upi' (:471), Phase 4 channel==null (:500).
    # Phase order beats priority, so a UPI rule on a RuPay card is reached before
    # the card's own base rate on EVERY purchase, not just UPI ones.
    ordered = []
    for phase in ("online", "upi", None):
        ordered.extend(sorted([v for v in live
                               if v["lane"] == "base" and v["row"].get("channel") == phase],
                              key=sortkey))
    for a_i, a in enumerate(ordered):
        if _is_gated(a["row"], _s(a["row"].get("rule_name"))):
            continue
        for b in ordered[a_i + 1:]:
            if b["shadowed_by"] is not None or not _dominates(a["domain"], b["domain"]):
                continue
            b["shadowed_by"] = a
            if a["row"].get("channel") == "upi" and b["row"].get("channel") is None:
                b["upi_swallow"] = True
    # category lane: the engine walks the parent chain, so a rule on an ancestor
    # category competes for every merchant the descendant rule could serve.
    cat_rules = [v for v in live if v["lane"] == "category"]
    for b in cat_rules:
        chain = [b["key"]] + ancestors(b["key"])
        for a in cat_rules:
            if a is b or a["key"] not in chain:
                continue
            if _is_gated(a["row"], _s(a["row"].get("rule_name"))):
                continue
            if not _dominates(a["domain"], b["domain"]):
                continue
            # Ties used to be broken by the JSON array index (sortkey ends in
            # v["i"]). The engine does not use array order that way.
            # _candidateCategoryRules walks the parent chain appending the CHILD
            # category's rules FIRST and each ancestor's after
            # (recommendation_engine.dart:734-744), and _sortList compares
            # priority then rewardRate and nothing else (:233-239). So on a
            # priority+rate tie the child keeps its place and it is the ANCESTOR
            # that loses — the exact opposite of the verdict this produced. 3 of
            # the 4 ancestor-shadow verdicts were decided purely by that wrong
            # tiebreak.
            #
            # Same-category ties are still broken by array order, because there
            # the two rules really are appended in file order in one pass.
            ka, kb = sortkey(a)[:2], sortkey(b)[:2]     # (-priority, -rate)
            if ka < kb:
                b["shadowed_by"] = a
                break
            if ka > kb:
                continue
            if a["key"] != b["key"]:
                # Dead tie between an ancestor and its descendant: the
                # descendant is reached first, so it is not shadowed. Nothing is
                # claimed about the ancestor either — Dart's List.sort is not
                # documented as stable, so naming a winner here would be a guess.
                continue
            if a["i"] < b["i"]:
                b["shadowed_by"] = a
                break
    for v in verdicts:
        if v["shadowed_by"] is not None:
            v["dead"] = True
            v["shadow_equal"] = _renders_same(v["shadowed_by"]["row"], v["row"])
            if v["why"] is None:
                v["why"] = "shadowed"
    return verdicts


def _one_card(ctx, entry, inner, cid, today, slug_to_id, parent_of, ancestors,
              have_categories, add, merchant_index=None):
    merchant_index = merchant_index or {}
    # ------------------------------------------------------------------ #
    # 0. Does the card load at all? Everything else is moot if it doesn't.
    # ------------------------------------------------------------------ #
    faults = _cast_faults(entry, inner)
    if faults:
        n_rows = sum(len(_rows(entry, b)) for b in BLOCKS)
        first = faults[0]
        add(Finding(
            severity=ERROR,
            code="L6.CARD_NEVER_LOADS",
            message=(f"{cid} crashes while the app is reading it, so the app skips the "
                     f"whole card. Everything it carries — its {n_rows} well-formed "
                     f"row(s) and the card itself — reaches nobody."),
            card_id=cid, block=first[0], index=first[1], field=first[2],
            evidence=trunc(f"{len(faults)} bad value(s); first: {first[0]}"
                           f"{'' if first[1] is None else '[%d]' % first[1]}"
                           f".{first[2]} = {trunc(first[3], 120)}"),
            impact=(f"{cid} is invisible in the app. Nobody can pick it, nobody is "
                    f"ever recommended it, and there is no error message anywhere — "
                    f"just one debug line a user never sees."),
            fix=("Fix the value's type so it matches the rest of the file, then confirm "
                 "the card count the app loads goes back up by one."),
        ))
        return   # no point analysing rows nobody will ever load

    # ------------------------------------------------------------------ #
    # 1. Inactive cards still rank.
    # ------------------------------------------------------------------ #
    act = inner.get("is_active")
    if act in (0, False, "0"):
        n_rr = len(_rows(entry, "reward_rules"))
        add(Finding(
            severity=ERROR,
            code="L6.INACTIVE_CARD_STILL_RANKS",
            message=(f"{cid} is marked inactive but the app has no idea what that "
                     f"means — it loads the card and ranks it like any other, with "
                     f"all {n_rr} of its reward rules live."),
            card_id=cid, block="card", field="is_active", evidence="is_active = 0",
            impact=("A card we have decided to withdraw is still being recommended to "
                    "users, who may go and apply for it."),
            fix=("Remove the card from cards.json, or accept that is_active does "
                 "nothing and stop relying on it."),
        ))

    # ------------------------------------------------------------------ #
    # 2. Exclusions that exclude nothing.
    # ------------------------------------------------------------------ #
    inert, samples, first_i = 0, [], None
    for j, row in enumerate(_rows(entry, "exclusion_rules")):
        if not isinstance(row, dict):
            continue
        t = _s(row.get("exclusion_type"))
        if t in LIVE_EXCLUSION_TYPES:
            continue
        inert += 1
        if first_i is None:
            first_i = j
        if len(samples) < 4:
            samples.append(f"{t}={_s(row.get('exclusion_value'))}")
    if inert:
        add(Finding(
            # ERROR, not WARN. This layer OWNS this defect — L2 reports the same
            # rows from the vocabulary side and is demoted to a note by the
            # runner — so this severity is the only one the founder now sees, and
            # it has to be the defensible one. An unenforced exclusion means the
            # app recommends a card for a purchase that pays the user nothing:
            # a wrong number on a screen, which is this tool's definition of
            # ERROR. The two layers used to disagree (L2 ERROR, L6 WARN) on the
            # identical 220 keys; they no longer can.
            severity=ERROR,
            code="L6.EXCLUSION_TYPE_INERT",
            message=(f"{cid} claims {inert} exclusion(s) the app cannot enforce, "
                     f"because it only understands exclusion_type 'mcc' and "
                     f"'category'."),
            card_id=cid, block="exclusion_rules", index=first_i,
            field="exclusion_type", evidence=trunc("; ".join(samples), 160),
            impact=(f"This card tells us these spends earn nothing, and the app will "
                    f"still promise the user rewards on them. If one of these is fuel "
                    f"or rent, the user is told to swipe a card that pays them zero."),
            fix=("Re-express each one as an 'mcc' exclusion (a merchant code) or a "
                 "'category' exclusion (a categories.json slug), or delete it."),
        ))

    # ------------------------------------------------------------------ #
    # 2b. A card that excludes the very thing it pays a bonus on.
    #
    # No layer checked a card's exclusion_rules against that same card's own
    # reward_rules, so a card could promise a bonus on a category it also
    # excludes and every one of the nine layers stayed silent. It is not
    # theoretical: 4 reward rules on 2 cards do this today.
    #
    # The engine tests exclusions FIRST — _evaluate step 1 at
    # recommendation_engine.dart:305-315 returns 'No rewards on this category'
    # with effectivePct 0 and never looks at a reward rule — so the bonus cannot
    # fire. The user is shown 0% on the exact category our own data advertises.
    # Unreachable by construction, which is what this layer exists to find.
    # ------------------------------------------------------------------ #
    excl_cats, excl_mccs = set(), set()
    for row in _rows(entry, "exclusion_rules"):
        if not isinstance(row, dict):
            continue
        t = _s(row.get("exclusion_type")) or ""
        v = _s(row.get("exclusion_value")) or ""
        if not v:
            continue
        if t == "category":
            excl_cats.add(v.strip().lower())
        elif t == "mcc":
            excl_mccs.add(v.strip().lower())

    if excl_cats or excl_mccs:
        clashes = []
        for j, row in enumerate(_rows(entry, "reward_rules")):
            if not isinstance(row, dict):
                continue
            cat = (_s(row.get("category_id")) or "").strip().lower()
            if cat and cat in excl_cats:
                clashes.append((j, row, f"pays on category '{cat}'",
                                f"the card excludes category '{cat}'"))
                continue
            # Same shape through a merchant: the engine excludes on the
            # MERCHANT's category_name / mcc_primary, so a merchant rule dies
            # just as completely. No live hits today; it costs nothing to cover.
            ref = (_s(row.get("merchant_ref")) or "").strip()
            if not ref:
                continue
            m = merchant_index.get(ref.lower())
            if not isinstance(m, dict):
                continue
            mcat = (_s(m.get("category_id")) or "").strip().lower()
            mmcc = (_s(m.get("mcc_primary")) or "").strip().lower()
            if mcat and mcat in excl_cats:
                clashes.append((j, row, f"pays at merchant '{ref}'",
                                f"that merchant is in category '{mcat}', which the card excludes"))
            elif mmcc and mmcc in excl_mccs:
                clashes.append((j, row, f"pays at merchant '{ref}'",
                                f"that merchant's MCC {mmcc} is excluded by the card"))
        if clashes:
            add(Finding(
                severity=ERROR,
                code="L6.RULE_EXCLUDED_BY_OWN_CARD",
                message=(f"{cid} promises a bonus on spending its own exclusion list "
                         f"rules out. {len(clashes)} reward rule(s) can never pay, "
                         f"because the app checks exclusions before it looks at any "
                         f"reward rule and stops there."),
                card_id=cid, block="reward_rules", index=clashes[0][0],
                field="category_id",
                evidence=trunc("; ".join(
                    f"[{j}] '{_s(r.get('rule_name')) or '(unnamed)'}' {what} but {why}"
                    for j, r, what, why in clashes[:3]), 400),
                impact=("The user is shown 0% and 'No rewards on this category' on the "
                        "exact spending this card's data advertises a bonus for. Both "
                        "rows cannot be right: one of them is a wrong number, and the "
                        "card is ranked as though the bonus does not exist."),
                fix=("Decide which row is true at the issuer and delete the other. The "
                     "exclusion is usually the correct one — an issuer that excludes "
                     "rent or insurance rarely also pays a bonus on it — so the reward "
                     "rule is the likelier mistake."),
            ))

    # ------------------------------------------------------------------ #
    # 3. Reward-rule reachability.
    # ------------------------------------------------------------------ #
    try:
        verdicts = _classify_reward_rules(entry, inner, slug_to_id, ancestors)
    except Exception:
        verdicts = []

    groups = {}
    for v in verdicts:
        if v["dead"] and v["why"]:
            groups.setdefault(v["why"], []).append(v)

    def name_of(v):
        return _s(v["row"].get("rule_name")) if isinstance(v["row"], dict) else None

    def ev(vs, n=3):
        return trunc("; ".join(f"[{v['i']}] {name_of(v) or '(unnamed)'}"
                               for v in vs[:n]) +
                     ("" if len(vs) <= n else f" (+{len(vs) - n} more)"), 200)

    # The two verdicts below are only meaningful when this run knows the app's
    # category vocabulary. Without it _effective_category() cannot resolve a
    # single slug, so EVERY category_bonus rule falls into "dropped" and every
    # other category rule into "unresolved" — 191 and 7 fabricated errors
    # respectively, describing rules that are perfectly fine. This is not a
    # question the run can answer, so it does not answer it; run() declares
    # L6.CATEGORY_REACHABILITY instead.
    g = groups.get("category_bonus_dropped") if have_categories else None
    if g:
        add(Finding(
            severity=ERROR,
            code="L6.CATEGORY_BONUS_DROPPED",
            message=(f"{cid} has {len(g)} bonus rule(s) with no category and no "
                     f"conditions. The app throws them away the moment it starts up."),
            card_id=cid, block="reward_rules", index=g[0]["i"],
            field="category_id", evidence=ev(g),
            impact=("The user is never shown this bonus rate — the card is ranked as "
                    "though the bonus does not exist."),
            fix=("Set category_id to a categories.json slug. The prose in category_ref "
                 "usually says which one."),
        ))

    g = groups.get("rule_type_skipped")
    if g:
        add(Finding(
            severity=WARN,
            code="L6.RULE_TYPE_NEVER_INDEXED",
            message=(f"{cid} has {len(g)} rule(s) whose rule_type the engine skips on "
                     f"purpose (portal_bonus / milestone), so they never win a "
                     f"recommendation."),
            card_id=cid, block="reward_rules", index=g[0]["i"],
            field="rule_type", evidence=ev(g),
            impact=("Card Detail still prints these rates, so the user reads a number "
                    "the app will never actually award them at the till."),
            fix=("Leave them if they are genuinely portal-only, but do not count them "
                 "as coverage — and never let one be the card's headline rate."),
        ))

    g = groups.get("rule_type_unknown")
    if g:
        add(Finding(
            severity=ERROR,
            code="L6.RULE_TYPE_UNKNOWN",
            message=(f"{cid} has {len(g)} rule(s) with a rule_type the app does not "
                     f"recognise. It drops them without a word — no error, no log, "
                     f"nothing."),
            card_id=cid, block="reward_rules", index=g[0]["i"],
            field="rule_type", evidence=ev(g),
            impact="A silently missing rate. Nobody will ever notice from the outside.",
            fix=("Use one of: merchant_specific, conditional, category_bonus, "
                 "threshold_tier, base_rate, channel_specific, promotional."),
        ))

    g = groups.get("no_merchant_ref")
    if g:
        add(Finding(
            severity=ERROR,
            code="L6.MERCHANT_RULE_HAS_NO_MERCHANT",
            message=(f"{cid} has {len(g)} merchant rule(s) that name no merchant, so "
                     f"the app has nothing to file them under and never uses them."),
            card_id=cid, block="reward_rules", index=g[0]["i"],
            field="merchant_ref", evidence=ev(g),
            impact="A merchant offer the user is never told about.",
            fix="Set merchant_ref to a slug from merchants.json.",
        ))

    g = groups.get("category_unresolved") if have_categories else None
    if g:
        add(Finding(
            severity=ERROR,
            code="L6.CATEGORY_ID_UNRESOLVABLE",
            message=(f"{cid} has {len(g)} rule(s) pointing at a spending category the "
                     f"app does not have, so the rule is filed nowhere and never "
                     f"fires."),
            card_id=cid, block="reward_rules", index=g[0]["i"],
            field="category_id",
            evidence=trunc("; ".join(sorted({str(v['row'].get('category_id'))
                                             for v in g}))[:160]),
            impact=("The bonus silently disappears. The card looks worse than it is and "
                    "is ranked below cards that are actually less rewarding."),
            fix=("Use a category_name from the app's categories.json, and remember the "
                 "app ships that list inside the APK — adding a new category needs a "
                 "release, not a data push."),
        ))

    g = groups.get("channel_never_matches")
    if g:
        chans = sorted({str(v["row"].get("channel")) for v in g})
        upi_only = chans == ["upi"]
        add(Finding(
            severity=ERROR,
            code="L6.CHANNEL_NEVER_MATCHES",
            message=(f"{cid} has {len(g)} rule(s) on a channel the app can never match "
                     f"({', '.join(chans)}), so they never fire for anybody."
                     + (" This card is not a RuPay-UPI card, so its UPI rules are "
                        "unreachable." if upi_only else "")),
            card_id=cid, block="reward_rules", index=g[0]["i"], field="channel",
            evidence=ev(g),
            impact=("The rate shows on the card's own page but the app will never "
                    "recommend the card for it — e.g. an 'international' rate is "
                    "printed and then ignored, because foreign spend is handled purely "
                    "as a forex mark-up."),
            fix=("Only null, 'online' and 'upi' work on base-lane rules; category and "
                 "merchant rules also accept 'offline' and 'app'. Anything else needs "
                 "app work before the data is worth writing."),
        ))

    g = groups.get("condition_always_false")
    if g:
        add(Finding(
            severity=ERROR,
            code="L6.CONDITION_ALWAYS_FALSE",
            message=(f"{cid} has {len(g)} rule(s) gated on a user setting the app has "
                     f"not built yet, and the unbuilt check answers 'no' every time."),
            card_id=cid, block="reward_rules", index=g[0]["i"],
            field="conditions_json", evidence=ev(g),
            impact="The rate can never be earned by any user, ever.",
            fix=f"Remove the '{ALWAYS_FALSE_CONDITION_FIELD}' condition, or wait for "
                f"the app to implement it.",
        ))

    swallowed = [v for v in verdicts if v.get("upi_swallow")]
    if swallowed:
        w = swallowed[0]["shadowed_by"]
        add(Finding(
            severity=ERROR,
            code="L6.UPI_RULE_SWALLOWS_BASE_RATE",
            message=(f"{cid} has a UPI-only reward rule that the app reaches before the "
                     f"card's ordinary base rate on EVERY purchase, not just UPI ones, "
                     f"because the UPI step is switched on by the card being RuPay — "
                     f"not by the payment actually being UPI. "
                     f"{len(swallowed)} base rule(s) are unreachable as a result."),
            card_id=cid, block="reward_rules", index=w["i"], field="channel",
            evidence=trunc(f"[{w['i']}] '{_s(w['row'].get('rule_name')) or '(unnamed)'}' "
                           f"(channel=upi, rate {w['row'].get('reward_rate')}) always "
                           f"beats [{swallowed[0]['i']}] "
                           f"'{_s(swallowed[0]['row'].get('rule_name')) or '(unnamed)'}'",
                           240),
            impact=("The user is quoted the UPI rate when they are about to swipe the "
                    "physical card at a shop, and will earn far less than we told them."),
            fix=("Give the UPI rule a cap or a condition so the engine can fall past it, "
                 "or raise app work — the UPI phase needs to test the transaction, not "
                 "the card."),
        ))

    # Split by whether the shadow costs the user anything. A row beaten by a rule
    # that renders the IDENTICAL percentage is duplicate data, not a wrong number
    # on a screen, and it must not be reported with an impact line that says the
    # user is being paid the worse of the two rates.
    # Shadowing needs the category vocabulary too, and not only for the category
    # lane. _lane() sends a category rule whose slug will not resolve into the
    # BASE lane instead, so a blind run pools rules the engine keeps apart and
    # sees rules beating each other that never actually compete — measured: one
    # invented L6.RULE_SHADOWED on the real catalogue, alongside the 9 genuine
    # ones it loses. Neither direction is a fact about the data, so the whole
    # analysis is withheld rather than half-trusted.
    shadowed_all = [v for v in verdicts
                    if v["shadowed_by"] is not None and not v.get("upi_swallow")] \
        if have_categories else []
    for equal, g in ((False, [v for v in shadowed_all if not v.get("shadow_equal")]),
                     (True, [v for v in shadowed_all if v.get("shadow_equal")])):
        if not g:
            continue
        first = g[0]
        w = first["shadowed_by"]
        add(Finding(
            severity=INFO if equal else WARN,
            code="L6.RULE_SHADOWED_EQUAL" if equal else "L6.RULE_SHADOWED",
            message=(f"{cid} has {len(g)} reward rule(s) that a higher-ranked rule on "
                     f"the same card always beats. The app checks rules in order and "
                     f"stops at the first match, so these are never reached."
                     + (" Both rules render the same percentage, so whichever one wins "
                        "the user sees and earns the same number." if equal else "")),
            card_id=cid, block="reward_rules", index=first["i"],
            evidence=trunc(
                f"e.g. [{first['i']}] '{name_of(first) or '(unnamed)'}' is always "
                f"beaten by [{w['i']}] '{name_of(w) or '(unnamed)'}' "
                f"(priority {w['row'].get('priority')} vs {first['row'].get('priority')}, "
                f"rate {w['row'].get('reward_rate')} vs {first['row'].get('reward_rate')})",
                240),
            impact=("No user-visible difference — the two pay identically. It is still "
                    "duplicate data: two rows to keep in step every time this card's "
                    "rate changes, and only one of them can ever be reached to prove it."
                    if equal else
                    "Dead weight. If the shadowed rule is the more generous one, the "
                    "user is being shown — and paid — the worse rate of the two."),
            fix=("Merge the duplicates, or make them genuinely different (different "
                 "category, channel, cap or condition). If the shadowed one is the "
                 "correct rate, raise its priority."),
        ))

    # ------------------------------------------------------------------ #
    # 4. Dates. The engine reads none of them — which cuts both ways.
    # ------------------------------------------------------------------ #
    past_exp, future_exp, bad_exp = [], [], []
    past_eff, future_eff, bad_eff = [], [], []
    for j, row in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(row, dict):
            continue
        for key, past, future, bad in (("expiry_date", past_exp, future_exp, bad_exp),
                                       ("effective_date", past_eff, future_eff, bad_eff)):
            v = row.get(key)
            if v is None or v == "":
                continue
            d = _as_date(v)
            if d is None:
                bad.append((j, v))
            elif d < today:
                past.append((j, v))
            elif d > today:
                future.append((j, v))

    if past_exp:
        add(Finding(
            severity=ERROR,
            code="L6.EXPIRED_RULE_STILL_FIRES",
            message=(f"{cid} has {len(past_exp)} reward rule(s) with an expiry date "
                     f"that has already passed — and the app never looks at expiry "
                     f"dates, so they are still live."),
            card_id=cid, block="reward_rules", index=past_exp[0][0],
            field="expiry_date",
            evidence=trunc("; ".join(f"[{i}] {v}" for i, v in past_exp[:4]), 160),
            impact=("Worse than dead code: the user is promised an offer that ended. "
                    "They swipe expecting the old rate and get the new one."),
            fix=("Delete the rule or replace its numbers. Do not rely on expiry_date to "
                 "switch anything off — nothing in the app reads it."),
        ))
    if future_exp:
        add(Finding(
            severity=WARN,
            code="L6.EXPIRY_DATE_NOT_ENFORCED",
            message=(f"{cid} sets a future expiry date on {len(future_exp)} rule(s). "
                     f"Nothing in the app will act on it when the day comes."),
            card_id=cid, block="reward_rules", index=future_exp[0][0],
            field="expiry_date",
            evidence=trunc("; ".join(f"[{i}] {v}" for i, v in future_exp[:4]), 160),
            impact=("The offer will keep being shown to users for as long as the rule "
                    "sits in the file — the date is a note to ourselves, nothing more."),
            fix="Put a reminder in the backlog to remove the rule by hand on that date.",
        ))
    if future_eff:
        add(Finding(
            severity=ERROR,
            code="L6.FUTURE_RULE_ALREADY_LIVE",
            message=(f"{cid} has {len(future_eff)} rule(s) dated to start in the "
                     f"future, but the app ignores start dates and is showing them "
                     f"today."),
            card_id=cid, block="reward_rules", index=future_eff[0][0],
            field="effective_date",
            evidence=trunc("; ".join(f"[{i}] {v}" for i, v in future_eff[:4]), 160),
            impact=("Users are being told about a rate that does not exist yet, and "
                    "will earn the old one instead."),
            fix="Hold the rule out of the file until the start date, then publish it.",
        ))
    elif past_eff:
        add(Finding(
            severity=INFO,
            code="L6.EFFECTIVE_DATE_NOT_ENFORCED",
            message=(f"{cid} records a start date on {len(past_eff)} rule(s); the app "
                     f"never reads it. Harmless today because the date has passed."),
            card_id=cid, block="reward_rules", index=past_eff[0][0],
            field="effective_date",
            evidence=trunc("; ".join(f"[{i}] {v}" for i, v in past_eff[:4]), 160),
            impact="None right now — but a future date here would go live immediately.",
            fix="Keep it as documentation only. Never schedule anything with it.",
        ))
    if bad_exp or bad_eff:
        b = (bad_exp + bad_eff)[0]
        add(Finding(
            severity=WARN,
            code="L6.DATE_UNREADABLE",
            message=(f"{cid} has {len(bad_exp) + len(bad_eff)} rule date(s) that are "
                     f"not a real YYYY-MM-DD date, so nobody — human or machine — can "
                     f"tell when the rule starts or ends."),
            card_id=cid, block="reward_rules", index=b[0],
            evidence=trunc(str(b[1]), 120),
            impact="We cannot tell whether the offer on this card is still running.",
            fix="Write dates as YYYY-MM-DD, or leave the field out entirely.",
        ))

    # ------------------------------------------------------------------ #
    # 5. conditions_json fields the engine does not understand.
    # ------------------------------------------------------------------ #
    unknown = []
    for j, row in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(row, dict):
            continue
        ma = _match_all(row)
        if not ma:
            continue
        for c in ma:
            f = c.get("field") if isinstance(c, dict) else None
            if isinstance(f, str) and f not in KNOWN_CONDITION_FIELDS:
                unknown.append((j, f))
    if unknown:
        add(Finding(
            severity=WARN,
            code="L6.CONDITION_FIELD_IGNORED",
            message=(f"{cid} gates {len(unknown)} rule(s) on a condition the app does "
                     f"not understand. It skips the condition instead of failing it, "
                     f"so the rule fires for everyone, all the time."),
            card_id=cid, block="reward_rules", index=unknown[0][0],
            field="conditions_json",
            evidence=trunc("; ".join(f"[{i}] {f}" for i, f in unknown[:4]), 160),
            impact=("A rate meant for a narrow case — a birthday, a special day — is "
                    "shown to every user on every purchase."),
            fix=("Use a condition the app knows: " +
                 ", ".join(KNOWN_CONDITION_FIELDS[:6]) + ", …"),
        ))

    # ------------------------------------------------------------------ #
    # 6. Rules gated only by their own NAME.
    # ------------------------------------------------------------------ #
    synth = []
    for j, row in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(row, dict) or _match_all(row) is not None:
            continue
        g2 = _synth_gate(_s(row.get("rule_name")))
        if g2:
            synth.append((j, g2))
    if synth:
        add(Finding(
            severity=WARN,
            code="L6.GATE_INFERRED_FROM_RULE_NAME",
            message=(f"{cid} has {len(synth)} rule(s) the app only switches on because "
                     f"of words in the rule's NAME (Prime / Swiggy One / Amazon Pay "
                     f"balance). Nothing in the data says so."),
            card_id=cid, block="reward_rules", index=synth[0][0], field="rule_name",
            evidence=trunc("; ".join(f"[{i}] -> {f}" for i, f in synth[:4]), 160),
            impact=("Rewording the rule name silently turns the membership requirement "
                    "on or off, and the user either loses a rate they qualify for or is "
                    "shown one they cannot earn."),
            fix=("Write the requirement into conditions_json so it stops depending on "
                 "prose, and never rename these rules casually."),
        ))

    # ------------------------------------------------------------------ #
    # 7. Payload written under a key name the parser does not try.
    #    Only reported when the key the app DOES read is missing on that row,
    #    i.e. the row really has lost its content. A field nothing reads
    #    file-wide is a one-off decision and is reported once, not per card.
    # ------------------------------------------------------------------ #
    for block, pairs in WRITE_ALIASES.items():
        lost = {}
        first_i = None
        for j, row in enumerate(_rows(entry, block)):
            if not isinstance(row, dict):
                continue
            for read_keys, aliases in pairs:
                if any(_has(row, k) for k in read_keys):
                    continue
                for a in aliases:
                    if _has(row, a):
                        lost.setdefault(a, []).append((j, read_keys[0]))
                        if first_i is None:
                            first_i = j
        if not lost:
            continue
        detail = "; ".join(f"'{a}' on {len(v)} row(s) where '{v[0][1]}' is missing"
                           for a, v in sorted(lost.items()))
        add(Finding(
            severity=ERROR,
            code="L6.PAYLOAD_UNDER_UNREAD_KEY",
            message=(f"{cid} writes {block.replace('_', ' ')} content under key name(s) "
                     f"the app does not read, and the name it does read is missing on "
                     f"those rows — so the app loads the row empty."),
            card_id=cid, block=block, index=first_i, field=sorted(lost)[0],
            evidence=trunc(detail, 220),
            impact=("The user sees the row with the value blanked out — a milestone "
                    "with no target and no reward, or a redemption channel with no "
                    "value. It looks like we simply do not know."),
            fix=("Rename to the spelling the rest of the file uses: " +
                 ", ".join(sorted({rk[0] for rk, _al in pairs})) + "."),
        ))

    # fuel rows past the first are never opened
    fr = _rows(entry, "fuel_surcharge_rules")
    if len(fr) > 1:
        add(Finding(
            severity=WARN,
            code="L6.FUEL_ROW_IGNORED",
            message=(f"{cid} ships {len(fr)} fuel-surcharge rows and the app opens only "
                     f"the first one."),
            card_id=cid, block="fuel_surcharge_rules", index=1,
            evidence=f"{len(fr) - 1} row(s) never read",
            impact="Any waiver terms in the later rows are invisible to the user.",
            fix="Collapse them into one row, keeping the terms that actually apply.",
        ))
