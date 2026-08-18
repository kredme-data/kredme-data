#!/usr/bin/env python3
"""
validate_cards.py — the one card-data check a non-technical founder can run and trust.

WHAT THIS IS
------------
Nine independent check modules live in tools/checks/. Each one answers a different
question about seed/cards.json and never prints, never exits and never decides
policy. This file is the runner: it loads the data once, runs every module, and
turns ~2,600 findings into a verdict and a scorecard a human can act on.

    L1 schema & shape                 can the app read this file at all?
    L2 vocabulary & enums             does the engine understand these words?
    L3 referential integrity          does every reference resolve?
    L4 numeric plausibility & units   is the number the right SIZE, in the right UNIT?
    L5 text-vs-number consistency     does the English agree with the fields?
    L6 engine reachability            will the app ever actually fire this rule?
    L7 cross-card coherence           is the catalogue coherent as a SET?
    L8 provenance & confidence        who says so, and can I read it myself?
    L9 temporal & lifecycle           has time made this wrong, and would we notice?

THE ONE RULE THAT MATTERS
-------------------------
**NOTHING IS GRANDFATHERED.**

tools/kredme.py measures today's defects against tools/rate_baseline.json, prints
seven WARN lines, and then concludes "0 errors, 0 warning(s)". That is how 26 rules
above 10%, 105 cards rendering a 0.00% base rate and 14 unreadable caps became
invisible: they are all "at baseline", so the summary says green.

This tool will not do that.

  * There is no default baseline. `tools/rate_baseline.json` is never read.
  * `--baseline FILE` is opt-in, and even then every suppressed finding is COUNTED
    and reported as "N findings suppressed by baseline" in the verdict.
  * The verdict line can never claim a clean run while findings exist.
  * A finding from a crashed check module can never be suppressed at all.

THE SECOND RULE: A CHECK THAT CANNOT LOOK MUST SAY SO
----------------------------------------------------
A check has three answers, not two: it found something, it found nothing, or it
COULD NOT LOOK. The third was missing, and the bill came to 309 fabricated
errors — run without the app checkout this tool reported 1,021 errors against a
file that has 712, because checks that had lost the app's category list carried
on anyway and called 191 healthy bonus rules dead.

So a check that loses an input now returns a `Skipped` instead of findings. A
skip is never an error, never a warning, never a note; it is counted separately,
printed under "Checks that DID NOT RUN", and stamps the verdict DEGRADED. It
cannot move the exit code — 0/1/2/3 are a contract automation gates on — but it
is why the counts above the verdict are described as a FLOOR.

The invariant, enforced by tools/test_validate_cards.py against the real
catalogue: a run with fewer inputs may report FEWER findings, never a finding a
fully-sighted run does not have, and every finding it loses is named by a skip.

WHY THERE IS A tools/app_mirror/
-------------------------------
kredme-data is PUBLIC; the app repo KredMe-main is PRIVATE. CI here can never
check the app out, so "no app checkout" is not an edge case — it is every run
that will ever gate a PR. Two facts only the app can answer (its category
vocabulary, and which of our JSON keys its Dart reads) are therefore vendored
into tools/app_mirror/ by hand, with provenance. --app-root still wins when it
is a real checkout; when both are readable they are compared, and a stale mirror
is reported as L3.APP_CATEGORY_MIRROR_DRIFT. That drift check can only ever fire
on a machine that has the app, which is exactly why it has to exist.

USAGE
-----
    python3 tools/validate_cards.py                       working tree, everything
    python3 tools/validate_cards.py --target dev          check dev before testing
    python3 tools/validate_cards.py --target prod         check what users have now
    python3 tools/validate_cards.py --layer L4 --layer L8 run a subset
    python3 tools/validate_cards.py --card hdfc_bank_infinia   one card, full detail
    python3 tools/validate_cards.py --issuer "HDFC Bank"
    python3 tools/validate_cards.py --severity error      show errors only
    python3 tools/validate_cards.py --summary             one screen, no per-finding noise
    python3 tools/validate_cards.py --json out.json       machine-readable, full findings
    python3 tools/validate_cards.py --html report.html    readable report, self-contained
    python3 tools/validate_cards.py --baseline FILE       OPT-IN ratchet only
    python3 tools/validate_cards.py --write-baseline FILE

EXIT CODES
----------
    0   no errors and no warnings
    1   at least one error
    2   warnings but no errors
    3   could not run (bad flag, missing branch, unreadable data) — no verdict given

Four codes, and only four. A skipped check does NOT get one of its own and does
not change the one you get: automation gates on these numbers, and quietly
giving 2 a second meaning is how a tool starts lying to a script. A degraded run
is announced in the output and in the JSON (`meta.degraded`, `meta.skipped_count`,
`skipped_checks`), never by moving the exit code.

Note 3, not 2, for a bad flag: argparse's own default is 2, which is THIS tool's
code for "warnings but no errors — publishable". A typo in a CI step must never
be able to report the second-greenest result a gate can return.

No third-party packages. Python 3.12+. Run from anywhere.
"""

from __future__ import annotations

import argparse
import atexit
import datetime as _dt
import hashlib
import html as _html
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

# tools/ on the path so `checks` resolves as a namespace package from anywhere.
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from checks.base import Ctx, Finding, Skipped, ERROR, WARN, INFO  # noqa: E402

# --------------------------------------------------------------------------- #
# What the runner knows about the world
# --------------------------------------------------------------------------- #
LIVE_SEED = REPO / "seed"
LIVE_NEWS = REPO / "news"
DEV_BRANCH = "dev"
PROD_BRANCH = "main"
SEED_FILES = ("cards.json", "merchants.json")
MANIFEST = "manifest.json"
FEED = "feed.json"

# The app checkout that owns categories.json and pubspec.yaml. Sibling by
# convention (kredme-data/CLAUDE.md: "`../KredMe-main` — the app that reads this").
APP_ROOT_DEFAULT = REPO.parent / "KredMe-main"
APP_CATEGORIES_REL = "assets/data/categories/categories.json"

# Vendored copies of the two facts that only the app can answer. This repo is
# PUBLIC and the app repo is PRIVATE, so CI here can NEVER check the app out;
# without these, every CI run is blind, and a blind run of this tool used to
# invent 309 errors rather than admit it could not see. --app-root still wins
# when it is a real checkout: a mirror is only as good as its last refresh.
# They live under tools/ and NOT under seed/ on purpose — seed/ is the publish
# surface, and L3.MANIFEST_UNDECLARED_FILE actively advises adding any stray
# seed/*.json to the manifest "so it reaches users".
MIRROR_DIR = REPO / "tools" / "app_mirror"
MIRROR_CATEGORIES = MIRROR_DIR / "categories.json"
MIRROR_APP_KEYS = MIRROR_DIR / "app_json_keys.json"

# Order is the order they run and the order they print. L1 first on purpose: a
# card that fails L1 never loads in the app, so every later layer is describing
# a card no user can see.
LAYER_MODULES = (
    "c1_schema",
    "c2_vocabulary",
    "c3_referential",
    "c4_numeric",
    "c5_consistency",
    "c6_reachability",
    "c7_coherence",
    "c8_provenance",
    "c9_temporal",
)

ROW_BLOCKS = (
    "reward_rules",
    "exclusion_rules",
    "milestone_rules",
    "fuel_surcharge_rules",
    "redemption_rules",
)

SEV_ORDER = {ERROR: 0, WARN: 1, INFO: 2}
SEV_WORD = {ERROR: "error", WARN: "warning", INFO: "note"}

# A crash inside a check is a defect in the checking, not in the data. It is
# reported as an ERROR so it can never be mistaken for "this layer found
# nothing", and it is the one code a baseline may never suppress.
CRASH_CODE = "RUNNER.CHECK_CRASHED"


# --------------------------------------------------------------------------- #
# Cross-layer duplicates
# --------------------------------------------------------------------------- #
# Three defect families were being reported by two or three layers on the
# IDENTICAL (card_id, block, index, field) key. That inflated the error count
# the founder reads — 85 of 785 errors and 220 of 1,105 warnings were one defect
# counted twice — and, worse, the two layers disagreed with each other about how
# serious the same rows were (L2 said ERROR, L6 said WARN, on the same 220 keys).
#
# ONE defect, ONE owner, ONE severity. The owner is the layer that can PROVE
# engine behaviour, because that is the layer whose severity can be defended.
#
# Nothing is deleted: the duplicate is kept, at INFO, with its own evidence
# intact and a pointer to the owner. So no detail is lost, the row is still
# greppable under its old code, and it stops being counted as a second defect.
# The demotion happens ONLY when the owning layer actually ran and actually
# emitted a finding on that exact key — a `--layer L2` run is not silently
# softened, because there is no owner present to carry the defect.
DUPLICATE_FAMILIES = (
    # (owning code, codes that restate the same defect on the same key)
    ("L6.EXCLUSION_TYPE_INERT", ("L2.EXCLUSION_TYPE_INERT",)),
    ("L6.CHANNEL_NEVER_MATCHES", ("L2.CHANNEL_NOT_IN_VOCAB", "L2.CHANNEL_WRONG_LANE")),
    ("L4.CAP_NOT_A_NUMBER", ("L1.NUMERIC_FIELD_NOT_A_NUMBER",)),
)

_DUP_OWNER = {dup: owner for owner, dups in DUPLICATE_FAMILIES for dup in dups}


def defect_key(f: Finding):
    """The thing two layers can both be talking about."""
    return (f.card_id, f.block, f.index, f.field)


def demote_duplicates(results):
    """(results, n_demoted). Non-owning copies of a defect drop to INFO."""
    owned = defaultdict(set)          # owning code -> {defect_key}
    for _lid, f in results:
        if f.code in {o for o, _d in DUPLICATE_FAMILIES}:
            owned[f.code].add(defect_key(f))

    out, n_demoted = [], 0
    for lid, f in results:
        owner = _DUP_OWNER.get(f.code)
        if (owner and f.severity in (ERROR, WARN)
                and defect_key(f) in owned.get(owner, ())):
            f = Finding(**{**f.to_dict(), "severity": INFO})
            f.message = (f"{flat(f.message)} (Counted once, under {owner}, which owns "
                         f"this defect because it can prove what the engine does with "
                         f"the row. Kept here at note level for the extra detail above; "
                         f"it is the same rows, not a second defect.)")
            n_demoted += 1
        out.append((lid, f))
    return out, n_demoted


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


