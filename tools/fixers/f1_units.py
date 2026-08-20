"""Rates & units — repair the numbers, never the sentence.

This is the family that carries the 5x-overstatement bug, and it has exactly one
derivation, applied five ways:

    the rule NAME states the issuer's mechanic          "15 EDGE Reward Points per Rs. 100"
    the card's rp_value_standard says what a point is    Rs 0.20
    therefore the true rate is  N * rp_value / M         15 * 0.20 / 100 = 3%

Where the stored reward_rate disagrees, THE STORED VALUE IS WRONG AND THE NAME IS
RIGHT. Never the reverse. The name is the only independent evidence in the file:
edit it to make a number agree and the rule can never be audited again. It is
also the key the app buckets a user's saved cap progress under, so changing the
string wipes real people's progress. This module therefore never proposes an
edit whose field is `rule_name`, and there is an assertion at the bottom of
plan() that makes that structural rather than a promise.

WHAT COMES OUT
--------------
Retypes, and one scalar. A rule stored as `multiplier 15` or `cashback_pct 0.15`
becomes `points_per_spend 15 per 100`, which is what the sentence in its own name
always said. Nothing is invented: N and M are read out of the name, the point
value is read off the card, and where either is missing the defect survives.

THE ONLY-GO-DOWN RULE
---------------------
A rate correction that is not issuer-sourced may only go DOWN. Raising a number
on the strength of our own arithmetic ships a figure nobody can defend. Every
proposed edit is rendered through the app's own arithmetic before and after, and
any that would raise what a user sees is refused and recorded — see refusals().

There is exactly one carve-out, and it is deliberate: a rule the app currently
shows as "Rate not published" (L5.NAME_STATES_RATE_APP_HAS_NONE) makes NO claim
today, so letting it render the issuer's own sentence is not raising a claim, it
is making one for the first time. That path carries its own tighter guardrail —
the card must state a real point value of its own, never the app's invented 25
paise — plus a hard 15% plausibility ceiling, and anything above 10% is marked
for a human to eyeball.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _dc_field

from checks import c8_provenance as _c8
from checks.base import Ctx, num
from checks.c4_numeric import _is_cashback_card
from checks.c5_consistency import (
    _close, _engine, _pct_claims, _pps_claims, _pt_count_claims, _sane_pv, _txt,
)
from fixers.base import CERTAIN, LIKELY, Edit, trunc

FAMILY = "rates & units"

HANDLES = [
    "L5.RATE_CONTRADICTS_NAME",
    "L4.BASE_FIELD_VS_BASE_RULE",
    "L5.NAME_STATES_RATE_APP_HAS_NONE",
    "L4.POINT_VALUE_VS_REDEMPTION",
    "L4.POINT_VALUE_OUT_OF_BAND",
]

# --------------------------------------------------------------------------- #
# Thresholds. Every one of these is the SAME number the validator already uses,
# so a fix can never be proposed that the checks would immediately re-flag.
# --------------------------------------------------------------------------- #
PV_LO, PV_HI = 0.05, 2.0        # c4_numeric.PV_BAND_LO / PV_BAND_HI
UNIT_LO, UNIT_HI = 50.0, 1000.0  # "N points per Rs M": M outside this is not an earn unit
CEILING_PCT = 15.0               # nothing derived here may render above this
REVIEW_PCT = 10.0                # above this, a human looks before it ships
AGREE_TOL = 0.005                # c4's "these two render the same number" tolerance
EPS = 1e-9


# --------------------------------------------------------------------------- #
# Refusal — the third answer, borrowed from checks.base.Skipped
# --------------------------------------------------------------------------- #
@dataclass
class Refusal:
    """A defect this module looked at and deliberately did NOT fix.

    plan() returns edits only, so without this the interesting half of the work
    would be invisible: "22 findings in, 13 edits out" says nothing about the 9,
    and one of those 9 is the down-only rule stopping a rate from being raised —
    which is the single most important thing this module does. A runner that
    wants the record calls refusals(); it is the same analysis, the other half.
    """
    card_id: str | None
    code: str
    what: str            # the thing not fixed, e.g. "reward_rules[2]"
    why: str             # one plain sentence
    block: str | None = None
    index: int | None = None
    evidence: str | None = None

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clean(x: float) -> float:
    """Kill binary-float noise before it lands in a JSON file a human reads.

    0.02 * 100 is 2.0000000000000004 in IEEE754, and a diff line reading
    `"reward_rate": 2.0000000000000004` is how a reviewer learns to stop reading
    diffs. Ten decimal places is far finer than any rate this file carries.
    """
    return round(float(x), 10)


def _pct(x) -> str:
    return "?" if x is None else ("%.2f" % x)


def _rows(entry: dict, block: str) -> list:
    v = entry.get(block)
    return v if isinstance(v, list) else []


def _fget(f, key):
    """Findings arrive either as Finding objects or as dicts out of --json."""
    if isinstance(f, dict):
        return f.get(key)
    return getattr(f, key, None)


def _cards_by_code(findings) -> dict:
    """{code: {card_id, ...}} for the codes this module owns.

    Keyed on the CARD, not the row, on purpose. L5's findings are aggregated one
    per (card, defect class) — `axis_bank_samsung_axis_bank_infinite` carries a
    single finding covering six rules and no index at all — so a fixer that
    trusted finding['index'] would repair one rule of six and call it done. The
    finding tells us WHICH CARD to look at; this module re-derives which rows
    are actually defective from the data itself.
    """
    out = {c: set() for c in HANDLES}
    for f in findings or []:
        code = _fget(f, "code")
        cid = _fget(f, "card_id")
        if code in out and cid:
            out[code].add(cid)
    return out


def _best_unconditional(entry: dict):
    """(rupees per point, channel) from THIS card's own redemption block.

    Settled KredMe policy, 17-Aug: rp_value_standard tracks the best route a
    user can take with NO conditions attached. That means cash back or statement
    credit and nothing else — never a travel portal, never an airmiles transfer,
    never a merchant voucher, because a user who does not want that airline gets
    none of that value. Imported by checks/c4_numeric as well, so the fix and the
    check that asked for it read the block the same way; one function, one
    answer.

    "No conditions attached" is read off the row, not off the channel name
    alone. `redemption_fee_inr` and `min_points` sit in the same row and are
    exactly the conditions the policy excludes — a Rs 99 fee or a 500-point
    floor makes a route conditional however it is labelled. Reading only
    channel_type derived three of the eight point-value corrections from fee-
    bearing routes, and because rp_value_standard is the denominator of every
    rate on the card, hdfc_bank_rupay_irctc's base rule went 1.00% -> 0.30% on
    the strength of a Rs 99 charge.
    """
    best, ch = None, None
    for r in _rows(entry, "redemption_rules"):
        if not isinstance(r, dict):
            continue
        c = r.get("channel_type")
        c = c.lower() if isinstance(c, str) else ""
        if c not in ("cashback", "statement_credit"):
            continue
        fee = num(r.get("redemption_fee_inr"))
        if fee is not None and fee > 0:
            continue                              # costs money to use — conditional
        if r.get("min_points") is not None:
            continue                              # a floor to clear — conditional
        v = num(r.get("point_value_inr"))
        if v is None:
            continue
        if best is None or v > best:
            best, ch = v, c
    return best, ch


# "1 RP= 0.50", "1 point = Rs 0.25", "1 reward point is worth ₹1". The issuer's
# own sentence pricing its own point, which is the strongest evidence in the file.
_QUOTED_PV = re.compile(
    r"(?i)\b1\s*(?:rp|rps|reward\s*points?|points?|pts?|cash\s*points?|miles?)\s*"
    r"(?:=|:|is\s+worth|worth|equals?)\s*"
    r"(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)")


def _issuer_priced(entry: dict, inner: dict):
    """(rupees per point the ISSUER states, evidence) off this card's own rows.

    Only counts when the row carries a link on the issuer's own domain AND a
    quote from it that prices the point. Both halves matter: a quote with no
    page is not a source, and a page with no quote does not state a number.

    This exists because the point-value stage read a card's redemption block and
    concluded the stored 0.5 was "a verbatim copy of card.rp_value_standard". On
    equitas_powermiles it was a verbatim copy of the issuer's own sentence —
    "Earn 3RP on every Rs.100 spent. 1 RP= 0.50" — fetched from equitas.bank.in
    on 2026-08-14 and sitting in the row being edited. Halving it took the app
    from 1.50% to 0.75% and left the bank's sentence in the file contradicting
    the new number. An issuer-stated figure is not ours to correct with
    arithmetic; it is corrected by reading the issuer again.
    """
    domains = _c8._issuer_domains(_txt(inner.get("issuer")))
    for block in ("reward_rules", "redemption_rules"):
        for j, r in enumerate(_rows(entry, block)):
            if not isinstance(r, dict):
                continue
            quote = _txt(r.get("source_quote"))
            if not quote:
                continue
            m = _QUOTED_PV.search(quote)
            if not m:
                continue
            if not _issuer_sourced(r, domains):
                continue
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if v <= 0:
                continue
            return v, (f"{block}[{j}] source_quote {trunc(quote, 80)!r} from "
                       f"{trunc(r.get('source_url'), 70)}")
    return None, None


# A rule name that gates its own rate. "8% ... after Rs. 5 lakhs total spend" is
# not an 8% card; switching it on with conditions_json null tells every user they
# earn it from the first rupee.
_THRESHOLD = re.compile(
    r"(?i)\bafter\s+(?:rs\.?|inr|₹)?\s*[\d,.]+|\blakhs?\b|\bcrores?\b|"
    r"\bfor\s+plus\s+members\b|\bmilestone\b|\bon\s+achieving\b|"
    r"\bminimum\s+(?:spends?|transaction)\b|\bfirst\s+\d+\s+(?:months?|transactions?)\b")


def _threshold_phrase(name: str):
    """The phrase in this name that gates the rate, or None."""
    m = _THRESHOLD.search(name or "")
    return m.group(0).strip() if m else None


def _pv_witness(entry: dict, inner: dict):
    """(point value we may PUBLISH a new rate on, one-line reason) — or (None, why not).

    Stricter than `_sane_pv`, and deliberately so: this is the only place a rate
    goes UP, and "the card states a point value" turned out to be satisfied by a
    number copied off a card-review site. To count, the value must be plausible
    AND be witnessed either by the issuer's own page or by an unconditional
    cash-out route on this card that is not itself tagged to an aggregator.
    """
    rp = num(inner.get("rp_value_standard"))
    if rp is None:
        return None, "never stated at all"
    if not (PV_LO <= rp <= PV_HI):
        return None, f"Rs {rp:g}, outside the band any real point sits in"

    quoted, where = _issuer_priced(entry, inner)
    if quoted is not None and abs(quoted - rp) <= AGREE_TOL:
        return rp, f"the issuer's own quote ({where})"

    best, ch = _best_unconditional(entry)
    if best is not None and abs(best - rp) <= AGREE_TOL:
        for r in _rows(entry, "redemption_rules"):
            if not isinstance(r, dict):
                continue
            c = _txt(r.get("channel_type")).strip().lower()
            if c not in ("cashback", "statement_credit"):
                continue
            if num(r.get("point_value_inr")) is None or \
                    abs(num(r.get("point_value_inr")) - best) > AGREE_TOL:
                continue
            if _aggregator_tagged(r):
                continue
            return rp, f"this card's own {ch} route at Rs {best:g}, no fee and no minimum"
        return None, (f"witnessed only by a {ch} row copied from a card-review site, "
                      "which is not a source this repo accepts")
    return None, ("stated with nothing behind it — no issuer quote, and no "
                  "unconditional cash-out route on the card agrees with it")


def _aggregator_tagged(row: dict) -> bool:
    """Does this row name a card-review aggregator as its source?"""
    raw = row.get("_sources")
    vals = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    for v in vals:
        if isinstance(v, str) and (_c8._aggregator_token(v)
                                   or (_c8._host_of(v) and _c8._aggregator_of(_c8._host_of(v)))):
            return True
    return False


def _stated_pct(rule: dict):
    """The percentage this rule's own words claim, or None.

    The rule NAME first, then the issuer quote sitting in the same row. Both are
    independent evidence in the sense that matters: neither was computed by us.
    Only answers when the sentence names exactly one percentage — two means a
    tier table, and picking one would be choosing on the issuer's behalf.
    """
    for text in (_txt(rule.get("rule_name")), _txt(rule.get("source_quote"))):
        if not text:
            continue
        got = _pct_claims(text)
        if len(got) == 1:
            return got[0]
    return None


def _issuer_sourced(row: dict, domains) -> bool:
    """Does THIS row cite the issuer's own website?

    c8_provenance's own definition, via c8's own helpers, so "issuer-sourced"
    means one thing across the checker and the fixers.
    """
    if not domains:
        return False
    for _where, raw in _c8._url_candidates(row):
        host = _c8._host_of(raw)
        if host is None or _c8._aggregator_of(host):
            continue
        if _c8._host_matches(host, domains):
            return True
    return False


# --------------------------------------------------------------------------- #
# The one derivation, per row
# --------------------------------------------------------------------------- #
@dataclass
class _Claim:
    """What one rule's own name claims, and what the app currently renders."""
    name: str = ""
    shown: float | None = None      # percent the app shows today (None = "Rate not published")
    pv: float = 0.25                # point value the app used to get there
    n: float | None = None          # "N points ..."
    unit: float | None = None       # "... per Rs M"
    pct: float | None = None        # "... P% ..." when the name states a percentage
    claim: float | None = None      # percent the name claims, via the app's own arithmetic
    pv_conflict: bool = False       # name's own two halves disagree about the point value
    notes: dict = _dc_field(default_factory=dict)


