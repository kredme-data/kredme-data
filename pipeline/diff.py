#!/usr/bin/env python3
"""
diff.py — turn verified issuer observations into a reviewable, field-level patch.

Usage:
    python3 pipeline/diff.py --observations .pipeline-work/verified.json
    python3 pipeline/diff.py --observations .pipeline-work/verified.json \
                             --cards seed/cards.json \
                             --out .pipeline-work/pr_body.md \
                             --summary-json .pipeline-work/diff_summary.json

    from pipeline.diff import observations_to_proposals, gate, apply_proposals

This module is the thing the previous scraper did not have. That scraper answered any
drift with shutil.copyfile() over the card file, which is how hand-curated benefits,
sources and changelog entries were destroyed without anyone noticing. Nothing here
ever replaces a card. It proposes ONE field at a time, carries the sentence the number
came from, refuses to apply anything a person should read first, and proves on the way
out that it touched nothing else.

Exit codes: 0 ok, 1 the file's contents are wrong, 2 the file is not there.

Stdlib only. No network, no API client — this module must stay importable and
testable on a bare Python.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import pathlib
import sys
from dataclasses import dataclass, replace
from typing import Any

# Running this file directly puts pipeline/ on sys.path rather than the repo root, so
# the package import below would fail with "No module named pipeline". Adding the repo
# root keeps `python3 pipeline/diff.py` and `from pipeline import diff` both working.
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline import config as C  # noqa: E402  (must follow the sys.path bootstrap)


# ---------------------------------------------------------------------------
# Units
#
# A unit here describes what `new_value` IS, not what the issuer said. The two
# differ on purpose: the issuer says "4 Reward Points per Rs 150" and we store
# 0.02666667 points per rupee, because that is the unit seed/cards.json actually
# uses for card.base_reward_rate (verified: 4/150 == 0.026667, ten cards).
# ---------------------------------------------------------------------------
UNIT_POINTS_PER_RUPEE = "points_per_rupee"
UNIT_INR_PER_POINT = "inr_per_point"
UNIT_INR = "inr"
UNIT_PERCENT = "percent"
UNIT_FLAG = "flag"

# ---------------------------------------------------------------------------
# What an extracted observation maps onto.
#
# Observation field -> (JSON path into the card entry, unit of the stored value).
#
# DELIBERATELY NOT MAPPED, and this is the point of `unmapped_field` rather than an
# oversight:
#   joining_fee_inr        no card in seed/cards.json carries a joining-fee key. All
#                          380 cards have the same 21 keys and that is not one of
#                          them. Inventing it would ship a field the app never reads.
#   category_rate/_cap,    these live on reward_rules, and a rule is addressed by its
#   excluded_category      rule_name — the exact string the app keys every user's cap
#                          progress on. This module does not go near rule arrays.
#   lounge_*, milestone_*, stored as prose in benefits/milestone_rules, not as a
#   points_expiry_months,  number this module can safely move.
#   fuel_surcharge_*
# Every one of those still comes back as a visible, blocked Proposal, because a field
# the issuer publishes and we cannot store is a gap in our schema worth seeing.
# ---------------------------------------------------------------------------
_TARGETS: dict[str, tuple[str, str]] = {
    "base_reward_rate": ("card.base_reward_rate", UNIT_POINTS_PER_RUPEE),
    "point_value_inr": ("card.rp_value_standard", UNIT_INR_PER_POINT),
    "annual_fee_inr": ("card.annual_fee", UNIT_INR),
    "forex_markup_pct": ("card.forex_markup_pct", UNIT_PERCENT),
    "card_discontinued": ("card.is_active", UNIT_FLAG),
}

_SETTABLE_PATHS = frozenset(path for path, _ in _TARGETS.values())

# Fields where "the number went up" means "the cardholder earns more". Those are the
# ones the asymmetry rule in gate() applies to. A fee going up is not in here: fees
# genuinely rise, and blocking every fee increase would mean we never track any.
REWARD_RATE_FIELDS = frozenset({"base_reward_rate", "point_value_inr", "category_rate"})

# The rule arrays we must leave byte-identical. Named explicitly so a new array added
# to the schema later fails loudly here rather than being quietly unprotected.
RULE_ARRAYS = (
    "reward_rules",
    "exclusion_rules",
    "milestone_rules",
    "redemption_rules",
    "fuel_surcharge_rules",
)

# Mirrors CreditCardData.sanePointValue (credit_card.dart:533) and tools/kredme.py:118.
# The app collapses any point value outside this range to Rs 0.25, so storing 5.0
# would not raise the card's rate — it would silently reset it and take every rule on
# the card with it. Two cards in the live catalogue already do this (5.0 and 300.0).
APP_POINT_VALUE_MAX = 1.5

# A quote shorter than this is a fragment, not a sentence, and cannot be checked
# against the source document by the person reading the PR.
MIN_QUOTE_CHARS = 20

# Where an applied change records what it was based on, on the card ENTRY (beside the
# rule arrays, never inside them). The four inner key names are fixed by the app.
PROVENANCE_KEY = "_provenance"

# Block reasons. `unmapped_field:` and `unknown_card:` carry a suffix.
REASON_UNMAPPED = "unmapped_field: "
REASON_UNKNOWN_CARD = "unknown_card: "
REASON_MALFORMED = "malformed_observation"
REASON_UNPARSEABLE = "unparseable"
REASON_NO_QUOTE = "no_quote"
REASON_WEASEL = "weasel_phrase"
REASON_NOT_ISSUER = "not_issuer_domain"
REASON_CEILING = "above_ceiling"
REASON_FLOOR = "below_floor"
REASON_UPWARD = "upward_revision"
REASON_LOW_CONFIDENCE = "low_confidence"
REASON_LARGE_DELTA = "large_delta"

# Reasons gate() cannot overturn because they are about the shape of the input, not
# about whether the number is trustworthy.
_STRUCTURAL_REASONS = (REASON_UNMAPPED, REASON_UNKNOWN_CARD, REASON_MALFORMED)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
_ZERO_WORDS = {"nil", "free", "zero"}

# "waived" is NOT here. A waived fee is conditional on spend; reading it as 0 would
# tell a cardholder a fee-bearing card is free.
_TRUE_WORDS = {"true", "yes", "y", "1", "discontinued", "withdrawn", "closed", "sunset"}
_FALSE_WORDS = {"false", "no", "n", "0", "active", "available", "open"}

_PERCENT_UNITS = {"percent", "pct", "%"}
_POINT_UNITS = {"points", "point", "reward_points", "rp"}


def _fnum(v: object) -> float | None:
    """A finite JSON number, or None. Booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _round(x: float, places: int) -> float:
    """Round, and never emit -0.0 — it round-trips through JSON as a visible diff."""
    r = round(x, places)
    return 0.0 if r == 0 else r


