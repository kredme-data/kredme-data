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
def test_optional_seed_file_is_copied_and_declared(td: Path):
    """card_details.json publishes without an app release — but ONLY if promote
    both copies it AND declares it. Either half alone is a broken sync."""
    repo = build_repo(td, seed_version="5.1.0")
    commit_to_dev(repo, lambda r: write_json(
        r / "seed" / "card_details.json", {"card_a": {"highlights": ["5% back"]}}))
    run(repo, "promote", "--yes", "--allow-warnings", expect=0)

    live = repo / "seed" / "card_details.json"
    assert live.exists(), "promote did not copy the optional file to prod"
    m = json.loads((repo / "seed" / "manifest.json").read_text())
    entry = next((f for f in m["files"] if f["name"] == "card_details.json"), None)
    assert entry is not None, "promote copied the file but never declared it"
    assert entry["path"] == "seed/card_details.json", entry
    # A declared checksum that does not match the served bytes is exactly what
    # makes the app reject the whole sync.
    assert entry["checksum"] == hashlib.sha256(live.read_bytes()).hexdigest()
    assert entry["size_bytes"] == len(live.read_bytes())


@test
def test_optional_file_alone_counts_as_a_seed_change(td: Path):
    """Before this, seed_changed only looked at cards/merchants, so a release
    that touched ONLY card_details.json reported 'nothing to promote'."""
    repo = build_repo(td)
    commit_to_dev(repo, lambda r: write_json(
        r / "seed" / "card_details.json", {"card_a": {"highlights": ["x"]}}))
    r = run(repo, "promote", "--yes", "--allow-warnings", expect=0)
    assert "nothing to promote" not in r.stdout.lower(), r.stdout


@test
def test_absent_optional_file_is_not_an_error(td: Path):
    """383 cards shipped for months with no card_details.json at all. Absence
    is the normal state, not a defect."""
    repo = build_repo(td)
    assert not (repo / "seed" / "card_details.json").exists()
    r = run(repo, "validate", "--target", "dev", expect=0)
    assert "card_details" not in r.stdout, r.stdout


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
def test_manifest_declaring_a_missing_optional_file_is_fatal(td: Path):
    """The one that stops card syncing for EVERY user: _applyFullSync returns
    false on any non-200 and aborts before saving the version, so the app
    retries the same failing sync on every cold start, forever."""
    def mutate(r):
        m = json.loads((r / "seed" / "manifest.json").read_text())
        m["files"].append({"name": "card_details.json", "path": "seed/card_details.json",
                           "checksum": "0" * 64, "size_bytes": 1})
        write_json(r / "seed" / "manifest.json", m)   # file itself never written
    out = _dev_validate(td, mutate).stdout
    assert "declared in manifest but file is missing" in out, out


@test
def test_present_but_undeclared_optional_file_warns(td: Path):
    """Not an error — publishing is a deliberate step — but it reaches nobody,
    so it must not pass silently."""
    # A warning, not an error — publishing is a deliberate step, so validate
    # still exits 0. The point is that it does not pass SILENTLY.
    out = _dev_validate(td, lambda r: write_json(
        r / "seed" / "card_details.json", {"card_a": {}}), expect=0).stdout
    assert "does not declare it" in out, out


@test
def test_optional_file_shape_is_checked(td: Path):
    """The app's loader swallows a parse failure and caches an empty map, so a
    broken file is a silently blank tab. Nothing downstream ever complains."""
    out = _dev_validate(td, lambda r: (r / "seed" / "card_details.json")
                        .write_text("[]", encoding="utf-8")).stdout
    assert "root must be an object" in out, out

    # An unresolvable key is only a warning: the content is dead, but nothing
    # a user sees is wrong, so it must not block a publish.
    out2 = _dev_validate(td, lambda r: write_json(
        r / "seed" / "card_details.json", {"card_a": {}, "no_such_card": {}}),
        expect=0).stdout
    assert "match no card" in out2, out2


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