def _claim_of(inner: dict, rule: dict) -> _Claim:
    """Replicate c5_consistency's claim extraction EXACTLY, decision for decision.

    Not "something equivalent". The check and the fix must read the same
    sentence the same way, or this module will confidently repair a row the
    validator never flagged and leave the one it did.
    """
    c = _Claim()
    c.name = _txt(rule.get("rule_name")).strip()
    shown, pv = _engine(inner, rule)
    c.shown, c.pv = shown, pv

    pps = _pps_claims(c.name)
    pcts = _pct_claims(c.name)
    if pps:
        c.n, c.unit = pps[0]
        c.claim = (c.n / c.unit) * pv * 100.0
    elif pcts:
        c.pct = pcts[0]
        c.claim = pcts[0]

    # The sentence gives BOTH "N points per Rs M" and "P%". Those two agree at
    # exactly one point value; if that is not the value the card stores, the
    # point value is what is under suspicion, not the rate. c5 routes that row
    # to L5.NAME_IMPLIES_OTHER_POINT_VALUE — a code this module does not own —
    # so retyping the rate here would "fix" a row whose real defect is one field
    # up, and would do it by baking the wrong point value in.
    n_claim = d_claim = None
    if pps:
        n_claim, d_claim = pps[0]
    elif pcts:
        pts = _pt_count_claims(c.name)
        u = num(rule.get("reward_unit_spend"))
        if pts and u and u > 0:
            n_claim, d_claim = pts[0], u
    if n_claim and pcts:
        implied = (pcts[0] * d_claim) / (n_claim * 100.0) if n_claim > 0 else None
        if implied is not None and not _close(implied, pv, rel=0.10, tol=0.005):
            c.pv_conflict = True
    return c