def _to_number(value: object) -> float | None:
    """A number out of whatever the extractor put in `value`, or None.

    EXTRACTION_SCHEMA types every value as a string on purpose: the model reports the
    issuer's own token and never its own arithmetic. So this is where "Rs 1,500",
    "3.5%" and "Nil" become numbers. Anything it cannot read returns None and becomes
    a blocked proposal — guessing here writes a wrong number into a live catalogue.
    """
    direct = _fnum(value)
    if direct is not None:
        return direct
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    if s in _ZERO_WORDS:
        return 0.0
    for junk in ("₹", "rs.", "rs", "inr", ",", "%", "/-", " "):
        s = s.replace(junk, "")
    try:
        return _fnum(float(s))
    except ValueError:
        return None


def _to_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    return None


def _today() -> str:
    """Date stamp in the format the existing provenance in seed/cards.json uses."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Proposal:
    """One field, on one card, with the sentence that justifies changing it.

    Frozen because the whole safety story of this module is that a proposal is
    decided once and then only read. gate() returns a new Proposal rather than
    editing one, so nothing downstream can quietly flip auto_applicable to True.
    """

    card_id: str
    field: str
    path: str
    old_value: object
    new_value: object
    unit: str
    source_url: str
    source_quote: str
    confidence: str
    delta_pct: float | None
    auto_applicable: bool = False
    blocked_reason: str = ""


# ---------------------------------------------------------------------------
# Observations -> proposals
# ---------------------------------------------------------------------------
def observations_to_proposals(
    card_entry: dict, observations: list[dict], source_url: str
) -> list[Proposal]:
    """Map verified observations onto concrete fields of one card entry.

    Returns one Proposal per observation, already passed through gate(). Nothing is
    ever dropped: an observation we cannot map, cannot read, or must not trust still
    comes back with auto_applicable=False and a reason, because a silent drop is how
    a schema gap stays invisible for a year.
    """
    if not isinstance(card_entry, dict):
        raise TypeError(f"card_entry must be a dict, got {type(card_entry).__name__}")
    inner = card_entry.get("card")
    if not isinstance(inner, dict):
        raise ValueError("card entry has no 'card' object")
    card_id = str(inner.get("id") or "").strip()
    if not card_id:
        raise ValueError("card entry has no 'card.id'")
    if not isinstance(observations, list):
        raise TypeError(
            f"observations must be a list, got {type(observations).__name__}"
        )

    url = source_url if isinstance(source_url, str) else ""
    return [gate(_propose(inner, card_id, obs, url)) for obs in observations]


def _propose(inner: dict, card_id: str, obs: object, url: str) -> Proposal:
    """Build the ungated Proposal for a single observation."""
    if not isinstance(obs, dict):
        return Proposal(
            card_id=card_id, field="", path="", old_value=None, new_value=None,
            unit="", source_url=url, source_quote="", confidence="",
            delta_pct=None, blocked_reason=REASON_MALFORMED,
        )

    name = str(obs.get("field") or "").strip()
    quote = obs.get("source_quote") if isinstance(obs.get("source_quote"), str) else ""
    confidence = str(obs.get("confidence") or "").strip().lower()
    raw_unit = str(obs.get("unit") or "").strip().lower()

    if not name:
        return Proposal(
            card_id=card_id, field="", path="", old_value=None,
            new_value=obs.get("value"), unit=raw_unit, source_url=url,
            source_quote=quote, confidence=confidence, delta_pct=None,
            blocked_reason=REASON_MALFORMED,
        )

    target = _TARGETS.get(name)
    if target is None:
        return Proposal(
            card_id=card_id, field=name, path=f"<unmapped>.{name}", old_value=None,
            new_value=obs.get("value"), unit=raw_unit, source_url=url,
            source_quote=quote, confidence=confidence, delta_pct=None,
            blocked_reason=REASON_UNMAPPED + name,
        )

    path, unit = target
    key = path.split(".", 1)[1]
    old_value = inner.get(key)
    new_value = _convert(name, obs, inner)
    return Proposal(
        card_id=card_id, field=name, path=path, old_value=old_value,
        new_value=new_value, unit=unit, source_url=url, source_quote=quote,
        confidence=confidence, delta_pct=_delta_pct(old_value, new_value, unit),
    )


def _convert(name: str, obs: dict, inner: dict) -> object:
    """The observation's value in the unit seed/cards.json stores, or None."""
    raw = obs.get("value")

    if name == "base_reward_rate":
        return _convert_base_rate(raw, obs.get("per_spend_inr"),
                                  str(obs.get("unit") or "").strip().lower(), inner)

    if name == "point_value_inr":
        v = _to_number(raw)
        # Outside this range the app does not store what we wrote — it substitutes
        # Rs 0.25 and every rate on the card moves with it. Not storable, so it goes
        # to a human instead.
        if v is None or v <= 0 or v > APP_POINT_VALUE_MAX:
            return None
        return _round(v, 6)

    if name == "annual_fee_inr":
        v = _to_number(raw)
        return None if v is None or v < 0 else _round(v, 2)

    if name == "forex_markup_pct":
        v = _to_number(raw)
        return None if v is None or v < 0 else _round(v, 4)

    if name == "card_discontinued":
        flag = _to_bool(raw)
        # is_active is stored as an int (1/0) on all 380 cards, not a bool. Writing
        # True here would change the JSON type of a field the app reads as a number.
        return None if flag is None else (0 if flag else 1)

    return None


