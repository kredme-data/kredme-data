#!/usr/bin/env python3
"""
kredme.py — safe publish pipeline for the kredme-data OTA backend.

WHY THIS EXISTS
---------------
This repo IS the live backend. Anything committed to `main` and pushed is
served to every real user by GitHub Pages within a minute. There is no staging
gate, no validation, and no way back. This tool adds all three.

THE MODEL
---------
    staging/            <- you edit HERE. Never served to users.
      seed/{manifest,cards,merchants}.json
      news/feed.json

    seed/ , news/       <- LIVE. Only ever written by `publish`.

    .published/         <- automatic snapshots of live, taken before every
                           publish. This is what `undo` restores from.
                           Git-ignored: never served, never pushed.

COMMANDS
--------
    python3 tools/kredme.py status              what's staged vs live
    python3 tools/kredme.py validate            check staging is safe to ship
    python3 tools/kredme.py publish --dry-run   show exactly what would change
    python3 tools/kredme.py publish             staging -> live (still local)
    python3 tools/kredme.py undo                restore the previous live state
    python3 tools/kredme.py undo --list         show restore points

`publish` NEVER touches the network. It writes local files and prints the one
git command that actually goes live, so pushing stays a deliberate human act.

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
STAGING = REPO / "staging"
STAGING_SEED = STAGING / "seed"
STAGING_NEWS = STAGING / "news"
SNAPSHOTS = REPO / ".published"

SEED_FILES = ("cards.json", "merchants.json")
MANIFEST = "manifest.json"
FEED = "feed.json"

PAGES_BASE = "https://kredme-data.github.io/kredme-data"

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


def size_str(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f}KB"
    return f"{n/1024**2:.2f}MB"


def version_tuple(v: str):
    """Leading numeric components of a version string. '0.0.1-test' -> (0,0,1)."""
    parts = re.split(r"[.\-+]", str(v))
    out = []
    for p in parts:
        if p.isdigit():
            out.append(int(p))
        else:
            break
    return tuple(out) if out else (0,)


def bump_major(v: str) -> str:
    """News: the app refetches ONLY when int(version.split('.')[0]) increases."""
    return f"{version_tuple(v)[0] + 1}.0.0"


def bump_patch(v: str) -> str:
    """Seed: the app compares the version string, so any change syncs."""
    t = list(version_tuple(v))
    while len(t) < 3:
        t.append(0)
    t[2] += 1
    return ".".join(str(x) for x in t[:3])


def git(*args: str):
    try:
        r = subprocess.run(["git", "-C", str(REPO), *args],
                           capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:  # git missing / not a repo — never fatal
        return 1, "", str(e)


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


def validate_seed(seed_dir: Path, rep: Report, strict_checksums: bool = True) -> set:
    """Validate a seed/ directory. Returns the set of card IDs found.

    strict_checksums=True  (live)    — a mismatch is fatal: the app rejects the
                                       sync and the user sees "Sync failed".
    strict_checksums=False (staging) — a mismatch is expected while editing;
                                       publish rebuilds the manifest from bytes.
    """
    head(f"Seed  {seed_dir.relative_to(REPO)}")
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
            rep.warn(f"{name}: edited since the manifest was written — publish will regenerate it")
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
            for entry in cards:
                if not isinstance(entry, dict):
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
            # Guard against a truncated/partial publish wiping the catalog.
            if seen and len(seen) < 50:
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
            # or that reward rule can never fire.
            if merchant_refs and seen_name:
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
    head(f"News  {news_dir.relative_to(REPO)}")
    fpath = news_dir / FEED
    if not fpath.exists():
        rep.error(f"{FEED} is missing")
        return

    try:
        feed = read_json(fpath)
    except json.JSONDecodeError as e:
        rep.error(f"{FEED} is not valid JSON: {e}")
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


def ensure_staging() -> None:
    """Create staging/ from live on first use, so there is no manual setup step."""
    if STAGING_SEED.exists() and (STAGING_SEED / MANIFEST).exists():
        return
    print(f"{C.DIM}  (first run — creating staging/ from the current live data){C.X}")
    STAGING_SEED.mkdir(parents=True, exist_ok=True)
    STAGING_NEWS.mkdir(parents=True, exist_ok=True)
    for name in (*SEED_FILES, MANIFEST):
        src = LIVE_SEED / name
        if src.exists():
            shutil.copy2(src, STAGING_SEED / name)
    src = LIVE_NEWS / FEED
    if src.exists():
        shutil.copy2(src, STAGING_NEWS / FEED)
    ensure_gitignore()


def run_validation(target: str) -> Report:
    rep = Report()
    if target == "staging":
        ensure_staging()
        seed_dir, news_dir = STAGING_SEED, STAGING_NEWS
    else:
        seed_dir, news_dir = LIVE_SEED, LIVE_NEWS

    card_ids = validate_seed(seed_dir, rep, strict_checksums=(target == "live"))
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
    if ".published" in have and "staging" in have:
        return
    with open(gi, "a", encoding="utf-8") as fh:
        if lines and lines[-1].strip():
            fh.write("\n")
        fh.write(
            "# local publish snapshots — never serve or commit these\n"
            ".published/\n"
            "# local staging working area — recreated from live by any command\n"
            "staging/\n"
        )
    info("added .published/ to .gitignore")


# -------------------------------------------------------------- commands ----

def cmd_init(args) -> None:
    head("Initialising staging/ from the current live data")
    if STAGING.exists() and any(STAGING.rglob("*.json")) and not args.force:
        die("staging/ already exists. Use --force to overwrite it from live.")
    STAGING_SEED.mkdir(parents=True, exist_ok=True)
    STAGING_NEWS.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in (*SEED_FILES, MANIFEST):
        src = LIVE_SEED / name
        if src.exists():
            shutil.copy2(src, STAGING_SEED / name)
            ok(f"staging/seed/{name}  ({size_str(src.stat().st_size)})")
            copied += 1
    src = LIVE_NEWS / FEED
    if src.exists():
        shutil.copy2(src, STAGING_NEWS / FEED)
        ok(f"staging/news/{FEED}")
        copied += 1
    ensure_gitignore()
    print(f"\n{C.G}{C.BOLD}staging/ ready{C.X} — {copied} file(s) copied from live.")
    print(f"{C.DIM}Edit staging/, then:  python3 tools/kredme.py validate{C.X}\n")


def cmd_status(args) -> None:
    head("Where things stand")
    print(f"  repo      {REPO}")
    print(f"  branch    {git_branch()}   {C.DIM}(live is served from 'main'){C.X}")
    if git_branch() not in ("main", "unknown"):
        warn(f"you are on '{git_branch()}' — publishing is only live from 'main'")

    lsv, lnv = read_versions(LIVE_SEED, LIVE_NEWS)
    print(f"\n  {C.BOLD}LIVE{C.X}      seed {lsv}   news {lnv}")
    if STAGING.exists():
        ssv, snv = read_versions(STAGING_SEED, STAGING_NEWS)
        print(f"  {C.BOLD}STAGING{C.X}   seed {ssv}   news {snv}")
    else:
        ensure_staging()
        ssv, snv = read_versions(STAGING_SEED, STAGING_NEWS)
        print(f"  {C.BOLD}STAGING{C.X}   seed {ssv}   news {snv}")

    head("Staged changes (staging vs live)")
    changed = False
    pairs = [(STAGING_SEED / n, LIVE_SEED / n) for n in (*SEED_FILES, MANIFEST)]
    pairs.append((STAGING_NEWS / FEED, LIVE_NEWS / FEED))
    for s, l in pairs:
        rel = s.relative_to(REPO)
        if not s.exists():
            continue
        if not l.exists():
            print(f"  {C.G}NEW{C.X}     {rel}")
            changed = True
        elif sha256(s) != sha256(l):
            delta = s.stat().st_size - l.stat().st_size
            sign = "+" if delta >= 0 else "-"
            print(f"  {C.Y}CHANGED{C.X} {rel}  ({size_str(s.stat().st_size)}, {sign}{size_str(abs(delta))})")
            changed = True
    if not changed:
        print(f"  {C.DIM}nothing staged — staging matches live{C.X}")

    snaps = snapshot_dirs()
    head("Restore points")
    if snaps:
        for d in snaps[:5]:
            print(f"  {d.name}")
        if len(snaps) > 5:
            print(f"  {C.DIM}… and {len(snaps)-5} older{C.X}")
    else:
        print(f"  {C.DIM}none yet — the first publish creates one{C.X}")
    print()


def cmd_validate(args) -> None:
    rep = run_validation(args.target)
    print_verdict(rep, args.target)
    print()
    sys.exit(1 if rep.failed else 0)


def cmd_publish(args) -> None:
    ensure_staging()

    head("Step 1 — validate staging (nothing is written until this passes)")
    rep = run_validation("staging")
    print_verdict(rep, "staging")
    if rep.failed:
        die("staging has errors — refusing to publish. This is the gate working.")

    # Work out the new versions.
    live_sv, live_nv = read_versions(LIVE_SEED, LIVE_NEWS)
    stage_manifest = read_json(STAGING_SEED / MANIFEST)
    stage_feed = read_json(STAGING_NEWS / FEED)

    seed_changed = any(
        (STAGING_SEED / n).exists() and (
            not (LIVE_SEED / n).exists()
            or sha256(STAGING_SEED / n) != sha256(LIVE_SEED / n)
        )
        for n in SEED_FILES
    )
    news_changed = (
        (STAGING_NEWS / FEED).exists() and (
            not (LIVE_NEWS / FEED).exists()
            or sha256(STAGING_NEWS / FEED) != sha256(LIVE_NEWS / FEED)
        )
    )
    # Nothing staged? Say so and stop — warnings are irrelevant when there is
    # nothing to ship.
    if not seed_changed and not news_changed:
        die("staging is identical to live — nothing to publish.", code=0)

    # Only now does the warnings gate apply.
    if rep.warnings and not args.allow_warnings and not args.dry_run:
        print(f"\n{C.Y}{C.BOLD}{len(rep.warnings)} warning(s).{C.X} "
              f"Re-run with --allow-warnings to publish anyway.")
        sys.exit(2)

    new_sv = bump_patch(live_sv) if seed_changed else live_sv
    # The app only refetches news when the MAJOR integer increases. Bake that
    # in so the operator can never get it wrong.
    new_nv = bump_major(live_nv) if news_changed else live_nv

    head("Step 2 — what will change")
    if seed_changed:
        print(f"  seed   {live_sv}  ->  {C.G}{new_sv}{C.X}   {C.DIM}(patch bump; app compares the string){C.X}")
    else:
        print(f"  seed   {live_sv}      {C.DIM}unchanged{C.X}")
    if news_changed:
        print(f"  news   {live_nv}  ->  {C.G}{new_nv}{C.X}   {C.DIM}(MAJOR bump — app ignores minor bumps){C.X}")
    else:
        print(f"  news   {live_nv}      {C.DIM}unchanged{C.X}")
    for n in (*SEED_FILES, MANIFEST):
        s = STAGING_SEED / n
        if s.exists() and (not (LIVE_SEED / n).exists() or sha256(s) != sha256(LIVE_SEED / n)):
            print(f"  write  seed/{n}  ({size_str(s.stat().st_size)})")
    if news_changed:
        print(f"  write  news/{FEED}")

    if args.dry_run:
        print(f"\n{C.B}{C.BOLD}Dry run — nothing was written.{C.X}\n")
        return

    if not args.yes:
        print()
        reply = input(f"  Publish staging -> live locally? {C.DIM}(y/N){C.X} ").strip().lower()
        if reply not in ("y", "yes"):
            die("cancelled — nothing was written.", code=0)

    head("Step 3 — snapshot the current live data (this is your undo)")
    snap = take_snapshot(reason=f"pre-publish seed {live_sv}->{new_sv}, news {live_nv}->{new_nv}")
    ensure_gitignore()
    ok(f"saved {snap.relative_to(REPO)}")

    head("Step 4 — promote staging to live")
    LIVE_SEED.mkdir(parents=True, exist_ok=True)
    LIVE_NEWS.mkdir(parents=True, exist_ok=True)

    if news_changed:
        stage_feed["version"] = new_nv
        stage_feed["updated_at"] = now_iso()
        write_json(LIVE_NEWS / FEED, stage_feed)
        write_json(STAGING_NEWS / FEED, stage_feed)  # keep staging in step
        ok(f"news/{FEED} -> {new_nv}")

    if seed_changed:
        for n in SEED_FILES:
            s = STAGING_SEED / n
            if s.exists():
                shutil.copy2(s, LIVE_SEED / n)
                ok(f"seed/{n}")

    # Rebuild the manifest from the bytes we just wrote. Never trust a
    # hand-edited checksum — a mismatch is what causes "Sync failed".
    manifest = dict(stage_manifest)
    manifest["version"] = new_sv
    manifest["updated_at"] = now_iso()
    manifest["news_version"] = new_nv
    files = []
    for n in SEED_FILES:
        p = LIVE_SEED / n
        if p.exists():
            files.append({
                "name": n,
                "path": f"seed/{n}",
                "checksum": sha256(p),
                "size_bytes": p.stat().st_size,
            })
    manifest["files"] = files
    write_json(LIVE_SEED / MANIFEST, manifest)
    ok(f"seed/{MANIFEST} rebuilt — {len(files)} file(s), checksums recomputed")

    stage_manifest_out = dict(manifest)
    write_json(STAGING_SEED / MANIFEST, stage_manifest_out)

    head("Step 5 — verify what we just wrote")
    rep2 = run_validation("live")
    print_verdict(rep2, "live")
    if rep2.failed:
        print(f"\n{C.R}The write produced invalid live data.{C.X} Roll it back now:")
        print(f"    python3 tools/kredme.py undo\n")
        sys.exit(1)

    print(f"\n{C.G}{C.BOLD}Published locally.{C.X} Nothing has reached users yet.")
    print(f"\n{C.BOLD}To go live{C.X} (this is the only networked step):")
    print(f"    git -C {REPO} checkout main")
    print(f"    git -C {REPO} add seed news")
    print(f'    git -C {REPO} commit -m "data: seed {new_sv}, news {new_nv}"')
    print(f"    git -C {REPO} push origin main")
    print(f"\n{C.BOLD}To check it landed{C.X} (~1 min after push):")
    print(f"    curl -s {PAGES_BASE}/seed/manifest.json | python3 -m json.tool | head -5")
    print(f"\n{C.BOLD}If it goes wrong{C.X}:")
    print(f"    python3 tools/kredme.py undo     {C.DIM}then commit + push again{C.X}\n")


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

    head("Undo — restore live from a snapshot")
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
    rep = run_validation("live")
    print_verdict(rep, "live")

    print(f"\n{C.G}{C.BOLD}Restored locally.{C.X} To make the rollback live:")
    print(f"    git -C {REPO} checkout main")
    print(f"    git -C {REPO} add seed news")
    print(f'    git -C {REPO} commit -m "data: roll back to seed {meta.get("seed_version","?")}"')
    print(f"    git -C {REPO} push origin main\n")


# ------------------------------------------------------------------ main ----

def main() -> None:
    p = argparse.ArgumentParser(
        prog="kredme.py",
        description="Safe publish pipeline for the kredme-data OTA backend.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical day:  edit staging/  ->  validate  ->  publish --dry-run  ->  publish  ->  git push",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create staging/ from the current live data")
    s.add_argument("--force", action="store_true", help="overwrite an existing staging/")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="show staging vs live and restore points")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("validate", help="check data is safe to ship")
    s.add_argument("--target", choices=("staging", "live"), default="staging")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("publish", help="promote staging -> live (local only)")
    s.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--allow-warnings", action="store_true", help="publish despite warnings")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("undo", help="restore live from the previous snapshot")
    s.add_argument("--list", action="store_true", help="show restore points")
    s.add_argument("--to", metavar="SNAPSHOT", help="restore a specific snapshot")
    s.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    s.set_defaults(func=cmd_undo)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