def _retyped(rule: dict, n: float, unit: float) -> dict:
    """The same row, restated as the accrual its own name describes.

    Returned as a WHOLE ROW rather than three field edits, and that is the point:
    reward_type, reward_rate and reward_unit_spend have to move together or not
    at all. Apply the type without the rate and the app divides 0.15 by 100 and
    shows 0.03%; apply the rate without the type and it shows 1500%. Half of this
    edit is worse than none of it, so it is not offered in halves.

    rule_name is copied through untouched, deliberately and permanently.
    """
    out = dict(rule)
    out["reward_type"] = "points_per_spend"
    out["reward_rate"] = _clean(n)
    out["reward_unit_spend"] = _clean(unit)
    return out


def _cap_note(rule: dict) -> dict:
    """Retyping to points_per_spend also changes the unit the app counts a cap in.

    RewardRule.usedAgainstCap counts RUPEES of cashback for a `cashback_pct`
    rule and POINTS for a `points_per_spend` one. On a points card that flip is
    the correction — caps go in the issuer's unit, and a rupee cap on a points
    card is L4.CAP_IN_RUPEES — but it is a second thing this edit does, so it is
    stated rather than left for someone to discover.
    """
    if rule.get("reward_type") != "cashback_pct":
        return {}
    if num(rule.get("cap_amount")) is None:
        return {}
    if _txt(rule.get("cap_kind")).strip().lower() == "spend":
        return {}   # cap_kind 'spend' already pins the unit to rupees of spend
    return {"cap_unit_flips": "rupees_of_cashback -> points",
            "cap_amount": rule.get("cap_amount")}


