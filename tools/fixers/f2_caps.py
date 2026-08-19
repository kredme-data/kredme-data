"""Cap repairs — the ceiling a reward rule stops paying at.

A cap is the most dangerous number in this file to touch, and the reason is not
arithmetic. The app keys a user's spend bucket on the rule NAME and counts their
progress against `cap_amount`. Move the cap and you move a ceiling somebody is
already part-way up: a user who has earned 900 of "1,000 points a month" wakes up
either finished or back at the start. So every edit here states, in one sentence
a non-technical reader can audit, which number it is trusting and why.

WHAT THIS MODULE WILL AND WILL NOT DO
-------------------------------------
It will:
  R1  turn a cap written as a sentence ("2000 Fuel Points per month") into the
      plain number the app can actually enforce. Today the app drops the string
      and treats the rule as UNCAPPED, so this is always a move from "no ceiling
      at all" to "the issuer's ceiling".
  R2  restore a cap the rule's own name states, when the stored field disagrees
      and the app is already counting in the same unit the name uses.
  R3  add `cap_kind: "spend"` when the name says the ceiling is on eligible
      SPEND and the stored number is exactly the number in that sentence.
  R4  fill in `cap_period` when — and only when — the rule's own name says one.

It will NOT:
  * touch `rule_name`. Ever. The name is the only independent evidence in the
    file, and it is the key the app buckets saved cap progress under. A fixer
    that edits it is both destroying the audit trail and wiping user progress.
  * convert a cap into rupees. Settled KredMe policy: caps live in the ISSUER's
    unit — points for a points card, rupees for a cashback card. A rupee cap on
    a points card silently changes meaning the day anybody corrects the point
    value, which is exactly the incident this rule was written after.
  * guess a `cap_period`. A cap with a guessed period is worse than a cap with
    none: the user's rewards stop on a date nobody chose, and nothing in the file
    records that it was a guess.
  * repair a cap whose unit is wrong because the RULE'S TYPE is wrong. On those
    rows the name says points and `reward_type: 'cashback_pct'` makes the app
    subtract rupees. The field that needs changing is `reward_type`, which
    belongs to the rate family, not to this one — two modules editing one row is
    how half-fixes ship. Those rows are left standing on purpose, and they
    converge: once the rate family retypes the rule, the same row resurfaces as
    L5.CAP_AMOUNT_MISMATCH and R2 below repairs it on the next pass.

The name parser is imported from tools/checks/c5_consistency rather than
reimplemented. That is deliberate: an edit is only worth proposing if it
provably clears the finding that prompted it, and the only way to be sure of
that is to read the sentence with the same eyes the validator used.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from checks.base import num
from checks.c5_consistency import _BUCKET, _cap_claims, _engine_cap_unit
from fixers.base import CERTAIN, LIKELY, Edit, trunc

FAMILY = "caps"

HANDLES = [
    "L4.CAP_NOT_A_NUMBER",
    "L4.CAP_IN_RUPEES",
    "L4.CAP_WITHOUT_PERIOD",
    "L5.CAP_UNIT_MISMATCH",
    "L5.CAP_AMOUNT_MISMATCH",
    "L5.CAP_IN_TEXT_NOT_ENFORCED",
]

BLOCK = "reward_rules"

# Nothing in this module may ever write these keys. Enforced, not documented:
# `rule_name` is the audit trail AND the user's cap-progress key, and the three
# reward_* fields are the rate family's — a cap fixer reaching into them is how
# one row gets edited twice by two modules that each saw half of it.
FORBIDDEN_FIELDS = frozenset({
    "rule_name", "reward_type", "reward_rate", "reward_unit_spend",
})

# A cap below 1 unit is not a ceiling anybody publishes, and above 10 million it
# is not a ceiling either — it is a spend threshold or a typo. Both ends refuse.
CAP_MIN, CAP_MAX = 1.0, 10_000_000.0

# The unit words that make a cap string a POINTS ceiling. Stripping these and
# writing the bare number onto a rule the app reads in rupees is how two
# IndianOil cards ended up with a ceiling about 2x the one the bank states:
# "2000 Fuel Points per month" became 2000.0 on a cashback_pct 0.075 rule, so
# the app exhausted it at Rs 26,667 of fuel spend where the issuer exhausts it
# at Rs 13,333. Before the edit `double.tryParse` returned null and no cap was
# enforced at all, so the repair converted "unreadable" into "confidently
# wrong" — strictly worse, and the reason this list exists.
_POINTS_UNIT = re.compile(
    r"(?i)\b(?:fuel\s+points?|reward\s+points?|edge\s+points?|cash\s*points?|"
    r"saving\s+points?|points?|pts?|fps?|rps?|miles?|coins?|"
    r"supercoins?|neucoins?|stars?)\b")


@dataclass
class Refusal:
    """A cap defect this module looked at and deliberately did NOT repair.

    Same shape as f1_units.Refusal, and here for the same reason: "31 cap
    findings in, 28 edits out" says nothing about the 3, and the 3 are the
    interesting ones. The runner picks this up through the optional refusals()
    accessor and prints it beside the plan.
    """
    card_id: str | None
    code: str
    what: str
    why: str
    block: str | None = BLOCK
    index: int | None = None
    evidence: str | None = None

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}

# --------------------------------------------------------------------------- #
# Reading a cap out of a string
# --------------------------------------------------------------------------- #
_SCALE = {
    "k": 1_000.0, "l": 100_000.0, "lakh": 100_000.0, "lakhs": 100_000.0,
    "lac": 100_000.0, "lacs": 100_000.0, "cr": 10_000_000.0,
    "crore": 10_000_000.0, "crores": 10_000_000.0,
}
_NUMBER = re.compile(
    r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)\s*(lakhs?|lacs?|crores?|cr|k|L)?\b")

# "2x base cashback earned in same statement month" carries the digit 2 and
# means nothing of the kind — the ceiling is a MULTIPLE of another number that
# is not in this file. Reading 2 out of it would cap the rule at two rupees.
_RELATIVE = re.compile(r"(?i)\bbase\b|\d\s*[xX]\b|\btimes\b|\bwhichever\b|\bequal\s+to\b")

_PERIOD_WORDS = [
    ("year",    re.compile(r"(?i)\b(?:per\s+annum|annually|annual|yearly|per\s+year|"
                           r"in\s+a\s+year|calendar\s+year|/\s*(?:yr|year))\b")),
    ("quarter", re.compile(r"(?i)\b(?:per\s+quarter|quarterly|calendar\s+quarter|"
                           r"/\s*(?:qtr|quarter))\b")),
    ("cycle",   re.compile(r"(?i)\b(?:statement\s+(?:cycle|month|period)|billing\s+cycle|"
                           r"payment\s+cycle|per\s+cycle|per\s+statement|bill\s+cycle)\b")),
    ("month",   re.compile(r"(?i)\b(?:per\s+(?:calendar\s+)?month|monthly|per\s+month|"
                           r"/\s*(?:mo|month)|in\s+a\s+month)\b")),
]


def _scaled(digits: str, scale: str) -> float | None:
    try:
        v = float(digits.replace(",", ""))
    except (TypeError, ValueError):
        return None
    if scale:
        v *= _SCALE.get(scale.strip().lower(), 1.0)
    return v


def parse_cap_string(raw):
    """('2000 Fuel Points per month') -> (2000.0, 'month'), or (None, None).

    Pure parsing, and deliberately timid. It answers only when the string names
    exactly ONE magnitude, because the strings that name two — "250 FP/month
    first 6 months, 150 FP/month thereafter" — encode a schedule the app has no
    way to hold, and picking either number would be choosing on the issuer's
    behalf. Those stay defects.
    """
    if not isinstance(raw, str):
        return None, None                      # a dict or a list is a schedule
    s = raw.strip()
    if not s or _RELATIVE.search(s):
        return None, None
    vals = []
    for m in _NUMBER.finditer(s):
        v = _scaled(m.group(1), m.group(2) or "")
        if v is not None:
            vals.append(v)
    distinct = {round(v, 6) for v in vals}
    if len(distinct) != 1:
        return None, None                      # zero magnitudes, or a schedule
    amount = vals[0]
    if not (CAP_MIN <= amount <= CAP_MAX):
        return None, None
    periods = {k for k, rx in _PERIOD_WORDS if rx.search(s)}
    return amount, (periods.pop() if len(periods) == 1 else None)


# --------------------------------------------------------------------------- #
# Reading the cap out of the rule's own name
# --------------------------------------------------------------------------- #
def _name_caps(name: str):
    """(claims, units, amounts, unit_flavour) exactly as c5_consistency sees them.

    `unit_flavour` reproduces the validator's own branch: the single unit the
    name uses, when it uses exactly one, else None. Sharing this logic is the
    point — an edit that does not clear the finding is not a fix.
    """
    claims = _cap_claims(name or "")
    units = {u for _, u, _ in claims if u}
    amounts = {a for a, u, _ in claims if u != "spend"}
    flavour = units.pop() if len(units) == 1 else None
    return claims, flavour, amounts


def _sole_claim(claims, unit=None):
    """The one claim in this name, or None if the sentence is not that simple."""
    picked = [c for c in claims if unit is None or c[1] == unit]
    return picked[0] if len(picked) == 1 else None


def _close(a, b) -> bool:
    """The validator's own tolerance, so 'agrees' means the same thing here."""
    return abs(a - b) <= max(0.02 * max(abs(a), abs(b)), 0.5)


