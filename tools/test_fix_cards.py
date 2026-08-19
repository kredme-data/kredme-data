#!/usr/bin/env python3
"""
test_fix_cards.py — the self-tests for the fix runner.

Stdlib unittest, no third-party packages, no network. Run it directly:

    python3 tools/test_fix_cards.py
    python3 tools/test_fix_cards.py -v
    python3 tools/test_fix_cards.py TestRuleNameIsUntouchable

WHAT THESE TESTS ARE FOR
------------------------
A tool that edits 1.78 MB of card data on behalf of a non-technical founder is
only as trustworthy as the guarantees it can DEMONSTRATE. Each class below pins
one guarantee, and nearly every one of them is a scar:

  TestFixersArePure          a fixer that mutates ctx has already broken the
                             promise that a dry run is a dry run
  TestRuleNameIsUntouchable  renaming a rule wipes every user's saved cap
                             progress, because the app keys the spend bucket on
                             the name string
  TestDryRunWritesNothing    checksummed before and after — the default path
                             must not be able to touch a byte
  TestApplyOnAFixture        the edit that lands is EXACTLY the edit that was
                             planned, and nothing else moves
  TestIdempotency            a fix that must run twice is a fix nobody can
                             reason about
  TestManifest               a stale checksum is what the app shows a user as
                             "Sync failed"
  TestBackup                 taken BEFORE the first write, not after the first
                             success
  TestCrashIsolation         a fixer that raises must cost its own edits and
                             nothing else
  TestSerialisation          seed/cards.json is indent=1; writing it at indent=2
                             re-indents 131,664 lines and the PR becomes
                             unreviewable

The real fixers are exercised against the REAL catalogue where the guarantee is
about them (purity, rule_name), and stub fixers are used where the guarantee is
about the RUNNER (ordering, anchors, gating, writing), because a stub can state
the expected edit exactly and a real one cannot.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

os.environ.setdefault("NO_COLOR", "1")

import fix_cards as F                                            # noqa: E402
import validate_cards as V                                       # noqa: E402
from fixers.base import CERTAIN, LIKELY, Edit                    # noqa: E402

APP_ROOT = os.environ.get("KREDME_APP_ROOT")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def sample_card(card_id="t_card", **over):
    """One card entry in the exact shape seed/cards.json uses."""
    inner = {
        "id": card_id, "card_name": "Test Card", "issuer": "Test Bank",
        "network": "visa", "card_tier": "entry", "annual_fee": 0.0,
        "fee_waiver_spend": None, "base_reward_rate": 0.01,
        "reward_currency": "cashback", "rp_value_standard": None,
        "rp_value_travel": None, "rp_value_transfer": None,
        "forex_markup_pct": 3.5, "has_rupay_upi": 0,
        "image_asset": f"assets/cards/{card_id}.png",
        "metadata": {"contactless": None, "material": "Plastic",
                     "invite_only": False, "no_preset_limit": False},
        "is_active": 1, "is_travel": 0, "points_expiry_months": None,
        "min_redemption_points": None, "points_clawback_on_default": None,
    }
    inner.update(over)
    return {
        "card": inner,
        "reward_rules": [{
            "rule_name": "1% cashback on all other spends",
            "rule_type": "base_rate", "merchant_ref": None, "category_ref": None,
            "channel": None, "reward_type": "cashback_pct", "reward_rate": 1.0,
            # cap_period is populated so that the stock test edit (which fills in
            # cap_amount) does not trip L4.CAP_WITHOUT_PERIOD and turn every
            # apply-path test into a regression report about the fixture.
            "reward_unit_spend": None, "cap_amount": None, "cap_period": "monthly",
            "min_txn_amount": None, "priority": 20, "effective_date": None,
            "expiry_date": None, "conditions_json": None,
        }],
        "exclusion_rules": [],
        "milestone_rules": [],
        "fuel_surcharge_rules": [],
        "redemption_rules": [],
    }


def write_fixture(root: Path, cards=None, news=None):
    """A complete, internally consistent little repo: seed/ + news/."""
    seed, newsd = root / "seed", root / "news"
    seed.mkdir(parents=True, exist_ok=True)
    newsd.mkdir(parents=True, exist_ok=True)
    cards = [sample_card()] if cards is None else cards
    merchants = {"merchants": [{"merchant_name": "test_merchant", "mcc": "5411"}]}
    F.write_doc(seed / "cards.json", cards, 1)
    F.write_doc(seed / "merchants.json", merchants, 1)
    feed = news if news is not None else {
        "version": "1.0.0", "updated_at": "2026-08-01T00:00:00Z", "items": [],
    }
    F.write_doc(newsd / "feed.json", feed, 2)
    man = {
        "version": "1.0.0", "updated_at": "2026-08-01T00:00:00Z",
        "min_app_version": "1.1.0", "source": "test fixture",
        "stats": {"total_cards": len(cards)},
        "files": [
            {"name": "cards.json", "path": "seed/cards.json",
             "checksum": "", "size_bytes": 0},
            {"name": "merchants.json", "path": "seed/merchants.json",
             "checksum": "", "size_bytes": 0},
        ],
        "delta_file": None, "news_version": "1.0.0",
    }
    F.regen_manifest(seed, man)
    # regen bumps the version and stamps 'now'; the fixture must start from a
    # known, clearly-old state or "was this refreshed?" is untestable inside the
    # same wall-clock second.
    man["version"] = "1.0.0"
    man["updated_at"] = "2026-08-01T00:00:00Z"
    F.write_doc(seed / "manifest.json", man, 1)
    return seed, newsd


def tree_digest(*dirs) -> dict:
    """{relative path: sha256} over every file under these directories."""
    out = {}
    for d in dirs:
        d = Path(d)
        for p in sorted(d.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(d.parent))] = hashlib.sha256(
                    p.read_bytes()).hexdigest()
    return out


def an_edit(**over):
    kw = dict(
        card_id="t_card", block="reward_rules", index=0, field="cap_amount",
        old_value=None, new_value=5000.0, code="L4.CAP_NOT_A_NUMBER",
        reason="The cap is written as text, so the app reads no cap at all.",
        evidence="cap_amount was the string '5,000'", confidence=CERTAIN,
    )
    kw.update(over)
    return Edit(**kw)


def stub_fixer(name, edits, family="test family", handles=("L4.CAP_NOT_A_NUMBER",),
               raise_with=None, mutate=False):
    """A fixer module built in memory, registered so importlib finds it."""
    mod = types.ModuleType(f"fixers.{name}")
    mod.FAMILY = family
    mod.HANDLES = list(handles)

    def plan(ctx, findings, _edits=edits):
        if raise_with is not None:
            raise raise_with
        if mutate:
            ctx.cards.append({"card": {"id": "injected_by_a_bad_fixer"}})
        return [copy.deepcopy(e) for e in _edits]

    mod.plan = plan
    sys.modules[f"fixers.{name}"] = mod
    return mod


def settling_fixer(name="t_settle"):
    """A stub that behaves like a REAL fixer: it proposes only what is still wrong.

    The plain stub_fixer re-emits its edits forever regardless of the file, which
    is fine for testing anchors and gating but useless for testing whether a
    sweep settles — a real fixer's edit disappears once the finding it repairs is
    gone. This one reads the data each time, so a second pass over an
    already-fixed file genuinely proposes nothing.
    """
    mod = types.ModuleType(f"fixers.{name}")
    mod.FAMILY, mod.HANDLES = "settling", ["L4.CAP_NOT_A_NUMBER"]

    def plan(ctx, findings):
        out = []
        for _i, entry, inner, cid in ctx.entries():
            rows = entry.get("reward_rules") or []
            for j, r in enumerate(rows):
                if isinstance(r, dict) and r.get("cap_amount") is None:
                    out.append(an_edit(card_id=cid, index=j))
        return out

    mod.plan = plan
    sys.modules[f"fixers.{name}"] = mod
    return mod


def run_main(argv):
    """(exit_code, stdout). main() never gets to print into the test log."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = F.main(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 3
    return code, buf.getvalue()