# --------------------------------------------------------------------------- #
# Stage A — L4.BASE_FIELD_VS_BASE_RULE
# --------------------------------------------------------------------------- #
def _stage_base_field(entry, inner, cid, edits, refused, locked=frozenset(), from_finding=True):
    """The card states its ordinary earn rate twice and the two disagree.

    In the overwhelming majority they are the SAME NUMBER read under two unit
    conventions: the tile computes base_reward_rate x point value, the detail
    rule is typed `cashback_pct` and renders reward_rate x 100. Stored 0.02 is
    0.50% on one screen and 2.00% on the other, and the ratio between them is
    exactly 1 / point value.

    Only TWO of these 48 base rules have a digit anywhere in their name, so
    there is no independent evidence and no way to prove which surface is right.
    The tiebreak is the down-only rule: move to the lower of the two. If we are
    wrong, we have understated a rate rather than overstated one, and the name
    is untouched so the audit trail survives.
    """
    brr = num(inner.get("base_reward_rate"))
    rp = num(inner.get("rp_value_standard"))
    have_pv = rp is not None and PV_LO <= rp <= PV_HI
    base_pct = (brr or 0.0) * _sane_pv(inner.get("rp_value_standard")) * 100.0

    # The same gate the other two stages in this file already carry, and it was
    # missing only here. `_sane_pv` substitutes the app's invented 25 paise for
    # any absent or implausible point value, so without this the target
    # percentage is 0.25-derived arithmetic on a card that never priced a point:
    # 21 of the 47 rewrites rested on it, quartering yes_bank_uni's "Unlimited 1%
    # Reward points" to 0.25% and axis_bank_primus (which states 5.0, clamped) to
    # the same. It satisfies the down-only rule while being demonstrably false,
    # which is the worst shape a "fix" can have.
    if not have_pv:
        if from_finding:
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", "card.rp_value_standard",
                "This card never says what one of its points is worth, so the only number to "
                "move the rule towards would rest on the 25 paise the app invents — a figure "
                "nobody can defend, and the two screens would still be wrong together.",
                block="card",
                evidence=f"rp_value_standard={inner.get('rp_value_standard')!r}"))
        return

    if brr is None or brr <= 0:
        if from_finding:
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", "card.base_reward_rate",
                "The card's own base rate is zero or missing, so there is no second number to "
                "move the rule towards; finding the real one needs the issuer's earn table.",
                block="card", evidence=trunc(inner.get("base_reward_rate"))))
        return

    if _is_cashback_card(inner):
        if from_finding:
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", "card.reward_currency",
                "This is a cashback card, so 'cashback_pct' is the honest label for its base rule "
                "and the disagreement is somewhere else; only the issuer's page can settle it.",
                block="card", evidence=trunc(inner.get("reward_currency"))))
        return

    for j, r in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(r, dict) or r.get("rule_type") != "base_rate" or j in locked:
            continue
        shown, _pv = _engine(inner, r)
        if shown is None or abs(shown - base_pct) <= AGREE_TOL:
            continue                                   # already agrees — idempotent

        if r.get("reward_type") != "cashback_pct":
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", f"reward_rules[{j}]",
                "This base rule is not stored as a plain percentage, so restating it as points "
                "per Rs 100 would not make the two screens agree; it needs a human.",
                block="reward_rules", index=j, evidence=trunc(r.get("reward_type"))))
            continue
        if r.get("point_value") is not None:
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", f"reward_rules[{j}]",
                "This rule carries its own point value that overrides the card's, so restating "
                "it would still leave the two screens showing different numbers.",
                block="reward_rules", index=j, evidence=trunc(r.get("point_value"))))
            continue
        if base_pct > shown + EPS:
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", f"reward_rules[{j}]",
                f"Making the two agree here would RAISE what the user is shown from "
                f"{_pct(shown)}% to {_pct(base_pct)}%, and a rate we worked out ourselves is "
                "only ever allowed to go down.",
                block="reward_rules", index=j,
                evidence=f"base rule {_pct(shown)}% vs card field {_pct(base_pct)}%"))
            continue

        stated = _stated_pct(r)
        if stated is not None and not _close(stated, base_pct, rel=0.02, tol=AGREE_TOL):
            refused.append(Refusal(
                cid, "L4.BASE_FIELD_VS_BASE_RULE", f"reward_rules[{j}]",
                f"This rule's own words say {stated:g}% and making the two screens agree would "
                f"render {_pct(base_pct)}%, so the app would contradict the sentence printed "
                "beside it; the name is the only independent evidence in the file and a rewrite "
                "that argues with it destroys the audit trail exactly as a rename would.",
                block="reward_rules", index=j,
                evidence=f"'{trunc(r.get('rule_name'), 70)}'"
                         + (f" / quote {trunc(r.get('source_quote'), 60)!r}"
                            if _txt(r.get("source_quote")) else "")))
            continue

        notes = {"renders_before_pct": _clean(shown), "renders_after_pct": _clean(base_pct),
                 "was": {"reward_type": r.get("reward_type"),
                         "reward_rate": r.get("reward_rate"),
                         "reward_unit_spend": r.get("reward_unit_spend")}}
        notes.update(_cap_note(r))
        edits.append(Edit(
            card_id=cid, block="reward_rules", index=j, field=None,
            old_value=r, new_value=_retyped(r, (brr or 0.0) * 100.0, 100.0),
            code="L4.BASE_FIELD_VS_BASE_RULE",
            reason=(f"This card states its ordinary earn rate twice and the two disagree — the "
                    f"card tile shows {_pct(base_pct)}% and the detail screen {_pct(shown)}% — so "
                    f"the rule is restated as the points-per-Rs-100 accrual the card summary "
                    f"already implies, which leaves both screens on the lower {_pct(base_pct)}%."),
            evidence=(f"card.base_reward_rate={inner.get('base_reward_rate')!r} x point value "
                      f"{_sane_pv(inner.get('rp_value_standard')):g} = {_pct(base_pct)}%; "
                      f"rule '{trunc(r.get('rule_name'), 60)}' renders {_pct(shown)}%"),
            confidence=LIKELY, reversible=True, notes=notes))