# --- UNIT tests for the card-economics gate ---------------------------------
# The validator proved JSON shape for 1,102 lines and never once read a number,
# which is how 6 cards shipped rendering 20-75% cashback and 106 shipped
# rendering 0.00%. These lock in the maths and the ratchet.
#
# The percentages below are the ones the APP computes. If a test here starts
# failing after an app change, the gate — not the test — is what is now wrong.

def _c(cid, brr, rp=None, rules=None, excl=None):
    return {"card": {"id": cid, "base_reward_rate": brr, "rp_value_standard": rp},
            "reward_rules": rules or [], "exclusion_rules": excl or []}


def _gate(cards, baseline):
    """Run validate_economics against an injected baseline. Returns the Report."""
    import contextlib, io
    real = K.read_rate_baseline
    K.read_rate_baseline = lambda: baseline
    try:
        rep = K.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            K.validate_economics(cards, rep)
        return rep
    finally:
        K.read_rate_baseline = real


# A baseline describing a clean 2-card catalog, so any defect reads as NEW.
_CLEAN_BASE = {"card_count": 2, "zero_rate_cards": 0, "over_ceiling_cards": [],
               "over_ceiling_rules": [], "under_floor_cards": [],
               "prose_excluded_high_rate": [], "self_contradicting_rules": []}


@unit
def test_base_pct_matches_the_app_formula():
    # credit_card.dart:489 — baseRewardRate * sanePointValue(rp) * 100
    assert K.card_base_pct({"base_reward_rate": 0.01, "rp_value_standard": 1.0}) == 1.0
    # rp_value_standard null -> fromOtaJson's `?? 0.25`
    assert K.card_base_pct({"base_reward_rate": 0.04, "rp_value_standard": None}) == 1.0
    # sanePointValue collapses out-of-range values to 0.25, it does not clamp
    assert K.card_base_pct({"base_reward_rate": 0.04, "rp_value_standard": 9.0}) == 1.0
    assert K.card_base_pct({"base_reward_rate": 0.04, "rp_value_standard": 0}) == 1.0
    # the real Axis Bank Cashback defect: 0.75 read as a fraction
    assert K.card_base_pct({"base_reward_rate": 0.75, "rp_value_standard": None}) == 18.75


@unit
def test_rule_pct_per_reward_type():
    inner = {"base_reward_rate": 0.02, "rp_value_standard": 0.25}
    base = K.card_base_pct(inner)
    # cashback_pct: the rate IS the fraction
    assert K.rule_pct({"reward_type": "cashback_pct", "reward_rate": 0.05}, inner, base) == 5.0
    # points_per_spend: (rate / unit) * point_value * 100
    assert abs(K.rule_pct({"reward_type": "points_per_spend", "reward_rate": 4,
                           "reward_unit_spend": 150}, inner, base) - 0.6667) < 0.001
    # unit of 0 must fall back to the base rate, not divide by zero
    assert K.rule_pct({"reward_type": "points_per_spend", "reward_rate": 4,
                       "reward_unit_spend": 0}, inner, base) == base
    # multiplier is N x the base earn, not N absolute points
    assert K.rule_pct({"reward_type": "multiplier", "reward_rate": 5}, inner, base) == 2.5
    # an unknown type falls back to base rather than silently scoring 0
    assert K.rule_pct({"reward_type": "cashback_flat", "reward_rate": 500}, inner, base) == base


@unit
def test_new_rate_above_ceiling_is_fatal():
    rep = _gate([_c("ok_card", 0.01, 1.0), _c("bad_card", 0.75, 1.0)], _CLEAN_BASE)
    assert rep.failed, "a card rendering 75% must block the publish"
    assert "bad_card" in rep.errors[0]


