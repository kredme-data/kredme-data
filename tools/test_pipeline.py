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

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("kredme_mod", TOOL)
K = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(K)


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
    """A real git repo with `main` (prod) and `dev` branches, both with data."""
    repo = tmp
    (repo / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(TOOL, repo / "tools" / "kredme.py")
    write_data(repo, cards=cards, items=items,
               seed_version=seed_version, news_version=news_version)
    g(repo, "init", "-q")
    g(repo, "checkout", "-q", "-B", "main")
    g(repo, "add", "-A")
    g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "data")
    g(repo, "branch", "-f", "dev", "main")
    return repo


def write_data(repo: Path, *, cards=("card_a", "card_b"), items=None,
               seed_version="1.0.0", news_version="1.0.0") -> None:
    seed = repo / "seed"
    write_json(seed / "cards.json", [card(c) for c in cards])
    write_json(seed / "merchants.json", {
        "_metadata": {}, "categories": [{"id": "dining", "name": "Dining"}],
        "merchants": [{"id": 1, "merchant_name": "test_m", "category_id": "dining"}],
    })
    manifest = {"version": seed_version, "updated_at": "2026-07-19T00:00:00Z",
                "min_app_version": "1.0.0", "files": [], "delta_file": None,
                "news_version": news_version}
    for name in ("cards.json", "merchants.json"):
        b = (seed / name).read_bytes()
        manifest["files"].append({"name": name, "path": f"seed/{name}",
                                  "checksum": hashlib.sha256(b).hexdigest(),
                                  "size_bytes": len(b)})
    write_json(seed / "manifest.json", manifest)
    write_json(repo / "news" / "feed.json", {
        "version": news_version, "updated_at": "2026-07-19T00:00:00Z",
        "items": items if items is not None else [news_item()]})


def g(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


def rehash(repo: Path) -> None:
    """Recompute manifest checksums after editing a seed file."""
    seed = repo / "seed"
    m = json.loads((seed / "manifest.json").read_text())
    for f in m["files"]:
        b = (seed / f["name"]).read_bytes()
        f["checksum"] = hashlib.sha256(b).hexdigest()
        f["size_bytes"] = len(b)
    write_json(seed / "manifest.json", m)


def commit_to_dev(repo: Path, mutate, msg="dev change") -> None:
    """Apply `mutate(repo)` on the dev branch and commit it."""
    g(repo, "checkout", "-q", "dev")
    mutate(repo)
    g(repo, "add", "-A", "seed", "news")
    g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)
    g(repo, "checkout", "-q", "main")


def run(repo: Path, *args, expect=None):
    r = subprocess.run(
        [sys.executable, str(repo / "tools" / "kredme.py"), *args],
        capture_output=True, text=True, timeout=180,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    if expect is not None and r.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}, got {r.returncode}\n"
            f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}")
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


# --- environments -----------------------------------------------------------

@test
def test_clean_dev_validates(td: Path):
    repo = build_repo(td)
    run(repo, "validate", "--target", "dev", expect=0)
    run(repo, "validate", "--target", "prod", expect=0)


@test
def test_dev_branch_is_read_byte_exact(td: Path):
    """REGRESSION: reading a branch via `git show` with .strip() mangled the
    bytes, so every checksum comparison failed with a phantom mismatch."""
    repo = build_repo(td, cards=tuple(f"card_{i}" for i in range(30)))
    r = run(repo, "validate", "--target", "dev", expect=0)
    assert "checksum + size match" in r.stdout
    assert "mismatch" not in r.stdout


@test
def test_dev_edits_do_not_touch_prod(td: Path):
    """The whole point of two lanes: dev moving must not change prod."""
    repo = build_repo(td, seed_version="5.1.0")
    commit_to_dev(repo, lambda r: (
        write_json(r / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]),
        rehash(r)))
    assert ver(repo, "seed") == "5.1.0", "prod version changed when only dev moved"
    run(repo, "validate", "--target", "prod", expect=0)


@test
def test_status_detects_dev_ahead(td: Path):
    repo = build_repo(td)
    r = run(repo, "status", expect=0)
    assert "no — dev and prod data are the same" in r.stdout
    commit_to_dev(repo, lambda r2: (
        write_json(r2 / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]),
        rehash(r2)))
    r = run(repo, "status", expect=0)
    assert "not yet in prod" in r.stdout


# --- promote gate -----------------------------------------------------------

