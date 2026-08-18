"""L5 — does the English in a rule's name agree with the numbers the app runs on?

A ``rule_name`` is written by a human out of the issuer's own words. ``reward_rate``,
``cap_amount``, ``channel``, ``category_id`` and the rest are what the app actually
computes with. When the two disagree, one of them is lying to a real user — and the
name is usually the one that is right, because it is the only place in the file where
the issuer's own wording survives.

This layer parses the ENGLISH and compares it to the FIELDS.

It never proposes rewriting a rule_name to make a number "agree". That is circular:
the name is the evidence, and renaming it destroys the only independent check the file
carries. Worse, the app synthesises eligibility gates out of rule_name text
(credit_card.dart:390-431) and buckets caps by rule_name (recommendation_engine.dart
:819), so a rename can silently switch a Prime/Swiggy gate on or off and merge two
caps into one.

And a rule whose name carries no number is NOT consistent — it is UNVERIFIABLE. That
count is reported out loud, because a self-consistency gate that is blind to it will
print green while proving nothing.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .base import (Ctx, Finding, Skipped, ERROR, WARN, INFO, num, trunc, iso_ok,
                   card_base_pct)

LAYER = "L5 text-vs-number self-consistency"


# --------------------------------------------------------------------------- #
# 0.  tiny, total helpers — every one of these must survive None/dict/bool/junk
# --------------------------------------------------------------------------- #
def _txt(v) -> str:
    return v if isinstance(v, str) else ""


_SCALE = {"": 1.0, "k": 1e3, "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5,
          "lakhs": 1e5, "cr": 1e7, "crore": 1e7, "crores": 1e7}


def _amount(digits, suffix=None):
    """'1,50,000' -> 150000.0 ;  ('1.5','L') -> 150000.0 ;  junk -> None."""
    try:
        v = float(str(digits).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    s = (suffix or "").strip().lower().rstrip(".")
    return v * _SCALE.get(s, 1.0)


def _close(a, b, rel=0.05, tol=0.05) -> bool:
    """Deliberately generous. A false 'the text says X' claim is worse than a miss."""
    if a is None or b is None:
        return True
    return abs(a - b) <= max(tol, rel * max(abs(a), abs(b)))


def _fmt(x) -> str:
    if x is None:
        return "?"
    return ("%.4f" % x).rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- #
# 1.  the app's own arithmetic, replicated exactly
#     (credit_card.dart:633-636 sanePointValue, :655-678 rateForRule)
# --------------------------------------------------------------------------- #
def _sane_pv(v):
    n = num(v)
    if n is None or n <= 0 or n > 1.5:
        return 0.25
    return n


def _engine(inner: dict, rule: dict):
    """(effective percent the app will show, point value it used).

    percent is None when the app itself cannot produce a number ('Rate not published').
    """
    raw_pv = rule.get("point_value")
    if raw_pv is None:
        raw_pv = inner.get("rp_value_standard")
    pv = _sane_pv(raw_pv)

    base = num(inner.get("base_reward_rate"))
    if base is not None and base <= 0:
        base = None

    rt = _txt(rule.get("reward_type")).strip().lower()
    rate = num(rule.get("reward_rate"))
    rate = 0.0 if rate is None else rate
    unit = num(rule.get("reward_unit_spend"))
    unit = 100.0 if unit is None else unit

    if rt == "cashback_pct":
        return rate * 100.0, pv
    if rt == "multiplier":
        return (None if base is None else rate * base * pv * 100.0), pv
    if rt == "points_per_spend":
        if unit <= 0:
            return (None if base is None else base * pv * 100.0), pv
        return (rate / unit) * pv * 100.0, pv
    return (None if base is None else base * pv * 100.0), pv


def _engine_cap_unit(rule: dict) -> str:
    """What ``cap_amount`` is counted in, per credit_card.dart:362-383.

    'spend' = rupees of eligible spend, 'inr' = rupees of cashback, 'points' = points.
    """
    if _txt(rule.get("cap_kind")).strip().lower() == "spend":
        return "spend"
    rt = _txt(rule.get("reward_type")).strip().lower()
    if rt == "cashback_pct":
        return "inr"
    if rt in ("points_per_spend", "multiplier"):
        return "points"
    return "inr"


# --------------------------------------------------------------------------- #
# 2.  vocabulary the parser recognises
# --------------------------------------------------------------------------- #
_PT_WORDS = (
    r"reward\s*points?|neu\s*coins?|neucoins?|cash\s*points?|cashpoints?|"
    r"edge\s*(?:reward\s*)?points?|maharaja\s*points?|spiceclub\s*points?|"
    r"zen\s*points?|saving\s*points?|fuel\s*points?|adani\s*reward\s*points?|"
    r"travel\s*credits?|bonus\s*points?|membership\s*rewards?|club\s*vistara\s*points?|"
    r"inter\s*miles|intermiles|air\s*miles|miles|smiles|blu\s*chips?|bluchips?|"
    r"super\s*coins?|supercoins?|uni\s*coins?|scapia\s*coins?|coins?|points?|pts?|rps|rp"
)
_CB_WORDS = r"cash\s*back|cashback|value\s*back|rewards?|reward|back"
_PT = "(?:" + _PT_WORDS + ")"
_ANY = "(?:" + _PT_WORDS + "|" + _CB_WORDS + ")"

# ---- percent claim:  "5% cashback", "3.3% reward rate", "2% NeuCoins" -------
_PCT = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")
_SOFT = re.compile(r"(?i)(?:up\s*to|upto|~|about|approx\.?|around|maximum|max|nearly)\s*$")
_PCT_TAIL = re.compile(r"(?i)^(?:[a-z0-9&][a-z0-9&.\-']*\s+){0,2}?" + _ANY + r"\b")
# '50% more Reward Points' is an uplift on some other rate, not a rate.
_PCT_RELATIVE = re.compile(r"(?i)^(?:more|extra|additional|higher|further|off|discount|"
                           r"upto|up\s*to)\b")

# ---- points-per-spend claim: "4 Reward Points per Rs. 150 spent" -----------
_PPS = re.compile(
    r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)(?:\s*\)|)\s+"                  # 1  N, then a real break
    r"((?:[A-Za-z][A-Za-z0-9&.\-)]*\s+){0,5}?)"                      # 2  currency words
    r"(?:per|for\s+every|on\s+every|for\s+each|on\s+each|every|each|/)\s*"
    r"(?:(?!rs\b|inr\b)[a-z]+\s+){0,4}?"                             #    'spend of', ...
    r"(?:(?:Rs\.?|INR|₹)\s*(\d[\d,]*(?:\.\d+)?)"                # 3  M, with symbol
    r"|(\d[\d,]*(?:\.\d+)?)\s*(?:(?:Rs\.?|INR|₹)\s*)?(?:spent|spend)\b"   # 4  '100 spent'
    r"|(\d[\d,]*(?:\.\d+)?)\s*(?:Rs\.?|INR|₹)\b"                    # 5  '100 INR'
    r"|(?<=per\s)(\d{2,5})(?![\d.,%]))",                             # 6  bare 'per 100'
    re.I,
)
_PPS_REWARD = re.compile(r"(?i)\b" + _ANY + r"\b")
_POST_CAP = re.compile(
    r"(?i)(after\s+cap|beyond\s+(?:the\s+)?cap|thereafter|there\s*after|post[- ]?cap|"
    r"revert(?:s|ing)?|drops?\s+to|reduced\s+to|then\s+earns?|falls?\s+back|"
    r"over\s+and\s+above|in\s+addition\s+to|on\s+top\s+of|above\s+(?:the\s+)?base|"
    r"plus\s+(?:the\s+)?base)"
)

# ---- multiplier claim: "5X", "10x" ----------------------------------------
_MULT_X = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*[xX](?![A-Za-z])")

# ---- cap claim -------------------------------------------------------------
_CAP = re.compile(
    r"(?i)\b(?:cap(?:ped|s)?|max(?:imum)?|\blimit(?:ed)?|up\s*to|upto)\b"
    r"([^.;]{0,30}?)"                                                # 1 filler
    r"(?<![\d,.])(₹|Rs\.?|INR)?\s*"                             # 2 leading symbol
    r"(\d[\d,]*(?:\.\d+)?)\s*"                                       # 3 digits
    r"(L|lakhs?|lacs?|k|cr|crores?)?\b"                              # 4 scale
    r"([^.;]{0,36})"                                                 # 5 tail
)
_CAP_STOP = (r"(?!per\b|in\b|at\b|for\b|of\b|on\b|and\b|the\b|a\b|to\b|across\b|"
             r"with\b|combined\b|from\b|each\b|every\b|or\b|is\b|shared\b)")
_CAP_PTS = re.compile(r"(?i)^\s*(?:" + _CAP_STOP + r"[A-Za-z0-9][A-Za-z0-9]*\s+){0,3}?"
                      + _PT + r"\b")
# 'up to 40 Reward Points for every Rs 100' is a RATE, not a ceiling.
_CAP_SOFT = re.compile(r"(?i)^up\s*to\b|^upto\b")
_CAP_IS_RATE = re.compile(r"(?i)(?:per|for\s+every|on\s+every|/)\s*(?:Rs\.?|INR|\u20b9|\d)")
_CAP_INR = re.compile(r"(?i)^\s*(?:accelerated\s+|bonus\s+|additional\s+|extra\s+)?"
                      r"(?:cash\s*back|cashback|rupees?|inr)\b")
_SPENDY = re.compile(r"(?i)(spends?\b|spending|transaction\s+amount|bill\s+amount|"
                     r"purchase\s+value|eligible\s+spend)")
# '...cumulative spends up to Rs 2,00,000 per month' — the ceiling is on SPEND, and
# the word sits just before the trigger, not after the number.
_SPEND_BEFORE = re.compile(r"(?i)(?:spends?|spending|transaction\s+amount)\s+$")

_PERIODS = [
    ("year",        r"(?i)\b(?:per\s+annum|annually|annual|yearly|per\s+year|"
                    r"in\s+a\s+year|calendar\s+year|anniversary\s+year|/\s*(?:yr|year))\b"),
    ("quarter",     r"(?i)\b(?:per\s+quarter|quarterly|calendar\s+quarter|/\s*(?:qtr|quarter))\b"),
    ("cycle",       r"(?i)\b(?:statement\s+(?:cycle|month|period)|billing\s+cycle|"
                    r"payment\s+cycle|per\s+cycle|per\s+statement|bill\s+cycle)\b"),
    ("day",         r"(?i)\b(?:per\s+day|daily|/\s*day)\b"),
    ("transaction", r"(?i)\b(?:per\s+transaction|per\s+txn|per\s+bill\b|"
                    r"per\s+order\b|each\s+transaction)\b"),
    ("month",       r"(?i)\b(?:per\s+(?:calendar\s+)?month|monthly|"
                    r"per\s+month|/\s*(?:mo|month)|in\s+a\s+month)\b"),
]
_PERIODS = [(k, re.compile(p)) for k, p in _PERIODS]
# the engine buckets everything except quarter/year into the current calendar month
_BUCKET = {"year": "year", "quarter": "quarter", "month": "month", "cycle": "month",
           "transaction": "month", "day": "month"}

# ---- minimum transaction ---------------------------------------------------
_MIN_TXN = re.compile(
    r"(?i)(?:min(?:imum)?\.?\s*(?:transaction|txn|spend|order|purchase|bill|ticket)"
    r"(?:\s+(?:amount|value|size|of))?|transactions?\s+(?:above|over|exceeding)|"
    r"spends?\s+(?:above|over|exceeding))\s*(?:of\s+)?"
    r"(?:₹|Rs\.?|INR)?\s*(\d[\d,]*(?:\.\d+)?)\s*(L|lakhs?|k)?\b"
)
_BETWEEN = re.compile(
    r"(?i)\bbetween\s*(?:₹|Rs\.?|INR)?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:and|to|-)\s*"
    r"(?:₹|Rs\.?|INR)?\s*(\d[\d,]*(?:\.\d+)?)"
)

# ---- channel ---------------------------------------------------------------
_CHANNELS = [
    ("online",        r"(?i)\bonline\b"),
    ("offline",       r"(?i)(\boffline\b|\bin[- ]store\b|\bat\s+stores?\b|"
                      r"\bpoint[- ]of[- ]sale\b|\bretail\s+outlets?\b)"),
    ("upi",           r"(?i)(\bUPI\b|\brupay\s+upi\b|\bscan\s*(?:and|&)\s*pay\b)"),
    ("international", r"(?i)(\binternational\b|\boverseas\b|\babroad\b|"
                      r"\bforeign\s+currency\b|\bcross[- ]border\b)"),
    ("portal",        r"(?i)(\bsmartbuy\b|\bsmart\s*buy\b|\bgyftr\b|\brewards?\s+portal\b)"),
]
_CHANNELS = [(k, re.compile(p)) for k, p in _CHANNELS]
_BOTH_CHANNELS = re.compile(r"(?i)(?:online|offline|retail|in[- ]store|physical)\s*"
                            r"(?:,\s*)?(?:and|&|/|or)\s*"
                            r"(?:online|offline|retail|in[- ]store|physical)\b")
_NEG_BEFORE = re.compile(r"(?i)(?:non[- ]?|excluding\s+|except\s+(?:for\s+)?|"
                         r"other\s+than\s+|no\s+|not\s+)$")

# ---- category --------------------------------------------------------------
_TAG = re.compile(r"\[([a-z0-9_]+)\]\s*$")
_CAT_WORDS = [
    ("fuel",               r"(?i)\b(?:fuel|petrol|diesel|petrol\s+pumps?)\b"),
    ("dining",             r"(?i)\b(?:dining|restaurants?)\b"),
    ("grocery",            r"(?i)\b(?:grocer(?:y|ies)|supermarkets?)\b"),
    ("entertainment",      r"(?i)\b(?:movies?|cinemas?)\b"),
    ("insurance",          r"(?i)\binsurance\b"),
    ("utilities",          r"(?i)\b(?:utility|utilities)\b"),
    ("rent",               r"(?i)\brent(?:al)?\s+(?:payments?|spends?|transactions?)\b|\brent\b"),
    ("education",          r"(?i)\b(?:education(?:al)?|tuition|school\s+fees?)\b"),
    ("telecom",            r"(?i)\b(?:telecom|mobile\s+recharges?)\b"),
    ("departmental_store", r"(?i)\bdepartmental\s+stores?\b"),
    ("jewellery",          r"(?i)\bjewell?ery\b"),
    ("pharmacy",           r"(?i)\b(?:pharmac(?:y|ies)|chemists?)\b"),
    ("hotels",             r"(?i)\bhotels?\b"),
    ("airlines",           r"(?i)\b(?:flights?|airlines?|air\s+tickets?)\b"),
    ("railways",           r"(?i)\b(?:railways?|train\s+tickets?|irctc)\b"),
    ("wallet_load",        r"(?i)\bwallet\s+(?:loads?|reloads?)\b"),
    ("government",         r"(?i)\b(?:government\s+(?:services?|payments?)|tax\s+payments?)\b"),
]
_CAT_WORDS = [(k, re.compile(p)) for k, p in _CAT_WORDS]

# ---- dates -----------------------------------------------------------------
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_D_START = re.compile(
    r"(?i)\b(?:effective(?:\s+from)?|w\.?e\.?f\.?|with\s+effect\s+from|"
    r"starting(?:\s+from)?|applicable\s+from)\s+"
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+)?(?:" + _MON + r"\.?\s*)?\d{0,2},?\s*(20\d{2})"
)
_D_END = re.compile(
    r"(?i)\b(?:valid\s+(?:till|until|upto|up\s*to|through)|till|until|"
    r"expir(?:es|ing|y)\s+(?:on|date)?|ends?\s+on|last\s+date)\s+"
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+)?(?:" + _MON + r"\.?\s*)?\d{0,2},?\s*(20\d{2})"
)
_D_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

# ---- milestones ------------------------------------------------------------
_MS_SPEND = re.compile(
    r"(?i)\b(?:on\s+|upon\s+|after\s+|for\s+|if\s+)?"
    r"(?:annual\s+|yearly\s+|quarterly\s+|monthly\s+|cumulative\s+|total\s+)?"
    r"(?:spend(?:ing|s)?|spent|reaching|achieving)\s*"
    r"(?:of\s+|over\s+|above\s+|more\s+than\s+|at\s+least\s+)?"
    r"(?:₹|Rs\.?|INR)?\s*(\d[\d,]*(?:\.\d+)?)\s*(L|lakhs?|lacs?|k|cr|crores?)?\b"
)
# 'a SECOND spending of Rs 8 Lakhs' may mean 8L more or 16L cumulative — unknowable.
_MS_INCREMENTAL = re.compile(r"(?i)\b(?:additional|incremental|second|third|another|"
                             r"over\s+and\s+above)\b")
# 'in six months', 'first 90 days' — a period the app's vocabulary cannot express.
_MS_ODD_PERIOD = re.compile(r"(?i)\b(?:\d+\s*(?:months?|days?|weeks?)|six\s+months?|"
                            r"half[- ]year|semi[- ]annual|anniversary)\b")
_MS_SPEND_ALT = re.compile(
    r"(?i)\b(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d+)?)\s*(L|lakhs?|lacs?|k|cr|crores?)?"
    r"\s*(?:or\s+more\s+)?(?:is\s+)?spent\b"
)

# ---- redemption ------------------------------------------------------------
_PER_POINT = re.compile(
    r"(?i)(?:₹|Rs\.?|INR)\s*(\d+(?:\.\d+)?)\s*(?:per|/|a)\s*(?:reward\s*)?point\b"
    r"|(\d+(?:\.\d+)?)\s*paise\s*(?:per|/|a)\s*(?:reward\s*)?point\b"
)


# --------------------------------------------------------------------------- #
# 3.  claim extraction — conservative on purpose
# --------------------------------------------------------------------------- #
def _pct_claims(name: str):
    out = []
    for m in _PCT.finditer(name):
        if _SOFT.search(name[max(0, m.start() - 14):m.start()]):
            continue
        tail = name[m.end():m.end() + 46].lstrip(" ,")
        if _PCT_RELATIVE.match(tail) or not _PCT_TAIL.match(tail):
            continue
        v = _amount(m.group(1))
        if v is not None and 0 < v <= 100:
            out.append(v)
    if len(set(out)) > 1:
        return []          # two different percentages in one sentence: claim neither
    return out


def _pps_claims(name: str):
    """[(points N, per-rupees M)] stated as an absolute accrual."""
    out = []
    for m in _PPS.finditer(name):
        before = name[max(0, m.start() - 46):m.start()]
        if _POST_CAP.search(before):
            continue                              # 'after cap, earns 1 RP per Rs.100'
        words = m.group(2) or ""
        if not _PPS_REWARD.search(words):
            continue
        n = _amount(m.group(1))
        d = _amount(m.group(3) or m.group(4) or m.group(5) or m.group(6))
        if n is None or d is None:
            continue
        if not (10 <= d <= 100000) or not (0 < n <= 1000):
            continue
        out.append((n, d))
    return out


def _mult_claims(name: str):
    vals = []
    for m in _MULT_X.finditer(name):
        v = _amount(m.group(1))
        if v is not None and 1 <= v <= 100:
            vals.append(v)
    return vals


def _period_in(s: str):
    hits = []
    for key, rx in _PERIODS:
        if rx.search(s):
            hits.append(key)
    # 'per statement cycle' also matches nothing else; 'per calendar month' -> month
    if "cycle" in hits and "month" in hits:
        hits = [h for h in hits if h != "month"]
    return hits


# 'Base rate (up to Rs 1.5L monthly spend)' is a spend TIER, not a ceiling: the
# rate changes at the threshold, and the very next rule on the card states the
# rate ABOVE it. Nothing is capped, cap_amount is correctly null, and telling the
# reader "the app is failing to enforce a cap" sends them looking for a cap that
# does not exist. The tell is a parenthetical that talks about SPEND and names no
# reward currency — a real ceiling is denominated in points or rupees of reward.
_PAREN = re.compile(r"[(\[]([^)\]]{0,120})[)\]]")
_SPEND_WORD = re.compile(r"(?i)\bspends?\b")
_REWARD_NOUN = re.compile(r"(?i)\b(?:" + _PT_WORDS + r"|" + _CB_WORDS + r")\b")


def _spend_tier_spans(name: str):
    """Character ranges of parentheticals that describe a spend tier."""
    spans = []
    for m in _PAREN.finditer(name or ""):
        body = m.group(1)
        if _SPEND_WORD.search(body) and not _REWARD_NOUN.search(body):
            spans.append((m.start(), m.end()))
    return spans


def _cap_claims(name: str):
    """[(amount, unit, period)] where unit in {'inr','points','spend',None}."""
    out = []
    tier_spans = _spend_tier_spans(name)
    for m in _CAP.finditer(name):
        if any(a <= m.start() < b for a, b in tier_spans):
            # A spend tier, not a ceiling. Classified 'spend' so it is excluded
            # from the cap-amount comparison, exactly like every other
            # spend-denominated threshold this parser already recognises.
            amt0 = _amount(m.group(3) or "", m.group(4) or "")
            if amt0 is not None and amt0 > 0:
                out.append((amt0, "spend", None))
            continue
        filler, sym, digits, scale, tail = (m.group(1) or "", m.group(2) or "",
                                            m.group(3) or "", m.group(4) or "",
                                            m.group(5) or "")
        t = tail.lstrip()
        if t[:1] == "%" or re.match(r"(?i)^x(?![a-z])", t):
            continue                              # 'up to ~8.6% rate', 'up to 10X points'
        if _CAP_IS_RATE.search(tail):
            continue                              # 'up to 40 Reward Points for every Rs 100'
        amt = _amount(digits, scale)
        if amt is None or amt <= 0:
            continue
        unit = None
        if _CAP_PTS.match(tail):
            unit = "points"
        elif sym or _CAP_INR.match(tail):
            unit = "inr"
        per = _period_in(tail)
        if _CAP_SOFT.match(m.group(0)) and not sym and not per:
            continue          # a bare 'up to N points' with no period is prose, not a cap
        if _SPEND_BEFORE.search(name[max(0, m.start() - 26):m.start()]) or \
                _SPENDY.search(filler) or (unit is None and _SPENDY.search(tail)):
            unit = "spend"
        out.append((amt, unit, per[0] if len(per) == 1 else None))
    return out


def _channel_claims(name: str):
    if _BOTH_CHANNELS.search(name):
        return []                      # 'online and retail Landmark stores' spans both
    hits = []
    for key, rx in _CHANNELS:
        for m in rx.finditer(name):
            if _NEG_BEFORE.search(name[max(0, m.start() - 14):m.start()]):
                continue
            hits.append(key)
            break
    return sorted(set(hits))


"""'Earn 10 Reward Points on ...' — a points COUNT with no 'per Rs M' after it."""
_PT_COUNT = re.compile(r"(?<![\d.,%])(\d[\d,]*(?:\.\d+)?)\s+(?:" + _PT_WORDS + r")\b", re.I)


def _pt_count_claims(name: str):
    """Numerators only. Used solely to complete a point-value comparison whose
    denominator lives in reward_unit_spend rather than in the sentence, so it
    never raises a finding on its own — it only re-routes one from
    L5.RATE_CONTRADICTS_NAME to its actual root cause."""
    out = []
    for m in _PT_COUNT.finditer(name or ""):
        v = _amount(m.group(1))
        if v is not None and v > 0:
            out.append(v)
    return out


def _category_claims(name: str):
    return sorted({k for k, rx in _CAT_WORDS if rx.search(name)})


def _min_txn_claims(name: str):
    out = []
    for m in _MIN_TXN.finditer(name):
        v = _amount(m.group(1), m.group(2))
        if v is not None and v > 0:
            out.append(v)
    m = _BETWEEN.search(name)
    if m:
        lo = _amount(m.group(1))
        hi = _amount(m.group(2))
        if lo is not None and hi is not None and 0 < lo < hi:
            out.append(lo)
    return out


# --------------------------------------------------------------------------- #
# 4.  finding accumulator — one aggregated row per (card, defect class)
# --------------------------------------------------------------------------- #
_SPEC = {
    "L5.BASE_RULE_UNVERIFIABLE": dict(
        # INFO, not WARN. This is not a defect — it is the ABSENCE of evidence,
        # and its own fix text forbids the only edit that would clear it
        # ("NEVER edit the name to match the number"), so there was no action a
        # reader could take without issuer research. It was 261 of 1,105
        # warnings, the single biggest family, and it was what produced the
        # flagship sentence: removing it alone takes 'safe' cards from 0 to 29.
        # Manufacturing "0 of 383 cards are safe" out of a non-defect is exactly
        # the credibility failure that killed the grandfathered gate.
        #
        # Nothing is deleted and nothing is narrowed: every one of the 261 rows
        # is still found, still named, still counted. It is now counted where it
        # belongs — as coverage, alongside L8's issuer-sourced share, which
        # measures the same absence per rule instead of per card. "We have no
        # evidence" is a number to move, not a build to fail.
        severity=INFO, block="reward_rules",
        msg=lambda n: ("This card's base reward rule has no number in its name, so there is "
                       "nothing in the data that can confirm the rate the app shows is the "
                       "rate the bank actually pays. This is a gap in evidence, not a known "
                       "wrong number — the rate may well be right."),
        impact=("The base rate is the number every card falls back to and the one the whole "
                "list is ranked on. If it is wrong, this card sits in the wrong place in every "
                "recommendation and nothing in the file would ever say so."),
        fix=("Put the issuer's own wording in the rule name — 'Base reward rate' tells you "
             "nothing, '2 Reward Points per Rs 150 spent' can be checked. NEVER edit the name "
             "to match the number; that removes the only evidence and proves nothing."),
    ),
    "L5.RATE_UNVERIFIABLE": dict(
        severity=INFO, block="reward_rules",
        msg=lambda n: (f"{n} reward rule{'s' if n > 1 else ''} on this card have no rate in the "
                       f"rule name, so their value is UNVERIFIABLE from their own text."),
        impact=("Nobody can tell from the file whether these rules pay what the bank promises. "
                "A gate that only compares names to numbers is silently blind to them."),
        fix=("When the rule is next touched, paste the issuer's sentence into the name. Do not "
             "rewrite an existing name to match its number."),
    ),
    "L5.RATE_CONTRADICTS_NAME": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} reward rule{'s' if n > 1 else ''} on this card show a reward rate "
                       f"that does not match the rate written in the rule's own name."),
        impact=("The user sees a percentage the issuer never promised, and the card is ranked on "
                "that wrong number, so it can beat a card that would actually pay more."),
        fix=("Work out which is right from the issuer page, then correct reward_rate / "
             "reward_unit_spend / reward_type. Never 'fix' this by editing the rule name."),
    ),
    "L5.NAME_STATES_RATE_APP_HAS_NONE": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card state a reward rate in "
                       f"words, but the app cannot show any rate for them because this card's "
                       f"base_reward_rate is 0."),
        impact=("The card is shown as 'Rate not published' and is sorted below every card that "
                "does have a rate — even though the issuer's own wording, sitting right here in "
                "the file, says exactly what it pays."),
        fix=("Set the card's base_reward_rate (points per Rs 1, not a percentage) from the "
             "issuer's base earning, or store these rules as points_per_spend / cashback_pct so "
             "they no longer depend on a multiplier of zero."),
    ),
    "L5.NAME_IMPLIES_OTHER_POINT_VALUE": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} rule name{'s' if n > 1 else ''} on this card give both a points "
                       f"figure and a percentage, and the two only agree if a point is worth "
                       f"more (or less) than the value stored on the card."),
        impact=("Every rate on this card is computed from the stored point value, so if the "
                "issuer's own sentence implies a different one, every number the user sees on "
                "this card is off by the same multiple."),
        fix=("Settle the point value against the issuer's redemption page and set "
             "rp_value_standard (best unconditional channel only — never a travel portal or an "
             "airmiles transfer). Do not touch the rule name."),
    ),
    "L5.MULTIPLIER_TEXT_VS_RATE": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card say one multiplier in the "
                       f"name (like '5X') but carry a different multiplier in the field."),
        impact="The app multiplies the base rate by the stored number, so the user sees the wrong multiple.",
        fix="Confirm the multiple at the issuer and set reward_rate to it.",
    ),
    "L5.CAP_UNIT_MISMATCH": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} cap{'s' if n > 1 else ''} on this card are written in one unit in "
                       f"the rule name but counted in a different unit by the app."),
        impact=("The monthly ceiling bites at the wrong moment — either the user is cut off long "
                "before the bank would cut them off, or they are told they are still earning "
                "when the bank has already stopped."),
        fix=("Caps go in the issuer's own unit (points for a points card, rupees for a cashback "
             "card) and the rule's reward_type / cap_kind must match that unit. Fix the fields, "
             "not the sentence."),
    ),
    "L5.CAP_UNIT_LABEL_ONLY": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} cap{'s' if n > 1 else ''} on this card name points but are counted "
                       f"in rupees by the app. It happens to make no difference today because a "
                       f"point on this card is worth about one rupee."),
        impact=("Nothing is wrong on screen right now. The moment anyone corrects this card's "
                "point value, every one of these ceilings silently changes by the same multiple."),
        fix=("Store the cap in the issuer's unit and set reward_type / cap_kind so the app "
             "counts that unit, rather than relying on the point value being 1."),
    ),
    "L5.CAP_AMOUNT_MISMATCH": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} cap{'s' if n > 1 else ''} on this card store a different number "
                       f"from the cap written in the rule's own name."),
        impact="The progress bar and the cut-off both use the stored number, so the user is stopped at the wrong point.",
        fix="Check the issuer page and set cap_amount to the number the name states.",
    ),
    "L5.CAP_PERIOD_MISMATCH": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} cap{'s' if n > 1 else ''} on this card reset over a different "
                       f"period from the one the rule name states."),
        impact=("A yearly cap stored as monthly resets twelve times too often (and the reverse "
                "leaves the user capped for a whole year)."),
        fix="Set cap_period to the period in the name: month, cycle, quarter or year.",
    ),
    "L5.CAP_IN_TEXT_NOT_ENFORCED": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card describe a cap in words that "
                       f"the app cannot enforce, because cap_amount is not a plain number or "
                       f"cap_period is missing."),
        impact=("The app treats the rule as uncapped. It will keep promising the accelerated rate "
                "long after the bank has stopped paying it."),
        fix=("Put a bare number in cap_amount and a period in cap_period. Prose like "
             "'1000 points per month' is dropped silently by the app."),
    ),
    "L5.SPEND_TIER_NOT_MODELLED": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card change rate at a spend "
                       f"level rather than capping anything, and the app has no way to "
                       f"express a tier boundary."),
        impact=("The app applies one rate to every rupee. A user below the threshold is "
                "quoted the accelerated rate they have not earned yet, or a user above it "
                "is quoted the base rate when they should be on the higher one — depending "
                "on which of the two rules the engine reaches first."),
        fix=("Nothing to fix in cap_amount — it is correctly null, because nothing is "
             "capped. Modelling a tier needs spend_threshold_min / spend_threshold_max on "
             "both rules, and app work to read them."),
    ),
    "L5.MIN_TXN_IN_TEXT_ONLY": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card name a minimum transaction "
                       f"amount that is not stored in min_txn_amount."),
        impact=("The app recommends this card for small purchases that will not actually earn the "
                "accelerated rate. (Note the app does not read min_txn_amount today either, so "
                "filling it in is necessary but not sufficient.)"),
        fix="Store the number in min_txn_amount, and raise the fact that the app ignores that field.",
    ),
    "L5.CATEGORY_TAG_MISMATCH": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card end with a category tag in "
                       f"square brackets that does not match the category the rule is filed under."),
        impact="The rule fires for the wrong kind of purchase, or never fires at all.",
        fix="Make category_id equal the tag, or correct the tag if the tag is the wrong one.",
    ),
    "L5.CATEGORY_IN_TEXT_NOT_IN_FIELD": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} category rule{'s' if n > 1 else ''} on this card name a spending "
                       f"category in the rule name but carry no category the app can read."),
        impact=("The app drops a category rule that has no category, so this accelerated rate "
                "never reaches a user."),
        fix="Set category_id to the matching slug from the app's categories.json.",
    ),
    "L5.CHANNEL_TEXT_VS_FIELD": dict(
        severity=ERROR, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card name one payment channel in "
                       f"the rule name but are filed under a different channel."),
        impact="The rule fires on the wrong kind of payment, or never fires.",
        fix="Set channel to the one the name states.",
    ),
    "L5.CHANNEL_IN_TEXT_NOT_IN_FIELD": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card restrict the offer to one "
                       f"payment channel in words, but leave the channel field empty."),
        impact=("With no channel set the rule fires on every payment, so the app promises the "
                "accelerated rate on purchases the bank would pay the base rate on."),
        fix="Set channel to online, offline, upi or international to match the sentence.",
    ),
    "L5.DATE_IN_TEXT_NOT_IN_FIELD": dict(
        severity=WARN, block="reward_rules",
        msg=lambda n: (f"{n} rule{'s' if n > 1 else ''} on this card mention a start or end date "
                       f"in words, but the effective_date / expiry_date fields are empty."),
        impact=("A rule with an end date only in its name never expires. The app reads neither "
                "date field, so an offer that ended is still being recommended today."),
        fix=("Fill the date fields, and treat the missing expiry handling in the app as a "
             "separate piece of work — populating the field alone changes nothing."),
    ),
    "L5.MILESTONE_TARGET_MISMATCH": dict(
        severity=WARN, block="milestone_rules",
        msg=lambda n: (f"{n} milestone{'s' if n > 1 else ''} on this card store a spend target "
                       f"that does not match the amount written in the milestone's own text."),
        impact="The progress a user is shown towards a bonus is measured against the wrong target.",
        fix="Set spend_target to the amount the description states (in rupees, not lakhs).",
    ),
    "L5.MILESTONE_PERIOD_MISMATCH": dict(
        severity=WARN, block="milestone_rules",
        msg=lambda n: (f"{n} milestone{'s' if n > 1 else ''} on this card store a period that "
                       f"does not match the period in the milestone's own text."),
        impact="An annual milestone shown as monthly looks reachable when it is not.",
        fix="Set period to month, quarter or year to match the wording.",
    ),
    "L5.REDEMPTION_VALUE_VS_TEXT": dict(
        severity=WARN, block="redemption_rules",
        msg=lambda n: (f"{n} redemption channel{'s' if n > 1 else ''} on this card describe a "
                       f"rupee value per point that does not match point_value_inr."),
        impact="The redemption screen tells the user their points are worth something they are not.",
        fix="Set point_value_inr to the value the description states.",
    ),
}


class _Bag:
    def __init__(self):
        self.rows = defaultdict(list)   # (code, card_id) -> [(index, evidence)]

    def add(self, code, card_id, index, evidence):
        self.rows[(code, card_id)].append((index, evidence))

    def count(self, code):
        return sum(len(v) for (c, _), v in self.rows.items() if c == code)

    def findings(self):
        out = []
        for (code, cid), items in sorted(self.rows.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            spec = _SPEC.get(code)
            if not spec:
                continue
            n = len(items)
            ev = "; ".join(e for _, e in items[:3])
            if n > 3:
                ev += " … and %d more" % (n - 3)
            out.append(Finding(
                severity=spec["severity"],
                code=code,
                message=spec["msg"](n),
                card_id=cid,
                block=spec.get("block"),
                index=items[0][0] if n == 1 else None,
                evidence=trunc(ev, 320),
                fix=spec.get("fix"),
                impact=spec.get("impact"),
            ))
        return out


# --------------------------------------------------------------------------- #
# 5.  the check
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    bag = _Bag()
    stats = defaultdict(int)

    try:
        app_cats = ctx.app_category_names() or set()
    except Exception:
        app_cats = set()
    try:
        merch = ctx.merchant_slugs() or set()
    except Exception:
        merch = set()

    # ---------------- reward rules -------------------------------------- #
    for cid, inner, idx, rule in _safe_rows(ctx, "reward_rules"):
        try:
            _check_reward_rule(bag, stats, cid, inner, idx, rule, app_cats, merch)
        except Exception as exc:                       # one bad row never kills the layer
            stats["row_errors"] += 1
            bag.add("L5.RATE_UNVERIFIABLE", cid, idx,
                    "row %d could not be parsed (%s)" % (idx, type(exc).__name__))

    # ---------------- milestones ---------------------------------------- #
    for cid, inner, idx, row in _safe_rows(ctx, "milestone_rules"):
        try:
            _check_milestone(bag, stats, cid, idx, row)
        except Exception:
            stats["row_errors"] += 1

    # ---------------- redemption ---------------------------------------- #
    for cid, inner, idx, row in _safe_rows(ctx, "redemption_rules"):
        try:
            _check_redemption(bag, stats, cid, idx, row)
        except Exception:
            stats["row_errors"] += 1

    findings = bag.findings()
    findings.append(_coverage(stats, bag))
    if not app_cats:
        findings.append(Skipped(
            code="L5.CATEGORY_TAG_CROSSCHECK",
            what="No rule's [category] name tag was compared against the category the "
                 "rule is filed under, and no category named only in a rule's prose was "
                 "checked for a missing category_id.",
            reason="This run has no category vocabulary: there was no app checkout and "
                   "the vendored mirror at tools/app_mirror/categories.json could not be "
                   "read either.",
            impact="read the absence of tag findings as agreement between the names and "
                   "the fields. This comparison did not run — and when it was allowed to "
                   "run blind it produced 109 mismatches that were all artefacts of not "
                   "knowing what the app's categories are.",
            codes=("L5.CATEGORY_TAG_MISMATCH", "L5.CATEGORY_IN_TEXT_NOT_IN_FIELD"),
            restore="Restore tools/app_mirror/categories.json (python3 "
                    "tools/app_mirror/refresh.py --app-root ../KredMe-main), or pass "
                    "--app-root pointing at a KredMe-main checkout.",
        ))
    return findings


def _safe_rows(ctx: Ctx, block: str):
    try:
        for cid, inner, idx, row in ctx.rules(block):
            if isinstance(row, dict) and isinstance(inner, dict):
                yield cid, inner, idx, row
    except Exception:
        return


# --------------------------------------------------------------------------- #
def _check_reward_rule(bag, stats, cid, inner, idx, rule, app_cats, merch):
    name = _txt(rule.get("rule_name")).strip()
    stats["rules"] += 1
    if not name:
        stats["name_empty"] += 1

    rtype = _txt(rule.get("rule_type")).strip().lower()
    eng_pct, pv = _engine(inner, rule)

    # ---- 5.1  the rate the words claim vs the rate the app computes ------ #
    pps = _pps_claims(name)
    pcts = _pct_claims(name)
    mults = _mult_claims(name)

    claim = None
    kind = None
    if pps:
        n, d = pps[0]
        claim = (n / d) * pv * 100.0
        kind = "%s per Rs %s" % (_fmt(n), _fmt(d))
        stats["parsed_pps"] += 1
    elif pcts:
        claim = pcts[0]
        kind = "%s%%" % _fmt(pcts[0])
        stats["parsed_pct"] += 1

    pv_conflict = False
    # The sentence gives both "N points per Rs M" and "P%". Those two only agree at one
    # point value. If that is not the point value the card stores, the point value is the
    # thing under suspicion — not the rate — so say that instead of crying "wrong rate".
    #
    # The denominator does not have to be in the NAME. This guard used to require
    # both halves in the text, so when the name said "Earn 10 Reward Points ...
    # 1.25%" and the Rs-per-point denominator lived in the reward_unit_spend
    # FIELD, the guard was blind and the row was reported as a wrong RATE. Both
    # yes_bank_marquee rows flagged that way are pure point-value disagreements.
    # The field is the same number the app itself divides by, so reading it here
    # is not a guess.
    n_claim = d_claim = None
    if pps:
        n_claim, d_claim = pps[0]
    elif pcts:
        pts = _pt_count_claims(name)
        unit = num(rule.get("reward_unit_spend"))
        if pts and unit and unit > 0:
            n_claim, d_claim = pts[0], unit
    if n_claim and pcts:
        n0, d0 = n_claim, d_claim
        implied = (pcts[0] * d0) / (n0 * 100.0) if n0 > 0 else None
        if implied is not None and not _close(implied, pv, rel=0.10, tol=0.005):
            pv_conflict = True
            bag.add("L5.NAME_IMPLIES_OTHER_POINT_VALUE", cid, idx,
                    "%s: %s points per Rs %s and %s%% only agree at Rs %s per point; card "
                    "stores rp_value_standard = %s%s"
                    % (trunc(name, 64), _fmt(n0), _fmt(d0), _fmt(pcts[0]),
                       _fmt(implied), inner.get("rp_value_standard"),
                       "" if pps else " (spend-per-point read from reward_unit_spend, "
                                      "which the name does not state)"))

    if pv_conflict:
        # already reported, at its root cause — do not also report it as a wrong rate
        stats["rate_checked"] += 1
        stats["rate_pv_conflict"] += 1
    elif claim is not None:
        stats["rate_checked"] += 1
        if eng_pct is None:
            stats["rate_app_unknown"] += 1
            bag.add("L5.NAME_STATES_RATE_APP_HAS_NONE", cid, idx,
                    "%s: name says %s (about %s%%), app shows 'Rate not published'"
                    % (trunc(name, 70), kind, _fmt(claim)))
        elif not _close(claim, eng_pct):
            extra = ""
            if rtype == "base_rate":
                extra = " (card field implies %s%%)" % _fmt(card_base_pct(inner))
            bag.add("L5.RATE_CONTRADICTS_NAME", cid, idx,
                    "%s: name says %s = about %s%%, app computes %s%%%s"
                    % (trunc(name, 70), kind, _fmt(claim), _fmt(eng_pct), extra))
        else:
            stats["rate_agrees"] += 1
    elif mults and _txt(rule.get("reward_type")).strip().lower() == "multiplier":
        stats["parsed_mult"] += 1
        rate = num(rule.get("reward_rate"))
        if len(set(mults)) == 1 and rate is not None and not _close(mults[0], rate, rel=0.01, tol=0.001):
            bag.add("L5.MULTIPLIER_TEXT_VS_RATE", cid, idx,
                    "%s: name says %sX, field says %sX"
                    % (trunc(name, 70), _fmt(mults[0]), _fmt(rate)))
        else:
            stats["rate_agrees"] += 1
        stats["rate_checked"] += 1
    else:
        # ---- 5.2  THE NUMBER THAT MATTERS: no rate in the name at all ---- #
        stats["unverifiable"] += 1
        if rtype == "base_rate":
            stats["unverifiable_base"] += 1
            bag.add("L5.BASE_RULE_UNVERIFIABLE", cid, idx,
                    "base rule named %s; app shows %s%%"
                    % (trunc(name or "(blank)", 60), _fmt(eng_pct)))
        else:
            bag.add("L5.RATE_UNVERIFIABLE", cid, idx,
                    "%s (app shows %s%%)" % (trunc(name or "(blank)", 60), _fmt(eng_pct)))

    # ---- 5.3  caps ------------------------------------------------------ #
    caps = _cap_claims(name)
    if caps:
        stats["parsed_cap"] += 1
        cap_amt = num(rule.get("cap_amount"))
        cap_raw = rule.get("cap_amount")
        cap_per = _txt(rule.get("cap_period")).strip().lower() or None
        eng_unit = _engine_cap_unit(rule)

        units = {u for _, u, _ in caps if u}
        periods = {p for _, _, p in caps if p}
        amounts = {a for a, u, _ in caps if u != "spend"}

        if cap_amt is None or cap_per is None:
            if units == {"spend"} and _spend_tier_spans(name):
                # Not a ceiling at all — a spend TIER the app has no way to
                # model. Reported as its own thing rather than as a cap the app
                # is failing to enforce, which sent the reader hunting for a cap
                # that was never there. Still reported: a tier the engine cannot
                # express is a real gap, it is just a different one.
                bag.add("L5.SPEND_TIER_NOT_MODELLED", cid, idx,
                        "%s: %s — the rate changes at this spend level; nothing is capped"
                        % (trunc(name, 70),
                           ", ".join("Rs %s" % _fmt(a) for a, u, _ in caps if u == "spend")))
            else:
                bag.add("L5.CAP_IN_TEXT_NOT_ENFORCED", cid, idx,
                        "%s: cap_amount=%s cap_period=%s"
                        % (trunc(name, 70), trunc(cap_raw, 46), cap_per))
        else:
            unit_flagged = False
            if len(units) == 1:
                u = units.pop()
                factor = (1.0 / pv) if pv else None
                if u == "points" and eng_unit == "inr":
                    unit_flagged = True
                    if factor is not None and 0.91 <= factor <= 1.1:
                        bag.add("L5.CAP_UNIT_LABEL_ONLY", cid, idx,
                                "%s: cap_amount=%s, reward_type=%s, point value Rs %s"
                                % (trunc(name, 70), _fmt(cap_amt),
                                   _txt(rule.get("reward_type")), _fmt(pv)))
                    else:
                        bag.add("L5.CAP_UNIT_MISMATCH", cid, idx,
                                "%s: name caps points, app counts rupees of cashback "
                                "(cap_amount=%s, reward_type=%s) — the ceiling is about %sx "
                                "the one the bank states"
                                % (trunc(name, 70), _fmt(cap_amt),
                                   _txt(rule.get("reward_type")), _fmt(factor)))
                elif u == "inr" and eng_unit == "points":
                    unit_flagged = True
                    bag.add("L5.CAP_UNIT_MISMATCH", cid, idx,
                            "%s: name caps rupees, app counts points "
                            "(cap_amount=%s, reward_type=%s, point value Rs %s)"
                            % (trunc(name, 70), _fmt(cap_amt),
                               _txt(rule.get("reward_type")), _fmt(pv)))
                elif u == "spend" and eng_unit != "spend":
                    unit_flagged = True
                    bag.add("L5.CAP_UNIT_MISMATCH", cid, idx,
                            "%s: name caps eligible SPEND, app caps the reward "
                            "(cap_amount=%s, cap_kind=%s)"
                            % (trunc(name, 70), _fmt(cap_amt),
                               rule.get("cap_kind") or "(absent, defaults to reward)"))
            if len(amounts) == 1 and not unit_flagged:
                a = amounts.pop()
                if not _close(a, cap_amt, rel=0.02, tol=0.5):
                    bag.add("L5.CAP_AMOUNT_MISMATCH", cid, idx,
                            "%s: name says %s, cap_amount=%s"
                            % (trunc(name, 70), _fmt(a), _fmt(cap_amt)))
            if len(periods) == 1:
                p = periods.pop()
                if _BUCKET.get(p, "month") != _BUCKET.get(cap_per, "month"):
                    bag.add("L5.CAP_PERIOD_MISMATCH", cid, idx,
                            "%s: name says per %s, cap_period=%s" % (trunc(name, 70), p, cap_per))

    # ---- 5.4  minimum transaction --------------------------------------- #
    mins = _min_txn_claims(name)
    if mins:
        stats["parsed_min_txn"] += 1
        if num(rule.get("min_txn_amount")) is None and \
                num(rule.get("spend_threshold_min")) is None:
            bag.add("L5.MIN_TXN_IN_TEXT_ONLY", cid, idx,
                    "%s: name says minimum Rs %s, min_txn_amount=%s"
                    % (trunc(name, 70), _fmt(mins[0]), rule.get("min_txn_amount")))

    # ---- 5.5  category --------------------------------------------------- #
    # Both halves of this section are comparisons AGAINST the app's category
    # vocabulary, and neither means anything without it. With app_cats empty the
    # first branch could never match, so every tagged rule fell through to the
    # "neither an app category nor this rule's merchant" arm and was reported as
    # a mismatch — 109 of them, every one an artefact of not knowing what the
    # app's categories are. The second half fails the opposite way, silently
    # finding nothing. Neither is a fact about the data, so neither runs.
    # run() declares this as L5.CATEGORY_TAG_CROSSCHECK.
    cat_id = rule.get("category_id")
    cat_id = cat_id if isinstance(cat_id, str) and cat_id else None
    tag = _TAG.search(name) if app_cats else None
    if not app_cats:
        pass
    elif tag:
        stats["parsed_cat_tag"] += 1
        t = tag.group(1)
        if t in app_cats:
            if cat_id != t:
                bag.add("L5.CATEGORY_TAG_MISMATCH", cid, idx,
                        "%s: tag [%s], category_id=%s" % (trunc(name, 70), t, cat_id))
        elif merch and t not in merch and _txt(rule.get("merchant_ref")) != t:
            bag.add("L5.CATEGORY_TAG_MISMATCH", cid, idx,
                    "%s: tag [%s] is neither an app category nor this rule's merchant (%s)"
                    % (trunc(name, 70), t, rule.get("merchant_ref")))
    elif rtype == "category_bonus" and cat_id is None:
        cats = _category_claims(name)
        stats["parsed_cat_word"] += 1 if cats else 0
        if len(cats) == 1 and cats[0] in app_cats:
            bag.add("L5.CATEGORY_IN_TEXT_NOT_IN_FIELD", cid, idx,
                    "%s: name reads as '%s', category_id is empty (category_ref=%s)"
                    % (trunc(name, 70), cats[0], trunc(rule.get("category_ref"), 40)))

    # ---- 5.6  channel ---------------------------------------------------- #
    chans = _channel_claims(name)
    if len(chans) == 1:
        want = chans[0]
        have = _txt(rule.get("channel")).strip().lower() or None
        skip = (want == "online" and cat_id == "online_shopping") or \
               (want == "portal" and rtype == "portal_bonus")
        if not skip:
            stats["parsed_channel"] += 1
            if have and have != want:
                bag.add("L5.CHANNEL_TEXT_VS_FIELD", cid, idx,
                        "%s: name reads '%s', channel=%s" % (trunc(name, 70), want, have))
            elif have is None and want in ("online", "upi", "offline", "international"):
                bag.add("L5.CHANNEL_IN_TEXT_NOT_IN_FIELD", cid, idx,
                        "%s: name reads '%s', channel is empty" % (trunc(name, 70), want))

    # ---- 5.7  dates ------------------------------------------------------ #
    starts = _D_START.search(name)
    ends = _D_END.search(name)
    iso = _D_ISO.search(name)
    if starts or ends or iso:
        stats["parsed_date"] += 1
        eff = rule.get("effective_date")
        exp = rule.get("expiry_date")
        if (starts or iso) and not iso_ok(eff):
            bag.add("L5.DATE_IN_TEXT_NOT_IN_FIELD", cid, idx,
                    "%s: effective_date=%s" % (trunc(name, 80), eff))
        if ends and not iso_ok(exp):
            bag.add("L5.DATE_IN_TEXT_NOT_IN_FIELD", cid, idx,
                    "%s: name states an end date, expiry_date=%s" % (trunc(name, 80), exp))


# --------------------------------------------------------------------------- #
def _check_milestone(bag, stats, cid, idx, row):
    name = _txt(row.get("milestone_name")).strip()
    desc = _txt(row.get("bonus_description")) or _txt(row.get("reward_description"))
    text = (name + ". " + desc).strip()
    stats["milestones"] += 1
    if not text:
        return

    target = num(row.get("spend_target"))
    claims = []
    for rx in (_MS_SPEND, _MS_SPEND_ALT):
        for m in rx.finditer(text):
            v = _amount(m.group(1), m.group(2))
            if v is not None and v >= 500:
                claims.append(v)
    if claims and _MS_INCREMENTAL.search(text):
        stats["ms_target_ambiguous"] += 1          # cumulative-vs-incremental: claim nothing
    elif claims:
        stats["parsed_ms_target"] += 1
        best = claims[0]
        if target is None or target <= 0:
            bag.add("L5.MILESTONE_TARGET_MISMATCH", cid, idx,
                    "%s: text says Rs %s, spend_target=%s"
                    % (trunc(text, 80), _fmt(best), row.get("spend_target")))
        elif not any(_close(c, target, rel=0.02, tol=1.0) for c in claims):
            bag.add("L5.MILESTONE_TARGET_MISMATCH", cid, idx,
                    "%s: text says Rs %s, spend_target=%s"
                    % (trunc(text, 80), _fmt(best), _fmt(target)))
    else:
        stats["ms_target_unverifiable"] += 1

    per = _txt(row.get("period")).strip().lower() or None
    hits = _period_in(text)
    hits = [h for h in hits if h in ("month", "quarter", "year")]
    if _MS_ODD_PERIOD.search(text):
        hits = []
    if len(set(hits)) == 1 and per in ("month", "quarter", "year"):
        stats["parsed_ms_period"] += 1
        if hits[0] != per:
            bag.add("L5.MILESTONE_PERIOD_MISMATCH", cid, idx,
                    "%s: text says %s, period=%s" % (trunc(text, 80), hits[0], per))


# --------------------------------------------------------------------------- #
def _check_redemption(bag, stats, cid, idx, row):
    text = (_txt(row.get("channel_description")) + " " + _txt(row.get("rule_name"))).strip()
    stats["redemption_rows"] += 1
    if not text:
        return
    m = _PER_POINT.search(text)
    if not m:
        return
    stats["parsed_redemption"] += 1
    if m.group(1) is not None:
        claim = _amount(m.group(1))
    else:
        p = _amount(m.group(2))
        claim = None if p is None else p / 100.0
    have = num(row.get("point_value_inr"))
    if claim is None:
        return
    if have is None or not _close(claim, have, rel=0.02, tol=0.001):
        bag.add("L5.REDEMPTION_VALUE_VS_TEXT", cid, idx,
                "%s: text says Rs %s per point, point_value_inr=%s"
                % (trunc(text, 80), _fmt(claim), row.get("point_value_inr")))


# --------------------------------------------------------------------------- #
def _coverage(stats, bag) -> Finding:
    rules = stats.get("rules", 0)
    unver = stats.get("unverifiable", 0)
    checked = stats.get("rate_checked", 0)
    base_unver = stats.get("unverifiable_base", 0)
    pct = (100.0 * checked / rules) if rules else 0.0
    msg = (
        "Read the reward rate out of the rule name on %d of %d reward rules (%.1f%%). "
        "The other %d name no rate at all, so their value is UNVERIFIABLE from their own "
        "text — including %d card base rules, which is why a name-vs-number gate can look "
        "green while proving nothing about them."
        % (checked, rules, pct, unver, base_unver)
    )
    ev = ("points-per-spend claims %d; percent claims %d; multiplier-only claims %d; "
          "rates that agree %d; app could not compute a rate at all %d; "
          "cap sentences %d; minimum-transaction sentences %d; channel words %d; "
          "category tags %d; date words %d; milestones with a readable target %d of %d; "
          "redemption values read %d of %d; rows that threw %d"
          % (stats.get("parsed_pps", 0), stats.get("parsed_pct", 0),
             stats.get("parsed_mult", 0), stats.get("rate_agrees", 0),
             stats.get("rate_app_unknown", 0), stats.get("parsed_cap", 0),
             stats.get("parsed_min_txn", 0), stats.get("parsed_channel", 0),
             stats.get("parsed_cat_tag", 0), stats.get("parsed_date", 0),
             stats.get("parsed_ms_target", 0), stats.get("milestones", 0),
             stats.get("parsed_redemption", 0), stats.get("redemption_rows", 0),
             stats.get("row_errors", 0)))
    return Finding(
        severity=INFO,
        code="L5.PARSER_COVERAGE",
        message=msg,
        block="reward_rules",
        evidence=trunc(ev, 500),
        impact=("Everything this layer did not parse is unchecked, not clean. Treat the "
                "unverifiable count as the size of the evidence gap, not as a pass."),
        fix=("Raise coverage by pasting the issuer's own sentence into rule_name when a rule is "
             "next edited. Never by renaming a rule to match the number already stored."),
    )
