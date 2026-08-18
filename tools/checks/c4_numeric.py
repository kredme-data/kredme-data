"""L4 — numeric plausibility and units.

Every other layer asks "is this field the right SHAPE?". This one asks the only
question a user can feel: "is this number the right SIZE, in the right UNIT?"

A rate of 40% is a perfectly well-formed float. It is also a unit error: the
issuer said "10% value back" and somebody stored 0.4 where 0.1 belonged. A cap
of 40,000 is a perfectly well-formed int. It is also a unit error: 40,000
NeuCoins written into a field the app reads as rupees. Nothing upstream of this
layer can see either one.

Everything here replicates the APP's own display maths (lib/shared/models/
credit_card.dart + lib/core/engine/recommendation_engine.dart), because a number
is only wrong if the app RENDERS it wrong:

    sanePointValue(v)  = 0.25 if v is null / <= 0 / > 1.5 else v
    baseReward         = base_reward_rate * sanePointValue(rp_value_standard) * 100
    rateForRule        = per reward_type, see _rule_pct below

Two things this layer deliberately does NOT do:

  * It does not grandfather. There is no baseline file, no allow-list, no
    "known issue" suppression. The runner owns policy; this module owns truth.
  * It does not read issuer websites. It proves the file disagrees with itself
    or with physical plausibility. A number that is internally consistent and
    still wrong is out of reach from here, and always will be.
"""
from __future__ import annotations

import re

from .base import Ctx, Finding, ERROR, WARN, INFO, num, trunc, iso_ok, card_base_pct, reach_scaled

LAYER = "L4 numeric plausibility & units"

# --------------------------------------------------------------------------- #
# Thresholds. Every one of these is a judgement call, so each carries its
# reason — a threshold nobody can defend gets edited until it reports nothing.
# --------------------------------------------------------------------------- #

# 40, not 30: HDFC SmartBuy 10X genuinely renders ~33%. Above 40 there is no
# real Indian credit-card offer, only a unit error.
HARD_CEILING_PCT = 40.0
# Above 10% is possible but rare and almost always a capped, conditional promo.
SOFT_CEILING_PCT = 10.0
# Below 0.1% no issuer would print the offer at all.
FLOOR_PCT = 0.1

# The app's own point-value handling (CreditCardData.sanePointValue).
APP_PV_DEFAULT = 0.25
APP_PV_CLAMP_MAX = 1.5
# The band this layer calls plausible for an Indian rewards currency.
PV_BAND_LO, PV_BAND_HI = 0.05, 2.0

# Reading a rupee-denominated cap back as "monthly spend needed to exhaust it".
CAP_SPEND_LO, CAP_SPEND_HI = 5_000.0, 300_000.0

# How far one point's value has to be from Rs 1.00 before reading a cap in the
# wrong unit changes anything a user can see.
#
# A cap C on a cashback_pct rule binds when the app has counted C rupees of
# reward, i.e. at spend = C / rate. Read correctly as points it binds at
# spend = C * pv / rate. The two differ by exactly the factor pv — so at
# pv = 1.00 (one CashPoint = one rupee, one NeuCoin = one rupee) they bind at
# the SAME spend and no user ever sees a different number.
CAP_UNIT_MATERIALITY = 0.005

ANNUAL_FEE_ABSURD = 500_000.0          # Amex Centurion is Rs 3,00,000. Nothing is 5 lakh.
FEE_WAIVER_ABSURD = 10_000_000.0       # Rs 1 crore of annual spend to waive a fee.
FOREX_ABSURD_PCT = 5.0                 # No Indian issuer charges above ~3.5%.
MIN_REDEMPTION_ABSURD = 50_000.0
EXPIRY_MONTHS_ABSURD = 120             # 10 years
EXPIRY_MONTHS_SUSPECT = 6              # below this, suspect years-stored-as-months
FUEL_CAP_SUSPECT = 2_000.0             # real fuel-surcharge caps are Rs 100-1,000/month
MILESTONE_VOUCHER_MIN_INR = 100.0      # below this a "voucher" value is a COUNT, not rupees

_CASH_TOKENS = ("cashback", "cash_back", "cash", "inr")

# Words that prove a rule's own sentence is talking about a points currency.
_POINT_WORDS = (
    "reward point", "reward points", "points", "point", " rp", "rps",
    "neucoin", "cashpoint", "edge reward", "edge point", "bluchip",
    "air mile", "miles", "mile", "coins", "fuel point", " fp", "6e reward",
    "saving point", "intermile", "membership reward", "supercoin",
)


# --------------------------------------------------------------------------- #
# Numeric helpers — every one of these must survive None / str / dict / bool
# --------------------------------------------------------------------------- #
def _n(v):
    """A finite JSON number, or None. A numeric STRING is None on purpose: the
    app's Dart parser drops it, so 'not a number' is the honest answer."""
    f = num(v)
    if f is None or f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _sane_pv(v):
    """CreditCardData.sanePointValue — out-of-range point values collapse to 0.25."""
    f = _n(v)
    if f is None or f <= 0 or f > APP_PV_CLAMP_MAX:
        return APP_PV_DEFAULT
    return f


def _low(v) -> str:
    return v.lower() if isinstance(v, str) else ""


def _is_cashback_card(inner: dict) -> bool:
    """CreditCardData.isCashbackCard — prefix test on reward_currency, plus tier."""
    cur = _low(inner.get("reward_currency"))
    return cur.startswith("cashback") or cur in _CASH_TOKENS or _low(inner.get("card_tier")) == "cashback"


def _app_base_pct(inner: dict) -> float:
    """The base earn % the APP renders for this card.

    Deliberately not base.card_base_pct: the shared helper skips the app's
    sanePointValue clamp and reads a null point value as 0.0, where the app
    invents 0.25. Where the two disagree the card has a null or out-of-band
    rp_value_standard, which this layer reports under its own code.
    """
    brr = _n(inner.get("base_reward_rate")) or 0.0
    return brr * _sane_pv(inner.get("rp_value_standard")) * 100.0


def _rule_pv(rule: dict, inner: dict) -> float:
    pvr = _n(rule.get("point_value"))
    return _sane_pv(pvr if pvr is not None else inner.get("rp_value_standard"))


def _rule_pct(rule: dict, inner: dict, base_pct: float) -> float:
    """The % the app renders for one reward rule (credit_card.dart rateForRule)."""
    rtype = rule.get("reward_type")
    rate = _n(rule.get("reward_rate")) or 0.0
    pv = _rule_pv(rule, inner)
    if rtype == "cashback_pct":
        return rate * 100.0
    if rtype == "multiplier":
        brr = _n(inner.get("base_reward_rate")) or 0.0
        return rate * brr * pv * 100.0
    if rtype == "points_per_spend":
        unit = _n(rule.get("reward_unit_spend")) or 0.0
        if unit <= 0:
            return base_pct                       # the app's divide-by-zero guard
        return (rate / unit) * pv * 100.0
    return base_pct                               # unrecognised type renders the base rate


def _engine_cap_unit(rule: dict) -> str:
    """The unit the APP subtracts from cap_amount (RewardRule.usedAgainstCap)."""
    if rule.get("cap_kind") == "spend":
        return "inr"
    rtype = rule.get("reward_type")
    if rtype in ("points_per_spend", "multiplier"):
        return "points"
    return "inr"                                  # cashback_pct and every unknown type


def _names_points(text: str) -> bool:
    t = _low(text)
    return any(w in t for w in _POINT_WORDS)


