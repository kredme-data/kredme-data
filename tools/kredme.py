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