@test
def test_promote_refuses_invalid_dev(td: Path):
    """Bad data on dev must never reach prod."""
    repo = build_repo(td)
    bad = news_item()
    bad["expires_at"] = bad.pop("expiry_date")
    commit_to_dev(repo, lambda r: write_json(
        r / "news" / "feed.json",
        {"version": "1.0.0", "updated_at": "x", "items": [bad]}))
    run(repo, "promote", "--yes", expect=1)
    assert ver(repo, "news") == "1.0.0", "prod changed after a refused promote"


@test
def test_promote_bumps_news_major_and_seed_patch(td: Path):
    repo = build_repo(td, seed_version="5.1.0", news_version="1.0.0")
    commit_to_dev(repo, lambda r: (
        write_json(r / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]),
        rehash(r),
        write_json(r / "news" / "feed.json",
                   {"version": "1.0.0", "updated_at": "x",
                    "items": [news_item(), news_item("news_002")]})))
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    assert ver(repo, "seed") == "5.1.1", ver(repo, "seed")
    assert ver(repo, "news") == "2.0.0", ver(repo, "news")


@test
def test_promote_noop_when_identical(td: Path):
    repo = build_repo(td)
    r = run(repo, "promote", "--yes", expect=0)
    assert "nothing to promote" in r.stdout.lower()


@test
def test_promote_dry_run_writes_nothing(td: Path):
    repo = build_repo(td)
    commit_to_dev(repo, lambda r: (
        write_json(r / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]),
        rehash(r)))
    before = (repo / "seed" / "cards.json").read_bytes()
    run(repo, "promote", "--dry-run", "--allow-warnings", expect=0)
    assert (repo / "seed" / "cards.json").read_bytes() == before
    assert not (repo / ".published").exists()


@test
def test_promote_must_run_from_prod_branch(td: Path):
    repo = build_repo(td)
    g(repo, "checkout", "-q", "dev")
    r = run(repo, "promote", "--yes")
    assert r.returncode != 0
    assert "must run from" in r.stdout


@test
def test_promote_refuses_with_dirty_prod_tree(td: Path):
    repo = build_repo(td)
    commit_to_dev(repo, lambda r: (
        write_json(r / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]),
        rehash(r)))
    (repo / "news" / "feed.json").write_text('{"version":"9.9.9","items":[]}', encoding="utf-8")
    r = run(repo, "promote", "--yes")
    assert r.returncode != 0
    assert "uncommitted" in r.stdout


# --- undo + version safety --------------------------------------------------

@test
def test_undo_restores_prod(td: Path):
    repo = build_repo(td, seed_version="5.1.0")
    original = (repo / "seed" / "cards.json").read_bytes()
    commit_to_dev(repo, lambda r: (
        write_json(r / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]),
        rehash(r)))
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    assert ver(repo, "seed") == "5.1.1"
    run(repo, "undo", "--yes", expect=0)
    assert ver(repo, "seed") == "5.1.0"
    assert (repo / "seed" / "cards.json").read_bytes() == original


@test
def test_repromote_after_undo_never_reuses_a_version(td: Path):
    """CRITICAL: a correction pushed after undo must not re-use a version
    users already hold, or it reaches nobody."""
    repo = build_repo(td, news_version="3.0.0")
    commit_to_dev(repo, lambda r: write_json(
        r / "news" / "feed.json",
        {"version": "3.0.0", "updated_at": "x", "items": [news_item(title="WRONG")]}))
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    served = ver(repo, "news")
    run(repo, "undo", "--yes", expect=0)
    commit_to_dev(repo, lambda r: write_json(
        r / "news" / "feed.json",
        {"version": "3.0.0", "updated_at": "x", "items": [news_item(title="CORRECTED")]}),
        msg="fix")
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    assert K.version_gt(ver(repo, "news"), served), (
        f"re-promoted {ver(repo,'news')} but users already hold {served}")


