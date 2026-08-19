"""L1 — structural integrity of the card-data files.

This layer runs before every other layer and answers one question: **can the app
read this file at all?** Nothing here judges whether a reward number is right —
only whether the shape is something the Flutter parser can consume.

Why that ordering matters. `CreditCardData.fromOtaJson` is wrapped in a per-card
`try/catch` (`lib/core/utils.dart:222-224`). A single wrong type inside one card
throws, the catch swallows it, and **that whole card silently disappears from the
app** — no crash, no error screen, one `debugPrint` nobody reads. A numbers check
that runs on a card the app never loads is checking a card that reaches no user.

Three shapes of damage, in severity order:

  1. the value is read with a hard Dart cast (`as String`, `as int?`,
     `as Map<String, dynamic>`) — a wrong type throws and the card vanishes;
  2. the value is read through `_numOf`/`_intOf` — a wrong type returns null and
     the field is silently dropped (a dropped `cap_amount` is an *uncapped* rule,
     which pays its bonus rate for ever);
  3. the value is fine for the app but inconsistent with the rest of the column —
     cosmetic today, a trap for anything stricter later.

Aggregation: one finding per defect class per card, never per row. A card with
five string `cap_amount`s produces one finding that says five, not five findings.
Findings are never collapsed *across* cards — the founder needs the card list.

Stdlib only. Never prints, never exits, never mutates ctx.
"""
from __future__ import annotations

import difflib
import json
import re
from collections import Counter, defaultdict

from .base import Ctx, Finding, ERROR, WARN, INFO, num, trunc, iso_ok, card_base_pct

LAYER = "L1 schema & shape"

# card_base_pct is part of the shared contract but is a numeric helper; L1
# deliberately makes no judgement about reward values.
_ = card_base_pct

# --------------------------------------------------------------------------- #
# The six blocks every entry in cards.json must carry
# --------------------------------------------------------------------------- #
ROW_BLOCKS = (
    "reward_rules",
    "exclusion_rules",
    "milestone_rules",
    "fuel_surcharge_rules",
    "redemption_rules",
)
ALL_BLOCKS = ("card",) + ROW_BLOCKS

# Plain-English name for each block, for messages a non-technical reader gets.
BLOCK_WORDS = {
    "card": "card details",
    "reward_rules": "reward rules",
    "exclusion_rules": "exclusion rules",
    "milestone_rules": "milestone rules",
    "fuel_surcharge_rules": "fuel surcharge rules",
    "redemption_rules": "redemption options",
}

# What one row in each block is called, in the singular.
ROW_WORDS = {
    "reward_rules": "reward rule",
    "exclusion_rules": "exclusion rule",
    "milestone_rules": "milestone",
    "fuel_surcharge_rules": "fuel surcharge rule",
    "redemption_rules": "redemption option",
}


# --------------------------------------------------------------------------- #
# Field specification
# --------------------------------------------------------------------------- #
# kind      what the value must be
#   str        a string
#   num        a JSON number (int or float both fine — the app uses _numOf)
#   int        a whole number
#   flag01     a whole number that is exactly 0 or 1
#   object     a JSON object
#   json_object a JSON object; if it arrives as text, say whether the text is
#              even valid JSON, because the two need different fixes
#   array      a JSON array
#   date       a string shaped YYYY-MM-DD (or an ISO timestamp)
#   str_or_int a string slug or a legacy integer id
#   any        present, type unchecked
#
# required  the key must exist on every row
# nullable  the value may be JSON null
# hard      the Dart parser reads it with a hard cast — a wrong type here throws
#           and the ENTIRE CARD is dropped from the app
# empty_ok  for strings, whether "" is acceptable
def _f(kind, required=False, nullable=True, hard=False, empty_ok=True, why=None):
    return {
        "kind": kind,
        "required": required,
        "nullable": nullable,
        "hard": hard,
        "empty_ok": empty_ok,
        "why": why,
    }


CARD_SPEC = {
    # 'id' is listed so it counts as a known key, but every rule about it is
    # enforced explicitly below — a duplicate report here would say it twice.
    "id": _f("any"),
    "card_name": _f("str", required=True, nullable=False, empty_ok=False,
                    why="this is the name the user sees in the picker"),
    "issuer": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
                 why="the bank name shown on the card and used to group the picker"),
    "network": _f("str", required=True, nullable=False, hard=True),
    "card_tier": _f("str", required=True, nullable=False, hard=True),
    "annual_fee": _f("num", required=True, nullable=False),
    "fee_waiver_spend": _f("num", required=True),
    "base_reward_rate": _f("num", required=True, nullable=False,
                           why="every rupee figure on the card is computed from it"),
    "reward_currency": _f("str", required=True, nullable=False, hard=True),
    "rp_value_standard": _f("num", required=True),
    "rp_value_travel": _f("num", required=True),
    "rp_value_transfer": _f("num", required=True),
    "forex_markup_pct": _f("num", required=True, nullable=False,
                           why="left empty the app assumes 3.5% and quietly "
                               "docks that off every overseas spend"),
    "has_rupay_upi": _f("flag01", required=True, nullable=False,
                        why="it decides whether the card's UPI rules can ever fire"),
    "image_asset": _f("str", required=True, nullable=False, empty_ok=False),
    "metadata": _f("object", required=True, nullable=False),
    "is_active": _f("flag01", required=True, nullable=False),
    "is_travel": _f("flag01", required=True, nullable=False),
    "points_expiry_months": _f("int", required=True),
    "min_redemption_points": _f("int", required=True),
    "points_clawback_on_default": _f("any", required=True),
    # parsed by the app, not written by the pipeline today
    "point_currency": _f("str", hard=True),
    "redemption": _f("object", hard=True),
    "issuer_key": _f("str"),
}

REWARD_SPEC = {
    "rule_name": _f("str", required=True, nullable=False, empty_ok=False,
                    why="the rule name is the key the cap counter is stored under"),
    "rule_type": _f("str", required=True, nullable=False, hard=True, empty_ok=False),
    "merchant_ref": _f("str", required=True, hard=True),
    "category_ref": _f("str", required=True),
    "category_id": _f("str_or_int"),
    "channel": _f("str", required=True, hard=True),
    "reward_type": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
                      why="it picks which of the three reward arithmetics runs"),
    "reward_rate": _f("num", required=True, nullable=False),
    "reward_unit_spend": _f("num", required=True),
    "cap_amount": _f("num", required=True,
                     why="a cap the app cannot read is no cap at all"),
    "cap_period": _f("str", required=True, hard=True),
    "min_txn_amount": _f("num", required=True),
    "priority": _f("int", required=True, nullable=False),
    "effective_date": _f("date", required=True),
    "expiry_date": _f("date", required=True),
    "conditions_json": _f("json_object", required=True, hard=True),
    # sparse extras the app also parses
    "cap_kind": _f("str", hard=True),
    "cap_unit": _f("str"),
    "point_value": _f("num"),
    "point_currency": _f("str", hard=True),
    "portal_name": _f("str", hard=True),
    "spend_threshold_min": _f("num"),
    "spend_threshold_max": _f("num"),
    "threshold_period": _f("str", hard=True),
    "confidence": _f("str", hard=True),
    "source_conflict": _f("bool", hard=True),
    "source_quote": _f("str", hard=True,
                       why="the app hard-casts it to a string; an object here "
                           "throws and the card disappears"),
    "source_url": _f("str"),
    "source_doc_type": _f("str"),
    "source_fetched_on": _f("date"),
    "_sources": _f("array"),
}