# --------------------------------------------------------------------------- #
# Stage B — L5.RATE_CONTRADICTS_NAME
# --------------------------------------------------------------------------- #
def _stage_rate_vs_name(entry, inner, cid, edits, refused, locked=frozenset(), from_finding=True):
    """The rule's own name carries the number, and the TYPE is what is wrong.

    Two shapes, both of which leave the stored figure alone and only correct how
    it is labelled:

      (a) the name says "N points per Rs M" and the field already holds N — as
          `multiplier N` (which means N TIMES the base rate, not what the
          sentence says) or as `cashback_pct N/100` (which the app renders as
          N%). This is the 5x bug: "15 EDGE Reward Points per Rs. 100" stored
          `cashback_pct 0.15` renders 15% where the truth is 15 x 0.20 / 100 = 3%.

      (b) the name says "P%" and the stored figure times this card's point value
          comes to exactly P — which means the number was saved as points per
          rupee and typed as a percentage. Two independent witnesses inside the
          same card agree on P, so P is the number.

    In both, rp_value_standard must be stored on the card and plausible. Without
    it the app substitutes 25 paise it invented, and deriving a rate from an
    invented denominator is exactly the fabrication this module exists to stop.
    """
    rp = num(inner.get("rp_value_standard"))
    have_pv = rp is not None and PV_LO <= rp <= PV_HI

    for j, r in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(r, dict) or j in locked:
            continue
        c = _claim_of(inner, r)
        if c.claim is None or c.pv_conflict or c.shown is None:
            continue
        if _close(c.claim, c.shown):
            continue                                   # not defective — idempotent

        rtype = _txt(r.get("reward_type")).strip().lower()
        rate = num(r.get("reward_rate"))
        anchor = ("reward_rules[%d]" % j) if from_finding else (
            "reward_rules[%d] (surfaced by this plan's own point-value change)" % j)

        if rtype not in ("multiplier", "cashback_pct"):
            refused.append(Refusal(
                cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                "The rule is already stored as an accrual, so the disagreement is in the numbers "
                "rather than the label and only the issuer's page can say which one is right.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue

        new_n = new_unit = None
        derivation = ""
        conf = LIKELY

        if c.n is not None and c.unit is not None:
            # ---- (a) the name states the accrual outright -------------------
            if not (UNIT_LO <= c.unit <= UNIT_HI):
                refused.append(Refusal(
                    cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                    f"The spend block read out of the name (Rs {c.unit:g}) is not a plausible "
                    "earn unit, so the sentence was probably parsed wrong and nothing is changed.",
                    block="reward_rules", index=j, evidence=trunc(c.name, 90)))
                continue
            agrees = rate is not None and (
                (rtype == "multiplier" and abs(rate - c.n) < 1e-9) or
                (rtype == "cashback_pct" and abs(rate * 100.0 - c.n) < 1e-6))
            if not agrees:
                refused.append(Refusal(
                    cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                    f"The name says {c.n:g} but the field holds {rate!r}, so the name and the "
                    "number disagree on top of the label being wrong — a third disagreement "
                    "nothing in this file can settle.",
                    block="reward_rules", index=j, evidence=trunc(c.name, 90)))
                continue
            new_n, new_unit = c.n, c.unit
            derivation = (f"name says {c.n:g} per Rs {c.unit:g}; at Rs {rp if rp else '?'} a point "
                          f"that is {_pct((c.n / c.unit) * _sane_pv(rp) * 100.0)}%")
            conf = CERTAIN

        elif c.pct is not None and rtype == "cashback_pct" and rate is not None and rate > 0:
            # ---- (b) the percentage in the name, and a points-per-rupee field
            implied = rate * _sane_pv(rp) * 100.0
            if not have_pv or abs(implied - c.pct) > max(0.01, 0.02 * c.pct):
                refused.append(Refusal(
                    cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                    f"The name says {c.pct:g}% but nothing else on this card reproduces that "
                    "number, so which of the two is wrong cannot be settled from the file.",
                    block="reward_rules", index=j, evidence=trunc(c.name, 90)))
                continue
            new_n, new_unit = rate * 100.0, 100.0
            derivation = (f"name says {c.pct:g}%; stored {rate:g} x Rs {rp:g} a point x 100 = "
                          f"{_pct(implied)}%, so the figure was saved as points per rupee")
            conf = LIKELY
        else:
            # Direction first. "The name cannot be parsed" and "the name says a
            # BIGGER number than we show" are different answers, and reporting
            # the second as the first hides the only-go-DOWN rule doing its job —
            # which is the one refusal a reviewer most needs to see.
            why = ("The rule's name states a rate but not in a form that says how much has to be "
                   "spent to earn it, so there is nothing here to rebuild the number from.")
            if c.claim > c.shown + EPS:
                why = (f"The name claims {_pct(c.claim)}% where the app shows {_pct(c.shown)}%, so "
                       "believing it would RAISE what a user is told, and a rate we worked out "
                       "ourselves is only ever allowed to go down; this one needs the issuer's page.")
            refused.append(Refusal(
                cid, "L5.RATE_CONTRADICTS_NAME", anchor, why,
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue

        if not have_pv:
            refused.append(Refusal(
                cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                "This card does not say what one of its points is worth, and a rate worked out "
                "from the 25 paise the app invents would be a number nobody can defend.",
                block="reward_rules", index=j,
                evidence=f"rp_value_standard={inner.get('rp_value_standard')!r}"))
            continue

        after = (new_n / new_unit) * _sane_pv(rp) * 100.0
        if after > c.shown + EPS:
            refused.append(Refusal(
                cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                f"Believing the name here would RAISE what the user is shown from "
                f"{_pct(c.shown)}% to {_pct(after)}%, and a rate we worked out ourselves is only "
                "ever allowed to go down; this one needs the issuer's page.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue
        if after > CEILING_PCT:
            refused.append(Refusal(
                cid, "L5.RATE_CONTRADICTS_NAME", anchor,
                f"The corrected rate would still be {_pct(after)}%, above the {CEILING_PCT:g}% "
                "ceiling this repair trusts, so the row is left alone for a human.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue

        notes = {"renders_before_pct": _clean(c.shown), "renders_after_pct": _clean(after),
                 "was": {"reward_type": r.get("reward_type"),
                         "reward_rate": r.get("reward_rate"),
                         "reward_unit_spend": r.get("reward_unit_spend")}}
        notes.update(_cap_note(r))
        edits.append(Edit(
            card_id=cid, block="reward_rules", index=j, field=None,
            old_value=r, new_value=_retyped(r, new_n, new_unit),
            code="L5.RATE_CONTRADICTS_NAME",
            reason=(f"The app shows {_pct(c.shown)}% for this rule while the issuer's own wording "
                    f"in the rule's name works out to {_pct(after)}%, so the rule is stored the "
                    f"way its name describes it and the number the user sees comes down to "
                    f"{_pct(after)}%."),
            evidence=f"rule name: {trunc(c.name, 100)} | {derivation}",
            confidence=conf, reversible=True, notes=notes))


# --------------------------------------------------------------------------- #
# Stage C — L5.NAME_STATES_RATE_APP_HAS_NONE
# --------------------------------------------------------------------------- #
def _stage_name_no_rate(entry, inner, cid, edits, refused, locked=frozenset(), from_finding=True):
    """The issuer's sentence is right there and the app shows "Rate not published".

    These rules are stored as `multiplier N` on a card whose base_reward_rate is
    zero, so the app computes N x 0 x point value = nothing and prints no rate at
    all. Restating them as the accrual their own name describes makes them render
    WITHOUT inventing a base rate for the card.

    This is the one place a number rises, and it is not the down-only rule being
    bent: today the app makes no claim at all, and the new claim is read verbatim
    off the issuer's own sentence. It carries a tighter guardrail because of it —
    the card must state its own plausible point value (the app's invented 25
    paise is not evidence), the result may not exceed 15%, and anything above 10%
    is flagged for a human before it ships.
    """
    rp = num(inner.get("rp_value_standard"))
    have_pv = rp is not None and PV_LO <= rp <= PV_HI
    witness, witness_why = _pv_witness(entry, inner)

    for j, r in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(r, dict) or j in locked:
            continue
        c = _claim_of(inner, r)
        if c.shown is not None or c.claim is None or c.pv_conflict:
            continue                                   # renders already — idempotent

        rtype = _txt(r.get("reward_type")).strip().lower()
        rate = num(r.get("reward_rate"))
        anchor = ("reward_rules[%d]" % j) if from_finding else (
            "reward_rules[%d] (surfaced by this plan's own point-value change)" % j)

        if rtype != "multiplier":
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                "This rule shows nothing for a reason other than being a multiple of a zero base "
                "rate, so restating it as an accrual would not make it appear.",
                block="reward_rules", index=j, evidence=trunc(r.get("reward_type"))))
            continue
        if c.n is None or c.unit is None or not (UNIT_LO <= c.unit <= UNIT_HI):
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                "The rule's name does not say how much has to be spent to earn the reward, so "
                "there is no way to turn its words into a rate without guessing.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue
        if rate is None or abs(rate - c.n) >= 1e-9:
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                f"The name says {c.n:g} but the field holds {rate!r}, so the two already disagree "
                "and picking one would be a guess, not a repair.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue
        if not have_pv:
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                "This card never says what one of its points is worth, so the only rate we could "
                "publish would rest on the 25 paise the app invents — that is a fabricated number, "
                "not a fix.",
                block="reward_rules", index=j,
                evidence=f"rp_value_standard={inner.get('rp_value_standard')!r}"))
            continue

        # WHO SAYS the point is worth that. This is the only stage that RAISES a
        # published rate, so the point value under it has to be evidence and not
        # merely a number that is present. It was not checked at all: three of
        # these raises rested on rows the file itself tags _sources:
        # ['cardinsider'] — an aggregator this repo's rules forbid as a source —
        # and the top one, 12.00%, on a Flipkart gift_card row, a merchant
        # voucher the settled rp_value_standard policy excludes outright.
        if witness is None:
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                f"Publishing a rate here means publishing a point value, and this card's is "
                f"{witness_why}. A rate raised from nothing on a card-review site's number, or "
                "on a voucher route the user may not want, is a claim we cannot stand behind.",
                block="reward_rules", index=j,
                evidence=f"rp_value_standard={inner.get('rp_value_standard')!r}"))
            continue

        gate_word = _threshold_phrase(c.name)
        if gate_word and r.get("conditions_json") in (None, "", {}, []):
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                f"The rule's own name gates this rate on a condition ({gate_word!r}) that the "
                "file records nowhere the app can read, so switching it on would tell every "
                "user they earn it unconditionally.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue

        after = (c.n / c.unit) * _sane_pv(rp) * 100.0
        if after > CEILING_PCT:
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                f"Reading the name literally would publish a {_pct(after)}% return, above the "
                f"{CEILING_PCT:g}% ceiling this repair trusts, so nothing is changed.",
                block="reward_rules", index=j, evidence=trunc(c.name, 90)))
            continue
        if after > REVIEW_PCT:
            # This used to be a note in the JSON and nothing else, so an edit
            # nobody had reviewed still shipped on any flag that reached it —
            # and --confidence likely is a one-click workflow_dispatch choice.
            # A flag that does not hold anything back is not a flag.
            refused.append(Refusal(
                cid, "L5.NAME_STATES_RATE_APP_HAS_NONE", anchor,
                f"Reading the name literally would publish {_pct(after)}%, above the "
                f"{REVIEW_PCT:g}% a repair may switch on by itself. It is left for a human to "
                "check against the bank's page and turn on deliberately.",
                block="reward_rules", index=j,
                evidence=f"{trunc(c.name, 70)} -> {_pct(after)}%"))
            continue

        notes = {"renders_before_pct": None, "renders_after_pct": _clean(after),
                 "direction": "up_from_no_claim", "point_value_witness": witness_why,
                 "was": {"reward_type": r.get("reward_type"),
                         "reward_rate": r.get("reward_rate"),
                         "reward_unit_spend": r.get("reward_unit_spend")}}
        notes.update(_cap_note(r))

        tail = ""
        edits.append(Edit(
            card_id=cid, block="reward_rules", index=j, field=None,
            old_value=r, new_value=_retyped(r, c.n, c.unit),
            code="L5.NAME_STATES_RATE_APP_HAS_NONE",
            reason=(f"The app shows no rate at all for this rule because it is stored as a "
                    f"multiple of a base rate of zero, while the rule's own name says "
                    f"{c.n:g} points for every Rs {c.unit:g} — worth {_pct(after)}% at this "
                    f"card's own point value — so it is stored that way instead." + tail),
            evidence=(f"rule name: {trunc(c.name, 100)} | {c.n:g} x Rs {rp:g} / {c.unit:g} = "
                      f"{_pct(after)}%; app shows 'Rate not published'"),
            confidence=LIKELY, reversible=True, notes=notes))


