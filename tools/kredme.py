#!/usr/bin/env python3
"""
kredme.py — dev/prod data pipeline for the KredMe OTA backend.

THE MODEL
---------
Two parallel data environments, one per git branch:

    branch `dev`   -> DEV data
                      https://raw.githubusercontent.com/kredme-data/kredme-data/dev
                      read by TestFlight builds and dev APKs.
                      Safe to break: no store user ever reads it.

    branch `main`  -> PROD data
                      https://kredme-data.github.io/kredme-data
                      read by every App Store / Play Store build, always.

GitHub Pages can only serve ONE branch, which is why dev is served from
raw.githubusercontent (branch-addressable) rather than a Pages path.

    .published/    -> automatic snapshots of prod, taken before every promote.
                      This is what `undo` restores. Git-ignored.

Data flows one way: edit on dev -> test it on your phone -> promote to prod.

COMMANDS
--------
    python3 tools/kredme.py status               dev vs prod, and what is live
    python3 tools/kredme.py validate --target dev    check dev before testing
    python3 tools/kredme.py validate --target prod   check what users have now
    python3 tools/kredme.py promote --dry-run    show what prod would receive
    python3 tools/kredme.py promote              dev -> prod (still local)
    python3 tools/kredme.py undo                 restore the previous prod data

Nothing here touches the network. Commands write local files and print the git
command that actually reaches users, so publishing stays a deliberate act.

No third-party packages. Python 3.8+. Run from anywhere.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

LIVE_SEED = REPO / "seed"
LIVE_NEWS = REPO / "news"
SNAPSHOTS = REPO / ".published"

DEV_BRANCH = "dev"
PROD_BRANCH = "main"

# DEV data is the `dev` branch, served by raw.githubusercontent (branch-addressable).
# PROD data is `main`, served by GitHub Pages. Pages can only serve ONE branch,
# which is why dev uses raw rather than a Pages path.
DEV_BASE = "https://raw.githubusercontent.com/kredme-data/kredme-data/dev"
PROD_BASE = "https://kredme-data.github.io/kredme-data"

SEED_FILES = ("cards.json", "merchants.json")
MANIFEST = "manifest.json"
FEED = "feed.json"

# Keys the app's NewsArticle.fromJson actually reads. Anything else is silently
# dropped by the app, which is how the live feed ended up invisible.
NEWS_VALID_KEYS = {
    "id", "title", "summary", "category", "severity", "source", "source_url",
    "published_at", "expiry_date", "affected_cards", "affected_issuers",
    "tags", "action_text",
}
# Wrong key -> what the app actually wants. These are real mistakes already in
# the live feed, not hypotheticals.
NEWS_KEY_FIXES = {
    "expires_at": "expiry_date",
    "url": "source_url",
    "link": "source_url",
    "body": "summary",
    "description": "summary",
}
NEWS_SEVERITIES = {"negative", "warning", "positive", "info"}
NEWS_REQUIRED = ("id", "title", "summary")

# --- card economics thresholds ---------------------------------------------
# No Indian credit card earns more than ~10% effective on any category, and a
# card earning less than 0.1% is a data error rather than a bad product. Both
# bounds exist because the SAME unit bug produces both: Axis Neo renders 0.02%
# (5x too low) and Axis Cashback renders 75% (100x too high).
RATE_CEILING_PCT = 10.0
RATE_FLOOR_PCT = 0.1
# The ceiling above is ratcheted — live already violates it in 61 places, so a
# pre-existing breach is a warning. This one is not. Nothing may EVER render
# above 40%. The line is set on evidence, not a round number: HDFC's SmartBuy
# 10X on Diners Club Black genuinely reaches ~33%, so a 30% ceiling would block
# a real product. Nothing legitimate clears 40%, and every case observed above
# it has been a unit bug. There is deliberately no baseline key for this check
# — unlike every other one here, it cannot be grandfathered.
HARD_CEILING_PCT = 40.0
# How far a rule's rendered rate may drift from the rate its OWN sentence
# states before we call it a contradiction. 1.6x is loose enough to absorb
# rounding and issuer marketing ("up to ~8.6%") and tight enough to catch every
# known mechanism, all of which are off by 4x or more.
SELF_CONTRADICTION_RATIO = 1.6
# Below this, a disagreement is arithmetic noise on two tiny numbers.
SELF_CONTRADICTION_FLOOR_PCT = 0.4
# How far a `base_rate` RULE may drift from the card's own `base_reward_rate`
# FIELD before we call it a unit bug. The two describe the same thing and the
# app renders whichever is higher, so a disagreement means the user is shown a
# number the card's own data contradicts.
#
# This is the check that would have caught Paytm HDFC Mobile's 15%. That rule
# was named "Base reward rate" — no number in it — so check 7 had nothing to
# parse and stayed silent while the card sat baselined and green. 271 of 279
# base_rate rules carry no number in the name, so check 7 is structurally blind
# to 97% of them; this one needs no name at all.
#
# The signature is unmistakable: a base rule typed `cashback_pct` holding a
# points-per-rupee number renders it as a percent, so the drift is almost
# always EXACTLY 1/rp_value_standard — 4x at Rs.0.25, 5x at Rs.0.20, 10x at
# Rs.0.10. 1.25 absorbs rounding on cards that state a base like 1.33%.
BASE_RULE_UNIT_RATIO = 1.25
# Two tiny numbers can differ by a large ratio and still be noise.
BASE_RULE_UNIT_FLOOR_PCT = 0.05
# Mirrors CreditCardData.sanePointValue (credit_card.dart:533) and the
# `?? 0.25` fallback in fromOtaJson (credit_card.dart:766). If the app changes
# either, this gate starts measuring the wrong thing.
APP_POINT_VALUE_DEFAULT = 0.25
APP_POINT_VALUE_MAX = 1.5
# Spend categories whose exclusions live in free text the engine cannot read
# (987 of 1,418 exclusion rules are type `other`). A high rate on one of these
# is how "75% cashback at Indian Oil" reached users.
PROSE_EXCLUDED_HINTS = (
    "fuel", "petrol", "diesel", "rent", "emi", "cash withdrawal", "cash advance",
    "wallet", "wallet load", "wallet reload",
)
RATE_BASELINE = REPO / "tools" / "rate_baseline.json"


# ---------------------------------------------------------------- output ----

class C:
    on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    R = "\033[31m" if on else ""
    G = "\033[32m" if on else ""
    Y = "\033[33m" if on else ""
    B = "\033[34m" if on else ""
    DIM = "\033[2m" if on else ""
    BOLD = "\033[1m" if on else ""
    X = "\033[0m" if on else ""


def head(msg): print(f"\n{C.BOLD}{msg}{C.X}")
def ok(msg): print(f"  {C.G}OK{C.X}   {msg}")
def warn(msg): print(f"  {C.Y}WARN{C.X} {msg}")
def err(msg): print(f"  {C.R}FAIL{C.X} {msg}")
def info(msg): print(f"  {C.DIM}·{C.X}    {msg}")
def die(msg, code=1):
    print(f"\n{C.R}{C.BOLD}Stopped:{C.X} {msg}\n")
    sys.exit(code)


# ----------------------------------------------------------------- utils ----

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def rel(path: Path) -> str:
    """Display path — materialised git refs live outside the repo."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return f"{path.parent.name}/{path.name}"


def size_str(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f}KB"
    return f"{n/1024**2:.2f}MB"


class VersionError(ValueError):
    """A version string we refuse to guess at. Guessing here corrupts live."""


def version_tuple(v, *, strict: bool = False):
    """Leading numeric components of a version string. '0.0.1-test' -> (0,0,1).

    A leading 'v' is tolerated ('v7.0.0' -> (7,0,0)) because silently reading
    that as 0 once caused a live news version to be reset to 1.0.0.

    strict=True raises VersionError instead of falling back to (0,). Publish
    MUST use strict: if we cannot read the current live version we cannot know
    what to bump to, and emitting a LOWER version permanently stops the app
    from refetching (it only refetches when the leading integer increases).
    """
    if v is None or isinstance(v, bool):
        if strict:
            raise VersionError(f"version is {v!r}")
        return (0,)
    s = str(v).strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    parts = re.split(r"[.\-+]", s)
    out = []
    for p in parts:
        if p.isdigit():
            out.append(int(p))
        else:
            break
    if not out:
        if strict:
            raise VersionError(f"cannot read a number from version {v!r}")
        return (0,)
    return tuple(out)


def version_str(t) -> str:
    t = list(t)
    while len(t) < 3:
        t.append(0)
    return ".".join(str(x) for x in t[:3])


def bump_major(v, *, strict: bool = False) -> str:
    """News: the app refetches ONLY when int(version.split('.')[0]) increases."""
    return f"{version_tuple(v, strict=strict)[0] + 1}.0.0"


def bump_patch(v, *, strict: bool = False) -> str:
    """Seed: the app compares the version string, so any change syncs."""
    t = list(version_tuple(v, strict=strict))
    while len(t) < 3:
        t.append(0)
    t[2] += 1
    return version_str(t)


def version_gt(a, b) -> bool:
    """Is a strictly newer than b? Compares padded numeric components."""
    ta, tb = list(version_tuple(a)), list(version_tuple(b))
    n = max(len(ta), len(tb), 3)
    ta += [0] * (n - len(ta))
    tb += [0] * (n - len(tb))
    return tuple(ta) > tuple(tb)


def git(*args: str):
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args],
                           capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:  # git missing / not a repo — never fatal
        return 1, "", str(e)