EXCLUSION_SPEC = {
    "exclusion_type": _f("str", required=True, nullable=False, empty_ok=False),
    "exclusion_value": _f("str", required=True, nullable=False, empty_ok=False),
    "also_excludes_from_threshold": _f("flag01", required=True, hard=True,
                                       why="the app casts it straight to a whole "
                                           "number; true/false or \"1\" throws"),
    # The two provenance stamps a fix stage writes on an exclusion row, and
    # the reason they are BOTH in the agreed schema rather than one:
    #   _retyped_from   what the issuer's own words were before a sweep made
    #                   this row live. It is the only thing that makes the
    #                   change reversible without inventing a value.
    #   _reverted_from  what the row was made into, and then put back from. A
    #                   revert that deleted the stamp left nothing in the file
    #                   marking the row as deliberately inert, so the next
    #                   sweep re-activated it with no trace of the decision.
    "_retyped_from": _f("str"),
    "_reverted_from": _f("str"),
    "confidence": _f("str"),
    "source_quote": _f("str"),
    "source_url": _f("str"),
    "_sources": _f("array"),
}

MILESTONE_SPEC = {
    "milestone_name": _f("str", required=True, nullable=False, hard=True, empty_ok=False),
    "period": _f("str", required=True, nullable=False, hard=True),
    "spend_target": _f("num", required=True, nullable=False,
                       why="missing, the app reads the target as ₹0 and the "
                           "milestone shows as already achieved"),
    "bonus_type": _f("str", required=True, nullable=False, hard=True),
    "bonus_value": _f("num", required=True),
    "bonus_description": _f("str", required=True, hard=True),
    "is_progressive": _f("flag01"),
    "conditions_json": _f("json_object"),
    # alternative spellings the app genuinely accepts
    "rule_name": _f("str", hard=True),
    "reward_type": _f("str", hard=True),
    "reward_value": _f("num"),
    "reward_description": _f("str", hard=True),
    "confidence": _f("str"),
    "source_quote": _f("str"),
    "source_url": _f("str"),
    "_sources": _f("array"),
}

FUEL_SPEC = {
    "waiver_pct": _f("num", required=True, nullable=False),
    "min_txn_amount": _f("num", required=True),
    "max_txn_amount": _f("num", required=True),
    "monthly_cap": _f("num", required=True),
}

REDEMPTION_SPEC = {
    "rule_name": _f("str", required=True, nullable=False, empty_ok=False),
    "channel_type": _f("str", required=True, nullable=False, hard=True, empty_ok=False),
    "channel_description": _f("str", required=True),
    "point_value_inr": _f("num", required=True),
    "min_points": _f("int", required=True),
    "max_points": _f("int", required=True),
    "redemption_fee_inr": _f("num", required=True),
    "confidence": _f("str", required=True),
    "_sources": _f("array"),
    "_source_quotes": _f("object"),
    "transfer_partners": _f("array"),
    "voucher_options": _f("array"),
    "source_url": _f("str"),
    "source_doc_type": _f("str"),
    "source_fetched_on": _f("date"),
    # scaffold spellings the app also accepts
    "partner": _f("str", hard=True),
    "notes": _f("str", hard=True),
    "value_per_point_inr": _f("num"),
    "ratio": _f("num"),
    "bonus_pct": _f("num"),
    "bonus_until": _f("date", hard=True),
}

SPECS = {
    "card": CARD_SPEC,
    "reward_rules": REWARD_SPEC,
    "exclusion_rules": EXCLUSION_SPEC,
    "milestone_rules": MILESTONE_SPEC,
    "fuel_surcharge_rules": FUEL_SPEC,
    "redemption_rules": REDEMPTION_SPEC,
}

# Keys that duplicate a key the app DOES read, under a name it does NOT read.
# The row looks complete; the app takes its silent default instead.
UNREAD_ALIASES = {
    "milestone_rules": {
        "benefit_type": "bonus_type",
        "benefit_value": "bonus_value",
        "spend_threshold": "spend_target",
    },
}

MERCHANT_SPEC = {
    "id": _f("int", required=True, nullable=False),
    "merchant_name": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
                        why="the app hard-casts this one with no null guard"),
    "display_name": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
                       why="the app hard-casts this one with no null guard"),
    "category_id": _f("str_or_int", required=True),
    "mcc_primary": _f("str", required=True, hard=True),
    "mcc_codes": _f("array", required=True, nullable=False),
    "statement_aliases": _f("array", required=True, nullable=False),
    "is_online": _f("flag01", required=True, hard=True),
    "metadata": _f("object", required=True, nullable=False, hard=True),
    "logo_url": _f("str", hard=True),
}

NEWS_SPEC = {
    "id": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
             why="the app hard-casts it with no null guard"),
    "title": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
                why="the app hard-casts it with no null guard"),
    "summary": _f("str", required=True, nullable=False, hard=True, empty_ok=False,
                  why="the app hard-casts it with no null guard"),
    "category": _f("str", required=True, hard=True),
    "severity": _f("str", required=True, hard=True),
    "source": _f("str", required=True, hard=True),
    "source_url": _f("str", required=True, hard=True),
    "published_at": _f("date", required=True, nullable=False, hard=True),
    "expiry_date": _f("date", required=True, hard=True),
    "affected_cards": _f("array", required=True),
    "affected_issuers": _f("array"),
    "tags": _f("array"),
    "action_text": _f("str", hard=True),
}

CARD_ID_SLUG = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")