@test
def test_corrupt_prod_feed_refuses_promote(td: Path):
    """CRITICAL: must not silently emit a LOWER news version."""
    repo = build_repo(td, news_version="7.0.0")
    commit_to_dev(repo, lambda r: write_json(
        r / "news" / "feed.json",
        {"version": "7.0.0", "updated_at": "x", "items": [news_item(), news_item("n2")]}))
    (repo / "news" / "feed.json").write_text("{ not json", encoding="utf-8")
    g(repo, "add", "-A"); g(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-q", "-m", "corrupt prod")
    r = run(repo, "promote", "--yes", "--allow-warnings")
    assert r.returncode != 0
    assert "1.0.0" not in (repo / "news" / "feed.json").read_text()


@test
def test_v_prefixed_prod_version_bumps_correctly(td: Path):
    repo = build_repo(td, news_version="v7.0.0")
    commit_to_dev(repo, lambda r: write_json(
        r / "news" / "feed.json",
        {"version": "v7.0.0", "updated_at": "x", "items": [news_item(), news_item("n2")]}))
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    assert ver(repo, "news") == "8.0.0", ver(repo, "news")


# --- validator (run against dev) --------------------------------------------

def _dev_validate(td: Path, mutate, expect=1):
    repo = build_repo(td)
    commit_to_dev(repo, mutate)
    return run(repo, "validate", "--target", "dev", expect=expect)


@test
def test_wrong_news_keys_rejected(td: Path):
    def m(r):
        bad = news_item(); bad["expires_at"] = bad.pop("expiry_date"); bad["url"] = bad.pop("source_url")
        write_json(r / "news" / "feed.json", {"version": "1.0.0", "updated_at": "x", "items": [bad]})
    out = _dev_validate(td, m).stdout
    assert "expiry_date" in out and "source_url" in out


@test
def test_unknown_affected_card_rejected(td: Path):
    out = _dev_validate(td, lambda r: write_json(
        r / "news" / "feed.json",
        {"version": "1.0.0", "updated_at": "x",
         "items": [news_item(affected_cards=["card_a", "card_TYPO"])]})).stdout
    assert "card_TYPO" in out and "NOBODY" in out


@test
def test_empty_merchants_rejected(td: Path):
    out = _dev_validate(td, lambda r: (write_json(
        r / "seed" / "merchants.json",
        {"_metadata": {}, "categories": [{"id": "dining"}], "merchants": []}), rehash(r))).stdout
    assert "no merchants" in out.lower()


@test
def test_dangling_category_rejected(td: Path):
    out = _dev_validate(td, lambda r: (write_json(
        r / "seed" / "merchants.json",
        {"_metadata": {}, "categories": [{"id": "dining"}],
         "merchants": [{"id": 1, "merchant_name": "test_m", "category_id": "diningg"}]}),
        rehash(r))).stdout
    assert "missing category" in out


@test
def test_non_dict_cards_rejected(td: Path):
    out = _dev_validate(td, lambda r: (write_json(
        r / "seed" / "cards.json", [card("card_a"), "oops", None]), rehash(r))).stdout
    assert "not objects" in out


@test
def test_array_shaped_feed_errors_cleanly(td: Path):
    r = _dev_validate(td, lambda rp: write_json(rp / "news" / "feed.json", [news_item()]))
    assert "must be a JSON object" in r.stdout
    assert "Traceback" not in r.stderr


@test
def test_catalog_shrink_rejected(td: Path):
    repo = build_repo(td, cards=tuple(f"card_{i}" for i in range(100)))
    commit_to_dev(repo, lambda r: (write_json(
        r / "seed" / "cards.json", [card("card_0"), card("card_1")]), rehash(r)))
    r1 = run(repo, "validate", "--target", "dev", expect=1)
    assert "disappear" in r1.stdout
    run(repo, "validate", "--target", "dev", "--allow-shrink", expect=0)


@test
def test_prod_checksum_mismatch_is_fatal(td: Path):
    repo = build_repo(td)
    write_json(repo / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")])
    g(repo, "add", "-A"); g(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-q", "-m", "break checksum")
    r = run(repo, "validate", "--target", "prod", expect=1)
    assert "REJECT" in r.stdout or "mismatch" in r.stdout


# --- UNIT tests for the pure helpers ----------------------------------------
# These were the coverage gap: every test above drives the whole CLI, so a bug
# in version handling only surfaced if it changed a visible outcome. An audit
# found exactly that class of bug (a live version of "v7.0.0" silently
# collapsing to "1.0.0" and permanently stopping news refetch).



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



# --- REGRESSION (branch model) ----------------------------------------------

@test
def test_undo_to_invalid_data_does_not_offer_to_push(td: Path):
    """Undo used to print 'push it live' even after declaring the restored
    data invalid."""
    repo = build_repo(td)
    commit_to_dev(repo, lambda r: (write_json(
        r / "seed" / "cards.json", [card("card_a"), card("card_b"), card("card_c")]), rehash(r)))
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    snaps = sorted((repo / ".published").glob("2*"))
    assert snaps
    item = news_item(); item["expires_at"] = item.pop("expiry_date")
    write_json(snaps[0] / "news" / "feed.json",
               {"version": "1.0.0", "updated_at": "x", "items": [item]})
    r = run(repo, "undo", "--yes")
    assert r.returncode != 0, "must fail when the restored data is invalid"
    assert "git" not in r.stdout.split("does NOT validate")[-1]


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