def _periods_compatible(claim_period, stored_period) -> bool:
    """True when the name's cap and the stored cap are measured over the SAME
    stretch of time, so their numbers can honestly be compared.

    Raw equality, not the engine's bucket. The engine folds 'per transaction'
    into the calendar month, which makes "max Rs 100 per transaction" and
    "Rs 200 per month" look like the same period to a bucket comparison — and
    copying the 100 over the 200 would cut a real monthly cashback ceiling in
    half on the strength of a sentence that never mentioned a month.
    """
    if claim_period is None or stored_period is None:
        return True                            # one side is silent; nothing to clash
    return claim_period == stored_period


# --------------------------------------------------------------------------- #
# Finding access — the runner may hand us dicts or Finding objects
# --------------------------------------------------------------------------- #
def _fget(f, key):
    return f.get(key) if isinstance(f, dict) else getattr(f, key, None)


def _scope(findings) -> set:
    """The card ids this run is allowed to touch.

    Findings are card-level aggregates: one finding says "5 caps on this card"
    and names only the first row. So the finding tells us WHICH CARD to open,
    and every offending row is then re-derived from the data itself. Trusting
    the index alone would repair one row of five and report success.
    """
    ids = set()
    for f in findings or []:
        if _fget(f, "code") in HANDLES:
            cid = _fget(f, "card_id")
            if cid:
                ids.add(cid)
    return ids