def _convert_base_rate(
    raw: object, per_spend: object, unit_in: str, inner: dict
) -> float | None:
    """'4 points per Rs 150' -> 0.02666667 points per rupee.

    ★ TRAP: this is a change of denominator, never a collapse into a percentage. The
    percentage depends on a point value the document almost never states, and a wrong
    point value silently corrupts the rate. The block size itself lives on the reward
    RULES (reward_rate x reward_unit_spend), which this module never touches.
    """
    if unit_in in _POINT_UNITS:
        points = _to_number(raw)
        block = _to_number(per_spend)
        if points is None or points < 0 or block is None or block <= 0:
            return None  # "4 points" with no block is not a rate, it is half a rate
        return _round(points / block, 8)

    if unit_in in _PERCENT_UNITS:
        pct = _to_number(raw)
        if pct is None or pct < 0:
            return None
        # The app renders base_reward_rate * point_value * 100. Storing pct/100 is
        # therefore the issuer's percentage only when a point is worth exactly Rs 1 —
        # a cashback card (38 of 42 cashback_inr cards store 1.0). On a points card
        # the same write renders 4x low today and moves again the moment somebody
        # corrects the point value. That is the unit bug that put 0.02% on Axis Neo,
        # so refuse it and let a person decide.
        point_value = _fnum(inner.get("rp_value_standard"))
        if point_value is None or abs(point_value - 1.0) > 1e-9:
            return None
        return _round(pct / 100.0, 8)

    return None