@unit
def test_baselined_rate_does_not_block():
    # 15% is over the ratcheted ceiling but under the unwaivable one, so the
    # ratchet is what is under test here. (A baselined 75% is a different case
    # and must still block — see test_hard_ceiling_cannot_be_baselined.)
    base = dict(_CLEAN_BASE, over_ceiling_cards=["bad_card"],
                prose_excluded_high_rate=[], card_count=2)
    rep = _gate([_c("ok_card", 0.01, 1.0), _c("bad_card", 0.15, 1.0)], base)
    assert not rep.failed, "a known defect must not block unrelated publishes"


@unit
def test_hard_ceiling_cannot_be_baselined():
    """The one check with no escape hatch.

    Every other check is ratcheted, which is why 58 known-bad rules were written
    into rate_baseline.json on 7 Aug and stopped blocking anything. A rate above
    30% is never a card, always a bug, so listing it must not buy a pass.
    """
    base = dict(_CLEAN_BASE, over_ceiling_cards=["bad_card"],
                over_ceiling_rules=["bad_card::<base rate>"], card_count=2)
    rep = _gate([_c("ok_card", 0.01, 1.0), _c("bad_card", 0.75, 1.0)], base)
    assert rep.failed, "75% must block even when it is in the baseline"
    assert any("not waivable" in e for e in rep.errors), rep.errors


@unit
def test_rule_contradicting_its_own_text_is_fatal():
    """IndianOil Kotak, exactly as it ships today.

    The rule name states 24 points per Rs.150. At this card's Rs.0.20 point
    value that is 3.2%. The stored 0.24 renders 24%. No issuer page needed.
    """
    rule = {"rule_name": "4% back as reward points on fuel spends. 24 reward points "
                         "on every Rs. 150 spent on fuel at IndianOil outlets",
            "reward_type": "cashback_pct", "reward_rate": 0.24}
    rep = _gate([_c("ok_card", 0.01, 1.0), _c("indianoil", 0.02, 0.2, rules=[rule])],
                _CLEAN_BASE)
    assert rep.failed, "a rule contradicting its own text must block"
    assert any("contradicts" in e for e in rep.errors), rep.errors


@unit
def test_corrected_rule_passes():
    """The same rule after the fix: 0.24 -> 0.032 renders 3.2%, matching its text."""
    rule = {"rule_name": "4% back as reward points on fuel spends. 24 reward points "
                         "on every Rs. 150 spent on fuel at IndianOil outlets",
            "reward_type": "cashback_pct", "reward_rate": 0.032}
    rep = _gate([_c("ok_card", 0.01, 1.0), _c("indianoil", 0.02, 0.2, rules=[rule])],
                _CLEAN_BASE)
    assert not rep.failed, f"the corrected rate must pass: {rep.errors}"


@unit
def test_unparseable_prose_is_skipped_never_guessed():
    """Silence is the safe failure mode: a false accusation blocks a publish."""
    for name in ("Base reward rate", "Welcome benefit", "Lounge access", ""):
        rule = {"rule_name": name, "reward_type": "cashback_pct", "reward_rate": 0.05}
        rep = _gate([_c("ok_card", 0.01, 1.0), _c("x", 0.01, 1.0, rules=[rule])],
                    _CLEAN_BASE)
        assert not rep.failed, f"{name!r} states no claim and must not be flagged"


@unit
def test_claimed_pct_reads_the_issuer_units():
    inner = {"base_reward_rate": 0.02, "rp_value_standard": 0.2}
    pct, how = K.claimed_pct({"rule_name": "24 reward points on every Rs. 150 spent"}, inner)
    assert abs(pct - 3.2) < 1e-9, pct
    assert "24 pts per Rs.150" in how
    # an issuer-stated effective rate wins over a points count in the same sentence
    pct, _ = K.claimed_pct({"rule_name": "5X points on every ₹150, reward rate of 8.6%"}, inner)
    assert abs(pct - 8.6) < 1e-9, pct
    # a plain cashback percentage
    pct, _ = K.claimed_pct({"rule_name": "5% cashback on movie tickets"}, inner)
    assert abs(pct - 5.0) < 1e-9, pct
    # nothing parseable -> None, never a guess
    assert K.claimed_pct({"rule_name": "Milestone benefit"}, inner)[0] is None