class FixtureCase(unittest.TestCase):
    """A temp repo per test, and the fixer list forced to whatever the test wants."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fixcards-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.seed, self.news = write_fixture(self.tmp)
        self._real_discover = F.discover_fixers
        self.addCleanup(setattr, F, "discover_fixers", self._real_discover)

    def use_fixers(self, *mods):
        F.discover_fixers = lambda: [(m.__name__.split(".")[-1], m, None) for m in mods]

    def args(self, *extra):
        return ["--seed-dir", str(self.seed), "--news-dir", str(self.news),
                "--backup", str(self.tmp / "backups"), *extra]

    def cards(self):
        return json.loads((self.seed / "cards.json").read_text(encoding="utf-8"))

    def manifest(self):
        return json.loads((self.seed / "manifest.json").read_text(encoding="utf-8"))

    def feed(self):
        return json.loads((self.news / "feed.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. The real fixers: importable, and pure
# --------------------------------------------------------------------------- #
class TestFixersArePure(unittest.TestCase):
    """plan() is documented as pure. Documented is not the same as true.

    Every module is planned against the real catalogue with a deep copy taken
    first and deep-compared after. A module that mutated the data it was handed
    would make the dry run a lie — the 'preview' would already have happened.
    """

    @classmethod
    def setUpClass(cls):
        cls.mods = F.discover_fixers()
        app = Path(APP_ROOT) if APP_ROOT else None
        cls.ctx, _ = F.load_ctx(F.LIVE_SEED, F.LIVE_NEWS,
                                app if app and app.is_dir() else None)
        cls.findings, cls.skipped, cls.counts = F.validate(cls.ctx)

    def test_at_least_one_fixer_exists(self):
        self.assertGreaterEqual(len(self.mods), 1, "tools/fixers/ has no modules")

    def test_every_fixer_imports(self):
        for name, mod, imp_err in self.mods:
            with self.subTest(fixer=name):
                self.assertIsNone(imp_err, f"{name} failed to import: {imp_err}")
                self.assertIsNotNone(mod)

    def test_every_fixer_declares_a_family(self):
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                self.assertIsInstance(getattr(mod, "FAMILY", None), str)
                self.assertTrue(mod.FAMILY.strip())

    def test_every_fixer_declares_the_codes_it_handles(self):
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                self.assertIsInstance(getattr(mod, "HANDLES", None), list)
                self.assertTrue(all(isinstance(c, str) for c in mod.HANDLES))

    def test_every_fixer_exposes_plan(self):
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                self.assertTrue(callable(getattr(mod, "plan", None)))

    def test_plan_does_not_mutate_its_input(self):
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                before = F.snapshot(self.ctx)
                mod.plan(self.ctx, self.findings)
                self.assertEqual(F.snapshot(self.ctx), before,
                                 f"{name}.plan() modified the data it was given")

    def test_plan_does_not_mutate_the_findings_it_is_given(self):
        before = copy.deepcopy(self.findings)
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                mod.plan(self.ctx, self.findings)
                self.assertEqual(self.findings, before,
                                 f"{name}.plan() modified the findings list")

    def test_plan_returns_a_list_of_edits(self):
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                got = mod.plan(self.ctx, self.findings)
                self.assertIsInstance(got, list)
                self.assertTrue(all(isinstance(e, Edit) for e in got))

    def test_plan_is_deterministic(self):
        for name, mod, _e in self.mods:
            with self.subTest(fixer=name):
                a = [F.sort_key(e) for e in mod.plan(self.ctx, self.findings)]
                b = [F.sort_key(e) for e in mod.plan(self.ctx, self.findings)]
                self.assertEqual(a, b, f"{name}.plan() is not deterministic")

    def test_every_edit_carries_a_reason_and_evidence(self):
        for name, mod, _e in self.mods:
            for e in mod.plan(self.ctx, self.findings):
                with self.subTest(fixer=name, anchor=e.anchor()):
                    self.assertTrue(e.reason.strip())
                    self.assertTrue(str(e.evidence).strip())

    def test_every_edit_has_a_known_confidence(self):
        for name, mod, _e in self.mods:
            for e in mod.plan(self.ctx, self.findings):
                with self.subTest(fixer=name, anchor=e.anchor()):
                    self.assertIn(e.confidence, (CERTAIN, LIKELY))

    def test_optional_accessors_are_also_pure(self):
        for name, mod, _e in self.mods:
            for attr in ("refusals", "census"):
                fn = getattr(mod, attr, None)
                if not callable(fn):
                    continue
                with self.subTest(fixer=name, accessor=attr):
                    before = F.snapshot(self.ctx)
                    fn(self.ctx, self.findings)
                    self.assertEqual(F.snapshot(self.ctx), before)


# --------------------------------------------------------------------------- #
# 2. rule_name is untouchable
# --------------------------------------------------------------------------- #
class TestRuleNameIsUntouchable(unittest.TestCase):
    """Two locks, because one lock is a promise and two are a mechanism.

    The name is the only independent evidence in the file — every numeric check
    ultimately compares a stored number against the issuer's own sentence — and
    the app buckets each user's saved cap progress under that exact string.
    """

    @classmethod
    def setUpClass(cls):
        cls.mods = F.discover_fixers()
        app = Path(APP_ROOT) if APP_ROOT else None
        cls.ctx, _ = F.load_ctx(F.LIVE_SEED, F.LIVE_NEWS,
                                app if app and app.is_dir() else None)
        cls.findings, _s, _c = F.validate(cls.ctx)

    def test_no_real_fixer_emits_a_rule_name_edit(self):
        for name, mod, _e in self.mods:
            for e in mod.plan(self.ctx, self.findings):
                with self.subTest(fixer=name, anchor=e.anchor()):
                    self.assertNotEqual(e.field, "rule_name")

    def test_no_real_fixer_renames_a_rule_inside_a_row(self):
        for name, mod, _e in self.mods:
            for e in mod.plan(self.ctx, self.findings):
                with self.subTest(fixer=name, anchor=e.anchor()):
                    self.assertIsNone(F.forbidden(e), F.forbidden(e))

    def test_guard_blocks_a_direct_rule_name_edit(self):
        e = an_edit(field="rule_name", old_value="old name", new_value="new name")
        kept, blocked = F.guard([e])
        self.assertEqual(kept, [])
        self.assertEqual(len(blocked), 1)
        self.assertIn("rule_name", blocked[0][1])

    def test_guard_blocks_a_rename_hidden_in_a_row_edit(self):
        e = an_edit(field=None,
                    old_value={"rule_name": "5X on fuel", "reward_rate": 5.0},
                    new_value={"rule_name": "5 points per 100 on fuel",
                               "reward_rate": 5.0})
        kept, blocked = F.guard([e])
        self.assertEqual(kept, [])
        self.assertIn("in passing", blocked[0][1])

    def test_guard_blocks_a_rename_hidden_in_an_entry_edit(self):
        old = sample_card()
        new = copy.deepcopy(old)
        new["reward_rules"][0]["rule_name"] = "something else entirely"
        e = an_edit(block=None, index=0, field=None, old_value=old, new_value=new)
        kept, blocked = F.guard([e])
        self.assertEqual(kept, [])

    def test_guard_blocks_a_rename_hidden_in_a_replaced_block(self):
        e = an_edit(block=None, index=None, field="reward_rules",
                    old_value=[{"rule_name": "A"}], new_value=[{"rule_name": "B"}])
        kept, blocked = F.guard([e])
        self.assertEqual(kept, [])

    def test_guard_allows_a_row_edit_that_keeps_the_name(self):
        e = an_edit(field=None,
                    old_value={"rule_name": "5X on fuel", "reward_rate": 5.0},
                    new_value={"rule_name": "5X on fuel", "reward_rate": 1.0})
        kept, blocked = F.guard([e])
        self.assertEqual(len(kept), 1)
        self.assertEqual(blocked, [])

    def test_guard_allows_a_row_deletion_even_though_a_name_disappears(self):
        e = an_edit(field=None, old_value={"rule_name": "gone"}, new_value=None)
        kept, _blocked = F.guard([e])
        self.assertEqual(len(kept), 1, "a deletion removes a row, it does not rename one")

    def test_rule_names_finds_names_at_any_depth(self):
        blob = {"a": [{"rule_name": "x"}, {"b": {"rule_name": "y"}}]}
        self.assertEqual(sorted(F.rule_names(blob)), ["x", "y"])


class TestGuardRunsBeforeFilters(FixtureCase):
    """A filter must never be able to smuggle a forbidden edit past the guard."""

    def test_a_forbidden_edit_never_reaches_the_file(self):
        bad = an_edit(field="rule_name", old_value="1% cashback on all other spends",
                      new_value="renamed")
        self.use_fixers(stub_fixer("t_rename", [bad]))
        before = self.cards()
        code, out = run_main(self.args("--apply"))
        self.assertEqual(self.cards(), before, "a rule_name edit was written")
        self.assertIn("Blocked by the rule_name guard", out)
        self.assertEqual(code, 1, "a blocked edit must be reported as a problem")


# --------------------------------------------------------------------------- #
# 3. Dry run writes nothing
# --------------------------------------------------------------------------- #
class TestDryRunWritesNothing(FixtureCase):

    def test_default_invocation_is_a_dry_run(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = tree_digest(self.seed, self.news)
        run_main(self.args())
        self.assertEqual(tree_digest(self.seed, self.news), before)

    def test_explicit_dry_run_writes_nothing(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = tree_digest(self.seed, self.news)
        run_main(self.args("--dry-run"))
        self.assertEqual(tree_digest(self.seed, self.news), before)

    def test_dry_run_with_diff_writes_nothing(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = tree_digest(self.seed, self.news)
        run_main(self.args("--diff"))
        self.assertEqual(tree_digest(self.seed, self.news), before)

    def test_dry_run_with_json_writes_only_the_json(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = tree_digest(self.seed, self.news)
        out = self.tmp / "plan.json"
        run_main(self.args("--json", str(out)))
        self.assertEqual(tree_digest(self.seed, self.news), before)
        self.assertTrue(out.exists())

    def test_dry_run_says_so_in_the_verdict(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        _code, out = run_main(self.args())
        self.assertIn("DRY RUN", out)
        self.assertIn("nothing was written", out)

    def test_dry_run_exits_zero_with_a_clean_plan(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        code, _out = run_main(self.args())
        self.assertEqual(code, 0)

    def test_apply_and_dry_run_cannot_be_combined(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        code, _out = run_main(self.args("--apply", "--dry-run"))
        self.assertEqual(code, 3)

    def test_no_backup_with_apply_is_refused(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = tree_digest(self.seed, self.news)
        code, _out = run_main(self.args("--apply", "--no-backup"))
        self.assertEqual(code, 3)
        self.assertEqual(tree_digest(self.seed, self.news), before)


# --------------------------------------------------------------------------- #
# 4. Apply produces exactly the planned edit
# --------------------------------------------------------------------------- #
class TestApplyOnAFixture(FixtureCase):

    def test_a_field_edit_lands_exactly(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["cap_amount"], 5000.0)

    def test_nothing_else_in_the_row_moves(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = self.cards()[0]["reward_rules"][0]
        run_main(self.args("--apply"))
        after = self.cards()[0]["reward_rules"][0]
        moved = {k for k in set(before) | set(after)
                 if before.get(k) != after.get(k)}
        self.assertEqual(moved, {"cap_amount"})

    def test_the_rule_name_is_byte_identical_afterwards(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = self.cards()[0]["reward_rules"][0]["rule_name"]
        run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["rule_name"], before)

    def test_a_key_holding_null_can_be_deleted(self):
        """new_value None means 'remove this key' — even when it held null.

        A key present with a null value and a key that is absent are different
        bytes and, for several of the app's parsers, different behaviour.
        """
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="conditions_json", old_value=None, new_value=None)]))
        run_main(self.args("--apply"))
        self.assertNotIn("conditions_json", self.cards()[0]["reward_rules"][0])

    def test_deleting_an_already_absent_key_is_satisfied_not_refused(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="no_such_key", old_value=None, new_value=None)]))
        code, out = run_main(self.args("--apply"))
        self.assertIn("already satisfied", out)
        self.assertEqual(code, 0)

    def test_a_populated_key_can_be_deleted(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="priority", old_value=20, new_value=None)]))
        run_main(self.args("--apply"))
        self.assertNotIn("priority", self.cards()[0]["reward_rules"][0])

    def test_a_row_edit_replaces_the_whole_row(self):
        row = json.loads((self.seed / "cards.json").read_text())[0]["reward_rules"][0]
        new = dict(row, reward_rate=0.5)
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field=None, old_value=row, new_value=new)]))
        run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["reward_rate"], 0.5)

    def test_a_card_field_edit_lands_on_the_inner_card(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            block="card", index=None, field="rp_value_standard",
            old_value=None, new_value=0.25)]))
        run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["card"]["rp_value_standard"], 0.25)

    def test_an_entry_level_field_edit_lands_on_the_entry(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            block=None, index=None, field="exclusion_rules",
            old_value=[], new_value=[{"exclusion_type": "category",
                                      "exclusion_value": "fuel"}])]))
        run_main(self.args("--apply"))
        entry = self.cards()[0]
        self.assertEqual(len(entry["exclusion_rules"]), 1)
        self.assertNotIn("exclusion_rules", entry["card"],
                         "an entry-level key must not be written onto the inner card")

    def test_an_entry_removal_takes_the_card_out(self):
        entry = json.loads((self.seed / "cards.json").read_text())[0]
        self.use_fixers(stub_fixer("t_a", [an_edit(
            block=None, index=0, field=None, old_value=entry, new_value=None,
            confidence=LIKELY, code="L6.INACTIVE_CARD_STILL_RANKS")]))
        run_main(self.args("--apply", "--confidence", "likely"))
        self.assertEqual(self.cards(), [])

    def test_a_manifest_field_edit_lands(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            block="manifest", card_id=None, index=None, field="news_version",
            old_value="1.0.0", new_value="2.0.0",
            code="L9.NEWS_VERSION_DISAGREES_WITH_MANIFEST")]))
        run_main(self.args("--apply"))
        self.assertEqual(self.manifest()["news_version"], "2.0.0")

    def test_a_news_field_edit_lands(self):
        feed = {"version": "1.0.0", "updated_at": "2026-08-01T00:00:00Z",
                "items": [{"id": "news_x_2026_05_30_a", "title": "t",
                           "published_at": "2026-08-17T03:30:47Z"}]}
        write_fixture(self.tmp, news=feed)
        self.use_fixers(stub_fixer("t_a", [an_edit(
            block="news", card_id=None, index=0, field="published_at",
            old_value="2026-08-17T03:30:47Z", new_value="2026-05-30T00:00:00Z",
            code="L9.NEWS_DATE_CONTRADICTS_ITS_OWN_ID")]))
        run_main(self.args("--apply"))
        self.assertEqual(self.feed()["items"][0]["published_at"],
                         "2026-05-30T00:00:00Z")

    def test_an_edit_whose_anchor_moved_is_refused_not_forced(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="reward_rate", old_value=999.0, new_value=2.0)]))
        code, out = run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["reward_rate"], 1.0)
        self.assertIn("REFUSED", out)
        self.assertEqual(code, 1)

    def test_an_edit_for_an_unknown_card_is_refused(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(card_id="no_such_card")]))
        code, out = run_main(self.args("--apply"))
        self.assertIn("REFUSED", out)
        self.assertEqual(code, 1)

    def test_an_out_of_range_row_index_is_refused(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(index=99)]))
        code, out = run_main(self.args("--apply"))
        self.assertIn("REFUSED", out)

    def test_an_already_satisfied_edit_is_a_noop_not_a_failure(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="reward_rate", old_value=1.0, new_value=1.0)]))
        before = tree_digest(self.seed, self.news)
        code, out = run_main(self.args("--apply"))
        self.assertIn("already satisfied", out)
        self.assertEqual(tree_digest(self.seed, self.news), before,
                         "a no-op must not produce a file modification")
        self.assertEqual(code, 0)

    def test_row_replacement_is_applied_before_a_field_edit_on_the_same_row(self):
        """Otherwise the row replacement silently erases the field edit."""
        row = json.loads((self.seed / "cards.json").read_text())[0]["reward_rules"][0]
        replaced = dict(row, reward_type="points_per_spend", reward_unit_spend=100.0)
        self.use_fixers(
            stub_fixer("t_a", [an_edit(field=None, old_value=row, new_value=replaced)]),
            stub_fixer("t_b", [an_edit(field="confidence", old_value=None,
                                       new_value="low",
                                       code="L8.CONFIDENCE_DEFAULTS_TO_HIGH")]),
        )
        run_main(self.args("--apply"))
        got = self.cards()[0]["reward_rules"][0]
        self.assertEqual(got["reward_type"], "points_per_spend")
        self.assertEqual(got["confidence"], "low",
                         "the field edit was erased by the row replacement")


# --------------------------------------------------------------------------- #
# 5. Filters, gating and ordering
# --------------------------------------------------------------------------- #
class TestFiltersAndGate(FixtureCase):

    def test_likely_edits_are_held_back_by_default(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(confidence=LIKELY)]))
        run_main(self.args("--apply"))
        self.assertIsNone(self.cards()[0]["reward_rules"][0]["cap_amount"])

    def test_confidence_likely_applies_certain_too(self):
        self.assertEqual(F.gate_allows(LIKELY), {CERTAIN, LIKELY})

    def test_confidence_certain_is_certain_only(self):
        self.assertEqual(F.gate_allows(CERTAIN), {CERTAIN})

    def test_confidence_likely_lets_a_likely_edit_through(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(confidence=LIKELY)]))
        run_main(self.args("--apply", "--confidence", "likely"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["cap_amount"], 5000.0)

    def test_code_filter_selects_one_code(self):
        edits = [an_edit(code="L4.CAP_NOT_A_NUMBER"),
                 an_edit(field="reward_rate", old_value=1.0, new_value=0.5,
                         code="L5.RATE_CONTRADICTS_NAME")]
        self.use_fixers(stub_fixer("t_a", edits))
        run_main(self.args("--apply", "--code", "L4.CAP_NOT_A_NUMBER"))
        row = self.cards()[0]["reward_rules"][0]
        self.assertEqual(row["cap_amount"], 5000.0)
        self.assertEqual(row["reward_rate"], 1.0, "the other code was applied anyway")

    def test_card_filter_selects_one_card(self):
        cards = [sample_card("t_card"), sample_card("t_other")]
        write_fixture(self.tmp, cards=cards)
        self.use_fixers(stub_fixer("t_a", [an_edit(card_id="t_card"),
                                           an_edit(card_id="t_other")]))
        run_main(self.args("--apply", "--card", "t_card"))
        got = {c["card"]["id"]: c["reward_rules"][0]["cap_amount"]
               for c in self.cards()}
        self.assertEqual(got["t_card"], 5000.0)
        self.assertIsNone(got["t_other"])

    def test_family_filter_selects_one_fixer(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()], family="caps"),
                        stub_fixer("t_b", [an_edit(field="reward_rate",
                                                   old_value=1.0, new_value=0.5)],
                                   family="rates & units"))
        run_main(self.args("--apply", "--family", "caps"))
        row = self.cards()[0]["reward_rules"][0]
        self.assertEqual(row["cap_amount"], 5000.0)
        self.assertEqual(row["reward_rate"], 1.0)

    def test_family_filter_matches_on_a_substring(self):
        mod = stub_fixer("t_a", [], family="rates & units")
        self.assertTrue(F.family_matches(["units"], mod, "f1_units"))
        self.assertTrue(F.family_matches(["RATES"], mod, "f1_units"))
        self.assertFalse(F.family_matches(["caps"], mod, "f1_units"))

    def test_limit_caps_the_number_of_edits(self):
        cards = [sample_card(f"t_{i}") for i in range(5)]
        write_fixture(self.tmp, cards=cards)
        self.use_fixers(stub_fixer("t_a", [an_edit(card_id=f"t_{i}")
                                           for i in range(5)]))
        run_main(self.args("--apply", "--limit", "2"))
        done = sum(1 for c in self.cards()
                   if c["reward_rules"][0]["cap_amount"] == 5000.0)
        self.assertEqual(done, 2)

    def test_limit_is_deterministic(self):
        edits = [an_edit(card_id=f"t_{i}") for i in range(5)]
        a = [e.anchor() for e in F.select(edits, limit=2)]
        b = [e.anchor() for e in F.select(list(reversed(edits)), limit=2)]
        self.assertEqual(a, b, "--limit N must pick the same N whatever the input order")

    def test_negative_limit_is_rejected(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        code, _out = run_main(self.args("--limit", "-1"))
        self.assertEqual(code, 3)

    def test_sort_order_is_total_and_stable(self):
        edits = [an_edit(card_id="b"), an_edit(card_id="a"),
                 an_edit(card_id="a", index=1)]
        keys = [F.sort_key(e) for e in F.select(edits)]
        self.assertEqual(keys, sorted(keys))

    def test_a_bad_confidence_value_is_rejected(self):
        code, _out = run_main(self.args("--confidence", "vibes"))
        self.assertEqual(code, 3)

    def test_an_unknown_flag_exits_three_not_two(self):
        code, _out = run_main(self.args("--no-such-flag"))
        self.assertEqual(code, 3, "2 is reserved: it is the validator's 'publishable'")


# --------------------------------------------------------------------------- #
# 6. Idempotency
# --------------------------------------------------------------------------- #
class TestIdempotency(FixtureCase):

    def test_applying_twice_changes_nothing_the_second_time(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        after_first = tree_digest(self.seed, self.news)
        # The same plan re-offered: the runner must recognise it as satisfied.
        run_main(self.args("--apply"))
        self.assertEqual(tree_digest(self.seed, self.news), after_first)

    def test_a_second_apply_reports_no_work(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        _code, out = run_main(self.args("--apply"))
        self.assertIn("already satisfied", out)

    def test_idempotency_is_asserted_and_reported(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        _code, out = run_main(self.args("--apply"))
        self.assertIn("Idempotency", out)

    def test_a_run_truncated_by_limit_is_partial_not_unstable(self):
        """--limit is SUPPOSED to leave work behind.

        Calling that "not idempotent" would train the operator to ignore the one
        message that means a fixer is genuinely unstable.
        """
        write_fixture(self.tmp, cards=[sample_card(f"t_{i}") for i in range(4)])
        self.use_fixers(settling_fixer())
        code, out = run_main(self.args("--apply", "--limit", "2"))
        self.assertIn("partial run", out)
        self.assertNotIn("NOT IDEMPOTENT", out)
        self.assertEqual(code, 0)

    def test_a_partial_run_does_not_assert_idempotency_in_the_json(self):
        write_fixture(self.tmp, cards=[sample_card(f"t_{i}") for i in range(4)])
        self.use_fixers(settling_fixer())
        out = self.tmp / "plan.json"
        run_main(self.args("--apply", "--limit", "2", "--json", str(out)))
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsNone(doc["idempotent"], "a truncated run must not claim idempotency")
        self.assertTrue(doc["second_pass"]["truncated_by_limit"])

    def test_a_settling_fixer_finishes_and_plans_nothing_the_second_time(self):
        write_fixture(self.tmp, cards=[sample_card(f"t_{i}") for i in range(4)])
        self.use_fixers(settling_fixer())
        code, out = run_main(self.args("--apply"))
        self.assertIn("a second pass plans zero edits", out)
        self.assertEqual(code, 0)

    def test_a_limit_larger_than_the_plan_is_not_partial(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        code, out = run_main(self.args("--apply", "--limit", "99"))
        self.assertNotIn("partial run", out)
        self.assertEqual(code, 0)

    def test_a_fixer_that_never_settles_is_reported_loudly(self):
        """A fixer that re-proposes forever must fail the run, not loop quietly."""
        class Never:
            def __init__(self):
                self.calls = 0

            def plan(self, ctx, findings):
                self.calls += 1
                # Always proposes the value the file does NOT hold.
                return [an_edit(field="reward_rate", old_value=1.0,
                                new_value=1.0 + self.calls)]

        mod = types.ModuleType("fixers.t_never")
        mod.FAMILY, mod.HANDLES = "never settles", ["L4.CAP_NOT_A_NUMBER"]
        never = Never()
        mod.plan = never.plan
        sys.modules["fixers.t_never"] = mod
        self.use_fixers(mod)
        code, out = run_main(self.args("--apply"))
        self.assertIn("NOT IDEMPOTENT", out)
        self.assertEqual(code, 1)


# --------------------------------------------------------------------------- #
# 7. The manifest
# --------------------------------------------------------------------------- #
class TestManifest(FixtureCase):

    def test_checksums_match_the_bytes_after_an_apply(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        man = self.manifest()
        for f in man["files"]:
            raw = (self.seed / (f.get("file") or f["name"])).read_bytes()
            with self.subTest(file=f["name"]):
                self.assertEqual(f["checksum"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(f["size_bytes"], len(raw))

    def test_the_version_is_bumped_on_a_write(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = self.manifest()["version"]
        run_main(self.args("--apply"))
        self.assertNotEqual(self.manifest()["version"], before)

    def test_updated_at_is_refreshed(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = self.manifest()["updated_at"]
        run_main(self.args("--apply"))
        self.assertNotEqual(self.manifest()["updated_at"], before)

    def test_the_manifest_is_untouched_when_nothing_is_applied(self):
        self.use_fixers(stub_fixer("t_a", [an_edit(confidence=LIKELY)]))
        before = (self.seed / "manifest.json").read_bytes()
        run_main(self.args("--apply"))
        self.assertEqual((self.seed / "manifest.json").read_bytes(), before)

    def test_regen_manifest_reports_which_files_moved(self):
        man = self.manifest()
        (self.seed / "cards.json").write_text("[]\n", encoding="utf-8")
        changed = F.regen_manifest(self.seed, man)
        self.assertIn("cards.json", changed)

    def test_regen_manifest_survives_a_missing_declared_file(self):
        man = self.manifest()
        man["files"].append({"name": "ghost.json", "path": "seed/ghost.json",
                             "checksum": "x", "size_bytes": 1})
        F.regen_manifest(self.seed, man)          # must not raise
        self.assertEqual(man["files"][-1]["checksum"], "x")


# --------------------------------------------------------------------------- #
# 8. Backups
# --------------------------------------------------------------------------- #
class TestBackup(FixtureCase):

    def test_a_backup_is_written_before_applying(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        saved = list((self.tmp / "backups").rglob("cards.json"))
        self.assertEqual(len(saved), 1)

    def test_the_backup_holds_the_PRE_edit_bytes(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        before = (self.seed / "cards.json").read_bytes()
        run_main(self.args("--apply"))
        saved = list((self.tmp / "backups").rglob("cards.json"))[0]
        self.assertEqual(saved.read_bytes(), before)
        self.assertNotEqual((self.seed / "cards.json").read_bytes(), before)

    def test_the_backup_covers_every_file_that_could_be_touched(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        names = {p.name for p in (self.tmp / "backups").rglob("*.json")}
        self.assertEqual(names, {"cards.json", "manifest.json", "feed.json"})

    def test_no_backup_is_taken_on_a_dry_run(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args())
        self.assertFalse((self.tmp / "backups").exists())


# --------------------------------------------------------------------------- #
# 9. A fixer that misbehaves
# --------------------------------------------------------------------------- #
class TestCrashIsolation(FixtureCase):

    def test_a_fixer_that_raises_does_not_corrupt_data(self):
        self.use_fixers(stub_fixer("t_boom", [], raise_with=RuntimeError("boom")))
        before = tree_digest(self.seed, self.news)
        code, out = run_main(self.args("--apply"))
        self.assertEqual(tree_digest(self.seed, self.news), before)
        self.assertIn("did not deliver", out)
        self.assertEqual(code, 1)

    def test_a_fixer_that_raises_does_not_stop_the_others(self):
        self.use_fixers(stub_fixer("t_boom", [], raise_with=RuntimeError("boom")),
                        stub_fixer("t_ok", [an_edit()]))
        run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["cap_amount"], 5000.0)

    def test_a_fixer_that_mutates_ctx_has_its_edits_discarded(self):
        self.use_fixers(stub_fixer("t_mut", [an_edit()], mutate=True))
        before = tree_digest(self.seed, self.news)
        code, out = run_main(self.args("--apply"))
        self.assertEqual(tree_digest(self.seed, self.news), before)
        self.assertIn("mutation", out)
        self.assertEqual(code, 1)

    def test_a_fixer_that_mutates_does_not_poison_the_next_fixer(self):
        self.use_fixers(stub_fixer("t_mut", [], mutate=True),
                        stub_fixer("t_ok", [an_edit()]))
        run_main(self.args("--apply"))
        self.assertEqual(len(self.cards()), 1, "the injected card reached the file")

    def test_a_fixer_returning_the_wrong_type_is_reported(self):
        mod = types.ModuleType("fixers.t_bad")
        mod.FAMILY, mod.HANDLES = "bad", []
        mod.plan = lambda ctx, findings: "not a list"
        sys.modules["fixers.t_bad"] = mod
        self.use_fixers(mod)
        code, out = run_main(self.args("--apply"))
        self.assertIn("not a list", out)
        self.assertEqual(code, 1)

    def test_a_fixer_whose_optional_accessor_raises_still_contributes_edits(self):
        mod = stub_fixer("t_ref", [an_edit()])
        def boom(ctx, findings):
            raise RuntimeError("refusals is broken")
        mod.refusals = boom
        self.use_fixers(mod)
        run_main(self.args("--apply"))
        self.assertEqual(self.cards()[0]["reward_rules"][0]["cap_amount"], 5000.0)

    def test_an_edit_without_evidence_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            an_edit(evidence="")

    def test_an_edit_without_a_reason_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            an_edit(reason="   ")

    def test_an_edit_with_an_invented_confidence_cannot_be_constructed(self):
        with self.assertRaises(ValueError):
            an_edit(confidence="pretty sure")


# --------------------------------------------------------------------------- #
# 10. Serialisation
# --------------------------------------------------------------------------- #
class TestSerialisation(FixtureCase):

    def test_cards_json_round_trips_at_indent_one(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        raw = (self.seed / "cards.json").read_bytes()
        rebuilt = json.dumps(json.loads(raw), indent=1, ensure_ascii=False).encode() + b"\n"
        self.assertEqual(raw, rebuilt)

    def test_manifest_round_trips_at_indent_one(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        raw = (self.seed / "manifest.json").read_bytes()
        rebuilt = json.dumps(json.loads(raw), indent=1, ensure_ascii=False).encode() + b"\n"
        self.assertEqual(raw, rebuilt)

    def test_the_news_feed_round_trips_at_indent_two(self):
        raw = (self.news / "feed.json").read_bytes()
        rebuilt = json.dumps(json.loads(raw), indent=2, ensure_ascii=False).encode() + b"\n"
        self.assertEqual(raw, rebuilt)

    def test_the_declared_indents_match_the_existing_writer(self):
        """pipeline/cli.py is the writer these numbers were copied from."""
        src = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("json.dumps(new_cards, indent=1", src)
        self.assertIn("json.dumps(man, indent=1", src)
        self.assertEqual(F.INDENT["cards.json"], 1)
        self.assertEqual(F.INDENT["manifest.json"], 1)
        self.assertEqual(F.INDENT["feed.json"], 2)

    def test_key_order_is_preserved(self):
        before = list(self.cards()[0]["reward_rules"][0].keys())
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="reward_rate", old_value=1.0, new_value=0.5)]))
        run_main(self.args("--apply"))
        self.assertEqual(list(self.cards()[0]["reward_rules"][0].keys()), before)

    def test_a_one_field_change_is_a_one_line_diff(self):
        before = (self.seed / "cards.json").read_text(encoding="utf-8").splitlines()
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="reward_rate", old_value=1.0, new_value=0.5)]))
        run_main(self.args("--apply"))
        after = (self.seed / "cards.json").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(differing), 1)

    def test_non_ascii_is_written_unescaped(self):
        cards = [sample_card()]
        cards[0]["reward_rules"][0]["rule_name"] = "₹100 पर 1% कैशबैक"
        write_fixture(self.tmp, cards=cards)
        self.use_fixers(stub_fixer("t_a", [an_edit(
            field="reward_rate", old_value=1.0, new_value=0.5)]))
        run_main(self.args("--apply"))
        self.assertIn("₹100", (self.seed / "cards.json").read_text(encoding="utf-8"))

    def test_two_applies_of_the_same_plan_give_identical_bytes(self):
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply"))
        first = (self.seed / "cards.json").read_bytes()
        write_fixture(self.tmp)
        run_main(self.args("--apply"))
        self.assertEqual((self.seed / "cards.json").read_bytes(), first)


# --------------------------------------------------------------------------- #
# 11. The JSON report
# --------------------------------------------------------------------------- #
class TestJsonReport(FixtureCase):

    def _plan(self, *extra):
        out = self.tmp / "plan.json"
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--json", str(out), *extra))
        return json.loads(out.read_text(encoding="utf-8"))

    def test_the_plan_is_valid_json(self):
        self.assertIsInstance(self._plan(), dict)

    def test_the_plan_names_its_mode(self):
        self.assertEqual(self._plan()["meta"]["mode"], "dry-run")

    def test_the_plan_records_the_confidence_gate(self):
        self.assertEqual(self._plan()["meta"]["confidence_gate"], CERTAIN)

    def test_the_plan_carries_every_edit_with_its_evidence(self):
        doc = self._plan()
        self.assertEqual(len(doc["edits"]), 1)
        self.assertTrue(doc["edits"][0]["evidence"])
        self.assertTrue(doc["edits"][0]["reason"])

    def test_the_plan_names_the_module_that_authored_each_edit(self):
        self.assertEqual(self._plan()["edits"][0]["module"], "t_a")

    def test_the_plan_reports_whether_the_run_was_degraded(self):
        self.assertIn("degraded", self._plan()["meta"])

    def test_the_plan_says_where_the_app_facts_came_from(self):
        self.assertIn("categories_source", self._plan()["meta"])

    def test_an_apply_records_the_before_and_after_counts(self):
        out = self.tmp / "plan.json"
        self.use_fixers(stub_fixer("t_a", [an_edit()]))
        run_main(self.args("--apply", "--json", str(out)))
        doc = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("errors", doc["validation_before"])
        self.assertIn("errors", doc["validation_after"])
        self.assertIs(doc["idempotent"], True)
        self.assertEqual(doc["second_pass"]["would_apply"], 0)
        self.assertEqual(doc["second_pass"]["would_refuse"], 0)


# --------------------------------------------------------------------------- #
# 12. Helpers
# --------------------------------------------------------------------------- #
class TestHelpers(unittest.TestCase):

    def test_entry_of_finds_a_card_by_id_not_by_position(self):
        cards = [sample_card("a"), sample_card("b")]
        i, entry, inner = F.entry_of(cards, "b")
        self.assertEqual(i, 1)
        self.assertEqual(inner["id"], "b")

    def test_entry_of_returns_nothing_for_an_unknown_id(self):
        self.assertEqual(F.entry_of([sample_card("a")], "z"), (None, None, None))

    def test_entry_of_survives_a_non_list(self):
        self.assertEqual(F.entry_of({"cards": []}, "a"), (None, None, None))

    def test_news_items_reads_items_then_articles(self):
        self.assertEqual(F.news_items({"items": [1]}), [1])
        self.assertEqual(F.news_items({"articles": [2]}), [2])
        self.assertEqual(F.news_items([3]), [3])
        self.assertIsNone(F.news_items({"nope": 1}))

    def test_anchor_ok_treats_a_missing_key_as_none(self):
        self.assertTrue(F.anchor_ok(None, None))
        self.assertFalse(F.anchor_ok("x", None))
        self.assertTrue(F.anchor_ok("x", "x"))
        self.assertFalse(F.anchor_ok("x", "y"))

    def test_the_forbidden_field_is_rule_name(self):
        self.assertEqual(F.FORBIDDEN_FIELD, "rule_name")

    def test_discover_fixers_is_sorted_and_skips_base(self):
        names = [nm for nm, _m, _e in F.discover_fixers()]
        self.assertEqual(names, sorted(names))
        self.assertNotIn("base", names)


# --------------------------------------------------------------------------- #
# 13. A WRITE CANNOT TEAR  (adversarial review, corruption lens, defects 1 & 2)
# --------------------------------------------------------------------------- #
class TestWritesAreAtomic(FixtureCase):
    """`path.write_text` truncated the target to zero and streamed 1.85 MB back
    into the same inode. Reproduced with a file-size ceiling: cards.json ended up
    1,838,080 bytes and unparseable, and the tool never noticed. Every one of
    these pins a property of the staged-then-swapped writer that replaced it."""

    def test_a_failed_write_leaves_the_original_file_byte_identical(self):
        target = self.seed / "cards.json"
        before = target.read_bytes()
        real = os.fsync

        def blow_up(fd):
            raise OSError(27, "File too large")

        os.fsync = blow_up
        self.addCleanup(setattr, os, "fsync", real)
        with self.assertRaises(OSError):
            F.write_doc(target, [{"card": {"id": "new"}}], 1)
        os.fsync = real
        self.assertEqual(target.read_bytes(), before)
        json.loads(target.read_text(encoding="utf-8"))

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        real = os.fsync
        os.fsync = lambda fd: (_ for _ in ()).throw(OSError(28, "No space left"))
        self.addCleanup(setattr, os, "fsync", real)
        with self.assertRaises(OSError):
            F.write_doc(self.seed / "cards.json", [{"card": {"id": "new"}}], 1)
        os.fsync = real
        leftovers = [q.name for q in self.seed.iterdir() if ".fix-" in q.name]
        self.assertEqual(leftovers, [])

    def test_the_temp_file_is_a_sibling_so_the_swap_stays_on_one_filesystem(self):
        seen = []
        real_stage = F.stage

        def watch(path, raw):
            tmp = real_stage(path, raw)
            seen.append(tmp)
            return tmp

        F.stage = watch
        self.addCleanup(setattr, F, "stage", real_stage)
        F.write_doc(self.seed / "cards.json", [{"card": {"id": "new"}}], 1)
        self.assertTrue(seen)
        self.assertEqual(seen[0].parent, self.seed)

    def test_bytes_that_do_not_reparse_never_reach_the_target(self):
        target = self.seed / "cards.json"
        before = target.read_bytes()
        real_ser = F.serialise
        F.serialise = lambda obj, indent: b"{not json"
        self.addCleanup(setattr, F, "serialise", real_ser)
        with self.assertRaises(json.JSONDecodeError):
            F.write_doc(target, [{"card": {"id": "x"}}], 1)
        F.serialise = real_ser
        self.assertEqual(target.read_bytes(), before)


class TestTheThreeFilesAreOneTransaction(FixtureCase):
    """The manifest was written LAST and separately, so a failure in between left
    new card bytes under a manifest declaring the old checksum — the state the
    app shows a user as "Sync failed"."""

    def docs(self):
        docs = F.load_docs(self.seed, self.news)
        pristine = copy.deepcopy(docs)
        docs["cards"].append(sample_card("t_second"))
        return docs, pristine

    def test_a_failure_staging_the_manifest_leaves_cards_untouched(self):
        docs, pristine = self.docs()
        cards_before = (self.seed / "cards.json").read_bytes()
        man_before = (self.seed / "manifest.json").read_bytes()

        real_stage = F.stage
        def refuse_the_manifest(path, raw):
            if path.name == "manifest.json":
                raise OSError(13, "Permission denied")
            return real_stage(path, raw)
        F.stage = refuse_the_manifest
        self.addCleanup(setattr, F, "stage", real_stage)

        with self.assertRaises(OSError):
            F.write_all(self.seed, self.news, docs, pristine)
        F.stage = real_stage
        self.assertEqual((self.seed / "cards.json").read_bytes(), cards_before)
        self.assertEqual((self.seed / "manifest.json").read_bytes(), man_before)
        self.assertEqual([q.name for q in self.seed.iterdir() if ".fix-" in q.name], [])

    def test_the_manifest_is_checksummed_against_the_bytes_being_written(self):
        docs, pristine = self.docs()
        touched, _changed = F.write_all(self.seed, self.news, docs, pristine)
        self.assertIn("cards", touched)
        self.assertIn("manifest", touched)
        raw = (self.seed / "cards.json").read_bytes()
        man = self.manifest()
        row = [f for f in man["files"] if (f.get("name") or f.get("file")) == "cards.json"][0]
        self.assertEqual(row["checksum"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(row["size_bytes"], len(raw))

    def test_a_swap_failing_after_one_landed_raises_TreeInconsistent(self):
        docs, pristine = self.docs()
        real_replace = os.replace
        calls = []

        def fail_the_second(src, dst):
            calls.append(dst)
            if len(calls) > 1:
                raise OSError(13, "Permission denied")
            return real_replace(src, dst)

        os.replace = fail_the_second
        self.addCleanup(setattr, os, "replace", real_replace)
        with self.assertRaises(F.TreeInconsistent):
            F.write_all(self.seed, self.news, docs, pristine)
        os.replace = real_replace

    def test_exit_code_4_is_reserved_for_a_tree_that_may_be_inconsistent(self):
        self.use_fixers(stub_fixer("t_incon", [an_edit()]))
        real = F.write_all
        F.write_all = lambda *a, **k: (_ for _ in ()).throw(
            F.TreeInconsistent("the second swap failed"))
        self.addCleanup(setattr, F, "write_all", real)
        code, out = run_main(self.args("--apply"))
        F.write_all = real
        self.assertEqual(code, 4)
        self.assertEqual(code, F.EXIT_INCONSISTENT)
        self.assertIn("INCONSISTENT", out)

    def test_a_staging_failure_exits_3_and_says_nothing_was_written(self):
        self.use_fixers(stub_fixer("t_stagefail", [an_edit()]))
        before = self.cards()
        real = F.stage
        F.stage = lambda path, raw: (_ for _ in ()).throw(OSError(28, "No space left"))
        self.addCleanup(setattr, F, "stage", real)
        code, out = run_main(self.args("--apply"))
        F.stage = real
        self.assertEqual(code, 3)
        self.assertIn("NOTHING was written", out)
        self.assertEqual(self.cards(), before)


class TestOnlyOneApplyAtATime(FixtureCase):
    """Two applies against one seed directory lost one run's edits 6 times out of
    6, and the losing run blamed the FIXERS for it in its own report."""

    def test_a_second_apply_refuses_while_the_lock_is_held(self):
        self.use_fixers(stub_fixer("t_lock", [an_edit()]))
        with F.exclusive(self.seed):
            code, out = run_main(self.args("--apply"))
        self.assertEqual(code, 3)
        self.assertIn("already holds", out)

    def test_the_lock_is_released_when_the_run_finishes(self):
        self.use_fixers(stub_fixer("t_lock2", [an_edit()]))
        code, _out = run_main(self.args("--apply"))
        self.assertIn(code, (0, 1))
        with F.exclusive(self.seed):
            pass                       # would raise SystemExit if still held

    def test_cards_moving_under_the_run_aborts_rather_than_overwrites(self):
        self.use_fixers(stub_fixer("t_race", [an_edit()]))
        real = F.apply_grouped

        def meddle(docs, edits):
            out = real(docs, edits)
            cards = self.cards()
            cards.append(sample_card("t_written_by_someone_else"))
            F.write_doc(self.seed / "cards.json", cards, 1)
            return out

        F.apply_grouped = meddle
        self.addCleanup(setattr, F, "apply_grouped", real)
        code, out = run_main(self.args("--apply"))
        F.apply_grouped = real
        self.assertEqual(code, 3)
        self.assertIn("changed on disk", out)
        self.assertIn("t_written_by_someone_else",
                      json.dumps(self.cards()))     # the other write survived


# --------------------------------------------------------------------------- #
# 14. AN EDIT PAIR IS ONE EDIT  (corruption lens, defect 3)
# --------------------------------------------------------------------------- #
class TestGroupsAreAtomic(FixtureCase):
    """The gate filtered edit by edit, so it applied the DEPENDENT half of a pair
    and skipped the source: three Equitas cards ended up pricing one point at
    Rs 0.25 on the rule and Rs 0.50 on the card."""

    def a_pair(self):
        return [
            an_edit(block="card", index=None, field="rp_value_standard",
                    old_value=None, new_value=0.25, confidence=LIKELY,
                    group_id="pv:t_card"),
            an_edit(field="point_value", old_value=None, new_value=0.25,
                    confidence=CERTAIN, group_id="pv:t_card"),
        ]

    def test_the_certain_half_is_held_back_when_its_source_is_likely(self):
        passes, held = F.gate_split(self.a_pair(), F.gate_allows(CERTAIN))
        self.assertEqual(passes, [])
        self.assertEqual(len(held), 2)

    def test_both_halves_pass_together_at_the_lower_gate(self):
        passes, held = F.gate_split(self.a_pair(), F.gate_allows(LIKELY))
        self.assertEqual(len(passes), 2)
        self.assertEqual(held, [])

    def test_ungrouped_edits_are_gated_exactly_as_before(self):
        edits = [an_edit(confidence=CERTAIN), an_edit(index=0, field="cap_period",
                                                     old_value="monthly",
                                                     new_value="month",
                                                     confidence=LIKELY)]
        passes, held = F.gate_split(edits, F.gate_allows(CERTAIN))
        self.assertEqual([e.confidence for e in passes], [CERTAIN])
        self.assertEqual([e.confidence for e in held], [LIKELY])

    def test_a_group_with_one_moved_anchor_is_refused_whole(self):
        docs = F.load_docs(self.seed, self.news)
        pair = [
            an_edit(block="card", index=None, field="rp_value_standard",
                    old_value=None, new_value=0.25, group_id="g"),
            # anchor deliberately wrong: the fixture's cap_amount is None
            an_edit(field="cap_amount", old_value=999.0, new_value=1.0, group_id="g"),
        ]
        applied, _sat, refused = F.apply_grouped(docs, pair)
        self.assertEqual(applied, [])
        self.assertEqual(len(refused), 2)
        self.assertIsNone(docs["cards"][0]["card"]["rp_value_standard"])

    def test_the_default_run_never_splits_a_real_point_value_pair(self):
        """The live catalogue, at the default gate — the exact run that shipped
        the split. Every point_value copy must travel with its card-level source."""
        if not (F.LIVE_SEED / "cards.json").exists():
            self.skipTest("no live catalogue")
        ctx, _notes = F.load_ctx(F.LIVE_SEED, F.LIVE_NEWS,
                                 Path(APP_ROOT) if APP_ROOT else None)
        findings, _skipped, _counts = F.validate(ctx)
        edits, _fail, _extra = F.plan_all(ctx, findings, self._real_discover())
        kept, _blocked = F.guard(edits)
        for level in (CERTAIN, LIKELY):
            passes, _held = F.gate_split(kept, F.gate_allows(level))
            cards_moved = {e.card_id for e in passes if e.field == "rp_value_standard"}
            copies = {e.card_id for e in passes if e.field == "point_value"}
            self.assertEqual(copies - cards_moved, set(),
                             f"at --confidence {level} a rule-level point_value copy "
                             f"would be written without the card value it derives from")


# --------------------------------------------------------------------------- #
# 15. AN IRREVERSIBLE DELETION SETTLES IN ONE PASS  (corruption lens, defect 4)
# --------------------------------------------------------------------------- #
class TestEntryEditsAnchorOnIdentity(FixtureCase):
    """Anchored on the WHOLE entry, a card removal was refused the moment any
    field edit touched the same card — so 10 of 13 removals landed only on the
    SECOND run, and an irreversible operation's outcome depended on how many
    times it had been run."""

    def removal(self, **over):
        kw = dict(card_id="t_card", block=None, index=0, field=None,
                  new_value=None, code="L6.INACTIVE_CARD_STILL_RANKS",
                  reason="Our own data marks this card as withdrawn.",
                  evidence="card.is_active = 0", confidence=CERTAIN,
                  reversible=False)
        kw.update(over)
        return Edit(**kw)

    def test_a_field_edit_on_the_same_card_no_longer_blocks_the_removal(self):
        docs = F.load_docs(self.seed, self.news)
        entry = copy.deepcopy(docs["cards"][0])
        edits = [an_edit(),                       # touches reward_rules[0]
                 self.removal(old_value=entry,
                              anchor_fields={"card.is_active": 1})]
        applied, _sat, refused = F.apply_edits(docs, edits)
        self.assertEqual(len(applied), 2, [w for _e, w in refused])
        self.assertEqual(docs["cards"], [])

    def test_the_whole_entry_anchor_still_blocks_a_replacement(self):
        docs = F.load_docs(self.seed, self.news)
        stale = copy.deepcopy(docs["cards"][0])
        stale["card"]["annual_fee"] = 999.0        # not what is on disk
        edits = [self.removal(old_value=stale, new_value=None)]
        applied, _sat, refused = F.apply_edits(docs, edits)
        self.assertEqual(applied, [])
        self.assertIn("anchor", refused[0][1])

    def test_the_refusal_message_names_the_cause_not_the_edits_reason(self):
        docs = F.load_docs(self.seed, self.news)
        e = self.removal(old_value=docs["cards"][0],
                         anchor_fields={"card.is_active": 0})   # disk says 1
        _applied, _sat, refused = F.apply_edits(docs, [e])
        why = refused[0][1]
        self.assertIn("is_active", why)
        self.assertIn("anchor moved", why)
        self.assertNotIn("withdrawn", why)

    def test_dig_walks_a_dotted_path(self):
        self.assertEqual(F.dig({"card": {"is_active": 0}}, "card.is_active"), 0)
        self.assertIsNone(F.dig({"card": {}}, "card.is_active"))
        self.assertIsNone(F.dig({"card": 3}, "card.is_active"))

    def test_the_real_sweep_removes_every_inactive_card_in_one_pass(self):
        """The reviewer's own reproduction: three consecutive applies at the
        `likely` gate must remove the same set of cards, not 3 then 13."""
        if not (F.LIVE_SEED / "cards.json").exists():
            self.skipTest("no live catalogue")
        work = self.tmp / "live"
        shutil.copytree(F.LIVE_SEED, work / "seed")
        shutil.copytree(F.LIVE_NEWS, work / "news")
        F.discover_fixers = self._real_discover
        ids = lambda: {e.get("card", e).get("id")
                       for e in json.loads((work / "seed" / "cards.json").read_text())}
        start = ids()
        counts = []
        for _pass in range(3):
            run_main(["--apply", "--confidence", "likely", "--quiet",
                      "--seed-dir", str(work / "seed"),
                      "--news-dir", str(work / "news"),
                      "--backup", str(self.tmp / "bk")]
                     + (["--app-root", APP_ROOT] if APP_ROOT else []))
            counts.append(len(start - ids()))
        self.assertEqual(counts[0], counts[1], "pass 2 removed cards pass 1 did not")
        self.assertEqual(counts[1], counts[2])


# --------------------------------------------------------------------------- #
# 16. HALF OF A COUPLED CHANGE IS WORSE THAN NONE  (wrong-fixes lens, defect 8)
# --------------------------------------------------------------------------- #
class TestCoupledEditsAreNotAppliedAlone(FixtureCase):
    def test_an_edit_whose_partner_is_missing_is_held_back(self):
        e = an_edit(notes={"coupled_to": "reward_type retype (rate family)",
                           "coupled_code": "L5.RATE_CONTRADICTS_NAME"})
        self.use_fixers(stub_fixer("t_coupled", [e]))
        code, out = run_main(self.args("--apply"))
        self.assertIn("coupled partner", out)
        self.assertIsNone(self.cards()[0]["reward_rules"][0]["cap_amount"])
        self.assertIn(code, (0, 1))



# --------------------------------------------------------------------------- #
# 17. THE FIXERS THEMSELVES  (wrong-fixes lens, defects 7-16)
# --------------------------------------------------------------------------- #
import fixers.f1_units as F1                                     # noqa: E402
import fixers.f2_caps as F2                                      # noqa: E402
import fixers.f4_integrity as F4                                 # noqa: E402


def live_plan():
    """(edits, refusals-by-module) for the real catalogue. Cached across tests —
    a full plan is ~1s and eight tests want the same one."""
    if getattr(live_plan, "_cache", None) is None:
        if not (F.LIVE_SEED / "cards.json").exists():
            return None
        ctx, _notes = F.load_ctx(F.LIVE_SEED, F.LIVE_NEWS,
                                 Path(APP_ROOT) if APP_ROOT else None)
        findings, _sk, _c = F.validate(ctx)
        mods = F.discover_fixers()
        edits, _fail, extras = F.plan_all(ctx, findings, mods)
        live_plan._cache = (edits, extras)
    return live_plan._cache


class TestIssuerStatedNumbersAreNotOursToCorrect(unittest.TestCase):
    """equitas_powermiles carries the bank's own sentence — "Earn 3RP on every
    Rs.100 spent. 1 RP= 0.50" — fetched from equitas.bank.in. The fixer rewrote
    point_value 0.5 -> 0.25 at `certain`, halving what the app shows against the
    issuer's own words, and left the quote in the row contradicting it."""

    def card(self, quote, url="https://equitas.bank.in/x", pv=0.5):
        e = sample_card("eq", issuer="Equitas Small Finance Bank", rp_value_standard=pv)
        e["reward_rules"][0].update({
            "source_quote": quote, "source_url": url, "_sources": ["bank"],
            "point_value": pv, "reward_type": "points_per_spend",
            "reward_rate": 3.0, "reward_unit_spend": 100.0})
        return e, e["card"]

    def test_the_issuer_quote_is_read_off_the_row(self):
        entry, inner = self.card("Earn 3RP on every Rs.100 spent. 1 RP= 0.50")
        val, where = F1._issuer_priced(entry, inner)
        self.assertEqual(val, 0.5)
        self.assertIn("equitas.bank.in", where)

    def test_a_quote_on_a_non_issuer_domain_does_not_count(self):
        entry, inner = self.card("1 RP= 0.50", url="https://cardinsider.com/x")
        self.assertEqual(F1._issuer_priced(entry, inner), (None, None))

    def test_a_row_with_no_quote_does_not_count(self):
        entry, inner = self.card(None)
        self.assertEqual(F1._issuer_priced(entry, inner), (None, None))

    def test_the_three_equitas_cards_keep_their_issuer_stated_point_value(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        for cid in ("equitas_powermiles", "equitas_selfe", "equitas_tiga"):
            moved = [e for e in edits if e.card_id == cid
                     and e.field in ("rp_value_standard", "point_value")]
            self.assertEqual(moved, [], f"{cid}: {[e.anchor() for e in moved]}")


class TestAConditionalRouteIsNotTheUnconditionalOne(unittest.TestCase):
    """_best_unconditional's docstring promised "the best route a user can take
    with NO conditions attached" and read only channel_type, so three of the
    eight point-value corrections came from routes costing Rs 99-100 to use."""

    def rows(self, *rows):
        e = sample_card("c")
        e["redemption_rules"] = list(rows)
        return e

    def test_a_fee_bearing_cashback_row_is_skipped(self):
        entry = self.rows({"channel_type": "cashback", "point_value_inr": 0.3,
                           "redemption_fee_inr": 99.0, "min_points": None})
        self.assertEqual(F1._best_unconditional(entry), (None, None))

    def test_a_minimum_points_row_is_skipped(self):
        entry = self.rows({"channel_type": "cashback", "point_value_inr": 0.1,
                           "redemption_fee_inr": None, "min_points": 500})
        self.assertEqual(F1._best_unconditional(entry), (None, None))

    def test_a_free_row_still_counts(self):
        entry = self.rows({"channel_type": "statement_credit", "point_value_inr": 0.4,
                           "redemption_fee_inr": 0, "min_points": None})
        self.assertEqual(F1._best_unconditional(entry), (0.4, "statement_credit"))

    def test_the_three_fee_bearing_cards_keep_their_point_value(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        for cid in ("hdfc_bank_rupay_irctc", "indusind_bank_platinum", "equitas_tiga"):
            moved = [e for e in edits
                     if e.card_id == cid and e.field == "rp_value_standard"]
            self.assertEqual(moved, [], f"{cid} still derives from a conditional route")


class TestNoRateIsBuiltOnTheAppsInvented25Paise(unittest.TestCase):
    """21 of 47 base-rule rewrites computed their target from `_sane_pv`'s
    fallback on cards that never priced a point — the exact derivation the two
    sibling stages in the same file refuse outright."""

    NAMED = ["amex_centurion", "amex_gold", "amex_platinum_reserve", "amex_smartearn",
             "au_small_finance_bank_lit", "axis_bank_atlas", "axis_bank_my_wings",
             "axis_bank_olympus", "axis_bank_primus", "federal_bank_rupay_wave",
             "hdfc_bank_all_miles", "hdfc_bank_doctors_superia",
             "hdfc_bank_paytm_hdfc_bank", "hdfc_bank_pixel_play",
             "hsbc_bank_visa_platinum", "hsbc_rupay_platinum",
             "indusind_bank_eazydiner_indusind_platinum", "rbl_bank_iglobe",
             "yes_bank_uni", "yes_bank_uni_rupay"]

    def test_a_card_with_no_point_value_gets_no_base_rule_rewrite(self):
        entry = sample_card("c", rp_value_standard=None, base_reward_rate=0.01,
                            reward_currency="reward_points")
        entry["reward_rules"][0]["rule_type"] = "base_rate"
        edits, refused = [], []
        F1._stage_base_field(entry, entry["card"], "c", edits, refused)
        self.assertEqual(edits, [])
        self.assertIn("25 paise", refused[0].why)

    def test_a_point_value_outside_the_band_is_not_evidence_either(self):
        entry = sample_card("c", rp_value_standard=5.0, base_reward_rate=0.01,
                            reward_currency="reward_points")
        entry["reward_rules"][0]["rule_type"] = "base_rate"
        edits, refused = [], []
        F1._stage_base_field(entry, entry["card"], "c", edits, refused)
        self.assertEqual(edits, [])

    def test_a_rewrite_may_not_contradict_the_rules_own_sentence(self):
        entry = sample_card("c", rp_value_standard=0.25, base_reward_rate=0.01,
                            reward_currency="reward_points")
        entry["reward_rules"][0].update({
            "rule_type": "base_rate",
            "rule_name": "Unlimited 1% Reward points (Uni coins)",
            "reward_type": "cashback_pct", "reward_rate": 0.01})
        edits, refused = [], []
        F1._stage_base_field(entry, entry["card"], "c", edits, refused)
        self.assertEqual(edits, [])
        self.assertIn("contradict", refused[0].why)

    def test_stated_pct_reads_the_name_then_the_quote(self):
        self.assertEqual(F1._stated_pct({"rule_name": "Unlimited 1% Reward points"}), 1.0)
        self.assertEqual(F1._stated_pct({"rule_name": "Base reward rate",
                                         "source_quote": "Earn 2% back"}), 2.0)
        self.assertIsNone(F1._stated_pct({"rule_name": "Base reward rate"}))

    def test_none_of_the_twenty_named_cards_is_rewritten_any_more(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        hit = sorted({e.card_id for e in edits
                      if e.code == "L4.BASE_FIELD_VS_BASE_RULE"
                      and e.card_id in self.NAMED})
        self.assertEqual(hit, [])

    def test_the_stage_still_repairs_a_card_that_does_price_a_point(self):
        """Narrowed, not deleted. Stated against a FIXTURE and not against the
        live catalogue on purpose: this run repairs the live file, so a test that
        needed real defects to still exist would pass once and then rot."""
        entry = sample_card("c", rp_value_standard=0.2, base_reward_rate=0.02666666,
                            reward_currency="reward_points")
        entry["reward_rules"][0].update({
            "rule_type": "base_rate", "rule_name": "Base reward rate",
            "reward_type": "cashback_pct", "reward_rate": 0.02666666})
        edits, refused = [], []
        F1._stage_base_field(entry, entry["card"], "c", edits, refused)
        self.assertEqual(len(edits), 1, [r.why for r in refused])
        self.assertEqual(edits[0].new_value["reward_type"], "points_per_spend")
        self.assertAlmostEqual(edits[0].notes["renders_after_pct"], 0.5333332, places=5)
        self.assertLess(edits[0].notes["renders_after_pct"],
                        edits[0].notes["renders_before_pct"])


class TestARaiseNeedsARealWitness(unittest.TestCase):
    """The one stage that raises a published rate rested on point values the file
    itself tags _sources: ['cardinsider'], and on a Flipkart gift_card row."""

    def test_an_aggregator_tagged_route_is_not_a_witness(self):
        entry = sample_card("c", rp_value_standard=1.0)
        entry["redemption_rules"] = [{"channel_type": "cashback", "point_value_inr": 1.0,
                                      "redemption_fee_inr": None, "min_points": None,
                                      "_sources": ["cardinsider"]}]
        val, why = F1._pv_witness(entry, entry["card"])
        self.assertIsNone(val)
        self.assertIn("card-review site", why)

    def test_a_voucher_only_card_is_not_a_witness(self):
        entry = sample_card("c", rp_value_standard=1.0)
        entry["redemption_rules"] = [{"channel_type": "gift_card", "point_value_inr": 1.0,
                                      "_sources": ["cardinsider"]}]
        val, _why = F1._pv_witness(entry, entry["card"])
        self.assertIsNone(val)

    def test_a_clean_cashback_route_is_a_witness(self):
        entry = sample_card("c", rp_value_standard=0.4)
        entry["redemption_rules"] = [{"channel_type": "cashback", "point_value_inr": 0.4,
                                      "redemption_fee_inr": None, "min_points": None,
                                      "_sources": ["bank"]}]
        val, why = F1._pv_witness(entry, entry["card"])
        self.assertEqual(val, 0.4)
        self.assertIn("no fee", why)

    def test_a_threshold_phrase_in_the_name_is_detected(self):
        self.assertIsNotNone(F1._threshold_phrase(
            "10 Hand-picked Rewards per Rs. 100 after Rs. 5 lakhs total spend"))
        self.assertIsNotNone(F1._threshold_phrase(
            "12 SuperCoins per Rs. 100 on Flipkart for Plus members"))
        self.assertIsNone(F1._threshold_phrase("5 Reward Points per Rs. 200 spent"))

    def test_no_rate_is_switched_on_above_the_review_ceiling(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        loud = [e for e in edits if e.code == "L5.NAME_STATES_RATE_APP_HAS_NONE"
                and (e.notes or {}).get("renders_after_pct", 0) > F1.REVIEW_PCT]
        self.assertEqual(loud, [], [e.anchor() for e in loud])

    def test_the_twelve_percent_flipkart_raise_is_gone(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        bad = [e for e in edits
               if e.card_id in ("axis_bank_flipkart_axis_bank_super_elite",
                                "kotak_mahindra_bank_essentia_platinum",
                                "icici_bank_visa_signature",
                                "axis_bank_spicejet_axis_bank_voyage_black")
               and e.code == "L5.NAME_STATES_RATE_APP_HAS_NONE"]
        self.assertEqual(bad, [], [e.anchor() for e in bad])


class TestACapKeepsItsUnit(unittest.TestCase):
    """"2000 Fuel Points per month" became 2000.0 on a cashback_pct rule the app
    reads in rupees — an unenforceable cap turned into an enforceable one at
    about twice the issuer's ceiling, on the two IndianOil cards this repo has
    already had an incident on."""

    def one_card(self, **rule):
        e = sample_card("io")
        e["reward_rules"][0].update(rule)
        return e

    def plan_for(self, entry):
        class _Ctx:
            def entries(_self):
                yield 0, entry, entry["card"], entry["card"]["id"]
            cards = [entry]
        findings = [{"code": "L4.CAP_NOT_A_NUMBER", "card_id": entry["card"]["id"]},
                    {"code": "L5.CAP_UNIT_MISMATCH", "card_id": entry["card"]["id"]}]
        return F2._analyse(_Ctx(), findings)

    def test_a_points_cap_on_a_rupee_rule_is_refused(self):
        entry = self.one_card(cap_amount="2000 Fuel Points per month",
                              reward_type="cashback_pct", reward_rate=0.075,
                              rule_name="15 Fuel Points/Rs100 spent at IndianOil")
        edits, refused = self.plan_for(entry)
        self.assertEqual([e for e in edits if e.field == "cap_amount"], [])
        self.assertTrue(any("fuel points" in r.why.lower() for r in refused))

    def test_a_rupee_cap_on_a_rupee_rule_is_still_repaired(self):
        entry = self.one_card(cap_amount="Rs 5,000 per month",
                              reward_type="cashback_pct", reward_rate=0.05,
                              rule_name="5% cashback on fuel")
        edits, _refused = self.plan_for(entry)
        got = [e for e in edits if e.field == "cap_amount"]
        self.assertEqual([e.new_value for e in got], [5000.0])

    def test_a_points_cap_on_a_points_rule_is_still_repaired(self):
        entry = self.one_card(cap_amount="2000 Fuel Points per month",
                              reward_type="points_per_spend", reward_rate=15.0,
                              reward_unit_spend=100.0,
                              rule_name="15 Fuel Points per Rs 100")
        edits, _refused = self.plan_for(entry)
        got = [e for e in edits if e.field == "cap_amount"]
        self.assertEqual([e.new_value for e in got], [2000.0])

    def test_the_points_unit_words_are_recognised(self):
        for text in ("100 FP per month", "2000 Fuel Points per month",
                     "500 RP a month", "1,000 reward points", "50 miles",
                     "200 SuperCoins"):
            self.assertIsNotNone(F2._POINTS_UNIT.search(text), text)
        for text in ("Rs 5,000 per month", "₹4,000 per month", "5000 per month"):
            self.assertIsNone(F2._POINTS_UNIT.search(text), text)

    def test_the_three_indianoil_rows_are_not_edited_on_the_live_file(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        bad = [e for e in edits if e.code == "L4.CAP_NOT_A_NUMBER"
               and e.card_id in ("hdfc_bank_indianoil_hdfc_bank",
                                 "rbl_bank_indianoil_rbl_bank_xtra")]
        self.assertEqual(bad, [], [e.anchor() for e in bad])

    def test_the_cap_family_still_reads_a_period_out_of_the_same_sentence(self):
        """The R1 repair is narrowed by unit, not switched off: a cap the app can
        honestly enforce still gets both its number and its period."""
        entry = self.one_card(cap_amount="Rs 5,000 per month", cap_period=None,
                              reward_type="cashback_pct", reward_rate=0.05,
                              rule_name="5% cashback on fuel")
        edits, _refused = self.plan_for(entry)
        got = {e.field: e.new_value for e in edits}
        self.assertEqual(got.get("cap_amount"), 5000.0)
        self.assertEqual(got.get("cap_period"), "month")


class TestOneCardCannotCarryTwoReadingsOfOneSentence(unittest.TestCase):
    """cap_kind='spend' landed on two rules of idfc_first_bank_hpcl_first_power
    and not on the two beside them carrying the identical sentence, leaving one
    card with ceilings 40x apart for one issuer's wording."""

    def test_the_cap_phrase_ignores_the_number(self):
        a = F2._cap_phrase("Save 5% on fuel up to a maximum of Rs 5,000 per month")
        b = F2._cap_phrase("Save 2.5% on utilities up to a maximum of Rs 4,000 per month")
        self.assertIsNotNone(a)
        self.assertEqual(a, b)

    def test_an_unrelated_sentence_does_not_match(self):
        self.assertIsNone(F2._cap_phrase("5 Reward Points per Rs 200 spent"))

    def test_the_idfc_rows_are_left_alone_on_the_live_file(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        bad = [e for e in edits if e.field == "cap_kind"
               and e.card_id == "idfc_first_bank_hpcl_first_power"]
        self.assertEqual(bad, [], [e.anchor() for e in bad])

    def test_a_per_transaction_cap_is_never_retyped_as_spend(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        bad = [e for e in edits if e.field == "cap_kind"
               and e.card_id == "axis_bank_aura"]
        self.assertEqual(bad, [])


class TestProvenanceTagsSurvive(unittest.TestCase):
    """`_sources: ['bank']` is the only machine-readable marker of the 26
    issuer-sourced reward rates, and it is the field the only-go-DOWN rule has to
    be evaluated against. Deleting it for not being a URL made that rule
    unenforceable on the next run."""

    def test_bank_is_provenance_not_a_broken_url(self):
        self.assertEqual(F4._placeholder_sources({"_sources": ["bank"]}), [])
        self.assertEqual(F4._placeholder_sources({"_sources": ["issuer"]}), [])

    def test_a_genuinely_meaningless_token_is_still_caught(self):
        self.assertEqual(F4._placeholder_sources({"_sources": ["tbd"]}), ["tbd"])

    def test_a_real_url_is_left_alone(self):
        self.assertEqual(
            F4._placeholder_sources({"_sources": ["https://hdfcbank.com/x"]}), [])

    def test_no_sources_key_is_deleted_from_the_live_file(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        gone = [e for e in edits if e.field == "_sources" and e.new_value is None]
        self.assertEqual(gone, [], [e.anchor() for e in gone])


class TestTheUnreadBadgeIsNotBackdated(unittest.TestCase):
    """news_feed_service.dart:150 sorts on publishedAt and :176 computes the
    unread badge as publishedAt.isAfter(_lastSeen). Overwriting it on 29 of 32
    items — one back to 2024-02-05 — reorders the feed and zeroes the badge for
    every existing user."""

    def test_published_at_is_never_the_target(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        bad = [e for e in edits if e.block == "news" and e.field == "published_at"]
        self.assertEqual(bad, [], [e.anchor() for e in bad])

    def test_the_id_date_is_recorded_as_effective_date_instead(self):
        """Driven from a synthetic feed, so it keeps testing the edit's SHAPE
        after the live feed has been repaired and stops producing findings."""
        item = {"id": "news_icici_bank_2024_02_05_government_payments_now_da997f",
                "title": "t", "published_at": "2026-08-17T03:30:47Z"}

        class _Ctx:
            news = {"version": "1.0.0", "items": [item]}

        out = []
        F4._s8_news_dates(_Ctx(), [{"code": "L9.NEWS_DATE_CONTRADICTS_ITS_OWN_ID"}], out)
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual(e.field, "effective_date")
        self.assertEqual(e.new_value, "2024-02-05")
        self.assertEqual(e.confidence, LIKELY)
        self.assertIn("why_not_published_at", e.notes)

    def test_the_live_feed_kept_every_published_at_it_had(self):
        if not (F.LIVE_NEWS / "feed.json").exists():
            self.skipTest("no live feed")
        feed = json.loads((F.LIVE_NEWS / "feed.json").read_text(encoding="utf-8"))
        items = feed["items"] if isinstance(feed, dict) else feed
        self.assertTrue(all(i.get("published_at") for i in items))


class TestEvidenceSaysWhatIsActuallyThere(unittest.TestCase):
    """The one sourced reward rule the confidence sweep downgrades was told
    "issuer link: none" while its row holds ixigo.com — the co-brand partner's
    own product page for a card named 'ixigo AU'."""

    def test_the_edit_names_the_host_it_found(self):
        """Fixture-driven: the live row this was written about is repaired by the
        very sweep this suite guards, so the assertion is about the SENTENCE the
        fixer writes, not about that row still being broken."""
        entry = sample_card("au_small_finance_bank_ixigo_au",
                            issuer="AU Small Finance Bank")
        entry["reward_rules"][0].update({
            "rule_name": "5 Reward Points on every Rs 200 spent",
            "confidence": "high",
            "_sources": ["https://www.ixigo.com/travel-credit-card"]})

        class _Ctx:
            def entries(_self):
                yield 0, entry, entry["card"], entry["card"]["id"]

        out = []
        F4._s2_unearned_high(
            _Ctx(),
            [{"code": "L8.CONFIDENCE_HIGH_UNSOURCED",
              "card_id": "au_small_finance_bank_ixigo_au"}], out)
        self.assertEqual(len(out), 1)
        self.assertNotIn("issuer link: none", out[0].evidence)
        self.assertIn("ixigo.com", out[0].evidence)
        self.assertIn("not the issuer's own domain", out[0].evidence)

    def test_a_row_with_no_link_at_all_still_says_so(self):
        entry = sample_card("c", issuer="HDFC Bank")
        entry["reward_rules"][0]["confidence"] = "high"

        class _Ctx:
            def entries(_self):
                yield 0, entry, entry["card"], "c"

        out = []
        F4._s2_unearned_high(
            _Ctx(), [{"code": "L8.CONFIDENCE_HIGH_UNSOURCED", "card_id": "c"}], out)
        self.assertEqual(len(out), 1)
        self.assertIn("no link of any kind", out[0].evidence)



# --------------------------------------------------------------------------- #
# f5_exclusions — the family-aware exclusion guardrail
#
# Every class here is a scar or a near-scar. The exclusion switch in
# recommendation_engine.dart:486-497 reads 'mcc' and 'category' and nothing
# else, and _isExcluded runs at STEP 1 (recommendation_engine.dart:308-309),
# BEFORE any reward rule is looked at. So a wrong exclusion does not show a
# wrong number — it removes the card from the pick screen, and the user never
# finds out why. These tests exist to make that specific harm expensive to
# reintroduce.
# --------------------------------------------------------------------------- #
import fixers.f5_exclusions as F5                                # noqa: E402
from checks.base import Ctx as _RealCtx                          # noqa: E402

# The app's real tree, as of assets/data/categories/categories.json: 25
# categories, int ids, parent_id pointing at another int id. Only the branches
# the tests need are spelled out, but they are spelled out EXACTLY as the app
# ships them, because a stub tree that flattened the hierarchy would make the
# family tests pass for the wrong reason.
APP_TREE = [
    {"id": 1, "parent_id": None, "category_name": "dining"},
    {"id": 2, "parent_id": 1, "category_name": "food_delivery"},
    {"id": 3, "parent_id": None, "category_name": "grocery"},
    {"id": 4, "parent_id": None, "category_name": "fuel"},
    {"id": 5, "parent_id": None, "category_name": "travel"},
    {"id": 6, "parent_id": 5, "category_name": "airlines"},
    {"id": 7, "parent_id": 5, "category_name": "railways"},
    {"id": 8, "parent_id": 5, "category_name": "cabs"},
    {"id": 9, "parent_id": 5, "category_name": "hotels"},
    {"id": 10, "parent_id": None, "category_name": "online_shopping"},
    {"id": 14, "parent_id": None, "category_name": "utilities"},
    {"id": 15, "parent_id": None, "category_name": "insurance"},
    {"id": 16, "parent_id": None, "category_name": "education"},
    {"id": 17, "parent_id": None, "category_name": "government"},
    {"id": 18, "parent_id": None, "category_name": "wallet_load"},
    {"id": 19, "parent_id": None, "category_name": "jewellery"},
    {"id": 20, "parent_id": None, "category_name": "rent"},
    {"id": 21, "parent_id": None, "category_name": "pharmacy"},
    {"id": 23, "parent_id": None, "category_name": "telecom"},
]

# seed/merchants.json rows key on 'merchant_name' — there is no 'slug' field —
# and 'category_id' on a merchant row IS the app category NAME.
APP_MERCHANTS = {"merchants": [
    {"merchant_name": "swiggy", "category_id": "food_delivery"},
    {"merchant_name": "irctc", "category_id": "railways"},
    {"merchant_name": "paytm", "category_id": "wallet_load"},
]}


def f5_ctx(*entries, categories=None, merchants=None):
    """A real Ctx — the same class every check and fixer reads — around a few
    hand-built card entries. Using the real class rather than a stub means a
    change to the Ctx interface breaks these tests instead of silently making
    them test nothing."""
    return _RealCtx(
        seed_dir=Path("/nonexistent"), news_dir=Path("/nonexistent"),
        cards=list(entries),
        merchants=APP_MERCHANTS if merchants is None else merchants,
        manifest={}, news=None,
        app_categories=APP_TREE if categories is None else categories,
        app_root=None, categories_origin="app",
        categories_path=Path("/nonexistent/categories.json"),
    )


def excl_card(card_id, exclusions, rules=None):
    """A card entry with the exclusion rows and reward rules a test needs."""
    e = sample_card(card_id)
    e["exclusion_rules"] = [dict(r) for r in exclusions]
    if rules is not None:
        e["reward_rules"] = [dict(r) for r in rules]
    return e


def inert(value, etype="other", **extra):
    row = {"exclusion_type": etype, "exclusion_value": value,
           "also_excludes_from_threshold": 0}
    row.update(extra)
    return row


def earns(category=None, rate=1.0, merchant_ref=None, name="A rule"):
    return {"rule_name": name, "rule_type": "category_bonus",
            "category_id": category, "merchant_ref": merchant_ref,
            "reward_type": "cashback_pct", "reward_rate": rate,
            "channel": None, "category_ref": None, "priority": 10}


def f5_findings(*card_ids, code="L6.EXCLUSION_TYPE_INERT"):
    return [{"code": code, "card_id": c, "block": "exclusion_rules"}
            for c in card_ids]


class NoCession(unittest.TestCase):
    """Base for the tests that are about f5's OWN table and guardrail.

    Measured on the real catalogue: every one of the 346 rows f5's whole-string
    table maps is ALSO mapped by f3_reach's looser regex table (f5-only: 0,
    disagreements on target: 0), so with f3 present f5's forward half correctly
    stands down on every row and emits nothing at all. That cession is a
    guarantee with its own test class below — TestF5AndF3NeverWriteTheSameRow —
    and it is exactly why it has to be suspended here: a test of the mapping
    that ran with the cession live would assert on an empty list and pass
    whatever the table said.
    """

    def setUp(self):
        real = F5._f3_owns
        F5._f3_owns = lambda value, cats: False
        self.addCleanup(setattr, F5, "_f3_owns", real)


class TestTheGuardrailWalksTheCategoryFamily(NoCession):
    """The whole reason this module exists.

    f3_reach's guardrail asks "does the card pay on a category with THIS NAME?"
    and the app's categories are a TREE: 'railways' is a child of 'travel',
    'food_delivery' is a child of 'dining'. A card whose only reward rule is
    `category_id: travel` and whose exclusion list says 'railways' passes a
    name-exact guardrail because the strings differ — and then the engine
    removes that card at every railway merchant, including the travel rule that
    is the whole reason somebody carries it.

    Measured on this branch: exactly that happened twice, to
    kotak_mahindra_bank_royale_signature and rbl_bank_world_safari.
    """

    def test_a_child_is_in_its_parents_family(self):
        fam = F5.family_index(APP_TREE)
        self.assertIn("travel", fam["railways"])
        self.assertIn("railways", fam["travel"])
        self.assertIn("dining", fam["food_delivery"])

    def test_siblings_are_not_family(self):
        # A card that pays on flights has no claim on train tickets. If
        # siblings counted, every travel exclusion on every airline card would
        # be blocked and the guardrail would be useless.
        fam = F5.family_index(APP_TREE)
        self.assertNotIn("airlines", fam["railways"])
        self.assertNotIn("railways", fam["airlines"])

    def test_an_unrelated_category_is_not_family(self):
        fam = F5.family_index(APP_TREE)
        self.assertNotIn("rent", fam["government"])
        self.assertNotIn("fuel", fam["jewellery"])

    def test_excluding_a_child_while_earning_the_parent_is_blocked(self):
        entry = excl_card("child_excl", [inert("railways")],
                          rules=[earns("travel", 5.0)])
        edits = F5.plan(f5_ctx(entry), f5_findings("child_excl"))
        self.assertEqual(edits, [], "excluding railways would kill the travel rule")

    def test_excluding_a_parent_while_earning_the_child_is_blocked(self):
        # The reverse walk. A card paying on railways that excludes 'travel'
        # would be switched off at the station too — _isExcluded matches the
        # exclusion's own category, and the app files IRCTC under both.
        entry = excl_card("parent_excl", [inert("railway transactions")],
                          rules=[earns("railways", 5.0)])
        edits = F5.plan(f5_ctx(entry), f5_findings("parent_excl"))
        self.assertEqual(edits, [], "excluding a family the card earns in")

    def test_an_unrelated_earn_does_not_block(self):
        # The guardrail has to be survivable, not universal. A card that pays
        # on dining and excludes railways gets its exclusion made real.
        entry = excl_card("ok", [inert("railways")], rules=[earns("dining", 5.0)])
        edits = F5.plan(f5_ctx(entry), f5_findings("ok"))
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].new_value["exclusion_value"], "railways")


class TestTheThreeNamedGuardrailCasesStayInert(NoCession):
    """The three cards the audit named, each reproduced from its real shape in
    seed/cards.json. Each one maps cleanly onto an app category and each one
    must still produce NO edit, for a different reason:

        idfc_first_bank_millennia            excludes fuel, EARNS fuel (exact)
        indusind_bank_eazydiner_indusind_platinum
                                             excludes government, EARNS government
        rbl_bank_world_safari                excludes railways, EARNS travel —
                                             the PARENT, and only the family
                                             walk catches it
    """

    def case(self, cid, value, rules):
        entry = excl_card(cid, [inert(value)], rules=rules)
        return F5.plan(f5_ctx(entry), f5_findings(cid))

    def test_idfc_first_bank_millennia_keeps_its_fuel_rewards(self):
        # "1X Reward Points per Rs 200 spent on utilities, insurance, FASTag
        # recharges and fuel" — the card's own rule pays on fuel.
        self.assertEqual(self.case(
            "idfc_first_bank_millennia", "fuel purchases",
            [earns("utilities"), earns("insurance"), earns("fuel"),
             earns("railways")]), [])

    def test_indusind_eazydiner_keeps_its_government_rewards(self):
        # "Earn 0.7 Reward Points on spending towards insurance, rent,
        # utilities, and government payments".
        self.assertEqual(self.case(
            "indusind_bank_eazydiner_indusind_platinum", "government payments",
            [earns("insurance", 0.7), earns("rent", 0.7),
             earns("utilities", 0.7), earns("government", 0.7),
             earns("dining", 2.0)]), [])

    def test_rbl_world_safari_keeps_its_travel_rewards(self):
        # "5 Travel Points on every Rs 100 spent on travel" is this card's ONLY
        # reward rule, and railways is a child of travel.
        self.assertEqual(self.case(
            "rbl_bank_world_safari", "railways",
            [earns("travel", 5.0)]), [])

    def test_all_three_would_otherwise_have_mapped(self):
        # Proof the three above are blocked by the GUARDRAIL and not merely
        # unrecognised — a table that had simply failed to match them would
        # pass the three tests above for the wrong reason.
        names = {c["category_name"] for c in APP_TREE}
        for value, target in (("fuel purchases", "fuel"),
                              ("government payments", "government"),
                              ("railways", "railways")):
            with self.subTest(value=value):
                verdict, got, _conf, _d = F5.map_exclusion_value(value, names)
                self.assertEqual((verdict, got), ("category", target))


class TestAMerchantRefIsAnEarning(NoCession):
    """A card can earn in a category without ever naming it: the rule points at
    a merchant, and seed/merchants.json says what that merchant is. The key on
    a merchant row is 'merchant_name' — there is no 'slug' field, and a helper
    that looked for one returned an empty set for all 273 rows, which made
    every merchant branch of every guardrail unreachable code."""

    def test_a_merchant_only_earn_blocks_the_exclusion(self):
        entry = excl_card("m_only", [inert("railways")],
                          rules=[earns(None, 5.0, merchant_ref="irctc")])
        self.assertEqual(F5.plan(f5_ctx(entry), f5_findings("m_only")), [],
                         "irctc resolves to railways through merchants.json")

    def test_a_merchant_only_earn_blocks_through_the_family_too(self):
        # swiggy -> food_delivery -> child of dining. Excluding dining on a
        # card whose only earn is a Swiggy rule must be blocked.
        entry = excl_card("m_fam", [inert("wallet reloads")],
                          rules=[earns(None, 5.0, merchant_ref="paytm")])
        self.assertEqual(F5.plan(f5_ctx(entry), f5_findings("m_fam")), [],
                         "paytm resolves to wallet_load through merchants.json")

    def test_an_unknown_merchant_ref_does_not_invent_an_earning(self):
        entry = excl_card("m_unknown", [inert("railways")],
                          rules=[earns(None, 5.0, merchant_ref="not_in_the_file")])
        self.assertEqual(len(F5.plan(f5_ctx(entry), f5_findings("m_unknown"))), 1)

    def test_a_rule_that_pays_zero_guards_nothing(self):
        # A rule paying 0 cannot lose anything, so it must not block a fix. A
        # rule with NO rate at all is different: absent is not zero, and
        # unknown has to fail towards leaving the card alone.
        zero = excl_card("z", [inert("railways")], rules=[earns("travel", 0.0)])
        self.assertEqual(len(F5.plan(f5_ctx(zero), f5_findings("z"))), 1)
        unknown = excl_card("u", [inert("railways")], rules=[earns("travel", None)])
        self.assertEqual(F5.plan(f5_ctx(unknown), f5_findings("u")), [])


class TestConceptsTheAppCannotExpressAreNeverForced(NoCession):
    """Roughly 440 of the 983 inert rows name something the app's merchant model
    has no field for at all. Forcing one into a near-miss category is the worst
    outcome available: 'gift cards' filed as wallet_load switches off Paytm and
    PhonePe for a user whose bank only ever excluded gift vouchers.

    These stay inert, get counted, and get reported as an app feature request.
    """

    NEVER = [
        ("gift cards", "a gift card is a PURCHASE of a stored-value "
                       "instrument; a wallet load is a TRANSFER into one"),
        ("emi", "the app has no instalment field"),
        ("emi transactions", "same"),
        ("cash withdrawals", "quasi-cash, not a merchant category"),
        ("cash advance", "same"),
        ("balance transfer", "a lending product, not spending"),
        ("contracted services", "no app category holds this"),
        ("wallet cash withdrawals", "names a wallet, but it is about CASH"),
        ("tolls", "the app's travel is OTA bookings, not toll plazas"),
        ("international transactions", "a channel, not a category"),
        ("hospitals", "pharmacy also holds Apollo, 1mg and the labs"),
        ("movies", "entertainment also holds Netflix and Spotify"),
        ("wholesale clubs", "not in grocery's MCC set"),
        ("miscellaneous", "names nothing at all"),
    ]

    def test_none_of_them_map_to_a_category(self):
        names = {c["category_name"] for c in APP_TREE}
        for value, why in self.NEVER:
            with self.subTest(value=value):
                verdict, target, _c, _d = F5.map_exclusion_value(value, names)
                self.assertNotEqual(verdict, "category", f"{value!r}: {why}")
                self.assertIsNone(target)

    def test_none_of_them_produce_an_edit(self):
        rows = [inert(v) for v, _ in self.NEVER]
        entry = excl_card("nope", rows, rules=[earns("dining", 5.0)])
        self.assertEqual(F5.plan(f5_ctx(entry), f5_findings("nope")), [])

    def test_the_refusals_are_counted_and_named(self):
        # A refusal nobody can count gets re-derived from scratch every quarter.
        rows = [inert(v) for v, _ in self.NEVER]
        entry = excl_card("nope", rows, rules=[earns("dining", 5.0)])
        c = F5.census(f5_ctx(entry), f5_findings("nope"))
        self.assertGreater(sum(c["app_cannot_express"].values()), 0)
        self.assertIn("EMI / instalment conversion", c["app_cannot_express"])
        self.assertIn("Cash & quasi-cash", c["app_cannot_express"])


class TestOnlyWholeStringsMatch(unittest.TestCase):
    """No substring search, no edit distance, no fuzzy anything.

    A substring matcher reading "fuel purchases at non-BPCL fuel stations" sees
    'fuel' and switches a fuel card off at every pump — that is the BPCL Octane
    near-miss, verbatim. One reading "utility bill payments (reduced rate)" sees
    'utility' and turns a RATE CHANGE into a flat exclusion, which inverts the
    issuer's meaning: the spend still earns, just less.
    """

    SCOPED = [
        "fuel purchases at non-BPCL fuel stations",
        "fuel spends at non-Jio-BP fuel stations",
        "fuel transactions (except HPCL Energie credit card)",
        "utility bill payments (reduced rate)",
        "insurance premiums (reduced to 1 InterMile per 100)",
        "rent payments via Freecharge app (excluded from cashback as per T&C update)",
        "government spends like advance tax",
        "educational transactions (from October 11, 2025)",
        "utility spends beyond Rs 35,000 per billing cycle",
        "wallet cash withdrawals",
    ]

    def test_a_scoped_or_conditional_value_never_maps(self):
        names = {c["category_name"] for c in APP_TREE}
        for value in self.SCOPED:
            with self.subTest(value=value):
                verdict, _t, _c, _d = F5.map_exclusion_value(value, names)
                self.assertNotEqual(verdict, "category")

    def test_the_bare_phrasing_of_the_same_word_does_map(self):
        # Proof the test above is about the SCOPE and not about the noun.
        names = {c["category_name"] for c in APP_TREE}
        for value, target in (("fuel purchases", "fuel"),
                              ("utility bill payments", "utilities"),
                              ("rent payments", "rent"),
                              ("educational", "education")):
            with self.subTest(value=value):
                self.assertEqual(
                    F5.map_exclusion_value(value, names)[:2], ("category", target))

    def test_normalisation_is_case_space_and_trailing_punctuation_only(self):
        names = {c["category_name"] for c in APP_TREE}
        for value in ("  Rent Payments  ", "RENT   PAYMENTS.", "Rent payments;"):
            with self.subTest(value=value):
                self.assertEqual(
                    F5.map_exclusion_value(value, names)[:2], ("category", "rent"))


class TestF5AndF3NeverWriteTheSameRow(unittest.TestCase):
    """Two fixers writing one row is not a merge conflict — the runner resolves
    collisions by position and the second write silently wins, so the diff a
    human reviewed and the diff that landed would differ.

    f5 asks f3, per row, and stands down. The cession is COUNTED rather than
    implied, so a future change to either table shows up as a number moving."""

    def test_a_row_f3_would_map_produces_no_f5_edit(self):
        import fixers.f3_reach as F3
        names = {c["category_name"] for c in APP_TREE}
        value = "rent payments"
        self.assertEqual(F3.classify_exclusion(value, names)[0], "category")
        self.assertEqual(F5.map_exclusion_value(value, names)[0], "category")
        entry = excl_card("ceded", [inert(value)], rules=[earns("dining")])
        self.assertEqual(F5.plan(f5_ctx(entry), f5_findings("ceded")), [],
                         "f3 owns this row; f5 must not write it too")

    def test_the_cession_is_visible_in_the_census(self):
        entry = excl_card("ceded", [inert("rent payments")], rules=[earns("dining")])
        c = F5.census(f5_ctx(entry), f5_findings("ceded"))
        self.assertEqual(c["tally"].get("forward.ceded_to_f3"), 1)
        self.assertEqual(c["tally"].get("forward.would_map"), 1)

    def test_the_guardrail_is_checked_before_the_cession(self):
        # Load-bearing order. Ceding first would make f5 SILENT about exactly
        # the rows f3 is about to write against the family guardrail — which is
        # how the two railways rows got into the file. The outcome is 'no edit'
        # either way; the difference is whether anyone is told.
        entry = excl_card("rbl", [inert("railways")], rules=[earns("travel", 5.0)])
        c = F5.census(f5_ctx(entry), f5_findings("rbl"))
        self.assertEqual(c["tally"].get("forward.guardrail_blocked"), 1)
        self.assertEqual(c["tally"].get("forward.ceded_to_f3", 0), 0)
        self.assertEqual(c["guardrail_blocked_cases"],
                         ["rbl excl railways earns travel"])

    def test_neither_fixer_writes_the_same_anchor_on_the_real_catalogue(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        seen = {}
        for e in edits:
            key = (e.card_id, e.block, e.index, e.field)
            if key in seen:
                self.fail(f"{e.anchor()} written by both "
                          f"{seen[key]} and {getattr(e, 'module', '?')}")
            seen[key] = getattr(e, "module", "?")


class TestAnAlreadyLiveRowIsLeftAlone(NoCession):
    """Idempotence, and the reason it is not merely tidy: applying a plan twice
    must be a no-op, or nobody can reason about what a rerun does."""

    def test_a_category_row_produces_no_edit(self):
        entry = excl_card("done", [{"exclusion_type": "category",
                                    "exclusion_value": "rent",
                                    "also_excludes_from_threshold": 0}],
                          rules=[earns("dining")])
        self.assertEqual(F5.plan(f5_ctx(entry), f5_findings("done")), [])

    def test_an_mcc_row_produces_no_edit(self):
        entry = excl_card("done2", [{"exclusion_type": "mcc",
                                     "exclusion_value": "6513",
                                     "also_excludes_from_threshold": 0}],
                          rules=[earns("dining")])
        self.assertEqual(F5.plan(f5_ctx(entry), f5_findings("done2")), [])

    def test_planning_twice_over_an_applied_edit_returns_nothing(self):
        entry = excl_card("twice", [inert("railway bookings")],
                          rules=[earns("dining")])
        edits = F5.plan(f5_ctx(entry), f5_findings("twice"))
        self.assertEqual(len(edits), 1)
        applied = excl_card("twice", [edits[0].new_value], rules=[earns("dining")])
        self.assertEqual(F5.plan(f5_ctx(applied), f5_findings("twice")), [])


class TestTheRowKeepsEverythingElseItHad(NoCession):
    """All 983 inert rows carry also_excludes_from_threshold = 0, so they are
    pure reward-exclusions and not milestone-threshold exclusions. The app parses
    that field at credit_card.dart:243-244. Dropping it while retyping the row
    would silently change what the exclusion DOES as well as whether it runs."""

    def row(self):
        entry = excl_card("keep", [inert("railway bookings",
                                         also_excludes_from_threshold=0,
                                         notes="issuer T&C clause 4.2",
                                         source_url="https://bank.example/x")],
                          rules=[earns("dining")])
        edits = F5.plan(f5_ctx(entry), f5_findings("keep"))
        self.assertEqual(len(edits), 1)
        return edits[0]

    def test_the_threshold_flag_survives(self):
        self.assertEqual(self.row().new_value["also_excludes_from_threshold"], 0)

    def test_every_other_key_survives_untouched(self):
        e = self.row()
        for k, v in e.old_value.items():
            if k in ("exclusion_type", "exclusion_value"):
                continue
            self.assertEqual(e.new_value[k], v, f"{k} was not preserved")

    def test_the_original_wording_is_stamped_so_the_edit_is_reversible(self):
        e = self.row()
        self.assertEqual(e.new_value["_retyped_from"], "other:railway bookings")
        self.assertTrue(e.reversible)

    def test_the_edit_is_a_whole_row_so_type_and_value_move_together(self):
        # Applying half of the pair leaves the file claiming a category called
        # "railway bookings", which is worse than the defect it was fixing.
        e = self.row()
        self.assertEqual(e.shape, "row")
        self.assertIsNone(e.field)


class TestRuleNameIsNeverTouchedByF5(NoCession):
    """The app keys every user's saved cap progress on the rule NAME string, so
    changing one wipes their progress. Asserted here as well as in the runner's
    own guard, because two independent checks of that is the right number."""

    def test_no_f5_edit_targets_rule_name(self):
        entry = excl_card("rn", [inert("railway bookings"), inert("wallet reloads")],
                          rules=[earns("dining")])
        for e in F5.plan(f5_ctx(entry), f5_findings("rn")):
            self.assertNotEqual(e.field, "rule_name")
            self.assertEqual(e.block, "exclusion_rules")

    def test_the_runners_guard_passes_every_f5_edit_on_the_real_catalogue(self):
        got = live_plan()
        if got is None:
            self.skipTest("no live catalogue")
        edits, _extras = got
        mine = [e for e in edits if getattr(e, "module", "") == "f5_exclusions"]
        _kept, blocked = F.guard(mine)
        self.assertEqual(blocked, [], f"guard blocked: {[b[1] for b in blocked]}")


class TestPlanIsPure(NoCession):
    """A dry run that had already changed something is not a dry run."""

    def test_plan_does_not_mutate_the_ctx_it_is_given(self):
        entry = excl_card("pure", [inert("railway bookings"), inert("emi")],
                          rules=[earns("dining")])
        ctx = f5_ctx(entry)
        before = copy.deepcopy(ctx.cards)
        F5.plan(ctx, f5_findings("pure"))
        self.assertEqual(ctx.cards, before)

    def test_plan_does_not_mutate_the_findings_it_is_given(self):
        entry = excl_card("pure", [inert("railway bookings")], rules=[earns("dining")])
        findings = f5_findings("pure")
        before = copy.deepcopy(findings)
        F5.plan(f5_ctx(entry), findings)
        self.assertEqual(findings, before)

    def test_the_returned_row_is_a_copy_not_the_row_in_ctx(self):
        entry = excl_card("pure", [inert("railway bookings")], rules=[earns("dining")])
        ctx = f5_ctx(entry)
        e = F5.plan(ctx, f5_findings("pure"))[0]
        e.new_value["exclusion_value"] = "vandalised"
        self.assertEqual(ctx.cards[0]["exclusion_rules"][0]["exclusion_value"],
                         "railway bookings")

    def test_census_is_pure_too(self):
        entry = excl_card("pure", [inert("railway bookings")], rules=[earns("dining")])
        ctx = f5_ctx(entry)
        before = copy.deepcopy(ctx.cards)
        F5.census(ctx, f5_findings("pure"))
        self.assertEqual(ctx.cards, before)


class TestABlindRunDecidesNothing(NoCession):
    """No app checkout means the category vocabulary is UNKNOWN, which is not
    the same answer as "the app has no such category". Collapsing the two would
    be this module inventing a fact about the app — the exact defect that once
    put 309 phantom errors in the validator."""

    def test_no_vocabulary_produces_no_edits(self):
        entry = excl_card("blind", [inert("rent payments")], rules=[earns("dining")])
        ctx = f5_ctx(entry, categories=[])
        self.assertEqual(F5.plan(ctx, f5_findings("blind")), [])

    def test_no_vocabulary_is_reported_as_its_own_verdict(self):
        self.assertEqual(
            F5.map_exclusion_value("rent payments", set())[0], "no_vocabulary")

    def test_a_category_the_app_does_not_ship_is_a_different_verdict(self):
        self.assertEqual(
            F5.map_exclusion_value("rent payments", {"dining"})[0], "not_in_app")


class TestTheRepairPutsBackWhatItCanProve(unittest.TestCase):
    """A previous sweep wrote two rows past a name-exact guardrail that the
    family walk rejects. Putting them back is only legitimate because
    `_retyped_from` records the original verbatim — the old value is read, not
    guessed. A row with no such stamp is left exactly where it is."""

    def live_row(self, stamp="other:railways"):
        return {"exclusion_type": "category", "exclusion_value": "railways",
                "also_excludes_from_threshold": 0, "_retyped_from": stamp}

    def test_a_family_violating_retype_is_put_back(self):
        entry = excl_card("rbl", [self.live_row()], rules=[earns("travel", 5.0)])
        edits = F5.plan(f5_ctx(entry), [])
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0].new_value["exclusion_type"], "other")
        self.assertEqual(edits[0].new_value["exclusion_value"], "railways")
        self.assertNotIn("_retyped_from", edits[0].new_value)
        self.assertEqual(edits[0].confidence, CERTAIN)

    def test_a_retype_the_card_does_not_earn_against_is_left_alone(self):
        entry = excl_card("fine", [self.live_row()], rules=[earns("dining", 5.0)])
        self.assertEqual(F5.plan(f5_ctx(entry), []), [])

    def test_a_row_with_no_stamp_is_never_reverted(self):
        row = self.live_row()
        row.pop("_retyped_from")
        entry = excl_card("nostamp", [row], rules=[earns("travel", 5.0)])
        self.assertEqual(F5.plan(f5_ctx(entry), []), [],
                         "nothing records what this row said before")

    def test_reverting_twice_is_a_no_op(self):
        entry = excl_card("rbl", [self.live_row()], rules=[earns("travel", 5.0)])
        e = F5.plan(f5_ctx(entry), [])[0]
        again = excl_card("rbl", [e.new_value], rules=[earns("travel", 5.0)])
        self.assertEqual(F5.plan(f5_ctx(again), []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