# "capped at Rs 50,000 per billing cycle" / "up to ₹200 per month" — a cap word
# followed closely by an explicit RUPEE figure. When that figure is the number
# actually stored in cap_amount, the sentence is telling us the cap really is in
# rupees, whatever currency the rest of the sentence names. Used only to STOP a
# false accusation, never to raise one.
_RUPEE_CAP = re.compile(
    r"(?:cap(?:ped)?|maximum|max\.?|up\s*to|limit)[^.;]{0,25}?"
    r"(?:\u20b9|rs\.?\s*|inr\s*)\s*([\d,]+)", re.I)


def _prose_states_rupee_cap(text, cap: float) -> bool:
    if not isinstance(text, str):
        return False
    for m in _RUPEE_CAP.finditer(text):
        try:
            v = float(m.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if abs(v - cap) < 0.51:
            return True
    return False


def _label(rule: dict, idx: int) -> str:
    nm = rule.get("rule_name")
    return trunc(nm, 60) if isinstance(nm, str) and nm.strip() else f"<rule #{idx}>"


def _rows(entry: dict, block: str) -> list:
    v = entry.get(block)
    return v if isinstance(v, list) else []


def _pct(x: float) -> str:
    return f"{x:.2f}%"


def _inr(x: float) -> str:
    return f"Rs {x:,.0f}"


# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []

    # Whole-file counts a per-card message can quote, so a founder reading one
    # line knows whether they are looking at an outlier or at a systemic defect.
    zero_base_ids: list[str] = []
    for _i, _e, _inner, _cid in ctx.entries():
        try:
            if (_n(_inner.get("base_reward_rate")) or 0.0) <= 0:
                zero_base_ids.append(_cid or f"<card #{_i}>")
        except Exception:
            continue
    n_zero_base = len(zero_base_ids)

    for i, entry, inner, cid in ctx.entries():
        card = cid or f"<card #{i}>"
        try:
            out.extend(_check_card(ctx, entry, inner, card, n_zero_base))
        except Exception as exc:                  # a bad card must never kill the layer
            out.append(Finding(
                severity=WARN, code="L4.CHECK_ABORTED", card_id=card, block="card",
                message="This card's numbers could not be checked — something in it is shaped "
                        "so unusually that the numeric checks stopped part-way.",
                evidence=trunc(f"{type(exc).__name__}: {exc}"),
                impact="Any wrong rate, cap or fee on this card is currently unmeasured.",
                fix="Open this card in seed/cards.json and look for a value of an unexpected "
                    "type (a list where a number belongs, or similar)."))
    return out


# --------------------------------------------------------------------------- #
def _check_card(ctx: Ctx, entry: dict, inner: dict, card: str, n_zero_base: int) -> list[Finding]:
    f: list[Finding] = []
    base_pct = _app_base_pct(inner)
    brr = _n(inner.get("base_reward_rate"))
    rp_raw = inner.get("rp_value_standard")
    rp = _n(rp_raw)
    is_cash = _is_cashback_card(inner)
    rules = [(j, r) for j, r in enumerate(_rows(entry, "reward_rules")) if isinstance(r, dict)]

    f += _check_point_value(entry, inner, card, rp_raw, rp, brr, rules, is_cash,
                            ctx.block_reaches_app("redemption_rules"))
    f += _check_base_rate(inner, card, base_pct, brr, rules, n_zero_base)
    f += _check_rule_rates(inner, card, base_pct, brr, rules)
    f += _check_caps(inner, card, rules, is_cash)
    f += _check_card_scalars(inner, card)
    f += _check_fuel(entry, card)
    f += _check_milestones(entry, inner, card)
    return f


# ------------------------------------------------------------- point values --
def _check_point_value(entry, inner, card, rp_raw, rp, brr, rules, is_cash,
                       reaches=None) -> list[Finding]:
    f: list[Finding] = []

    # What the card's own redemption block says a point is worth. Settled KredMe
    # policy (17-Aug): rp_value_standard is the best UNCONDITIONAL channel —
    # never a travel portal, airmiles transfer or merchant voucher. So only
    # cashback / statement-credit channels are compared here. Anything else
    # being higher is the policy working, not a defect.
    best_uncond, best_ch, null_pv_rows = None, None, 0
    # The SAME plausibility band that is a hard ERROR on card.rp_value_standard
    # was never applied to point_value_inr on a redemption row — 641 numeric rows
    # across the file, of which the only ones ever compared to anything were the
    # cashback / statement_credit ones (POINT_VALUE_VS_REDEMPTION filters to
    # those before it looks). So the 497 rows on merchandise / other /
    # partner_transfer / travel / gift_card / voucher_catalog channels could
    # claim Rs 300 a point, or a negative one, and all nine layers stayed silent.
    # A point is not worth Rs 300 on ANY channel. The channel filter is right for
    # the comparison — settled policy is that rp_value_standard tracks the best
    # unconditional route — but it must not gate the band.
    band_bad, band_high = [], []
    for j, r in enumerate(_rows(entry, "redemption_rules")):
        if not isinstance(r, dict):
            continue
        raw = r.get("point_value_inr")
        v = _n(raw)
        if v is None:
            if raw is not None:
                band_bad.append((j, _low(r.get("channel_type")) or "?", trunc(raw, 40),
                                 "not a number the app can read"))
            else:
                null_pv_rows += 1
            continue
        ch = _low(r.get("channel_type"))
        if v < 0:
            band_bad.append((j, ch or "?", f"{v:g}", "negative"))
        elif v == 0:
            band_bad.append((j, ch or "?", f"{v:g}", "zero"))
        elif v > PV_BAND_HI:
            band_high.append((j, ch or "?", v))
        if ch in ("cashback", "statement_credit") and (best_uncond is None or v > best_uncond):
            best_uncond, best_ch = v, ch

    if band_bad:
        f.append(reach_scaled(
            reaches, ERROR, WARN,
            code="L4.REDEMPTION_POINT_VALUE_IMPOSSIBLE", card_id=card,
            block="redemption_rules", index=band_bad[0][0], field="point_value_inr",
            message=f"{len(band_bad)} redemption row(s) on this card price a point at a value "
                    "no point can have.",
            evidence=trunc("; ".join(f"#{j} ({ch}) = {v} — {why}"
                                     for j, ch, v, why in band_bad[:4]), 300),
            live_impact="The Redemption tab ranks the routes by this number, so a route is "
                        "presented as worthless or as better than everything else on evidence "
                        "that is not arithmetic.",
            dead_impact="Nobody sees this today — the app never reads the redemption_rules "
                        "block. It is still a wrong number sitting in the file, and it is the "
                        "number that would drive the screen the day the block is wired up.",
            fix="Store the real rupee value of one point on that route, or remove the row."))
    if band_high:
        f.append(reach_scaled(
            reaches, ERROR, WARN,
            code="L4.REDEMPTION_POINT_VALUE_OUT_OF_BAND", card_id=card,
            block="redemption_rules", index=band_high[0][0], field="point_value_inr",
            message=f"{len(band_high)} redemption row(s) on this card say one point is worth more "
                    f"than Rs {PV_BAND_HI:g}. A real Indian rewards point is worth between "
                    f"Rs {PV_BAND_LO:g} and Rs {PV_BAND_HI:g} on any channel.",
            evidence=trunc("; ".join(f"#{j} ({ch}) = Rs {v:g}" for j, ch, v in band_high[:4]), 300),
            live_impact="This route is shown to the user as worth many times what it pays, and it "
                        "sorts to the top of the Redemption tab ahead of the routes that are "
                        "actually better.",
            dead_impact="Nobody sees this today — the app never reads the redemption_rules block. "
                        "It is still a wrong number, and it is usually a unit error: a rupee "
                        "figure or a points-per-voucher COUNT stored where a per-point value "
                        "belongs.",
            fix="Re-read the issuer's redemption page. If the number is 'Rs 300 gets you a "
                "voucher', that is a redemption threshold, not a point value."))

    pv_matters = (brr or 0) > 0 or any(
        r.get("reward_type") in ("points_per_spend", "multiplier") for _j, r in rules)

    if rp is None and not isinstance(rp_raw, (int, float)):
        hint = (f" Its own redemption data says a point is worth Rs {best_uncond:g} "
                f"({best_ch}), so that is the number to use."
                if best_uncond is not None else "")
        f.append(Finding(
            severity=ERROR if pv_matters else WARN,
            code="L4.POINT_VALUE_MISSING", card_id=card, block="card", field="rp_value_standard",
            message="This card does not say what one of its reward points is worth, so the app "
                    "quietly invents 25 paise per point and shows every rate computed from that "
                    "made-up number." + hint,
            evidence=trunc(rp_raw),
            impact="Every percentage and rupee figure this card shows a user is built on a guess. "
                   "For a miles or Membership Rewards card 25 paise is usually far too low, so the "
                   "card is ranked below cards that are genuinely worse.",
            fix="Set rp_value_standard to the value of the best redemption route a user can take "
                "with no conditions attached — cash back or statement credit, never a travel "
                "portal or an airline transfer."))
    elif rp is not None and (rp <= 0 or rp < PV_BAND_LO or rp > PV_BAND_HI):
        clamped = rp <= 0 or rp > APP_PV_CLAMP_MAX
        f.append(Finding(
            severity=ERROR, code="L4.POINT_VALUE_OUT_OF_BAND", card_id=card, block="card",
            field="rp_value_standard",
            message=f"This card says one reward point is worth Rs {rp:g}. A real Indian rewards "
                    f"point is worth between Rs {PV_BAND_LO:g} and Rs {PV_BAND_HI:g}"
                    + (", and the app throws this value away and substitutes 25 paise."
                       if clamped else "."),
            evidence=trunc(rp_raw),
            impact=("The user is shown rates computed from 25 paise per point, which is nothing "
                    "to do with the number stored here — the file and the screen disagree."
                    if clamped else
                    "Every rate on this card is scaled by this number, so all of them are wrong "
                    "by the same factor."),
            fix="Re-read the issuer's redemption page and store the real rupee value of one point."))

    if best_uncond is not None and rp is not None and abs(rp - best_uncond) > 0.005:
        over = rp > best_uncond
        f.append(Finding(
            severity=ERROR if over else WARN,
            code="L4.POINT_VALUE_VS_REDEMPTION", card_id=card, block="card",
            field="rp_value_standard",
            message=f"This card values a point at Rs {rp:g}, but its own redemption data says the "
                    f"best no-strings-attached route ({best_ch}) pays Rs {best_uncond:g} a point. "
                    "The two screens show the user different arithmetic for the same card.",
            evidence=f"rp_value_standard={rp:g} vs {best_ch} channel {best_uncond:g}",
            impact=("Rates are OVER-stated: the card earns less than the app promises and is "
                    "ranked above cards that would actually pay the user more."
                    if over else
                    "Rates are UNDER-stated: the card is ranked below cards that pay less."),
            fix="Only cash-back and statement-credit channels are compared here, because settled "
                "policy is that rp_value_standard tracks the best UNCONDITIONAL route. Make the "
                "two agree — usually by correcting rp_value_standard."))

    if null_pv_rows:
        # Severity follows REACH. This used to be a flat WARN whose impact said
        # "On the Redemption tab the user is told these routes are worth zero
        # rupees per point" — which cannot happen to anybody, because the app
        # never receives this block at all (it reads `redemption_channels`; we
        # write `redemption_rules`). 128 of 128 of these were describing a screen
        # no user can reach: a 100% false-positive rate on the stated impact.
        #
        # The check is NOT deleted and its scope is unchanged — it still finds
        # every unpriced redemption row. What changed is the claim about who is
        # harmed, and therefore the severity. The day the key mismatch is closed
        # (L6.REDEMPTION_BLOCK_NEVER_READ) these go back to WARN on their own,
        # with no edit here.
        f.append(reach_scaled(
            reaches, WARN, INFO,
            code="L4.REDEMPTION_VALUE_NULL", card_id=card, block="redemption_rules",
            message=f"{null_pv_rows} of this card's redemption options do not say what a point is "
                    "worth, and the app turns a missing value into Rs 0.00 rather than 'unknown'.",
            evidence=f"{null_pv_rows} row(s) with point_value_inr = null",
            live_impact="On the Redemption tab the user is told these routes are worth zero rupees "
                        "per point, which reads as 'never redeem this way' even when it is the "
                        "best option.",
            dead_impact="Nobody sees this today: the app never reads the redemption_rules block, "
                        "so no user is shown a zero here and no user is shown anything else "
                        "either. It becomes a real WARN the moment that key mismatch is closed.",
            fix="Fill point_value_inr for these channels, or drop the channels we cannot price."))
    return f


# ------------------------------------------------------------- base earn rate -
def _check_base_rate(inner, card, base_pct, brr, rules, n_zero_base) -> list[Finding]:
    f: list[Finding] = []
    pv = _sane_pv(inner.get("rp_value_standard"))

    if (brr or 0.0) <= 0:
        zeroed = sum(1 for _j, r in rules
                     if r.get("reward_type") not in ("cashback_pct", "points_per_spend"))
        f.append(Finding(
            severity=ERROR, code="L4.ZERO_BASE_RATE", card_id=card, block="card",
            field="base_reward_rate",
            message=f"This card claims it earns nothing at all on ordinary spending — it renders a "
                    f"flat 0.00% base rate. It is one of {n_zero_base} cards in the file doing that."
                    + (f" It also drags {zeroed} of the card's own bonus rules to 0.00%, because "
                       "those rules are stored as a multiple of the base rate."
                       if zeroed else ""),
            evidence=trunc(inner.get("base_reward_rate")),
            impact="The card sorts to the BOTTOM of the pick screen for every merchant it has no "
                   "specific rule for, so a user holding it is told to swipe something else. With "
                   "only three cards shown, it usually disappears from the screen entirely.",
            fix="Look up the card's ordinary earn rate and store it as POINTS PER RUPEE (a card "
                "earning 2 points per Rs 100 is 0.02). If the card genuinely earns nothing on "
                "base spend, say so in the rule text instead of leaving the field at zero."))
    elif 0 < base_pct < FLOOR_PCT:
        f.append(Finding(
            severity=WARN, code="L4.BASE_RATE_BELOW_FLOOR", card_id=card, block="card",
            field="base_reward_rate",
            message=f"This card's ordinary earn rate renders as {base_pct:.4f}% — below the "
                    f"{FLOOR_PCT}% floor. No issuer advertises a rate that small; it is almost "
                    "always a decimal point in the wrong place.",
            evidence=f"base_reward_rate={brr!r} x point value {pv:g} = {base_pct:.4f}%",
            impact="The card is ranked as if it earns almost nothing, so it is never recommended.",
            fix="Check whether base_reward_rate was stored as a percentage instead of points per "
                "rupee, and whether rp_value_standard is right."))
    elif base_pct > HARD_CEILING_PCT:
        f.append(Finding(
            severity=ERROR, code="L4.BASE_RATE_ABOVE_HARD_CEILING", card_id=card, block="card",
            field="base_reward_rate",
            message=f"This card claims a {base_pct:.2f}% return on ALL ordinary spending. Nothing "
                    f"above {HARD_CEILING_PCT:.0f}% exists in the Indian market; this is a unit error.",
            evidence=f"base_reward_rate={brr!r} x point value {pv:g}",
            impact="The card wins every recommendation on the pick screen and pays out a fraction "
                   "of what the app promised.",
            fix="Re-check base_reward_rate (points per rupee) and rp_value_standard (rupees per point)."))

    # The card's base_reward_rate FIELD and its own base_rate RULE are two
    # different surfaces showing the same user the same number. They must agree.
    for j, r in rules:
        try:
            if r.get("rule_type") != "base_rate":
                continue
            p = _rule_pct(r, inner, base_pct)
            if abs(p - base_pct) <= 0.005:
                continue
            ratio = (p / base_pct) if base_pct > 0 else None
            unit_bug = ratio is not None and pv > 0 and abs(ratio - 1.0 / pv) < 0.02
            diag = (" The rule's number is exactly 1 divided by the point value bigger than the "
                    "field's — the classic sign that the rule stores a DISPLAY PERCENTAGE while "
                    "the field stores POINTS PER RUPEE."
                    if unit_bug else "")
            f.append(Finding(
                severity=ERROR, code="L4.BASE_FIELD_VS_BASE_RULE", card_id=card,
                block="reward_rules", index=j, field="reward_rate",
                message=f"This card states its ordinary earn rate twice and the two disagree: the "
                        f"card summary renders {_pct(base_pct)} while its own base rule renders "
                        f"{_pct(p)}." + diag,
                evidence=f"rule '{_label(r, j)}' -> {p:.3f}% vs base_reward_rate -> {base_pct:.3f}%"
                         + (f" (ratio {ratio:.2f}x)" if ratio else ""),
                impact="The same user sees one number on the card tile and a different one on the "
                       "card detail screen. Whichever they believe, one of them is a lie.",
                fix="Correct the number, never the rule's NAME. Renaming the rule to match the "
                    "number is circular — the name is the only independent evidence of what the "
                    "issuer actually said."))
        except Exception:
            continue
    return f


# ------------------------------------------------------------- per-rule rates -
def _check_rule_rates(inner, card, base_pct, brr, rules) -> list[Finding]:
    f: list[Finding] = []
    hard, soft, low, dead, bad_type, pv_band, unit_bad, unit_leak, big_cash = [], [], [], [], [], [], [], [], []
    base_is_zero = (brr or 0.0) <= 0

    for j, r in rules:
        try:
            rtype = r.get("reward_type")
            rate_raw = r.get("reward_rate")
            rate = _n(rate_raw)
            unit_raw = r.get("reward_unit_spend")
            unit = _n(unit_raw)

            if rate is None:
                bad_type.append((j, _label(r, j), trunc(rate_raw, 40)))
                continue

            if rtype == "points_per_spend" and (unit is None or unit <= 0):
                unit_bad.append((j, _label(r, j), trunc(unit_raw, 40)))
            if rtype != "points_per_spend" and unit_raw is not None:
                unit_leak.append((j, _label(r, j), trunc(unit_raw, 40)))
            if rtype == "cashback_pct" and rate > 1.0:
                big_cash.append((j, _label(r, j), rate))

            pvr = r.get("point_value")
            if pvr is not None:
                pvn = _n(pvr)
                if pvn is None or not (PV_BAND_LO <= pvn <= PV_BAND_HI):
                    pv_band.append((j, _label(r, j), trunc(pvr, 40)))

            p = _rule_pct(r, inner, base_pct)
            if p > HARD_CEILING_PCT:
                hard.append((j, _label(r, j), p, rtype, rate))
            elif p > SOFT_CEILING_PCT:
                soft.append((j, _label(r, j), p, rtype, rate))
            elif 0 < p < FLOOR_PCT:
                low.append((j, _label(r, j), p, rtype, rate))
            elif p == 0 and not (base_is_zero and rtype not in ("cashback_pct", "points_per_spend")):
                # A rule that renders 0.00% for a reason OTHER than the card's
                # zero base rate — that case is already reported once, per card,
                # under L4.ZERO_BASE_RATE.
                dead.append((j, _label(r, j), rtype, rate))
        except Exception:
            continue

    def _lines(items, n=4):
        return trunc("; ".join(f"#{j} {nm} -> {p:.2f}%" for j, nm, p, *_ in items[:n])
                     + (f" (+{len(items) - n} more)" if len(items) > n else ""), 300)

    if hard:
        f.append(Finding(
            severity=ERROR, code="L4.RATE_ABOVE_HARD_CEILING", card_id=card, block="reward_rules",
            index=hard[0][0],
            message=f"{len(hard)} rule(s) on this card render above {HARD_CEILING_PCT:.0f}% back. "
                    "No Indian credit card pays that; a number this large is a unit error, not an offer.",
            evidence=_lines(hard),
            impact="These rules win the pick screen for their merchant or category and promise a "
                   "user several times what the card will actually pay.",
            fix="Read the rule's own name. 'N points per Rs M' stored as a plain percentage, or an "
                "'NX points' multiple stored as an absolute rate, produce exactly this."))
    if soft:
        f.append(Finding(
            severity=WARN, code="L4.RATE_ABOVE_CEILING", card_id=card, block="reward_rules",
            index=soft[0][0],
            message=f"{len(soft)} rule(s) on this card render above {SOFT_CEILING_PCT:.0f}% back. "
                    "That is possible for a capped promotion, and it is also what a unit error "
                    "looks like, so each one needs a human to confirm it against the issuer.",
            evidence=_lines(soft),
            impact="If any of these is wrong the user is told to swipe the wrong card for that "
                   "merchant, and is short-changed on every purchase they make on that advice.",
            fix="Confirm against the issuer's own page. Rates above 10% almost always carry a "
                "monthly cap; if there is one it must be in cap_amount with a cap_period."))
    if low:
        f.append(Finding(
            severity=WARN, code="L4.RATE_BELOW_FLOOR", card_id=card, block="reward_rules",
            index=low[0][0],
            message=f"{len(low)} rule(s) on this card render below {FLOOR_PCT}% back — too small "
                    "for any issuer to have advertised.",
            evidence=_lines(low),
            impact="These rules never win a recommendation, so whatever the card really earns here "
                   "is invisible to the user.",
            fix="Look for a decimal point in the wrong place, or a point value stored 10x too small."))
    if dead:
        f.append(Finding(
            severity=WARN, code="L4.RULE_EARNS_NOTHING", card_id=card, block="reward_rules",
            index=dead[0][0],
            message=f"{len(dead)} rule(s) on this card render exactly 0.00% back — the rule exists, "
                    "and says the user earns nothing.",
            evidence=trunc("; ".join(f"#{j} {nm} ({t}, rate={rt!r})" for j, nm, t, rt in dead[:4]), 300),
            impact="The rule is shown on the card detail screen as a reward the card offers, next "
                   "to a zero.",
            fix="Either fill in the real rate or delete the rule. A zero-rate rule is worse than "
                "no rule: it looks like an answer."))
    if bad_type:
        f.append(Finding(
            severity=ERROR, code="L4.REWARD_RATE_NOT_A_NUMBER", card_id=card, block="reward_rules",
            index=bad_type[0][0], field="reward_rate",
            message=f"{len(bad_type)} rule(s) on this card store the reward rate as text rather "
                    "than a number, and the app reads text as zero.",
            evidence=trunc("; ".join(f"#{j} {nm} = {v}" for j, nm, v in bad_type[:4]), 300),
            impact="The rule renders 0.00% and never wins a recommendation.",
            fix="Store the rate as a bare number and put the prose in the rule name."))
    if unit_bad:
        f.append(Finding(
            severity=ERROR, code="L4.UNIT_SPEND_MISSING", card_id=card, block="reward_rules",
            index=unit_bad[0][0], field="reward_unit_spend",
            message=f"{len(unit_bad)} rule(s) on this card earn 'N points per Rs X' but do not say "
                    "what X is, so the app cannot do the division.",
            evidence=trunc("; ".join(f"#{j} {nm} = {v}" for j, nm, v in unit_bad[:4]), 300),
            impact="The app falls back to the card's plain base rate, so the whole accelerated "
                   "offer silently disappears for the user.",
            fix="Set reward_unit_spend to the spend block the issuer states (usually 100, 150 or 200)."))
    if unit_leak:
        f.append(Finding(
            severity=WARN, code="L4.UNIT_SPEND_ON_WRONG_TYPE", card_id=card, block="reward_rules",
            index=unit_leak[0][0], field="reward_unit_spend",
            message=f"{len(unit_leak)} rule(s) on this card carry a spend block (reward_unit_spend) "
                    "on a rule type that does not divide by it, so the number is ignored.",
            evidence=trunc("; ".join(f"#{j} {nm} = {v}" for j, nm, v in unit_leak[:4]), 300),
            impact="No user-visible effect today, but it means the rule's stated mechanics and its "
                   "stored type disagree — the next person to read it will trust the wrong one.",
            fix="Either change reward_type to points_per_spend, or remove reward_unit_spend."))
    if big_cash:
        f.append(Finding(
            severity=ERROR, code="L4.CASHBACK_RATE_MAGNITUDE", card_id=card, block="reward_rules",
            index=big_cash[0][0], field="reward_rate",
            message=f"{len(big_cash)} cashback rule(s) on this card store a rate above 1, which the "
                    "app reads as more than 100% back.",
            evidence=trunc("; ".join(f"#{j} {nm} = {v:g}" for j, nm, v in big_cash[:4]), 300),
            impact="The card promises the user more money back than they spend.",
            fix="A cashback rate is a fraction: 5% is 0.05, not 5."))
    if pv_band:
        f.append(Finding(
            severity=WARN, code="L4.RULE_POINT_VALUE_OUT_OF_BAND", card_id=card,
            block="reward_rules", index=pv_band[0][0], field="point_value",
            message=f"{len(pv_band)} rule(s) on this card override the card's point value with a "
                    f"figure outside the plausible Rs {PV_BAND_LO:g}-{PV_BAND_HI:g} range.",
            evidence=trunc("; ".join(f"#{j} {nm} = {v}" for j, nm, v in pv_band[:4]), 300),
            impact="The app clamps anything above Rs 1.50 back to 25 paise, so the rate the user "
                   "sees is computed from a number nobody chose.",
            fix="Remove the override, or set it to the real rupee value of a point on that route."))
    return f


# -------------------------------------------------------------------- caps ---
def _check_caps(inner, card, rules, is_cash) -> list[Finding]:
    """Cap plausibility and, most importantly, cap UNITS.

    Settled KredMe policy: cap_amount is stored in the ISSUER's unit — points for
    a points card, rupees for a cashback card. The app infers the unit instead of
    reading it: for a cashback_pct rule (or cap_kind 'spend') it subtracts RUPEES
    from the cap; for points_per_spend and multiplier it subtracts POINTS.

    So a unit violation is a points card whose cap the app reads in rupees. To
    avoid accusing a genuinely large point cap, a violation is only CONFIRMED
    when the rule's own sentence names a points currency; otherwise it is only
    called suspect, and only when reading the cap as rupees implies a monthly
    spend outside Rs 5,000 - Rs 3,00,000.
    """
    f: list[Finding] = []
    not_num, no_period, no_amount, non_positive = [], [], [], []
    confirmed, suspect, declared, latent = [], [], [], []
    # The point value the APP would use if the rule were typed to read points.
    pv_app = _sane_pv(inner.get("rp_value_standard"))
    unit_is_material = abs(pv_app - 1.0) > CAP_UNIT_MATERIALITY

    for j, r in rules:
        try:
            ca_raw = r.get("cap_amount")
            cp_raw = r.get("cap_period")
            has_period = isinstance(cp_raw, str) and cp_raw.strip() != ""
            if ca_raw is None:
                if has_period:
                    no_amount.append((j, _label(r, j), trunc(cp_raw, 30)))
                continue

            ca = _n(ca_raw)
            if ca is None:
                not_num.append((j, _label(r, j), trunc(ca_raw, 60)))
                continue
            if ca <= 0:
                non_positive.append((j, _label(r, j), ca))
            if not has_period:
                no_period.append((j, _label(r, j), ca))

            engine_unit = _engine_cap_unit(r)
            policy_unit = "inr" if is_cash else "points"

            cu = _low(r.get("cap_unit"))
            if cu in ("inr", "rupees", "points", "point"):
                declared_unit = "inr" if cu in ("inr", "rupees") else "points"
                if declared_unit != engine_unit:
                    declared.append((j, _label(r, j), declared_unit, engine_unit))

            if policy_unit == "points" and engine_unit == "inr":
                name = r.get("rule_name") if isinstance(r.get("rule_name"), str) else ""
                rate = _n(r.get("reward_rate")) or 0.0
                implied = (ca / rate) if rate > 0 and r.get("reward_type") == "cashback_pct" else None
                if _names_points(name) and not _prose_states_rupee_cap(name, ca):
                    # The docstring above guards against accusing a genuinely
                    # large POINT cap, by requiring the rule's own sentence to
                    # name a points currency. It had no guard on the point VALUE,
                    # so it raised ERROR — "counted in the WRONG UNIT", "the cap
                    # is hit at the wrong moment" — on cards where one point is
                    # worth exactly one rupee and the cap therefore binds at the
                    # identical spend either way. That was 8 of 19 card-level
                    # ERRORs and 43 of 66 cap rows: nothing a user could ever
                    # see. Still reported, because the number silently changes
                    # meaning the day somebody corrects the point value — but as
                    # a note, not as a broken card.
                    (confirmed if unit_is_material else latent).append(
                        (j, _label(r, j), ca, implied))
                elif implied is not None and (implied < CAP_SPEND_LO or implied > CAP_SPEND_HI):
                    suspect.append((j, _label(r, j), ca, implied))
        except Exception:
            continue

    cur = inner.get("reward_currency")
    if confirmed:
        ex = "; ".join(
            f"#{j} cap {c:,.0f} in '{nm}'" + (f" (= {_inr(im)}/month of spend if read as rupees)"
                                              if im is not None else "")
            for j, nm, c, im in confirmed[:3])
        f.append(Finding(
            severity=ERROR, code="L4.CAP_IN_RUPEES", card_id=card, block="reward_rules",
            index=confirmed[0][0], field="cap_amount",
            message=f"{len(confirmed)} cap(s) on this card are counted in the WRONG UNIT. The card "
                    f"pays in {cur!r}, and the rules name that currency in their own text, but the "
                    "way each rule is typed makes the app subtract RUPEES from the cap instead of "
                    "points.",
            evidence=trunc(ex, 400),
            impact="The cap is hit at the wrong moment — usually far too late, so the app keeps "
                   "recommending the card long after the user has stopped earning the bonus rate. "
                   "Worse, every time somebody corrects this card's point value the cap silently "
                   "moves too, because the number means a different amount of money.",
            fix="Store the cap in the issuer's own unit (points, NeuCoins, CashPoints - whatever "
                "the issuer prints) and give the rule a type the app reads in points "
                "(points_per_spend or multiplier). Never convert a point cap into rupees."))
    if latent:
        ex = "; ".join(
            f"#{j} cap {c:,.0f} in '{nm}'" + (f" (binds at {_inr(im)}/month either way)"
                                              if im is not None else "")
            for j, nm, c, im in latent[:3])
        f.append(Finding(
            severity=INFO, code="L4.CAP_UNIT_LATENT", card_id=card, block="reward_rules",
            index=latent[0][0], field="cap_amount",
            message=f"{len(latent)} cap(s) on this card are typed so the app subtracts RUPEES "
                    f"from them, while the card pays in {cur!r}. One {cur or 'point'} is worth "
                    f"Rs {pv_app:g} here, so today both readings run out at exactly the same "
                    f"spend and no user sees a different number.",
            evidence=trunc(ex, 400),
            impact="No difference today. But the cap's meaning is pinned to the point value "
                   "being exactly Rs 1.00 — the day somebody corrects that value to what the "
                   "issuer really pays, every one of these caps silently moves with it, and "
                   "nothing in the file records that it was ever supposed to be points.",
            fix="Not urgent, and not a number to change on its own. When this card's point "
                "value is next touched, restate the cap in the issuer's unit and give the rule "
                "a type the app reads in points (points_per_spend or multiplier)."))
    if suspect:
        ex = "; ".join(f"#{j} cap {c:,.0f} implies {_inr(im)} of spend a month in '{nm}'"
                       for j, nm, c, im in suspect[:3])
        f.append(Finding(
            severity=WARN, code="L4.CAP_UNIT_SUSPECT", card_id=card, block="reward_rules",
            index=suspect[0][0], field="cap_amount",
            message=f"{len(suspect)} cap(s) on this points-earning card are read by the app in "
                    "rupees, and the number does not look like rupees: dividing the cap by the "
                    "rule's own rate implies a monthly spend outside the plausible "
                    f"{_inr(CAP_SPEND_LO)} - {_inr(CAP_SPEND_HI)} range. The rule text does not "
                    "name a currency either way, so this needs a human, not an automatic fix.",
            evidence=trunc(ex, 400),
            impact="If these are really point caps the user keeps being recommended the card after "
                   "the bonus has run out; if they are really rupee caps the card stops being "
                   "recommended far too early.",
            fix="Check the issuer's page for the cap's unit, then either restate the cap in points "
                "or confirm the rupee reading and leave it."))
    if declared:
        f.append(Finding(
            severity=ERROR, code="L4.CAP_UNIT_CONTRADICTED", card_id=card, block="reward_rules",
            index=declared[0][0], field="cap_unit",
            message=f"{len(declared)} rule(s) on this card declare the cap's unit explicitly, and "
                    "the app ignores the declaration and uses the opposite unit.",
            evidence=trunc("; ".join(f"#{j} {nm}: declared {d}, app uses {e}"
                                     for j, nm, d, e in declared[:4]), 300),
            impact="Somebody wrote down the right answer and the app is still using the wrong one.",
            fix="cap_unit is not read by the app at all. Change the rule's reward_type / cap_kind "
                "so the app's own inference matches what cap_unit says."))
    if not_num:
        f.append(Finding(
            severity=ERROR, code="L4.CAP_NOT_A_NUMBER", card_id=card, block="reward_rules",
            index=not_num[0][0], field="cap_amount",
            message=f"{len(not_num)} cap(s) on this card are written as a sentence or a set of "
                    "options instead of a single number, so the app throws the cap away and treats "
                    "the rule as UNCAPPED.",
            evidence=trunc("; ".join(f"#{j} {nm}: {v}" for j, nm, v in not_num[:3]), 400),
            impact="The user is promised the accelerated rate on unlimited spending. In reality the "
                   "issuer stops paying at the cap, and the app never tells them.",
            fix="Put one number in cap_amount and one period in cap_period. If the cap really does "
                "change over time or split monthly/annually, split it into separate rules — the "
                "prose belongs in the rule name, never in a numeric field."))
    if no_period:
        f.append(Finding(
            severity=ERROR, code="L4.CAP_WITHOUT_PERIOD", card_id=card, block="reward_rules",
            index=no_period[0][0], field="cap_period",
            message=f"{len(no_period)} cap(s) on this card give an amount but never say per what — "
                    "per month, per cycle, per year. The app needs both, so it enforces neither.",
            evidence=trunc("; ".join(f"#{j} cap {c:,.0f} in '{nm}'" for j, nm, c in no_period[:3]), 300),
            impact="The rule reads as uncapped to the user and keeps being recommended after the "
                   "issuer has stopped paying the bonus rate.",
            fix="Add cap_period. Note the app only really distinguishes 'quarter' and 'year'; "
                "everything else is enforced as a calendar month."))
    if no_amount:
        f.append(Finding(
            severity=WARN, code="L4.CAP_PERIOD_WITHOUT_AMOUNT", card_id=card, block="reward_rules",
            index=no_amount[0][0], field="cap_amount",
            message=f"{len(no_amount)} rule(s) on this card name a cap period but no cap amount, so "
                    "there is nothing to enforce.",
            evidence=trunc("; ".join(f"#{j} {nm}: period {p}" for j, nm, p in no_amount[:4]), 300),
            impact="The rule reads as uncapped.",
            fix="Either fill in cap_amount or remove cap_period."))
    if non_positive:
        f.append(Finding(
            severity=ERROR, code="L4.CAP_NOT_POSITIVE", card_id=card, block="reward_rules",
            index=non_positive[0][0], field="cap_amount",
            message=f"{len(non_positive)} cap(s) on this card are zero or negative, which says the "
                    "user may earn nothing at all under this rule.",
            evidence=trunc("; ".join(f"#{j} {nm} = {c:g}" for j, nm, c in non_positive[:4]), 300),
            impact="The rule is exhausted before the first rupee is spent, so it never fires.",
            fix="Set the real cap, or remove cap_amount if the rule is uncapped."))
    return f


# ------------------------------------------------------------- card scalars --
def _check_card_scalars(inner, card) -> list[Finding]:
    f: list[Finding] = []

    def bad(field, raw, sev, code, msg, impact, fix):
        f.append(Finding(severity=sev, code=code, card_id=card, block="card", field=field,
                         message=msg, evidence=trunc(raw), impact=impact, fix=fix))

    # --- annual fee ---
    af_raw = inner.get("annual_fee")
    af = _n(af_raw)
    if af_raw is not None and af is None:
        bad("annual_fee", af_raw, ERROR, "L4.ANNUAL_FEE_NOT_A_NUMBER",
            "This card's annual fee is not stored as a number, so the app reads it as Rs 0.",
            "The card is shown as free and is ranked ahead of cards that really are free.",
            "Store the fee as a plain number of rupees.")
    elif af is not None and af < 0:
        bad("annual_fee", af_raw, ERROR, "L4.ANNUAL_FEE_NEGATIVE",
            f"This card's annual fee is negative ({_inr(af)}).",
            "The app treats a lower fee as better, so a negative fee makes this card beat every "
            "genuinely free card in a tie.",
            "Set the fee to 0 for a lifetime-free card.")
    elif af is not None and af > ANNUAL_FEE_ABSURD:
        bad("annual_fee", af_raw, WARN, "L4.ANNUAL_FEE_ABSURD",
            f"This card's annual fee is {_inr(af)}. India's most expensive card is around "
            "Rs 3,00,000, so this looks like an extra zero.",
            "The card is pushed down every tie-break it takes part in.",
            "Re-check the fee on the issuer's fee schedule.")

    # --- fee waiver ---
    fw_raw = inner.get("fee_waiver_spend")
    fw = _n(fw_raw)
    if fw_raw is not None and fw is None:
        bad("fee_waiver_spend", fw_raw, WARN, "L4.FEE_WAIVER_NOT_A_NUMBER",
            "The spend needed to waive this card's fee is not stored as a number.",
            "The waiver never shows up correctly on the card detail screen.",
            "Store the waiver threshold as a plain number of rupees.")
    elif fw is not None:
        if fw < 0:
            bad("fee_waiver_spend", fw_raw, ERROR, "L4.FEE_WAIVER_NEGATIVE",
                f"This card says you need to spend {_inr(fw)} — a negative amount — to have the "
                "annual fee waived.",
                "Nonsense on the card detail screen.",
                "Set the real annual spend threshold, or null if there is no waiver.")
        elif fw > FEE_WAIVER_ABSURD:
            bad("fee_waiver_spend", fw_raw, WARN, "L4.FEE_WAIVER_ABSURD",
                f"This card says the fee is waived after {_inr(fw)} of spending — over Rs 1 crore "
                "a year. That is almost certainly an extra zero.",
                "A user is told a waiver they could actually reach is out of reach.",
                "Re-check the threshold on the issuer's fee schedule.")
        elif af is not None and af > 0 and fw < af:
            bad("fee_waiver_spend", fw_raw, WARN, "L4.FEE_WAIVER_BELOW_FEE",
                f"This card says spending {_inr(fw)} waives a fee of {_inr(af)} — you would spend "
                "less than the fee itself to escape it.",
                "The card looks effectively free when it is not.",
                "One of the two numbers is wrong; check both against the issuer's fee schedule.")
        elif (af is None or af <= 0) and fw > 0:
            bad("fee_waiver_spend", fw_raw, WARN, "L4.FEE_WAIVER_ON_FREE_CARD",
                f"This card has no annual fee, yet it also says you must spend {_inr(fw)} to have "
                "the fee waived. There is nothing to waive.",
                "The card detail screen shows a spending target that means nothing.",
                "Either the card does have a fee and annual_fee is wrong, or fee_waiver_spend "
                "should be null.")

    # --- forex markup ---
    fx_raw = inner.get("forex_markup_pct")
    fx = _n(fx_raw)
    if fx_raw is None:
        bad("forex_markup_pct", fx_raw, WARN, "L4.FOREX_MISSING",
            "This card does not say what it charges on foreign spending, so the app assumes 3.5%.",
            "On any international merchant the app subtracts an invented 3.5% from this card's "
            "rate, which can drop it out of the recommendations for a reason nobody chose.",
            "Set forex_markup_pct from the issuer's fee schedule. Zero-forex cards must say 0, "
            "not be left blank.")
    elif fx is None:
        bad("forex_markup_pct", fx_raw, ERROR, "L4.FOREX_NOT_A_NUMBER",
            "This card's foreign-currency markup is not stored as a number.",
            "The app falls back to an assumed 3.5% on every international purchase.",
            "Store the markup as a plain percentage number, e.g. 3.5.")
    elif fx < 0 or fx > FOREX_ABSURD_PCT:
        bad("forex_markup_pct", fx_raw, ERROR, "L4.FOREX_OUT_OF_BAND",
            f"This card claims a foreign-currency markup of {fx:g}%. Indian issuers charge between "
            f"0% and about 3.5%; anything outside 0-{FOREX_ABSURD_PCT:g}% is a data error.",
            "The app subtracts this straight off the reward rate on every international merchant, "
            "so the card's international ranking is wrong by exactly this much.",
            "Re-check the markup on the issuer's fee schedule.")

    # --- minimum redemption ---
    mr_raw = inner.get("min_redemption_points")
    mr = _n(mr_raw)
    if mr_raw is not None and mr is None:
        bad("min_redemption_points", mr_raw, WARN, "L4.MIN_REDEMPTION_NOT_A_NUMBER",
            "The minimum number of points this card lets you redeem is not stored as a number.",
            "The redemption screen cannot tell the user when they can cash out.",
            "Store it as a plain number of points.")
    elif mr is not None and (mr < 0 or mr > MIN_REDEMPTION_ABSURD):
        bad("min_redemption_points", mr_raw, WARN, "L4.MIN_REDEMPTION_IMPLAUSIBLE",
            f"This card says you need {mr:,.0f} points before you can redeem anything. Real minimums "
            f"run from 100 to a few thousand; above {MIN_REDEMPTION_ABSURD:,.0f} is a data error.",
            "The redemption screen tells the user their points are unreachable.",
            "Re-check the minimum on the issuer's rewards page.")

    # --- points expiry ---
    pe_raw = inner.get("points_expiry_months")
    pe = _n(pe_raw)
    if pe_raw is not None and pe is None:
        bad("points_expiry_months", pe_raw, WARN, "L4.EXPIRY_NOT_A_NUMBER",
            "How long this card's points last is not stored as a number.",
            "The redemption screen cannot warn a user their points are about to expire.",
            "Store the lifetime as a plain number of months.")
    elif pe is not None:
        if pe <= 0 or pe > EXPIRY_MONTHS_ABSURD:
            bad("points_expiry_months", pe_raw, ERROR, "L4.EXPIRY_IMPLAUSIBLE",
                f"This card says its points last {pe:g} months, which is either instant expiry or "
                f"more than {EXPIRY_MONTHS_ABSURD // 12:g} years.",
                "A user is told the wrong deadline for spending their points.",
                "Re-check the expiry on the issuer's rewards terms.")
        elif pe < EXPIRY_MONTHS_SUSPECT:
            bad("points_expiry_months", pe_raw, WARN, "L4.EXPIRY_SUSPICIOUSLY_SHORT",
                f"This card says its points expire after {pe:g} month(s). Almost every Indian card "
                "uses 12, 24 or 36 months, so this looks like YEARS entered into a MONTHS field.",
                "The redemption screen tells the user to burn their points far sooner than they "
                "have to, or hides points it thinks are already dead.",
                f"Confirm with the issuer. If the answer is '{pe:g} years', store {pe * 12:g}.")
    return f


# -------------------------------------------------------------------- fuel ---
def _check_fuel(entry, card) -> list[Finding]:
    f: list[Finding] = []
    band, mixed, inverted, missing, cap_bad = [], [], [], [], []

    for j, r in enumerate(_rows(entry, "fuel_surcharge_rules")):
        if not isinstance(r, dict):
            continue
        try:
            wp = _n(r.get("waiver_pct"))
            mn = _n(r.get("min_txn_amount"))
            mx = _n(r.get("max_txn_amount"))
            mc = _n(r.get("monthly_cap"))

            if r.get("waiver_pct") is not None and wp is None:
                band.append((j, "waiver_pct", trunc(r.get("waiver_pct"), 30)))
            elif wp is not None and (wp < 0 or wp > 100):
                band.append((j, "waiver_pct", f"{wp:g}"))
            elif wp is not None and wp != 1.0:
                # The file mixes two conventions in one column: 1.0 meaning
                # "100% of the surcharge waived" and 100.0 meaning the same thing
                # as a percentage. Anything that is neither is ambiguous.
                mixed.append((j, wp))

            if mn is not None and mx is not None and mn > mx:
                inverted.append((j, mn, mx))
            if r.get("min_txn_amount") is None or r.get("max_txn_amount") is None:
                missing.append((j, r.get("min_txn_amount"), r.get("max_txn_amount")))
            if mc is not None and (mc <= 0 or mc > FUEL_CAP_SUSPECT):
                cap_bad.append((j, mc))
        except Exception:
            continue

    if band:
        f.append(Finding(
            severity=ERROR, code="L4.FUEL_WAIVER_OUT_OF_BAND", card_id=card,
            block="fuel_surcharge_rules", index=band[0][0], field="waiver_pct",
            message="This card's fuel surcharge waiver is not a percentage between 0 and 100.",
            evidence=trunc("; ".join(f"#{j} {fl}={v}" for j, fl, v in band[:4]), 200),
            impact="The card detail screen states a fuel benefit that cannot be read.",
            fix="Store the waived share of the surcharge as a number from 0 to 100."))
    if mixed:
        f.append(Finding(
            severity=WARN, code="L4.FUEL_WAIVER_UNIT_AMBIGUOUS", card_id=card,
            block="fuel_surcharge_rules", index=mixed[0][0], field="waiver_pct",
            message="This card's fuel surcharge waiver is written in a different unit from the rest "
                    "of the file: almost every other card writes 1.0 for a full waiver, this one "
                    f"writes {'/'.join(f'{v:g}' for _j, v in mixed[:3])}.",
            evidence=trunc("; ".join(f"#{j} waiver_pct={v:g}" for j, v in mixed[:4]), 200),
            impact="No live effect today — the app only checks whether a fuel block EXISTS and "
                   "ignores the number entirely — so a partial waiver is currently advertised to "
                   "the user as a full one.",
            fix="Pick one convention for the whole file (1.0 = full waiver is the majority) and "
                "restate this row in it."))
    if inverted:
        f.append(Finding(
            severity=ERROR, code="L4.FUEL_MIN_ABOVE_MAX", card_id=card,
            block="fuel_surcharge_rules", index=inverted[0][0],
            message="This card's fuel surcharge waiver has a minimum transaction larger than its "
                    "maximum, so no purchase can ever qualify.",
            evidence=trunc("; ".join(f"#{j} min {mn:g} > max {mx:g}" for j, mn, mx in inverted[:3]), 200),
            impact="The card advertises a fuel benefit that, as written, applies to no transaction "
                   "at all.",
            fix="Swap the two numbers, or re-read the issuer's stated transaction window."))
    if missing:
        f.append(Finding(
            severity=WARN, code="L4.FUEL_WINDOW_MISSING", card_id=card,
            block="fuel_surcharge_rules", index=missing[0][0],
            message="This card's fuel surcharge waiver does not state the transaction size it "
                    "applies to, so the app assumes Rs 400 to Rs 4,000.",
            evidence=trunc("; ".join(f"#{j} min={mn!r} max={mx!r}" for j, mn, mx in missing[:3]), 200),
            impact="A user filling up outside the assumed window is told they get a waiver they "
                   "may not get.",
            fix="Fill in min_txn_amount and max_txn_amount from the issuer's fuel terms."))
    if cap_bad:
        f.append(Finding(
            severity=WARN, code="L4.FUEL_CAP_IMPLAUSIBLE", card_id=card,
            block="fuel_surcharge_rules", index=cap_bad[0][0], field="monthly_cap",
            message="This card's monthly fuel surcharge cap is outside the plausible range — real "
                    f"caps run about Rs 100 to Rs 1,000 a month, and anything above "
                    f"{_inr(FUEL_CAP_SUSPECT)} usually means a spend limit was entered where a "
                    "surcharge-refund limit belongs.",
            evidence=trunc("; ".join(f"#{j} monthly_cap={v:,.0f}" for j, v in cap_bad[:3]), 200),
            impact="No live effect today — the app never enforces this cap — but the card detail "
                   "screen and any future enforcement would both be wrong.",
            fix="Confirm whether the issuer's number is the refund cap or the eligible-spend cap, "
                "and store the refund cap here."))
    return f


# -------------------------------------------------------------- milestones ---
def _check_milestones(entry, inner, card) -> list[Finding]:
    """A milestone is 'spend X, get Y'. Y divided by X is an effective rate, and
    the same ceilings apply. Y's unit has to be guessed from bonus_type, so this
    only scores rows where the type is unambiguous, and it never scores a
    'voucher' worth under Rs 100 — that is a COUNT of vouchers, not a value.
    """
    f: list[Finding] = []
    pv = _sane_pv(inner.get("rp_value_standard"))
    absurd, high, no_target, no_value = [], [], [], []

    for j, m in enumerate(_rows(entry, "milestone_rules")):
        if not isinstance(m, dict):
            continue
        try:
            name = m.get("milestone_name") or m.get("rule_name") or f"<milestone #{j}>"
            name = trunc(name, 55)
            st = _n(m.get("spend_target"))
            bv = _n(m.get("bonus_value"))
            bt = _low(m.get("bonus_type"))

            if st is None or st <= 0:
                no_target.append((j, name, trunc(m.get("spend_target"), 30)))
                continue
            if bv is None or bv <= 0:
                no_value.append((j, name, trunc(m.get("bonus_value"), 30)))
                continue

            if any(w in bt for w in ("point", "mile", "chip", "coin")):
                worth = bv * pv
            elif any(w in bt for w in ("voucher", "cashback", "credit", "fee", "discount", "value")):
                if bv < MILESTONE_VOUCHER_MIN_INR:
                    continue                      # a count of vouchers, not a rupee value
                worth = bv
            else:
                continue                          # free-text benefit; no honest way to price it

            pct = worth / st * 100.0
            if pct > HARD_CEILING_PCT:
                absurd.append((j, name, pct, bv, st, bt))
            elif pct > SOFT_CEILING_PCT:
                high.append((j, name, pct, bv, st, bt))
        except Exception:
            continue

    def _ml(items, n=3):
        return trunc("; ".join(
            f"#{j} {nm}: {v:g} {bt or 'units'} for {_inr(s)} spend = {p:.1f}%"
            for j, nm, p, v, s, bt in items[:n]), 400)

    if absurd:
        f.append(Finding(
            severity=ERROR, code="L4.MILESTONE_RATE_ABSURD", card_id=card, block="milestone_rules",
            index=absurd[0][0],
            message=f"{len(absurd)} milestone(s) on this card promise a return above "
                    f"{HARD_CEILING_PCT:.0f}% of the spend required to earn them. No issuer runs a "
                    "milestone like that; one of the two numbers is in the wrong magnitude.",
            evidence=_ml(absurd),
            impact="The card detail screen tells the user a milestone is worth several times what "
                   "it really pays.",
            fix="Usually the spend target has lost a zero or two — check the issuer's stated "
                "milestone, and that bonus_value is in the same unit as bonus_type says."))
    if high:
        f.append(Finding(
            severity=WARN, code="L4.MILESTONE_RATE_HIGH", card_id=card, block="milestone_rules",
            index=high[0][0],
            message=f"{len(high)} milestone(s) on this card imply a return above "
                    f"{SOFT_CEILING_PCT:.0f}% of the spend needed to earn them. Possible for a "
                    "small monthly transaction bonus, and also what a magnitude error looks like.",
            evidence=_ml(high),
            impact="If wrong, the card detail screen overstates a headline benefit.",
            fix="Confirm the spend target and the bonus against the issuer's milestone table."))
    if no_target:
        f.append(Finding(
            severity=ERROR, code="L4.MILESTONE_TARGET_MISSING", card_id=card,
            block="milestone_rules", index=no_target[0][0], field="spend_target",
            message=f"{len(no_target)} milestone(s) on this card do not say how much you have to "
                    "spend to earn them, and the app stores a missing target as zero.",
            evidence=trunc("; ".join(f"#{j} {nm}: {v}" for j, nm, v in no_target[:4]), 300),
            impact="The card detail screen shows a reward with no spending requirement next to it — "
                   "it reads like a free gift.",
            fix="Fill in spend_target. Note the app reads spend_target only; a value written into "
                "spend_threshold is thrown away."))
    if no_value:
        f.append(Finding(
            severity=WARN, code="L4.MILESTONE_VALUE_MISSING", card_id=card, block="milestone_rules",
            index=no_value[0][0], field="bonus_value",
            message=f"{len(no_value)} milestone(s) on this card state a spending target but never "
                    "say what the user gets for hitting it.",
            evidence=trunc("; ".join(f"#{j} {nm}: {v}" for j, nm, v in no_value[:4]), 300),
            impact="The user is shown a target with no reward attached, so there is no reason to "
                   "chase it.",
            fix="Fill in bonus_value and bonus_type. Note the app reads bonus_value / reward_value "
                "only; a value written into benefit_value is thrown away."))
    return f
