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



# --- UNIT tests for the pure helpers ----------------------------------------
# These were the coverage gap: every test above drives the whole CLI, so a bug
# in version handling only surfaced if it changed a visible outcome. An audit
# found exactly that class of bug (a live version of "v7.0.0" silently
# collapsing to "1.0.0" and permanently stopping news refetch).

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("kredme_mod", TOOL)
K = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(K)


def unit(fn):
    name = "unit: " + fn.__name__.replace("test_", "").replace("_", " ")
    try:
        fn()
        _results.append((name, None))
        print(f"  \033[32mPASS\033[0m  {name}")
    except AssertionError as e:
        _results.append((name, str(e)))
        print(f"  \033[31mFAIL\033[0m  {name}")
    except Exception as e:  # noqa: BLE001
        _results.append((name, f"{type(e).__name__}: {e}"))
        print(f"  \033[31mERROR\033[0m {name}  ({type(e).__name__}: {e})")
    return fn


@unit
def test_version_tuple_forms():
    assert K.version_tuple("5.2.0") == (5, 2, 0)
    assert K.version_tuple("0.0.1-test") == (0, 0, 1)
    assert K.version_tuple("v7.0.0") == (7, 0, 0), "leading v must not read as 0"
    assert K.version_tuple("V7.0.0") == (7, 0, 0)
    assert K.version_tuple("3") == (3,)


@unit
def test_strict_refuses_unreadable_versions():
    for bad in (None, "", "?", "abc", "vv1"):
        try:
            K.version_tuple(bad, strict=True)
            raise AssertionError(f"{bad!r} should have raised VersionError")
        except K.VersionError:
            pass


@unit
def test_bump_major_never_lowers_a_v_prefixed_version():
    # The critical bug: "v7.0.0" -> "1.0.0" would freeze news forever.
    assert K.bump_major("v7.0.0") == "8.0.0"
    assert K.bump_major("7.0.0") == "8.0.0"
    assert K.version_gt(K.bump_major("v7.0.0"), "7.0.0")


@unit
def test_bump_patch():
    assert K.bump_patch("5.2.0") == "5.2.1"
    assert K.bump_patch("5") == "5.0.1"
    assert K.bump_patch("v1.0.0") == "1.0.1"


@unit
def test_version_gt_ordering():
    assert K.version_gt("2.0.0", "1.0.0")
    assert K.version_gt("5.2.1", "5.2.0")
    assert not K.version_gt("1.0.0", "1.0.0")
    assert not K.version_gt("1.0.0", "2.0.0")
    assert K.version_gt("1.0.1", "1.0")       # padding
    assert not K.version_gt("1.0", "1.0.0")


@unit
def test_every_bump_is_strictly_greater():
    for v in ("0.0.1", "1.0.0", "5.2.0", "9.9.9", "v3.1.4", "12.0.0"):
        assert K.version_gt(K.bump_major(v), v), f"bump_major({v}) not greater"
        assert K.version_gt(K.bump_patch(v), v), f"bump_patch({v}) not greater"



# --- REGRESSION: each of these reproduces a bug found by the 2026-07-19 audit.
# Every one of them FAILED before the fix and passes after.

@test
def test_corrupt_live_feed_refuses_publish_not_downgrade(td: Path):
    """CRITICAL: a corrupt live feed used to publish version 1.0.0, which is
    LOWER than what users hold — the app then never refetches news again."""
    repo = build_repo(td, news_version="7.0.0")
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "7.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(), news_item("news_002")]})
    (repo / "news" / "feed.json").write_text("{ this is not json", encoding="utf-8")
    r = run(repo, "publish", "--yes", "--allow-warnings")
    assert r.returncode != 0, "must refuse to publish over an unreadable live feed"
    assert "1.0.0" not in (repo / "news" / "feed.json").read_text(), "must not downgrade"


@test
def test_v_prefixed_live_version_bumps_correctly(td: Path):
    """CRITICAL: live 'v7.0.0' used to collapse to '1.0.0' with a green tick."""
    repo = build_repo(td, news_version="v7.0.0")
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "v7.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(), news_item("news_002")]})
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    assert ver(repo, "news") == "8.0.0", f"expected 8.0.0, got {ver(repo,'news')}"


