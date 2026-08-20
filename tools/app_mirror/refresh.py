#!/usr/bin/env python3
"""Refresh the vendored mirrors of facts that live in the KredMe Flutter app.

    python3 tools/app_mirror/refresh.py --app-root ../KredMe-main

WHY THIS EXISTS
---------------
kredme-data is a PUBLIC repo. KredMe-main is PRIVATE. CI running here can never
check the app out. Two of the validator's questions can only be answered by
reading the app:

    1. Which spending categories does the app recognise?
       (assets/data/categories/categories.json — shipped inside the APK)
    2. Which of our JSON keys does the app's Dart actually read?
       (a substring search over lib/**.dart)

Without an answer, the checks that depend on them go blind — and a blind check
does not merely check less, it invents. Measured on 2026-08-18: running the
validator with no app checkout reported 1,021 errors against a file that has
712. The extra 309 were fabrications about a category vocabulary the run could
not see.

So the answers are mirrored into this directory, by hand, from a real checkout.
This script is the "by hand" — it MEASURES the app rather than restating what
somebody remembers about it, and it records provenance (commit, date, sha256)
so a reviewer can tell how old the answer is.

WHAT A MIRROR IS NOT
--------------------
It is not a source of truth. If it disagrees with the app, the app is right.
The validator raises L3.APP_CATEGORY_MIRROR_DRIFT whenever it is run with a
real --app-root and the two disagree; that alarm can only fire on a developer's
machine, never in CI, which is precisely why refreshing this is a human duty
attached to app releases.

This script only ever WRITES to tools/app_mirror/. It never touches seed/.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

CATEGORIES_REL = "assets/data/categories/categories.json"

# The keys the check modules actually ask Ctx.app_reads_json_key() about. Each
# one is measured, never assumed. Keep this list in step with the call sites:
#     grep -rn "app_reads_json_key\|block_reaches_app" tools/checks/
KEYS_TO_MEASURE = (
    "reward_rules",
    "exclusion_rules",
    "milestone_rules",
    "fuel_surcharge_rules",
    "redemption_rules",
    "redemption_channels",
)


def git(app_root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(app_root), *args],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def measure_key(lib: Path, key: str):
    """Exactly the algorithm Ctx.app_reads_json_key() uses on a live checkout:
    a plain substring search for the QUOTED key across lib/**.dart.

    It over-reports on purpose (a key named only in a comment counts as read),
    and that is the safe direction — over-reporting keeps a finding loud, while
    under-reporting silences a real defect."""
    needles = (f"'{key}'", f'"{key}"')
    hits = []
    for p in sorted(lib.rglob("*.dart")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if any(nd in line for nd in needles):
                hits.append(f"{p.relative_to(lib.parent)}:{i}")
                break
    return bool(hits), hits


def write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"wrote {path.relative_to(REPO)}  ({path.stat().st_size:,} bytes)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="refresh.py",
        description="Re-copy the app-derived facts the validator needs into "
                    "tools/app_mirror/. Requires a real KredMe-main checkout.")
    ap.add_argument("--app-root", required=True, metavar="DIR",
                    help="a KredMe-main checkout to mirror FROM")
    args = ap.parse_args(argv)

    app = Path(args.app_root).expanduser().resolve()
    if not app.is_dir():
        print(f"Stopped: {app} is not a directory.", file=sys.stderr)
        return 3

    src = app / CATEGORIES_REL
    lib = app / "lib"
    if not src.is_file():
        print(f"Stopped: {src} does not exist — that is not a KredMe-main checkout.",
              file=sys.stderr)
        return 3
    if not lib.is_dir():
        print(f"Stopped: {lib} does not exist — cannot measure which keys the app reads.",
              file=sys.stderr)
        return 3

    head = git(app, "rev-parse", "HEAD")
    branch = git(app, "rev-parse", "--abbrev-ref", "HEAD")
    today = datetime.date.today().isoformat()

    # ---------------------------------------------------------------- 1/2 --
    raw = src.read_bytes()
    try:
        cats = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Stopped: the app's categories.json is not valid JSON — {e}", file=sys.stderr)
        return 3
    if not isinstance(cats, list) or not cats:
        print("Stopped: the app's categories.json is not a non-empty list.", file=sys.stderr)
        return 3

    write(HERE / "categories.json", {
        "_this_file_is": (
            "A MIRROR. A hand-maintained copy of a file that lives in the KredMe Flutter "
            "app, not something authored here. Nothing in this repo is the source of truth "
            "for it. If it disagrees with the app, the app is right and this file is stale."),
        "_why_it_exists": (
            "kredme-data is a PUBLIC repo; KredMe-main is PRIVATE, so CI running here can "
            "never check the app out. Without this copy every CI run is blind to the app's "
            "category vocabulary — and a blind run does not merely check less, it invents. "
            "Measured 2026-08-18: with no app checkout the validator reported 1,021 errors "
            "against a file that has 712, the extra 309 being fabrications about categories "
            "it could not see (191 bonus rules called dead, 109 category tags called "
            "unrecognised, 7 category ids called unresolvable)."),
        "_mirrored_from_repo": "KredMe-main (PRIVATE)",
        "_mirrored_from_path": CATEGORIES_REL,
        "_app_commit_at_copy": head,
        "_app_branch_at_copy": branch,
        "_source_last_changed_in_commit": git(app, "log", "-1", "--format=%H", "--", CATEGORIES_REL),
        "_source_last_changed_on": git(app, "log", "-1", "--format=%ad", "--date=short",
                                       "--", CATEGORIES_REL),
        "_source_sha256": hashlib.sha256(raw).hexdigest(),
        "_source_bytes": len(raw),
        "_copied_on": today,
        "_update_by_hand_when": (
            "ANY category is added, removed or renamed in the app. There is no automation "
            "behind this file and there cannot be — this repo has no read access to the app. "
            "The app ships categories.json inside the APK, so a new category needs an app "
            "RELEASE, which is exactly the moment to refresh this copy."),
        "_how_to_update": "python3 tools/app_mirror/refresh.py --app-root ../KredMe-main",
        "_drift_detection": (
            "Any validator run that DOES have --app-root pointing at a real checkout compares "
            "this copy against the app's own file and raises L3.APP_CATEGORY_MIRROR_DRIFT when "
            "they differ. That alarm can only fire on a developer's machine, never in CI — "
            "which is the whole reason the drift check exists."),
        "_do_not": (
            "Do not move this file into seed/. seed/ is the publish surface: tools/kredme.py "
            "copies seed/cards.json and seed/merchants.json to users, and "
            "L3.MANIFEST_UNDECLARED_FILE advises whoever runs the validator that any other "
            ".json sitting in seed/ should be added to seed/manifest.json so it 'reaches "
            "users'. Following that advice would ship the app's internal category table to "
            "every device as card data."),
        "categories": cats,
    })

    # ---------------------------------------------------------------- 2/2 --
    keys, evidence = {}, {}
    for k in KEYS_TO_MEASURE:
        found, hits = measure_key(lib, k)
        keys[k] = found
        evidence[k] = hits[:4]

    dart_files = sum(1 for _ in lib.rglob("*.dart"))
    write(HERE / "app_json_keys.json", {
        "_this_file_is": (
            "A MIRROR of a MEASUREMENT, not a copy of a file. For each JSON key below, "
            "the answer is whether that quoted string occurs anywhere in the app's "
            "lib/**.dart — the same substring search Ctx.app_reads_json_key() runs when "
            "a checkout IS available."),
        "_why_it_exists": (
            "Four checks scale their severity by whether the app can even reach the rows "
            "they are about: a defect in a block the app never reads costs a user nothing "
            "today. With no app checkout that question has no answer, and the old code "
            "resolved 'unknown' by keeping the LOUDER severity — which turned 2 warnings "
            "into errors and 128 notes into warnings on every CI run. Mirroring the "
            "measured answer makes a CI run and a developer's run agree."),
        "_measured_how": "substring search for \"key\" and 'key' over lib/**.dart",
        "_measured_over_files": dart_files,
        "_app_commit_at_copy": head,
        "_app_branch_at_copy": branch,
        "_copied_on": today,
        "_over_reports_on_purpose": (
            "A key mentioned only in a comment counts as read. That is the safe direction: "
            "over-reporting keeps a finding loud; under-reporting would silence a real defect."),
        "_the_finding_this_encodes": (
            "redemption_rules is false and redemption_channels is true. We write 884 "
            "redemption rows under 'redemption_rules'; the app builds its redemption list "
            "from 'redemption_channels'. The two never meet, so the entire redemption block "
            "is dead to the app today."),
        "_update_by_hand_when": (
            "the app starts or stops reading one of these keys — in practice, any release "
            "that touches lib/shared/models/credit_card.dart or the recommendation engine. "
            "A stale 'false' here is the dangerous direction: it would keep a real defect "
            "quiet. When in doubt, delete the key's entry — an absent key answers 'unknown', "
            "which is honest, and the check will SKIP rather than guess."),
        "_how_to_update": "python3 tools/app_mirror/refresh.py --app-root ../KredMe-main",
        "keys_read_by_app": keys,
        "evidence": evidence,
    })

    print(f"\nmirrored from {app}")
    print(f"  app commit   {head[:12]} ({branch})")
    print(f"  categories   {len(cats)}")
    print(f"  keys read    " + ", ".join(f"{k}={str(v).lower()}" for k, v in keys.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
