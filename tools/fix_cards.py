#!/usr/bin/env python3
"""
fix_cards.py — apply the repairs the validator can PROVE, and then measure them.

WHAT THIS IS
------------
tools/validate_cards.py finds 712 errors and fixes nothing. The modules under
tools/fixers/ know how to repair some of them but write nothing. This file is
the runner in between: it loads the data once, runs the validator to get
findings, asks every fixer to PLAN edits against those findings, and — only when
explicitly told to — applies them.

    validate  ->  plan  ->  [gate]  ->  apply  ->  re-validate  ->  prove

The last step is the point. A fix tool that says "applied 1,218 edits" and stops
has told you what it DID, not what it ACHIEVED. This one re-runs the whole
validator afterwards and prints the error count per code before and after, so
the effect is measured. If any code went UP, that is the first thing on screen,
in red, before anything that sounds like success.

DRY RUN IS THE DEFAULT
----------------------
There is no invocation of this tool with no arguments that modifies data.
Writing requires --apply, typed by a human. Everything else — every filter,
every report, --diff, --json — plans and prints and touches nothing.

THE THREE RULES THAT COST US SOMETHING TO LEARN
-----------------------------------------------
1. NEVER rewrite a rule_name. The name is the only independent evidence in the
   file — it is the issuer's own sentence, and every numeric check in the
   validator is ultimately checking a number against it. Edit the name to make
   the number agree and that rule can never be audited again. Worse, the app
   keys each user's saved cap progress on that exact string, so changing it
   silently resets real people's progress towards a cap they have nearly
   earned. This runner refuses any edit whose field is `rule_name`, and any row
   or entry edit that changes a rule_name in passing. It is a structural guard
   here AND an assertion inside the fixers, on purpose: two locks, because one
   lock is a promise and two are a mechanism.

2. A rate correction that is not issuer-sourced may only go DOWN. That is
   enforced inside f1_units, which renders every proposed edit through the app's
   own arithmetic before and after and refuses the ones that would raise what a
   user sees. This runner surfaces those refusals rather than hiding them —
   see `refusals()` on a fixer module.

3. A blind sweep is how BPCL Octane nearly lost its genuine fuel rewards. So
   nothing here is a regex over the file. Every edit is anchored to one
   addressable thing, carries the evidence it was derived from, and is checked
   against the CURRENT value before it is written: if the anchor moved, the edit
   is refused, not forced.

WHAT COUNTS AS SAFE TO APPLY
----------------------------
Two confidence levels, set by the fixer, gated by the runner:

    certain   the edit is FORCED by the data — "5,000" -> 5000.0, a min above a
              max, a confidence key the app is silently defaulting to 'high'.
              There is exactly one value the field could hold and still be
              consistent with what is already in the file.
    likely    a documented heuristic or a policy call a human owns — retyping an
              exclusion, removing a withdrawn card. Planned and shown, never
              applied unless someone passes --confidence likely.

Default gate is `certain`. `--confidence likely` means "certain AND likely",
because a gate names the floor, not a single band.

WHAT IT NEVER DOES
------------------
Never publishes. Never pushes. Never touches main. Never writes to seed/ without
--apply. Never writes at all without taking a backup first. Nothing here reaches
a user: the end of the road is a modified working tree that a human reviews as a
diff and merges as a PR.

USAGE
-----
    python3 tools/fix_cards.py                              plan everything, write nothing
    python3 tools/fix_cards.py --diff                       ... and show every before/after
    python3 tools/fix_cards.py --code L4.CAP_NOT_A_NUMBER   plan one defect class
    python3 tools/fix_cards.py --family caps                plan one fixer family
    python3 tools/fix_cards.py --card hdfc_bank_infinia     plan one card
    python3 tools/fix_cards.py --json plan.json             machine-readable plan
    python3 tools/fix_cards.py --apply                      APPLY the 'certain' edits
    python3 tools/fix_cards.py --apply --confidence likely  APPLY certain + likely
    python3 tools/fix_cards.py --apply --code L8.SOURCE_URL_NOT_A_URL --limit 5

HOW A WRITE CANNOT TEAR
-----------------------
Every write is staged and then swapped. The three documents are serialised to
sibling temp files in their own directories, fsynced, read back and re-parsed,
and only then os.replace()d onto the targets one after another. os.replace is
atomic within a filesystem, so a reader — the app's own sync, a concurrent
validate, the next run of this tool — sees either the whole old file or the
whole new one, never 1.8 MB of half-written JSON. The manifest is checksummed
against the STAGED cards bytes rather than against the file on disk, so the
index can never describe bytes that are not there. Anything that fails before
the first swap leaves the tree byte-identical and exits 3; the run is untouched,
not half-done.

Only one --apply may hold a seed directory at a time. The lock is an exclusive
flock on `.fix.lock` beside seed/, taken before the data is read and released
after the last swap, because two overlapping applies are an unsynchronised
read-modify-write: measured, one run's edits were silently lost 6 times out of 6
and the loser blamed the FIXERS for it in its own report.

EXIT CODES
----------
    0   planned (or applied) cleanly
    1   something went wrong that a human must look at — a fixer crashed, a
        fixer mutated its input, an edit was refused, an error count went UP,
        or a second --apply still had work to do (not idempotent)
    3   could not run (bad flag, unreadable data, another apply holds the lock)
        — NOTHING was written and the tree is exactly as it was
    4   THE TREE MAY BE INCONSISTENT. A swap failed after an earlier one had
        already landed, so seed/ and the manifest may disagree. Restore from the
        backup printed above before doing anything else.

1 and 4 are deliberately different codes. 1 is a reviewable result — the sweep
ran, and something in it wants a human. 4 is damage. A caller that cannot tell
them apart will open a PR out of a broken tree, which is what .github's Verdict
step used to do.

A run truncated by --limit is a PARTIAL run, not an unstable one: it is meant to
leave work behind, so idempotency is reported as 'not asserted' rather than
failed, and the exit code stays 0.

3, not 2, for a bad flag, matching tools/validate_cards.py: argparse's own
default of 2 is that tool's code for "warnings only, publishable", and a typo in
a CI step must never report a publishable result.

No third-party packages. Python 3.12+. Run from anywhere.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as _dt
import fcntl
import hashlib
import importlib
import json
import os
import shutil
import sys
import traceback
from collections import Counter, defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent

# tools/ on the path so `checks` and `fixers` resolve from anywhere, exactly the
# way tools/validate_cards.py does it.
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import validate_cards as V                                        # noqa: E402
from checks.base import ERROR, WARN, INFO                         # noqa: E402
from fixers.base import CERTAIN, LIKELY, Edit                     # noqa: E402

# --------------------------------------------------------------------------- #
# What the runner knows about the world
# --------------------------------------------------------------------------- #
FIXERS_DIR = TOOLS / "fixers"
LIVE_SEED = V.LIVE_SEED
LIVE_NEWS = V.LIVE_NEWS
MANIFEST = V.MANIFEST                 # "manifest.json"
FEED = V.FEED                         # "feed.json"
CARDS = "cards.json"

# The five row blocks that hang off a card ENTRY (not off the inner `card`).
ROW_BLOCKS = V.ROW_BLOCKS

# Indentation is not cosmetic here. seed/cards.json is 1.78 MB stored at
# indent=1; writing it back at indent=2 re-indents all 131,664 lines and the PR
# — the only place a human ever sees what was decided — becomes unreviewable.
# tests/test_cli.py asserts this exact round-trip, and pipeline/cli.py:608 is the
# existing writer these numbers are copied from. Do not "tidy" them.
INDENT = {CARDS: 1, MANIFEST: 1, FEED: 2}

# The one field no fixer may ever touch. See rule 1 in the module docstring.
FORBIDDEN_FIELD = "rule_name"

DEFAULT_BACKUP = REPO / ".fix-backups"

# A gate names a FLOOR, not a band: --confidence likely means certain + likely.
GATE_ORDER = (CERTAIN, LIKELY)

# See EXIT CODES in the module docstring. 4 exists so that "the sweep found
# something a human should read" and "the files on disk may disagree with each
# other" can never be answered by the same number.
EXIT_PROBLEMS = 1
EXIT_CANNOT_RUN = 3
EXIT_INCONSISTENT = 4

# The lock file that makes two overlapping applies impossible. It lives beside
# seed/ rather than inside it so it can never be mistaken for data, and it is
# gitignored.
LOCK_NAME = ".fix.lock"


# ---------------------------------------------------------------- output ----
# Copied from tools/kredme.py rather than imported, for the same reason
# validate_cards.py copies it: importing kredme.py runs its module-level setup,
# and a fix runner must not be able to inherit another tool's side effects.

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


def die(msg, code=EXIT_CANNOT_RUN):
    """Stop. 3 = could not run; see the module docstring on why not 2."""
    print(f"\n{C.R}{C.BOLD}Stopped:{C.X} {msg}\n")
    sys.exit(code)


def n(x) -> str:
    return f"{x:,}"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trunc(v, width=72) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= width else s[: width - 1] + "…"


# --------------------------------------------------------------------------- #
# Loading the fixers
# --------------------------------------------------------------------------- #
def discover_fixers():
    """[(name, module_or_None, import_error_or_None)] in a FIXED order.

    Sorted by filename, so f1_units always plans before f4_integrity and two
    runs of this tool produce byte-identical output. Order is not merely tidy:
    the apply phase resolves collisions by position, so a wobbling module order
    would make the result depend on the filesystem's mood.
    """
    out = []
    if not FIXERS_DIR.is_dir():
        return out
    for path in sorted(FIXERS_DIR.glob("f*.py")):
        name = path.stem
        if name.startswith("_") or name == "base":
            continue
        try:
            mod = importlib.import_module(f"fixers.{name}")
        except BaseException as e:  # noqa: BLE001 — a bad import must not kill the run
            if isinstance(e, KeyboardInterrupt):
                raise
            out.append((name, None, f"{type(e).__name__}: {e}"))
            continue
        out.append((name, mod, None))
    return out


def family_of(mod, name) -> str:
    fam = getattr(mod, "FAMILY", None)
    return fam if isinstance(fam, str) and fam else name


def handles_of(mod) -> list:
    h = getattr(mod, "HANDLES", None)
    return [c for c in h if isinstance(c, str)] if isinstance(h, list) else []


def family_matches(tokens, mod, name) -> bool:
    """--family caps matches 'caps'; --family reach matches 'reachability';
    --family units matches both 'rates & units' and the module f1_units."""
    if not tokens:
        return True
    hay = f"{family_of(mod, name)} {name}".lower()
    return any(t.lower().strip() in hay for t in tokens)


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
def load_ctx(seed_dir: Path, news_dir: Path, app_root: Path | None):
    ctx, notes = V.build_ctx(seed_dir, news_dir, app_root,
                             {"strict_checksums": False})
    return ctx, notes


def validate(ctx):
    """(findings_as_dicts, skipped, counts). Runs every check module.

    Duplicate demotion is applied, exactly as the validator's own CLI does it,
    so a fixer sees the same 2,627 findings a human reading the report sees —
    and, importantly, sees L2.EXCLUSION_TYPE_INERT already demoted to a note
    under its owner L6.EXCLUSION_TYPE_INERT. A fixer keying off the wrong one of
    a duplicate pair would plan the same edit twice.
    """
    V.set_ctx(ctx)
    raw, skipped, _timings = V.run_all(V.load_checks(None))
    raw, _demoted = V.demote_duplicates(raw)
    findings = [dict(f.to_dict(), layer=lid) for lid, f in raw]
    counts = severity_counts(raw)
    return findings, skipped, counts


def severity_counts(raw) -> dict:
    by_code = Counter()
    totals = Counter()
    for _lid, f in raw:
        totals[f.severity] += 1
        if f.severity == ERROR:
            by_code[f.code] += 1
    return {
        "errors": totals[ERROR], "warnings": totals[WARN], "notes": totals[INFO],
        "by_code": dict(by_code),
    }


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def snapshot(ctx) -> dict:
    """A deep copy of everything a fixer is allowed to read.

    Only the DATA. Ctx also memoises app-key lookups in `_key_cache`, and that
    cache filling up is a fixer using the API correctly, not a fixer mutating
    its input. Comparing the cache would fail every honest module.
    """
    return {
        "cards": copy.deepcopy(ctx.cards),
        "merchants": copy.deepcopy(ctx.merchants),
        "manifest": copy.deepcopy(ctx.manifest),
        "news": copy.deepcopy(ctx.news),
    }


def restore(ctx, snap) -> None:
    ctx.cards = snap["cards"]
    ctx.merchants = snap["merchants"]
    ctx.manifest = snap["manifest"]
    ctx.news = snap["news"]


def plan_all(ctx, findings, mods):
    """(edits, failures, extras).

    Every module is planned in isolation and audited twice:

      * PURITY. plan() is documented as pure. That is checked, not trusted: the
        data is deep-copied before the call and deep-compared after. A module
        that mutated ctx has its edits DISCARDED and the data restored, because
        a module that writes where it promised only to read has already proved
        its edits are not the whole story of what it does.

      * CRASH ISOLATION. A module that raises contributes zero edits and one
        loud failure. It can never take the run down, and — this is the part
        that matters — it can never leave half its edits behind either, because
        nothing has been written at this stage. Planning is the phase where
        crashing is free.
    """
    edits, failures, extras = [], [], {}
    for name, mod, imp_err in mods:
        if mod is None:
            failures.append({"module": name, "kind": "import",
                             "detail": imp_err or "module did not import"})
            continue
        before = snapshot(ctx)
        try:
            got = mod.plan(ctx, findings)
        except BaseException as e:  # noqa: BLE001 — a fixer may raise anything
            if isinstance(e, KeyboardInterrupt):
                raise
            tb = traceback.extract_tb(e.__traceback__)
            spot = f" at {Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else ""
            failures.append({"module": name, "kind": "crash",
                             "detail": f"{type(e).__name__}: {e}{spot}"})
            restore(ctx, before)          # it may have mutated before it fell over
            continue
        after = snapshot(ctx)
        if after != before:
            failures.append({
                "module": name, "kind": "mutation",
                "detail": ("plan() modified the data it was given. Its edits are "
                           "discarded and the data restored: a module that writes "
                           "where it promised to read cannot be reviewed as a diff."),
            })
            restore(ctx, before)
            continue
        if not isinstance(got, list):
            failures.append({"module": name, "kind": "contract",
                             "detail": f"plan() returned {type(got).__name__}, not a list"})
            continue
        bad = [x for x in got if not isinstance(x, Edit)]
        if bad:
            failures.append({"module": name, "kind": "contract",
                             "detail": f"{len(bad)} returned item(s) were not Edit objects"})
        fam = family_of(mod, name)
        for e in got:
            if not isinstance(e, Edit):
                continue
            e.family = e.family or fam
            e.module = name           # lands in to_dict(), so the JSON names the author
            edits.append(e)
        extras[name] = collect_extras(ctx, findings, mod, name)
    return edits, failures, extras


def collect_extras(ctx, findings, mod, name) -> dict:
    """Optional, module-specific commentary: what was deliberately NOT fixed.

    f1_units publishes refusals() — every defect it examined and declined to
    repair, with the sentence explaining why (nearly all of them "this would
    RAISE the number a user sees"). f3_reach publishes census(). Neither is
    required by the contract, so both are called defensively: an optional
    accessor that raises must not cost the run its edits.
    """
    out = {}
    for attr in ("refusals", "census"):
        fn = getattr(mod, attr, None)
        if not callable(fn):
            continue
        before = snapshot(ctx)
        try:
            val = fn(ctx, findings)
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            restore(ctx, before)
            out[attr] = {"error": f"{type(e).__name__}: {e}"}
            continue
        if snapshot(ctx) != before:
            restore(ctx, before)
            out[attr] = {"error": f"{attr}() mutated the data — result discarded"}
            continue
        out[attr] = jsonable(val)
    return out


def jsonable(val):
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if isinstance(val, dict):
        return {str(k): jsonable(v) for k, v in val.items()}
    if isinstance(val, (list, tuple, set)):
        return [jsonable(v) for v in val]
    if hasattr(val, "to_dict"):
        try:
            return jsonable(val.to_dict())
        except Exception:                        # noqa: BLE001
            pass
    if hasattr(val, "__dict__"):
        return {k: jsonable(v) for k, v in vars(val).items()}
    return str(val)


# --------------------------------------------------------------------------- #
# The guard that cannot be argued with
# --------------------------------------------------------------------------- #
def rule_names(obj) -> list:
    """Every rule_name reachable inside a row, a list of rows, or a whole entry."""
    found = []
    if isinstance(obj, dict):
        if isinstance(obj.get(FORBIDDEN_FIELD), str):
            found.append(obj[FORBIDDEN_FIELD])
        for v in obj.values():
            found.extend(rule_names(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(rule_names(v))
    return found


def forbidden(e: Edit) -> str | None:
    """The one-sentence reason this edit may never be applied, or None.

    Deliberately checks the VALUES, not just the field name. A fixer does not
    have to write `field='rule_name'` to rename a rule — handing back a whole
    row, or a whole entry, with a different name inside it does exactly the same
    damage and reads innocently in a summary. So the guard compares the names
    present before and after, wherever they are nested.
    """
    if e.field == FORBIDDEN_FIELD:
        return (f"edits {FORBIDDEN_FIELD} directly — the name is the only "
                f"independent evidence in the file, and the app keys users' saved "
                f"cap progress on that exact string")
    if e.new_value is None:
        return None                     # a deletion removes a row, it does not rename one
    before, after = sorted(rule_names(e.old_value)), sorted(rule_names(e.new_value))
    if before != after:
        gone = [x for x in before if x not in after]
        added = [x for x in after if x not in before]
        return (f"changes a {FORBIDDEN_FIELD} in passing "
                f"({trunc(gone[0] if gone else '?', 40)!r} -> "
                f"{trunc(added[0] if added else '(removed)', 40)!r}) — forbidden: "
                f"renaming a rule wipes every user's saved cap progress on it")
    return None


def guard(edits):
    """(kept, blocked). Runs before any filter, so nothing can be filtered past it."""
    kept, blocked = [], []
    for e in edits:
        why = forbidden(e)
        if why:
            blocked.append((e, why))
        else:
            kept.append(e)
    return kept, blocked


# --------------------------------------------------------------------------- #
# Filtering and ordering
# --------------------------------------------------------------------------- #
def sort_key(e: Edit):
    """Total order over edits. Two runs on the same bytes must produce the same
    list in the same sequence, or 'applying twice is idempotent' is untestable."""
    return (
        e.card_id or "",
        e.block or "",
        -1 if e.index is None else e.index,
        e.field or "",
        e.code,
        getattr(e, "module", "") or "",
        json.dumps(e.new_value, sort_keys=True, ensure_ascii=False, default=str),
    )


def gate_allows(level: str) -> set:
    """A gate names the floor. 'likely' means certain AND likely."""
    idx = GATE_ORDER.index(level)
    return set(GATE_ORDER[: idx + 1])


def gate_split(edits, allowed: set) -> tuple:
    """(passes, held). The confidence gate, applied to GROUPS and not to rows.

    Filtering edit by edit split a two-part change and applied only the
    dependent half. f1_units emits a card's corrected `rp_value_standard` as
    `likely` and the rule-level `point_value` copy that DERIVES FROM IT as
    `certain`, so the plain default run — no flags — wrote the copy and skipped
    the source. The edit's own reason sentence says what that produces, in
    words: "otherwise the same point would be priced two different ways on one
    card." Three live Equitas cards ended up priced at Rs 0.25 on the base rule
    and Rs 0.50 on the card, and the validator reported three NEW
    L4.BASE_FIELD_VS_BASE_RULE errors that the fix run had created itself.

    A group's effective confidence is therefore the LOWEST any member carries.
    Below the gate, every member is held back, including the ones that would
    individually have passed. An edit that derives from another can never be
    surer than the thing it derives from.
    """
    groups = defaultdict(list)
    passes, held = [], []
    for e in edits:
        gid = getattr(e, "group_id", None)
        if gid:
            groups[gid].append(e)
        elif e.confidence in allowed:
            passes.append(e)
        else:
            held.append(e)
    for _gid, members in groups.items():
        (passes if all(m.confidence in allowed for m in members) else held).extend(members)
    return sorted(passes, key=sort_key), sorted(held, key=sort_key)


def select(edits, codes=None, cards=None, limit=None):
    """Filters that are NOT the confidence gate. Confidence is applied later and
    separately, so the plan can always show what was held back and why."""
    out = edits
    if codes:
        want = set(codes)
        out = [e for e in out if e.code in want]
    if cards:
        want = set(cards)
        out = [e for e in out if e.card_id in want]
    out = sorted(out, key=sort_key)
    if limit is not None and limit >= 0:
        out = out[:limit]
    return out


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
def read_doc(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class TreeInconsistent(RuntimeError):
    """A swap failed AFTER an earlier swap had already landed.

    The one failure this module cannot undo, and therefore the one it must never
    report as an ordinary error. Carries exit code 4 all the way out.
    """


def serialise(obj, indent: int) -> bytes:
    """Byte-for-byte the shape pipeline/cli.py already writes.

    indent, ensure_ascii=False, and a single trailing newline. Nothing here
    re-orders keys: json.load preserves insertion order and json.dump writes it
    back, so a one-field change produces a one-field diff.
    """
    return (json.dumps(obj, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")


def stage(path: Path, raw: bytes) -> Path:
    """Write `raw` to a sibling temp file, fsync it, and PROVE it re-parses.

    A sibling, not /tmp: os.replace is only atomic within one filesystem, and a
    temp file two mounts away turns the swap back into a copy, which is the
    tearing this whole function exists to stop.

    The read-back is not paranoia. `path.write_text` truncates the target to
    zero and then streams 1.85 MB into the same inode, so a full disk, a
    filesystem quota, a SIGTERM or an OOM kill during that window left the live
    card catalogue as unparseable JSON with no error anyone would see until the
    app refused to sync. Reproduced with a file-size ceiling: 1,838,080 bytes on
    disk, JSONDecodeError at line 68,201. Here the same failure hits the temp
    file, raises, and the real file is never opened for writing at all.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.fix-{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        back = tmp.read_bytes()
        if back != raw:
            raise OSError(f"{tmp.name} holds {len(back)} byte(s), not the "
                          f"{len(raw)} that were written — the write was short")
        json.loads(back.decode("utf-8"))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    return tmp


def write_doc(path: Path, obj, indent: int) -> None:
    """Stage, verify, swap. The single-file case of write_all() below."""
    tmp = stage(path, serialise(obj, indent))
    os.replace(tmp, path)


def write_all(seed_dir: Path, news_dir: Path, docs: dict, pristine: dict) -> tuple:
    """Write every document that MOVED, as one transaction. (touched, rechecksummed).

    The old code wrote the three files in sequence with the manifest LAST, so a
    failure in between left new card bytes under a manifest declaring the old
    checksum — the state regen_manifest's own docstring calls "what the app
    surfaces to a user as 'Sync failed'". Reproduced by making manifest.json
    read-only: cards.json 1,822,638 -> 1,851,818 bytes, manifest still
    declaring the old digest.

    Now: serialise everything, checksum the manifest against the STAGED cards
    bytes, stage every file (each fsynced and re-parsed), and only then swap.
    Nothing is swapped until every file has proved it is whole, so a failure
    while staging leaves the tree byte-identical. A failure DURING the swaps —
    which needs the directory to become unwritable between two renames — is the
    one unrecoverable case and raises TreeInconsistent so the caller can exit 4
    and point at the backup rather than opening a PR out of it.
    """
    writes = []                       # [(final_path, raw_bytes, key)]
    if docs["cards"] != pristine["cards"]:
        writes.append((Path(seed_dir) / CARDS,
                       serialise(docs["cards"], INDENT[CARDS]), "cards"))
    if docs.get("news") is not None and docs["news"] != pristine["news"]:
        writes.append((Path(news_dir) / FEED,
                       serialise(docs["news"], INDENT[FEED]), "news"))

    touched = {k for _p, _r, k in writes}
    rechecksummed = []
    if docs.get("manifest") is not None and (
            "cards" in touched or docs["manifest"] != pristine["manifest"]):
        # Regenerated ALWAYS after a seed write, even when no edit named the
        # manifest: cards.json changed, so every checksum in it is now stale.
        # Against the bytes we are ABOUT to write, never the bytes on disk —
        # nothing has been swapped yet, and hashing the old file here is how the
        # manifest ends up describing a version of cards.json that never
        # existed.
        pending = {p.name: raw for p, raw, _k in writes
                   if p.parent == Path(seed_dir)}
        rechecksummed = regen_manifest(seed_dir, docs["manifest"], pending)
        writes.append((Path(seed_dir) / MANIFEST,
                       serialise(docs["manifest"], INDENT[MANIFEST]), "manifest"))
        touched.add("manifest")

    staged = []
    try:
        for final, raw, _k in writes:
            staged.append((stage(final, raw), final))
    except BaseException:
        for tmp, _final in staged:
            with contextlib.suppress(OSError):
                tmp.unlink()
        raise

    for i, (tmp, final) in enumerate(staged):
        try:
            os.replace(tmp, final)
        except OSError as ex:
            for leftover, _f in staged[i:]:
                with contextlib.suppress(OSError):
                    leftover.unlink()
            if i:
                raise TreeInconsistent(
                    f"{final} could not be replaced after "
                    f"{', '.join(str(f) for _t, f in staged[:i])} already had "
                    f"been — {ex}") from ex
            raise
    return sorted(touched), rechecksummed


def load_docs(seed_dir: Path, news_dir: Path) -> dict:
    """The three files any edit can address, read fresh from disk.

    Read from DISK and not from ctx on purpose: ctx is the object the fixers
    were handed, and a plan is only trustworthy applied to the bytes it was
    planned against. Anything that drifted between planning and applying shows
    up as a refused anchor, which is the outcome we want.
    """
    docs = {"cards": None, "manifest": None, "news": None}
    docs["cards"] = read_doc(Path(seed_dir) / CARDS)
    mp = Path(seed_dir) / MANIFEST
    docs["manifest"] = read_doc(mp) if mp.exists() else None
    fp = Path(news_dir) / FEED
    docs["news"] = read_doc(fp) if fp.exists() else None
    return docs


def news_items(news):
    """Mirrors checks/c9_temporal.py:_news_items — same key order, same answer."""
    if isinstance(news, dict):
        for key in ("items", "articles"):
            if isinstance(news.get(key), list):
                return news[key]
        return None
    return news if isinstance(news, list) else None


def entry_of(cards, card_id):
    """(index, entry, inner) for a card id, or (None, None, None).

    Looked up by ID every time, never cached. Removing an entry shifts every
    index after it, and a stale index is how a runner deletes the wrong card.
    """
    if not isinstance(cards, list):
        return None, None, None
    for i, e in enumerate(cards):
        if not isinstance(e, dict):
            continue
        inner = e.get("card") if isinstance(e.get("card"), dict) else e
        if isinstance(inner, dict) and inner.get("id") == card_id:
            return i, e, inner
    return None, None, None


def resolve(docs, e: Edit):
    """(container, key, where) for a FIELD edit, or (None, None, reason).

    block=None with a field set addresses a key on the card ENTRY — the object
    holding `card` plus the five row blocks — not on the inner card. That is
    what fixers/f4_integrity.py documents and relies on: it deliberately chose a
    field edit over an entry edit there because replacing a whole entry silently
    rolled back another fixer's edit to the same card.
    """
    if e.block == "manifest":
        if not isinstance(docs.get("manifest"), dict):
            return None, None, "seed/manifest.json is not readable as an object"
        return docs["manifest"], e.field, f"seed/{MANIFEST}"
    if e.block == "news":
        items = news_items(docs.get("news"))
        if items is None:
            return None, None, "news/feed.json has no item list"
        if e.index is None or not (0 <= e.index < len(items)):
            return None, None, f"news item {e.index} is out of range ({len(items)} items)"
        row = items[e.index]
        if not isinstance(row, dict):
            return None, None, f"news item {e.index} is not an object"
        return row, e.field, f"news/{FEED} items[{e.index}]"

    _i, entry, inner = entry_of(docs.get("cards"), e.card_id)
    if entry is None:
        return None, None, f"no card with id {e.card_id!r} in seed/{CARDS}"
    if e.block is None:
        return entry, e.field, f"seed/{CARDS} {e.card_id} (entry)"
    if e.block == "card":
        if not isinstance(inner, dict):
            return None, None, f"{e.card_id} has no inner card object"
        return inner, e.field, f"seed/{CARDS} {e.card_id}.card"
    rows = entry.get(e.block)
    if not isinstance(rows, list):
        return None, None, f"{e.card_id}.{e.block} is not a list"
    if e.index is None or not (0 <= e.index < len(rows)):
        return None, None, (f"{e.card_id}.{e.block}[{e.index}] is out of range "
                            f"({len(rows)} rows)")
    row = rows[e.index]
    if not isinstance(row, dict):
        return None, None, f"{e.card_id}.{e.block}[{e.index}] is not an object"
    return row, e.field, f"seed/{CARDS} {e.card_id}.{e.block}[{e.index}]"


def anchor_ok(current, old) -> bool:
    """Is the thing still what the fixer thought it was?

    old_value None covers two truthful shapes: the key is absent, or the key is
    there holding null. Both read as `container.get(field) is None`, and both
    are what a fixer meant by "there is nothing here yet".
    """
    return current is None if old is None else current == old


def dig(entry: dict, path: str):
    """entry['card']['is_active'] from "card.is_active". Missing -> None."""
    cur = entry
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def entry_anchor_ok(entry: dict, e: Edit) -> tuple:
    """(ok, why_not) for an ENTRY-shaped edit.

    With `anchor_fields`, the check is the named fields and nothing else. That
    is the whole repair: comparing the WHOLE entry meant any field edit landing
    on the same card in the same run moved the anchor, so at
    `--confidence likely` 10 of 13 card removals were refused on pass 1 and
    applied on pass 2 — an irreversible operation whose outcome depended on how
    many times it was run, and whose refusal message printed the edit's reason
    ("this card is withdrawn") instead of the cause, so it read as a success.

    Without anchor_fields the old whole-entry comparison stands, which is right
    for an entry REPLACEMENT: there the fixer is handing back a rewritten copy
    of everything, so everything is what it read.
    """
    fields = getattr(e, "anchor_fields", None)
    if not fields:
        if anchor_ok(entry, e.old_value):
            return True, ""
        return False, ("the card entry is no longer what the fixer read — "
                       "anchor moved (this edit is anchored on the whole entry, "
                       "so any other edit to this card moves it)")
    for path, want in fields.items():
        got = dig(entry, path)
        if got != want:
            return False, (f"anchor moved: {e.card_id}.{path} is now {trunc(got, 40)}, "
                           f"not the {trunc(want, 40)} the fixer decided on")
    return True, ""


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #
def apply_edits(docs, edits):
    """(applied, satisfied, refused). Mutates `docs` in place.

    ORDER IS PART OF THE CORRECTNESS, not an optimisation:

      1. ROW REPLACEMENTS first. A retype hands back a whole row. If a field
         edit landed first and the row were replaced afterwards, the field edit
         would be silently erased — the exact failure f4_integrity documents
         having measured, where an entry snapshot rolled back a `_sources`
         deletion planned in the same run.
      2. FIELD EDITS second, so they land ON TOP of any replaced row.
      3. ROW DELETIONS third, highest index first within each (card, block), so
         removing row 4 cannot renumber row 7 out from under the next deletion.
      4. ENTRY EDITS last, resolved by card id at the moment of application, for
         the same reason one level up.

    Anything whose anchor has moved is REFUSED, never forced. Anything already
    holding the intended value is 'satisfied' — a no-op, counted separately, so
    a re-run of a partially-applied plan reads as "already done" rather than as
    either a success or a failure.
    """
    applied, satisfied, refused = [], [], []

    def note(bucket, e, why=""):
        # `applied` is a plain list of Edits — it is counted, grouped and
        # re-serialised everywhere. The other two carry the sentence explaining
        # what happened instead, because "refused" with no reason is just a
        # number nobody can act on.
        bucket.append(e if bucket is applied else (e, why))

    row_repl = [e for e in edits
                if e.shape == "row" and e.new_value is not None]
    field_ed = [e for e in edits if e.shape == "field"]
    row_del = [e for e in edits if e.shape == "row" and e.new_value is None]
    entry_ed = [e for e in edits if e.shape == "entry"]

    # -- 1. row replacements ------------------------------------------------ #
    for e in row_repl:
        _i, entry, _inner = entry_of(docs.get("cards"), e.card_id)
        if entry is None:
            note(refused, e, f"no card with id {e.card_id!r}")
            continue
        rows = entry.get(e.block)
        if not isinstance(rows, list) or e.index is None or not (0 <= e.index < len(rows)):
            note(refused, e, f"{e.card_id}.{e.block}[{e.index}] is not addressable")
            continue
        current = rows[e.index]
        if current == e.new_value:
            note(satisfied, e, "the row already holds the intended value")
            continue
        if not anchor_ok(current, e.old_value):
            note(refused, e, "the row is no longer what the fixer read — anchor moved")
            continue
        rows[e.index] = copy.deepcopy(e.new_value)
        note(applied, e)

    # -- 2. field edits ----------------------------------------------------- #
    for e in field_ed:
        container, key, where = resolve(docs, e)
        if container is None:
            note(refused, e, where)
            continue
        current = container.get(key)
        if e.new_value is None:
            if key not in container:
                note(satisfied, e, "the key is already gone")
                continue
            if not anchor_ok(current, e.old_value):
                note(refused, e, "the value is no longer what the fixer read — anchor moved")
                continue
            container.pop(key, None)
            note(applied, e)
            continue
        if current == e.new_value:
            note(satisfied, e, "the field already holds the intended value")
            continue
        if not anchor_ok(current, e.old_value):
            note(refused, e, "the value is no longer what the fixer read — anchor moved")
            continue
        container[key] = copy.deepcopy(e.new_value)
        note(applied, e)

    # -- 3. row deletions, highest index first ------------------------------ #
    for e in sorted(row_del, key=lambda x: (x.card_id or "", x.block or "",
                                            -(x.index if x.index is not None else -1))):
        _i, entry, _inner = entry_of(docs.get("cards"), e.card_id)
        if entry is None:
            note(refused, e, f"no card with id {e.card_id!r}")
            continue
        rows = entry.get(e.block)
        if not isinstance(rows, list) or e.index is None or not (0 <= e.index < len(rows)):
            note(refused, e, f"{e.card_id}.{e.block}[{e.index}] is not addressable")
            continue
        if not anchor_ok(rows[e.index], e.old_value):
            note(refused, e, "the row is no longer what the fixer read — anchor moved")
            continue
        rows.pop(e.index)
        note(applied, e)

    # -- 4. entry replacements and removals --------------------------------- #
    for e in entry_ed:
        i, entry, _inner = entry_of(docs.get("cards"), e.card_id)
        if entry is None:
            if e.new_value is None:
                note(satisfied, e, "the card is already gone from the file")
            else:
                note(refused, e, f"no card with id {e.card_id!r}")
            continue
        okay, why = entry_anchor_ok(entry, e)
        if not okay:
            note(refused, e, why)
            continue
        if e.new_value is None:
            docs["cards"].pop(i)
        else:
            docs["cards"][i] = copy.deepcopy(e.new_value)
        note(applied, e)

    return applied, satisfied, refused


def apply_grouped(docs, edits):
    """apply_edits, with GROUPS honoured all-or-nothing. (applied, satisfied, refused).

    The confidence gate already keeps a group whole (gate_split). This closes
    the other half: a group whose members pass the gate but one of whose anchors
    has MOVED must not be applied in part either, for the same reason — half of
    a coupled change is worse than none of it.

    Done as a rehearsal on a throwaway deep copy rather than as a rollback,
    because a rollback of a partially applied entry deletion is not something
    this file should be trying to write. The rehearsal can never touch disk: the
    copy is never handed to write_all.
    """
    grouped = {getattr(e, "group_id", None) for e in edits} - {None}
    if not grouped:
        return apply_edits(docs, edits)

    trial = copy.deepcopy(docs)
    _a, _s, trial_refused = apply_edits(trial, edits)
    broken = {getattr(e, "group_id", None) for e, _w in trial_refused
              if getattr(e, "group_id", None)}
    if not broken:
        return apply_edits(docs, edits)

    runnable = [e for e in edits if getattr(e, "group_id", None) not in broken]
    applied, satisfied, refused = apply_edits(docs, runnable)
    seen = {id(e) for e, _w in refused}
    for e, why in trial_refused:
        if id(e) not in seen:
            refused.append((e, why))
    for e in edits:
        gid = getattr(e, "group_id", None)
        if gid in broken and all(e is not x for x, _w in trial_refused):
            refused.append((e, (
                f"held back whole: another edit in group {gid!r} could not be "
                f"applied, and this pair lands together or not at all")))
    return applied, satisfied, refused


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #
def regen_manifest(seed_dir: Path, manifest: dict, pending: dict | None = None) -> list:
    """Rewrite every declared checksum and size from the bytes that WILL be there.

    Never optional after touching a seed file. A stale checksum is the precise
    failure the app surfaces to a user as "Sync failed" — the data is fine, the
    index disagrees with it, and the app refuses the whole sync. Mirrors
    pipeline/cli.py:_regen_manifest, including the patch-version bump, so the
    two writers cannot drift into producing different manifests.

    `pending` maps a file's name to the bytes staged for it but not yet swapped
    in. Those win over what is on disk, because in a staged transaction the disk
    still holds the OLD cards.json when the manifest is being built, and
    checksumming that would produce a manifest describing bytes that are about
    to stop existing. Absent (the direct call), it reads the disk as before.
    """
    pending = pending or {}
    changed = []
    for f in manifest.get("files", []):
        name = f.get("file") or f.get("name")
        if not name:
            continue
        p = Path(seed_dir) / name
        if name in pending:
            raw = pending[name]
        elif p.exists():
            raw = p.read_bytes()
        else:
            continue
        digest, size = hashlib.sha256(raw).hexdigest(), len(raw)
        if f.get("checksum") != digest or f.get("size_bytes") != size:
            changed.append(name)
        f["checksum"] = digest
        f["size_bytes"] = size
    parts = str(manifest.get("version", "0.0.0")).split(".")
    if len(parts) == 3 and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        manifest["version"] = ".".join(parts)
    manifest["updated_at"] = now_iso()
    return changed


@contextlib.contextmanager
def exclusive(seed_dir: Path):
    """Hold an exclusive lock on this seed directory for the whole apply.

    Two applies against one seed/ are an unsynchronised read-modify-write: both
    read cards.json, both mutate their own copy, both write, and the second
    write erases the first. Measured across six paired runs, one run's edits
    were lost every single time — and the losing run diagnosed it as its own
    fixers being unstable ("NOT IDEMPOTENT ... this run cannot be trusted as
    complete"), which is the worst kind of wrong answer: confident, detailed and
    pointing at the wrong thing. The manifest was lost-updated too, so two
    different byte-states both claimed data version 5.1.22.

    Non-blocking on purpose. A second operator should be told there is already a
    sweep running, not left staring at a hung terminal.
    """
    path = Path(seed_dir).parent / LOCK_NAME
    try:
        fh = open(path, "a+")
    except OSError as ex:
        die(f"could not open the apply lock {path} — {ex}", EXIT_CANNOT_RUN)
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            held = " ".join(fh.read().split())[:120] or "another process"
            die(f"another --apply already holds {path} ({held}). Two applies "
                f"against one seed directory silently lose one run's edits, so "
                f"this one is refusing to start. Wait for it to finish.",
                EXIT_CANNOT_RUN)
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} since {now_iso()}\n")
        fh.flush()
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def fingerprint(path: Path):
    """(size, sha256) of a file, or None. Cheap enough to take twice per run."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return len(raw), hashlib.sha256(raw).hexdigest()


def backup(seed_dir: Path, news_dir: Path, dest: Path) -> list:
    """Copy every file this run could touch, BEFORE it touches anything.

    Runs before the first byte is written, not after the first success. A backup
    taken after a partial write preserves the damage rather than the original.
    """
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for src, rel in ((Path(seed_dir) / CARDS, f"seed/{CARDS}"),
                     (Path(seed_dir) / MANIFEST, f"seed/{MANIFEST}"),
                     (Path(news_dir) / FEED, f"news/{FEED}")):
        if not src.exists():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        saved.append(rel)
    return saved


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def census(edits) -> dict:
    by_code = Counter(e.code for e in edits)
    by_conf = Counter(e.confidence for e in edits)
    by_family = Counter(e.family or "?" for e in edits)
    by_code_conf = defaultdict(Counter)
    for e in edits:
        by_code_conf[e.code][e.confidence] += 1
    return {
        "total": len(edits),
        "by_code": dict(by_code),
        "by_confidence": dict(by_conf),
        "by_family": dict(by_family),
        "by_code_confidence": {k: dict(v) for k, v in by_code_conf.items()},
        "cards_touched": len({e.card_id for e in edits if e.card_id}),
        "irreversible": sum(1 for e in edits if not e.reversible),
    }


def print_plan(cen, gated_cen, gate, holdback):
    head("Planned edits")
    if not cen["total"]:
        info("nothing to do — no fixer proposed an edit for the selected filters")
        return
    width = max((len(c) for c in cen["by_code"]), default=10)
    for code in sorted(cen["by_code"], key=lambda c: (-cen["by_code"][c], c)):
        split = cen["by_code_confidence"][code]
        bits = " · ".join(f"{n(split[k])} {k}" for k in GATE_ORDER if split.get(k))
        applies = gated_cen["by_code"].get(code, 0)
        mark = (f"{C.G}{n(applies)} apply{C.X}" if applies
                else f"{C.Y}held back{C.X}")
        print(f"  {C.DIM}·{C.X}    {code:<{width}}  {n(cen['by_code'][code]):>6}   "
              f"{bits}   [{mark}]")
    info(f"{n(cen['total'])} edit(s) across {n(cen['cards_touched'])} card(s), "
         f"by family: " + " · ".join(f"{k} {n(v)}" for k, v in
                                     sorted(cen["by_family"].items())))
    if cen["irreversible"]:
        warn(f"{n(cen['irreversible'])} edit(s) are NOT reversible from the file "
             f"itself (a removed card cannot be reconstructed from what is left)")
    head(f"Confidence gate: {gate}")
    ok(f"{n(gated_cen['total'])} edit(s) would be applied at --confidence {gate}")
    if holdback:
        info(f"{n(holdback)} edit(s) held back — they are planned and visible above, "
             f"and need --confidence likely plus a human who agrees")


def print_diff(edits, limit=None):
    head("Every proposed change")
    shown = edits if limit is None else edits[:limit]
    for i, e in enumerate(shown, 1):
        tag = f"{C.G}certain{C.X}" if e.confidence == CERTAIN else f"{C.Y}likely{C.X}"
        print(f"\n  {C.BOLD}{i}. {e.anchor()}{C.X}   {e.code}   [{tag}]"
              + ("" if e.reversible else f"   {C.R}NOT REVERSIBLE{C.X}"))
        print(f"     {C.DIM}before{C.X}  {trunc(e.old_value, 100)}")
        print(f"     {C.DIM}after {C.X}  {trunc(e.new_value, 100)}")
        print(f"     {C.DIM}why{C.X}     {trunc(e.reason, 150)}")
        print(f"     {C.DIM}from{C.X}    {trunc(e.evidence, 150)}")
    if limit is not None and len(edits) > limit:
        info(f"... and {n(len(edits) - limit)} more (use --json to read them all)")


def print_delta(before: dict, after: dict) -> list:
    """Before/after error counts per code. Returns the codes that got WORSE.

    Regressions are printed FIRST and in red, above anything that reads as
    success, because the failure mode this whole function exists to catch is a
    tool reporting '1,218 edits applied' while quietly adding errors elsewhere.
    """
    codes = sorted(set(before["by_code"]) | set(after["by_code"]))
    worse = [c for c in codes
             if after["by_code"].get(c, 0) > before["by_code"].get(c, 0)]

    if worse:
        head(f"{C.R}{C.BOLD}REGRESSION — an error count went UP{C.X}")
        for c in worse:
            b, a = before["by_code"].get(c, 0), after["by_code"].get(c, 0)
            err(f"{c}: {n(b)} -> {n(a)}  (+{n(a - b)})")
        err("These edits made something worse. Review the diff before committing; "
            "the backup written above is the pre-edit file.")

    head("Errors by code, before -> after")
    for c in codes:
        b, a = before["by_code"].get(c, 0), after["by_code"].get(c, 0)
        if b == a:
            continue
        line = f"{c:<40} {n(b):>6} -> {n(a):>6}   ({a - b:+d})"
        (err if a > b else ok)(line)
    unchanged = sum(1 for c in codes
                    if before["by_code"].get(c, 0) == after["by_code"].get(c, 0))
    info(f"{n(unchanged)} code(s) unchanged")

    head("Totals")
    for label, key in (("errors", "errors"), ("warnings", "warnings"), ("notes", "notes")):
        b, a = before[key], after[key]
        line = f"{label:<9} {n(b):>6} -> {n(a):>6}   ({a - b:+d})"
        if a > b:
            err(line)
        elif a < b:
            ok(line)
        else:
            info(line)
    return worse


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag. Here 2 is free for other meanings and 3
    is 'could not run', which is what a mistyped flag actually is."""

    def error(self, message):
        die(f"{message}\n\n{self.format_usage().strip()}", 3)


def build_parser():
    p = _Parser(
        prog="fix_cards.py",
        description="Apply the card-data repairs the validator can prove. "
                    "Dry run unless --apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Nothing here publishes. The end of the road is a reviewable diff.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=False,
                   help="plan and print, write nothing (THE DEFAULT)")
    g.add_argument("--apply", action="store_true", default=False,
                   help="actually write the edits that pass the confidence gate")
    p.add_argument("--code", action="append", metavar="CODE",
                   help="only fix this finding code (repeatable)")
    p.add_argument("--family", action="append", metavar="NAME",
                   help="only run fixers in this family, e.g. caps (repeatable)")
    p.add_argument("--confidence", choices=GATE_ORDER, default=CERTAIN,
                   help="lowest confidence that may be APPLIED (default: certain)")
    p.add_argument("--card", action="append", metavar="CARD_ID",
                   help="only fix this card (repeatable)")
    p.add_argument("--diff", action="store_true",
                   help="print a before/after for every proposed edit")
    p.add_argument("--json", metavar="FILE", help="write the full plan as JSON")
    p.add_argument("--limit", type=int, metavar="N",
                   help="stop after N edits (deterministic: the first N in sort order)")
    p.add_argument("--app-root", metavar="DIR",
                   help="the KredMe-main checkout; without it the vendored "
                        "tools/app_mirror/ is used and the run says so")
    p.add_argument("--backup", metavar="DIR", default=str(DEFAULT_BACKUP),
                   help=f"where to write the pre-edit files (default: {DEFAULT_BACKUP})")
    p.add_argument("--no-backup", action="store_true",
                   help="do not write a backup — refuses to run together with --apply "
                        "unless you also pass --i-have-a-backup")
    p.add_argument("--i-have-a-backup", action="store_true",
                   help=argparse.SUPPRESS)
    p.add_argument("--seed-dir", metavar="DIR", help=argparse.SUPPRESS)
    p.add_argument("--news-dir", metavar="DIR", help=argparse.SUPPRESS)
    p.add_argument("--quiet", action="store_true", help="only the verdict")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    quiet = bool(args.quiet)
    applying = bool(args.apply)

    if args.no_backup and applying and not args.i_have_a_backup:
        die("--no-backup with --apply removes the only way back from a bad sweep. "
            "Pass --i-have-a-backup if you genuinely have one.", 3)
    if args.limit is not None and args.limit < 0:
        die("--limit cannot be negative", 3)

    seed_dir = Path(args.seed_dir) if args.seed_dir else LIVE_SEED
    news_dir = Path(args.news_dir) if args.news_dir else LIVE_NEWS
    app_root = Path(args.app_root) if args.app_root else V.APP_ROOT_DEFAULT
    if not (app_root and app_root.is_dir()):
        app_root = None

    if not (seed_dir / CARDS).exists():
        die(f"{seed_dir / CARDS} does not exist — is --seed-dir right?", 3)

    # ---------------------------------------------------------- validate --- #
    ctx, notes = load_ctx(seed_dir, news_dir, app_root)
    findings, skipped, before = validate(ctx)

    if not quiet:
        head("fix_cards — plan" if not applying else "fix_cards — APPLY")
        info(f"data      {seed_dir}")
        info(f"app facts {ctx.categories_source()}"
             + (f" ({ctx.categories_origin})" if ctx.categories_origin else ""))
        for note in notes:
            info(note)
        if skipped:
            warn(f"{n(len(skipped))} check(s) DID NOT RUN — this plan is built on a "
                 f"partial picture and cannot be read as complete")
            for s in skipped:
                skip_line = f"{s.code}: {s.what}"
                print(f"  {C.Y}SKIP{C.X} {trunc(skip_line, 130)}")
        else:
            ok("every check had the inputs it needs — 0 skipped")
        info(f"{n(before['errors'])} error(s) · {n(before['warnings'])} warning(s) · "
             f"{n(before['notes'])} note(s) in {n(len(findings))} finding(s)")

    # -------------------------------------------------------------- plan --- #
    mods = [(name, mod, e) for name, mod, e in discover_fixers()]
    if args.family:
        mods = [(nm, m, e) for nm, m, e in mods
                if m is None or family_matches(args.family, m, nm)]
    if not mods:
        die("no fixer modules selected — check --family", 3)
    if not quiet:
        head("Fixers")
        for nm, m, imp_err in mods:
            if m is None:
                err(f"{nm}: {imp_err}")
            else:
                info(f"{nm:<14} {family_of(m, nm):<32} "
                     f"handles {n(len(handles_of(m)))} code(s)")

    edits, failures, extras = plan_all(ctx, findings, mods)

    kept, blocked = guard(edits)
    if blocked and not quiet:
        head(f"{C.R}Blocked by the rule_name guard{C.X}")
        for e, why in blocked:
            err(f"{getattr(e, 'module', '?')} {e.anchor()} — {why}")

    # The limit is applied SEPARATELY from the other filters so the runner knows
    # whether it truncated the plan. --code and --card name a stable subset: once
    # every edit for that code is applied, no more are planned. --limit does not —
    # it deliberately stops early, so leftover work is the intended outcome, and
    # the idempotency check below must not report a partial run as an unstable one.
    selected_all = select(kept, codes=args.code, cards=args.card)
    selected = (selected_all[: args.limit] if args.limit is not None else selected_all)
    truncated = args.limit is not None and len(selected_all) > len(selected)
    allowed = gate_allows(args.confidence)
    gated, _held = gate_split(selected, allowed)

    # An edit whose own notes name a partner edit ("coupled_to") is a half of
    # something. f2_caps stamps that on a cap it can make readable but cannot
    # re-unit, and the partner — the rule's reward_type — belongs to another
    # family and may not be in this plan at all. Applying the half turns an
    # unenforceable cap into an enforceable one at the wrong denomination, which
    # is how two IndianOil cards got a ceiling about 2x the one the bank states.
    # The fixers now refuse to emit those at all; this is the runner's own lock
    # on the same door, so a future fixer cannot reopen it by accident.
    planned_codes = {(e.card_id, e.code) for e in selected}
    orphaned = [e for e in gated
                if (e.notes or {}).get("coupled_to")
                and (e.card_id, (e.notes or {}).get("coupled_code")) not in planned_codes]
    if orphaned:
        gated = [e for e in gated if e not in orphaned]
        if not quiet:
            head(f"{C.Y}Held back — a coupled partner edit is missing{C.X}")
            for e in orphaned:
                warn(f"{e.anchor()} needs {e.notes['coupled_to']!r} to land in the "
                     f"same run, and no such edit is planned for this card")

    cen, gated_cen = census(selected), census(gated)
    if not quiet:
        print_plan(cen, gated_cen, args.confidence, len(selected) - len(gated))
        if args.diff:
            print_diff(selected, None if args.limit else 200)

    if failures and not quiet:
        head(f"{C.R}Fixer modules that did not deliver{C.X}")
        for f in failures:
            err(f"{f['module']} ({f['kind']}): {f['detail']}")

    result = {
        "meta": {
            "generated_at": now_iso(), "git_head": V.git_head()[:12],
            "mode": "apply" if applying else "dry-run",
            "confidence_gate": args.confidence,
            "seed_dir": str(seed_dir), "news_dir": str(news_dir),
            "app_root": str(app_root) if app_root else None,
            "categories_origin": ctx.categories_origin,
            "categories_source": ctx.categories_source(),
            "degraded": bool(skipped), "skipped_count": len(skipped),
            "filters": {"code": args.code, "family": args.family,
                        "card": args.card, "limit": args.limit},
        },
        "validation_before": before,
        "plan": cen,
        "would_apply": gated_cen,
        "edits": [e.to_dict() for e in selected],
        "blocked_by_guard": [dict(e.to_dict(), blocked_because=why)
                             for e, why in blocked],
        "fixer_failures": failures,
        "fixer_notes": extras,
        "applied": None, "validation_after": None, "regressions": [],
        "idempotent": None, "idempotent_strict": None, "second_pass": None,
    }

    problems = bool(failures or blocked)

    # ------------------------------------------------------------- apply --- #
    if applying and gated:
        # Everything from here to the last swap runs under the lock. Reading the
        # data, mutating it and writing it back is one operation; another --apply
        # getting in between is how one run's 15 edits vanished with no error.
        with exclusive(seed_dir):
            saved = []
            if not args.no_backup:
                stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                dest = Path(args.backup) / stamp
                saved = backup(seed_dir, news_dir, dest)
                if not quiet:
                    head("Backup")
                    ok(f"wrote {n(len(saved))} file(s) to {dest} BEFORE touching anything")
                result["meta"]["backup_dir"] = str(dest)
                result["meta"]["backup_files"] = saved

            docs = load_docs(seed_dir, news_dir)
            before_fp = fingerprint(seed_dir / CARDS)
            pristine = copy.deepcopy(docs)
            applied, satisfied, refused = apply_grouped(docs, gated)

            # Belt as well as braces. The lock stops another copy of THIS tool;
            # this stops anything else — an editor, a git checkout, the pipeline
            # — that moved cards.json while the plan was being applied to the
            # copy in memory. Overwriting it would silently discard whatever
            # that was.
            if fingerprint(seed_dir / CARDS) != before_fp:
                die(f"{seed_dir / CARDS} changed on disk while this run was "
                    f"applying its edits. Nothing has been written. Re-run so the "
                    f"plan is built against the current bytes.", EXIT_CANNOT_RUN)

            # Write only what actually moved, decided by comparing against the
            # bytes loaded a moment ago rather than by which edits were
            # attempted. An edit that turned out to be a no-op must not produce a
            # file modification, because a no-op diff on a 1.78 MB file is
            # exactly the noise that makes the reviewable-PR promise worthless.
            try:
                touched, rechecksummed = write_all(seed_dir, news_dir, docs, pristine)
            except TreeInconsistent as ex:
                where = result["meta"].get("backup_dir") or "(no backup was taken)"
                die(f"THE DATA ON DISK MAY BE INCONSISTENT — {ex}\n"
                    f"  Restore seed/ and news/ from {where} before doing anything "
                    f"else, and do not open a PR from this tree.",
                    EXIT_INCONSISTENT)
            except (OSError, ValueError) as ex:
                where = result["meta"].get("backup_dir") or "(no backup was taken)"
                die(f"could not write the repaired files — {ex}\n"
                    f"  NOTHING was written: every file is staged and verified "
                    f"before any of them is swapped in, so the tree is exactly as "
                    f"it was. The backup at {where} is untouched.", EXIT_CANNOT_RUN)
            touched = set(touched)
            if rechecksummed:
                result["meta"]["manifest_files_rechecksummed"] = rechecksummed

        if not quiet:
            head("Applied")
            ok(f"{n(len(applied))} edit(s) written")
            if satisfied:
                info(f"{n(len(satisfied))} edit(s) were already satisfied — no-ops")
            if refused:
                warn(f"{n(len(refused))} edit(s) REFUSED because the data moved "
                     f"underneath them — nothing was forced")
                for e, why in refused[:20]:
                    warn(f"{e.anchor()} — {why}")
            for f in sorted(touched):
                info(f"rewrote {f}")

        result["applied"] = {
            "count": len(applied), "satisfied": len(satisfied),
            "refused": len(refused), "files": sorted(touched),
            "by_code": dict(Counter(e.code for e in applied)),
            "by_confidence": dict(Counter(e.confidence for e in applied)),
            "refusals": [dict(e.to_dict(), refused_because=why)
                         for e, why in refused],
        }
        if refused:
            problems = True

        # ------------------------------------------------- measure it ----- #
        ctx2, _ = load_ctx(seed_dir, news_dir, app_root)
        findings2, skipped2, after = validate(ctx2)
        result["validation_after"] = after
        worse = [] if quiet else print_delta(before, after)
        if quiet:
            worse = [c for c in set(before["by_code"]) | set(after["by_code"])
                     if after["by_code"].get(c, 0) > before["by_code"].get(c, 0)]
        result["regressions"] = sorted(worse)
        if worse:
            problems = True

        # ------------------------------------------------- idempotency ---- #
        # A fix that must be run twice is a fix nobody can reason about, and it
        # is how a sweep ends up applied three times with three different
        # results. So the second plan is not a nicety: it is the assertion.
        edits2, failures2, _ = plan_all(ctx2, findings2, mods)
        kept2, _blocked2 = guard(edits2)
        again, _held2 = gate_split(
            select(kept2, codes=args.code, cards=args.card, limit=args.limit),
            allowed)

        # Two questions, and they are not the same question.
        #
        #   strict     does a second pass plan ZERO edits? That is the clean
        #              result, and it is what the real catalogue gives.
        #   settles    would a second --apply actually WRITE anything? This is
        #              the property that protects the data. Re-proposing an edit
        #              the file already satisfies is harmless noise; re-proposing
        #              one the file DISAGREES with is not, and a refusal is a
        #              disagreement — it means the plan and the bytes have come
        #              apart, which is exactly the state a third run would
        #              resolve differently again.
        #
        # The probe applies the second plan to a throwaway copy read from disk.
        # It can never write: apply_edits mutates the dict it is handed, and this
        # dict is never passed to write_doc.
        probe = load_docs(seed_dir, news_dir)
        p_applied, p_satisfied, p_refused = apply_grouped(probe, again)
        settles = not p_applied and not p_refused
        result["idempotent"] = settles
        result["idempotent_strict"] = not again
        result["second_pass"] = {
            "planned": len(again), "would_apply": len(p_applied),
            "would_refuse": len(p_refused), "already_satisfied": len(p_satisfied),
            "truncated_by_limit": bool(truncated),
        }
        # A run truncated by --limit is SUPPOSED to leave work behind. Calling that
        # "not idempotent" would train the operator to ignore the one message that
        # means a fixer is genuinely unstable. A refusal is different: it means the
        # plan and the bytes disagree, which --limit does not explain away.
        partial = truncated and not p_refused and settles is False

        if not quiet:
            head("Idempotency")
        if partial:
            result["idempotent"] = None      # not asserted: the run was partial
            if not quiet:
                warn(f"partial run — --limit {args.limit} applied "
                     f"{n(len(applied))} of {n(len(selected_all))} planned edit(s), so "
                     f"{n(len(p_applied))} remain BY DESIGN. Idempotency is not asserted "
                     f"for a truncated run; re-run without --limit to finish the sweep.")
        elif not settles:
            problems = True
            if not quiet:
                err(f"{C.BOLD}NOT IDEMPOTENT{C.X} — a second --apply would still "
                    f"change {n(len(p_applied))} thing(s) and refuse "
                    f"{n(len(p_refused))}. Applying twice would keep moving the "
                    f"file, so this run cannot be trusted as complete.")
                for code, cnt in sorted(
                        Counter(e.code for e in p_applied
                                + [x for x, _w in p_refused]).items(),
                        key=lambda kv: -kv[1])[:12]:
                    err(f"{code}: {n(cnt)} still outstanding")
                if refused:
                    info(f"some of these may be the {n(len(refused))} refusal(s) "
                         f"above — an edit whose anchor moved is planned again, "
                         f"correctly, because it was never applied")
        elif again and not quiet:
            warn(f"a second pass re-proposes {n(len(again))} edit(s), but the file "
                 f"already satisfies every one of them — no byte would change")
        elif not quiet:
            ok("a second pass plans zero edits — applying again would change nothing")
        if failures2:
            result["fixer_failures"].extend(failures2)

    elif applying and not gated:
        if not quiet:
            head("Applied")
            info("nothing to apply — no planned edit passes the confidence gate")
        result["applied"] = {"count": 0, "satisfied": 0, "refused": 0, "files": [],
                             "by_code": {}, "by_confidence": {}, "refusals": []}
        result["idempotent"] = True
        result["idempotent_strict"] = True

    # -------------------------------------------------------------- json --- #
    if args.json:
        p = Path(args.json)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, ensure_ascii=False, default=str)
                fh.write("\n")
        except OSError as ex:
            die(f"could not write the plan to {p} — {ex}", 3)
        if not quiet:
            info(f"wrote {p}")

    # ----------------------------------------------------------- verdict --- #
    if not quiet:
        head("Verdict")
        if not applying:
            ok(f"DRY RUN — nothing was written. {n(gated_cen['total'])} edit(s) would "
               f"be applied at --confidence {args.confidence}; "
               f"re-run with --apply to write them.")
            info("Nothing in this tool publishes to users. --apply changes files in "
                 "your working tree and stops there.")
        elif problems:
            err("applied, but with something a human must look at above")
        else:
            ok("applied cleanly — review `git diff seed/` and open a PR against dev")
        if skipped:
            warn(f"this run was DEGRADED: {n(len(skipped))} check(s) could not look, "
                 f"so the counts above are a floor, not a total")

    return EXIT_PROBLEMS if problems else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        die("interrupted", 3)
