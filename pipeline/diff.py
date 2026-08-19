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
import re
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

# Units added when the writer learned to reach inside the rule arrays. A reward
# rule does not store "a rate" — it stores one of THREE different numbers, and
# which one it is depends on that row's own reward_type (credit_card.dart:591/
# 602/607). Labelling them apart is not decoration: it is what stops a 5% observed
# on the issuer's page being written as 5.0 onto a row the app reads as a
# fraction, which renders 500%.
UNIT_FRACTION = "fraction_of_spend"     # reward_type cashback_pct: 0.05 == "5%"
UNIT_POINTS_PER_BLOCK = "points_per_block"   # points_per_spend: 24 per ₹150
UNIT_MULTIPLIER = "multiplier"          # multiplier: 10.0 == "10X"
UNIT_POINTS = "points"                  # a cap on a points card, in points
UNIT_MONTHS = "months"
UNIT_CATEGORY = "category"              # an exclusion's type/value, a string

# ---------------------------------------------------------------------------
# What an extracted observation maps onto — part 1, the CARD-LEVEL scalars.
#
# Observation field -> (JSON path into the card entry, unit of the stored value).
# Every path here names a key that already exists on all 383 cards. This module
# never invents a key: the app would not read it and tools/kredme.py would not
# check it.
# ---------------------------------------------------------------------------
_CARD_TARGETS: dict[str, tuple[str, str]] = {
    "base_reward_rate": ("card.base_reward_rate", UNIT_POINTS_PER_RUPEE),
    "point_value_inr": ("card.rp_value_standard", UNIT_INR_PER_POINT),
    "annual_fee_inr": ("card.annual_fee", UNIT_INR),
    "forex_markup_pct": ("card.forex_markup_pct", UNIT_PERCENT),
    "card_discontinued": ("card.is_active", UNIT_FLAG),
    # Added 2026-08-19. Both keys were already on all 383 cards and both were
    # being thrown away: 128 fee-waiver readings and 86 expiry readings across
    # the paid sweep, with nowhere to go.
    "fee_waiver_spend_inr": ("card.fee_waiver_spend", UNIT_INR),
    "points_expiry_months": ("card.points_expiry_months", UNIT_MONTHS),
}

_SETTABLE_PATHS = frozenset(path for path, _ in _CARD_TARGETS.values())


# ---------------------------------------------------------------------------
# Part 2 — the ROW targets, and why this was the hard half.
#
# 1,793 of the 2,411 observations the 17-Aug sweep paid for (74%) had no target at
# all, and the reason is structural rather than lazy: these fields do not name a
# field on the card, they name a ROW inside a list. "5% on groceries" is not a
# property of the card, it is a property of ONE reward rule, and until the writer
# could say WHICH rule, the only safe thing it could do was drop the observation.
#
# So a row target carries the array, the key inside the row, and nothing else —
# finding the row is `_match_rows`, and it is allowed to fail. Failing is the
# feature: an observation whose row cannot be identified is reported, never
# guessed at, and never appended as a second rule that would double-count.
#
# ★ WHY NOTHING HERE EVER CREATES A REWARD RULE. A reward_rules row must carry a
# rule_name, and the app keys every user's cap progress on '${cardId}|${ruleName}'
# (app_database.dart:238). A name we invent is a name that has to be right for
# ever. Worse, the name is the only INDEPENDENT evidence in the file — the one
# thing a validator can check a number against — so a rule whose name we wrote
# from the same sentence as its number can never disagree with itself, and a
# check that cannot fail is not a check. Rows are matched and updated. Only
# exclusion_rules, which have no name at all, may gain a row.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RowTarget:
    """One array, one key inside its rows."""

    block: str
    key: str
    unit: str

    @property
    def path(self) -> str:
        return f"{self.block}[].{self.key}"


_ROW_TARGETS: dict[str, RowTarget] = {
    "category_rate": RowTarget("reward_rules", "reward_rate", UNIT_FRACTION),
    "reward_unit_spend": RowTarget("reward_rules", "reward_unit_spend", UNIT_INR),
    "category_cap": RowTarget("reward_rules", "cap_amount", UNIT_POINTS),
    "excluded_category": RowTarget("exclusion_rules", "exclusion_type", UNIT_CATEGORY),
    "milestone_spend_inr": RowTarget("milestone_rules", "spend_target", UNIT_INR),
    "fuel_surcharge_waiver_pct": RowTarget("fuel_surcharge_rules", "waiver_pct",
                                           UNIT_PERCENT),
}

# The one table the rest of the module — and the report, and anyone asking "what
# can this thing place?" — reads. Card scalars and row targets in one namespace,
# because from the outside they are the same question.
_TARGETS: dict[str, tuple[str, str]] = {
    **_CARD_TARGETS,
    **{name: (t.path, t.unit) for name, t in _ROW_TARGETS.items()},
}

# STILL NOT MAPPED, and each one is a schema gap rather than an oversight:
#
#   joining_fee_inr        166 observations. No card in seed/cards.json carries a
#                          joining-fee key — all 383 share the same 21 keys and that
#                          is not one of them, and milestone_rules' "Joining Fee
#                          Waiver" rows hold the WAIVER, not the fee. Writing it
#                          would ship a number the app never reads and the publish
#                          gate never checks, which reads as data and is decoration.
#   lounge_domestic_visits 79 observations, and 53 for the spend gate. Lounge access
#   lounge_spend_gate_inr  is prose in the app's benefits text; there is no numeric
#                          field for visits or for the spend that unlocks them.
#
# Both come back as visible, blocked proposals under `unmapped_field:` so the gap
# stays countable. Adding a key for them is a schema decision plus an app change,
# not something a writer may decide on its own.
UNMAPPABLE_FIELDS = ("joining_fee_inr", "lounge_domestic_visits",
                     "lounge_spend_gate_inr")

# Fields where "the number went up" means "the cardholder earns more". Those are the
# ones the asymmetry rule in gate() applies to. A fee going up is not in here: fees
# genuinely rise, and blocking every fee increase would mean we never track any.
#
# category_cap joined the list on 2026-08-19: a cap is where a user's earning STOPS,
# so raising one raises what the card pays exactly as raising a rate does, and it is
# quoted out of the same marketing prose.
REWARD_RATE_FIELDS = frozenset({
    "base_reward_rate", "point_value_inr", "category_rate", "category_cap",
})

# reward_unit_spend is guarded too, but INVERTED: the block is a divisor, so
# "4 points per ₹150" -> "per ₹100" is the block going DOWN and the cardholder
# earning MORE. Reading it like the others would wave through every improvement
# and stop every devaluation, which is precisely backwards.
INVERTED_REWARD_FIELDS = frozenset({"reward_unit_spend"})

# The rule arrays. Named explicitly so a new array added to the schema later fails
# loudly here rather than being quietly unprotected.
RULE_ARRAYS = (
    "reward_rules",
    "exclusion_rules",
    "milestone_rules",
    "redemption_rules",
    "fuel_surcharge_rules",
)

# ---------------------------------------------------------------------------
# What may be written INSIDE a row, per array. Everything else in the file is
# read-only to this module, and both `_propose_row` and `apply_proposals` check
# against this table — once when deciding and once when writing, because the
# whole point of the second check is that it does not trust the first.
#
# rule_name and milestone_name are absent and must stay absent. See RowTarget.
# ---------------------------------------------------------------------------
WRITABLE_ROW_KEYS: dict[str, frozenset] = {
    "reward_rules": frozenset({"reward_rate", "reward_unit_spend",
                               "cap_amount", "cap_period"}),
    # exclusion_value moves only as half of a retype, and only with
    # `_retyped_from` written in the same breath — that stamp is what makes the
    # change reversible without anyone having to guess what the row used to say.
    "exclusion_rules": frozenset({"exclusion_type", "exclusion_value",
                                  "_retyped_from"}),
    "milestone_rules": frozenset({"spend_target"}),
    "fuel_surcharge_rules": frozenset({"waiver_pct"}),
}

# The provenance a row carries after this module touches it. These four names are
# what tools/checks/c8_provenance.py reads (L8), and writing them onto the ROW —
# not onto the card — is the entire point of this change. A card-level
# `_provenance` stamp is evidence about a card; it is not evidence about a rule,
# and counting it as one is what made verified coverage look four times better
# than it was.
#
# ★ `_sources` is NOT written, and that is deliberate. The seed convention is
# `_sources: ["cardinsider"]` — a bare token. L8 reads every _sources entry as a
# source candidate, finds no host in "bank", files it under SOURCE_URL_NOT_A_URL,
# and that alone caps the card at grade B for ever (c8_provenance.py:_grade,
# `soft`). A real https URL in source_url is worth more than a word.
PROVENANCE_ROW_KEYS = ("source_url", "source_quote", "source_fetched_on",
                       "confidence")