# --------------------------------------------------------------------------- #
# Stages D & E — the card's point value
# --------------------------------------------------------------------------- #
def _stage_point_value(entry, inner, cid, code, edits, refused, done):
    """rp_value_standard, corrected against the card's OWN redemption block.

    This is the denominator of every rate on the card, so it is the highest-blast
    -radius edit in the family and it moves in one direction only. Lowering it
    lowers every percentage the card shows; raising it raises all of them, which
    the down-only rule forbids outright — an under-stated card is ranked too low,
    an over-stated one takes a user's money.
    """
    if (cid, "rp_value_standard") in done:
        return
    rp = num(inner.get("rp_value_standard"))
    best, ch = _best_unconditional(entry)
    out_of_band = rp is not None and not (PV_LO <= rp <= PV_HI)

    if rp is None:
        refused.append(Refusal(
            cid, code, "card.rp_value_standard",
            "This card does not price a point at all, and filling that in needs the issuer's "
            "redemption page rather than anything already in the file.",
            block="card", evidence=trunc(inner.get("rp_value_standard"))))
        return
    if best is None:
        refused.append(Refusal(
            cid, code, "card.rp_value_standard",
            "Nothing in this card's own redemption data prices a point on a no-strings route "
            "(cash back or statement credit), so there is no second witness to correct it with.",
            block="card",
            evidence=trunc([r.get("channel_type") for r in _rows(entry, "redemption_rules")
                            if isinstance(r, dict)], 120)))
        return
    if abs(rp - best) <= AGREE_TOL and not out_of_band:
        return                                          # already agrees — idempotent
    if not (PV_LO <= best <= PV_HI):
        refused.append(Refusal(
            cid, code, "card.rp_value_standard",
            f"The card's own redemption data prices a point at Rs {best:g}, which is not a value "
            "any real rewards point has, so it cannot be used to correct anything.",
            block="card", evidence=f"{ch} channel = {best:g}"))
        return
    if best > rp + EPS:
        refused.append(Refusal(
            cid, code, "card.rp_value_standard",
            f"Trusting the redemption block would RAISE this card's point value from Rs {rp:g} to "
            f"Rs {best:g} and lift every rate on the card with it, and a number we worked out "
            "ourselves is only ever allowed to go down.",
            block="card", evidence=f"rp_value_standard={rp:g} vs {ch} channel {best:g}"))
        return

    quoted, where = _issuer_priced(entry, inner)
    if quoted is not None and abs(quoted - best) > AGREE_TOL:
        refused.append(Refusal(
            cid, code, "card.rp_value_standard",
            f"The bank's own page, quoted in this card's own data, prices a point at "
            f"Rs {quoted:g}. Moving it to Rs {best:g} on our arithmetic would leave the issuer's "
            f"sentence sitting in the file contradicting the number we publish, and an "
            f"issuer-stated figure is corrected by reading the issuer again, not by us.",
            block="card", evidence=where))
        return

    done.add((cid, "rp_value_standard"))
    # The card's value and any rule that copies it move as ONE change. Without
    # this the default gate applied the derived copies and skipped the sources
    # they derive from, and the file ended up pricing the same point two ways on
    # three live cards — the exact outcome the copy edit's own reason forbids.
    group = f"pv:{cid}"
    edits.append(Edit(
        card_id=cid, block="card", index=None, field="rp_value_standard",
        old_value=inner.get("rp_value_standard"), new_value=_clean(best),
        code=code,
        reason=(f"This card says one of its points is worth Rs {rp:g}, but its own {ch} redemption "
                f"route — the best one with no strings attached — pays Rs {best:g} a point, so the "
                f"lower figure the card itself evidences is used and every rate on the card comes "
                f"down with it."),
        evidence=f"redemption_rules {ch} channel point_value_inr = {best:g} "
                 f"(no fee, no minimum)",
        confidence=LIKELY, reversible=True, group_id=group,
        notes={"scales_every_rate_on_card_by": _clean(best / rp) if rp else None,
               "was": inner.get("rp_value_standard")}))