def _edit(card_id, index, field, old, new, code, reason, evidence,
          confidence=LIKELY, notes=None) -> Edit:
    if field in FORBIDDEN_FIELDS:
        raise AssertionError(
            f"the caps fixer tried to write {field!r}; that field is not its to "
            "write and the refusal is deliberate")
    return Edit(card_id=card_id, block=BLOCK, index=index, field=field,
                old_value=old, new_value=new, code=code, reason=reason,
                evidence=evidence, confidence=confidence, reversible=True,
                family=FAMILY, notes=notes or {})


def _move(old, new) -> dict:
    """Structured before/after, so a reviewer sees the size of the swing without
    doing the division themselves."""
    d = {"direction": "raised" if new > (old or 0) else "lowered"}
    if old:
        d["factor"] = round(new / old, 4)
    return d


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def _txt_kind(row) -> str:
    v = row.get("cap_kind")
    return v.strip().lower() if isinstance(v, str) else ""


def _would_take_spend(row) -> bool:
    """Would R3 stamp cap_kind='spend' on this sibling too, on its own merits?"""
    if not isinstance(row, dict):
        return False
    stored = num(row.get("cap_amount"))
    if stored is None:
        return False
    period = row.get("cap_period")
    period = period.strip().lower() if isinstance(period, str) and period.strip() else None
    if period == "transaction":
        return False
    name = row.get("rule_name") if isinstance(row.get("rule_name"), str) else ""
    claims, flavour, _amounts = _name_caps(name)
    if flavour != "spend" or _engine_cap_unit(row) == "spend":
        return False
    claim = _sole_claim(claims, "spend")
    return bool(claim and _close(claim[0], stored)
                and _periods_compatible(claim[2], period))