# Mirrors CreditCardData.sanePointValue (credit_card.dart:533) and tools/kredme.py:118.
# The app collapses any point value outside this range to Rs 0.25, so storing 5.0
# would not raise the card's rate — it would silently reset it and take every rule on
# the card with it. Two cards in the live catalogue already do this (5.0 and 300.0).
APP_POINT_VALUE_MAX = 1.5

# The app substitutes this for a missing point value (`?? 0.25` in fromOtaJson,
# credit_card.dart:766, and the same default at :556; tools/kredme.py:117 mirrors it).
# So a stored null is NOT an unset field waiting to be filled — it is Rs 0.25 on the
# user's screen right now, and raising it to Rs 1.50 multiplies every reward rule on
# that card by six at once.
APP_POINT_VALUE_DEFAULT = 0.25

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

# Row-targeting reasons, all added 2026-08-19. Every one of them is a case where
# the evidence is real and we still must not write it, and each is counted so the
# founder can see the shape of what is being refused rather than a silent zero.
REASON_NO_ROW = "no_matching_row"            # nothing on this card is about that
REASON_AMBIGUOUS_ROW = "ambiguous_row"       # more than one rule could be meant
REASON_CONFLICT = "conflicting_observations"  # two readings, one row, two numbers
REASON_ROW_UNIT = "row_unit_mismatch"        # points quoted at a rupee rule, or back
REASON_CAP_NO_PERIOD = "cap_without_period"  # a cap with no period is not enforced
REASON_NAME_DISAGREES = "contradicts_rule_name"
REASON_QUOTE_LACKS_NUMBER = "quote_lacks_the_number"
REASON_QUOTE_LACKS_RATE = "quote_does_not_state_the_rule_rate"
REASON_CARD_EARNS = "card_earns_in_that_category"
REASON_UNTYPEABLE = "exclusion_not_typeable"
REASON_EXCLUSION_SCOPED = "exclusion_narrower_than_a_row"

# Reasons gate() cannot overturn because they are about the shape of the input, not
# about whether the number is trustworthy.
_STRUCTURAL_REASONS = (
    REASON_UNMAPPED, REASON_UNKNOWN_CARD, REASON_MALFORMED,
    REASON_NO_ROW, REASON_AMBIGUOUS_ROW, REASON_CONFLICT, REASON_ROW_UNIT,
    REASON_CAP_NO_PERIOD, REASON_NAME_DISAGREES, REASON_CARD_EARNS,
    REASON_UNTYPEABLE, REASON_QUOTE_LACKS_RATE, REASON_EXCLUSION_SCOPED,
)


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

    # --- row targeting. All default to "this is a card-level scalar", so every
    # Proposal built before 2026-08-19 still means exactly what it meant. ---
    block: str = ""
    # Which rows. More than one only when they are the SAME rule fanned out across
    # sibling categories — 8 rows sharing one rule_name on IDFC Hello Cashback,
    # 2 on IndianOil Kotak. Writing one of a fan-out and not the others would make
    # the card contradict itself.
    rows: tuple = ()
    # Every key/value this proposal writes on those rows, primary field first.
    # A cap that arrives with its period, or a points rate that arrives with its
    # spend block, must land together or not at all: half of either is a wrong
    # number, not a partial improvement.
    writes: tuple = ()
    # A row to APPEND, as key/value pairs. Only ever an exclusion_rules row.
    new_row: tuple = ()
    # The issuer's own figures that must appear in source_quote. gate() checks
    # these; an empty tuple means there is no number to check (an exclusion) or
    # the caller is a test that predates the rule.
    evidence_numbers: tuple = ()
    # The date the cited document was FETCHED, from pipeline/state/sources.json.
    # Not today: stamping today onto a quote read two days ago is a small lie that
    # makes a stale citation look fresh, and freshness is a third of an L8 grade.
    source_fetched_on: str = ""