@unit
def test_new_rate_below_floor_is_fatal():
    # Axis Neo's real defect: point value applied twice -> 0.02%
    rep = _gate([_c("ok_card", 0.01, 1.0), _c("neo", 0.001, 0.2)], _CLEAN_BASE)
    assert rep.failed, "0.02% is a unit bug in the other direction"


@unit
def test_zero_rate_count_may_not_grow():
    base = dict(_CLEAN_BASE, zero_rate_cards=0, card_count=2)
    rep = _gate([_c("a", 0.01, 1.0), _c("b", 0.0, 1.0)], base)
    assert rep.failed, "a card newly rendering 0.00% must be caught"
    assert any("0.00%" in e for e in rep.errors)


@unit
def test_losing_even_one_card_is_fatal():
    # The pre-existing shrink guard tolerated a 20% drop; one lost card is
    # still a card that vanishes from a real wallet.
    base = dict(_CLEAN_BASE, card_count=3)
    rep = _gate([_c("a", 0.01, 1.0), _c("b", 0.01, 1.0)], base)
    assert rep.failed and any("card count fell" in e for e in rep.errors)


@unit
def test_high_rate_on_a_prose_excluded_category_is_fatal():
    """The 'Axis Cashback shows 75% at Indian Oil' defect. The exclusion is
    free text (`other`), which the engine never reads, so only the gate can
    see it."""
    cards = [_c("a", 0.01, 1.0),
             _c("pump", 0.20, 1.0,
                excl=[{"exclusion_type": "other", "exclusion_value": "fuel purchases"}])]
    # Baseline the ceiling violation so ONLY the prose finding can fail here —
    # otherwise this test would pass on the wrong error.
    base = dict(_CLEAN_BASE, over_ceiling_cards=["pump"])
    rep = _gate(cards, base)
    assert rep.failed, "a rate advertised on an excluded category must block"
    assert len(rep.errors) == 1, f"expected only the prose error: {rep.errors}"
    assert "exclusion text excludes" in rep.errors[0], rep.errors[0]
    # And once it is known, it must stop blocking unrelated work.
    rep2 = _gate(cards, dict(base, prose_excluded_high_rate=["pump::fuel"]))
    assert not rep2.failed, f"a baselined prose finding must not block: {rep2.errors}"


@unit
def test_ordinary_cards_pass_cleanly():
    rep = _gate([_c("cashback_1p5", 0.015, 1.0),
                 _c("points_card", 0.0067, 0.25,
                    rules=[{"reward_type": "points_per_spend", "reward_rate": 4,
                            "reward_unit_spend": 150, "rule_name": "Base"}])],
                _CLEAN_BASE)
    assert not rep.failed, f"a normal catalog must pass: {rep.errors}"


@unit
def test_baseline_identity_keys_are_stable():
    """The growth guard and the writer must derive identity the same way, or
    the ratchet silently stops ratcheting."""
    scan = K.scan_economics([_c("x", 0.75, 1.0,
                                excl=[{"exclusion_type": "other",
                                       "exclusion_value": "rent"}])])
    payload = K.baseline_payload(scan, "test")
    for key, ident in K.BASELINE_LISTS.items():
        assert payload[key] == sorted(ident(scan)), f"{key} drifted between writer and guard"


# --- RULE INTEGRITY ---------------------------------------------------------
# Shape defects that never reach a number, so validate_economics cannot see
# them. Each one fails silently in the app: no exception, no log.

def _r(name, **kw):
    r = {"rule_name": name, "rule_type": "category_bonus", "reward_type": "cashback_pct",
         "reward_rate": 0.05, "reward_unit_spend": None, "cap_amount": None,
         "cap_period": None, "priority": 50}
    r.update(kw)
    return r