def _delta_pct(old: object, new: object, unit: str) -> float | None:
    """Relative change, or None when the question does not have an answer.

    None when there is nothing to compare against — including old == 0, which 106 of
    the 380 cards genuinely store for base_reward_rate. Dividing there is the bug,
    not the guard.
    """
    if unit == UNIT_FLAG:
        return None  # the relative change between two flags is not a number
    o, n = _fnum(old), _fnum(new)
    if o is None or n is None or o == 0:
        return None
    return _round(((n - o) / abs(o)) * 100.0, 4)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def gate(p: Proposal) -> Proposal:
    """Decide whether this change may be applied without a person reading it."""
    if not isinstance(p, Proposal):
        raise TypeError(f"gate() takes a Proposal, got {type(p).__name__}")
    reason = _block_reason(p)
    return replace(p, auto_applicable=(reason == ""), blocked_reason=reason)


def _block_reason(p: Proposal) -> str:
    """The first reason this change must not apply itself, or "".

    The order is the order a reviewer wants to read: whether the SOURCE is worth
    anything at all comes before whether the NUMBER is plausible, which comes before
    how confident a model felt. A quote that says "up to" is worthless however
    confident the model was, so saying "low confidence" there would misdirect.
    """
    if p.blocked_reason.startswith(_STRUCTURAL_REASONS):
        return p.blocked_reason

    if not _is_storable(p):
        return REASON_UNPARSEABLE

    quote = p.source_quote if isinstance(p.source_quote, str) else ""
    if len(quote.strip()) < MIN_QUOTE_CHARS:
        return REASON_NO_QUOTE
    if C.contains_weasel(quote):
        return REASON_WEASEL
    if not C.is_issuer_domain(p.source_url):
        return REASON_NOT_ISSUER

    implied = _implied_pct(p)
    if implied is not None and implied > C.RATE_CEILING_PCT:
        return REASON_CEILING
    # ⚠ The floor is mirrored from config, but the two floors do not agree today:
    # config.RATE_FLOOR_PCT is 0.0 while tools/kredme.py enforces 0.1, and a NEW card
    # under 0.1% is a hard FAIL there. So a downward correction landing at, say,
    # 0.09% passes this gate and then fails the publish gate — which is exactly the
    # "cannot pass here and then fail the publish gate" promise config.py makes.
    # Raising config.RATE_FLOOR_PCT to 0.1 closes it; this line needs no change.
    if implied is not None and implied < C.RATE_FLOOR_PCT:
        return REASON_FLOOR

    # THE ASYMMETRY. Corrections read out of issuer prose have only ever been valid
    # downward. An early pass raised one rule from 10% to 33% off a marketing
    # sentence, and the whole catalogue audit found 129 of 376 cards contradicting
    # their own text in the same direction. So an upward reward revision goes to a
    # person every time, at any confidence, including the 0 -> something case on the
    # 106 cards that store no base rate at all.
    if _is_upward_reward_revision(p):
        return REASON_UPWARD

    if p.confidence != "high":
        return REASON_LOW_CONFIDENCE
    if p.delta_pct is not None and abs(p.delta_pct) > C.MAX_AUTO_DELTA_PCT:
        return REASON_LARGE_DELTA
    return ""


def _is_storable(p: Proposal) -> bool:
    """Is new_value the type this field actually holds in seed/cards.json?"""
    v = p.new_value
    if isinstance(v, bool):
        return False  # is_active and every rate are numbers in the seed, never bools
    if p.unit == UNIT_FLAG:
        return isinstance(v, int) and v in (0, 1)
    return _fnum(v) is not None


def _implied_pct(p: Proposal) -> float | None:
    """The percentage of spend this change would make the card render at.

    Exact for the percent-denominated fields. For points per rupee it is an UPPER
    bound: one point is worth at most about a rupee, so value x 100 is the most the
    app could ever render. Using the card's real point value would be tighter, but the
    ceiling only has to catch the absurd, and an upper bound can only fail by letting
    something through to a human — never by blocking a rate that is genuinely fine.
    """
    v = _fnum(p.new_value)
    if v is None:
        return None
    if p.unit == UNIT_PERCENT:
        return v
    if p.unit == UNIT_POINTS_PER_RUPEE:
        return v * 100.0
    return None


def _is_upward_reward_revision(p: Proposal) -> bool:
    """True when this raises what a cardholder earns.

    An old value of None is a gap being filled, not a revision, and is judged on
    confidence and quote like anything else. An old value of 0 IS a number, so 0 ->
    anything counts as upward.
    """
    if p.field not in REWARD_RATE_FIELDS:
        return False
    o, n = _fnum(p.old_value), _fnum(p.new_value)
    if o is None or n is None:
        return False
    return n > o