# ---------------------------------------------------------------------------
# Observations -> proposals
# ---------------------------------------------------------------------------
def observations_to_proposals(
    card_entry: dict,
    observations: list[dict],
    source_url: str,
    *,
    taxonomy: object = None,
    fetched_on: str = "",
) -> list[Proposal]:
    """Map verified observations onto concrete fields of one card entry.

    Returns one Proposal per observation, already passed through gate(). Nothing is
    ever dropped: an observation we cannot map, cannot read, or must not trust still
    comes back with auto_applicable=False and a reason, because a silent drop is how
    a schema gap stays invisible for a year.

    `taxonomy` decides what a spend category is (pipeline/taxonomy.py); the module's
    own default is built from seed/merchants.json plus the app's mirrored
    categories.json. `fetched_on` is the date the cited document was READ — pass the
    `fetched_at` this card's source carries in pipeline/state/sources.json, so a
    citation cannot claim to be fresher than the fetch it came from.
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
    tax = taxonomy if taxonomy is not None else _default_taxonomy()
    stamped = _iso_date(fetched_on) or _today()
    raw = [_propose(card_entry, inner, card_id, obs, url, tax, stamped)
           for obs in observations]
    # gate() first and the quote-covers-the-rate pass second, in that order: the
    # second one asks what the row will ACTUALLY end up quoting, and a proposal
    # the gate just held back writes nothing and therefore quotes nothing. Run the
    # other way round, a rate held back as an upward revision still "covered" the
    # cap that applied beside it, and the row shipped a cap-only citation — which
    # is the exact L8 error the pass exists to prevent.
    gated = [gate(p) for p in raw]
    return _mark_unsupported_quotes(_mark_conflicts(gated), card_entry)


# Reasons that mean "this was never a reading of the document at all" — no
# sentence, a marketing sentence, someone else's website, a sentence without the
# number in it, or something we could not parse. A non-reading must not get a
# vote: letting a discarded "up to 10%" sentence veto the real 5% beside it would
# hide a good number behind a bad one, which is the opposite of careful.
_NOT_A_READING = (
    REASON_UNMAPPED, REASON_UNKNOWN_CARD, REASON_MALFORMED, REASON_UNPARSEABLE,
    REASON_NO_QUOTE, REASON_WEASEL, REASON_NOT_ISSUER, REASON_QUOTE_LACKS_NUMBER,
    REASON_NO_ROW, REASON_AMBIGUOUS_ROW, REASON_ROW_UNIT, REASON_CAP_NO_PERIOD,
    REASON_NAME_DISAGREES, REASON_CARD_EARNS, REASON_UNTYPEABLE,
    REASON_EXCLUSION_SCOPED,
)


def _mark_conflicts(proposals: list[Proposal]) -> list[Proposal]:
    """Block every proposal that disagrees with another about the same target.

    One document routinely states a figure twice — IndianOil HDFC caps grocery at
    100 points in the fee table and at 1,000 points in the T&C, IDFC Hello states
    two fee-waiver spends because there are two waiver tiers. Whichever of those
    two the writer applied would depend on the order the model happened to emit
    them in, and the other would be silently lost. Neither is safe to apply
    without a person, so both are held and the disagreement is named.

    A reading held back only for CONFIDENCE, direction or size still counts as a
    reading and still conflicts: "the model felt better about this one" is not a
    reason to pick between two things the bank said.
    """
    groups: dict[tuple, list[int]] = {}
    for i, p in enumerate(proposals):
        if p.blocked_reason.startswith(_NOT_A_READING):
            continue
        groups.setdefault((p.path, p.rows, p.block), []).append(i)
    out = list(proposals)
    for _key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        values = {_hashable(out[i].new_value) for i in idxs}
        if len(values) == 1:
            continue        # the same reading twice is agreement, not conflict
        for i in idxs:
            out[i] = replace(out[i], blocked_reason=REASON_CONFLICT,
                             auto_applicable=False)
    return out


def _mark_unsupported_quotes(proposals: list[Proposal], entry: dict) -> list[Proposal]:
    """Never leave a reward rule quoting a sentence that does not state its rate.

    ★ THE ONE THIS NEARLY SHIPPED. A cap sentence usually does not restate the
    rate: "5X is capped at 600 EDGE Miles" proves the 600 and says nothing about
    the 6X the rule claims. Stamp that alone onto the row and L8 reads the quote
    against the RULE's rate, finds 5 and 600 where it wanted 6, and reports
    QUOTE_DOES_NOT_SUPPORT_RATE — an ERROR, and a hard cap at grade C. We would
    have paid to attach evidence that fails our own audit.

    So the test is done per ROW, over every sentence this run would put on it
    together: if none of them states what the rule pays, none of them is written.
    That case is not a quoting problem, it is our number and the issuer's page
    disagreeing, and disagreements go to a person.
    """
    rows = entry.get("reward_rules")
    if not isinstance(rows, list):
        return proposals
    per_row: dict[int, list[int]] = {}
    for i, p in enumerate(proposals):
        if p.block != "reward_rules" or not p.auto_applicable:
            continue
        for r in p.rows:
            per_row.setdefault(r, []).append(i)

    unsupported: set = set()
    for r, idxs in per_row.items():
        if not (0 <= r < len(rows)) or not isinstance(rows[r], dict):
            continue
        after = dict(rows[r])
        for i in idxs:
            for key, value in proposals[i].writes:
                after[key] = value
        claims = _claimed_numbers(after, "", None, ())
        if not claims:
            continue      # a rule claiming no rate cannot be contradicted
        said = _numbers_in(" ".join(str(proposals[i].source_quote or "")
                                    for i in idxs))
        if not any(_close(c, x) for c in claims for x in said):
            unsupported.update(idxs)

    out = list(proposals)
    for i in unsupported:
        out[i] = replace(out[i], blocked_reason=REASON_QUOTE_LACKS_RATE,
                         auto_applicable=False)
    return out


def _hashable(value: object) -> object:
    """A value usable as a set member; unhashable ones compare by their JSON text."""
    try:
        hash(value)
        return value
    except TypeError:
        return json.dumps(value, sort_keys=True, default=str)


def _propose(entry: dict, inner: dict, card_id: str, obs: object, url: str,
             tax: object, fetched_on: str) -> Proposal:
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

    if name in _ROW_TARGETS:
        return _propose_row(entry, inner, card_id, name, obs, url, tax, fetched_on)

    target = _CARD_TARGETS.get(name)
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
        evidence_numbers=_evidence_numbers(obs, quote),
        source_fetched_on=fetched_on,
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

    if name == "fee_waiver_spend_inr":
        v = _to_number(raw)
        # A waiver spend of zero is not "the fee is waived at ₹0" — it is an
        # unparsed value, and the app would render the card as fee-free for ever.
        return None if v is None or v <= 0 else _round(v, 2)

    if name == "points_expiry_months":
        v = _to_number(raw)
        if v is None or v <= 0 or v != int(v):
            return None
        return int(v)

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
# Quotes as evidence
#
# "A quote that does not contain the number it is cited for is worse than no
# quote" — it manufactures confidence, reads as verified in every report, and is
# about something else. So every proposal carrying a number must be able to point
# at that number INSIDE the sentence, and the ones that cannot are counted rather
# than quietly dropped.
#
# The tolerance and the number grammar mirror tools/checks/c8_provenance.py
# (_NUM_TOKEN, _close) on purpose: the writer must refuse exactly what L8 would
# later call an unsupported quote, or we write rows that fail the audit we are
# writing them for. It is duplicated rather than imported because the dependency
# runs the other way — c8 imports pipeline, never the reverse.
# ---------------------------------------------------------------------------
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers_in(text: object) -> set:
    out: set = set()
    if not isinstance(text, str):
        return out
    for m in _NUM_TOKEN.finditer(text):
        try:
            out.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def _close(a: float, b: float) -> bool:
    """Equal to within half a percent — the same tolerance L8 grades on."""
    try:
        return abs(a - b) <= max(1e-9, abs(a) * 0.005)
    except TypeError:
        return False


def _quote_states(quote: object, numbers) -> bool:
    """True when every required figure appears in the sentence."""
    found = _numbers_in(quote)
    return all(any(_close(n, x) for x in found) for n in numbers)


def _evidence_numbers(obs: dict, quote: str) -> tuple:
    """The issuer's OWN figures that the quote has to contain.

    The issuer's token, never our stored value: the page says "4 Reward Points per
    ₹150" and we store 0.0266667, and demanding 0.0266667 in the sentence would
    refuse every correct citation there is. The block size is required too — "3
    points per ₹150" and "3 points per ₹100" pay 50% differently and read
    identically in a report.
    """
    if not isinstance(obs, dict):
        return ()
    raw = obs.get("value")
    out = []
    value = _to_number(raw)
    if value is not None:
        # "Nil" / "Free" / "Zero" is a stated figure with no digit in it. The
        # sentence has to contain the WORD instead, and if it does not, the zero
        # came from somewhere other than this quote.
        word = raw.strip().lower() if isinstance(raw, str) else ""
        if word in _ZERO_WORDS:
            if word not in " ".join(str(quote).lower().split()):
                out.append(0.0)
        else:
            out.append(value)
    block = _to_number(obs.get("per_spend_inr"))
    if block is not None and block > 0:
        out.append(block)
    return tuple(out)


def _iso_date(value: object) -> str:
    """'2026-08-17T16:04:49Z' or '2026-08-17' -> '2026-08-17'; anything else ''."""
    if not isinstance(value, str):
        return ""
    head = value.strip()[:10]
    try:
        _dt.date.fromisoformat(head)
    except ValueError:
        return ""
    return head


# ---------------------------------------------------------------------------
# Rows — finding the ONE rule an observation is about
#
# An observation says `category: "groceries_and_bill_payments"`. The card holds a
# list of rules, some tagged with a category_id, some with only a merchant_ref,
# some with nothing but a prose category_ref. Deciding which row that sentence is
# about is the whole difficulty, and the rule for handling doubt is simple and
# absolute: WHEN IN DOUBT, PLACE NOTHING AND SAY SO.
#
# The ladder, in order, and it stops at the first rung that hits:
#
#   1. CATEGORY IDENTITY. The row's own category_id, or the app category of its
#      merchant_ref (seed/merchants.json: 'indian_oil' -> 'fuel'), against the
#      slugs the observation's category resolves to.
#   2. THE ISSUER'S PHRASE against the rule's own prose (category_ref, merchant_ref,
#      channel). One token set must contain the other — "groceries" inside
#      "groceries and bill payments" is a match, an overlap of one word out of six
#      is not.
#
# Then the result is collapsed:
#
#   * one row                          -> that is the rule.
#   * several rows, ONE rule_name, one current value -> that is ONE rule fanned out
#     across sibling categories (8 rows on IDFC Hello Cashback, 2 on IndianOil
#     Kotak share a single name because they share a single cap bucket). Writing
#     one and not the others would make the card disagree with itself.
#   * several rows, and the new value already equals what every one of them stores
#     -> a CONFIRMATION. Nothing numeric moves, only the citation lands, so which
#     row was "really" meant does not matter.
#   * anything else                    -> ambiguous. Blocked, counted, named.
#
# An empty `category` on a row-targeted field cannot identify anything and is
# blocked as `no_matching_row` — the same answer as a category naming something
# this card has no rule for.
# ---------------------------------------------------------------------------
_NAME_KEY = {"reward_rules": "rule_name", "milestone_rules": "milestone_name"}


def _default_taxonomy():
    """Imported here rather than at module scope only to keep the import graph
    one-directional and the module cheap to import in CI."""
    from pipeline.taxonomy import default_taxonomy

    return default_taxonomy()


def _match_tokens(text: object) -> set:
    from pipeline.taxonomy import match_tokens

    return set(match_tokens(text))


def _rows_of(entry: dict, block: str) -> list:
    rows = entry.get(block)
    if not isinstance(rows, list):
        return []
    return [(i, r) for i, r in enumerate(rows) if isinstance(r, dict)]


def _row_value(entry: dict, block: str, index: int, key: str) -> object:
    rows = entry.get(block)
    if not isinstance(rows, list) or index >= len(rows):
        return None
    row = rows[index]
    return row.get(key) if isinstance(row, dict) else None


def _same_value(a: object, b: object) -> bool:
    """Equality that treats 0.05 and 0.0500001 as the same stored number."""
    na, nb = _fnum(a), _fnum(b)
    if na is not None and nb is not None:
        return _close(na, nb)
    return a == b


def _match_rows(entry: dict, block: str, obs: dict, tax) -> tuple:
    """(row indexes, blocked reason). An empty reason means the indexes are usable."""
    if block == "fuel_surcharge_rules":
        # These rows have no identity at all — no name, no category, four numbers.
        # One row is unambiguous; two rows are two different surcharge regimes and
        # a person has to say which the sentence is about.
        rows = _rows_of(entry, block)
        if len(rows) == 1:
            return (rows[0][0],), ""
        return (), (REASON_NO_ROW if not rows else REASON_AMBIGUOUS_ROW)

    text = obs.get("category")
    slugs = set(tax.resolve(text)) if text else set()

    if block == "milestone_rules":
        return _match_by_prose(entry, block, text,
                               ("milestone_name", "bonus_description", "bonus_type"))

    hits = []
    for i, row in _rows_of(entry, block):
        row_slugs = set()
        cid = row.get("category_id")
        if isinstance(cid, str) and cid:
            row_slugs.add(cid)
        merchant_cat = tax.category_of_merchant(row.get("merchant_ref"))
        if merchant_cat:
            row_slugs.add(merchant_cat)
        if slugs and row_slugs & slugs:
            hits.append(i)
    if hits:
        return tuple(hits), ""
    return _match_by_prose(entry, block, text,
                           ("category_ref", "merchant_ref", "channel"))


def _match_by_prose(entry: dict, block: str, text: object, keys: tuple) -> tuple:
    """Rung 2: the issuer's phrase against the row's own words."""
    want = _match_tokens(text)
    if not want:
        return (), REASON_NO_ROW
    hits = []
    for i, row in _rows_of(entry, block):
        have = _match_tokens(" ".join(str(row.get(k) or "") for k in keys))
        if not have:
            continue
        if want <= have or have <= want:
            hits.append(i)
    return tuple(hits), ("" if hits else REASON_NO_ROW)


def _collapse(entry: dict, block: str, key: str, rows: tuple, value: object) -> tuple:
    """(rows to write, blocked reason) — see the ladder above."""
    if len(rows) <= 1:
        return rows, ""
    name_key = _NAME_KEY.get(block, "")
    names = {str(_row_value(entry, block, i, name_key)) for i in rows} if name_key \
        else {""}
    values = [_row_value(entry, block, i, key) for i in rows]
    one_rule = len(names) == 1 and all(_same_value(values[0], v) for v in values)
    confirmation = all(_same_value(value, v) for v in values)
    if one_rule or confirmation:
        return rows, ""
    return rows, REASON_AMBIGUOUS_ROW


# ---------------------------------------------------------------------------
# Turning an observation into a value in the ROW's own arithmetic
#
# ★ THE UNIT TRAP, again, and it is worse here than at card level. A reward rule
# stores its rate in one of three different units depending on its own
# reward_type: a fraction for cashback_pct (0.05 IS "5%"), points-and-a-block for
# points_per_spend, a bare number for a multiplier. The issuer states one of them.
# If those two do not agree we do NOT convert between them — converting needs a
# point value the document almost never states, and getting it wrong is how a card
# ends up rendering 0.02%. We refuse and count it.
# ---------------------------------------------------------------------------
def _convert_row(name: str, obs: dict, entry: dict, inner: dict, rows: tuple) -> tuple:
    """(value, extra writes, unit, blocked reason)."""
    block = _ROW_TARGETS[name].block
    # Every row in a matched group shares the reward_type and the current value
    # that _collapse checked, so the first one answers for all of them.
    row = dict(_rows_of(entry, block)).get(rows[0], {}) if rows else {}
    raw = obs.get("value")
    unit_in = str(obs.get("unit") or "").strip().lower()

    if name == "category_rate":
        rtype = str(row.get("reward_type") or "").strip()
        if rtype == "cashback_pct":
            if unit_in not in _PERCENT_UNITS:
                return None, (), UNIT_FRACTION, REASON_ROW_UNIT
            pct = _to_number(raw)
            if pct is None or pct < 0:
                return None, (), UNIT_FRACTION, ""
            return _round(pct / 100.0, 6), (), UNIT_FRACTION, ""
        if rtype == "points_per_spend":
            if unit_in not in _POINT_UNITS:
                return None, (), UNIT_POINTS_PER_BLOCK, REASON_ROW_UNIT
            points = _to_number(raw)
            block_size = _to_number(obs.get("per_spend_inr"))
            if points is None or points < 0 or block_size is None or block_size <= 0:
                # "24 points" with no block is half a rate, and half a rate is not
                # a smaller improvement — it is a wrong one.
                return None, (), UNIT_POINTS_PER_BLOCK, ""
            return (_round(points, 4), (("reward_unit_spend", _round(block_size, 2)),),
                    UNIT_POINTS_PER_BLOCK, "")
        if rtype == "multiplier":
            if unit_in != "multiplier":
                return None, (), UNIT_MULTIPLIER, REASON_ROW_UNIT
            mult = _to_number(raw)
            if mult is None or mult < 0:
                return None, (), UNIT_MULTIPLIER, ""
            return _round(mult, 4), (), UNIT_MULTIPLIER, ""
        return None, (), UNIT_FRACTION, REASON_ROW_UNIT

    if name == "reward_unit_spend":
        if str(row.get("reward_type") or "") != "points_per_spend":
            return None, (), UNIT_INR, REASON_ROW_UNIT
        v = _to_number(raw)
        if v is None or v <= 0:
            return None, (), UNIT_INR, ""
        return _round(v, 2), (), UNIT_INR, ""

    if name == "category_cap":
        return _convert_cap(obs, inner, row, raw, unit_in)

    if name == "milestone_spend_inr":
        if unit_in not in ("inr", ""):
            return None, (), UNIT_INR, REASON_ROW_UNIT
        v = _to_number(raw)
        if v is None or v <= 0:
            return None, (), UNIT_INR, ""
        return _round(v, 2), (), UNIT_INR, ""

    if name == "fuel_surcharge_waiver_pct":
        if unit_in not in _PERCENT_UNITS:
            return None, (), UNIT_PERCENT, REASON_ROW_UNIT
        v = _to_number(raw)
        if v is None or v < 0 or v > 100:
            return None, (), UNIT_PERCENT, ""
        return _round(v, 4), (), UNIT_PERCENT, ""

    return None, (), "", REASON_UNMAPPED + name


def _convert_cap(obs: dict, inner: dict, row: dict, raw: object, unit_in: str) -> tuple:
    """A cap, in the ISSUER'S unit, with a period the engine actually enforces.

    ★ CAPS GO IN THE ISSUER'S UNIT, NOT RUPEES (settled 2026-08-11). A points card
    whose cap is stored in rupees corrupts every time somebody corrects the point
    value — that is the 5x IndianOil regression. There is no cap_unit key on these
    rows, so the unit is implied by the card's reward_currency, and an observation
    quoting the other unit is refused rather than converted.

    ★ A CAP WITH NO PERIOD IS NOT A CAP. _checkCap returns null unless both are
    set, so the rule pays its accelerated rate for ever — 19 rules in this
    catalogue already do. The period comes from the issuer's own sentence or the
    cap does not land.
    """
    rupee_card = str(inner.get("reward_currency") or "") == "cashback_inr"
    if unit_in in _POINT_UNITS and rupee_card:
        return None, (), UNIT_INR, REASON_ROW_UNIT
    if unit_in == "inr" and not rupee_card:
        return None, (), UNIT_POINTS, REASON_ROW_UNIT
    unit = UNIT_INR if rupee_card else UNIT_POINTS
    if unit_in not in _POINT_UNITS and unit_in != "inr":
        return None, (), unit, REASON_ROW_UNIT

    # ★ AND THE ROW HAS TO AGREE WITH THE CARD. The app decides what it subtracts
    # from cap_amount off the ROW's reward_type, not off the card's currency
    # (RewardRule.usedAgainstCap): points for points_per_spend and multiplier,
    # RUPEES for cashback_pct and anything unrecognised. So a points cap written
    # onto a cashback_pct row of a points card is counted in rupees by the engine
    # — L4.CAP_IN_RUPEES, an ERROR, and on IndianOil HDFC (₹0.20 a point) the cap
    # would bind five times too early. 29 of 185 capped rules in this file are
    # already in that state; the writer must not add the 30th. Placing nothing
    # leaves the row exactly as it is and names the gap: it needs cap_unit, or
    # the row needs the right reward_type, and both are decisions above this
    # module's pay grade.
    if _engine_cap_unit(row) != ("inr" if unit_in == "inr" else "points"):
        return None, (), unit, REASON_ROW_UNIT

    value = _to_number(raw)
    if value is None or value <= 0:
        return None, (), unit, ""

    period = row.get("cap_period")
    if isinstance(period, str) and period.strip():
        return value, (), unit, ""
    derived = _cap_period_from_quote(obs.get("source_quote"))
    if not derived:
        return None, (), unit, REASON_CAP_NO_PERIOD
    return value, (("cap_period", derived),), unit, ""


def _engine_cap_unit(row: dict) -> str:
    """What the APP subtracts from cap_amount for this row.

    Mirrors RewardRule.usedAgainstCap and tools/checks/c4_numeric._engine_cap_unit
    exactly, including the part that surprises everyone: a cashback_pct rule's cap
    counts RUPEES even on a card that pays in points.
    """
    if not isinstance(row, dict):
        return "inr"
    if row.get("cap_kind") == "spend":
        return "inr"
    if row.get("reward_type") in ("points_per_spend", "multiplier"):
        return "points"
    return "inr"


# The issuer's own words for a period, longest and most specific first. 'cycle'
# is tested before 'month' because "statement cycle" and "calendar month" are
# genuinely different windows and the app treats them differently
# (monthKeysForPeriod double-counts two months as one cycle on 122 rules today).
_CAP_PERIODS = (
    ("statement cycle", "cycle"), ("billing cycle", "cycle"), ("per cycle", "cycle"),
    ("statement month", "cycle"),
    ("per month", "month"), ("a month", "month"), ("every month", "month"),
    ("monthly", "month"), ("calendar month", "month"), ("per customer per month", "month"),
    ("per quarter", "quarter"), ("quarterly", "quarter"),
    ("per year", "year"), ("a year", "year"), ("annually", "year"),
    ("per annum", "year"), ("anniversary year", "year"),
    ("per transaction", "transaction"), ("per txn", "transaction"),
    ("per day", "day"), ("daily", "day"),
)


def _cap_period_from_quote(quote: object) -> str:
    """The cap window, read out of the issuer's sentence, or ''.

    Only ever from the words in front of us. There is no default: a cap whose
    period we had to assume is a cap we invented.
    """
    if not isinstance(quote, str):
        return ""
    flat = " ".join(quote.lower().split())
    for phrase, period in _CAP_PERIODS:
        if phrase in flat:
            return period
    return ""


# ---------------------------------------------------------------------------
# The rule name is evidence, so it gets a vote
#
# NEVER EDIT A rule_name. The app keys users' cap progress on it
# (app_database.dart:238), so a rename wipes accrued progress mid-cycle — and
# "fixing" the text so it agrees with a new number is circular, because the name
# is the only independent record of what the rule was supposed to be.
#
# Which leaves one honest option when a new number and an existing name disagree:
# do not write the number either. Hand both to a person. That keeps rule 3 of the
# definition of perfect — the English and the arithmetic agree — without ever
# touching the English.
# ---------------------------------------------------------------------------
def _claimed_numbers(row: dict, key: str, value: object, extra: tuple) -> set:
    """Every way the row's rate could legitimately be written, after the write.

    Mirrors credit_card.dart:655-678 and L8's _claimed_rate_numbers: 0.05 is also
    "5%", and 24 points per ₹150 is also "16%".
    """
    after = dict(row)
    if key:
        after[key] = value
    for k, v in extra:
        after[k] = v
    rate = _fnum(after.get("reward_rate"))
    unit = _fnum(after.get("reward_unit_spend"))
    rtype = str(after.get("reward_type") or "points_per_spend")
    out = set()
    if rate is None:
        return out
    out.add(rate)
    if rtype == "cashback_pct":
        out.add(rate * 100.0)
    elif rtype == "points_per_spend" and unit:
        out.add(rate / unit * 100.0)
    return {x for x in out if x}


def _name_disagrees(entry: dict, block: str, rows: tuple, key: str,
                    value: object, extra: tuple) -> bool:
    """True when writing this number would make a rule contradict its own name."""
    if block != "reward_rules":
        return False
    for i, row in _rows_of(entry, block):
        if i not in rows:
            continue
        name = row.get("rule_name")
        in_name = _numbers_in(name)
        if not in_name:
            continue        # a name with no number cannot contradict one
        if key in ("reward_rate", "reward_unit_spend"):
            claims = _claimed_numbers(row, key, value, extra)
            if claims and not any(_close(c, x) for c in claims for x in in_name):
                return True
        if key == "cap_amount":
            # Only when the name SPELLS OUT the cap it is replacing. "capped at
            # 1200 Reward Points" in the name and 800 in the file is a
            # contradiction; a name that never mentions a cap is not.
            old = _fnum(row.get("cap_amount"))
            new = _fnum(value)
            if old is not None and new is not None and not _close(old, new) \
                    and any(_close(old, x) for x in in_name):
                return True
    return False


# ---------------------------------------------------------------------------
# Proposing a row write
# ---------------------------------------------------------------------------
def _blocked_row(card_id: str, name: str, target: RowTarget, obs: dict, url: str,
                 quote: str, confidence: str, reason: str,
                 fetched_on: str) -> Proposal:
    return Proposal(
        card_id=card_id, field=name, path=target.path, old_value=None,
        new_value=obs.get("value"), unit=target.unit, source_url=url,
        source_quote=quote, confidence=confidence, delta_pct=None,
        blocked_reason=reason, block=target.block, source_fetched_on=fetched_on,
    )


def _propose_row(entry: dict, inner: dict, card_id: str, name: str, obs: dict,
                 url: str, tax, fetched_on: str) -> Proposal:
    """Build the ungated Proposal for an observation that targets a row."""
    target = _ROW_TARGETS[name]
    quote = obs.get("source_quote") if isinstance(obs.get("source_quote"), str) else ""
    confidence = str(obs.get("confidence") or "").strip().lower()

    if name == "excluded_category":
        return _propose_exclusion(entry, card_id, obs, url, quote, confidence,
                                  tax, fetched_on)

    rows, reason = _match_rows(entry, target.block, obs, tax)
    if reason:
        return _blocked_row(card_id, name, target, obs, url, quote, confidence,
                            reason, fetched_on)

    value, extra, unit, reason = _convert_row(name, obs, entry, inner, rows)
    if reason:
        return _blocked_row(card_id, name, target, obs, url, quote, confidence,
                            reason, fetched_on)

    rows, reason = _collapse(entry, target.block, target.key, rows, value)
    if reason:
        return _blocked_row(card_id, name, target, obs, url, quote, confidence,
                            reason, fetched_on)

    if _name_disagrees(entry, target.block, rows, target.key, value, extra):
        return _blocked_row(card_id, name, target, obs, url, quote, confidence,
                            REASON_NAME_DISAGREES, fetched_on)

    writes = ((target.key, value),) + tuple(extra)
    allowed = WRITABLE_ROW_KEYS.get(target.block, frozenset())
    for k, _v in writes:
        if k not in allowed:      # unreachable by construction; a tripwire, not a check
            return _blocked_row(card_id, name, target, obs, url, quote, confidence,
                                REASON_MALFORMED, fetched_on)

    old = _row_value(entry, target.block, rows[0], target.key)
    return Proposal(
        card_id=card_id, field=name,
        path=f"{target.block}[{','.join(str(i) for i in rows)}].{target.key}",
        old_value=old, new_value=value, unit=unit, source_url=url,
        source_quote=quote, confidence=confidence,
        delta_pct=_delta_pct(old, value, unit), block=target.block, rows=rows,
        writes=writes, evidence_numbers=_evidence_numbers(obs, quote),
        source_fetched_on=fetched_on,
    )


# ---------------------------------------------------------------------------
# Exclusions — the only rows this module may create, and the only ones with a
# guardrail in front of them
#
# ★ recommendation_engine.dart:308-309 runs exclusions BEFORE rule matching. So
# switching one on does not shave a category off a card, it ZEROES the card for
# every merchant in that family, bonus rules included. Two thirds of the
# exclusions in this file are inert today (typed 'other', which the engine does
# not read), and a sweep that blindly typed them 'category' would have zeroed
# BPCL Octane's own fuel bonus.
#
# Hence the rule, with no exception: an exclusion may only be activated on a card
# that does NOT earn in that category family. Family means the slug, everything
# above it and everything below it, and "earns" is read from the card's own rows
# — category_id, merchant_ref resolved through seed/merchants.json, and the prose
# category_ref. If the card earns there, the issuer's sentence and our rules
# disagree, and a disagreement is for a person to settle.
# ---------------------------------------------------------------------------
_MCC_CODE = re.compile(r"^\d{4}$")


def _norm_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _card_earns_in(entry: dict, family: frozenset, tax) -> bool:
    """Does any reward rule on this card pay out inside that category family?"""
    if not family:
        return False
    for _i, row in _rows_of(entry, "reward_rules"):
        cid = row.get("category_id")
        if isinstance(cid, str) and cid in family:
            return True
        if tax.category_of_merchant(row.get("merchant_ref")) in family:
            return True
        ref = row.get("category_ref")
        if isinstance(ref, str) and ref and set(tax.resolve(ref)) & family:
            return True
    return False


# --------------------------------------------------------------------------
# Is the issuer's exclusion narrower than an exclusion row can say?
# --------------------------------------------------------------------------
# An exclusion row carries a type and a value and nothing else, and the engine
# applies it to EVERY transaction in that category whatever rail it arrived on —
# before any reward rule is even matched (recommendation_engine.dart:308-309).
# So a row may only carry an exclusion the issuer states unconditionally.
#
# Both Tata Neu pages say:
#   "Education payments made through third-party apps like (but not limited to)
#    CRED, Cheq, MobiKwik, and others will NOT earn NeuCoins."
# Stored as `category: education` that zeroes a fee paid DIRECTLY to a school —
# which both cards do pay on, at 1.5% and 1% — and it zeroes it before the 5%
# Tata-brand rules run. The issuer excluded a payment route, not a category.
#
# The extractor was not wrong. It recorded the scope in its own value,
# "third-party-education-payments", and only its `category` field said
# "education". Taking the wider of the two is the mistake, and without this it
# was about to be made on two of the ten cards — the exact "wrongly-typed
# exclusion zeroes the card" scar, arriving through the one door left open.
_NARROWING_WORDS = frozenset({
    "third", "thirdparty", "party", "parties",
    "app", "apps", "application", "applications",
    "aggregator", "aggregators", "platform", "platforms",
    "gateway", "gateways", "intermediary", "intermediaries",
})

_WORD = re.compile(r"[a-z0-9]+")

# Clause, not sentence. An issuer's exclusion list routinely carries one item that
# IS route-scoped ("Third party integrated purchase like Flipkart Health") beside
# six that are not; refusing the whole sentence would throw away six good
# exclusions to catch one bad one.
_CLAUSE_SPLIT = re.compile(r"[,;:.()\[\]•–—/\n]|\band\b|\bor\b")


def _words(text: object) -> set:
    if not isinstance(text, (str, int, float)) or isinstance(text, bool):
        return set()
    return set(_WORD.findall(str(text).lower()))


def _exclusion_is_route_scoped(obs: dict, quote: object, slugs: tuple) -> bool:
    """True when the issuer tied this exclusion to a payment route we cannot store."""
    cat_words = _words(obs.get("category"))
    slug_words = {w for s in slugs for w in _WORD.findall(str(s).lower())}
    known = cat_words | slug_words
    # 1. The observation's own value says more than its category does.
    if (_words(obs.get("value")) - known) & _NARROWING_WORDS:
        return True
    # 2. The clause of the sentence that names this category also names a route.
    if not known:
        return False
    for clause in _CLAUSE_SPLIT.split(str(quote or "")):
        cw = _words(clause)
        if (cw & known) and (cw & _NARROWING_WORDS):
            return True
    return False


# Does the bank's sentence also take this spend out of the fee-waiver total?
# Axis says of Atlas: "Excluded spend categories for reward earns / spend based
# fee waiver: ... Rent, Insurance, Wallet, Government Institutions, Utilities,
# Fuel." A new row hard-coded to 0 states the opposite of that sentence — that
# fuel spend still counts towards the waiver — and inventing nothing means not
# inventing this either. Read over the whole sentence, not the clause: the bank
# puts "fee waiver" in the heading and the categories in the list below it.
#
# The field is inert in the app today (credit_card.dart:243 parses it and nothing
# reads it), so this corrects the record rather than a screen.
_FEE_WAIVER_PHRASE = re.compile(
    r"fee\s*waiver|waiver\s+of\s+(?:the\s+)?(?:annual|renewal|joining)\s+fee"
    r"|spend[s]?\s+counted|milestone\s+spend", re.IGNORECASE)


def _excludes_from_threshold(quote: object) -> int:
    return 1 if _FEE_WAIVER_PHRASE.search(str(quote or "")) else 0


def _propose_exclusion(entry: dict, card_id: str, obs: dict, url: str, quote: str,
                       confidence: str, tax, fetched_on: str) -> Proposal:
    target = _ROW_TARGETS["excluded_category"]
    raw = obs.get("value")
    unit_in = str(obs.get("unit") or "").strip().lower()

    # 1. Can this even be typed in a way the engine reads? The engine looks at
    #    'mcc' and 'category' and nothing else, so anything that resolves to
    #    neither would land as another inert row pretending to be protection.
    #    EMI, ATM withdrawals and "UPI via other apps" are real exclusions the
    #    issuer publishes and the app has no vocabulary for. They are refused and
    #    counted, not approximated.
    text = str(raw) if isinstance(raw, str) else ""
    slugs = tax.resolve(obs.get("category"), text)
    if unit_in == "mcc" and _MCC_CODE.match(str(raw or "").strip()):
        etype, evalue = "mcc", str(raw).strip()
        family = frozenset()
    elif len(slugs) == 1:
        etype, evalue = "category", slugs[0]
        family = tax.family(slugs[0])
    else:
        return _blocked_row(card_id, "excluded_category", target, obs, url, quote,
                            confidence, REASON_UNTYPEABLE, fetched_on)

    # 1b. Did the issuer exclude the CATEGORY, or only one route into it? A row
    #     cannot say "but only when paid through CRED", so storing a route-scoped
    #     exclusion silently widens it to every transaction in the category.
    if _exclusion_is_route_scoped(obs, quote, slugs):
        return _blocked_row(card_id, "excluded_category", target, obs, url, quote,
                            confidence, REASON_EXCLUSION_SCOPED, fetched_on)

    # 2. THE GUARDRAIL.
    if _card_earns_in(entry, family, tax):
        return _blocked_row(card_id, "excluded_category", target, obs, url, quote,
                            confidence, REASON_CARD_EARNS, fetched_on)

    # 3. Does a row for this already exist? Updating in place is the only way to
    #    stay idempotent — an exclusion appended twice excludes nothing twice and
    #    makes the file look busier than it is.
    live = []
    retypeable = []
    for i, row in _rows_of(entry, "exclusion_rules"):
        rtype = str(row.get("exclusion_type") or "").strip().lower()
        rvalue = row.get("exclusion_value")
        if rtype == etype and _norm_key(rvalue) == _norm_key(evalue):
            live.append(i)
        elif rtype not in ("mcc", "category") and set(tax.resolve(rvalue)) == set(slugs) \
                and slugs:
            retypeable.append(i)

    if live:
        # Already switched on and already correct. The only thing missing is the
        # evidence, so that is the only thing written.
        return Proposal(
            card_id=card_id, field="excluded_category",
            path=f"exclusion_rules[{live[0]}].{target.key}", old_value=etype,
            new_value=etype, unit=UNIT_CATEGORY, source_url=url, source_quote=quote,
            confidence=confidence, delta_pct=None, block="exclusion_rules",
            rows=(live[0],), writes=(), source_fetched_on=fetched_on,
        )

    if len(retypeable) > 1:
        return _blocked_row(card_id, "excluded_category", target, obs, url, quote,
                            confidence, REASON_AMBIGUOUS_ROW, fetched_on)

    if retypeable:
        i = retypeable[0]
        row = entry["exclusion_rules"][i]
        stamp = "%s:%s" % (row.get("exclusion_type"), row.get("exclusion_value"))
        return Proposal(
            card_id=card_id, field="excluded_category",
            path=f"exclusion_rules[{i}].{target.key}",
            old_value=row.get("exclusion_type"), new_value=etype, unit=UNIT_CATEGORY,
            source_url=url, source_quote=quote, confidence=confidence, delta_pct=None,
            block="exclusion_rules", rows=(i,),
            writes=(("exclusion_type", etype), ("exclusion_value", evalue),
                    ("_retyped_from", stamp)),
            source_fetched_on=fetched_on,
        )

    return Proposal(
        card_id=card_id, field="excluded_category",
        path=f"exclusion_rules[+{evalue}].{target.key}", old_value=None,
        new_value=etype,
        unit=UNIT_CATEGORY, source_url=url, source_quote=quote, confidence=confidence,
        delta_pct=None, block="exclusion_rules",
        new_row=(("exclusion_type", etype), ("exclusion_value", evalue),
                 ("also_excludes_from_threshold",
                  _excludes_from_threshold(quote))),
        source_fetched_on=fetched_on,
    )


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
    # The sentence has to contain the figure it is cited for. A quote that does
    # not is worse than no quote at all: it reads as verified everywhere, and it
    # is about something else. This is also exactly what L8 would later report as
    # QUOTE_DOES_NOT_SUPPORT_RATE — an ERROR — so writing one would mean paying to
    # fail our own audit.
    if p.evidence_numbers and not _quote_states(quote, p.evidence_numbers):
        return REASON_QUOTE_LACKS_NUMBER

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
    if p.unit == UNIT_CATEGORY:
        # An exclusion's type is a string, and only two of them mean anything:
        # the engine reads 'mcc' and 'category' and ignores everything else.
        return v in ("mcc", "category")
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
        # A fuel-surcharge waiver is a percentage of the SURCHARGE, not of spend,
        # and it is capped at 100 in _convert_row. Passing it through the reward
        # ceiling would be comparing two different things.
        return None if p.field == "fuel_surcharge_waiver_pct" else v
    if p.unit == UNIT_POINTS_PER_RUPEE:
        return v * 100.0
    if p.unit == UNIT_FRACTION:
        return v * 100.0
    if p.unit == UNIT_POINTS_PER_BLOCK:
        # points / block, as a percentage. An upper bound again: one point is
        # worth at most about a rupee, so this is the most the app could render.
        block = None
        for key, value in p.writes:
            if key == "reward_unit_spend":
                block = _fnum(value)
        if block:
            return v / block * 100.0
        return None
    return None


def _is_upward_reward_revision(p: Proposal) -> bool:
    """True when this raises what a cardholder earns.

    An old value of None is usually a gap being filled, not a revision, and is judged
    on confidence and quote like anything else. An old value of 0 IS a number, so
    0 -> anything counts as upward.

    The point value is the exception, and it is the dangerous one. 71 of 380 live cards
    store `rp_value_standard: null`, and the app renders those at Rs 0.25 — so null is
    not a gap, it is a value the user already sees. Treating it as a gap let a proposal
    of Rs 1.50 auto-apply with no ceiling check, no delta check and no upward check,
    because every one of those comparisons short-circuits on a None old value. Since
    the app multiplies EVERY reward rule on the card by this single field, one such
    write moved a whole card at once and produced a 60% rendered rate — past the 40%
    ceiling that tools/kredme.py calls unwaivable and that config.py promises this
    module mirrors.
    """
    if p.field in INVERTED_REWARD_FIELDS:
        # The spend block is a divisor. "4 points per ₹150" becoming "per ₹100"
        # is the number going DOWN and the cardholder earning 50% more, so the
        # comparison is the other way round for this one field.
        o, n = _fnum(p.old_value), _fnum(p.new_value)
        return o is not None and n is not None and n < o
    if p.field not in REWARD_RATE_FIELDS:
        return False
    o, n = _fnum(p.old_value), _fnum(p.new_value)
    if o is None and p.unit == UNIT_INR_PER_POINT:
        o = APP_POINT_VALUE_DEFAULT
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

    Field-level and additive. A card changes in exactly three ways and no others:
    the single card key a scalar proposal names; the whitelisted keys inside the
    rows a row proposal names, plus that row's provenance; and, for exclusions
    only, one appended row. No card is ever replaced, no key is ever removed, no
    row is ever removed or reordered, and no rule_name or milestone_name is ever
    touched by anything.

    The proof is at the bottom: every key that actually moved is compared against
    the set this function INTENDED to move, and a single unintended byte raises.
    """
    if not isinstance(cards, list):
        raise TypeError(f"cards must be a list, got {type(cards).__name__}")
    if not isinstance(proposals, list):
        raise TypeError(f"proposals must be a list, got {type(proposals).__name__}")

    before_names = _rule_names(cards)
    before_rows = _row_snapshot(cards)
    before_keys = _card_key_shape(cards)

    new_cards = copy.deepcopy(cards)
    index: dict[str, dict] = {}
    position: dict[str, int] = {}
    for n, entry in enumerate(new_cards):
        if not isinstance(entry, dict) or not isinstance(entry.get("card"), dict):
            continue
        cid = entry["card"].get("id")
        if isinstance(cid, str) and cid and cid not in index:
            index[cid] = entry
            position[cid] = n

    stamped_on = _today()
    applied: list[Proposal] = []
    intended: set = set()
    # Exclusions created in THIS run, so the same reading twice appends one row
    # rather than two. _propose_exclusion reads the entry as it was before the
    # run, so it cannot see a row this loop just added. The second reading is not
    # discarded — its sentence is merged onto the row the first one created, which
    # is also what makes the next run a no-op instead of a diff with no issuer
    # change behind it.
    created: dict = {}
    # Rows this run has already given a fresh quote to. The first write onto a row
    # replaces its quote; later writes onto the SAME row in the SAME run append
    # theirs, because a rate and its cap often come from two different sentences
    # and L8 checks both numbers against the one quote the row carries.
    quoted: dict = {}
    for p in proposals:
        if not isinstance(p, Proposal):
            raise TypeError(f"proposals must hold Proposal, got {type(p).__name__}")
        if only_auto and not p.auto_applicable:
            continue
        if not _is_storable(p):
            continue
        entry = index.get(p.card_id)
        if entry is None:
            continue
        pos = position[p.card_id]

        if p.block:
            if not _apply_row(entry, pos, p, stamped_on, intended, quoted, created):
                continue
        else:
            if p.path not in _SETTABLE_PATHS:
                continue  # never write a path this module did not define itself
            inner = entry["card"]
            key = p.path.split(".", 1)[1]
            if key not in inner:
                continue  # never invent a key: the app would not read it and the
                # gate in tools/kredme.py would not check it
            inner[key] = p.new_value
            _stamp_provenance(entry, p, stamped_on)
        applied.append(p)

    # ★ TRAP 1. The app keys every user's cap progress on '${cardId}|${ruleName}'
    # (app_database.dart:238). Renaming one rule orphans that user's spend history and
    # resets their cap mid-cycle, silently. A bare `assert` is not used because
    # python -O deletes it, and this is the check that protects real people's data.
    after_names = _rule_names(new_cards)
    for key, name in before_names.items():
        if after_names.get(key, _MISSING) != name:
            raise AssertionError(
                "apply_proposals changed a rule_name — this orphans users' cap progress"
            )
    # ★ TRAP 2. Nothing may be replaced wholesale, no row may vanish, and every key
    # that moved must be one this function set out to move. This replaced a
    # byte-for-byte fingerprint of the rule arrays: that fingerprint was the right
    # guard while the writer could not reach inside a row, and the wrong one the
    # moment it could, because it forbids the intended write as loudly as the
    # accident. What has to hold is not "nothing changed" but "only what was meant
    # to change, changed".
    changed, complaint = _row_changes(before_rows, _row_snapshot(new_cards))
    if complaint:
        raise AssertionError("apply_proposals %s" % complaint)
    stray = changed - intended
    if stray:
        raise AssertionError(
            "apply_proposals wrote %d key(s) it did not propose, e.g. %s"
            % (len(stray), sorted(stray)[:3])
        )
    for _n, _arr, _i, key in changed:
        if key in ("rule_name", "milestone_name"):
            raise AssertionError(
                "apply_proposals changed %s — that string is a user's cap key" % key
            )
    # And no card gained or lost a key; only values of existing keys moved.
    if _card_key_shape(new_cards) != before_keys:
        raise AssertionError("apply_proposals added or removed a key on card.*")

    return new_cards, applied