# The ceiling phrase, with the number taken out. "up to a maximum of ₹5,000 per
# month" and "up to a maximum of ₹4,000 per month" are the SAME sentence written
# about two different rules, and any reading of one is a reading of both.
_CAP_PHRASE = re.compile(
    r"(?i)\b(up\s*to\s*(?:a\s*)?(?:maximum|max)\s*of|maximum\s*of|capped\s*at|"
    r"subject\s*to\s*a?\s*(?:maximum|cap)\s*of|up\s*to)\s*"
    r"(?:rs\.?|₹|inr)?\s*[\d,]+(?:\.\d+)?\s*"
    r"([a-z][a-z\s]{0,25}?)?\s*(?:per|/|a)\s*"
    r"(month|year|quarter|cycle|statement|transaction|annum|day)\b")


def _cap_phrase(name: str):
    """The normalised ceiling sentence in this rule's name, or None."""
    m = _CAP_PHRASE.search(name or "")
    if not m:
        return None
    return ("".join(m.group(1).lower().split()),
            " ".join((m.group(2) or "").split()).lower(),
            m.group(3).lower())


def _sibling_pattern(rows, j, _claims) -> list:
    """Row indexes on the SAME card whose name states its cap in the same words.

    Compared on the SENTENCE, not on what c5's claim parser made of it — that
    is the whole point. On idfc_first_bank_hpcl_first_power four rules carry
    "up to a maximum of ₹N per month" and the parser returns 'spend' for two of
    them, 'inr' for one and nothing at all for the fourth, purely because of what
    else is in the sentence. Stamping cap_kind on the two it happened to read as
    'spend' left one card carrying two contradictory meanings for one issuer's
    wording, with rule[2] biting at Rs 4,000 of spend a month and its
    identically-worded sibling rule[0] at Rs 100,000 — 40x apart, and at most one
    of them right.
    """
    want = _cap_phrase(rows[j].get("rule_name") if isinstance(rows[j], dict) else "")
    if want is None:
        return []
    out = []
    for k, other in enumerate(rows):
        if k == j or not isinstance(other, dict):
            continue
        name = other.get("rule_name") if isinstance(other.get("rule_name"), str) else ""
        if _cap_phrase(name) == want:
            out.append(k)
    return out


def plan(ctx, findings) -> list[Edit]:
    """PURE. Reads ctx and the findings, returns proposed edits, writes nothing."""
    return _analyse(ctx, findings)[0]


def refusals(ctx, findings) -> list[Refusal]:
    """PURE. The other half: cap defects examined and deliberately left alone."""
    return _analyse(ctx, findings)[1]