# ---------------------------------------------------------------------------
# Applying
#
# Everything above decides. This applies, and then proves what it did not do.
# ---------------------------------------------------------------------------
def apply_proposals(
    cards: list[dict], proposals: list[Proposal], *, only_auto: bool = True
) -> tuple[list[dict], list[Proposal]]:
    """Return (new_cards, applied). The input list is never mutated.

    Field-level and additive: the ONLY thing that changes on a card is the single key
    each applied proposal names, plus a provenance record beside the rule arrays. No
    card is ever replaced, no key is ever removed, and no rule is ever read for
    anything other than proving it came out the far side untouched.
    """
    if not isinstance(cards, list):
        raise TypeError(f"cards must be a list, got {type(cards).__name__}")
    if not isinstance(proposals, list):
        raise TypeError(f"proposals must be a list, got {type(proposals).__name__}")

    before_names = _rule_names(cards)
    before_rules = _rule_fingerprint(cards)
    before_keys = _card_key_shape(cards)

    new_cards = copy.deepcopy(cards)
    index: dict[str, dict] = {}
    for entry in new_cards:
        if not isinstance(entry, dict) or not isinstance(entry.get("card"), dict):
            continue
        cid = entry["card"].get("id")
        if isinstance(cid, str) and cid and cid not in index:
            index[cid] = entry

    stamped_on = _today()
    applied: list[Proposal] = []
    for p in proposals:
        if not isinstance(p, Proposal):
            raise TypeError(f"proposals must hold Proposal, got {type(p).__name__}")
        if only_auto and not p.auto_applicable:
            continue
        if p.path not in _SETTABLE_PATHS:
            continue  # never write a path this module did not define itself
        if not _is_storable(p):
            continue
        entry = index.get(p.card_id)
        if entry is None:
            continue
        inner = entry["card"]
        key = p.path.split(".", 1)[1]
        if key not in inner:
            continue  # never invent a key: the app would not read it and the gate
            # in tools/kredme.py would not check it
        inner[key] = p.new_value
        _stamp_provenance(entry, p, stamped_on)
        applied.append(p)

    # ★ TRAP 1. The app keys every user's cap progress on '${cardId}|${ruleName}'
    # (app_database.dart:238). Renaming one rule orphans that user's spend history and
    # resets their cap mid-cycle, silently. A bare `assert` is not used because
    # python -O deletes it, and this is the check that protects real people's data.
    if _rule_names(new_cards) != before_names:
        raise AssertionError(
            "apply_proposals changed a rule_name — this orphans users' cap progress"
        )
    # ★ TRAP 2. Nothing may be replaced wholesale. Every rule array, byte for byte.
    if _rule_fingerprint(new_cards) != before_rules:
        raise AssertionError("apply_proposals modified a rule array")
    # And no card gained or lost a key; only values of existing keys moved.
    if _card_key_shape(new_cards) != before_keys:
        raise AssertionError("apply_proposals added or removed a key on card.*")

    return new_cards, applied


def _stamp_provenance(entry: dict, p: Proposal, stamped_on: str) -> None:
    """Record what this change was based on, on the entry beside the rule arrays.

    One record per field path, replaced rather than appended, so the file stays
    bounded no matter how many weeks the pipeline runs — git already holds the history
    of every earlier source.
    """
    # The app defaults a missing `confidence` to 'high', so an empty stamp would make
    # an unreviewed number claim to be verified. Every AUTO-applied proposal is 'high'
    # by construction (the gate refuses anything else), so this writes 'high' on the
    # normal path and the truth on the only_auto=False override path.
    confidence = p.confidence if p.confidence in ("high", "medium", "low") else "low"
    record = {
        "field": p.field,
        "path": p.path,
        "old_value": p.old_value,
        "new_value": p.new_value,
        "source_url": p.source_url,
        "source_quote": p.source_quote,
        "confidence": confidence,
        "source_fetched_on": stamped_on,
    }
    records = entry.get(PROVENANCE_KEY)
    if not isinstance(records, list):
        records = []
        entry[PROVENANCE_KEY] = records
    for i, existing in enumerate(records):
        if isinstance(existing, dict) and existing.get("path") == p.path:
            records[i] = record
            return
    records.append(record)


def _rule_names(cards: list) -> list[tuple]:
    """Every rule_name in the catalogue, positionally addressed."""
    out: list[tuple] = []
    for n, entry in enumerate(cards):
        if not isinstance(entry, dict):
            continue
        for arr in RULE_ARRAYS:
            rules = entry.get(arr)
            if not isinstance(rules, list):
                continue
            for i, rule in enumerate(rules):
                if isinstance(rule, dict):
                    out.append((n, arr, i, rule.get("rule_name")))
    return out