_MISSING = object()

# How confident is confident. Used to keep the LOWER of two claims when two
# observations land on one row: the row is only as verified as its weakest
# citation, and the app defaults a missing value to 'high' (credit_card.dart:463),
# so silence here is a claim in itself.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _apply_row(entry: dict, pos: int, p: Proposal, stamped_on: str,
               intended: set, quoted: dict, created: dict) -> bool:
    """Write one row proposal. False means nothing was written and nothing moved."""
    allowed = WRITABLE_ROW_KEYS.get(p.block, frozenset())
    for key, _value in p.writes:
        if key not in allowed:
            return False        # a tripwire: _propose_row already refuses these

    if p.new_row:
        if p.block != "exclusion_rules":
            return False        # only nameless rows may be created. See RowTarget.
        fingerprint = (pos, p.block, dict(p.new_row).get("exclusion_type"),
                       dict(p.new_row).get("exclusion_value"))
        rows = entry.get(p.block)
        if not isinstance(rows, list):
            rows = []
            entry[p.block] = rows
        if fingerprint in created:
            # One row, two sentences. Same shape as two readings landing on an
            # existing row.
            i = created[fingerprint]
            _stamp_row(rows[i], p, stamped_on, (pos, p.block, i), intended, quoted)
            return True
        row = dict(p.new_row)
        i = len(rows)
        rows.append(row)
        created[fingerprint] = i
        for key in row:
            intended.add((pos, p.block, i, key))
        _stamp_row(row, p, stamped_on, (pos, p.block, i), intended, quoted)
        return True

    rows = entry.get(p.block)
    if not isinstance(rows, list) or not p.rows:
        return False
    targets = [i for i in p.rows
               if isinstance(i, int) and 0 <= i < len(rows) and isinstance(rows[i], dict)]
    if len(targets) != len(p.rows):
        return False            # an index that moved since the proposal was built

    for i in targets:
        row = rows[i]
        for key, value in p.writes:
            row[key] = value
            intended.add((pos, p.block, i, key))
        _stamp_row(row, p, stamped_on, (pos, p.block, i), intended, quoted)
    return True


