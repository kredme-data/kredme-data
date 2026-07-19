#!/usr/bin/env python3
"""
Regression tests for tools/kredme.py — the safe publish pipeline.

Runs the real CLI against throwaway fixture repos in a temp directory.
Never touches the actual seed/, news/ or staging/ trees.

    python3 tools/test_pipeline.py            # run all
    python3 tools/test_pipeline.py -v         # show CLI output on failure

No third-party packages. Python 3.8+.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "kredme.py"
VERBOSE = "-v" in sys.argv

_results: list = []


# ------------------------------------------------------------- fixtures ----

def card(cid: str) -> dict:
    """A card entry in the real nested shape: {"card": {...}, "reward_rules": []}."""
    return {
        "card": {"id": cid, "card_name": cid.replace("_", " ").title(), "issuer": "test"},
        "reward_rules": [{"rule_id": f"{cid}_base", "reward_rate": 1}],
    }


def news_item(iid="news_001", **over) -> dict:
    item = {
        "id": iid,
        "title": "Test headline",
        "summary": "Test summary body.",
        "category": "announcement",
        "severity": "info",
        "source": "KredMe",
        "source_url": "https://example.com",
        "published_at": "2026-07-19T00:00:00Z",
        "expiry_date": None,
        "affected_cards": [],
    }
    item.update(over)
    return item


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def build_repo(tmp: Path, *, cards=("card_a", "card_b"), items=None,
               seed_version="1.0.0", news_version="1.0.0") -> Path:
    """Create a minimal but structurally real kredme-data repo."""
    repo = tmp
    (repo / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOOL, repo / "tools" / "kredme.py")

    seed = repo / "seed"
    write_json(seed / "cards.json", [card(c) for c in cards])
    write_json(seed / "merchants.json", {
        "_metadata": {"version": seed_version},
        "categories": [{"id": "dining", "name": "Dining"}],
        "merchants": [{"id": 1, "merchant_name": "test_m", "category_id": "dining"}],
    })
    manifest = {
        "version": seed_version,
        "updated_at": "2026-07-19T00:00:00Z",
        "min_app_version": "1.0.0",
        "files": [],
        "delta_file": None,
        "news_version": news_version,
    }
    for name in ("cards.json", "merchants.json"):
        b = (seed / name).read_bytes()
        manifest["files"].append({
            "name": name, "path": f"seed/{name}",
            "checksum": hashlib.sha256(b).hexdigest(), "size_bytes": len(b),
        })
    write_json(seed / "manifest.json", manifest)

    write_json(repo / "news" / "feed.json", {
        "version": news_version,
        "updated_at": "2026-07-19T00:00:00Z",
        "items": items if items is not None else [news_item()],
    })
    return repo


def run(repo: Path, *args, expect=None):
    r = subprocess.run(
        [sys.executable, str(repo / "tools" / "kredme.py"), *args],
        capture_output=True, text=True, timeout=120,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    if expect is not None and r.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}, got {r.returncode}\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        )
    return r


def ver(repo: Path, which: str) -> str:
    if which == "seed":
        return json.loads((repo / "seed" / "manifest.json").read_text())["version"]
    return json.loads((repo / "news" / "feed.json").read_text())["version"]


# ---------------------------------------------------------------- checks ----

def test(fn):
    name = fn.__name__.replace("test_", "").replace("_", " ")
    with tempfile.TemporaryDirectory() as td:
        try:
            fn(Path(td))
            _results.append((name, None))
            print(f"  \033[32mPASS\033[0m  {name}")
        except AssertionError as e:
            _results.append((name, str(e)))
            print(f"  \033[31mFAIL\033[0m  {name}")
            if VERBOSE:
                print(f"        {str(e)[:2000]}")
        except Exception as e:  # noqa: BLE001
            _results.append((name, f"{type(e).__name__}: {e}"))
            print(f"  \033[31mERROR\033[0m {name}  ({type(e).__name__}: {e})")
    return fn


# --- validation gate --------------------------------------------------------

@test
def test_clean_data_validates(td: Path):
    repo = build_repo(td)
    run(repo, "init", expect=0)
    run(repo, "validate", "--target", "staging", expect=0)


@test
def test_wrong_news_keys_are_rejected(td: Path):
    """expires_at / url are silently ignored by the app — must be fatal."""
    bad = news_item()
    bad["expires_at"] = bad.pop("expiry_date")
    bad["url"] = bad.pop("source_url")
    repo = build_repo(td, items=[bad])
    run(repo, "init", expect=0)
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "expiry_date" in r.stdout, "should name the correct key"
    assert "source_url" in r.stdout, "should name the correct key"


@test
def test_unknown_affected_card_is_rejected(td: Path):
    """A typo'd card id means the alert reaches nobody — must be fatal."""
    repo = build_repo(td, items=[news_item(affected_cards=["card_a", "card_TYPO"])])
    run(repo, "init", expect=0)
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "card_TYPO" in r.stdout
    assert "NOBODY" in r.stdout


@test
def test_missing_required_news_field_is_rejected(td: Path):
    item = news_item()
    del item["summary"]
    repo = build_repo(td, items=[item])
    run(repo, "init", expect=0)
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "summary" in r.stdout


@test
def test_duplicate_news_ids_rejected(td: Path):
    repo = build_repo(td, items=[news_item("dup"), news_item("dup")])
    run(repo, "init", expect=0)
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "duplicate id" in r.stdout


@test
def test_bad_severity_rejected(td: Path):
    repo = build_repo(td, items=[news_item(severity="catastrophic")])
    run(repo, "init", expect=0)
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "severity" in r.stdout