def _rule_fingerprint(cards: list) -> str:
    payload = [
        {arr: entry.get(arr) for arr in RULE_ARRAYS} if isinstance(entry, dict) else None
        for entry in cards
    ]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _card_key_shape(cards: list) -> list:
    out = []
    for entry in cards:
        inner = entry.get("card") if isinstance(entry, dict) else None
        out.append(sorted(inner.keys()) if isinstance(inner, dict) else None)
    return out


# ---------------------------------------------------------------------------
# Rendering — the PR body a non-technical person reads and merges
# ---------------------------------------------------------------------------
# Nobody outside this repo should have to read a snake_case field name, so every
# field EXTRACTION_SCHEMA can emit has a plain-English label, mapped or not.
_FIELD_LABELS = {
    "base_reward_rate": "base earn rate",
    "reward_unit_spend": "the spend block points are earned in",
    "point_value_inr": "what one reward point is worth",
    "annual_fee_inr": "annual fee",
    "joining_fee_inr": "joining fee",
    "fee_waiver_spend_inr": "spend needed to get the fee waived",
    "forex_markup_pct": "foreign-currency markup",
    "card_discontinued": "availability",
    "category_rate": "earn rate on one spend category",
    "category_cap": "cap on one spend category",
    "excluded_category": "a spend type that earns nothing",
    "lounge_domestic_visits": "free domestic lounge visits",
    "lounge_spend_gate_inr": "spend needed to unlock lounge access",
    "milestone_spend_inr": "spend that triggers a milestone bonus",
    "points_expiry_months": "how long points last",
    "fuel_surcharge_waiver_pct": "fuel surcharge waiver",
}

_REASON_TITLES = {
    REASON_UPWARD: "The bank's page says this card earns MORE than we have on file",
    REASON_WEASEL: "The sentence says \"up to\"",
    REASON_NOT_ISSUER: "Not from the bank's own website",
    REASON_CEILING: "The new rate is too high to be real",
    REASON_FLOOR: "The new rate is too low to be real",
    REASON_LARGE_DELTA: "A very big jump from what we have",
    REASON_NO_QUOTE: "No sentence from the bank to back it up",
    REASON_UNPARSEABLE: "We could not read the value",
    REASON_LOW_CONFIDENCE: "The check was not confident",
    REASON_MALFORMED: "The observation itself was broken",
}

_REASON_HELP = {
    REASON_UPWARD: (
        "Every time a rate went UP off a bank's prose it turned out to be marketing, "
        "not a mechanic — one early pass read a sentence as raising a card from 10% to "
        "33%. So these never apply on their own, however sure the check was. Open the "
        "link, read the sentence, and if the bank really did improve the card, say so."
    ),
    REASON_WEASEL: (
        "\"Up to 10% back\" is a ceiling a marketer chose, not what a cardholder earns "
        "at the till. We never take a number out of one of these sentences."
    ),
    REASON_NOT_ISSUER: (
        "The page is not on the bank's own domain. Card comparison sites copy each "
        "other's mistakes, and their numbers are how the catalogue went stale before."
    ),
    REASON_CEILING: (
        "Nothing in India earns more than about 33% (HDFC SmartBuy at 10X), so anything "
        "over 40% has always turned out to be a units mix-up rather than a good card."
    ),
    REASON_FLOOR: (
        "A card earning almost nothing is nearly always a units mix-up rather than a "
        "bad card — Axis Neo renders 0.02% for that reason and earns far more."
    ),
    REASON_LARGE_DELTA: (
        "The number moved by more than half of what we had. That is either a real "
        "devaluation worth telling users about, or a units mix-up. Both need eyes."
    ),
    REASON_NO_QUOTE: (
        "We only change a number when we can show the bank's own sentence beside it. "
        "There isn't one here."
    ),
    REASON_UNPARSEABLE: (
        "The value did not come back as a number we can store in that field — or it "
        "was outside the range the app can actually hold."
    ),
    REASON_LOW_CONFIDENCE: (
        "The second-pass check would not call this confirmed. It is usually a page "
        "that describes a whole family of cards rather than this one."
    ),
    REASON_MALFORMED: "The observation did not have the shape we expect. Worth a look at the extraction step.",
}

_UNMAPPED_HELP = (
    "The bank publishes this, and we have nowhere to put it. Nothing is broken — but "
    "each of these is a real gap in what the app can tell someone."
)
_UNKNOWN_CARD_HELP = (
    "We were given a reading for a card that is not in the catalogue. Either the card "
    "was renamed, or the extraction step is using a stale id."
)
_MAX_QUOTE_CHARS = 400