def _stage_stale_rule_point_value(entry, inner, cid, code, pv_old, pv_new, edits, refused,
                                  group=None):
    """A rule that keeps its own copy of the card's point value, after we move it.

    Three rules in the entire file carry a `point_value` of their own, and all
    three are the base rule of an Equitas card holding a verbatim copy of that
    card's rp_value_standard. The app prefers the rule's copy, so correcting the
    card and stopping there leaves the SAME point priced two ways on one card:
    the tile drops, the base rule does not, and the plan hands back a fresh
    L4.BASE_FIELD_VS_BASE_RULE it created itself. Measured, on all three.

    The edit is forced rather than judged: the number being replaced is byte-for
    -byte the number we just corrected one level up. A rule whose copy DIFFERS
    from the card's is a genuine per-rule override — somebody meant that — and
    is left alone.

    Returns {row_index: new_point_value} so the caller can analyse the rest of
    the card through the corrected value instead of the stale one.
    """
    patches = {}
    for j, r in enumerate(_rows(entry, "reward_rules")):
        if not isinstance(r, dict):
            continue
        own = num(r.get("point_value"))
        if own is None:
            continue
        if abs(own - pv_old) > AGREE_TOL:
            refused.append(Refusal(
                cid, code, f"reward_rules[{j}]",
                f"This rule prices a point at Rs {own:g} of its own accord rather than copying the "
                f"card's Rs {pv_old:g}, so somebody meant it and it is left exactly as it is.",
                block="reward_rules", index=j, evidence=f"point_value={r.get('point_value')!r}"))
            continue
        patches[j] = _clean(pv_new)
        edits.append(Edit(
            card_id=cid, block="reward_rules", index=j, field="point_value",
            old_value=r.get("point_value"), new_value=_clean(pv_new), code=code,
            reason=(f"This rule keeps its own copy of what a point is worth, and it is the same "
                    f"Rs {pv_old:g} this fix has just corrected on the card itself, so the copy "
                    f"moves with it to Rs {pv_new:g} — otherwise the same point would be priced "
                    f"two different ways on one card."),
            evidence=(f"reward_rules[{j}].point_value={r.get('point_value')!r} is a verbatim copy "
                      f"of card.rp_value_standard={pv_old:g}"),
            # LIKELY, not CERTAIN, and grouped with the card-level edit it
            # follows. An edit may never be surer than the edit it derives from:
            # this copy is only right if the card's value moves too, and marking
            # it 'certain' let the default gate apply it alone.
            confidence=LIKELY, reversible=True, group_id=group,
            notes={"follows": "card.rp_value_standard"}))
    return patches