def skip(msg):
    """A check that did not run. Deliberately NOT the WARN prefix: WARN belongs
    to findings about the data, and --severity is documented as a display filter
    over findings. A skip is neither — it is never filtered out, at any severity,
    because the one thing that must never happen to it is going quiet."""
    print(f"  {C.Y}SKIP{C.X} {msg}")


# Where this run was asked to write its reports, recorded as soon as the flags
# are parsed so die() can invalidate them. See _invalidate_reports.
_REPORT_PATHS: dict = {}


def _invalidate_reports(msg: str, code: int) -> None:
    """Replace any report this run was asked to write with a 'could not run' stub.

    A run that stops does not just fail to write a report — it leaves the
    PREVIOUS run's report sitting at that path, and a CI step that publishes
    report.json then presents yesterday's verdict as today's. Demonstrated: a
    run that exited 3 on a bad --card left behind a complete, plausible-looking
    file claiming exit_code 1 and 2,606 findings, timestamped before the run
    that supposedly produced it.

    So a stopped run overwrites its own targets with a document that says, in
    the same fields a consumer already reads, that nothing was checked. Never
    raises: this runs on the way out of a failure and must not replace one
    error message with another.
    """
    stub = {
        "meta": {
            "generated_at": _dt.datetime.now(_dt.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "exit_code": code, "ran": False, "complete": False, "degraded": True,
            "error": flat(msg),
        },
        "scorecard": None,
        "headline": ["This run stopped before checking anything: " + flat(msg)],
        "honesty": HONESTY,
        "findings": [],
        "suppressed": [],
        "skipped_checks": [],
        "_read_this_first": (
            "findings is empty because NOTHING WAS CHECKED, not because nothing is "
            "wrong. Gate on meta.exit_code (3 = could not run), never on len(findings)."),
    }
    for kind, p in list(_REPORT_PATHS.items()):
        if not p:
            continue
        try:
            p = Path(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            if kind == "json":
                p.write_text(json.dumps(stub, indent=2, ensure_ascii=False,
                                        default=str) + "\n", encoding="utf-8")
            else:
                p.write_text(
                    "<!doctype html><meta charset=utf-8><title>Validator: could not run"
                    "</title><body style=\"font:16px system-ui;margin:3rem;max-width:44rem\">"
                    f"<h1>This run stopped before checking anything</h1><p>{_html.escape(flat(msg))}</p>"
                    "<p>Nothing on this page is a statement about the card data. "
                    "The previous report was removed so it could not be mistaken for "
                    "this one.</p>", encoding="utf-8")
        except Exception:
            pass


def die(msg, code=3):
    """Could not run. Deliberately NOT exit 1 — that means 'the data has an
    error', and a missing branch is not a statement about the data."""
    _invalidate_reports(msg, code)
    print(f"\n{C.R}{C.BOLD}Stopped:{C.X} {msg}\n")
    raise SystemExit(code)


def n(x) -> str:
    return f"{x:,}"


def pct(part, whole) -> str:
    return "0.0%" if not whole else f"{100.0 * part / whole:.1f}%"


def flat(s) -> str:
    """One line. A finding message is prose and may carry newlines."""
    return re.sub(r"\s+", " ", str(s or "")).strip()


# ------------------------------------------------------------------- git ----
# Copied from tools/kredme.py rather than imported: kredme.py runs argparse and
# owns the publish path, and this tool must never be able to trigger it.

def git(*args: str):
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args],
                           capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def git_bytes(*args: str):
    """git output as RAW bytes — never stripped. The text helper calls .strip(),
    which alters file content and makes every checksum comparison fail."""
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args],
                           capture_output=True, timeout=60)
        return r.returncode, r.stdout
    except Exception:
        return 1, b""


def git_head() -> str:
    code, out, _ = git("rev-parse", "HEAD")
    return out if code == 0 else "unknown"


def materialise(ref: str) -> Path:
    """Extract seed/ and news/ from a git ref into a temp dir.

    Same approach as tools/kredme.py:materialise — inspect a branch without
    checking it out, so validating never disturbs the operator's working tree.
    """
    import tempfile
    dest = Path(tempfile.mkdtemp(prefix=f"kredme-validate-{ref.replace('/', '-')}-"))
    # ~1.9 MB of the full card catalogue, one per --target dev/prod run. Left
    # behind, these accumulate silently on any machine that runs the gate on a
    # schedule. Removed when the process ends, however it ends.
    atexit.register(shutil.rmtree, str(dest), ignore_errors=True)
    (dest / "seed").mkdir(parents=True, exist_ok=True)
    (dest / "news").mkdir(parents=True, exist_ok=True)
    got = 0
    for relpath in [f"seed/{x}" for x in (*SEED_FILES, MANIFEST)] + [f"news/{FEED}"]:
        code, blob = git_bytes("show", f"{ref}:{relpath}")
        if code == 0:
            (dest / relpath).write_bytes(blob)
            got += 1
    if not got:
        die(f"branch '{ref}' has no data files — is it the right branch?")
    return dest


def data_dirs(target: str):
    """(seed_dir, news_dir, label) for 'dev', 'prod' or 'working'."""
    if target == "working":
        return LIVE_SEED, LIVE_NEWS, "working tree (seed/, news/)"
    ref = DEV_BRANCH if target == "dev" else PROD_BRANCH
    code, _, _ = git("rev-parse", "--verify", ref)
    if code != 0:
        die(f"branch '{ref}' not found locally. Try:  git fetch origin {ref}:{ref}")
    code, sha, _ = git("rev-parse", "--short", ref)
    tmp = materialise(ref)
    return tmp / "seed", tmp / "news", f"branch {ref} @ {sha or '?'}"


# -------------------------------------------------------------- loading ----