def render_markdown(proposals: list[Proposal]) -> str:
    """The PR body. Counts first, then what changed, then what a person must decide."""
    if not isinstance(proposals, list):
        raise TypeError(f"proposals must be a list, got {type(proposals).__name__}")

    s = summarise(proposals)
    out = ["# Card data: what this week's check at the banks found", ""]

    if s["total"] == 0:
        out += [
            "Nothing to review. Either the pages we watch did not move, or nothing on "
            "them disagreed with what we already ship.",
            "",
        ]
        return "\n".join(out)

    cards = len(s["by_card"])
    out += [
        f"**{_count(s['auto'], 'change')} applied. "
        f"{_count(s['blocked'], 'change')} held back for you to decide.** "
        f"{s['total']} in total across {_count(cards, 'card')}.",
        "",
        "Every line carries the bank's own sentence and a link to the page it came "
        "from. Nothing below was published — this pull request is the step where a "
        "person looks first.",
        "",
    ]

    auto = [p for p in proposals if p.auto_applicable]
    blocked = [p for p in proposals if not p.auto_applicable]

    if auto:
        out += [
            "## Applied",
            "",
            "These moved a number we already store, matched the bank's own page word "
            "for word, and moved it in the direction that is safe to trust.",
            "",
        ]
        for p in auto:
            out += _render_one(p)
        out.append("")

    if blocked:
        out += [
            "## Held back",
            "",
            "None of these changed anything. They are here because they are the ones "
            "worth an opinion.",
            "",
        ]
        # Biggest group first: that is the one thing worth fixing this week.
        groups: dict[str, list[Proposal]] = {}
        for p in blocked:
            groups.setdefault(p.blocked_reason or REASON_MALFORMED, []).append(p)
        for reason in sorted(groups, key=lambda r: (-len(groups[r]), r)):
            items = groups[reason]
            out += [f"### {_reason_title(reason)} ({len(items)})", "",
                    _reason_help(reason), ""]
            for p in items:
                out += _render_one(p)
            out.append("")

    return "\n".join(out).rstrip() + "\n"


def _render_one(p: Proposal) -> list[str]:
    label = _FIELD_LABELS.get(p.field, p.field or "something we could not name")
    line = f"- **{p.card_id or 'unknown card'}** — {label}: " \
           f"{_fmt(p.old_value, p.unit)} → {_fmt(p.new_value, p.unit)}"
    if p.delta_pct is not None:
        line += f" ({p.delta_pct:+.1f}%)"
    rows = [line]
    quote = (p.source_quote or "").strip()
    if quote:
        rows.append(f"  > {_one_line(quote)}")
    else:
        rows.append("  > (the bank's sentence was not supplied)")
    if p.source_url:
        rows.append(f"  [Where this came from]({p.source_url})")
    return rows


def _one_line(text: str) -> str:
    """Flatten a quote so it cannot break out of its blockquote."""
    flat = " ".join(text.split())
    if len(flat) > _MAX_QUOTE_CHARS:
        flat = flat[:_MAX_QUOTE_CHARS].rstrip() + "…"
    return flat


def _fmt(value: object, unit: str) -> str:
    if unit == UNIT_FLAG:
        if value == 1:
            return "still offered"
        if value == 0:
            return "no longer offered"
        return "not set"
    if value is None:
        return "not set"
    n = _fnum(value)
    if n is None:
        return str(value)
    if unit == UNIT_INR:
        return _inr(n)
    if unit == UNIT_PERCENT:
        return f"{n:g}%"
    if unit == UNIT_INR_PER_POINT:
        return f"{_inr(n)} a point"
    if unit == UNIT_POINTS_PER_RUPEE:
        # Per ₹100 rather than per ₹1: "0.0266667 points per ₹1" is the stored unit
        # and unreadable, and the issuer's own sentence sits directly underneath.
        return f"{n * 100:.4g} points per ₹100"
    return f"{n:g}"


def _inr(n: float) -> str:
    """Rupees, grouped the Indian way: ₹1,00,000 rather than ₹100,000."""
    neg = n < 0
    whole = int(abs(n))
    paise = abs(n) - whole
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    if paise >= 0.005:
        s += f"{paise:.2f}"[1:]
    return ("-₹" if neg else "₹") + s