def _stamp_row(row: dict, p: Proposal, stamped_on: str, rowkey: tuple,
               intended: set, quoted: dict) -> None:
    """Put the evidence ON THE ROW, which is the entire point of this change.

    A card-level `_provenance` record says a card was looked at. It does not say
    that THIS rule's number came from THAT sentence, and counting it as if it did
    is what made verified coverage read four times better than it was. L8 grades
    reward rules on four keys and these are they.

    `source_quote` is a STRING and must stay one: the app hard-casts it
    (credit_card.dart:415), and a Map there throws inside RewardRule.fromJson,
    which utils.dart:222 swallows per card — the card disappears from the
    catalogue with no error anywhere.
    """
    quote = " ".join(str(p.source_quote or "").split())
    existing = row.get("source_quote")
    existing = existing if isinstance(existing, str) else ""
    if rowkey in quoted:
        # Second sentence for the same row. Keep both, elided, so the rate and the
        # cap each have the text that proves them — L8 checks every number the row
        # claims against the one quote the row carries.
        if quote and quote not in existing:
            row["source_quote"] = ("%s … %s" % (existing, quote)).strip(" …")
    else:
        row["source_quote"] = quote

    if p.source_url:
        row["source_url"] = p.source_url
    row["source_fetched_on"] = _iso_date(p.source_fetched_on) or stamped_on

    conf = p.confidence if p.confidence in _CONFIDENCE_RANK else "low"
    previous = quoted.get(rowkey)
    if previous is not None and _CONFIDENCE_RANK[previous] < _CONFIDENCE_RANK[conf]:
        conf = previous      # a row is only as verified as its weakest citation
    quoted[rowkey] = conf
    row["confidence"] = conf

    for key in PROVENANCE_ROW_KEYS:
        intended.add(rowkey + (key,))


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