def _igate(cards, baseline):
    """Run validate_rule_integrity against an injected baseline."""
    import contextlib, io
    real = K.read_rate_baseline
    K.read_rate_baseline = lambda: baseline
    try:
        rep = K.Report()
        with contextlib.redirect_stdout(io.StringIO()):
            K.validate_rule_integrity(cards, rep)
        return rep
    finally:
        K.read_rate_baseline = real


_CLEAN_INTEG = {"non_numeric_caps": [], "duplicate_rule_names": [], "cap_without_period": []}


@unit
def test_non_numeric_cap_is_fatal():
    """double.tryParse('12000 RPs per cycle') returns null, and a null cap is
    NO cap — the rule pays its accelerated rate for ever."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount="12000 RPs per statement cycle",
                                          cap_period="cycle")])]
    assert _igate(cards, _CLEAN_INTEG).failed, "a string cap_amount must fail"
    # A dict is the other live shape, and _numOf returns null for it too.
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount={"monthly": 2500}, cap_period="month")])]
    assert _igate(cards, _CLEAN_INTEG).failed, "an object cap_amount must fail"


@unit
def test_numeric_cap_passes():
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount=800, cap_period="month")])]
    assert not _igate(cards, _CLEAN_INTEG).failed


@unit
def test_duplicate_rule_name_is_fatal():
    """`${cardId}|${ruleName}` is the reward-rule primary key AND the cap bucket
    key, so two rules sharing a name on one card lose one of them."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("same name"), _r("same name")])]
    assert _igate(cards, _CLEAN_INTEG).failed, "a duplicate rule_name must fail"


@unit
def test_same_rule_name_on_different_cards_is_fine():
    """The key is scoped per card. 'Base reward rate' is on all 376."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("Base reward rate")]),
             _c("y", 0.01, 1.0, rules=[_r("Base reward rate")])]
    assert not _igate(cards, _CLEAN_INTEG).failed


@unit
def test_duplicates_are_detected_on_the_full_name():
    """_rule_key truncates to 80 chars for baseline readability. Detecting on
    the truncated form reported 67 collisions on dev where 24 were real."""
    stem = "x" * 78
    cards = [_c("x", 0.01, 1.0, rules=[_r(stem + "AAAA"), _r(stem + "BBBB")])]
    assert not _igate(cards, _CLEAN_INTEG).failed, \
        "two rules that differ only after char 80 are distinct to the app"


@unit
def test_cap_without_period_is_fatal():
    """_checkCap returns null unless BOTH are set, so the cap never applies."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount=1200)])]
    assert _igate(cards, _CLEAN_INTEG).failed


@unit
def test_baselined_integrity_defect_does_not_block():
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount=1200)])]
    base = dict(_CLEAN_INTEG, cap_without_period=["x::a"])
    assert not _igate(cards, base).failed, "known debt must not block a publish"


@unit
def test_integrity_fails_closed_when_the_baseline_is_removed():
    """Every list-shaped ratchet in this file fails closed; the two scalar ones
    fail open. A removed integrity baseline must behave like the lists."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount=1200)])]
    assert _igate(cards, None).failed, "no baseline at all must fail, not skip"
    assert _igate(cards, {}).failed, "a baseline missing the key must fail, not skip"


@unit
def test_points_rule_without_unit_cannot_be_baselined():
    """Unwaivable, like over_hard_ceiling: the app would silently read the rule
    as 'per ₹100'. Clean today (284/284) and it must stay that way."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", reward_type="points_per_spend",
                                          reward_rate=5.0, reward_unit_spend=None)])]
    stuffed = dict(_CLEAN_INTEG, points_rule_no_unit=["x::a"])
    assert _igate(cards, stuffed).failed, "there must be no waiver for this"