def _reason_title(reason: str) -> str:
    if reason.startswith(REASON_UNMAPPED):
        return f"We do not store this: {reason[len(REASON_UNMAPPED):]}"
    if reason.startswith(REASON_UNKNOWN_CARD):
        return f"Card not in our catalogue: {reason[len(REASON_UNKNOWN_CARD):]}"
    return _REASON_TITLES.get(reason, reason)


def _reason_help(reason: str) -> str:
    if reason.startswith(REASON_UNMAPPED):
        return _UNMAPPED_HELP
    if reason.startswith(REASON_UNKNOWN_CARD):
        return _UNKNOWN_CARD_HELP
    return _REASON_HELP.get(reason, "Held back for review.")


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def summarise(proposals: list[Proposal]) -> dict:
    """The five numbers that fit in a job summary."""
    if not isinstance(proposals, list):
        raise TypeError(f"proposals must be a list, got {type(proposals).__name__}")
    by_reason: dict[str, int] = {}
    by_card: dict[str, int] = {}
    auto = 0
    for p in proposals:
        by_card[p.card_id] = by_card.get(p.card_id, 0) + 1
        if p.auto_applicable:
            auto += 1
        else:
            reason = p.blocked_reason or REASON_MALFORMED
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {
        "total": len(proposals),
        "auto": auto,
        "blocked": len(proposals) - auto,
        "by_reason": dict(sorted(by_reason.items())),
        "by_card": dict(sorted(by_card.items())),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _records(doc: Any) -> list[dict]:
    """The per-card records out of a verified-observations document.

    Accepts {"cards": [...]}, a bare list of records, or a single record, because the
    stage that writes this file and this one are separate jobs in separate workflow
    runs and should not be able to fail each other over a wrapper key.
    """
    if isinstance(doc, dict):
        if isinstance(doc.get("cards"), list):
            rows = doc["cards"]
        elif "card_id" in doc:
            rows = [doc]
        else:
            raise ValueError("expected an object with a 'cards' list, or a list")
    elif isinstance(doc, list):
        rows = doc
    else:
        raise ValueError(f"expected an object or a list, got {type(doc).__name__}")

    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("every record must be an object with a 'card_id'")
        out.append(row)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Turn verified issuer observations into a reviewable patch, and "
                    "print the pull-request body a person reads before it ships.",
    )
    ap.add_argument("--observations", required=True, type=pathlib.Path,
                    help="verified observations JSON from the verification stage")
    ap.add_argument("--cards", type=pathlib.Path, default=C.CARDS_JSON,
                    help="card catalogue to diff against (default: seed/cards.json)")
    ap.add_argument("--out", type=pathlib.Path,
                    help="also write the markdown here (the PR body path)")
    ap.add_argument("--summary-json", type=pathlib.Path, dest="summary_json",
                    help="also write the counts here as JSON")
    args = ap.parse_args()

    for path, what in ((args.observations, "verified observations"),
                       (args.cards, "card catalogue")):
        if not path.exists():
            print(f"error: no {what} file at {path}", file=sys.stderr)
            return 2

    try:
        doc = json.loads(args.observations.read_text(encoding="utf-8"))
        cards = json.loads(args.cards.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"error: could not read the input: {exc}", file=sys.stderr)
        return 1

    if not isinstance(cards, list):
        print(f"error: {args.cards} is not a list of card entries", file=sys.stderr)
        return 1
    try:
        records = _records(doc)
    except ValueError as exc:
        print(f"error: {args.observations}: {exc}", file=sys.stderr)
        return 1

    index = {}
    for entry in cards:
        if isinstance(entry, dict) and isinstance(entry.get("card"), dict):
            cid = entry["card"].get("id")
            if isinstance(cid, str) and cid:
                index.setdefault(cid, entry)

    proposals: list[Proposal] = []
    for record in records:
        card_id = str(record.get("card_id") or "").strip()
        url = str(record.get("source_url") or "")
        observations = record.get("observations")
        if not isinstance(observations, list):
            observations = []
        entry = index.get(card_id)
        if entry is None:
            # Surfaced, not dropped: a stale card id means the extraction stage is
            # working from a catalogue we no longer ship.
            proposals.append(Proposal(
                card_id=card_id or "(no card_id)", field="", path="", old_value=None,
                new_value=None, unit="", source_url=url, source_quote="",
                confidence="", delta_pct=None,
                blocked_reason=REASON_UNKNOWN_CARD + (card_id or "(no card_id)"),
            ))
            continue
        try:
            proposals.extend(observations_to_proposals(entry, observations, url))
        except (TypeError, ValueError) as exc:
            print(f"error: {card_id}: {exc}", file=sys.stderr)
            return 1

    markdown = render_markdown(proposals)
    print(markdown)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(
            json.dumps(summarise(proposals), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