@test
def test_live_checksum_mismatch_is_fatal(td: Path):
    """The app rejects a checksum mismatch — live validation must too."""
    repo = build_repo(td)
    cards = repo / "seed" / "cards.json"
    cards.write_text(json.dumps([card("card_a"), card("card_b"), card("card_c")], indent=2))
    r = run(repo, "validate", "--target", "live", expect=1)
    assert "REJECT" in r.stdout or "mismatch" in r.stdout


@test
def test_staging_checksum_mismatch_is_only_a_warning(td: Path):
    """Editing staging by hand is normal; publish regenerates the manifest."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    st = repo / "staging" / "seed" / "cards.json"
    st.write_text(json.dumps([card("card_a"), card("card_b"), card("card_c")], indent=2))
    r = run(repo, "validate", "--target", "staging", expect=0)
    assert "regenerate" in r.stdout


@test
def test_truncated_catalog_warns(td: Path):
    repo = build_repo(td, cards=("only_one",))
    run(repo, "init", expect=0)
    r = run(repo, "validate", "--target", "staging", expect=0)
    assert "Truncated" in r.stdout or "expected a few hundred" in r.stdout


# --- publish ----------------------------------------------------------------

@test
def test_publish_refuses_invalid_staging(td: Path):
    """The whole point: bad data must never reach live."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    bad = news_item()
    bad["expires_at"] = bad.pop("expiry_date")
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "1.0.0", "updated_at": "2026-07-19T00:00:00Z", "items": [bad]})
    run(repo, "publish", "--yes", expect=1)
    assert ver(repo, "news") == "1.0.0", "live must be untouched after a refused publish"


@test
def test_publish_bumps_news_major_and_seed_patch(td: Path):
    repo = build_repo(td, seed_version="5.2.0", news_version="1.0.0")
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "1.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(), news_item("news_002")]})
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), card("card_b"), card("card_c")])
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    assert ver(repo, "news") == "2.0.0", f"news must MAJOR-bump, got {ver(repo,'news')}"
    assert ver(repo, "seed") == "5.2.1", f"seed must patch-bump, got {ver(repo,'seed')}"


@test
def test_publish_regenerates_correct_checksums(td: Path):
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), card("card_b"), card("card_c")])
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    # Live must now be internally consistent under STRICT checking.
    run(repo, "validate", "--target", "live", expect=0)
    manifest = json.loads((repo / "seed" / "manifest.json").read_text())
    for f in manifest["files"]:
        actual = hashlib.sha256((repo / "seed" / f["name"]).read_bytes()).hexdigest()
        assert f["checksum"] == actual, f"{f['name']} checksum not regenerated"


@test
def test_publish_syncs_manifest_news_version(td: Path):
    repo = build_repo(td, news_version="3.0.0")
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "3.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(), news_item("news_002")]})
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    manifest = json.loads((repo / "seed" / "manifest.json").read_text())
    assert manifest["news_version"] == ver(repo, "news") == "4.0.0"


@test
def test_publish_noop_when_identical(td: Path):
    repo = build_repo(td)
    run(repo, "init", expect=0)
    r = run(repo, "publish", "--yes", expect=0)
    assert "nothing to publish" in r.stdout.lower()


@test
def test_dry_run_writes_nothing(td: Path):
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), card("card_b"), card("card_c")])
    before = (repo / "seed" / "cards.json").read_bytes()
    run(repo, "publish", "--dry-run", "--allow-warnings", expect=0)
    assert (repo / "seed" / "cards.json").read_bytes() == before, "dry run modified live"
    assert not (repo / ".published").exists(), "dry run created a snapshot"


# --- undo -------------------------------------------------------------------

@test
def test_undo_restores_previous_live(td: Path):
    repo = build_repo(td, seed_version="5.2.0", news_version="1.0.0")
    run(repo, "init", expect=0)
    original = (repo / "seed" / "cards.json").read_bytes()
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), card("card_b"), card("card_c")])
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    assert ver(repo, "seed") == "5.2.1"

    run(repo, "undo", "--yes", expect=0)
    assert ver(repo, "seed") == "5.2.0", "undo must restore the version"
    assert (repo / "seed" / "cards.json").read_bytes() == original, "undo must restore bytes"


@test
def test_undo_is_itself_undoable(td: Path):
    """Undo snapshots first, so a mistaken rollback is recoverable."""
    repo = build_repo(td, seed_version="5.2.0")
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), card("card_b"), card("card_c")])
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    run(repo, "undo", "--yes", expect=0)
    assert ver(repo, "seed") == "5.2.0"
    run(repo, "undo", "--yes", expect=0)          # roll the rollback forward
    assert ver(repo, "seed") == "5.2.1", "should return to the published state"


@test
def test_undo_with_no_snapshots_fails_safely(td: Path):
    repo = build_repo(td)
    r = run(repo, "undo", "--yes", expect=1)
    assert "nothing to undo" in r.stdout.lower()


@test
def test_snapshot_is_gitignored(td: Path):
    """Snapshots must never be committed — Pages would serve them publicly."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    gi = (repo / ".gitignore").read_text()
    assert ".published" in gi, ".published/ must be git-ignored"


# ------------------------------------------------------------------ main ----

def main() -> None:
    print(f"\n\033[1mkredme.py pipeline tests\033[0m  ({TOOL})\n")
    failed = [(n, e) for n, e in _results if e]
    print()
    if failed:
        print(f"\033[31m\033[1m{len(failed)} failed\033[0m / {len(_results)} total")
        for n, e in failed:
            print(f"\n  \033[1m{n}\033[0m\n    {e.splitlines()[0] if e else ''}")
        print()
        sys.exit(1)
    print(f"\033[32m\033[1mall {len(_results)} tests passed\033[0m\n")


if __name__ == "__main__":
    main()