@unit
def test_unknown_cap_period_cannot_be_baselined():
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount=100, cap_period="fortnight")])]
    stuffed = dict(_CLEAN_INTEG, unknown_cap_period=["x::a"])
    assert _igate(cards, stuffed).failed, "there must be no waiver for this"


@unit
def test_integrity_identity_keys_are_stable():
    """Same contract as the economics baseline: writer and guard must derive
    identity identically, or the ratchet stops ratcheting."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("a", cap_amount="800 pts"), _r("a", cap_amount=5)])]
    integ = K.scan_rule_integrity(cards)
    econ = K.scan_economics(cards)
    payload = K.baseline_payload(econ, "test", integ)
    for key, ident in K.INTEGRITY_LISTS.items():
        assert payload[key] == sorted(ident(integ)), f"{key} drifted between writer and guard"


@unit
def test_baseline_writer_never_blanks_integrity_by_omission():
    """A caller that cannot measure integrity must not silently erase those
    keys — that would hand the next publish a free pass on all three."""
    econ = K.scan_economics([_c("x", 0.01, 1.0)])
    payload = K.baseline_payload(econ, "test")
    for key in K.INTEGRITY_LISTS:
        assert key not in payload, f"{key} must be absent, not empty, when unmeasured"



@unit
def test_a_pooled_brand_cap_may_share_one_rule_name():
    """An issuer that pools ONE cap across a brand list ("5% on Amazon,
    Flipkart, Myntra, Ajio, Uber, Swiggy, Zomato — capping of 500 reward
    points per calendar month") is expressed as one row per merchant_ref, all
    carrying the SAME rule_name deliberately: the cap bucket key is
    `ruleName|periodKey`. Distinct names would give one 500-point cap PER
    BRAND. The engine indexes merchant rules by merchant_ref, so all of them
    still fire."""
    cards = [_c("x", 0.01, 1.0, rules=[
        _r("5% on select online brands", rule_type="merchant_specific", merchant_ref="amazon"),
        _r("5% on select online brands", rule_type="merchant_specific", merchant_ref="uber"),
        _r("5% on select online brands", rule_type="merchant_specific", merchant_ref="swiggy"),
    ])]
    assert K.scan_rule_integrity(cards)["duplicate_rule_names"] == [], \
        "a pooled brand cap is the intended construct, not a collision"


@unit
def test_the_same_name_on_the_same_merchant_is_still_a_collision():
    """Two rows on one merchant_ref genuinely are ambiguous — nothing decides
    which the user gets."""
    cards = [_c("x", 0.01, 1.0, rules=[
        _r("5% on select online brands", rule_type="merchant_specific", merchant_ref="amazon"),
        _r("5% on select online brands", rule_type="merchant_specific", merchant_ref="amazon"),
    ])]
    assert K.scan_rule_integrity(cards)["duplicate_rule_names"], "same merchant twice must fail"


@unit
def test_the_same_name_with_different_maths_is_still_a_collision():
    """Sharing a cap bucket is fine; disagreeing about the RATE inside it is
    not — the number the user is shown becomes undefined."""
    cards = [_c("x", 0.01, 1.0, rules=[
        _r("5% on select online brands", rule_type="merchant_specific",
           merchant_ref="amazon", reward_rate=0.05),
        _r("5% on select online brands", rule_type="merchant_specific",
           merchant_ref="uber", reward_rate=0.02),
    ])]
    assert K.scan_rule_integrity(cards)["duplicate_rule_names"], "differing rates must fail"


@unit
def test_a_repeated_name_with_no_merchant_is_still_a_collision():
    """The narrowing is only for merchant-scoped rows. Two category rules
    sharing a name still collide, which is the case the check was written for."""
    cards = [_c("x", 0.01, 1.0, rules=[_r("Base reward rate"), _r("Base reward rate")])]
    assert K.scan_rule_integrity(cards)["duplicate_rule_names"], "category dupes must still fail"


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