def git_bytes(*args: str):
    """git output as RAW bytes — never stripped.

    The text helper calls .strip(), which alters file content and makes every
    checksum comparison fail. Anything reading a blob must use this.
    """
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args],
                           capture_output=True, timeout=60)
        return r.returncode, r.stdout
    except Exception:
        return 1, b""


def git_head() -> str:
    code, out, _ = git("rev-parse", "HEAD")
    return out if code == 0 else "unknown"


def git_branch() -> str:
    code, out, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    return out if code == 0 else "unknown"


# ------------------------------------------------------------ validation ----

class Report:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # Populated by validate_economics so callers can persist a new baseline
        # without re-parsing the 1.9 MB catalog.
        self.economics: dict | None = None
        self.integrity: dict | None = None

    def error(self, msg): self.errors.append(msg); err(msg)
    def warn(self, msg): self.warnings.append(msg); warn(msg)

    @property
    def failed(self) -> bool:
        return bool(self.errors)


def _iso_ok(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    v = value.strip().replace("Z", "+00:00")
    try:
        _dt.datetime.fromisoformat(v)
        return True
    except ValueError:
        return False


def live_card_count() -> int:
    """How many cards live serves right now — the baseline for shrink detection."""
    try:
        cards = read_json(LIVE_SEED / "cards.json")
        if not isinstance(cards, list):
            return 0
        n = 0
        for e in cards:
            if isinstance(e, dict):
                inner = e.get("card") if isinstance(e.get("card"), dict) else e
                if inner.get("id"):
                    n += 1
        return n
    except Exception:
        return 0


# ------------------------------------------------------- card economics ----
#
# Everything above proves the FILE is well-formed. None of it reads a number.
# That gap is why every wrong rate that ever reached a user passed validation
# cleanly: 6 cards rendering 20-75% cashback, 106 cards rendering 0.00%, and
# Axis Bank Cashback advertising 75% at petrol pumps where it earns nothing.
#
# These checks replicate the APP's own display maths, because a rate is only
# wrong if the app RENDERS it wrong:
#
#     baseReward   = base_reward_rate * sanePointValue(rp_value_standard) * 100
#     rateForRule  = per reward_type          (credit_card.dart:538-574)
#
# Keep them in step with lib/shared/models/credit_card.dart. If the app changes
# a formula and this does not, the gate measures the wrong thing and goes quiet.
#
# THE RATCHET. The live catalog already violates these bounds in 61 places, so
# a gate that simply failed would block every publish forever and get disabled
# within a week. Instead it compares against a committed baseline
# (tools/rate_baseline.json): a NEW violation is fatal, a pre-existing one is
# reported and counted, and the count is not allowed to grow. Fix a card, run
# `--update-baseline`, and the floor moves down permanently.


def _fnum(v):
    """A JSON number, or None. Booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return None if f != f else f  # NaN


def _sane_point_value(v):
    """CreditCardData.sanePointValue — out-of-range point values collapse to 0.25."""
    f = _fnum(v)
    if f is None or f <= 0 or f > APP_POINT_VALUE_MAX:
        return APP_POINT_VALUE_DEFAULT
    return f


def card_base_pct(inner: dict) -> float:
    """The base earn % the app shows for this card (credit_card.dart:489)."""
    rp = _fnum(inner.get("rp_value_standard"))
    if rp is None:
        rp = APP_POINT_VALUE_DEFAULT          # fromOtaJson `?? 0.25`
    brr = _fnum(inner.get("base_reward_rate")) or 0.0
    return brr * _sane_point_value(rp) * 100


def rule_pct(rule: dict, inner: dict, base_pct: float) -> float:
    """The % the app shows for one reward rule (credit_card.dart rateForRule)."""
    rtype = rule.get("reward_type")
    rate = _fnum(rule.get("reward_rate")) or 0.0
    rp = _fnum(inner.get("rp_value_standard"))
    if rp is None:
        rp = APP_POINT_VALUE_DEFAULT
    pv_raw = rule.get("point_value")
    pv = _sane_point_value(pv_raw if _fnum(pv_raw) is not None else rp)

    if rtype == "cashback_pct":
        return rate * 100
    if rtype == "multiplier":
        brr = _fnum(inner.get("base_reward_rate")) or 0.0
        return rate * brr * pv * 100
    if rtype == "points_per_spend":
        unit = _fnum(rule.get("reward_unit_spend")) or 0.0
        if unit <= 0:
            return base_pct                    # app's division-by-zero guard
        return (rate / unit) * pv * 100
    return base_pct


# ---------------------------------------------------------------- self-consistency
#
# Every check above asks "is this number plausible?". This one asks a stricter
# and more useful question: "does this number agree with the sentence sitting
# next to it?"
#
# Almost every reward rule carries the issuer's own mechanics in `rule_name`:
#
#     "24 reward points on every Rs. 150 spent on fuel at IndianOil outlets"
#
# At this card's point value (Rs.0.20) that sentence says 3.2%. The stored
# number renders 24%. The file contradicts itself, and no issuer website is
# needed to prove it — which is what makes this check cheap enough to run on
# every publish and impossible to argue with.
#
# The parsers are deliberately narrow. A rule whose prose we cannot read
# confidently is skipped, never guessed at: a false accusation here blocks a
# publish, so silence is the safe failure mode.

_CL_NUM = r"(\d+(?:\.\d+)?)"
_CL_RS = r"(?:Rs\.?\s*|₹\s*|INR\s*)"
# "24 reward points on every Rs. 150" / "(24 Points per ₹100)" / "40 RP per Rs 200"
_CL_POINTS_PER_SPEND = re.compile(
    _CL_NUM + r"\s*(?:reward\s+)?(?:points?|RP)\b[^.;]{0,45}?"
    r"(?:per|on every|for every|for each|on each|each|every|/)\s*" + _CL_RS + r"(\d[\d,]*)", re.I)
# "reward rate of 2.5%" / "up to ~8.6% reward rate" — the issuer's own effective rate
_CL_EFFECTIVE = re.compile(
    r"(?:reward\s+rate\s+of\s*~?\s*" + _CL_NUM + r"\s*%"
    r"|~?\s*" + _CL_NUM + r"\s*%\s*(?:reward|earn)\s+rate)", re.I)
# "5% cashback on ..." / "7% back on ..."
_CL_CASHBACK = re.compile(
    _CL_NUM + r"\s*%\s*(?:cash\s*back|cashback|back\b|discount)", re.I)
# "50% more Reward Points" / "50% extra points" — a RELATIVE uplift on the base
# earn, not an absolute rate. Stored as 0.5 it renders 50% cashback; the issuer
# means base x 1.5. HDFC Superia, Solitaire and Doctors Superia all ship this.
_CL_RELATIVE = re.compile(
    _CL_NUM + r"\s*%\s*(?:more|extra|additional|bonus)\s+(?:reward\s+)?points?", re.I)
# "5X Reward Points" / "10X points" — a multiple of the base earn. Stored as an
# absolute rate it renders 5X as 500%, or as here 0.2 -> 20%.
_CL_MULTIPLE = re.compile(
    r"\b" + _CL_NUM + r"\s*X\s+(?:accelerated\s+)?(?:reward\s+)?points?", re.I)


def claimed_pct(rule: dict, inner: dict, base_pct: float = 0.0) -> tuple[float | None, str]:
    """The rate this rule's own sentence claims, and how we read it.

    Returns (None, "") when the prose states nothing we can parse — which is
    most rules, and is fine. Skipping is always safe; guessing never is.

    Order matters: an issuer-stated effective rate ("reward rate of 8.6%") is
    the strongest signal and wins over a raw points count in the same sentence.
    """
    name = rule.get("rule_name") or ""
    rp = _fnum(inner.get("rp_value_standard"))
    pv = _sane_point_value(rp if rp is not None else APP_POINT_VALUE_DEFAULT)

    m = _CL_EFFECTIVE.search(name)
    if m:
        return float(next(g for g in m.groups() if g)), "states an effective reward rate"

    m = _CL_POINTS_PER_SPEND.search(name)
    if m:
        pts, spend = float(m.group(1)), float(m.group(2).replace(",", ""))
        if spend > 0:
            return (pts * pv / spend) * 100, f"{pts:g} pts per Rs.{spend:g} at Rs.{pv:g}/pt"

    # Relative forms need the card's base earn to mean anything. With no base
    # rate there is no claim to test, so stay silent rather than invent one.
    if base_pct > 0:
        m = _CL_RELATIVE.search(name)
        if m:
            up = float(m.group(1))
            return base_pct * (1 + up / 100), f"{up:g}% more than a {base_pct:.2f}% base"

        m = _CL_MULTIPLE.search(name)
        if m:
            mult = float(m.group(1))
            return base_pct * mult, f"{mult:g}x a {base_pct:.2f}% base"

    m = _CL_CASHBACK.search(name)
    if m:
        return float(m.group(1)), "states a cashback percentage"

    return None, ""


def _rule_key(cid: str, rule: dict) -> str:
    """Stable-enough identity for a rule: rule_name survives reordering, index does not."""
    return f"{cid}::{(rule.get('rule_name') or '').strip()[:80]}"


def scan_economics(cards: list) -> dict:
    """Compute every number the gate reasons about. Pure — no reporting."""
    over_cards, under_cards, zero_cards = [], [], []
    over_rules, prose_risk = [], []
    hard_ceiling, contradictions = [], []
    base_unit_mismatch = []
    n_cards = 0

    for entry in cards:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("card") if isinstance(entry.get("card"), dict) else entry
        cid = inner.get("id")
        if not cid:
            continue
        n_cards += 1

        base = card_base_pct(inner)
        if base > HARD_CEILING_PCT:
            hard_ceiling.append((f"{cid}::<base rate>", round(base, 2)))
        if base == 0:
            zero_cards.append(cid)
        elif base > RATE_CEILING_PCT:
            over_cards.append((cid, round(base, 2)))
        elif base < RATE_FLOOR_PCT:
            under_cards.append((cid, round(base, 4)))

        # Exclusions the engine cannot read (type `other`/`txn_type` are free
        # text — 1,039 of 1,418 rules). Kept as text for the prose check below.
        prose = " ".join(
            str(x.get("exclusion_value") or "").lower()
            for x in (entry.get("exclusion_rules") or [])
            if isinstance(x, dict) and x.get("exclusion_type") in ("other", "txn_type")
        )
        excluded_here = [h for h in PROSE_EXCLUDED_HINTS if h in prose]

        # The BASE rate is what the app shows for every merchant it has no
        # specific rule for — including the categories this card excludes in
        # prose the engine cannot read. An inflated base rate therefore lands
        # directly on a petrol pump. This is the "75% at Indian Oil" defect,
        # and it is why the check keys on the base rate rather than rule names.
        if excluded_here and base > RATE_CEILING_PCT:
            prose_risk.append((cid, excluded_here[0], round(base, 2)))

        for rule in (entry.get("reward_rules") or []):
            if not isinstance(rule, dict):
                continue
            pct = rule_pct(rule, inner, base)

            # Unwaivable. See HARD_CEILING_PCT.
            if pct > HARD_CEILING_PCT:
                hard_ceiling.append((_rule_key(cid, rule), round(pct, 2)))

            # Does the number agree with its own sentence?
            #
            # EVIDENCE BEATS THE PARSER. This check reads the rule NAME, which is
            # the issuer's marketing sentence, and issuers write things like
            # "33% Bonus Reward Points" meaning 33% MORE POINTS, not 33% of spend.
            # Parsing that as a percentage and then overriding a number taken
            # verbatim from the bank's own T&C would be exactly backwards.
            # So a rule whose stored value is quoted from the issuer is exempt --
            # but only when the quote actually contains the number, so an
            # unrelated quote cannot be pinned on a rule to wave it through.
            claim, how = claimed_pct(rule, inner, base)
            if _issuer_quoted(rule):
                claim = None
            if claim is not None and max(pct, claim) >= SELF_CONTRADICTION_FLOOR_PCT:
                ratio = (pct / claim) if claim > 0 else float("inf")
                if ratio > SELF_CONTRADICTION_RATIO or ratio < 1 / SELF_CONTRADICTION_RATIO:
                    contradictions.append(
                        (_rule_key(cid, rule), round(pct, 2), round(claim, 2), how))

            # The base rule and the card's base_reward_rate field describe the
            # same thing. When they disagree, one of them is in the wrong unit
            # and the app shows whichever is larger. No rule name needed.
            if rule.get("rule_type") == "base_rate":
                if (max(pct, base) >= BASE_RULE_UNIT_FLOOR_PCT
                        and min(pct, base) > 0
                        and max(pct, base) / min(pct, base) > BASE_RULE_UNIT_RATIO):
                    base_unit_mismatch.append(
                        (f"{cid}::<base rule vs field>", round(pct, 2), round(base, 2)))

            if pct > RATE_CEILING_PCT:
                over_rules.append((_rule_key(cid, rule), round(pct, 2)))
                # A named rule that advertises a category the card excludes.
                name = (rule.get("rule_name") or "").lower()
                hit = next((h for h in excluded_here if h in name), None)
                if hit:
                    prose_risk.append((cid, hit, round(pct, 2)))

    return {
        "card_count": n_cards,
        "zero_rate_cards": len(zero_cards),
        "over_ceiling_cards": sorted(over_cards, key=lambda x: -x[1]),
        "under_floor_cards": sorted(under_cards, key=lambda x: x[1]),
        "over_ceiling_rules": sorted(over_rules, key=lambda x: -x[1]),
        "prose_excluded_high_rate": prose_risk,
        "over_hard_ceiling": sorted(hard_ceiling, key=lambda x: -x[1]),
        "self_contradicting_rules": sorted(contradictions, key=lambda x: -(x[1] / max(x[2], 1e-9))),
        "base_rule_unit_mismatch": sorted(
            base_unit_mismatch, key=lambda x: -(max(x[1], x[2]) / max(min(x[1], x[2]), 1e-9))),
    }


def _issuer_quoted(rule: dict) -> bool:
    """True when this rule's stored rate is backed by the issuer's own words.

    Requires BOTH a source_url and a source_quote, AND the quote must literally
    contain the rate's digits. Attaching a quote that does not mention the number
    grants no exemption -- that restriction is the whole point, otherwise this
    becomes a way to silence the self-consistency gate with any stray sentence.
    """
    if not isinstance(rule, dict):
        return False
    url, quote = rule.get("source_url"), rule.get("source_quote")
    if not (isinstance(url, str) and url and isinstance(quote, str) and quote):
        return False
    rate = rule.get("reward_rate")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool):
        return False
    # Match the number as a WHOLE TOKEN in the quote, not as a digit substring:
    # searching a concatenated digit soup would let "25" match inside "1,250"
    # and hand out exemptions the quote never justified.
    numbers = set()
    for tok in re.findall(r"\d+(?:,\d{2,3})*(?:\.\d+)?", quote):
        try:
            numbers.add(float(tok.replace(",", "")))
        except ValueError:
            continue
    if not numbers:
        return False
    # A rate may be written as a fraction, a percent, or a count of points, so
    # accept any of those spellings of the same value.
    for cand in (rate, rate * 100, rate / 100):
        for seen in numbers:
            if abs(seen - cand) <= max(abs(cand), 1.0) * 1e-6:
                return True
    return False


def read_rate_baseline() -> dict | None:
    if not RATE_BASELINE.exists():
        return None
    try:
        return read_json(RATE_BASELINE)
    except (json.JSONDecodeError, OSError):
        return None


def validate_economics(cards: list, rep: Report) -> dict:
    """Numeric plausibility. Returns the scan so callers can print or persist it."""
    head("Card economics")
    scan = scan_economics(cards)
    base = read_rate_baseline()

    if base is None:
        rep.warn(
            f"{rel(RATE_BASELINE)} is missing — cannot tell a new bad rate from an old "
            f"one. Run `kredme.py validate --target prod --update-baseline` once, review "
            f"the file, and commit it."
        )

    known_cards = set((base or {}).get("over_ceiling_cards") or [])
    known_rules = set((base or {}).get("over_ceiling_rules") or [])

    # 1. Nothing NEW may exceed the ceiling. Pre-existing ones are reported.
    new_cards = [c for c in scan["over_ceiling_cards"] if c[0] not in known_cards]
    if new_cards:
        rep.error(
            f"{len(new_cards)} card(s) NEWLY display a base rate above "
            f"{RATE_CEILING_PCT:g}% — that is a unit bug, not a great card: "
            f"{[f'{c}={p}%' for c, p in new_cards[:5]]}"
        )
    if base is not None and scan["over_ceiling_cards"]:
        n = len(scan["over_ceiling_cards"])
        (ok if n < len(known_cards) else warn)(
            f"{n} card(s) still above {RATE_CEILING_PCT:g}% (baseline {len(known_cards)}): "
            f"{[f'{c}={p}%' for c, p in scan['over_ceiling_cards'][:6]]}"
        )

    # Two rules on the same card can share a rule_name, so compare unique keys
    # to unique keys — counting raw findings against a deduped baseline would
    # read as a regression forever and hide a real one.
    seen_rule_keys = {k for k, _ in scan["over_ceiling_rules"]}
    new_rules = sorted(seen_rule_keys - known_rules)
    if new_rules:
        preview = [k.split("::")[0] for k in new_rules[:5]]
        rep.error(
            f"{len(new_rules)} reward rule(s) NEWLY display above {RATE_CEILING_PCT:g}%: {preview}"
        )
    if base is not None and seen_rule_keys:
        n = len(seen_rule_keys)
        (ok if n < len(known_rules) else warn)(
            f"{n} reward rule(s) still above {RATE_CEILING_PCT:g}% (baseline {len(known_rules)})"
        )

    # 2. A rate below the floor is the same unit bug pointing the other way —
    #    Axis Neo renders 0.02% because its rate was multiplied by the point
    #    value twice. Ratcheted, so pre-existing ones do not block a publish.
    known_floor = set((base or {}).get("under_floor_cards") or [])
    new_floor = [c for c in scan["under_floor_cards"] if c[0] not in known_floor]
    if new_floor:
        rep.error(
            f"{len(new_floor)} card(s) NEWLY display a non-zero base rate below "
            f"{RATE_FLOOR_PCT:g}% — the rate is almost certainly scaled wrong: {new_floor[:5]}"
        )
    if base is not None and scan["under_floor_cards"]:
        n = len(scan["under_floor_cards"])
        (ok if n < len(known_floor) else warn)(
            f"{n} card(s) still below {RATE_FLOOR_PCT:g}% (baseline {len(known_floor)}): "
            f"{scan['under_floor_cards'][:4]}"
        )

    # 3. Zero-rate cards render "0.00%" on every merchant. This must never grow.
    if base is not None:
        was = base.get("zero_rate_cards")
        now = scan["zero_rate_cards"]
        if isinstance(was, int) and now > was:
            rep.error(
                f"cards rendering 0.00% grew {was} -> {now}. Every one of those is a card "
                f"the user is told earns nothing."
            )
        elif isinstance(was, int) and now < was:
            ok(f"cards rendering 0.00%: {now} (was {was}) — {was - now} fixed")
        else:
            warn(f"{now} card(s) render a 0.00% base rate")

    # 4. Card count must not fall. The existing shrink guard allows a 20% drop;
    #    losing even one card removes it from a user's wallet.
    if base is not None:
        was = base.get("card_count")
        if isinstance(was, int) and scan["card_count"] < was:
            rep.error(
                f"card count fell {was} -> {scan['card_count']} — those cards disappear "
                f"from every wallet that holds them"
            )

    # 5. A high rate in a category the card's own exclusion text rules out.
    #    The engine reads only `category` and `mcc` exclusions, so these never
    #    fire and the user is shown an earn rate on a spend that earns nothing.
    #    Ratcheted like the rest: these all resolve when the base rates are
    #    fixed, so an unconditional failure would block unrelated publishes.
    known_prose = set((base or {}).get("prose_excluded_high_rate") or [])
    prose_now = [(f"{c}::{h}", c, h, p) for c, h, p in scan["prose_excluded_high_rate"]]
    new_prose = [x for x in prose_now if x[0] not in known_prose]
    if new_prose:
        rep.error(
            f"{len(new_prose)} rule(s) NEWLY show a rate above {RATE_CEILING_PCT:g}% in a "
            f"category the card's own exclusion text excludes — the user acts on this at "
            f"the pump: {[(c, h, p) for _, c, h, p in new_prose[:4]]}"
        )
    if base is not None and prose_now:
        n = len(prose_now)
        (ok if n < len(known_prose) else warn)(
            f"{n} rate(s) still advertised on an excluded category "
            f"(baseline {len(known_prose)}): {[(c, h, p) for _, c, h, p in prose_now[:4]]}"
        )

    # 6. THE UNWAIVABLE ONE. Every check above is ratcheted, which is what makes
    #    them shippable — and also what let 58 known-bad rules into the baseline
    #    on 7 Aug, after which none of them blocked anything. This check has no
    #    baseline key on purpose. Nothing renders above 30%, ever, for any reason.
    if scan["over_hard_ceiling"]:
        worst = scan["over_hard_ceiling"][:6]
        rep.error(
            f"{len(scan['over_hard_ceiling'])} rate(s) render above {HARD_CEILING_PCT:g}% — "
            f"no Indian card pays that, so this is a unit bug and it is not waivable: "
            f"{[f'{k.split('::')[0]}={p}%' for k, p in worst]}"
        )

    # 7. A rule whose number contradicts its own sentence. Ratcheted so existing
    #    debt does not block unrelated work, but a NEW one is always fatal —
    #    there is no version of "this rule says 24 points per Rs.150 and also
    #    24% cashback" that is a judgement call. It is a typo, provable offline.
    known_contra = set((base or {}).get("self_contradicting_rules") or [])
    contra_now = {k for k, _, _, _ in scan["self_contradicting_rules"]}
    new_contra = sorted(contra_now - known_contra)
    if new_contra:
        detail = [f"{k.split('::')[0]}" for k in new_contra[:5]]
        rep.error(
            f"{len(new_contra)} reward rule(s) NEWLY display a rate their own text "
            f"contradicts — the issuer mechanics are in the rule name and they disagree "
            f"with the stored number: {detail}"
        )
    if base is not None and contra_now:
        n = len(contra_now)
        (ok if n < len(known_contra) else warn)(
            f"{n} rule(s) still contradict their own text (baseline {len(known_contra)}); "
            f"worst: " + ", ".join(
                f"{k.split('::')[0]} shows {p}% vs {c}% claimed"
                for k, p, c, _ in scan["self_contradicting_rules"][:3])
        )

    # 8. A base rule that disagrees with the card's own base_reward_rate field.
    #    Check 7 can only fire when the rule name states mechanics, and 271 of
    #    279 base rules carry no number at all — so this is the only check that
    #    reaches them. Ratcheted like the rest, because the live set resolves
    #    card by card and each one needs its unit confirmed before it moves;
    #    but a NEW one is fatal, since it means a rule and its own card field
    #    were written in different units in the same edit.
    known_unit = set((base or {}).get("base_rule_unit_mismatch") or [])
    unit_now = {k for k, _, _ in scan["base_rule_unit_mismatch"]}
    new_unit = sorted(unit_now - known_unit)
    if new_unit:
        rep.error(
            f"{len(new_unit)} card(s) NEWLY show a base rate that disagrees with their own "
            f"base_reward_rate — one of the two is in the wrong unit: "
            f"{[k.split('::')[0] for k in new_unit[:5]]}"
        )
    if base is not None and unit_now:
        n = len(unit_now)
        (ok if n < len(known_unit) else warn)(
            f"{n} card(s) still show a base rate their own base_reward_rate contradicts "
            f"(baseline {len(known_unit)}); worst: " + ", ".join(
                f"{k.split('::')[0]} rule {p}% vs field {f}%"
                for k, p, f in scan["base_rule_unit_mismatch"][:3])
        )

    if not rep.failed:
        ok(f"{scan['card_count']} cards priced within {RATE_FLOOR_PCT:g}-{RATE_CEILING_PCT:g}% "
           f"or explicitly baselined, none above {HARD_CEILING_PCT:g}%, "
           f"none contradicting their own text or their own base rate")
    return scan


# ── Rule integrity ───────────────────────────────────────────────────────────
# Shape defects the economics scan cannot see, because it reads numbers and
# these rules never reach a number at all. Every class below was found live on
# dev @ 5.1.10 and every one of them fails SILENTLY in the app: no exception,
# no log, no error surface. A rule that is malformed in these ways still
# renders, still ranks, and still tells the user a figure.

# cap_period values the app can actually act on. Note the app collapses
# everything except 'quarter' and 'year' into the calendar month
# (recommendation_engine.dart _getSpentForRule), so 'cycle', 'transaction' and
# 'day' are accepted here but NOT honoured there. Accepting them is not
# endorsing them — it keeps this check about shape, and leaves the engine bug
# to be fixed in the app rather than papered over by rejecting live data.
CAP_PERIODS = {"month", "cycle", "transaction", "quarter", "year", "day"}


def scan_rule_integrity(cards: list) -> dict:
    """Structural defects in reward rules. Pure — no reporting."""
    non_numeric_caps, dup_rule_names, cap_no_period = [], [], []
    points_no_unit, unknown_cap_period = [], []

    for c in cards:
        if not isinstance(c, dict):
            continue
        inner = c.get("card") or {}
        cid = inner.get("id") or "?"
        seen = set()

        for r in c.get("reward_rules") or []:
            if not isinstance(r, dict):
                continue
            key = _rule_key(cid, r)
            cap = r.get("cap_amount")
            per = r.get("cap_period")

            # The app parses cap_amount with double.tryParse (_numOf,
            # credit_card.dart), which returns null for "12000 RPs per
            # statement cycle" and for any object. A null cap is NO cap: the
            # rule keeps paying its accelerated rate for ever, so the user is
            # promised more than the issuer pays. 19 rules on 9 cards.
            if cap is not None and _fnum(cap) is None:
                non_numeric_caps.append((key, type(cap).__name__))

            # `${cardId}|${ruleName}` is both the reward-rule primary key and
            # the cap-usage bucket key (app_database.dart). Two rules sharing a
            # name on one card share one cap bucket, and only one survives the
            # per-card {ruleName: rule} map wallet_insights builds. This is the
            # identity the whole file depends on — trap 1 forbids renaming a
            # rule precisely because of it — so a collision is data loss.
            #
            # Detected on the FULL name, not _rule_key: the app keys on the
            # whole string, while _rule_key truncates to 80 chars for baseline
            # readability. Truncating here would report 67 collisions where 24
            # are real, and send someone hunting rules that are actually fine.
            full = f"{cid}::{(r.get('rule_name') or '').strip()}"
            if full in seen:
                dup_rule_names.append(key)
            seen.add(full)

            # _checkCap returns null unless BOTH are set, so a cap with no
            # period is never enforced at all.
            if cap is not None and per is None:
                cap_no_period.append(key)

            if per is not None and per not in CAP_PERIODS:
                unknown_cap_period.append((key, str(per)))

            # A points rule that loses its unit silently becomes "per ₹100":
            # the app defaults rewardUnitSpend to 100.0 and rule_pct falls back
            # to the base rate. Neither surfaces an error. Holds 284/284 today.
            if r.get("reward_type") == "points_per_spend" and (_fnum(r.get("reward_unit_spend")) or 0) <= 0:
                points_no_unit.append(key)

    return {
        "non_numeric_caps":   sorted(non_numeric_caps),
        "duplicate_rule_names": sorted(dup_rule_names),
        "cap_without_period": sorted(cap_no_period),
        "unknown_cap_period": sorted(unknown_cap_period),
        "points_rule_no_unit": sorted(points_no_unit),
    }


def validate_rule_integrity(cards: list, rep: Report) -> dict:
    """Ratchet the three live defect classes; the other two are unwaivable."""
    head("Rule integrity")
    scan = scan_rule_integrity(cards)
    base = read_rate_baseline()

    # Ratcheted: real debt exists today, so freeze it and fail on anything new.
    for key, label in (
        ("non_numeric_caps",
         "cap_amount is not a number — the app drops the cap and the rule reads as uncapped"),
        ("duplicate_rule_names",
         "share a rule_name with another rule on the same card — same cap bucket, one overwrites the other"),
        ("cap_without_period",
         "have a cap_amount but no cap_period — the cap is never enforced"),
    ):
        found = scan[key]
        ident = {f[0] if isinstance(f, tuple) else f for f in found}
        known = set((base or {}).get(key) or [])
        new = sorted(ident - known)
        if new:
            rep.error(f"{len(new)} reward rule(s) NEWLY {label}: {new[:3]}")
        elif found:
            n = len(ident)
            (ok if n < len(known) else warn)(
                f"{n} reward rule(s) still {label} (baseline {len(known)})")
        else:
            ok(f"no reward rule {label}")

    # Unwaivable: both are clean today (0 and 0). There is no baseline key for
    # either, deliberately — the same reasoning as over_hard_ceiling. A file
    # that has never had one of these defects should never acquire one, and a
    # waiver is how "never" turns into "always".
    if scan["points_rule_no_unit"]:
        rep.error(
            f"{len(scan['points_rule_no_unit'])} points_per_spend rule(s) have no "
            f"reward_unit_spend — the app will silently read them as 'per ₹100': "
            f"{scan['points_rule_no_unit'][:3]}"
        )
    else:
        ok("every points_per_spend rule carries a reward_unit_spend")

    if scan["unknown_cap_period"]:
        rep.error(
            f"{len(scan['unknown_cap_period'])} rule(s) use a cap_period outside "
            f"{sorted(CAP_PERIODS)}: {scan['unknown_cap_period'][:3]}"
        )
    else:
        ok(f"every cap_period is one of {sorted(CAP_PERIODS)}")

    return scan


# The identity of a finding, per field. validate_economics compares against
# these exact keys, so the growth guard and the writer must not drift apart.
BASELINE_LISTS = {
    "over_ceiling_cards":       lambda scan: {c for c, _ in scan["over_ceiling_cards"]},
    "over_ceiling_rules":       lambda scan: {k for k, _ in scan["over_ceiling_rules"]},
    "under_floor_cards":        lambda scan: {c for c, _ in scan["under_floor_cards"]},
    "prose_excluded_high_rate": lambda scan: {f"{c}::{h}" for c, h, _ in scan["prose_excluded_high_rate"]},
    # NOTE: `over_hard_ceiling` is deliberately absent. It is the one check with
    # no waiver, so writing it to the baseline would defeat the point.
    "self_contradicting_rules": lambda scan: {k for k, _, _, _ in scan["self_contradicting_rules"]},
    "base_rule_unit_mismatch":  lambda scan: {k for k, _, _ in scan["base_rule_unit_mismatch"]},
}

# Same contract, for the structural defects. Kept in a second dict because
# these come from scan_rule_integrity, not scan_economics, and merging them
# would let one scan's missing key silently zero the other's baseline.
# NOTE: `points_rule_no_unit` and `unknown_cap_period` are deliberately absent —
# both are clean today and neither gets a waiver.
INTEGRITY_LISTS = {
    "non_numeric_caps":     lambda scan: {k for k, _ in scan["non_numeric_caps"]},
    "duplicate_rule_names": lambda scan: set(scan["duplicate_rule_names"]),
    "cap_without_period":   lambda scan: set(scan["cap_without_period"]),
}


def baseline_payload(scan: dict, source: str, integrity: dict | None = None) -> dict:
    payload = {
        "_note": "Known numeric defects in live card data. The gate fails on anything NOT "
                 "listed here, and on any growth in the counts. Shrink it; never grow it.",
        "generated_from": source,
        "generated_at": now_iso(),
        "ceiling_pct": RATE_CEILING_PCT,
        "floor_pct": RATE_FLOOR_PCT,
        "card_count": scan["card_count"],
        "zero_rate_cards": scan["zero_rate_cards"],
    }
    for key, ident in BASELINE_LISTS.items():
        payload[key] = sorted(ident(scan))
    # Absent integrity scan means "don't touch those keys" — a caller that
    # cannot measure them must not be able to blank them by omission.
    if integrity is not None:
        for key, ident in INTEGRITY_LISTS.items():
            payload[key] = sorted(ident(integrity))
    return payload


def write_rate_baseline(scan: dict, source: str, integrity: dict | None = None) -> None:
    prev = read_rate_baseline() or {}
    payload = baseline_payload(scan, source, integrity)
    if integrity is None:
        for key in INTEGRITY_LISTS:
            if key in prev:
                payload[key] = prev[key]
    write_json(RATE_BASELINE, payload)


def validate_seed(seed_dir: Path, rep: Report, strict_checksums: bool = True,
                  allow_shrink: bool = False) -> set:
    """Validate a seed/ directory. Returns the set of card IDs found.

    strict_checksums=True  (live)    — a mismatch is fatal: the app rejects the
                                       sync and the user sees "Sync failed".
    strict_checksums=False (working) — a mismatch is expected mid-edit;
                                       publish rebuilds the manifest from bytes.
    """
    head(f"Seed  {rel(seed_dir)}")
    card_ids: set = set()
    merchant_refs: set = set()

    mpath = seed_dir / MANIFEST
    if not mpath.exists():
        rep.error(f"{MANIFEST} is missing")
        return card_ids

    try:
        manifest = read_json(mpath)
    except json.JSONDecodeError as e:
        rep.error(f"{MANIFEST} is not valid JSON: {e}")
        return card_ids
    if not isinstance(manifest, dict):
        rep.error(f"{MANIFEST} must be a JSON object, got {type(manifest).__name__}")
        return card_ids
    ok(f"{MANIFEST} parses")

    version = manifest.get("version")
    if not version:
        rep.error("manifest has no 'version' — the app cannot detect an update")
    else:
        ok(f"seed version {version}")

    if not _iso_ok(manifest.get("updated_at")):
        rep.warn(f"manifest 'updated_at' is not ISO-8601: {manifest.get('updated_at')!r}")

    # Every declared file must exist and match its checksum + size exactly.
    # The app REJECTS a sync on mismatch, which surfaces as "Sync failed".
    declared = {f.get("name") for f in manifest.get("files", [])}
    for name in SEED_FILES:
        if name not in declared:
            rep.error(f"manifest does not declare {name}")

    for entry in manifest.get("files", []):
        name = entry.get("name", "?")
        fpath = seed_dir / Path(entry.get("path", f"seed/{name}")).name
        if not fpath.exists():
            rep.error(f"{name}: declared in manifest but file is missing")
            continue
        actual_sum = sha256(fpath)
        actual_size = fpath.stat().st_size
        mismatch = (entry.get("checksum") != actual_sum
                    or entry.get("size_bytes") != actual_size)
        if mismatch and strict_checksums:
            rep.error(
                f"{name}: checksum/size mismatch — the app will REJECT this sync "
                f'("Sync failed"). manifest={str(entry.get("checksum"))[:12]}… '
                f"actual={actual_sum[:12]}…"
            )
        elif mismatch:
            info(f"{name}: edited since the manifest was written — publish will regenerate it")
        else:
            ok(f"{name}: checksum + size match ({size_str(actual_size)})")

    # cards.json — each entry is {"card": {"id": ...}, "reward_rules": [...], ...}
    # Older/flatter shapes put "id" at the top level, so accept both.
    cpath = seed_dir / "cards.json"
    if cpath.exists():
        try:
            cards = read_json(cpath)
        except json.JSONDecodeError as e:
            rep.error(f"cards.json is not valid JSON: {e}")
            cards = None
        if isinstance(cards, list):
            seen, dupes = set(), set()
            missing_id = 0
            rule_count = 0
            bad_shape = 0
            for entry in cards:
                if not isinstance(entry, dict):
                    # Was silently skipped, so cards could vanish from live
                    # while the gate still reported the file as fine.
                    bad_shape += 1
                    continue
                inner = entry.get("card") if isinstance(entry.get("card"), dict) else entry
                cid = inner.get("id")
                if not cid:
                    missing_id += 1
                    continue
                if cid in seen:
                    dupes.add(cid)
                seen.add(cid)
                rules = entry.get("reward_rules")
                if isinstance(rules, list):
                    rule_count += len(rules)
            card_ids = seen
            if bad_shape:
                rep.error(f"cards.json: {bad_shape} entr(ies) are not objects — those cards "
                          f"would silently vanish from live")
            # Reward rules point at merchants by SLUG (merchant_ref), which is
            # what the app keys on — not the numeric merchant id.
            def _collect_refs(node, out):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k == "merchant_ref" and isinstance(v, str) and v:
                            out.add(v)
                        else:
                            _collect_refs(v, out)
                elif isinstance(node, list):
                    for i in node:
                        _collect_refs(i, out)
            _collect_refs(cards, merchant_refs)
            if missing_id:
                rep.error(f"cards.json: {missing_id} card(s) have no 'id'")
            if dupes:
                rep.error(f"cards.json: duplicate card ids: {sorted(dupes)[:5]}")
            if not seen:
                rep.error("cards.json contains no cards")
            else:
                ok(f"cards.json: {len(seen)} unique cards, {rule_count} reward rules")
            # Guard against a truncated/partial publish gutting the catalog.
            # An absolute floor is near-useless against a 376-card catalog, so
            # compare against what is actually live right now.
            if seen:
                live_count = live_card_count()
                if live_count and not allow_shrink and len(seen) < live_count * 0.8:
                    lost = live_count - len(seen)
                    rep.error(
                        f"cards.json has {len(seen)} cards but live has {live_count} — "
                        f"{lost} would disappear ({lost/live_count:.0%} of the catalog). "
                        f"Truncated export? Pass --allow-shrink if this is deliberate."
                    )
                elif not live_count and len(seen) < 50:
                    rep.warn(f"only {len(seen)} cards — expected a few hundred. Truncated file?")
        elif cards is not None:
            rep.error("cards.json must be a JSON array of card objects")

        # Structure is proven; now read the numbers. This is the only part of
        # the validator that can catch a rate being WRONG rather than malformed.
        if isinstance(cards, list) and cards:
            rep.economics = validate_economics(cards, rep)
            rep.integrity = validate_rule_integrity(cards, rep)

    # merchants.json — {"_metadata":…, "categories":[…], "merchants":[…]}
    mch = seed_dir / "merchants.json"
    if mch.exists():
        try:
            mdoc = read_json(mch)
        except json.JSONDecodeError as e:
            rep.error(f"merchants.json is not valid JSON: {e}")
            mdoc = None
        if isinstance(mdoc, dict):
            merchants = mdoc.get("merchants", [])
            categories = mdoc.get("categories", [])
        elif isinstance(mdoc, list):
            merchants, categories = mdoc, []
        else:
            merchants, categories = [], []

        if mdoc is not None:
            cat_ids = set()
            for c in categories:
                if isinstance(c, dict) and c.get("id"):
                    cat_ids.add(c["id"])
            # The APP keys merchants by merchant_name (MerchantData.fromJson:
            # `id: json['merchant_name']`). The numeric `id` is NOT read by the
            # app — a collision there is hygiene, not breakage. Grade them
            # accordingly so a real bug never hides behind a cosmetic one.
            seen_num, dupe_num, no_id = set(), set(), 0
            seen_name, dupe_name, no_name = set(), set(), 0
            dangling = []
            for m in merchants:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id")
                if mid is None:
                    no_id += 1
                elif mid in seen_num:
                    dupe_num.add(mid)
                else:
                    seen_num.add(mid)

                mname = m.get("merchant_name")
                if not mname:
                    no_name += 1
                elif mname in seen_name:
                    dupe_name.add(mname)
                else:
                    seen_name.add(mname)

                # A merchant pointing at a category that doesn't exist falls
                # back to "Other", so category reward rules never match it.
                cref = m.get("category_id")
                if cref and cat_ids and cref not in cat_ids:
                    dangling.append(f"{mname or mid}->{cref}")

            # An emptied merchants file used to pass silently AND disable the
            # merchant_ref cross-check below, so the gate reported "safe" while
            # every merchant-specific reward rule stopped matching.
            if not merchants:
                rep.error("merchants.json contains no merchants — every merchant-specific "
                          "reward rule would stop matching. Truncated or failed export?")
            if not categories:
                rep.error("merchants.json declares no categories — category reward rules "
                          "cannot resolve, and the dangling-category check is disabled")
            if no_name:
                rep.error(f"merchants.json: {no_name} merchant(s) have no 'merchant_name' (the app's key)")
            if dupe_name:
                rep.error(
                    f"merchants.json: duplicate merchant_name — the app keys on this, "
                    f"so one entry wins silently: {sorted(dupe_name)[:5]}"
                )
            if no_id:
                rep.warn(f"merchants.json: {no_id} merchant(s) have no numeric 'id'")
            if dupe_num:
                rep.warn(
                    f"merchants.json: {len(dupe_num)} duplicate numeric id(s) "
                    f"{sorted(dupe_num)[:6]} — the app ignores this field, so it is "
                    f"hygiene not breakage, but fix it before anything starts keying on it"
                )
            if dangling:
                rep.error(
                    f"merchants.json: {len(dangling)} merchant(s) reference a missing "
                    f"category (falls back to 'Other', category rules will miss): {dangling[:5]}"
                )
            ok(f"merchants.json: {len(seen_name)} merchants, {len(cat_ids)} categories")

            # Every merchant_ref in cards.json must resolve to a merchant_name,
            # or that reward rule can never fire. Deliberately NOT gated on
            # seen_name being non-empty: an emptied merchants file is exactly
            # when this check matters most.
            if merchant_refs:
                unresolved = sorted(merchant_refs - seen_name)
                if unresolved:
                    rep.error(
                        f"cards.json: {len(unresolved)} merchant_ref(s) do not match any "
                        f"merchant_name — those reward rules can never fire: {unresolved[:5]}"
                    )
                else:
                    ok(f"all {len(merchant_refs)} merchant_ref(s) in cards.json resolve")

    return card_ids


def validate_news(news_dir: Path, card_ids: set, rep: Report, manifest_news_version=None):
    head(f"News  {rel(news_dir)}")
    fpath = news_dir / FEED
    if not fpath.exists():
        rep.error(f"{FEED} is missing")
        return

    try:
        feed = read_json(fpath)
    except json.JSONDecodeError as e:
        rep.error(f"{FEED} is not valid JSON: {e}")
        return
    if not isinstance(feed, dict):
        rep.error(f"{FEED} must be a JSON object, got {type(feed).__name__}")
        return
    ok(f"{FEED} parses")

    version = feed.get("version")
    if not version:
        rep.error("feed has no 'version' — the app will never refetch it")
    else:
        ok(f"news version {version}")

    if not _iso_ok(feed.get("updated_at")):
        rep.warn(f"feed 'updated_at' is not ISO-8601: {feed.get('updated_at')!r}")

    if manifest_news_version is not None and version and str(manifest_news_version) != str(version):
        rep.warn(
            f"seed manifest says news_version={manifest_news_version!r} but the feed "
            f"is {version!r} — publish will reconcile these"
        )

    items = feed.get("items")
    if items is None:
        items = feed.get("articles")
    if not isinstance(items, list):
        rep.error("feed has no 'items' (or 'articles') list")
        return
    if not items:
        rep.warn("feed has zero items — users will see an empty news screen")

    seen_ids = set()
    for i, item in enumerate(items):
        where = f"item[{i}]"
        if not isinstance(item, dict):
            rep.error(f"{where}: not an object")
            continue
        label = item.get("id") or where

        for key in NEWS_REQUIRED:
            if not item.get(key):
                rep.error(f"{label}: missing required '{key}'")

        iid = item.get("id")
        if iid:
            if iid in seen_ids:
                rep.error(f"{label}: duplicate id")
            seen_ids.add(iid)

        # The silent-drop class of bug: keys the app never reads.
        for bad, good in NEWS_KEY_FIXES.items():
            if bad in item:
                rep.error(
                    f"{label}: uses '{bad}' but the app reads '{good}' — "
                    f"this field is silently ignored today"
                )
        for key in item:
            if key not in NEWS_VALID_KEYS and key not in NEWS_KEY_FIXES:
                rep.warn(f"{label}: '{key}' is not read by the app (harmless, but dead weight)")

        sev = item.get("severity")
        if sev is None:
            rep.warn(f"{label}: no 'severity' — devaluations should be 'negative' to show the red chip")
        elif sev not in NEWS_SEVERITIES:
            rep.error(f"{label}: severity {sev!r} invalid (use one of {sorted(NEWS_SEVERITIES)})")

        if not item.get("category"):
            rep.warn(f"{label}: no 'category' (e.g. 'devaluation', 'promo', 'announcement')")

        for key in ("published_at", "expiry_date"):
            if key in item and not _iso_ok(item.get(key)):
                rep.error(f"{label}: '{key}' is not ISO-8601 or null: {item.get(key)!r}")

        # Targeting correctness: a typo'd card id means the alert reaches nobody.
        ac = item.get("affected_cards")
        if ac is not None:
            if not isinstance(ac, list):
                rep.error(f"{label}: 'affected_cards' must be a list")
            elif ac and card_ids:
                unknown = [c for c in ac if c not in card_ids]
                if unknown:
                    rep.error(
                        f"{label}: affected_cards not found in cards.json → this alert "
                        f"would reach NOBODY: {unknown[:5]}"
                    )
                else:
                    ok(f"{label}: {len(ac)} affected card id(s) all resolve")

    if seen_ids:
        ok(f"{len(items)} news item(s) checked")


def materialise(ref: str) -> Path:
    """Extract seed/ and news/ from a git ref into a temp dir.

    Lets us inspect the dev branch without checking it out, so validating or
    promoting never disturbs the operator's working tree.
    """
    import tempfile
    dest = Path(tempfile.mkdtemp(prefix=f"kredme-{ref.replace('/', '-')}-"))
    (dest / "seed").mkdir(parents=True, exist_ok=True)
    (dest / "news").mkdir(parents=True, exist_ok=True)
    got = 0
    for relpath in [f"seed/{n}" for n in (*SEED_FILES, MANIFEST)] + [f"news/{FEED}"]:
        code, blob = git_bytes("show", f"{ref}:{relpath}")
        if code == 0:
            (dest / relpath).write_bytes(blob)
            got += 1
    if not got:
        die(f"branch '{ref}' has no data files — is it the right branch?")
    return dest


def data_dirs(target: str):
    """(seed_dir, news_dir, cleanup) for 'dev', 'prod' or 'working'."""
    if target == "working":
        return LIVE_SEED, LIVE_NEWS, None
    ref = DEV_BRANCH if target == "dev" else PROD_BRANCH
    code, _, _ = git("rev-parse", "--verify", ref)
    if code != 0:
        die(f"branch '{ref}' not found locally. Try:  git fetch origin {ref}:{ref}")
    tmp = materialise(ref)
    return tmp / "seed", tmp / "news", tmp


def run_validation(target: str, allow_shrink: bool = False) -> Report:
    rep = Report()
    seed_dir, news_dir, _tmp = data_dirs(target)
    # Checksums must be exact for anything an app actually fetches. Both dev and
    # prod are fetched by a real app, so both are strict; only the local working
    # tree is lenient (it is mid-edit by definition).
    card_ids = validate_seed(seed_dir, rep, strict_checksums=(target != "working"),
                             allow_shrink=allow_shrink)
    mnv = None
    mpath = seed_dir / MANIFEST
    if mpath.exists():
        try:
            mnv = read_json(mpath).get("news_version")
        except Exception:
            pass
    validate_news(news_dir, card_ids, rep, manifest_news_version=mnv)
    return rep


def print_verdict(rep: Report, target: str) -> None:
    print()
    if rep.failed:
        print(f"{C.R}{C.BOLD}✗ {target} is NOT safe to publish{C.X} — "
              f"{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")
        print(f"{C.DIM}  Fix the errors above, then run validate again.{C.X}")
    else:
        print(f"{C.G}{C.BOLD}✓ {target} passed{C.X} — 0 errors, {len(rep.warnings)} warning(s)")


# ------------------------------------------------------------- snapshots ----

def snapshot_dirs() -> list:
    if not SNAPSHOTS.exists():
        return []
    return sorted((d for d in SNAPSHOTS.iterdir() if d.is_dir()), reverse=True)


def read_versions(seed_dir: Path, news_dir: Path):
    sv = nv = "?"
    try:
        sv = read_json(seed_dir / MANIFEST).get("version", "?")
    except Exception:
        pass
    try:
        nv = read_json(news_dir / FEED).get("version", "?")
    except Exception:
        pass
    return sv, nv


HIGHWATER = SNAPSHOTS / "HIGHWATER.json"


def published_ceiling():
    """Highest seed/news versions we have EVER published.

    `undo` moves live's version BACKWARDS. Without this, the next publish would
    re-emit a version users already hold — and for news the app only refetches
    when the leading integer INCREASES, so a correction pushed during an
    incident would silently reach nobody. Every publish must clear this bar.

    Derived from the ledger plus every snapshot on disk, so deleting the ledger
    cannot quietly lower the bar.
    """
    seed_v = news_v = None

    def raise_to(cur, cand):
        if not cand:
            return cur
        if cur is None or version_gt(cand, cur):
            return cand
        return cur

    try:
        led = read_json(HIGHWATER)
        seed_v = raise_to(seed_v, led.get("seed_version"))
        news_v = raise_to(news_v, led.get("news_version"))
    except Exception:
        pass

    for d in snapshot_dirs():
        try:
            meta = read_json(d / "meta.json")
            seed_v = raise_to(seed_v, meta.get("seed_version"))
            news_v = raise_to(news_v, meta.get("news_version"))
        except Exception:
            continue

    return seed_v, news_v


def record_published(seed_v: str, news_v: str) -> None:
    cur_s, cur_n = published_ceiling()
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    write_json(HIGHWATER, {
        "seed_version": seed_v if (cur_s is None or version_gt(seed_v, cur_s)) else cur_s,
        "news_version": news_v if (cur_n is None or version_gt(news_v, cur_n)) else cur_n,
        "updated_at": now_iso(),
        "note": "Highest versions ever published. publish never re-emits a version at or below these.",
    })


def take_snapshot(reason: str) -> Path:
    """Copy the CURRENT live tree into .published/ before we overwrite it."""
    sv, nv = read_versions(LIVE_SEED, LIVE_NEWS)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = SNAPSHOTS / f"{stamp}__seed-{sv}__news-{nv}"
    dest.mkdir(parents=True, exist_ok=True)
    if LIVE_SEED.exists():
        shutil.copytree(LIVE_SEED, dest / "seed", dirs_exist_ok=True)
    if LIVE_NEWS.exists():
        shutil.copytree(LIVE_NEWS, dest / "news", dirs_exist_ok=True)
    write_json(dest / "meta.json", {
        "taken_at": now_iso(),
        "reason": reason,
        "seed_version": sv,
        "news_version": nv,
        "git_branch": git_branch(),
        "git_head": git_head(),
        "note": "Restore with:  python3 tools/kredme.py undo",
    })
    return dest


def ensure_gitignore() -> None:
    """.published/ must never be committed — it would be served publicly."""
    gi = REPO / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    have = {l.strip().rstrip("/") for l in lines}
    if ".published" in have:
        return
    with open(gi, "a", encoding="utf-8") as fh:
        if lines and lines[-1].strip():
            fh.write("\n")
        fh.write(
            "# local publish snapshots — never serve or commit these\n"
            ".published/\n"
        )
    info("added .published/ to .gitignore")


# -------------------------------------------------------------- commands ----

def branch_versions(target: str):
    seed_dir, news_dir, _ = data_dirs(target)
    return read_versions(seed_dir, news_dir)


def cmd_status(args) -> None:
    head("Environments")
    print(f"  repo      {REPO}")
    print(f"  on branch {git_branch()}")

    try:
        dsv, dnv = branch_versions("dev")
    except SystemExit:
        dsv = dnv = "?"
    psv, pnv = branch_versions("prod")

    print(f"\n  {C.BOLD}DEV{C.X}   branch '{DEV_BRANCH}'    seed {dsv}   news {dnv}")
    print(f"        {C.DIM}{DEV_BASE}{C.X}")
    print(f"        {C.DIM}read by TestFlight / dev APK builds{C.X}")
    print(f"\n  {C.BOLD}PROD{C.X}  branch '{PROD_BRANCH}'   seed {psv}   news {pnv}")
    print(f"        {C.DIM}{PROD_BASE}{C.X}")
    print(f"        {C.DIM}read by every store build — always{C.X}")

    head("Is dev ahead of prod?")
    code, out, _ = git("log", "--oneline", f"{PROD_BRANCH}..{DEV_BRANCH}", "--", "seed", "news")
    if code == 0 and out:
        for line in out.split("\n")[:8]:
            print(f"  {line}")
        print(f"\n  {C.Y}dev has data commits not yet in prod{C.X} — promote when ready")
    else:
        print(f"  {C.DIM}no — dev and prod data are the same{C.X}")

    snaps = snapshot_dirs()
    head("Prod restore points")
    if snaps:
        for d in snaps[:5]:
            print(f"  {d.name}")
    else:
        print(f"  {C.DIM}none yet — the first promote creates one{C.X}")
    print()


def cmd_validate(args) -> None:
    rep = run_validation(args.target, allow_shrink=getattr(args, "allow_shrink", False))
    if getattr(args, "update_baseline", False):
        if rep.economics is None:
            die("no card data was read, so there is nothing to baseline")
        old = read_rate_baseline() or {}
        new = rep.economics
        # A baseline may only ever record FEWER defects. Allowing it to grow
        # would turn the ratchet into a rubber stamp for the next bad publish.
        for key, label in (("zero_rate_cards", "cards rendering 0.00%"),):
            was, now = old.get(key), new.get(key)
            if isinstance(was, int) and isinstance(now, int) and now > was:
                die(f"refusing to update the baseline: {label} would grow {was} -> {now}.\n"
                    f"          Fix the data instead.")
        for key, ident in BASELINE_LISTS.items():
            if not isinstance(old.get(key), list):
                continue
            now_n = len(ident(new))
            if now_n > len(old[key]):
                die(f"refusing to update the baseline: '{key}' would grow "
                    f"{len(old[key])} -> {now_n}.\n          Fix the data instead.")
        for key, ident in INTEGRITY_LISTS.items():
            if not isinstance(old.get(key), list) or rep.integrity is None:
                continue
            now_n = len(ident(rep.integrity))
            if now_n > len(old[key]):
                die(f"refusing to update the baseline: '{key}' would grow "
                    f"{len(old[key])} -> {now_n}.\n          Fix the data instead.")
        write_rate_baseline(new, f"{args.target}@{git_head()[:7]}", rep.integrity)
        head("Baseline")
        ok(f"wrote {rel(RATE_BASELINE)} — review the diff, then commit it")
    print_verdict(rep, args.target)
    print()
    sys.exit(1 if rep.failed else 0)


def cmd_promote(args) -> None:
    """dev -> prod. The only path by which real users ever get new data."""
    if git_branch() != PROD_BRANCH:
        die(f"promote must run from '{PROD_BRANCH}' (you are on '{git_branch()}').\n"
            f"          Run:  git checkout {PROD_BRANCH}")
    code, dirty, _ = git("status", "--porcelain", "--", "seed", "news")
    if code == 0 and dirty:
        die("seed/ or news/ has uncommitted changes — commit or discard them first.")

    head(f"Step 1 — validate the '{DEV_BRANCH}' branch (nothing moves until this passes)")
    rep = run_validation("dev", allow_shrink=getattr(args, "allow_shrink", False))
    print_verdict(rep, "dev")
    if rep.failed:
        die("dev has errors — refusing to promote. This is the gate working.")

    dev_seed, dev_news, _ = data_dirs("dev")
    live_sv, live_nv = read_versions(LIVE_SEED, LIVE_NEWS)
    dev_sv, dev_nv = read_versions(dev_seed, dev_news)

    seed_changed = any(
        (dev_seed / n).exists() and (
            not (LIVE_SEED / n).exists() or sha256(dev_seed / n) != sha256(LIVE_SEED / n))
        for n in SEED_FILES)
    news_changed = (dev_news / FEED).exists() and (
        not (LIVE_NEWS / FEED).exists() or sha256(dev_news / FEED) != sha256(LIVE_NEWS / FEED))

    if not seed_changed and not news_changed:
        die("dev and prod data are identical — nothing to promote.", code=0)

    if rep.warnings and not args.allow_warnings and not args.dry_run:
        print(f"\n{C.Y}{C.BOLD}{len(rep.warnings)} warning(s).{C.X} "
              f"Re-run with --allow-warnings to promote anyway.")
        sys.exit(2)

    try:
        new_sv = bump_patch(live_sv, strict=True) if seed_changed else live_sv
    except VersionError as e:
        die(f"cannot read the current PROD seed version ({e}). Fix seed/{MANIFEST} first.")
    try:
        new_nv = bump_major(live_nv, strict=True) if news_changed else live_nv
    except VersionError as e:
        die(f"cannot read the current PROD news version ({e}). Fix news/{FEED} first — "
            f"a wrong bump would stop every app from refetching news.")

    # dev may already carry a higher version than a naive bump would produce.
    if seed_changed and version_gt(dev_sv, new_sv):
        new_sv = dev_sv
    if news_changed and version_gt(dev_nv, new_nv):
        new_nv = dev_nv

    ceil_sv, ceil_nv = published_ceiling()
    if seed_changed and ceil_sv and not version_gt(new_sv, ceil_sv):
        new_sv = bump_patch(ceil_sv)
        warn(f"seed version already served — raising to {new_sv}")
    if news_changed and ceil_nv and not version_gt(new_nv, ceil_nv):
        new_nv = bump_major(ceil_nv)
        warn(f"news version already served — raising to {new_nv}")

    head("Step 2 — what prod will receive")
    print(f"  seed   {live_sv}  ->  {C.G}{new_sv}{C.X}" if seed_changed
          else f"  seed   {live_sv}      {C.DIM}unchanged{C.X}")
    print(f"  news   {live_nv}  ->  {C.G}{new_nv}{C.X}   {C.DIM}(MAJOR — app ignores minor bumps){C.X}"
          if news_changed else f"  news   {live_nv}      {C.DIM}unchanged{C.X}")

    if args.dry_run:
        print(f"\n{C.B}{C.BOLD}Dry run — nothing was written.{C.X}\n")
        return

    if not args.yes:
        print()
        if input(f"  Promote dev -> prod locally? {C.DIM}(y/N){C.X} ").strip().lower() not in ("y", "yes"):
            die("cancelled — nothing was written.", code=0)

    head("Step 3 — snapshot prod (this is your undo)")
    snap = take_snapshot(reason=f"pre-promote seed {live_sv}->{new_sv}, news {live_nv}->{new_nv}")
    ensure_gitignore()
    ok(f"saved {snap.relative_to(REPO)}")

    head("Step 4 — copy dev data into prod")
    if news_changed:
        feed = read_json(dev_news / FEED)
        feed["version"] = new_nv
        feed["updated_at"] = now_iso()
        write_json(LIVE_NEWS / FEED, feed)
        ok(f"news/{FEED} -> {new_nv}")
    if seed_changed:
        for n in SEED_FILES:
            if (dev_seed / n).exists():
                shutil.copy2(dev_seed / n, LIVE_SEED / n)
                ok(f"seed/{n}")

    manifest = dict(read_json(dev_seed / MANIFEST)) if seed_changed else dict(read_json(LIVE_SEED / MANIFEST))
    manifest["version"] = new_sv
    manifest["updated_at"] = now_iso()
    manifest["news_version"] = new_nv
    manifest["files"] = [
        {"name": n, "path": f"seed/{n}", "checksum": sha256(LIVE_SEED / n),
         "size_bytes": (LIVE_SEED / n).stat().st_size}
        for n in SEED_FILES if (LIVE_SEED / n).exists()
    ]
    write_json(LIVE_SEED / MANIFEST, manifest)
    ok(f"seed/{MANIFEST} rebuilt — checksums recomputed")

    record_published(new_sv, new_nv)

    head("Step 5 — verify what prod now holds")
    rep2 = run_validation("working")
    print_verdict(rep2, "prod (working tree)")
    if rep2.failed:
        print(f"\n{C.R}That write produced invalid prod data.{C.X} Roll back now:")
        print(f"    python3 tools/kredme.py undo\n")
        sys.exit(1)

    print(f"\n{C.G}{C.BOLD}Promoted locally.{C.X} Real users have NOT received it yet.")
    print(f"\n{C.BOLD}To reach users{C.X} (the only networked step):")
    print(f"    git -C {REPO} add seed news")
    print(f'    git -C {REPO} commit -m "data: seed {new_sv}, news {new_nv}"')
    print(f"    git -C {REPO} push origin {PROD_BRANCH}")
    print(f"\n{C.BOLD}Check it landed{C.X} (~1 min):")
    print(f"    curl -s {PROD_BASE}/seed/manifest.json | python3 -m json.tool | head -5")
    print(f"\n{C.BOLD}If it goes wrong{C.X}:  python3 tools/kredme.py undo\n")


def cmd_undo(args) -> None:
    snaps = snapshot_dirs()
    if args.list:
        head("Restore points (newest first)")
        if not snaps:
            print(f"  {C.DIM}none yet{C.X}\n")
            return
        for d in snaps:
            meta = {}
            try:
                meta = read_json(d / "meta.json")
            except Exception:
                pass
            print(f"  {C.BOLD}{d.name}{C.X}")
            print(f"    {C.DIM}taken {meta.get('taken_at','?')} · seed {meta.get('seed_version','?')} · news {meta.get('news_version','?')}{C.X}")
        print()
        return

    if not snaps:
        die("no restore points yet — nothing to undo.")

    target = snaps[0]
    if args.to:
        matches = [d for d in snaps if d.name == args.to or d.name.startswith(args.to)]
        if not matches:
            die(f"no restore point matching {args.to!r}. Use --list to see them.")
        target = matches[0]

    meta = {}
    try:
        meta = read_json(target / "meta.json")
    except Exception:
        pass

    head("Undo — restore PROD from a snapshot")
    cur_sv, cur_nv = read_versions(LIVE_SEED, LIVE_NEWS)
    print(f"  now      seed {cur_sv}   news {cur_nv}")
    print(f"  restore  seed {meta.get('seed_version','?')}   news {meta.get('news_version','?')}   {C.DIM}({target.name}){C.X}")

    if not args.yes:
        print()
        reply = input(f"  Restore this? {C.DIM}(y/N){C.X} ").strip().lower()
        if reply not in ("y", "yes"):
            die("cancelled — nothing changed.", code=0)

    # Snapshot the current state too, so undo is itself undoable.
    take_snapshot(reason=f"pre-undo (was seed {cur_sv}, news {cur_nv})")

    if (target / "seed").exists():
        shutil.copytree(target / "seed", LIVE_SEED, dirs_exist_ok=True)
        ok("seed/ restored")
    if (target / "news").exists():
        shutil.copytree(target / "news", LIVE_NEWS, dirs_exist_ok=True)
        ok("news/ restored")

    head("Verify the restored data")
    rep = run_validation("working")
    print_verdict(rep, "prod (working tree)")

    if rep.failed:
        print(f"\n{C.R}{C.BOLD}The restored data does NOT validate.{C.X} "
              f"Do not push it. Try another restore point:")
        print(f"    python3 tools/kredme.py undo --list\n")
        sys.exit(1)

    print(f"\n{C.G}{C.BOLD}Restored locally.{C.X} To make the rollback live:")
    print(f"    git -C {REPO} checkout main")
    print(f"    git -C {REPO} add seed news")
    print(f'    git -C {REPO} commit -m "data: roll back to seed {meta.get("seed_version","?")}"')
    print(f"    git -C {REPO} push origin main\n")


# ------------------------------------------------------------------ main ----

def main() -> None:
    p = argparse.ArgumentParser(
        prog="kredme.py",
        description="Dev/prod data pipeline for the KredMe OTA backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical day:  edit on dev  ->  validate  ->  test on your phone  ->  promote  ->  git push",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="dev vs prod versions and restore points")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("validate", help="check an environment's data is safe")
    s.add_argument("--target", choices=("dev", "prod", "working"), default="dev")
    s.add_argument("--allow-shrink", action="store_true",
                   help="permit a large drop in card count (deliberate reduction)")
    s.add_argument("--update-baseline", action="store_true",
                   help="rewrite tools/rate_baseline.json from this target. Review the "
                        "diff before committing — it can only be used to LOWER the floor")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("promote", help="dev -> prod (local only; you still push)")
    s.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--allow-warnings", action="store_true", help="promote despite warnings")
    s.add_argument("--allow-shrink", action="store_true",
                   help="permit a large drop in card count (deliberate reduction)")
    s.set_defaults(func=cmd_promote)

    s = sub.add_parser("undo", help="restore prod from the previous snapshot")
    s.add_argument("--list", action="store_true", help="show restore points")
    s.add_argument("--to", metavar="SNAPSHOT", help="restore a specific snapshot")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_undo)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