def read_json(path: Path):
    """(value, error_string). Never raises — an unreadable file is a finding,
    not a stack trace."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "file not found"
    except json.JSONDecodeError as e:
        return None, f"not valid JSON — {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def build_ctx(seed_dir: Path, news_dir: Path, app_root: Path | None,
              config: dict | None = None):
    """(ctx, load_notes). Loads every file the checks may read, exactly once."""
    notes = []
    cards, e = read_json(Path(seed_dir) / "cards.json")
    if e:
        die(f"seed/cards.json could not be read — {e}")
    merchants, e = read_json(Path(seed_dir) / "merchants.json")
    if e:
        notes.append(f"merchants.json: {e}")
    manifest, e = read_json(Path(seed_dir) / MANIFEST)
    if e:
        notes.append(f"manifest.json: {e}")
    news, e = read_json(Path(news_dir) / FEED)
    if e:
        notes.append(f"news/feed.json: {e}")

    root = Path(app_root) if app_root else None
    if not (root and root.is_dir()):
        root = None
    app_categories, cat_origin, cat_path, drift = resolve_categories(root, notes)
    app_keys, keys_origin = resolve_app_keys(root, notes)
    if root is None:
        notes.append(
            "no app checkout — the image check cannot run and will report itself SKIPPED"
            + ("" if cat_origin != "mirror" else
               "; the category vocabulary came from the vendored mirror instead"))

    # cards is passed through EXACTLY as parsed. Coercing a non-list to [] here
    # made L1.CARDS_FILE_NOT_A_LIST and L6.CARDS_FILE_NOT_A_LIST unreachable dead
    # code, and told the operator "seed/cards.json holds no cards" — "restore 383
    # cards from a backup" — when every card was present and only the wrapper was
    # wrong. Both check modules already guard with their own isinstance(); so does
    # Ctx.entries(). The runner's job is to report the file, not to launder it.
    if not isinstance(cards, list):
        notes.append(f"seed/cards.json is a JSON {type(cards).__name__} at the top level, "
                     f"not a list — the card list itself may be intact inside it")

    ctx = Ctx(
        seed_dir=Path(seed_dir), news_dir=Path(news_dir),
        cards=cards,
        merchants=merchants if merchants is not None else {},
        manifest=manifest if isinstance(manifest, dict) else {},
        news=news, app_categories=app_categories, app_root=root,
        config=dict(config or {}),
        categories_origin=cat_origin, categories_path=cat_path,
        categories_drift=drift,
        app_keys=app_keys, app_keys_origin=keys_origin,
    )
    return ctx, notes


# ------------------------------------------------- app-derived facts ----
# Two questions only the app can answer: which categories it recognises, and
# which of our JSON keys its Dart actually reads. A real checkout answers both.
# CI never has one, so both fall back to a hand-refreshed mirror under
# tools/app_mirror/. Order is always checkout first — the mirror is a copy, and
# a copy may be stale; when both are readable they are COMPARED, and the
# difference is reported as drift rather than quietly preferred either way.

def _mirror_doc(path: Path, notes: list):
    """The mirror's JSON object, or None. A broken mirror is a note, never a
    crash and never a silent empty answer."""
    doc, e = read_json(path)
    if e:
        notes.append(f"vendored mirror {path.name}: {e}")
        return None
    if not isinstance(doc, dict):
        notes.append(f"vendored mirror {path.name} is not a JSON object — ignored")
        return None
    return doc


def resolve_categories(root: Path | None, notes: list):
    """(categories, origin, path, drift). origin is 'app' / 'mirror' / None."""
    app_cats = None
    if root is not None:
        cats, e = read_json(root / APP_CATEGORIES_REL)
        if e:
            notes.append(f"app categories.json: {e}")
        elif not isinstance(cats, list) or not cats:
            notes.append("app categories.json is not a non-empty list — ignored")
        else:
            app_cats = cats

    doc = _mirror_doc(MIRROR_CATEGORIES, notes)
    mirror_cats = doc.get("categories") if doc else None
    if mirror_cats is not None and not isinstance(mirror_cats, list):
        notes.append("vendored mirror categories.json: 'categories' is not a list — ignored")
        mirror_cats = None

    # Drift is only computable when BOTH were readable. It is the only alarm
    # that the mirror has gone stale, and by construction it can only ever fire
    # on a machine that has the app — never in CI.
    drift = None
    if app_cats is not None and mirror_cats is not None:
        drift = category_drift(app_cats, mirror_cats)

    if app_cats is not None:
        return app_cats, "app", root / APP_CATEGORIES_REL, drift
    if mirror_cats:
        return mirror_cats, "mirror", MIRROR_CATEGORIES, drift
    return None, None, None, drift


def category_drift(app_cats, mirror_cats) -> list:
    """Differences between the app's category list and our vendored copy of it.

    [{'slug', 'kind', 'text'}]. Compared on the three fields the checks actually
    rely on — the id, the slug the data points at, and the parent the engine
    walks — not on the whole row, so a cosmetic display_name edit does not cry
    wolf while a renamed slug does.
    """
    def index(rows):
        out = {}
        for c in rows or []:
            if isinstance(c, dict) and isinstance(c.get("category_name"), str):
                out[c["category_name"]] = (c.get("id"), c.get("parent_id"))
        return out

    a, m = index(app_cats), index(mirror_cats)
    out = []
    for slug in sorted(set(a) - set(m)):
        out.append({"slug": slug, "kind": "missing_from_mirror",
                    "text": f"the app has '{slug}' and the mirror does not"})
    for slug in sorted(set(m) - set(a)):
        out.append({"slug": slug, "kind": "gone_from_app",
                    "text": f"the mirror still has '{slug}' and the app does not"})
    for slug in sorted(set(a) & set(m)):
        if a[slug] != m[slug]:
            out.append({"slug": slug, "kind": "id_or_parent_changed",
                        "text": f"'{slug}': app id/parent {a[slug]}, mirror {m[slug]}"})
    return out


def resolve_app_keys(root: Path | None, notes: list):
    """(mirrored answers dict or None, origin). Only consulted by
    Ctx.app_reads_json_key when there is no lib/ to measure directly."""
    if root is not None and (root / "lib").is_dir():
        return None, "app"          # measured live; the mirror is not needed
    doc = _mirror_doc(MIRROR_APP_KEYS, notes)
    keys = doc.get("keys_read_by_app") if doc else None
    if not isinstance(keys, dict):
        return None, None
    return {k: v for k, v in keys.items() if isinstance(v, bool)}, "mirror"


# ----------------------------------------------------------- the modules ----

def layer_id(name: str) -> str:
    """'c4_numeric' -> 'L4'. The id is the module's number, not its position, so
    a module can be removed without renumbering everything after it."""
    m = re.match(r"c(\d+)_", name)
    return f"L{m.group(1)}" if m else name.upper()


def load_checks(only: set | None = None):
    """[(layer_id, module_name, module_or_None, import_error_or_None)] in run order."""
    out = []
    for name in LAYER_MODULES:
        lid = layer_id(name)
        if only and lid not in only:
            continue
        try:
            mod = importlib.import_module(f"checks.{name}")
        except Exception as e:
            out.append((lid, name, None, f"{type(e).__name__}: {e}"))
            continue
        out.append((lid, name, mod, None))
    return out


def crash_finding(lid: str, module_name: str, exc: BaseException, where: str) -> Finding:
    tb = traceback.extract_tb(exc.__traceback__)
    spot = ""
    if tb:
        last = tb[-1]
        spot = f" at {Path(last.filename).name}:{last.lineno}"
    return Finding(
        severity=ERROR, code=CRASH_CODE,
        message=(f"The {lid} check module 'checks/{module_name}.py' {where} and was "
                 f"skipped, so nothing it looks for was checked at all. This is a bug "
                 f"in the checking code, not necessarily a defect in the data."),
        block=module_name,
        evidence=f"{type(exc).__name__}: {exc}{spot}",
        fix=(f"Fix checks/{module_name}.py, then run again. Until then treat this run "
             f"as INCOMPLETE — do not read a clean {lid} as a clean {lid}."),
        impact=("A whole class of defect went unexamined. Any 'safe to publish' "
                "conclusion drawn from this run is unsupported."),
    )


def run_layer(lid: str, module_name: str, mod):
    """(findings, skips, seconds). A module that raises never kills the run."""
    t0 = time.perf_counter()
    if mod is None:
        return [crash_finding(lid, module_name, ImportError("module did not import"),
                              "could not be imported")], [], 0.0
    try:
        found = mod.run(_RUN_CTX)
    except BaseException as e:  # noqa: BLE001 — a check may raise anything
        # ONLY Ctrl-C gets to end the run. SystemExit used to be re-raised too,
        # which meant a module that called sys.exit(0) — directly, or through
        # argparse, or through a die()-style helper copied from kredme.py —
        # terminated the whole run at exit 0, with no layer results, no
        # scorecard and no verdict. A silently green run is the worst failure
        # shape this tool has, and it is the exact mistake CRASH_CODE exists to
        # make impossible. A check that exits is a crashed check.
        if isinstance(e, KeyboardInterrupt):
            raise
        where = ("called sys.exit() instead of returning findings"
                 if isinstance(e, SystemExit) else "crashed")
        return [crash_finding(lid, module_name, e, where)], [], time.perf_counter() - t0
    secs = time.perf_counter() - t0
    if not isinstance(found, list):
        return [crash_finding(lid, module_name,
                              TypeError(f"run() returned {type(found).__name__}, not a list"),
                              "broke the check contract")], [], secs
    # A module returns two kinds of thing in one list: Findings about the DATA,
    # and Skipped records about ITSELF. They are separated here and never mixed
    # again — a skip must not be able to reach a severity count, an exit code or
    # a baseline, and a finding must not be able to hide inside the skip list.
    clean = [f for f in found if isinstance(f, Finding)]
    skips = [s for s in found if isinstance(s, Skipped)]
    for s in skips:
        s.layer = lid
    junk = len(found) - len(clean) - len(skips)
    if junk:
        clean.append(crash_finding(
            lid, module_name,
            TypeError(f"{junk} returned item(s) were neither Findings nor Skipped"),
            "broke the check contract"))
    return clean, skips, secs


# `run_layer` reads the ctx from module scope so the signature stays small and a
# test can drive it with a hand-made module. set_ctx() is the only way in.
_RUN_CTX: Ctx | None = None


def set_ctx(ctx):
    global _RUN_CTX
    _RUN_CTX = ctx


def run_all(checks, on_layer=None):
    """([(layer_id, Finding)], [Skipped], timings). Order preserved; nothing suppressed."""
    results, skipped, timings = [], [], {}
    for lid, name, mod, imp_err in checks:
        if imp_err and mod is None:
            found, skips, secs = [crash_finding(lid, name, ImportError(imp_err),
                                                "could not be imported")], [], 0.0
        else:
            found, skips, secs = run_layer(lid, name, mod)
        timings[lid] = secs
        for f in found:
            results.append((lid, f))
        skipped.extend(skips)
        if on_layer:
            on_layer(lid, getattr(mod, "LAYER", lid), found, secs, skips)
    return results, skipped, timings


# ------------------------------------------------------------- baseline ----

def fingerprint(f: Finding) -> str:
    """The identity a baseline suppresses on.

    Deliberately narrow — code, card, block, row index and field. A looser key
    (code alone, say) would let one recorded defect silence a hundred new ones,
    which is the exact failure this whole tool exists to end. Narrow means a
    baseline goes stale and stops suppressing; that direction is safe.

    A PORTFOLIO finding — one that describes the file as a whole — has no
    card_id, usually no index and often no field, so those five parts collapsed
    to the code alone: precisely the looser key the paragraph above rules out.
    96 of 2,582 keys in a baseline of this catalogue were in that degenerate
    form across 39 codes, so recording "some card is on an unrecognised network"
    once silenced every future unrecognised network, forever, on any card. The
    same collapse also made 27 findings share 7 keys inside a single run, so the
    baseline under-recorded what it was hiding.

    For those findings — and only those — the population they describe is folded
    in as a short hash of the message and evidence. Change which cards are in the
    set and the key changes, so the new state is NOT suppressed. That keeps the
    ratchet useful (an unchanged file stays quiet) while restoring the property
    the docstring claims: one recorded defect can never silence a new one.
    """
    idx = "" if f.index is None else str(f.index)
    base = f"{f.code}|{f.card_id or ''}|{f.block or ''}|{idx}|{f.field or ''}"
    if f.card_id:
        return base
    payload = f"{flat(f.message)}\x00{flat(f.evidence)}".encode("utf-8", "replace")
    return f"{base}|{hashlib.sha256(payload).hexdigest()[:12]}"


def load_baseline(path: Path):
    data, e = read_json(Path(path))
    if e:
        die(f"baseline {path} could not be read — {e}")
    if isinstance(data, dict) and isinstance(data.get("fingerprints"), list):
        return {str(x) for x in data["fingerprints"]}
    if isinstance(data, list):
        return {str(x) for x in data}
    die(f"baseline {path} is not a recognised baseline file "
        f"(expected an object with a 'fingerprints' list)")


def apply_baseline(results, baseline: set | None):
    """(kept, suppressed). Suppressed findings are RETURNED, never discarded —
    the verdict has to be able to count and name them."""
    if not baseline:
        return list(results), []
    kept, gone = [], []
    for lid, f in results:
        if f.code != CRASH_CODE and fingerprint(f) in baseline:
            gone.append((lid, f))
        else:
            kept.append((lid, f))
    return kept, gone


def write_baseline(path: Path, results, source: str) -> int:
    fps = sorted({fingerprint(f) for _lid, f in results if f.code != CRASH_CODE})
    payload = {
        "_note": ("An OPT-IN ratchet for tools/validate_cards.py. Every fingerprint here "
                  "is a defect that EXISTS in the data — this file records them, it does "
                  "not fix them. A run using --baseline still counts and reports every "
                  "suppressed finding. Never treat a suppressed finding as a fixed one."),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "count": len(fps),
        "fingerprints": fps,
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return len(fps)


# -------------------------------------------------------------- filters ----

def parse_layers(values):
    if not values:
        return None
    known = {layer_id(m) for m in LAYER_MODULES}
    out = set()
    for v in values:
        s = str(v).strip().upper()
        if s.isdigit():
            s = "L" + s
        if s not in known:
            die(f"unknown layer '{v}'. Known layers: {', '.join(sorted(known))}")
        out.add(s)
    return out


def issuer_of(ctx, card_id):
    for _i, _e, inner, cid in ctx.entries():
        if cid == card_id:
            return inner.get("issuer")
    return None


def card_ids_for_issuer(ctx, issuer: str) -> set:
    want = re.sub(r"[^a-z0-9]+", "", (issuer or "").lower())
    out = set()
    for _i, _e, inner, cid in ctx.entries():
        got = re.sub(r"[^a-z0-9]+", "", str(inner.get("issuer") or "").lower())
        if cid and want and got == want:
            out.add(cid)
    return out


def filter_results(results, cards: set | None, min_sev: str | None):
    """cards=None means no card filter. min_sev filters DISPLAY only."""
    out = []
    floor = SEV_ORDER.get(min_sev, 9) if min_sev else 9
    for lid, f in results:
        if cards is not None and (f.card_id not in cards):
            continue
        if min_sev and SEV_ORDER.get(f.severity, 9) > floor:
            continue
        out.append((lid, f))
    return out


def sort_key(pair):
    lid, f = pair
    return (SEV_ORDER.get(f.severity, 9), lid, f.code, f.card_id or "",
            f.index if f.index is not None else -1)


# ------------------------------------------------------------ scorecard ----

# L8._grade returns exactly one of A B C D F N/A. Anchored to those six so a
# future grade the runner does not understand shows up as missing, not as a
# silently mangled letter.
_GRADE_RE = re.compile(r"\bgrade=(N/A|[A-F])\b")
_SOURCED_RE = re.compile(r"issuer-sourced\s+(\d+)\s*/\s*(\d+)")


def grade_distribution(results):
    """{grade: count} read out of L8.CARD_GRADE.

    L8 owns the grading maths; re-deriving it here would let the two drift and
    give the founder two different answers to the same question. Parsed
    defensively — if L8 did not run, this is simply empty.
    """
    grades = {}
    for _lid, f in results:
        if f.code != "L8.CARD_GRADE" or not f.card_id:
            continue
        m = _GRADE_RE.search(f.evidence or "")
        if m:
            grades[f.card_id] = m.group(1)
    return grades


_RULES_RE = re.compile(r"\brules=(\d+)")
_SOURCED_N_RE = re.compile(r"\bissuer_sourced=(\d+)")


def sourced_share(results, ids=None):
    """(sourced, total) reward rates backed by an issuer link, or None.

    Summed from L8's own per-card ledger (L8.CARD_GRADE evidence) so the answer
    stays correct when the view is filtered to one card or one issuer. Verified
    to agree exactly with L8.HEADLINE_VERIFIED_SHARE over the full catalogue
    (26/1279); that finding is the fallback if the per-card rows are absent.
    None means 'not measured', never 0.
    """
    sourced = total = 0
    seen = False
    for _lid, f in results:
        if f.code != "L8.CARD_GRADE" or not f.card_id:
            continue
        if ids is not None and f.card_id not in ids:
            continue
        ev = f.evidence or ""
        m, s = _RULES_RE.search(ev), _SOURCED_N_RE.search(ev)
        if m:
            seen = True
            total += int(m.group(1))
        if s:
            sourced += int(s.group(1))
    if seen:
        return sourced, total
    for _lid, f in results:
        if f.code == "L8.HEADLINE_VERIFIED_SHARE":
            m = _SOURCED_RE.search(f.evidence or "")
            if m:
                return int(m.group(1)), int(m.group(2))
    return None


def block_counts(ctx):
    out = Counter()
    cards = 0
    for _i, e, _inner, _cid in ctx.entries():
        cards += 1
        for b in ROW_BLOCKS:
            rows = e.get(b)
            if isinstance(rows, list):
                out[b] += len(rows)
    return cards, out


def card_counts(ctx):
    """(entries, distinct ids). The header used to print the first and the
    scorecard the second, twelve lines apart, so a file with a repeated card id
    reported two different catalogue sizes for itself and the founder's
    "0 of N cards are safe" quietly used the smaller N. Duplicate ids are a live
    risk here — L3.DUPLICATE_CARD_NAME already fires on this catalogue. One
    helper, so both places say the same thing, and say when they differ.
    """
    entries = 0
    ids = set()
    for _i, _e, _inner, cid in ctx.entries():
        entries += 1
        if cid:
            ids.add(cid)
    return entries, len(ids)


def catalogue_line(entries: int, distinct: int, blocks) -> str:
    rows = " · ".join(f"{n(v)} {k.replace('_', ' ')}" for k, v in blocks.items() if v)
    if entries != distinct:
        head_ = f"{n(entries)} entries under {n(distinct)} distinct card ids"
    else:
        head_ = f"{n(distinct)} cards"
    return f"{head_}{' · ' + rows if rows else ''}"


def scorecard(ctx, results, suppressed, layers_run, timings, scope=None,
              all_results=None, verdict_results=None, demoted=0, skipped=()):
    """What is IN THE DATA — suppressed findings included.

    This is the split that keeps the tool honest. A baseline is a statement
    about what we have agreed not to fail the build on; it is not a statement
    about the file. So every count here is over results + suppressed, and only
    the exit code is computed on `results` alone. Without this, running with a
    full baseline would print "383 of 383 cards are safe to show a user", which
    is the exact sentence this tool exists to stop anyone from writing.
    """
    _cards, blocks = block_counts(ctx)
    file_entries, file_cards = card_counts(ctx)
    all_ids = {cid for _i, _e, _inner, cid in ctx.entries() if cid}
    # A --card / --issuer view must not count the cards it never looked at as
    # safe. The denominator is the cards actually in scope, and only those.
    if scope is not None:
        all_ids = all_ids & set(scope)
    cards = len(all_ids)

    everything = list(results) + list(suppressed)

    by_sev = Counter(f.severity for _l, f in everything)
    by_sev_new = Counter(f.severity for _l, f in results)
    # What the exit code is actually computed on. Under --card / --issuer this
    # is WIDER than `results`: a finding that names no single card still has to
    # fail the build, or `--card X` returns 0 on a file the same run found 785
    # errors in. Unfiltered, the two sets are identical.
    vres = list(results if verdict_results is None else verdict_results)
    by_sev_verdict = Counter(f.severity for _l, f in vres)
    excluded = [f for _l, f in vres if f.card_id is None] if scope is not None else []
    by_layer = defaultdict(Counter)
    for lid, f in everything:
        by_layer[lid][f.severity] += 1

    err_cards, warn_cards = set(), set()
    for _lid, f in everything:
        if not f.card_id:
            continue
        if f.severity == ERROR:
            err_cards.add(f.card_id)
        elif f.severity == WARN:
            warn_cards.add(f.card_id)

    clean_cards = all_ids - (err_cards | warn_cards)
    portfolio = [f for _l, f in everything
                 if not f.card_id and f.severity in (ERROR, WARN)]

    # Grades and the sourced share are per-card facts L8 computes for the whole
    # catalogue. Under a filter they are read from the unfiltered run and then
    # restricted to the cards in scope, so a filtered view still gets a real
    # number instead of "not measured".
    ledger = list(all_results) if all_results is not None else everything
    grades = {cid: g for cid, g in grade_distribution(ledger).items() if cid in all_ids}
    share = sourced_share(ledger, all_ids)

    return {
        "cards": cards,
        "file_entries": file_entries,
        "file_cards": file_cards,
        "scoped": scope is not None,
        "card_ids": sorted(all_ids),
        "blocks": dict(blocks),
        # over the whole file, suppressed included
        "by_severity": {k: by_sev.get(k, 0) for k in (ERROR, WARN, INFO)},
        # over what a baseline did NOT suppress, inside the current scope
        "by_severity_new": {k: by_sev_new.get(k, 0) for k in (ERROR, WARN, INFO)},
        # what the exit code and the verdict sentence are computed on
        "by_severity_verdict": {k: by_sev_verdict.get(k, 0) for k in (ERROR, WARN, INFO)},
        "scoped_file_wide": {
            "total": len(excluded),
            ERROR: sum(1 for f in excluded if f.severity == ERROR),
            WARN: sum(1 for f in excluded if f.severity == WARN),
        },
        "cross_layer_duplicates_demoted": demoted,
        # Checks that could not run. Counted apart from every severity above,
        # because a skip is a fact about this RUN, not about the data — but
        # printed next to them, because a run that checked less must never be
        # mistaken for a run that found less.
        "skipped_checks": [s.to_dict() for s in skipped],
        "skipped_count": len(skipped),
        "degraded": bool(skipped),
        "categories_source": ctx.categories_source(),
        "categories_origin": ctx.categories_origin,
        "app_keys_origin": ctx.app_keys_origin,
        "by_layer": {lid: dict(by_layer[lid]) for lid in layers_run},
        "layers_run": list(layers_run),
        "timings": {k: round(v, 3) for k, v in timings.items()},
        "total_findings": len(everything),
        "suppressed": len(suppressed),
        "cards_clean": len(clean_cards),
        "cards_with_error": len(err_cards),
        "cards_with_warn_only": len(warn_cards - err_cards),
        "portfolio_findings": len(portfolio),
        "grades": dict(Counter(grades.values())),
        "graded_cards": len(grades),
        "sourced_rules": None if share is None else share[0],
        "total_rules_measured": None if share is None else share[1],
    }


def headline(sc, complete: bool) -> list:
    """The one sentence the founder reads. Every number in it is defined below it.

    Every qualifier here exists because the sentence is the part that gets
    pasted into a deck. A partial run and an incomplete run both produce a
    HIGHER 'safe' count than the truth, so neither may print the plain sentence.
    """
    total = sc["cards"]
    safe = sc["cards_clean"]
    broken = sc["cards_with_error"]
    partial = len(sc["layers_run"]) < len(LAYER_MODULES)
    scoped = " matching this filter" if sc.get("scoped") else ""
    share = ""
    if sc["sourced_rules"] is None:
        # "L8 did not run" is a claim about which checks executed, and it was
        # being made off `sourced_rules is None` — which is also true when L8 ran
        # perfectly and simply found no reward rule to grade. This sentence gets
        # pasted into a deck; it may not misreport what was checked.
        why = ("L8 did not run" if "L8" not in sc["layers_run"]
               else "L8 ran but found no reward rule to grade")
        share = ("and the share of reward numbers backed by an issuer source was NOT "
                 f"MEASURED on this run ({why})")
    else:
        where = "on these cards" if sc.get("scoped") else "in this file"
        share = (f"and {pct(sc['sourced_rules'], sc['total_rules_measured'])} of the reward "
                 f"numbers {where} ({n(sc['sourced_rules'])} of "
                 f"{n(sc['total_rules_measured'])}) are backed by a link to the issuer's "
                 f"own website")
    if partial:
        ran = ", ".join(sc["layers_run"])
        lines = [
            f"Against {ran} ONLY, {n(safe)} of {n(total)} cards{scoped} are clean and "
            f"{n(broken)} have a defect that changes a number a user sees; {share}.",
            f"This is a PARTIAL run — {len(LAYER_MODULES) - len(sc['layers_run'])} of "
            f"{len(LAYER_MODULES)} layers did not run, so no card here has been shown to be "
            f"safe. Run without --layer before quoting a safety number.",
        ]
    else:
        lines = [
            f"{n(safe)} of {n(total)} cards{scoped} are safe to show a user without "
            f"further checking; {n(broken)} have a defect that changes a number a user "
            f"sees; {share}.",
        ]
    if sc["suppressed"]:
        lines.append(
            f"{n(sc['suppressed'])} of the findings counted above are suppressed by a "
            f"baseline. They are counted here because they are still in the data — the "
            f"baseline only stops them failing the build.")
    if not complete:
        lines.append("This run was INCOMPLETE — a check module failed, so the numbers "
                     "above are a floor, not a total.")
    return lines


HONESTY = (
    "What this tool CANNOT prove: not one of these nine checks opens a bank's website. "
    "Every layer compares this file against itself and against the app's own Dart code. "
    "A reward rate can pass all nine and still be wrong, because the issuer changed it "
    "and nobody told us. Passing here means READABLE BY THE APP AND INTERNALLY "
    "CONSISTENT. It does not mean CORRECT, and it must never be quoted as 'our card "
    "data is validated' or 'verified'. The only number on this page that speaks to "
    "correctness is the issuer-sourced share."
)


# --------------------------------------------------------------- console ----

def finding_line(lid, f, detail=False):
    where = []
    if f.card_id:
        where.append(f.card_id)
    if f.block:
        where.append(f.block if f.index is None else f"{f.block}#{f.index}")
    if f.field:
        where.append(f.field)
    loc = f"{C.DIM}[{' · '.join(where)}]{C.X} " if where else ""
    line = f"{C.DIM}{f.code}{C.X} {loc}{flat(f.message)}"
    emit = err if f.severity == ERROR else warn if f.severity == WARN else info
    emit(line)
    if detail:
        for label, val in (("evidence", f.evidence), ("impact", f.impact), ("fix", f.fix)):
            if val:
                print(f"         {C.DIM}{label}:{C.X} {flat(val)}")


def print_header(target, label, ctx, notes, layers_run, filters):
    head("KredMe card-data validator")
    info(f"target       {target} — {label}")
    _entries, blocks = block_counts(ctx)
    entries, distinct = card_counts(ctx)
    info(f"catalogue    {catalogue_line(entries, distinct, blocks)}")
    info(f"app checkout {ctx.app_root if ctx.app_root else 'not found'}")
    # Never print a category count without saying where it came from. A mirror
    # and a checkout produce identical-looking numbers and are not the same
    # claim: one is measured, the other is as good as its last hand refresh.
    info(f"categories   {ctx.categories_source()}"
         f"{'' if not ctx.app_categories else f' — {n(len(ctx.app_categories))} categories'}")
    info(f"layers       {', '.join(layers_run)}")
    for f in filters:
        info(f"filter       {f}")
    for note in notes:
        warn(note)


def print_layer(lid, layer_label, shown, secs, summary, detail, skips=()):
    label = layer_label or lid
    counts = Counter(f.severity for _lid, f in shown)
    head(f"{label}  {C.DIM}({secs*1000:.0f} ms){C.X}")
    for s in skips:
        skip(f"{C.BOLD}{s.code}{C.X} — {flat(s.what)}")
    if summary:
        if not counts:
            ok("nothing to report" if not skips else
               "nothing to report from the checks that ran")
        else:
            bits = []
            if counts.get(ERROR):
                bits.append(f"{C.R}{counts[ERROR]} error(s){C.X}")
            if counts.get(WARN):
                bits.append(f"{C.Y}{counts[WARN]} warning(s){C.X}")
            if counts.get(INFO):
                bits.append(f"{C.DIM}{counts[INFO]} note(s){C.X}")
            info(" · ".join(bits))
        return
    if not shown:
        ok("nothing to report" if not skips else
           "nothing to report from the checks that ran")
        return
    for lid2, f in sorted(shown, key=lambda x: sort_key(x)):
        finding_line(lid2, f, detail=detail)


def print_scorecard(sc, complete):
    head("Confidence scorecard")
    line = catalogue_line(sc["file_entries"], sc["file_cards"], sc["blocks"])
    if sc.get("scoped"):
        info(f"in scope     {n(sc['cards'])} cards  {C.DIM}(whole file: {line}){C.X}")
    else:
        info(f"catalogue    {line}")

    s = sc["by_severity"]
    info(f"findings     {C.R}{n(s[ERROR])} error(s){C.X} · "
         f"{C.Y}{n(s[WARN])} warning(s){C.X} · {C.DIM}{n(s[INFO])} note(s){C.X}"
         f"   {C.DIM}(everything below counts the whole file, including anything "
         f"a baseline is hiding from the exit code){C.X}")
    if sc["suppressed"]:
        warn(f"of those, {n(sc['suppressed'])} are suppressed by the baseline — still "
             f"in the data, just not failing the build")

    # Right under the finding counts, because it qualifies them: these numbers
    # are the result of however many checks actually ran.
    ns = sc.get("skipped_count", 0)
    if ns:
        skip(f"checks run   {n(ns)} check(s) SKIPPED — this run could not see everything "
             f"it needs, so the counts above are a FLOOR, not a total")
    else:
        info(f"checks run   every check had the inputs it needs — 0 skipped")

    for lid in sc["layers_run"]:
        c = sc["by_layer"].get(lid, {})
        e, w, i = c.get(ERROR, 0), c.get(WARN, 0), c.get(INFO, 0)
        bar = f"{e:>5} error  {w:>5} warn  {i:>5} note"
        info(f"  {lid:<4} {bar}   {C.DIM}{sc['timings'].get(lid, 0)*1000:.0f} ms{C.X}")

    if sc["graded_cards"]:
        order = ["A", "B", "C", "D", "F", "N/A"]
        got = sc["grades"]
        parts = [f"{g} {n(got.get(g, 0))}" for g in order if got.get(g)]
        extra = [f"{g} {n(v)}" for g, v in sorted(got.items()) if g not in order]
        info(f"verification grades over {n(sc['graded_cards'])} cards: "
             + " · ".join(parts + extra))
    else:
        info("verification grades: not measured ("
             + ("L8 did not run" if "L8" not in sc["layers_run"]
                else "L8 ran but graded no card") + ")")

    if sc.get("cross_layer_duplicates_demoted"):
        info(f"{n(sc['cross_layer_duplicates_demoted'])} finding(s) were the same defect "
             f"reported by a second layer on the same row — counted once, under the "
             f"layer that owns it, and kept as notes so no detail is lost")

    if sc["sourced_rules"] is not None:
        info(f"issuer-sourced {n(sc['sourced_rules'])} of {n(sc['total_rules_measured'])} "
             f"reward rates ({pct(sc['sourced_rules'], sc['total_rules_measured'])})")

    print_skipped(sc.get("skipped_checks") or [])

    print()
    for line in headline(sc, complete):
        print(f"  {C.BOLD}{line}{C.X}")
    print()
    tail = ("Findings that describe the file as a whole are not shown in a filtered view."
            if sc.get("scoped") else
            f"{n(sc['portfolio_findings'])} finding(s) describe the file as a whole and "
            f"name no single card.")
    info(f"'safe' = no error and no warning names that card. "
         f"{n(sc['cards_with_warn_only'])} more cards carry a warning but no error. {tail}")
    print()
    print(f"  {C.DIM}{_wrap(HONESTY, 92, '  ')}{C.X}")


def print_skipped(skips):
    """The checks that did not run, spelled out.

    This block is the whole point of having a skip at all. A check that goes
    quiet when it loses its inputs is indistinguishable from a check that
    passed, and the counts above would then be read as a total when they are
    only a floor.
    """
    if not skips:
        return
    head(f"Checks that DID NOT RUN  {C.DIM}({len(skips)}){C.X}")
    print(f"  {C.Y}These did not pass. They did not run. Nothing below is evidence "
          f"that the data is clean in these respects.{C.X}\n")
    for s in skips:
        lid = s.get("layer") or ""
        print(f"  {C.Y}SKIP{C.X} {C.BOLD}{s.get('code','')}{C.X}"
              f"{f'  {C.DIM}({lid}){C.X}' if lid else ''}")
        info(f"not checked  {_wrap(flat(s.get('what')), 84, '               ')}")
        info(f"because      {_wrap(flat(s.get('reason')), 84, '               ')}")
        if s.get("impact"):
            info(f"so do NOT    {_wrap(flat(s['impact']), 84, '               ')}")
        if s.get("codes"):
            info(f"blind to     {', '.join(s['codes'])}")
        info(f"to restore   {_wrap(flat(s.get('restore')), 84, '               ')}")
        print()


def _wrap(text, width, indent):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return f"\n{indent}".join(lines)


def exit_code_for(results) -> int:
    sev = {f.severity for _l, f in results}
    if ERROR in sev:
        return 1
    if WARN in sev:
        return 2
    return 0


def print_verdict(results, sc, code, complete, baseline_path):
    """The one line that gets screenshotted.

    It may never read as clean while findings exist. A baseline changes the exit
    code — that is what a ratchet is for — but it may not change the sentence
    into a claim the data does not support, which is the exact failure mode
    tools/kredme.py has today ("0 errors, 0 warning(s)" under seven WARN lines).
    """
    head("Verdict")
    s = sc.get("by_severity_verdict") or sc["by_severity_new"]
    e, w, i = s[ERROR], s[WARN], s[INFO]
    sup = sc["suppressed"]

    # A --layer run refuses the word "safe" and says so. A --card / --issuer run
    # used to keep the full unqualified vocabulary while silently dropping every
    # file-wide finding from both the verdict AND the exit code, so `--card X`
    # printed "✓ no errors and no warnings" and returned 0 on a file the same run
    # had just found 785 errors in. Those findings are back in the counts above;
    # this line says they are there and that they are not about the card asked for.
    fw = sc.get("scoped_file_wide") or {}
    if sc.get("scoped") and fw.get("total"):
        print(f"  {C.Y}SCOPED{C.X} — {n(fw['total'])} finding(s) describe the whole file "
              f"rather than the card(s) you asked for ({n(fw.get(ERROR, 0))} error(s), "
              f"{n(fw.get(WARN, 0))} warning(s)). They are counted in this verdict and in "
              f"the exit code, because they are true of the data either way.")

    if code == 1:
        print(f"  {C.R}{C.BOLD}✗ NOT safe to publish{C.X} — {n(e)} error(s), "
              f"{n(w)} warning(s), {n(i)} note(s)")
    elif code == 2:
        print(f"  {C.Y}{C.BOLD}! publishable, but not clean{C.X} — 0 errors, "
              f"{n(w)} warning(s), {n(i)} note(s)")
    elif sup:
        # Exit 0, but the file is not clean. Say the second half in the same breath.
        print(f"  {C.Y}{C.BOLD}! nothing NEW, but this file is NOT clean{C.X} — "
              f"0 new errors and 0 new warnings, and {n(sup)} known finding(s) "
              f"still present in the data")
    elif i:
        print(f"  {C.G}{C.BOLD}✓ no errors and no warnings{C.X} — "
              f"{n(i)} note(s) recorded for information")
    else:
        print(f"  {C.G}{C.BOLD}✓ no findings at all{C.X}")

    if sup:
        print(f"  {C.Y}{n(sup)} finding(s) suppressed by baseline {baseline_path}{C.X} — "
              f"they are STILL IN THE DATA and a user can still be shown them. This run is "
              f"not clean; it is clean-except-for-{n(sup)}. Run without --baseline to see "
              f"them.")

    # A skip cannot move the exit code — 0/1/2/3 are a contract other scripts
    # gate on, and inventing a fourth meaning for one of them is how a tool
    # starts lying to automation. It CAN, and must, qualify the sentence.
    ns = sc.get("skipped_count", 0)
    if ns:
        print(f"  {C.Y}{C.BOLD}DEGRADED{C.X} — {n(ns)} check(s) could not run, so the "
              f"counts above are a FLOOR. This run checked less than a full one; it did "
              f"not find less. See 'Checks that DID NOT RUN' above before reading this "
              f"verdict as a clean bill of health.")
    if not complete:
        print(f"  {C.R}A check module failed. This run is INCOMPLETE — the counts above "
              f"are a floor.{C.X}")

    print(f"\n  {C.DIM}exit {code} — " + {
        0: "no errors and no warnings",
        1: "at least one error: a user can be shown a wrong number, or a card can vanish",
        2: "warnings but no errors: nothing is broken, but value is being lost",
    }.get(code, "could not run") + f"{C.X}")
    print(f"  {C.DIM}0 = clean · 1 = error(s) · 2 = warning(s) only · 3 = could not run{C.X}")


# ------------------------------------------------------------------ html ----

_HTML_CSS = """
:root{color-scheme:light dark;
 --bg:#fbfaf8;--fg:#1c1b19;--muted:#6f6d67;--line:#e5e2db;--panel:#ffffff;
 --err:#a3251d;--errbg:#fbeceb;--warn:#7c5306;--warnbg:#fdf3e0;
 --info:#3f5f8f;--infobg:#eef2f8;--chip:#f1efe9;--accent:#1c1b19;}
@media (prefers-color-scheme:dark){:root{
 --bg:#141416;--fg:#eceae5;--muted:#9a978f;--line:#2c2c30;--panel:#1c1c1f;
 --err:#ff8b80;--errbg:#3a1f1d;--warn:#f0bd63;--warnbg:#332614;
 --info:#8fb2e0;--infobg:#1c2735;--chip:#26262a;--accent:#eceae5;}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:1200px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:18px;margin:36px 0 12px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:0 0 24px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:0 0 16px}
.headline{font-size:19px;line-height:1.5;font-weight:600;margin:0}
.honesty{border-left:3px solid var(--warn);background:var(--warnbg);color:var(--fg);
 padding:14px 16px;border-radius:0 8px 8px 0;font-size:13.5px;margin:16px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.stat .v{font-size:24px;font-weight:650;letter-spacing:-.02em;margin-top:2px}
.stat .n{font-size:12px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
/* The findings table carries long prose and long unbroken URLs. Fixed layout
   plus anywhere-wrapping keeps the message column on screen instead of pushing
   it off the right edge behind a horizontal scrollbar. */
#ft{table-layout:fixed}
#ft td{overflow-wrap:anywhere}
#ft th:nth-child(1),#ft td:nth-child(1){width:92px}
#ft th:nth-child(2),#ft td:nth-child(2){width:215px}
#ft th:nth-child(3),#ft td:nth-child(3){width:180px}
#ft th:nth-child(5),#ft td:nth-child(5){width:64px}
#ft code{overflow-wrap:anywhere}
@media (max-width:860px){#ft{table-layout:auto;min-width:720px}}
th{position:sticky;top:0;background:var(--bg);cursor:pointer;user-select:none;
 font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);white-space:nowrap}
th:hover{color:var(--fg)}
tbody tr:hover{background:var(--chip)}
code{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--chip);
 padding:1px 5px;border-radius:4px}
.sev{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.05em;
 padding:2px 7px;border-radius:5px}
.sev.ERROR{color:var(--err);background:var(--errbg)}
.sev.WARN{color:var(--warn);background:var(--warnbg)}
.sev.INFO{color:var(--info);background:var(--infobg)}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
input,select{font:inherit;font-size:13px;padding:7px 10px;border:1px solid var(--line);
 border-radius:7px;background:var(--panel);color:var(--fg)}
input{flex:1;min-width:220px}
.meta{color:var(--muted);font-size:12px}
details{border:1px solid var(--line);border-radius:9px;background:var(--panel);
 padding:10px 14px;margin:0 0 8px}
summary{cursor:pointer;font-weight:600;font-size:14px}
summary::marker{color:var(--muted)}
.gradechip{display:inline-block;font-size:11px;font-weight:700;padding:1px 7px;
 border-radius:5px;background:var(--chip);color:var(--muted);margin-left:8px}
.det{font-size:12.5px;color:var(--muted);margin-top:3px}
.det b{color:var(--fg);font-weight:600}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.empty{padding:26px;text-align:center;color:var(--muted)}
"""

_HTML_JS = r"""
var F = DATA.findings, byCard = {};
F.forEach(function(f){ var k=f.card_id||"(whole file)"; (byCard[k]=byCard[k]||[]).push(f); });

var q=document.getElementById('q'), sv=document.getElementById('sv'), ly=document.getElementById('ly');
var tb=document.getElementById('tb'), cnt=document.getElementById('cnt');
var sortCol='severity', sortDir=1;
var RANK={ERROR:0,WARN:1,INFO:2};

function esc(s){var d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}

function current(){
  var t=(q.value||'').toLowerCase(), s=sv.value, l=ly.value;
  return F.filter(function(f){
    if(s && f.severity!==s) return false;
    if(l && f.layer!==l) return false;
    if(!t) return true;
    return (f.code+' '+(f.card_id||'')+' '+(f.message||'')+' '+(f.evidence||'')+' '+(f.block||''))
      .toLowerCase().indexOf(t)>=0;
  });
}
function sorted(rows){
  return rows.slice().sort(function(a,b){
    var x,y;
    if(sortCol==='severity'){x=RANK[a.severity];y=RANK[b.severity];}
    else {x=(a[sortCol]==null?'':String(a[sortCol]));y=(b[sortCol]==null?'':String(b[sortCol]));}
    if(x<y) return -1*sortDir; if(x>y) return 1*sortDir;
    return RANK[a.severity]-RANK[b.severity];
  });
}
function render(){
  var rows=sorted(current());
  cnt.textContent=rows.length.toLocaleString()+' of '+F.length.toLocaleString()+' findings';
  if(!rows.length){tb.innerHTML='<tr><td colspan="5" class="empty">Nothing matches that filter.</td></tr>';return;}
  var h=[];
  for(var i=0;i<rows.length;i++){var f=rows[i];
    var where=[]; if(f.block) where.push(f.index==null?f.block:f.block+'#'+f.index);
    if(f.field) where.push(f.field);
    h.push('<tr><td><span class="sev '+f.severity+'">'+f.severity+'</span>'
      +(f.suppressed?'<div class="det">suppressed</div>':'')+'</td>'
      +'<td><code>'+esc(f.code)+'</code></td>'
      +'<td>'+esc(f.card_id||'—')+'<div class="det">'+esc(where.join(' · '))+'</div></td>'
      +'<td>'+esc(f.message)
      +(f.evidence?'<div class="det"><b>evidence</b> '+esc(f.evidence)+'</div>':'')
      +(f.impact?'<div class="det"><b>impact</b> '+esc(f.impact)+'</div>':'')
      +(f.fix?'<div class="det"><b>fix</b> '+esc(f.fix)+'</div>':'')
      +'</td><td>'+esc(f.layer)+'</td></tr>');
  }
  tb.innerHTML=h.join('');
}
[].forEach.call(document.querySelectorAll('th[data-c]'),function(th){
  th.addEventListener('click',function(){
    var c=th.getAttribute('data-c');
    if(c===sortCol){sortDir=-sortDir;}else{sortCol=c;sortDir=1;}
    render();
  });
});
q.addEventListener('input',render); sv.addEventListener('change',render); ly.addEventListener('change',render);
render();

var cards=DATA.cards, host=document.getElementById('cards'), out=[];
for(var i=0;i<cards.length;i++){
  var c=cards[i], fs=byCard[c.id]||[];
  var e=0,w=0,n0=0;
  fs.forEach(function(f){ if(f.severity==='ERROR')e++; else if(f.severity==='WARN')w++; else n0++; });
  var bits=[]; if(e)bits.push(e+' error'); if(w)bits.push(w+' warning'); if(n0)bits.push(n0+' note');
  out.push('<details><summary>'+esc(c.id)
    +'<span class="gradechip">grade '+esc(c.grade||'—')+'</span>'
    +'<span class="gradechip">'+(bits.length?esc(bits.join(' · ')):'clean')+'</span></summary>');
  if(!fs.length){out.push('<div class="det">No finding names this card.</div>');}
  else{
    fs.sort(function(a,b){return RANK[a.severity]-RANK[b.severity];});
    fs.forEach(function(f){
      out.push('<div style="margin:8px 0"><span class="sev '+f.severity+'">'+f.severity+'</span> '
        +'<code>'+esc(f.code)+'</code> '+esc(f.message)
        +(f.evidence?'<div class="det"><b>evidence</b> '+esc(f.evidence)+'</div>':'')
        +(f.fix?'<div class="det"><b>fix</b> '+esc(f.fix)+'</div>':'')+'</div>');
    });
  }
  out.push('</details>');
}
host.innerHTML=out.join('');
"""


# Unicode bidirectional controls. The report's own esc() is injection-safe
# against markup (verified) but passed these straight through, so a U+202E in a
# card_name or an issuer visually reverses the rest of the rendered line: what
# the reader sees is not what the file says. The console path already drops them
# (flat() collapses on \s and these are not \s — it is the terminal that ignores
# them). Rendered as their codepoint name so the content is still legible and
# the fact that something odd is in the data is visible rather than silent.
_BIDI = {
    "‪": "[U+202A]", "‫": "[U+202B]", "‬": "[U+202C]",
    "‭": "[U+202D]", "‮": "[U+202E]", "⁦": "[U+2066]",
    "⁧": "[U+2067]", "⁨": "[U+2068]", "⁩": "[U+2069]",
}
_BIDI_RE = re.compile("[" + "".join(_BIDI) + "]")


def debidi(text: str) -> str:
    """Neutralise bidi overrides. Safe to run on the whole document: the
    template itself contains none, and the replacement carries no markup
    characters, so it cannot break the embedded JSON or the HTML around it."""
    return _BIDI_RE.sub(lambda m: _BIDI[m.group(0)], text)


def render_html(sc, results, meta, complete, suppressed=()) -> str:
    # Suppressed findings go INTO the report, flagged. A baseline changes the
    # exit code; it must never make a defect invisible to the person reading it.
    everything = [(lid, f, False) for lid, f in results] + \
                 [(lid, f, True) for lid, f in suppressed]
    everything.sort(key=lambda x: sort_key((x[0], x[1])))
    grades = grade_distribution([(lid, f) for lid, f, _s in everything])
    payload = {
        "findings": [dict(f.to_dict(), layer=lid, suppressed=sup)
                     for lid, f, sup in everything],
        "cards": [{"id": cid, "grade": grades.get(cid)} for cid in sc["card_ids"]],
    }
    blob = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")

    s = sc["by_severity"]
    order = ["A", "B", "C", "D", "F", "N/A"]
    gbits = " · ".join(f"{g} {sc['grades'].get(g, 0)}" for g in order
                       if sc["grades"].get(g)) or "not measured"
    share = ("not measured" if sc["sourced_rules"] is None
             else f"{pct(sc['sourced_rules'], sc['total_rules_measured'])}")

    stats = [
        ("cards", n(sc["cards"]), f"{n(sc['cards_clean'])} with no error or warning"),
        ("errors", n(s[ERROR]), f"on {n(sc['cards_with_error'])} cards"),
        ("warnings", n(s[WARN]), f"{n(sc['cards_with_warn_only'])} cards warn-only"),
        ("notes", n(s[INFO]), "recorded, not blocking"),
        ("issuer-sourced", share,
         "not measured" if sc["sourced_rules"] is None
         else f"{n(sc['sourced_rules'])} of {n(sc['total_rules_measured'])} rates"),
        ("grades", gbits, f"{n(sc['graded_cards'])} cards graded"),
    ]
    stat_html = "".join(
        f'<div class="stat"><div class="k">{_html.escape(k)}</div>'
        f'<div class="v">{_html.escape(str(v))}</div>'
        f'<div class="n">{_html.escape(str(note))}</div></div>'
        for k, v, note in stats)

    rows = "".join(
        f"<tr><td><code>{lid}</code></td><td>{_html.escape(str(sc['by_layer'].get(lid, {}).get(ERROR, 0)))}</td>"
        f"<td>{_html.escape(str(sc['by_layer'].get(lid, {}).get(WARN, 0)))}</td>"
        f"<td>{_html.escape(str(sc['by_layer'].get(lid, {}).get(INFO, 0)))}</td>"
        f"<td>{sc['timings'].get(lid, 0) * 1000:.0f} ms</td></tr>"
        for lid in sc["layers_run"])

    blocks = " · ".join(f"{n(v)} {k.replace('_', ' ')}" for k, v in sc["blocks"].items() if v)
    layer_opts = "".join(f'<option value="{lid}">{lid}</option>' for lid in sc["layers_run"])

    supp = ""
    if sc["suppressed"]:
        supp = (f'<p class="honesty"><b>{n(sc["suppressed"])} finding(s) were suppressed '
                f'by the baseline {_html.escape(str(meta.get("baseline") or ""))}.</b> '
                f'They are still in the data. This report is not clean; it is '
                f'clean-except-for-{n(sc["suppressed"])}.</p>')
    incomplete = ("" if complete else
                  '<p class="honesty"><b>This run was INCOMPLETE.</b> A check module '
                  'failed, so every count below is a floor rather than a total.</p>')

    # A shareable report is the single most likely thing to be read as "we
    # checked the data". If some checks did not run, that has to be the first
    # thing on the page, not a footnote under 2,600 rows.
    degraded = ""
    skips = sc.get("skipped_checks") or []
    if skips:
        items = "".join(
            "<li><b><code>{}</code></b> — {}<br><span class='meta'>Not run because: {} "
            "&nbsp;·&nbsp; To restore: {}</span>{}</li>".format(
                _html.escape(str(s.get("code", ""))),
                _html.escape(flat(s.get("what"))),
                _html.escape(flat(s.get("reason"))),
                _html.escape(flat(s.get("restore"))),
                ("<br><span class='meta'>Blind to: <code>"
                 + _html.escape(", ".join(s["codes"])) + "</code></span>")
                if s.get("codes") else "")
            for s in skips)
        degraded = (
            f'<p class="honesty"><b>DEGRADED RUN — {n(len(skips))} check(s) did not run.'
            f'</b> They did not pass; they could not look. Every count on this page is a '
            f'FLOOR, not a total, and the absence of a finding in these areas is not '
            f'evidence that the data is clean in them.</p>'
            f'<div class="panel"><h2 style="margin-top:0">Checks that did not run</h2>'
            f'<ul>{items}</ul></div>')

    head_lines = "".join(f"<p class='headline'>{_html.escape(x)}</p>"
                         for x in headline(sc, complete))

    return debidi(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KredMe card data — validation report</title>
<style>{_HTML_CSS}</style></head><body><div class="wrap">
<h1>KredMe card data — validation report</h1>
<p class="sub">{_html.escape(meta.get('target', ''))} · {_html.escape(meta.get('label', ''))}
 · generated {_html.escape(meta.get('generated_at', ''))} · exit {meta.get('exit_code')}
 · layers {_html.escape(', '.join(sc['layers_run']))}</p>

<div class="panel">{head_lines}</div>
{supp}{incomplete}{degraded}
<div class="grid">{stat_html}</div>

<p class="honesty">{_html.escape(HONESTY)}</p>

<h2>By layer</h2>
<div class="scroll"><table><thead><tr><th>layer</th><th>errors</th><th>warnings</th>
<th>notes</th><th>time</th></tr></thead><tbody>{rows}</tbody></table></div>
<p class="meta">Catalogue: {_html.escape(blocks)}.</p>

<h2>Findings</h2>
<div class="controls">
 <input id="q" type="search" placeholder="Filter by card, code, or words in the message…">
 <select id="sv"><option value="">all severities</option><option value="ERROR">errors</option>
 <option value="WARN">warnings</option><option value="INFO">notes</option></select>
 <select id="ly"><option value="">all layers</option>{layer_opts}</select>
 <span class="meta" id="cnt"></span>
</div>
<div class="scroll"><table id="ft"><thead><tr>
<th data-c="severity">severity</th><th data-c="code">code</th><th data-c="card_id">card</th>
<th data-c="message">what is wrong</th><th data-c="layer">layer</th>
</tr></thead><tbody id="tb"></tbody></table></div>
<p class="meta">Click a column heading to sort.</p>

<h2>Every card</h2>
<div id="cards"></div>
</div>
<script id="data" type="application/json">{blob}</script>
<script>var DATA=JSON.parse(document.getElementById('data').textContent);{_HTML_JS}</script>
</body></html>
""")


# ------------------------------------------------------------------ main ----

class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a mistyped flag or a bad --target value.

    2 is THIS tool's code for "warnings but no errors: nothing is broken" — the
    second-greenest result it can return. So a typo in a CI step or a runbook
    line made a gate that validated nothing report that it was publishable, and
    nothing intercepted it. Every other could-not-run path here goes through
    die() -> 3, and the docstring promises 3. So does this one now.

    --help and --version still exit 0: those ran fine, they just did not check
    anything, and a runbook does not gate on them.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(3)

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise SystemExit(0 if status == 0 else 3)


def build_parser():
    p = _Parser(
        prog="validate_cards.py",
        description="Check KredMe card data against nine independent layers. "
                    "Nothing is grandfathered.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="exit 0 = clean · 1 = error(s) · 2 = warning(s) only · 3 = could not run",
    )
    p.add_argument("--target", choices=("working", "dev", "prod"), default="working",
                   help="which data to check (default: the working tree)")
    p.add_argument("--layer", action="append", metavar="Lx",
                   help="run only these layers, e.g. --layer L4 --layer L8 (repeatable)")
    p.add_argument("--card", metavar="CARD_ID",
                   help="show only this card, with full evidence, impact and fix")
    p.add_argument("--issuer", metavar="NAME", help="show only this issuer's cards")
    p.add_argument("--severity", choices=("error", "warn", "info"),
                   help="show findings at this severity and above (display only)")
    p.add_argument("--summary", action="store_true",
                   help="one screen: counts per layer, scorecard, verdict")
    p.add_argument("--json", metavar="FILE", help="write full findings as JSON")
    p.add_argument("--html", metavar="FILE", help="write a self-contained HTML report")
    p.add_argument("--baseline", metavar="FILE",
                   help="OPT-IN ratchet: suppress findings recorded in this file. "
                        "Suppressed findings are still counted and reported.")
    p.add_argument("--write-baseline", metavar="FILE",
                   help="record today's findings as a baseline file")
    p.add_argument("--app-root", metavar="DIR",
                   help=f"KredMe app checkout (default: {APP_ROOT_DEFAULT})")
    p.add_argument("--today", metavar="YYYY-MM-DD",
                   help="pin the date the temporal checks call today")
    p.add_argument("--max-age-days", type=int, metavar="N",
                   help="how old an issuer source may be before it is stale (default 90)")
    p.add_argument("--quiet", action="store_true",
                   help="print nothing; use the exit code (and --json / --html)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    quiet = bool(args.quiet)

    # Recorded before anything can fail, so that if this run stops it can
    # invalidate the reports it was asked for rather than leaving the previous
    # run's verdict standing at those paths. Reset each call: main() is
    # re-entrant under test.
    _REPORT_PATHS.clear()
    _REPORT_PATHS.update(json=args.json, html=args.html)

    layers = parse_layers(args.layer)
    seed_dir, news_dir, label = data_dirs(args.target)

    app_root = Path(args.app_root) if args.app_root else APP_ROOT_DEFAULT
    config = {"strict_checksums": args.target != "working"}
    if args.today:
        config["today"] = args.today
    if args.max_age_days is not None:
        config["provenance_max_age_days"] = args.max_age_days

    ctx, notes = build_ctx(seed_dir, news_dir, app_root, config)
    set_ctx(ctx)

    checks = load_checks(layers)
    if not checks:
        die("no layers selected")
    layers_run = [lid for lid, _n, _m, _e in checks]

    # --- card / issuer scoping ------------------------------------------- #
    scope, filters = None, []
    if args.card:
        known = {cid for _i, _e, _inner, cid in ctx.entries() if cid}
        if args.card not in known:
            import difflib
            near = difflib.get_close_matches(args.card, sorted(known), n=5, cutoff=0.5)
            near += [c for c in sorted(known)
                     if args.card in c and c not in near][:5]
            hint = ("\n          Did you mean:  " + "\n                         ".join(near[:5])
                    if near else "")
            die(f"no card with id '{args.card}' in this data "
                f"({n(len(known))} cards loaded).{hint}")
        scope = {args.card}
        filters.append(f"card {args.card} (issuer {issuer_of(ctx, args.card) or '?'})")
    if args.issuer:
        ids = card_ids_for_issuer(ctx, args.issuer)
        if not ids:
            die(f"no cards with issuer '{args.issuer}' in this data")
        scope = ids if scope is None else (scope & ids)
        filters.append(f"issuer {args.issuer} ({n(len(ids))} cards)")
    if args.severity:
        filters.append(f"severity {args.severity} and above")
    if scope is not None:
        filters.append("cross-card layers still ran on the WHOLE catalogue — "
                       "filtering happens after, never before")

    min_sev = {"error": ERROR, "warn": WARN, "info": INFO}.get(args.severity or "")

    if not quiet:
        print_header(args.target, label, ctx, notes, layers_run, filters)

    # --- run --------------------------------------------------------------- #
    pending = []

    def on_layer(lid, layer_label, found, secs, skips):
        pending.append((lid, layer_label, found, secs, skips))

    raw, skipped, timings = run_all(checks, on_layer=on_layer)
    raw, n_demoted = demote_duplicates(raw)

    baseline = load_baseline(Path(args.baseline)) if args.baseline else None
    results, suppressed = apply_baseline(raw, baseline)
    complete = not any(f.code == CRASH_CODE for _l, f in results)

    # Scope + severity are DISPLAY filters. The verdict is computed on the
    # findings actually in scope, before the severity filter — hiding warnings
    # from the screen must never turn a 2 into a 0.
    in_scope = filter_results(results, scope, None)
    suppressed_in_scope = filter_results(suppressed, scope, None)
    shown = filter_results(results, scope, min_sev)

    # Scope hides file-wide findings from the LIST. It must not hide them from
    # the exit code — see print_verdict. filter_results already drops every
    # card_id=None row when a scope is set, so this cannot double-count.
    verdict_results = in_scope if scope is None else (
        in_scope + [(lid, f) for lid, f in results if f.card_id is None])

    if not quiet:
        by_layer_shown = defaultdict(list)
        for lid, f in shown:
            by_layer_shown[lid].append((lid, f))
        for lid, layer_label, _found, secs, skips in pending:
            print_layer(lid, layer_label, by_layer_shown.get(lid, []), secs,
                        args.summary, detail=bool(args.card), skips=skips)

    sc = scorecard(ctx, in_scope, suppressed_in_scope, layers_run, timings,
                   scope=scope, all_results=results + suppressed,
                   verdict_results=verdict_results, demoted=n_demoted,
                   skipped=skipped)
    if args.baseline:
        sc["baseline"] = str(args.baseline)
    code = exit_code_for(verdict_results)

    if not quiet:
        print_scorecard(sc, complete)
        print_verdict(verdict_results, sc, code, complete, args.baseline)

    meta = {
        "target": args.target, "label": label,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_head": git_head()[:12], "exit_code": code,
        # `ran` separates "this run finished and produced a verdict" from the
        # stub _invalidate_reports leaves behind, which has the same shape and
        # an empty findings list. `complete` means no check CRASHED; `degraded`
        # means a check declined to run. All three are different questions.
        "ran": True, "complete": complete,
        "baseline": args.baseline, "filters": filters,
        "seed_dir": str(seed_dir), "news_dir": str(news_dir),
        "app_root": str(ctx.app_root) if ctx.app_root else None,
        # A consumer must be able to tell a degraded run from a full one WITHOUT
        # parsing prose. `complete` already means "no check crashed"; `degraded`
        # means "a check declined to run because it was missing an input".
        "degraded": bool(skipped),
        "skipped_count": len(skipped),
        "categories_origin": ctx.categories_origin,
        "categories_source": ctx.categories_source(),
        "app_keys_origin": ctx.app_keys_origin,
    }

    if args.json:
        payload = {
            "meta": meta,
            "scorecard": sc,
            "headline": headline(sc, complete),
            "honesty": HONESTY,
            "findings": [dict(f.to_dict(), layer=lid)
                         for lid, f in sorted(in_scope, key=sort_key)],
            "suppressed": [dict(f.to_dict(), layer=lid, fingerprint=fingerprint(f))
                           for lid, f in sorted(suppressed_in_scope, key=sort_key)],
            # Deliberately NOT merged into "findings". A skip is not a defect in
            # the data and must never be counted as one; equally it must not be
            # possible to read this file, see an empty list, and conclude the
            # run was complete. It is always present, empty on a full run.
            "skipped_checks": [s.to_dict() for s in skipped],
        }
        # An unwritable path used to raise a bare OSError traceback and exit 1 —
        # the code this tool defines as "a user can be shown a wrong number".
        # Under --quiet the traceback was the only output at all. Failing to
        # write a report is a could-not-run, like every other one here: exit 3.
        p = Path(args.json)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
                fh.write("\n")
        except OSError as ex:
            die(f"could not write the JSON report to {p} — {ex}")
        if not quiet:
            info(f"wrote {p} — {n(len(in_scope))} findings")

    if args.html:
        p = Path(args.html)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(render_html(sc, in_scope, meta, complete, suppressed_in_scope),
                         encoding="utf-8")
        except OSError as ex:
            die(f"could not write the HTML report to {p} — {ex}")
        if not quiet:
            info(f"wrote {p} — {p.stat().st_size / 1024:.0f} KB, opens in any browser")

    if args.write_baseline:
        if (scope is not None or layers) and not quiet:
            warn("writing a PARTIAL baseline — it only covers the filtered run above")
        try:
            count = write_baseline(Path(args.write_baseline), in_scope,
                                   f"{args.target}@{git_head()[:7]}")
        except OSError as ex:
            die(f"could not write the baseline to {args.write_baseline} — {ex}")
        if not quiet:
            info(f"wrote {args.write_baseline} — {n(count)} fingerprints. "
                 f"This records defects; it does not fix them.")

    if not quiet:
        print()
    return code


if __name__ == "__main__":
    sys.exit(main())