def _rule_names(cards: list) -> dict:
    """Every rule_name in the catalogue, keyed by where it sits.

    A dict rather than a list because an exclusion row may now be appended, and a
    positional LIST would then report every later rule as having moved — turning
    the one guard that protects users' cap progress into noise nobody could read.
    Keyed, an appended row is a new key and every existing name is still checked.
    """
    out: dict = {}
    for n, entry in enumerate(cards):
        if not isinstance(entry, dict):
            continue
        for arr in RULE_ARRAYS:
            rules = entry.get(arr)
            if not isinstance(rules, list):
                continue
            for i, rule in enumerate(rules):
                if isinstance(rule, dict):
                    out[(n, arr, i)] = rule.get("rule_name")
    return out


def _row_snapshot(cards: list) -> dict:
    """A shallow copy of every row in every rule array, keyed by position."""
    snap: dict = {}
    for n, entry in enumerate(cards):
        if not isinstance(entry, dict):
            continue
        for arr in RULE_ARRAYS:
            rules = entry.get(arr)
            if not isinstance(rules, list):
                continue
            for i, row in enumerate(rules):
                snap[(n, arr, i)] = dict(row) if isinstance(row, dict) else row
    return snap


def _row_changes(before: dict, after: dict) -> tuple:
    """({(card, block, index, key) that moved}, complaint or "").

    A complaint means something happened that no proposal may ever cause — a row
    that vanished, a row replaced by something that is not an object, a key
    deleted. Those are reported as text rather than as a changed key because they
    are not a write anyone intended and there is no key to name.
    """
    changed: set = set()
    for key, row_before in before.items():
        if key not in after:
            return changed, "dropped a row (%s)" % (key,)
        row_after = after[key]
        if not isinstance(row_before, dict) or not isinstance(row_after, dict):
            if row_before != row_after:
                return changed, "replaced a row (%s)" % (key,)
            continue
        for k in row_before:
            if k not in row_after:
                return changed, "removed the key %r from a row (%s)" % (k, key)
            if row_after[k] != row_before[k]:
                changed.add(key + (k,))
        for k in row_after:
            if k not in row_before:
                changed.add(key + (k,))
    for key, row_after in after.items():
        if key in before:
            continue
        if not isinstance(row_after, dict):
            return changed, "appended something that is not a row (%s)" % (key,)
        for k in row_after:
            changed.add(key + (k,))
    return changed, ""


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
    REASON_NO_ROW: "We have no rule on this card about that",
    REASON_AMBIGUOUS_ROW: "More than one rule could be the one they mean",
    REASON_CONFLICT: "The bank's own page says two different things",
    REASON_ROW_UNIT: "The bank counts this in a different unit than we store",
    REASON_CAP_NO_PERIOD: "A cap with no time limit is not a cap",
    REASON_NAME_DISAGREES: "The new number contradicts what the rule is called",
    REASON_QUOTE_LACKS_NUMBER: "The sentence does not contain the number",
    REASON_CARD_EARNS: "This card DOES earn there, so we will not switch it off",
    REASON_UNTYPEABLE: "The app has no category for this kind of spend",
    REASON_QUOTE_LACKS_RATE: "The sentence proves the cap but not what the rule pays",
    REASON_EXCLUSION_SCOPED: "The bank excluded one payment route, not the category",
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
    REASON_NO_ROW: (
        "The bank states this for a kind of spend we have no rule for on this card. "
        "That is usually a missing rule rather than a wrong one — worth adding by "
        "hand, with a name, so users' cap progress has something stable to hang on."
    ),
    REASON_AMBIGUOUS_ROW: (
        "The sentence could be about two different rules on this card and they do "
        "not agree with each other, so writing it to either one would be a coin "
        "toss. Open the link and say which."
    ),
    REASON_CONFLICT: (
        "The same page gives two different figures for the same thing — usually a "
        "headline in one place and the fine print in another, or two tiers. We hold "
        "both rather than picking the one that happened to be read first."
    ),
    REASON_ROW_UNIT: (
        "The bank quotes points where we store a percentage, or rupees where we "
        "store points. Converting needs a point value the page does not state, and "
        "guessing it is how a card ends up showing 0.02%."
    ),
    REASON_CAP_NO_PERIOD: (
        "A cap the app cannot time is a cap it never applies, so the rule would pay "
        "its bonus rate for ever. We only store the cap when the bank's own "
        "sentence says whether it is per month, per cycle, per quarter or per year."
    ),
    REASON_NAME_DISAGREES: (
        "The rule's own name states a different number. We never edit that name — "
        "it is the key every user's cap progress is stored under, and rewriting it "
        "to match would delete their history and destroy the only independent "
        "record of what the rule was meant to be. So the number waits for a person."
    ),
    REASON_QUOTE_LACKS_NUMBER: (
        "The sentence we were given does not contain the figure it is supposed to "
        "prove. That is worse than having no sentence: it reads as verified "
        "everywhere and is about something else."
    ),
    REASON_CARD_EARNS: (
        "The bank's page says this spend earns nothing, but our own rules say this "
        "card pays on it. Switching the exclusion on would zero the card for that "
        "whole merchant family, bonus rules included, so the disagreement goes to a "
        "person first."
    ),
    REASON_UNTYPEABLE: (
        "EMI, ATM withdrawals and 'UPI through another app' are real exclusions the "
        "bank publishes, and the app has no category for any of them. Stored anyway, "
        "they would sit in the file looking like protection and do nothing."
    ),
    REASON_EXCLUSION_SCOPED: (
        "The bank did not say this category earns nothing — it said this category "
        "earns nothing WHEN PAID A PARTICULAR WAY ('education payments made through "
        "third-party apps like CRED'). An exclusion row has no room for the 'when', "
        "so storing it would stop the card earning on that category however it was "
        "paid, including straight to the merchant, and it would do that before any "
        "bonus rule runs. Needs a person, or a schema that can hold the route."
    ),
    REASON_QUOTE_LACKS_RATE: (
        "Everything the bank said about this rule proves its cap or its limit, and "
        "none of it states the rate the rule pays — usually because the bank counts "
        "it differently from us (they print '5X accelerated plus 1X base', we store "
        "6X). Attaching the sentence anyway would make the rule look verified while "
        "the evidence beside it says a different number."
    ),
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
        confirmations = sum(1 for p in auto
                            if _fmt(p.old_value, p.unit) == _fmt(p.new_value, p.unit))
        out += [
            "## Applied",
            "",
            "These matched the bank's own page word for word, and where they moved a "
            "number they moved it in the direction that is safe to trust.",
            "",
        ]
        if confirmations:
            out += [
                f"**{confirmations} of them changed no number at all.** The "
                "bank's page said exactly what we already had, so all that landed was "
                "the sentence and the link — which is the difference between a rule "
                "somebody once typed and a rule you can check.",
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
    was, now = _fmt(p.old_value, p.unit), _fmt(p.new_value, p.unit)
    if was == now:
        # Most of what this writer does is confirm a number and attach the proof.
        # Printing "5% → 5% (+0.0%)" made that read like churn, and the founder is
        # right to distrust numbers that appear to keep moving.
        line = (f"- **{p.card_id or 'unknown card'}** — {label}: unchanged at {now}, "
                f"now with the bank's own sentence behind it")
        rows = [line]
        quote = (p.source_quote or "").strip()
        rows.append(f"  > {_one_line(quote)}" if quote
                    else "  > (the bank's sentence was not supplied)")
        if p.source_url:
            rows.append(f"  [Where this came from]({p.source_url})")
        return rows
    line = f"- **{p.card_id or 'unknown card'}** — {label}: {was} → {now}"
    if p.delta_pct is not None and abs(p.delta_pct) >= 0.05:
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
    if unit == UNIT_CATEGORY:
        if value == "category":
            return "switched on as a spend category"
        if value == "mcc":
            return "switched on as a merchant code"
        return "not enforced" if value is None else str(value)
    if value is None:
        return "not set"
    n = _fnum(value)
    if n is None:
        # A cap stored as prose ("250 FP/month first 6 months"). The app's _numOf
        # returns null for it, which means no cap at all, so showing it verbatim is
        # the point: the reader sees what is being replaced.
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
    if unit == UNIT_FRACTION:
        return f"{n * 100:g}% of the spend"
    if unit == UNIT_POINTS_PER_BLOCK:
        return f"{n:g} points a block"
    if unit == UNIT_MULTIPLIER:
        return f"{n:g}X the base rate"
    if unit == UNIT_POINTS:
        return f"{n:g} points"
    if unit == UNIT_MONTHS:
        return f"{n:g} months"
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
        # The date the page was READ, if the record carries it. Falls back to
        # today inside observations_to_proposals, but a real fetch date is what
        # makes a citation's freshness checkable rather than asserted.
        fetched_on = str(record.get("source_fetched_on")
                         or record.get("fetched_at") or "")
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
            proposals.extend(observations_to_proposals(
                entry, observations, url, fetched_on=fetched_on))
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
