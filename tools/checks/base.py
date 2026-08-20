"""Shared contract for every card-data check module.

Stdlib only. Every module under tools/checks/ exposes:

    LAYER = "L4 numeric"          # human label, printed as a section banner
    def run(ctx: Ctx) -> list[Finding | Skipped]

A check NEVER prints, NEVER exits, NEVER mutates ctx. It returns findings, and
— when an input it depends on is not available — Skipped records saying so.
The runner (tools/validate_cards.py) decides severity policy, ordering and output.

THE THIRD ANSWER
----------------
A check has exactly three honest answers, not two:

    Finding   I looked, and this is wrong.
    (nothing) I looked, and it is fine.
    Skipped   I could not look.

The third one used to be missing, and the cost was measured: run this validator
without the app checkout and it reported 1,021 errors instead of 712. The extra
309 were not defects. The app's categories.json was unreadable, so every
category slug in the file resolved to nothing, and 191 bonus rules were declared
"thrown away at startup" and 109 category tags "not a category the app has" — by
a run that had no idea what categories the app has. A check that has lost its
authority must SKIP and SAY SO. It may never convert its own blindness into
errors against the data, and it may never fall silent either, because a silent
skip reads exactly like a pass.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Severity
# --------------------------------------------------------------------------- #
# ERROR  the app will misbehave, drop data, or show a user a wrong number.
# WARN   the data is suspect or a rule is inert; no crash, but value is lost.
# INFO   observation worth counting, not worth blocking on.
ERROR, WARN, INFO = "ERROR", "WARN", "INFO"
_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass
class Finding:
    """One defect, anchored to the smallest addressable thing."""
    severity: str            # ERROR / WARN / INFO
    code: str                # stable slug, e.g. "L1.ROW_NOT_OBJECT" — never reword
    message: str             # one sentence, plain English, no jargon
    card_id: str | None = None
    block: str | None = None      # reward_rules / exclusion_rules / card / ...
    index: int | None = None      # row index within that block
    field: str | None = None
    evidence: str | None = None   # the actual offending value, truncated
    fix: str | None = None        # what a human should do about it
    impact: str | None = None     # who sees what, in user terms

    def key(self):
        return (_ORDER.get(self.severity, 9), self.code, self.card_id or "", self.index or -1)

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Skipped:
    """One check that could not run, because an input it depends on was absent.

    A Skipped is NOT a Finding. It is never an error, never a warning and never
    a note about the data — it is a statement about THIS RUN. The runner counts
    skips separately, prints them under their own heading, and stamps the
    verdict DEGRADED so that a run which checked less cannot be read as a run
    that found less.
    """
    code: str                # stable slug for the check that did not run
    what: str                # one sentence: what was not checked
    reason: str              # why the authority to check it was missing
    restore: str             # the concrete thing that makes it runnable again
    impact: str | None = None    # what a reader must NOT conclude from this run
    codes: tuple = ()        # finding codes this run could not produce or trust
    layer: str | None = None     # filled in by the runner

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items() if v is not None}
        d["codes"] = list(self.codes)
        return d


@dataclass
class Ctx:
    """Everything a check may read. Loaded once by the runner."""
    seed_dir: Path
    news_dir: Path
    cards: list                       # raw cards.json
    merchants: dict | list            # raw merchants.json
    manifest: dict
    news: dict | list | None
    app_categories: list | None       # the app's category vocabulary, from wherever
    app_root: Path | None             # KredMe-main checkout, or None
    config: dict = field(default_factory=dict)
    # -- where the app-derived facts came from ----------------------------- #
    # 'app'    read out of a KredMe-main checkout: authoritative
    # 'mirror' read out of this repo's vendored copy: as good as its last update
    # None     neither was available: every check that needs it must SKIP
    categories_origin: str | None = None
    categories_path: Path | None = None
    categories_drift: list | None = None   # mirror-vs-app differences, when both seen
    app_keys: dict | None = None      # mirrored answers to app_reads_json_key()
    app_keys_origin: str | None = None
    _key_cache: dict = field(default_factory=dict, repr=False, compare=False)

    # -- what the app actually reads -------------------------------------- #
    def app_reads_json_key(self, key: str):
        """True / False / None — does any Dart file under the app's lib/ mention
        this JSON key at all?

        None means 'no app checkout, so this cannot be answered' and must never
        be read as False. This exists because a check asserted from memory that
        the app reads the `redemption_rules` block, hung 147 findings off that
        belief, and was wrong: the app parses `redemption_channels`, and the
        string `redemption_rules` does not occur once in lib/. Beliefs about the
        app go stale. Measuring does not.

        Deliberately a plain substring search for the QUOTED key. It over-reports
        (a key named only in a comment counts as read) and that is the safe
        direction: over-reporting keeps a finding loud, under-reporting silences
        a real defect.
        """
        if key in self._key_cache:
            return self._key_cache[key]
        root = self.app_root
        lib = Path(root) / "lib" if root else None
        if not lib or not lib.is_dir():
            # No app checkout. Before giving up, consult the vendored mirror —
            # this repo is PUBLIC and the app repo is PRIVATE, so CI here can
            # never check the app out, and without the mirror every run in CI
            # would answer None to a question that has a known, recorded answer.
            # The mirror only ever answers for keys it explicitly lists; a key
            # it does not name still returns None, which is the truth.
            mirrored = (self.app_keys or {}).get(key)
            ans = mirrored if isinstance(mirrored, bool) else None
            self._key_cache[key] = ans
            return ans
        needles = (f"'{key}'", f'"{key}"')
        found = False
        for p in lib.rglob("*.dart"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(nd in text for nd in needles):
                found = True
                break
        self._key_cache[key] = found
        return found

    def block_reaches_app(self, block: str):
        """True / False / None for one of our top-level row blocks."""
        return self.app_reads_json_key(block)

    # -- convenience iterators -------------------------------------------- #
    def entries(self):
        """(index, entry, card_inner, card_id) for every well-formed card entry.

        `cards` is whatever cards.json actually parsed to — the runner hands it
        over raw so a wrapped-in-an-object file can be diagnosed as exactly that
        rather than as an empty catalogue. A non-list simply has no entries; the
        layers that own that fault (L1, L6) report it with their own guard.
        """
        if not isinstance(self.cards, list):
            return
        for i, e in enumerate(self.cards):
            if not isinstance(e, dict):
                continue
            inner = e.get("card") if isinstance(e.get("card"), dict) else e
            yield i, e, inner, inner.get("id")

    def rules(self, block="reward_rules"):
        """(card_id, card_inner, row_index, row) for every row in `block`.

        Yields malformed rows too — a check that only wants dicts must filter.
        """
        for _, e, inner, cid in self.entries():
            rows = e.get(block)
            if not isinstance(rows, list):
                continue
            for j, r in enumerate(rows):
                yield cid, inner, j, r

    def app_category_names(self) -> set:
        if not self.app_categories:
            return set()
        return {c.get("category_name") for c in self.app_categories
                if isinstance(c, dict) and c.get("category_name")}

    def have_categories(self) -> bool:
        """True when this run knows the app's category vocabulary — from a
        checkout or from the vendored mirror. False means every category check
        must SKIP, because an unknown vocabulary makes every slug look invalid
        and turns a blind run into 300 fabricated errors."""
        return bool(self.app_category_names())

    def categories_source(self) -> str:
        """One phrase naming where the vocabulary came from, for the header."""
        if self.categories_origin == "app":
            return f"app checkout {self.categories_path}"
        if self.categories_origin == "mirror":
            return f"vendored mirror {self.categories_path}"
        return "not available"

    def merchant_slugs(self) -> set:
        """Every name a rule's merchant_ref may legitimately point at.

        'merchant_name' is the key seed/merchants.json actually uses, and it was
        missing from this list, so the helper returned an empty set for all 273
        rows and every caller's merchant branch was unreachable code. 'id' is
        gone: it is an INTEGER in the real file, so the isinstance(str) guard
        could only ever have rejected it — keeping it implied a string id exists.
        c3_referential used to carry a private copy of this to work around the
        gap; this is now the single definition of 'the merchants we ship'.
        """
        m = self.merchants
        rows = m.get("merchants") if isinstance(m, dict) else m
        out = set()
        for r in rows or []:
            if isinstance(r, dict):
                for k in ("merchant_name", "slug", "merchant_slug", "merchant_ref"):
                    if isinstance(r.get(k), str) and r[k]:
                        out.add(r[k])
        return out


# --------------------------------------------------------------------------- #
# Small shared helpers — keep numeric handling identical across every layer
# --------------------------------------------------------------------------- #
def reach_scaled(reaches, live_sev, dead_sev, *, live_impact, dead_impact, **kw) -> Finding:
    """One finding whose severity follows whether a user can reach the row.

    `reaches` is the three-state answer from Ctx.block_reaches_app():

        True   the app reads this block  -> live_sev, live_impact
        False  it does not               -> dead_sev, dead_impact ("nobody sees this today")
        None   unknown                   -> the louder severity, CAPPED AT WARN, + a caveat

    Unknown keeps the LOUDER severity, because silence has to be earned by
    evidence and a missing app checkout is not evidence.

    But it is capped at WARN, and that cap matters. ERROR is not just "louder" —
    it is the severity that exits 1 and blocks a publish, and it means "a user
    can be shown a wrong number". A run that cannot tell whether anybody can
    reach these rows cannot support that claim about a user. Letting it make the
    claim anyway is the same defect as the category cascade one layer down:
    a check converting its own blindness into errors against the data. WARN
    keeps the finding visible and still exits non-zero; it just does not let an
    unknown masquerade as a demonstrated harm.

    In practice this path is a fallback of last resort. The answer is normally
    known — measured from a checkout, or read from tools/app_mirror/. Reaching
    here at all means both were missing, and the layer that owns the question
    reports that as a skip.
    """
    if reaches is False:
        return Finding(severity=dead_sev, impact=dead_impact, **kw)
    if reaches is None:
        sev = WARN if live_sev == ERROR else live_sev
        return Finding(severity=sev, impact=(
            live_impact + " (Not verified on this run: whether these rows reach a user "
                          "at all could not be checked, so this is reported at "
                          f"{sev} rather than {live_sev} — an unverified reach may not "
                          "be presented as demonstrated user harm.)"), **kw)
    return Finding(severity=live_sev, impact=live_impact, **kw)


def num(v):
    """Coerce to float, or None. A numeric STRING returns None on purpose:
    the app's Dart parser drops it, so 'not a number' is the honest answer."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def trunc(v, n=90):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)")


def iso_ok(v) -> bool:
    return isinstance(v, str) and bool(ISO_DATE.match(v))


def card_base_pct(inner: dict) -> float:
    """Percent-of-spend the card's own base_reward_rate field claims.
    Mirrors tools/kredme.py:card_base_pct — keep the two in step."""
    r = num(inner.get("base_reward_rate")) or 0.0
    cur = (inner.get("reward_currency") or "").lower()
    if cur in ("cashback", "cash_back", "inr"):
        return r * 100.0 if r <= 1 else r
    pv = num(inner.get("rp_value_standard"))
    return r * (pv or 0.0) * 100.0