def _analyse(ctx, findings):
    """(edits, refusals). One pass, both answers."""
    scope = _scope(findings)
    refused: list[Refusal] = []
    if not scope:
        return [], refused

    edits: list[Edit] = []
    seen: set = set()                       # (card, index, field) — one edit each

    def add(e: Edit):
        k = (e.card_id, e.index, e.field)
        if k not in seen:
            seen.add(k)
            edits.append(e)

    for _, entry, inner, cid in ctx.entries():
        if cid not in scope:
            continue
        rows = entry.get(BLOCK)
        if not isinstance(rows, list):
            continue
        for j, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = row.get("rule_name") if isinstance(row.get("rule_name"), str) else ""
            raw = row.get("cap_amount")
            stored = num(raw)
            period = row.get("cap_period")
            period = period.strip().lower() if isinstance(period, str) and period.strip() else None
            eng = _engine_cap_unit(row)
            claims, flavour, amounts = _name_caps(name)

            # ---- R1  the cap is a sentence, so today there is no cap ------ #
            if stored is None and raw is not None:
                amount, str_period = parse_cap_string(raw)
                if amount is not None:
                    # THE UNIT COMES FIRST. If the sentence names points and the
                    # app counts rupees on this rule, writing the bare number
                    # makes a ceiling the app WILL enforce, at a denomination
                    # the issuer never stated. An unreadable cap enforces
                    # nothing, which is safe; an enforceable cap that is ~2x
                    # wrong is not. base.py:70-80 already forbids shipping half
                    # of a coupled pair, and this was exactly that: the partner
                    # retype belongs to the rate family and was never planned
                    # for either IndianOil card.
                    named_unit = _POINTS_UNIT.search(raw)
                    if named_unit and eng != "points":
                        refused.append(Refusal(
                            cid, "L4.CAP_NOT_A_NUMBER", f"reward_rules[{j}]", index=j,
                            why=(f"The ceiling is written in {named_unit.group(0).lower()}, but "
                                 f"this rule's type makes the app count "
                                 f"{'rupees of cashback' if eng == 'inr' else eng}, so storing "
                                 f"{amount:,.0f} as a plain number would enforce a ceiling in the "
                                 f"wrong unit — roughly twice the one the bank states. The cap "
                                 f"stays unreadable, and unenforced, until the rule's type is "
                                 f"corrected too."),
                            evidence=f"cap_amount={raw!r}, reward_type={row.get('reward_type')!r}"))
                        continue
                    notes = {"parsed_from": trunc(raw, 90), "engine_counts": eng}
                    add(_edit(
                        cid, j, "cap_amount", raw, amount,
                        "L4.CAP_NOT_A_NUMBER",
                        f"The ceiling was written as the sentence \"{trunc(raw, 60)}\", which the "
                        f"app cannot read, so this rule has been running with no ceiling at all; "
                        f"this stores the issuer's own number, {amount:,.0f}, as a plain number "
                        f"the app can enforce, in the unit the rule already counts in.",
                        evidence=f"cap_amount = {raw!r}",
                        confidence=CERTAIN, notes=notes))
                    if str_period and period is None:
                        add(_edit(
                            cid, j, "cap_period", row.get("cap_period"), str_period,
                            "L4.CAP_NOT_A_NUMBER",
                            f"The same sentence says the ceiling resets per {str_period}, and the "
                            f"app needs a period as well as an amount before it will enforce "
                            f"either.",
                            evidence=f"cap_amount = {raw!r}",
                            confidence=CERTAIN,
                            notes={"parsed_from": trunc(raw, 90)}))
                # Two magnitudes, a dict, or a cap defined as a multiple of some
                # other number: nothing here is derivable, so it stays a defect.
                continue

            # ---- R3  the name caps SPEND, the app caps the reward --------- #
            # Reproduces the validator's own branch: it only calls this a unit
            # mismatch when the name uses exactly one unit and that unit is
            # 'spend'. Requiring the stored number to BE the number in that
            # sentence is the guardrail — it is what proves the field is holding
            # a spend figure rather than a reward figure that merely looks odd.
            if flavour == "spend" and eng != "spend" and stored is not None:
                claim = _sole_claim(claims, "spend")
                if claim and _close(claim[0], stored) and \
                        _periods_compatible(claim[2], period):
                    # The engine has no per-transaction bucket.
                    # _getSpentForRule handles 'quarter' and 'year' and sends
                    # everything else — 'month', 'cycle', 'transaction' — to the
                    # current calendar month. So cap_kind 'spend' on a
                    # cap_period 'transaction' row does not build the ceiling the
                    # sentence describes; it silently converts a per-purchase cap
                    # into a per-month one, a 20x tightening this edit would be
                    # making without saying so.
                    if period == "transaction":
                        refused.append(Refusal(
                            cid, "L5.CAP_UNIT_MISMATCH", f"reward_rules[{j}]", index=j,
                            why=("The sentence caps a single transaction, and the app has no "
                                 "per-transaction bucket — it folds that into the calendar "
                                 "month — so telling it to count spend here would enforce a "
                                 "monthly ceiling roughly twenty times tighter than the one "
                                 "the issuer wrote."),
                            evidence=trunc(name, 160)))
                        continue
                    # A cap_kind edit is a claim about how the issuer WRITES a
                    # sentence, so it cannot be true of one rule and false of the
                    # rule beside it. Where a sibling on this card states its cap
                    # the same way and is not getting the same edit, both
                    # readings would ship on one card and at most one can be
                    # right.
                    twins = _sibling_pattern(rows, j, claims)
                    untouched = [k for k in twins
                                 if _txt_kind(rows[k]) != "spend"
                                 and not _would_take_spend(rows[k])]
                    if untouched:
                        refused.append(Refusal(
                            cid, "L5.CAP_UNIT_MISMATCH", f"reward_rules[{j}]", index=j,
                            why=(f"Rule(s) {untouched} on this same card state their ceiling in "
                                 f"the same words and would keep the opposite reading, so the "
                                 f"card would carry two contradictory meanings for one issuer's "
                                 f"sentence — and this one's ceiling would bite 40x sooner than "
                                 f"its twin's. Either all of them move or none of them do."),
                            evidence=trunc(name, 160)))
                        continue
                    add(_edit(
                        cid, j, "cap_kind", row.get("cap_kind"), "spend",
                        "L5.CAP_UNIT_MISMATCH",
                        f"The rule's own sentence says the ceiling is on how much a customer "
                        f"SPENDS, and the stored number is exactly the figure in that sentence "
                        f"({claim[0]:,.0f}), so this tells the app to count spending against it "
                        f"instead of counting rewards — the ceiling will now bite far sooner than "
                        f"it does today.",
                        evidence=trunc(name, 160),
                        confidence=LIKELY,
                        notes={"engine_counted": eng, "engine_will_count": "spend",
                               "reads_much_tighter": True}))
                continue

            # A unit mismatch of the points-vs-rupees kind is a wrong rule TYPE,
            # not a wrong cap. Repairing it means editing reward_type, which is
            # the rate family's field. Left standing on purpose; it comes back
            # as L5.CAP_AMOUNT_MISMATCH once the retype lands, and R2 takes it.
            if flavour in ("points", "inr") and stored is not None and (
                    (flavour == "points" and eng == "inr") or
                    (flavour == "inr" and eng == "points")):
                continue

            # ---- R2  the name states the cap and the field disagrees ------ #
            if stored is not None and len(amounts) == 1:
                claim = _sole_claim(claims)
                want = next(iter(amounts))
                if claim and not _close(want, stored) and \
                        CAP_MIN <= want <= CAP_MAX and \
                        _periods_compatible(claim[2], period):
                    unit_named = claim[1] is not None
                    add(_edit(
                        cid, j, "cap_amount", raw, want,
                        "L5.CAP_AMOUNT_MISMATCH",
                        f"The rule's own name states a ceiling of {want:,.0f} and the app is "
                        f"counting in the same unit, but the stored ceiling is {stored:,.0f}, so "
                        f"customers are being stopped at the wrong point; the name is the only "
                        f"independent evidence in the file, so the number moves to match it and "
                        f"the name is left untouched.",
                        evidence=trunc(name, 160),
                        confidence=CERTAIN if unit_named else LIKELY,
                        notes={**_move(stored, want), "engine_counts": eng,
                               "name_unit": claim[1] or "unstated",
                               "resets_user_progress": True}))

            # ---- R4  a cap with no period ---------------------------------- #
            # Only from the cap's OWN phrase in the name. If the sentence does
            # not say one, no edit: a guessed period silently changes the day a
            # user's rewards stop, and it would look identical to a real one.
            if stored is not None and period is None:
                claim = _sole_claim(claims)
                if claim and claim[2]:
                    add(_edit(
                        cid, j, "cap_period", row.get("cap_period"), claim[2],
                        "L4.CAP_WITHOUT_PERIOD",
                        f"The rule gives a ceiling but never says per what, so the app enforces "
                        f"nothing at all; the rule's own name says the ceiling is per "
                        f"{claim[2]}, and that is the only period stated anywhere in the file.",
                        evidence=trunc(name, 160),
                        confidence=LIKELY,
                        notes={"read_from": "rule name"}))

    return edits, refused