@test
def test_republish_after_undo_never_reuses_a_version(td: Path):
    """CRITICAL: a correction pushed after undo re-used a version users already
    held, so it silently reached nobody while printing success."""
    repo = build_repo(td, news_version="3.0.0")
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "3.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(title="WRONG alert")]})
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    served = ver(repo, "news")
    assert served == "4.0.0"
    run(repo, "undo", "--yes", expect=0)
    assert ver(repo, "news") == "3.0.0"
    write_json(repo / "staging" / "news" / "feed.json",
               {"version": "3.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(title="CORRECTED alert")]})
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    now = ver(repo, "news")
    assert K.version_gt(now, served), (
        f"republish produced {now}, which users already have as {served} — "
        f"the correction would reach nobody")


@test
def test_empty_merchants_is_rejected(td: Path):
    """CRITICAL false-negative: an emptied merchants file passed the gate AND
    disabled the merchant_ref check that would have caught it."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "merchants.json",
               {"_metadata": {}, "categories": [{"id": "dining"}], "merchants": []})
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "no merchants" in r.stdout.lower()


@test
def test_missing_categories_is_rejected(td: Path):
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "merchants.json",
               {"_metadata": {}, "categories": [],
                "merchants": [{"id": 1, "merchant_name": "test_m", "category_id": "dining"}]})
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "no categories" in r.stdout.lower()


@test
def test_non_dict_card_entries_rejected(td: Path):
    """Cards that are not objects used to be skipped silently — they would
    simply vanish from live while the gate reported the file as fine."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), "oops_a_string", None, card("card_b")])
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "not objects" in r.stdout


@test
def test_catalog_shrink_is_rejected(td: Path):
    """An absolute floor of 50 was useless against a 376-card catalog."""
    repo = build_repo(td, cards=tuple(f"card_{i}" for i in range(100)))
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "cards.json", [card("card_0"), card("card_1")])
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "disappear" in r.stdout
    # ...unless the operator says it is deliberate
    run(repo, "validate", "--target", "staging", "--allow-shrink", expect=0)


@test
def test_array_shaped_json_errors_cleanly(td: Path):
    """A JSON array where an object belongs used to raise AttributeError."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "news" / "feed.json", [news_item()])
    r = run(repo, "validate", "--target", "staging", expect=1)
    assert "must be a JSON object" in r.stdout
    assert "Traceback" not in r.stderr, "should not crash"


@test
def test_half_written_staging_is_not_silently_wiped(td: Path):
    """Auto-init used to overwrite a partial staging tree, destroying
    unpublished edits with no snapshot and no undo."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    precious = {"version": "1.0.0", "updated_at": "2026-07-19T00:00:00Z",
                "items": [news_item(title="hours of unpublished work")]}
    write_json(repo / "staging" / "news" / "feed.json", precious)
    (repo / "staging" / "seed" / "manifest.json").unlink()
    r = run(repo, "validate", "--target", "staging")
    assert r.returncode != 0, "must refuse rather than overwrite"
    assert json.loads((repo / "staging" / "news" / "feed.json").read_text()) == precious, \
        "unpublished staging edits were destroyed"


@test
def test_undo_to_invalid_data_does_not_offer_to_push(td: Path):
    """Undo used to print 'push it live' even after declaring the restored
    data invalid."""
    repo = build_repo(td)
    run(repo, "init", expect=0)
    write_json(repo / "staging" / "seed" / "cards.json",
               [card("card_a"), card("card_b"), card("card_c")])
    run(repo, "publish", "--yes", "--allow-warnings", expect=0)
    # corrupt the snapshot so the restore is invalid
    snaps = sorted((repo / ".published").glob("2*"))
    assert snaps
    bad = snaps[0] / "news" / "feed.json"
    item = news_item()
    item["expires_at"] = item.pop("expiry_date")
    write_json(bad, {"version": "1.0.0", "updated_at": "x", "items": [item]})
    r = run(repo, "undo", "--yes")
    assert r.returncode != 0, "must fail when the restored data is invalid"
    assert "git" not in r.stdout.split("does NOT validate")[-1], \
        "must not print push instructions for invalid data"


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