# Fields whose whole point is a number. A string or object here is not a style
# question — the number is gone.
_NUMERIC_KINDS = {"num", "int"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _jtype(v) -> str:
    """The JSON type name of a value. bool is checked before int on purpose."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true/false"
    if isinstance(v, int):
        return "whole number"
    if isinstance(v, float):
        return "decimal number"
    if isinstance(v, str):
        return "text"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "list"
    return type(v).__name__


def _a(jt) -> str:
    """The JSON type name with the right article in front of it."""
    return {
        "null": "empty",
        "true/false": "true or false",
        "text": "text",
        "whole number": "a whole number",
        "decimal number": "a decimal number",
        "object": "an object",
        "list": "a list",
    }.get(jt, f"a {jt}")


def _allowed(kind) -> set:
    return {
        "str": {"text"},
        "num": {"whole number", "decimal number"},
        "int": {"whole number"},
        "flag01": {"whole number"},
        "object": {"object"},
        "json_object": {"object"},
        "array": {"list"},
        "date": {"text"},
        "str_or_int": {"text", "whole number"},
        "bool": {"true/false"},
        "any": set(),
    }.get(kind, set())


def _kind_word(kind) -> str:
    return {
        "str": "a piece of text",
        "num": "a number",
        "int": "a whole number",
        "flag01": "0 or 1",
        "object": "an object",
        "json_object": "an object",
        "array": "a list",
        "date": "a date written YYYY-MM-DD",
        "str_or_int": "a category slug",
        "bool": "true or false",
        "any": "anything",
    }.get(kind, kind)


class _Bag:
    """Collects rows into one finding per (card, defect class) bucket."""

    def __init__(self):
        self.rows = defaultdict(list)   # key -> [(index, value)]
        self.meta = {}                  # key -> dict of extras

    def add(self, key, index, value, **meta):
        self.rows[key].append((index, value))
        if meta:
            self.meta.setdefault(key, {}).update(meta)

    def items(self):
        for key, hits in self.rows.items():
            yield key, hits, self.meta.get(key, {})


def _count_word(n, singular, plural=None):
    plural = plural or singular + "s"
    return f"{n} {singular if n == 1 else plural}"


def _sample(hits, n=3):
    """A short, readable sample of the offending values."""
    vals = []
    for _idx, v in hits[:n]:
        vals.append(trunc(v, 60))
    more = "" if len(hits) <= n else f" (+{len(hits) - n} more)"
    return "; ".join(vals) + more


# --------------------------------------------------------------------------- #
# Per-row field validation (shared by cards, merchants and news)
# --------------------------------------------------------------------------- #
def _check_row(row, spec, bag, index, label_missing_required=True):
    """Validate one object against a spec, funnelling hits into `bag`.

    bag keys are (code, field, detail). Never raises.
    """
    if not isinstance(row, dict):
        return

    for key, val in row.items():
        f = spec.get(key)
        if f is None:
            near = difflib.get_close_matches(key, list(spec.keys()), n=1, cutoff=0.82)
            if near:
                bag.add(("KEY_LOOKS_LIKE_TYPO", key, near[0]), index, key)
            else:
                bag.add(("UNKNOWN_KEY", key, None), index, key)
            continue

        kind = f["kind"]
        jt = _jtype(val)

        if jt == "null":
            if not f["nullable"]:
                bag.add(("NULL_IN_REQUIRED_FIELD", key, None), index, None, why=f["why"])
            continue

        if kind == "any":
            continue

        ok = _allowed(kind)
        if jt not in ok:
            if kind == "json_object" and jt == "text":
                # The app hard-casts this to an object without decoding it, so a
                # string always throws — but a founder needs to know whether the
                # text is salvageable JSON or free prose.
                try:
                    parsed = json.loads(val)
                    good = isinstance(parsed, dict)
                except Exception:
                    good = None
                bag.add(("CONDITIONS_AS_TEXT", key, good), index, val)
                continue
            if kind == "flag01":
                bag.add(("FLAG_NOT_01", key, jt), index, val,
                        hard=f["hard"], why=f["why"])
            elif kind in _NUMERIC_KINDS:
                bag.add(("NUMERIC_FIELD_NOT_A_NUMBER", key, jt), index, val,
                        hard=f["hard"], why=f["why"])
            elif f["hard"]:
                bag.add(("FIELD_TYPE_BREAKS_PARSING", key, jt), index, val,
                        kind=kind, why=f["why"])
            elif kind == "object" and jt == "text":
                bag.add(("FIELD_TYPE_UNEXPECTED", key, jt), index, val, kind=kind)
            else:
                bag.add(("FIELD_TYPE_UNEXPECTED", key, jt), index, val, kind=kind)
            continue

        # right family, now the finer rules
        if kind == "flag01" and val not in (0, 1):
            bag.add(("FLAG_NOT_01", key, "out of range"), index, val,
                    hard=f["hard"], why=f["why"])
        elif kind == "date" and not iso_ok(val):
            bag.add(("DATE_NOT_ISO", key, None), index, val)
        elif kind == "str" and not f["empty_ok"] and not val.strip():
            bag.add(("REQUIRED_TEXT_EMPTY", key, None), index, val, why=f["why"])

    if not label_missing_required:
        return

    required = [k for k, f in spec.items() if f["required"]]
    missing = [k for k in required if k not in row]
    # A row that is missing most of what it needs is a stub, not a row with N
    # separate defects. Report it once, or the founder reads the same story
    # fourteen times.
    if len(missing) >= 4 and len(missing) * 2 >= len(required):
        bag.add(("ROW_INCOMPLETE", None, None), index, sorted(row.keys()),
                missing=missing, have=len(row), need=len(required))
        return
    for key in missing:
        f = spec[key]
        bag.add(("REQUIRED_FIELD_MISSING", key, None), index, None,
                kind=f["kind"], why=f["why"], nullable=f["nullable"])


# --------------------------------------------------------------------------- #
# Finding construction from a bag
# --------------------------------------------------------------------------- #
def _emit(bag, out, *, card_id, block, subject=None, file_hint=None):
    """Turn one bag into findings.

    `subject` names a single row when the block is not one of the card blocks
    (a merchant is a "shop", a news item is a "story").
    """
    word = subject or ROW_WORDS.get(block, "row")
    tail = f" in {file_hint}" if (file_hint and block not in BLOCK_WORDS) else ""
    home = BLOCK_WORDS.get(block) or file_hint or "this file"

    for (code, field, detail), hits, meta in bag.items():
        n = len(hits)
        rows = _count_word(n, word) + tail
        # "on this card" reads better than "on 1 card detail" — the card block
        # is a single object, never a list of rows.
        scope = "on this card" if block == "card" else f"on {rows}"
        idx = hits[0][0] if hits and hits[0][0] is not None else None
        ev = _sample(hits)
        why = meta.get("why")

        if code == "NUMERIC_FIELD_NOT_A_NUMBER":
            parses = all(
                isinstance(v, str) and _looks_numeric(v) for _i, v in hits
            )
            if detail == "text" and parses:
                msg = (f"'{field}' is written as text in quotes {scope} — the "
                       f"digits are there but the file says text, not a number.")
                impact = ("It works today only because the app tries to read a "
                          "number out of the text. Any stricter reader, and the "
                          "value is gone.")
            elif detail == "text":
                msg = f"'{field}' holds a sentence instead of a number {scope}."
                impact = ("The app gets nothing back and carries on as if the "
                          "field were empty. " + _sent(why)).strip()
            else:
                msg = f"'{field}' holds {_a(detail)} instead of a number {scope}."
                impact = ("The app cannot read a number out of it and treats the "
                          "field as empty. " + _sent(why)).strip()
            out.append(Finding(
                severity=ERROR, code="L1.NUMERIC_FIELD_NOT_A_NUMBER",
                message=msg, card_id=card_id, block=block, index=idx,
                field=field, evidence=ev, impact=impact,
                fix=(f"Store a bare number in '{field}' and move any wording into "
                     f"the rule's name or description. If the real limit changes "
                     f"part-way through the year, split it into two rows."),
            ))

        elif code == "FIELD_TYPE_BREAKS_PARSING":
            out.append(Finding(
                severity=ERROR, code="L1.FIELD_TYPE_BREAKS_PARSING",
                message=(f"'{field}' is {_a(detail)} {scope}, where it must be "
                         f"{_kind_word(meta.get('kind', 'str'))}."),
                card_id=card_id, block=block, index=idx, field=field, evidence=ev,
                impact=(_kill_impact(block) + " " + _sent(why)).strip(),
                fix=f"Write '{field}' as {_kind_word(meta.get('kind', 'str'))}.",
            ))

        elif code == "FIELD_TYPE_UNEXPECTED":
            out.append(Finding(
                severity=WARN, code="L1.FIELD_TYPE_UNEXPECTED",
                message=(f"'{field}' is {_a(detail)} {scope}, where "
                         f"{_kind_word(meta.get('kind', 'str'))} is expected."),
                card_id=card_id, block=block, index=idx, field=field, evidence=ev,
                impact="The app ignores the value; whatever it was meant to say is lost.",
                fix=f"Write '{field}' as {_kind_word(meta.get('kind', 'str'))}.",
            ))

        elif code == "NULL_IN_REQUIRED_FIELD":
            out.append(Finding(
                severity=WARN, code="L1.NULL_IN_REQUIRED_FIELD",
                message=f"'{field}' is empty {scope}, but it must hold a value.",
                card_id=card_id, block=block, index=idx, field=field,
                impact=(_sent(why) or "The app substitutes its own built-in "
                        "default, so the screen shows a number nobody chose."),
                fix=(f"Fill in '{field}'." if block == "card" else
                     f"Fill in '{field}', or drop the row if the value is "
                     f"genuinely unknown."),
            ))

        elif code == "ROW_INCOMPLETE":
            miss = meta.get("missing") or []
            need = meta.get("need", len(miss))
            head = ("This card is" if block == "card"
                    else f"{_count_word(n, word).capitalize()} "
                         f"{'is' if n == 1 else 'are'}")
            noun = "details every card must carry" if block == "card" \
                else "fields every row must carry"
            out.append(Finding(
                severity=ERROR, code="L1.ROW_INCOMPLETE",
                message=(f"{head} missing {len(miss)} of the {need} {noun}, "
                         f"including {', '.join(repr(m) for m in miss[:4])}."),
                card_id=card_id, block=block, index=idx, evidence=ev,
                impact=("This is not a row with a few gaps — it is a stub. The app "
                        "fills every hole with a built-in default, so whatever it "
                        "shows for this row is invented rather than read."),
                fix=(("Rebuild this card from the source it came from, or remove "
                      "it." if block == "card" else
                      "Rebuild this row from the source it came from, or remove "
                      "it.") + " Patching one field at a time will not make it "
                     "correct."),
            ))

        elif code == "REQUIRED_FIELD_MISSING":
            # A missing key reads as null in Dart — it never throws. What decides
            # severity is whether the app can carry on without a real value.
            must_have = not meta.get("nullable", True)
            out.append(Finding(
                severity=ERROR if must_have else WARN,
                code="L1.REQUIRED_FIELD_MISSING",
                message=f"'{field}' is missing entirely {scope}.",
                card_id=card_id, block=block, index=idx, field=field,
                impact=(_sent(why) or ("The app has no value to use and falls back "
                        "to a built-in default, so this shows a number nobody wrote."
                        if must_have else
                        "The field is expected on every row; its absence means "
                        "whatever wrote this row stopped early.")),
                fix=f"Add '{field}' to every row in {home}.",
            ))

        elif code == "CONDITIONS_AS_TEXT":
            if detail is True:
                tale = ("the text is valid JSON, so the wording is salvageable; it "
                        "just needs to be stored as an object rather than quoted")
            elif detail is False:
                tale = ("the text reads as JSON but is not an object, so there is "
                        "no set of conditions in there to apply")
            else:
                tale = ("the text is not JSON at all, so nothing can be recovered "
                        "from it automatically")
            out.append(Finding(
                severity=ERROR, code="L1.CONDITIONS_JSON_AS_TEXT",
                message=(f"'{field}' is stored as quoted text {scope} rather than "
                         f"as an object — {tale}."),
                card_id=card_id, block=block, index=idx, field=field, evidence=ev,
                impact=("The app reads this field as an object with no safety net. "
                        "Text here throws, the error is swallowed, and the entire "
                        "card vanishes from the app."),
                fix=("Write the conditions as a real JSON object, e.g. "
                     "{\"match_all\": [ ... ]} — not as a quoted string."),
            ))

        elif code == "FLAG_NOT_01":
            hard = meta.get("hard")
            if detail == "out of range":
                what = (f"'{field}' is a yes/no switch but holds a number that is "
                        f"neither 0 nor 1 {scope}.")
                harm = ("Anything that is not exactly 1 counts as 'no', so the "
                        "switch is silently off.")
            elif hard:
                what = (f"'{field}' is written as {_a(detail)} {scope}; it must "
                        f"be the whole number 0 or 1.")
                harm = _kill_impact(block)
            else:
                what = (f"'{field}' is written as {_a(detail)} {scope}; it must "
                        f"be the whole number 0 or 1.")
                harm = ("The app converts it quietly today, but nothing guarantees "
                        "the next reader will, and the switch is one flip away "
                        "from being silently off.")
            out.append(Finding(
                severity=ERROR, code="L1.FLAG_NOT_01",
                message=what, card_id=card_id, block=block, index=idx,
                field=field, evidence=ev,
                impact=(harm + " " + _sent(why)).strip(),
                fix=(f"Write '{field}' as the whole number 0 or 1 — not true/false, "
                     f"not the text \"1\"."),
            ))

        elif code == "REQUIRED_TEXT_EMPTY":
            out.append(Finding(
                severity=ERROR, code="L1.REQUIRED_TEXT_EMPTY",
                message=f"'{field}' is blank {scope}.",
                card_id=card_id, block=block, index=idx, field=field,
                impact=(_sent(why) or "The app has nothing to show and nothing "
                        "to match on."),
                fix=f"Give '{field}' a real value.",
            ))

        elif code == "DATE_NOT_ISO":
            out.append(Finding(
                severity=WARN, code="L1.DATE_NOT_ISO",
                message=(f"'{field}' is not written as a date {scope} — "
                         f"dates must read YYYY-MM-DD."),
                card_id=card_id, block=block, index=idx, field=field, evidence=ev,
                impact="The app cannot read the date and quietly treats it as today.",
                fix=f"Write '{field}' as YYYY-MM-DD, e.g. 2026-08-18.",
            ))

        elif code == "KEY_LOOKS_LIKE_TYPO":
            out.append(Finding(
                severity=WARN, code="L1.KEY_LOOKS_LIKE_TYPO",
                message=(f"A field called '{field}' appears {scope}, one character "
                         f"away from '{detail}' — most likely a typo."),
                card_id=card_id, block=block, index=idx, field=field, evidence=ev,
                impact=("The app does not know this name, so whatever was written "
                        "here is thrown away and the real field is left empty."),
                fix=f"Rename '{field}' to '{detail}' — after checking it really is the same thing.",
            ))

        elif code == "UNKNOWN_KEY":
            out.append(Finding(
                severity=WARN, code="L1.UNKNOWN_KEY",
                message=(f"A field called '{field}' appears {scope} but is not "
                         f"part of the agreed schema."),
                card_id=card_id, block=block, index=idx, field=field, evidence=ev,
                impact=("The app has no code that reads this name, so the value "
                        "ships in the file and reaches nobody."),
                fix=("Either add it to the schema and to the app, or remove it. "
                     "A field nobody reads is dead weight that looks like data."),
            ))

        elif code == "UNREAD_ALIAS_KEY":
            pairs = meta.get("pairs") or {}
            listed = ", ".join(f"'{a}' should be '{t}'"
                               for a, t in sorted(pairs.items()))
            out.append(Finding(
                severity=ERROR, code="L1.UNREAD_ALIAS_KEY",
                message=(f"{_count_word(n, word).capitalize()} on this card "
                         f"{'uses' if n == 1 else 'use'} field names the app never "
                         f"reads: {listed}."),
                card_id=card_id, block=block, index=idx,
                field=sorted(pairs)[0] if pairs else None, evidence=ev,
                impact=("The row looks complete in the file, but the app looks for "
                        "the other spelling, finds nothing, and falls back to its "
                        "own built-in default — so the target and the reward shown "
                        "here are values nobody wrote."),
                fix=("Rename these fields to the names the app reads. They are the "
                     "same values, just filed under the wrong headings."),
            ))


def _kill_impact(block) -> str:
    """What a wrong type costs, in the words of the file it is in."""
    if block == "merchants":
        return ("The app reads this field with no safety net. From the live feed "
                "the shop is skipped; from the copy built into the app, one bad "
                "shop loses ALL of them — and every shop-specific reward rule "
                "stops firing for everyone.")
    if block == "news":
        return ("The app reads the whole feed in one pass with no safety net. One "
                "bad story throws and every story is lost — the news screen goes "
                "empty for everyone.")
    return ("The app reads this field with no safety net. A wrong type throws an "
            "error, the error is swallowed, and the entire card vanishes from the "
            "app — no crash, no warning, the card simply never appears for any "
            "user.")


def _sent(t) -> str:
    """Normalise a spec note into a standalone sentence."""
    if not t:
        return ""
    t = str(t).strip()
    return t[0].upper() + t[1:] + ("" if t.endswith((".", "!", "?")) else ".")


def _looks_numeric(s) -> bool:
    try:
        float(str(s).strip())
        return True
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# The check
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    try:
        _check_cards(ctx, out)
    except Exception as e:                                   # pragma: no cover
        out.append(_internal("seed/cards.json", e))
    try:
        _check_merchants(ctx, out)
    except Exception as e:                                   # pragma: no cover
        out.append(_internal("seed/merchants.json", e))
    try:
        _check_news(ctx, out)
    except Exception as e:                                   # pragma: no cover
        out.append(_internal("news/feed.json", e))
    return out


def _internal(where, e):
    return Finding(
        severity=ERROR, code="L1.CHECK_CRASHED",
        message=f"The structure check could not finish reading {where}.",
        evidence=trunc(f"{type(e).__name__}: {e}"),
        impact="Nothing below this point in the file was checked at all.",
        fix="Send this line to whoever maintains the validator.",
    )


# --------------------------------------------------------------------------- #
# cards.json
# --------------------------------------------------------------------------- #
def _check_cards(ctx: Ctx, out: list[Finding]):
    cards = ctx.cards

    if not isinstance(cards, list):
        out.append(Finding(
            severity=ERROR, code="L1.CARDS_FILE_NOT_A_LIST",
            message=(f"seed/cards.json is {_a(_jtype(cards))} at the top level; it "
                     f"must "
                     f"be a plain list of cards."),
            block="card",
            impact="The app cannot read the file at all. No card reaches any user.",
            fix="Wrap the card entries in a single JSON list.",
        ))
        return

    if not cards:
        out.append(Finding(
            severity=ERROR, code="L1.CARDS_FILE_EMPTY",
            message="seed/cards.json holds no cards.",
            block="card",
            impact="The app falls back to its bundled copy, so users keep seeing "
                   "whatever shipped with their installed version.",
            fix="Restore the card list before publishing.",
        ))
        return

    seen_ids = defaultdict(list)
    style = defaultdict(Counter)     # (block, field) -> Counter of int/float
    style_hits = []                  # (card_id, block, field, index, value, jt)

    for i, entry in enumerate(cards):
        label = None
        try:
            if not isinstance(entry, dict):
                out.append(Finding(
                    severity=ERROR, code="L1.ENTRY_NOT_AN_OBJECT",
                    message=(f"Entry number {i + 1} in seed/cards.json is "
                             f"{_a(_jtype(entry))}, not a card."),
                    block="card", index=i, evidence=trunc(entry),
                    impact="The app throws while reading it and drops this card entirely.",
                    fix="Replace the entry with a proper card object, or delete it.",
                ))
                continue

            inner = entry.get("card")
            if "card" not in entry:
                out.append(Finding(
                    severity=ERROR, code="L1.CARD_BLOCK_MISSING",
                    message=f"Entry number {i + 1} in seed/cards.json has no 'card' section.",
                    block="card", index=i,
                    evidence=trunc(sorted(entry.keys())),
                    impact=("The app reads the 'card' section first and with no "
                            "safety net. Without it the whole entry is dropped."),
                    fix="Add the 'card' object holding the card's name, issuer and rates.",
                ))
                continue
            if not isinstance(inner, dict):
                out.append(Finding(
                    severity=ERROR, code="L1.CARD_BLOCK_NOT_AN_OBJECT",
                    message=(f"The 'card' section of entry number {i + 1} is "
                             f"{_a(_jtype(inner))}, not an object."),
                    block="card", index=i, evidence=trunc(inner),
                    impact=("The app reads the 'card' section with no safety net. "
                            "This entry is dropped and reaches no user."),
                    fix="Replace it with the card's details as a JSON object.",
                ))
                continue

            cid = inner.get("id")
            label = cid if isinstance(cid, str) and cid.strip() else f"<entry #{i + 1}>"

            # -- identity ------------------------------------------------- #
            if "id" not in inner or cid is None:
                out.append(Finding(
                    severity=ERROR, code="L1.CARD_ID_MISSING",
                    message=f"The card in entry number {i + 1} has no id.",
                    card_id=label, block="card", index=i, field="id",
                    evidence=trunc(inner.get("card_name")),
                    impact=("Every saved transaction and every cap counter is filed "
                            "under the card id. Without one the card can be shown "
                            "but nothing about it can be remembered."),
                    fix="Give the card a permanent id, e.g. hdfc_bank_millennia.",
                ))
            elif not isinstance(cid, str):
                out.append(Finding(
                    severity=ERROR, code="L1.CARD_ID_NOT_TEXT",
                    message=(f"The card id in entry number {i + 1} is "
                             f"{_a(_jtype(cid))}, not text."),
                    card_id=label, block="card", index=i, field="id",
                    evidence=trunc(cid),
                    impact="Ids are matched as text everywhere; a number will not match.",
                    fix="Write the id as a quoted slug.",
                ))
            elif not cid.strip():
                out.append(Finding(
                    severity=ERROR, code="L1.CARD_ID_EMPTY",
                    message=f"The card id in entry number {i + 1} is blank.",
                    card_id=label, block="card", index=i, field="id",
                    impact="Two blank ids collide and overwrite each other's saved data.",
                    fix="Give the card a permanent id.",
                ))
            else:
                seen_ids[cid].append(i)
                if not CARD_ID_SLUG.match(cid):
                    bad = sorted({c for c in cid if not re.match(r"[a-z0-9_-]", c)})
                    worst = "/" in cid
                    out.append(Finding(
                        severity=WARN, code="L1.CARD_ID_NOT_SLUG_SHAPED",
                        message=(f"This card's id contains characters that do not "
                                 f"belong in an id: {' '.join(bad) or 'capitals or spaces'}."),
                        card_id=cid, block="card", index=i, field="id",
                        evidence=trunc(cid),
                        impact=("The id is pasted straight into file paths and "
                                "storage keys. " +
                                ("A forward slash reads as a folder separator and "
                                 "will break any path built from it. " if worst else "") +
                                "Renaming it later wipes every user's saved cap "
                                "progress on this card, so this is cheapest to fix "
                                "before the card has any users."),
                        fix=("Use only lowercase letters, digits and underscores. "
                             "Changing an id already in the wild is a migration, "
                             "not an edit — decide before it ships."),
                    ))

            # -- blocks ---------------------------------------------------- #
            row_blocks_ok = {}
            for b in ROW_BLOCKS:
                if b not in entry:
                    out.append(Finding(
                        severity=ERROR, code="L1.BLOCK_MISSING",
                        message=f"This card has no '{b}' section at all.",
                        card_id=label, block=b, index=i,
                        impact=("Every card is expected to carry all six sections, "
                                "even when empty. A missing one means whatever "
                                "wrote this card stopped half-way."),
                        fix=f"Add \"{b}\": [] if the card genuinely has none.",
                    ))
                    continue
                v = entry[b]
                if isinstance(v, list):
                    row_blocks_ok[b] = v
                    continue

                # THE yes_bank_uni_rupay CASE
                if isinstance(v, str):
                    out.append(Finding(
                        severity=ERROR, code="L1.BLOCK_NOT_A_LIST",
                        message=(f"This card's {BLOCK_WORDS.get(b, b)} section is a "
                                 f"{len(v)}-character line of text instead of a list "
                                 f"of rules."),
                        card_id=label, block=b, index=i, evidence=trunc(v, 140),
                        impact=("The app expects a list here and gets text, which "
                                "throws an error the app quietly swallows — so this "
                                "entire card never loads and reaches no user at all. "
                                "It is also why a simple checker reports "
                                f"{len(v)} rules on this card: it is counting "
                                "letters, not rules."),
                        fix=(f"Replace the sentence with the real list of "
                             f"{BLOCK_WORDS.get(b, b)}. If the note says to copy "
                             f"another card's list, copy it — do not leave the note."),
                    ))
                else:
                    out.append(Finding(
                        severity=ERROR, code="L1.BLOCK_NOT_A_LIST",
                        message=(f"This card's {BLOCK_WORDS.get(b, b)} section is "
                                 f"{_a(_jtype(v))}, not a list."),
                        card_id=label, block=b, index=i, evidence=trunc(v),
                        impact=("The app expects a list here. Anything else throws "
                                "and the whole card silently disappears."),
                        fix=f"Write '{b}' as a JSON list, or [] when there are none.",
                    ))

            # -- rows are objects ------------------------------------------ #
            for b, rows in row_blocks_ok.items():
                bad = [(j, r) for j, r in enumerate(rows) if not isinstance(r, dict)]
                if bad:
                    kinds = Counter(_jtype(r) for _j, r in bad)
                    kind_txt = ", ".join(f"{v}× {k}" for k, v in kinds.most_common())
                    first_only = (b == "fuel_surcharge_rules" and bad[0][0] == 0)
                    out.append(Finding(
                        severity=ERROR, code="L1.ROW_NOT_AN_OBJECT",
                        message=(f"{_count_word(len(bad), 'row')} out of {len(rows)} "
                                 f"in this card's {BLOCK_WORDS.get(b, b)} "
                                 f"{'is' if len(bad) == 1 else 'are'} not a rule "
                                 f"object ({kind_txt})."),
                        card_id=label, block=b, index=bad[0][0],
                        evidence=_sample(bad),
                        impact=("The app reads every row as an object with no "
                                "safety net. One bad row throws and the whole card "
                                "silently disappears from the app." if not first_only
                                else "The app reads the first fuel row as an object "
                                     "with no safety net; this throws and the whole "
                                     "card silently disappears from the app."),
                        fix=f"Every entry in '{b}' must be a JSON object, or remove it.",
                    ))

            # -- fields ----------------------------------------------------- #
            bag = _Bag()
            _check_row(inner, CARD_SPEC, bag, i)
            _emit(bag, out, card_id=label, block="card")
            _collect_style(inner, "card", CARD_SPEC, label, i, style, style_hits)

            for b, rows in row_blocks_ok.items():
                spec = SPECS[b]
                aliases = UNREAD_ALIASES.get(b, {})
                bag = _Bag()
                for j, r in enumerate(rows):
                    if not isinstance(r, dict):
                        continue
                    try:
                        # unread aliases first — they explain the missing target
                        found = {a: t for a, t in aliases.items() if a in r}
                        masked = {t for t in found.values() if t not in r}
                        if found:
                            bag.add(("UNREAD_ALIAS_KEY", None, None), j,
                                    {a: r.get(a) for a in found}, pairs=found)
                            trimmed = {k: v for k, v in spec.items() if k not in masked}
                            probe = {k: v for k, v in r.items() if k not in aliases}
                            _check_row(probe, trimmed, bag, j)
                        else:
                            _check_row(r, spec, bag, j)
                        _collect_style(r, b, spec, label, j, style, style_hits)
                    except Exception:
                        # one unreadable row must never stop the rest of the card
                        bag.add(("UNKNOWN_KEY", "<unreadable row>", None), j, trunc(r))
                _emit(bag, out, card_id=label, block=b)

        except Exception as e:
            out.append(Finding(
                severity=ERROR, code="L1.CHECK_CRASHED",
                message=(f"The structure check could not finish reading "
                         f"{label or f'entry number {i + 1}'}."),
                card_id=label, block="card", index=i,
                evidence=trunc(f"{type(e).__name__}: {e}"),
                impact="This card was only partly checked.",
                fix="Send this line to whoever maintains the validator.",
            ))

    # -- duplicate ids ------------------------------------------------------ #
    for cid, where in seen_ids.items():
        if len(where) > 1:
            out.append(Finding(
                severity=ERROR, code="L1.CARD_ID_DUPLICATE",
                message=(f"The id '{cid}' is used by {len(where)} different entries "
                         f"in seed/cards.json."),
                card_id=cid, block="card", index=where[0],
                evidence="entries " + ", ".join(str(w + 1) for w in where),
                impact=("Everything the app remembers about a card is filed under "
                        "its id, so these entries overwrite each other. Whichever "
                        "loads last wins and the others are invisible."),
                fix="Give each card its own permanent id.",
            ))

    _emit_style(style, style_hits, out)


def _collect_style(row, block, spec, card_id, index, style, hits):
    """Record int-vs-decimal usage per field so the odd one out can be named."""
    for key, val in row.items():
        f = spec.get(key)
        if not f or f["kind"] != "num":
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        jt = "whole number" if isinstance(val, int) else "decimal number"
        style[(block, key)][jt] += 1
        hits.append((card_id, block, key, index, val, jt))


def _emit_style(style, hits, out):
    """One INFO per card+block naming every field written in the odd style."""
    minority = {}
    for (block, key), counts in style.items():
        total = sum(counts.values())
        if total < 20 or len(counts) < 2:
            continue
        least, n = counts.most_common()[-1]
        if n / total <= 0.10:
            minority[(block, key)] = least

    grouped = defaultdict(list)
    for card_id, block, key, index, val, jt in hits:
        if minority.get((block, key)) == jt:
            grouped[(card_id, block)].append((key, index, val))

    for (card_id, block), rows in sorted(grouped.items()):
        fields = sorted({k for k, _i, _v in rows})
        idx = rows[0][1]
        out.append(Finding(
            severity=INFO, code="L1.NUMBER_STYLE_INCONSISTENT",
            message=(f"{', '.join(repr(f) for f in fields)} "
                     + ("is written as a whole number" if len(fields) == 1
                        else "are written as whole numbers")
                     + " here while nearly every other card writes "
                     + ("it" if len(fields) == 1 else "them")
                     + " with a decimal point."),
            card_id=card_id, block=block, index=idx,
            field=fields[0] if len(fields) == 1 else None,
            evidence=trunc({k: v for k, _i, v in rows}),
            impact=("No effect on what a user sees today. It is the fingerprint of "
                    "a row hand-written outside the pipeline, and it will trip any "
                    "stricter schema check added later."),
            fix="Re-emit these rows through the same writer as the rest of the file.",
        ))


# --------------------------------------------------------------------------- #
# seed/merchants.json
# --------------------------------------------------------------------------- #
def _check_merchants(ctx: Ctx, out: list[Finding]):
    m = ctx.merchants
    if m is None:
        out.append(Finding(
            severity=ERROR, code="L1.MERCHANTS_FILE_MISSING",
            message="seed/merchants.json was not loaded.",
            impact="Nothing can be checked about the shops the app matches against.",
            fix="Restore seed/merchants.json.",
        ))
        return

    if isinstance(m, dict):
        rows = m.get("merchants")
        if rows is None:
            out.append(Finding(
                severity=ERROR, code="L1.MERCHANTS_LIST_MISSING",
                message=("seed/merchants.json is an object but has no 'merchants' "
                         "list inside it."),
                evidence=trunc(sorted(m.keys())),
                impact=("The app looks for that exact key and gets nothing, so it "
                        "loads zero shops and every shop-specific reward rule stops "
                        "firing."),
                fix="Put the shop list under a top-level \"merchants\" key.",
            ))
            return
    elif isinstance(m, list):
        rows = m
    else:
        out.append(Finding(
            severity=ERROR, code="L1.MERCHANTS_FILE_SHAPE",
            message=(f"seed/merchants.json is {_a(_jtype(m))}; it must be a list "
                     f"of shops or an object with a 'merchants' list."),
            impact="The app cannot read it and loads zero shops.",
            fix="Emit the file as {\"merchants\": [ ... ]}.",
        ))
        return

    if not isinstance(rows, list):
        out.append(Finding(
            severity=ERROR, code="L1.MERCHANTS_FILE_SHAPE",
            message=(f"The 'merchants' key in seed/merchants.json is "
                     f"{_a(_jtype(rows))}, not a list."),
            evidence=trunc(rows),
            impact="The app cannot read it and loads zero shops.",
            fix="Write 'merchants' as a JSON list.",
        ))
        return

    bad_rows = [(j, r) for j, r in enumerate(rows) if not isinstance(r, dict)]
    if bad_rows:
        out.append(Finding(
            severity=ERROR, code="L1.MERCHANT_ROW_NOT_AN_OBJECT",
            message=(f"{_count_word(len(bad_rows), 'row')} out of {len(rows)} in "
                     f"seed/merchants.json are not shop objects."),
            index=bad_rows[0][0], evidence=_sample(bad_rows),
            impact=("When the app is reading its built-in copy there is no per-row "
                    "safety net — one bad row throws and it loads zero shops, so "
                    "every shop-specific reward rule stops firing for everyone."),
            fix="Every entry in 'merchants' must be a JSON object.",
        ))

    bag = _Bag()
    names = defaultdict(list)
    no_category = []
    for j, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        try:
            _check_row(r, MERCHANT_SPEC, bag, j)
            nm = r.get("merchant_name")
            if isinstance(nm, str) and nm.strip():
                names[nm].append(j)
            if r.get("category_id") is None:
                no_category.append((j, nm or f"row {j + 1}"))
        except Exception:
            bag.add(("UNKNOWN_KEY", "<unreadable row>", None), j, trunc(r))

    _emit(bag, out, card_id=None, block="merchants", subject="shop",
          file_hint="seed/merchants.json")

    for nm, where in names.items():
        if len(where) > 1:
            out.append(Finding(
                severity=ERROR, code="L1.MERCHANT_NAME_DUPLICATE",
                message=(f"The shop key '{nm}' appears {len(where)} times in "
                         f"seed/merchants.json."),
                index=where[0], evidence="rows " + ", ".join(str(w + 1) for w in where),
                impact=("The app files shops by this name, so the duplicates "
                        "overwrite each other and only one survives."),
                fix="Give each shop its own merchant_name.",
            ))

    if no_category:
        out.append(Finding(
            severity=WARN, code="L1.MERCHANT_NO_CATEGORY",
            message=(f"{_count_word(len(no_category), 'shop')} in "
                     f"seed/merchants.json have no category."),
            index=no_category[0][0],
            evidence=trunc([n for _j, n in no_category[:12]]),
            impact=("When the app reads its built-in copy it skips these shops "
                    "entirely, and even from the live feed a shop with no category "
                    "can never match a category reward rule — so the best card for "
                    "that shop is judged on its base rate alone."),
            fix="Give every shop a category slug that exists in the app's category list.",
        ))


# --------------------------------------------------------------------------- #
# news/feed.json
# --------------------------------------------------------------------------- #
def _check_news(ctx: Ctx, out: list[Finding]):
    n = ctx.news
    if n is None:
        return

    if isinstance(n, list):
        items = n
    elif isinstance(n, dict):
        items = n.get("articles")
        if items is None:
            items = n.get("items")
        if items is None:
            out.append(Finding(
                severity=ERROR, code="L1.NEWS_ITEMS_MISSING",
                message="news/feed.json has no 'items' list.",
                evidence=trunc(sorted(n.keys())),
                impact=("The app looks for 'articles' or 'items'. With neither, the "
                        "news screen is empty and the alert bell never lights up."),
                fix="Put the stories under a top-level \"items\" key.",
            ))
            return
        for key in ("version", "updated_at"):
            if key not in n:
                out.append(Finding(
                    severity=WARN, code="L1.NEWS_HEADER_MISSING",
                    message=f"news/feed.json has no '{key}'.",
                    field=key,
                    impact=("The app compares the feed's version against the one it "
                            "already has. Without a version it cannot tell whether "
                            "there is anything new, so nothing refreshes."),
                    fix=f"Add '{key}' to the top of the feed.",
                ))
            elif not isinstance(n[key], (str, int)):
                out.append(Finding(
                    severity=WARN, code="L1.NEWS_HEADER_TYPE",
                    message=(f"'{key}' in news/feed.json is {_a(_jtype(n[key]))}; it "
                             f"should be text."),
                    field=key, evidence=trunc(n[key]),
                    impact="The app may not be able to tell this feed apart from the last one.",
                    fix=f"Write '{key}' as text.",
                ))
        if isinstance(n.get("updated_at"), str) and not iso_ok(n["updated_at"]):
            out.append(Finding(
                severity=WARN, code="L1.DATE_NOT_ISO",
                message="'updated_at' in news/feed.json is not written as a date.",
                field="updated_at", evidence=trunc(n["updated_at"]),
                impact="Nothing can tell how fresh the feed is.",
                fix="Write it as YYYY-MM-DDTHH:MM:SSZ.",
            ))
    else:
        out.append(Finding(
            severity=ERROR, code="L1.NEWS_FILE_SHAPE",
            message=(f"news/feed.json is {_a(_jtype(n))}; it must be a list of "
                     f"stories or an object with an 'items' list."),
            impact="The app cannot read it, so the news screen stays empty.",
            fix="Emit the file as {\"version\": ..., \"items\": [ ... ]}.",
        ))
        return

    if not isinstance(items, list):
        out.append(Finding(
            severity=ERROR, code="L1.NEWS_FILE_SHAPE",
            message=f"The stories in news/feed.json are {_a(_jtype(items))}, not a list.",
            evidence=trunc(items),
            impact="The app cannot read them, so the news screen stays empty.",
            fix="Write the stories as a JSON list.",
        ))
        return

    bad_rows = [(j, r) for j, r in enumerate(items) if not isinstance(r, dict)]
    if bad_rows:
        out.append(Finding(
            severity=ERROR, code="L1.NEWS_ROW_NOT_AN_OBJECT",
            message=(f"{_count_word(len(bad_rows), 'story')} out of {len(items)} in "
                     f"news/feed.json are not story objects."),
            index=bad_rows[0][0], evidence=_sample(bad_rows),
            impact=("The app reads the whole feed in one pass with no per-story "
                    "safety net. One bad story throws and every story is lost — the "
                    "news screen goes empty for everyone."),
            fix="Every entry must be a JSON object.",
        ))

    bag = _Bag()
    ids = defaultdict(list)
    bad_cards = []
    for j, r in enumerate(items):
        if not isinstance(r, dict):
            continue
        try:
            _check_row(r, NEWS_SPEC, bag, j)
            rid = r.get("id")
            if isinstance(rid, str) and rid.strip():
                ids[rid].append(j)
            ac = r.get("affected_cards")
            if isinstance(ac, list):
                offenders = [x for x in ac if not isinstance(x, str)]
                if offenders:
                    bad_cards.append((j, offenders))
        except Exception:
            bag.add(("UNKNOWN_KEY", "<unreadable story>", None), j, trunc(r))

    _emit(bag, out, card_id=None, block="news", subject="story",
          file_hint="news/feed.json")

    for rid, where in ids.items():
        if len(where) > 1:
            out.append(Finding(
                severity=WARN, code="L1.NEWS_ID_DUPLICATE",
                message=f"The story id '{rid}' appears {len(where)} times in news/feed.json.",
                index=where[0], evidence="stories " + ", ".join(str(w + 1) for w in where),
                impact="The same story can be shown twice, and marking one read does "
                       "not reliably clear the other.",
                fix="Give each story its own id.",
            ))

    if bad_cards:
        out.append(Finding(
            severity=ERROR, code="L1.NEWS_AFFECTED_CARDS_NOT_TEXT",
            message=(f"{_count_word(len(bad_cards), 'story')} list affected cards "
                     f"that are not written as text."),
            index=bad_cards[0][0],
            evidence=trunc([o for _j, offs in bad_cards[:3] for o in offs[:3]]),
            impact=("The app converts this list to text with no safety net. A number "
                    "in it throws, and because the whole feed is read in one pass, "
                    "every story is lost — the news screen goes empty for everyone."),
            fix="Write every affected card id as a quoted string.",
        ))