def _patched(entry: dict, patches: dict) -> dict:
    """A read-only view of the card with this plan's own row patches applied.

    Nothing in the file is touched. This exists so the stages below derive rates
    through the point value this plan is about to write, not the one it already
    knows is wrong.
    """
    if not patches:
        return entry
    rows = list(_rows(entry, "reward_rules"))
    for j, pv in patches.items():
        if 0 <= j < len(rows) and isinstance(rows[j], dict):
            r = dict(rows[j])
            r["point_value"] = pv
            rows[j] = r
    v = dict(entry)
    v["reward_rules"] = rows
    return v


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
def _analyse(ctx: Ctx, findings):
    """One card at a time, point value first, then the rules that divide by it.

    ORDER IS LOAD-BEARING, and it was measured rather than assumed. Correcting
    rp_value_standard rescales every points-based rate on the card but leaves
    every rule typed `cashback_pct` exactly where it was — so a plan that lowers
    the point value and stops there PULLS THE CARD TILE AWAY FROM THE CARD'S OWN
    BASE RULE and hands the reviewer five brand-new L4.BASE_FIELD_VS_BASE_RULE
    errors it created itself. That happened on the first run of this module, on
    the five cards whose point value it corrects.

    So two things follow. The rate stages read a POST-EDIT VIEW of the card, not
    the file's stored value, or they would derive every rate through a
    denominator this same plan is about to change. And a card whose point value
    this plan touches goes through the rule stages whether or not the validator
    flagged it — not scope creep, but the plan cleaning up after itself. A fix
    that leaves the file worse in a way it can see and repair is not a fix.
    """
    edits: list[Edit] = []
    refused: list[Refusal] = []
    want = _cards_by_code(findings)
    done: set = set()

    for _i, entry, inner, cid in ctx.entries():
        if not cid:
            continue
        # Point value first: it is the denominator every rate below is derived
        # through, so correcting a rule against a value we are about to change
        # would bake in the number we already know is wrong.
        before = len(edits)
        for code in ("L4.POINT_VALUE_OUT_OF_BAND", "L4.POINT_VALUE_VS_REDEMPTION"):
            if cid in want[code]:
                _stage_point_value(entry, inner, cid, code, edits, refused, done)

        view, ent, knock_on, locked = inner, entry, False, set()
        for e in edits[before:]:
            if e.field == "rp_value_standard":
                pv_old = num(inner.get("rp_value_standard"))
                view = dict(inner)
                view["rp_value_standard"] = e.new_value
                knock_on = True
                patches = _stage_stale_rule_point_value(
                    entry, inner, cid, e.code, pv_old, e.new_value, edits, refused,
                    group=e.group_id)
                locked |= set(patches)
                ent = _patched(entry, patches)

        if knock_on or cid in want["L4.BASE_FIELD_VS_BASE_RULE"]:
            _stage_base_field(ent, view, cid, edits, refused, locked,
                              from_finding=cid in want["L4.BASE_FIELD_VS_BASE_RULE"])
        if knock_on or cid in want["L5.RATE_CONTRADICTS_NAME"]:
            _stage_rate_vs_name(ent, view, cid, edits, refused, locked,
                                from_finding=cid in want["L5.RATE_CONTRADICTS_NAME"])
        if knock_on or cid in want["L5.NAME_STATES_RATE_APP_HAS_NONE"]:
            _stage_name_no_rate(ent, view, cid, edits, refused, locked,
                                from_finding=cid in want["L5.NAME_STATES_RATE_APP_HAS_NONE"])

    return edits, refused


def plan(ctx: Ctx, findings) -> list[Edit]:
    """PURE. Reads ctx and the findings; returns proposed edits; writes nothing.

    Note what is NOT read: finding['index']. L5's findings are aggregated one per
    (card, defect class), so a single finding can stand for six defective rules
    and carry no index at all. The finding names the card; the defective rows are
    re-derived here from the data, through the same parser the check used.
    """
    edits, _ = _analyse(ctx, findings)

    # Structural, not a promise. Renaming a rule is forbidden twice over: the
    # name is the only independent evidence in the file, and the app keys every
    # user's saved cap progress on that exact string, so changing it silently
    # resets real people's progress towards a cap they have nearly earned.
    for e in edits:
        if e.field == "rule_name":
            raise AssertionError("f1_units proposed an edit to rule_name — forbidden")
        if e.shape == "row" and isinstance(e.new_value, dict) and isinstance(e.old_value, dict):
            if e.new_value.get("rule_name") != e.old_value.get("rule_name"):
                raise AssertionError(
                    f"f1_units changed rule_name on {e.anchor()} — forbidden")
        e.family = FAMILY
    return edits


def refusals(ctx: Ctx, findings) -> list[Refusal]:
    """PURE. The other half of the same analysis: every defect this module looked
    at and deliberately did not fix, each with the sentence explaining why."""
    _, refused = _analyse(ctx, findings)
    return refused
