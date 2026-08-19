#!/usr/bin/env python3
"""
test_validate_cards.py — tests for the card-data validator and its nine layers.

Run:
    python3 tools/test_validate_cards.py
    python3 tools/test_validate_cards.py -v

Stdlib unittest. No network, no third-party packages, and nothing here reads the
real 1.8 MB seed/cards.json except the two tests that say so in their names — the
rest run against small synthetic cards built in memory, so the suite is fast and
its failures point at a defect class rather than at today's catalogue.

WHAT THIS SUITE IS ACTUALLY DEFENDING
-------------------------------------
1.  **Every known defect class still fires.** One test per class, each injecting
    exactly one defect into an otherwise-clean card and asserting the specific
    code. If a check module is refactored into silence, these go red.

2.  **A clean card stays clean.** The false-positive guard. A checker that cries
    wolf on good data is worse than no checker, because the founder learns to
    ignore it. `_clean_entry()` is a card with nothing wrong with it, and every
    layer must have nothing to say about it.

3.  **Nothing is grandfathered.** The verdict may never read as clean while
    findings exist, suppressed findings must stay counted, and a crashed check
    may never be suppressed at all.

4.  **A broken check module cannot hide a broken file.** A module that raises,
    returns junk, or fails to import must produce a loud ERROR and let the other
    eight layers finish.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import validate_cards as vc                                    # noqa: E402
from checks.base import Ctx, Finding, Skipped, ERROR, WARN, INFO  # noqa: E402

CHECKS_DIR = TOOLS / "checks"
MODULE_NAMES = list(vc.LAYER_MODULES)


# --------------------------------------------------------------------------- #
# Fixtures — one card with nothing wrong with it, and the world around it.
#
# Every value here was chosen against the modules' own vocabularies, not by
# guessing: cap_period 'month' (L2 PERIOD_OK), category_id as a STRING slug
# (that is what the real file ships and what L5 reads), base_reward_rate 0.01
# for a 1% cashback card (card_base_pct multiplies by 100 for cashback), and an
# issuer whose domain is in L8's hand-maintained map.
# --------------------------------------------------------------------------- #
ISSUER = "HDFC Bank"
SOURCE = "https://www.hdfcbank.com/personal/pay/cards/credit-cards/alpha"
CARD_ID = "hdfc_bank_alpha"
TODAY = "2026-08-18"

APP_CATEGORIES = [
    {"id": 1, "category_name": "dining", "display_name": "Dining",
     "parent_id": None, "mcc_ranges": [{"exact": "5811"}], "is_excluded_default": 0},
    {"id": 2, "category_name": "fuel", "display_name": "Fuel",
     "parent_id": None, "mcc_ranges": [{"exact": "5541"}], "is_excluded_default": 1},
]

MERCHANTS = {"merchants": [{
    "id": 1, "merchant_name": "testmerch", "display_name": "Test Merch",
    "category_id": "dining", "mcc_primary": "5811", "mcc_codes": ["5811"],
    "statement_aliases": ["TESTMERCH"], "is_online": 1, "metadata": {},
    "logo_url": None,
}]}

# `stats` carries the same row counts the real seed/manifest.json declares, and
# they match _clean_entry() exactly. That is not decoration: it is the only
# independent record of how big this file is supposed to be, and therefore the
# only thing in the tool that can notice a DELETION — every other check reads
# rows that are present, so removing rows makes the report shorter, not louder.
# A fixture with no stats is a fixture that cannot be checked for missing data.
MANIFEST = {
    "version": "9.0.0", "updated_at": "2026-08-15T00:00:00Z",
    "min_app_version": "1.0.0", "source": "test",
    "stats": {
        "total_cards": 1, "total_reward_rules": 2, "total_exclusion_rules": 1,
        "total_milestone_rules": 0, "total_fuel_surcharge_rules": 0,
        "total_redemption_rules": 0, "total_travel_cards": 0,
        "issuers": 1, "total_merchants": 1,
    },
    "files": [], "news_version": "9.0.0",
}

NEWS = {"version": "9.0.0", "updated_at": "2026-08-15T00:00:00Z", "items": []}


def _card():
    return {
        "id": CARD_ID, "card_name": "HDFC Bank Alpha", "issuer": ISSUER,
        "network": "visa", "card_tier": "premium", "annual_fee": 1000.0,
        "fee_waiver_spend": 100000.0, "base_reward_rate": 0.01,
        "reward_currency": "cashback", "rp_value_standard": 1.0,
        "rp_value_travel": 1.0, "rp_value_transfer": 1.0, "forex_markup_pct": 3.5,
        "has_rupay_upi": 0, "image_asset": f"assets/images/cards/{CARD_ID}.png",
        "metadata": {}, "is_active": 1, "is_travel": 0,
        "points_expiry_months": 36, "min_redemption_points": 500,
        "points_clawback_on_default": 0,
    }


def _rule(**kw):
    row = {
        "rule_name": "", "rule_type": "base_rate", "merchant_ref": None,
        "category_ref": None, "category_id": None, "channel": None,
        "reward_type": "cashback_pct", "reward_rate": 0.01,
        "reward_unit_spend": None, "cap_amount": None, "cap_period": None,
        "min_txn_amount": None, "priority": 20, "effective_date": None,
        "expiry_date": None, "conditions_json": None, "cap_kind": "reward",
        "confidence": "high", "source_url": SOURCE, "source_quote": "",
        "source_fetched_on": "2026-08-15",
    }
    row.update(kw)
    return row


def _clean_entry():
    """A card with nothing wrong with it. Mutate a copy, never this."""
    return {
        "card": _card(),
        "reward_rules": [
            _rule(rule_name="1% cashback on all other spends", reward_rate=0.01,
                  source_quote="Earn 1% cashback on all other spends."),
            _rule(rule_name="5% cashback on dining, capped at 500 per month [dining]",
                  rule_type="category_bonus", category_id="dining",
                  category_ref="dining", reward_rate=0.05, cap_amount=500.0,
                  cap_period="month", priority=10,
                  source_quote="Earn 5% cashback on dining, capped at Rs 500 per month."),
        ],
        "exclusion_rules": [{
            "exclusion_type": "category", "exclusion_value": "fuel",
            "also_excludes_from_threshold": 1, "confidence": "high",
            "source_url": SOURCE, "source_quote": "No rewards are earned on fuel.",
        }],
        "milestone_rules": [], "fuel_surcharge_rules": [], "redemption_rules": [],
    }


def make_ctx(entries=None, **over):
    kw = dict(
        seed_dir=REPO / "seed", news_dir=REPO / "news",
        cards=entries if entries is not None else [_clean_entry()],
        merchants=copy.deepcopy(MERCHANTS), manifest=copy.deepcopy(MANIFEST),
        news=copy.deepcopy(NEWS), app_categories=copy.deepcopy(APP_CATEGORIES),
        app_root=None, config={"today": TODAY},
    )
    kw.update(over)
    return Ctx(**kw)


def run_layers(ctx, names=None):
    """[(layer_id, Finding)] across the named modules, no runner policy applied.

    Skipped records are partitioned out here exactly as the runner partitions
    them, so a helper that asks "what did this layer find" cannot be answered
    with "it did not look". Use run_skips() for the other half.
    """
    out = []
    for name in (names or MODULE_NAMES):
        mod = importlib.import_module(f"checks.{name}")
        for f in mod.run(ctx):
            if isinstance(f, Finding):
                out.append((vc.layer_id(name), f))
    return out


def run_skips(ctx, names=None):
    """[(layer_id, Skipped)] across the named modules."""
    out = []
    for name in (names or MODULE_NAMES):
        mod = importlib.import_module(f"checks.{name}")
        for f in mod.run(ctx):
            if isinstance(f, Skipped):
                out.append((vc.layer_id(name), f))
    return out


def skip_codes(ctx, names=None):
    return {s.code for _lid, s in run_skips(ctx, names)}


def codes(findings):
    return {f.code for _lid, f in findings}


def codes_for(entry, names=None):
    return codes(run_layers(make_ctx([entry]), names))


def mutate(fn):
    """A clean entry with exactly one defect injected."""
    e = _clean_entry()
    fn(e)
    return e


# --------------------------------------------------------------------------- #
# Helpers for the regression suite at the bottom of this file.
# --------------------------------------------------------------------------- #
def _one(findings, code):
    """The single finding with this code. Fails loudly if there is not exactly
    one — a test that silently matched the first of several proves nothing."""
    hits = [f for _lid, f in findings if f.code == code]
    if len(hits) != 1:
        raise AssertionError(
            f"expected exactly one {code}, got {len(hits)}. "
            f"Codes present: {sorted({f.code for _l, f in findings})}")
    return hits[0]


def _sev(findings, code):
    return _one(findings, code).severity


def _app_reading(*keys):
    """A throwaway app checkout whose lib/ mentions exactly these JSON keys.

    Ctx.app_reads_json_key() searches the real Dart source rather than trusting
    a hand-written belief about it — that belief is what was wrong about
    `redemption_rules`. So the tests have to give it something to search.
    """
    d = Path(tempfile.mkdtemp(prefix="kredme-test-app-"))
    _TMPDIRS.append(d)
    lib = d / "lib" / "shared" / "models"
    lib.mkdir(parents=True)
    body = "\n".join(f"    final raw = json['{k}'] as List<dynamic>? ?? const [];"
                     for k in keys)
    (lib / "credit_card.dart").write_text(
        "class CreditCardData {\n  factory CreditCardData.fromOtaJson(Map j) {\n"
        + body + "\n  }\n}\n", encoding="utf-8")
    cats = d / "assets" / "data" / "categories"
    cats.mkdir(parents=True)
    (cats / "categories.json").write_text(json.dumps(APP_CATEGORIES), encoding="utf-8")
    return d


_TMPDIRS = []

# The app's categories.json is what defines a parent chain, and L6's category
# lane only has an ancestor to test against when one exists. 'airlines' under
# 'travel' is the exact shape the wrong tiebreak was decided on.
NESTED_CATEGORIES = APP_CATEGORIES + [
    {"id": 5, "category_name": "travel", "display_name": "Travel",
     "parent_id": None, "mcc_ranges": [{"exact": "4722"}], "is_excluded_default": 0},
    {"id": 6, "category_name": "airlines", "display_name": "Airlines",
     "parent_id": 5, "mcc_ranges": [{"exact": "3000"}], "is_excluded_default": 0},
]


def _ctx_with_child_category(rate_child, rate_parent, priority=55):
    """One card carrying a 'travel' rule and an 'airlines' rule.

    'airlines' is a CHILD of 'travel', so the engine appends the airlines rule
    first and the travel rule second, then sorts on priority and rate only.
    """
    e = _clean_entry()
    e["reward_rules"] = [
        _rule(rule_name="Base rate of 1% on all other spends", reward_rate=0.01,
              source_quote="Earn 1% cashback on all other spends."),
        _rule(rule_name=f"{rate_parent} Reward Points per Rs 100 on travel [travel]",
              rule_type="category_bonus", category_id="travel", category_ref="travel",
              reward_type="points_per_spend", reward_rate=rate_parent,
              reward_unit_spend=100.0, priority=priority,
              source_quote=f"Earn {rate_parent} Reward Points per Rs 100 on travel."),
        _rule(rule_name=f"{rate_child} Reward Points per Rs 100 on airlines [airlines]",
              rule_type="category_bonus", category_id="airlines", category_ref="airlines",
              reward_type="points_per_spend", reward_rate=rate_child,
              reward_unit_spend=100.0, priority=priority,
              source_quote=f"Earn {rate_child} Reward Points per Rs 100 on airlines."),
    ]
    return make_ctx([e], app_categories=copy.deepcopy(NESTED_CATEGORIES))


def _write_mirror(root: Path, categories=None, keys=None):
    """Write a vendored-mirror pair under `root` and point the runner at it.

    Returns the (categories_path, keys_path) so a caller can restore. Fixtures
    MUST do this: without it a synthetic run silently compares its 2-category
    stub app against the real repo's 25-category mirror, and the drift check —
    correctly — fails the run. A test fixture has to own every input it is
    judged on.
    """
    d = root / "app_mirror"
    d.mkdir(parents=True, exist_ok=True)
    cats = APP_CATEGORIES if categories is None else categories
    (d / "categories.json").write_text(
        json.dumps({"_this_file_is": "test fixture mirror",
                    "categories": copy.deepcopy(cats)}), encoding="utf-8")
    (d / "app_json_keys.json").write_text(
        json.dumps({"_this_file_is": "test fixture mirror",
                    "keys_read_by_app": keys if keys is not None else {}}),
        encoding="utf-8")
    return d / "categories.json", d / "app_json_keys.json"


def _cli(*argv, cards=None):
    """Run main() end to end against synthetic data on disk. (exit code, stdout)."""
    root = Path(tempfile.mkdtemp(prefix="kredme-test-cli-"))
    _TMPDIRS.append(root)
    (root / "seed").mkdir()
    (root / "news").mkdir()
    entries = [_clean_entry()] if cards is None else cards
    (root / "seed" / "cards.json").write_text(json.dumps(entries), encoding="utf-8")
    (root / "seed" / "merchants.json").write_text(json.dumps(MERCHANTS), encoding="utf-8")
    (root / "seed" / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    (root / "news" / "feed.json").write_text(json.dumps(NEWS), encoding="utf-8")
    app = root / "app" / "assets" / "data" / "categories"
    app.mkdir(parents=True)
    (app / "categories.json").write_text(json.dumps(APP_CATEGORIES), encoding="utf-8")
    mc, mk = _write_mirror(root)

    old_s, old_n = vc.LIVE_SEED, vc.LIVE_NEWS
    old_mc, old_mk = vc.MIRROR_CATEGORIES, vc.MIRROR_APP_KEYS
    vc.LIVE_SEED, vc.LIVE_NEWS = root / "seed", root / "news"
    vc.MIRROR_CATEGORIES, vc.MIRROR_APP_KEYS = mc, mk
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = vc.main(["--today", TODAY, "--app-root", str(root / "app"), *argv])
    finally:
        vc.LIVE_SEED, vc.LIVE_NEWS = old_s, old_n
        vc.MIRROR_CATEGORIES, vc.MIRROR_APP_KEYS = old_mc, old_mk
    return code, out.getvalue()


# The findings a CLEAN card still produces at ERROR/WARN. Exactly one, and it is
# a statement about the APP (nothing reads `confidence`), not about the card —
# note it carries no card_id. Pinned so that if a layer starts reporting a clean
# card, this test says so instead of the count quietly drifting.
CLEAN_PORTFOLIO_CODES = {"L2.CONFIDENCE_NOT_READ"}


class TestModuleContract(unittest.TestCase):
    """Every module under checks/ obeys the contract in checks/base.py."""

    def test_all_nine_modules_import(self):
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(f"checks.{name}"))

    def test_every_module_exposes_LAYER(self):
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                mod = importlib.import_module(f"checks.{name}")
                self.assertIsInstance(getattr(mod, "LAYER", None), str)
                self.assertTrue(mod.LAYER.strip())

    def test_every_module_exposes_run(self):
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                mod = importlib.import_module(f"checks.{name}")
                self.assertTrue(callable(getattr(mod, "run", None)))

    def test_layer_label_matches_module_number(self):
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                mod = importlib.import_module(f"checks.{name}")
                self.assertTrue(mod.LAYER.startswith(vc.layer_id(name)),
                                f"{name}: LAYER {mod.LAYER!r} should start with "
                                f"{vc.layer_id(name)}")

    def test_no_module_ever_prints(self):
        """A check that prints steals the runner's job of deciding what a human sees."""
        ctx = make_ctx()
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                mod = importlib.import_module(f"checks.{name}")
                out, errbuf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
                    mod.run(ctx)
                self.assertEqual(out.getvalue(), "", f"{name} wrote to stdout")
                self.assertEqual(errbuf.getvalue(), "", f"{name} wrote to stderr")

    def test_no_module_prints_on_import(self):
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                mod = importlib.import_module(f"checks.{name}")
                out, errbuf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errbuf):
                    importlib.reload(mod)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(errbuf.getvalue(), "")

    def test_run_returns_findings_and_skips_and_nothing_else(self):
        """A module returns two kinds of thing and only two: Findings about the
        DATA, and Skipped records about ITSELF. Anything else is a contract
        break the runner turns into a loud crash finding."""
        ctx = make_ctx()
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                got = importlib.import_module(f"checks.{name}").run(ctx)
                self.assertIsInstance(got, list)
                for f in got:
                    self.assertIsInstance(f, (Finding, Skipped))

    def test_a_skip_is_never_a_finding(self):
        """The whole repair rests on these being different types. If Skipped
        ever became a Finding subclass it would land in a severity bucket, an
        exit code and a baseline, and the cascade would be back."""
        self.assertFalse(issubclass(Skipped, Finding))
        self.assertFalse(hasattr(Skipped("c", "w", "r", "x"), "severity"))

    def test_findings_carry_their_own_layer_prefix(self):
        ctx = make_ctx([mutate(lambda e: e["reward_rules"].append("junk"))])
        for name in MODULE_NAMES:
            with self.subTest(module=name):
                lid = vc.layer_id(name)
                for f in importlib.import_module(f"checks.{name}").run(ctx):
                    self.assertTrue(f.code.startswith(lid + "."),
                                    f"{name} emitted {f.code}, expected {lid}. prefix")

    def test_findings_use_known_severities(self):
        ctx = make_ctx()
        for _lid, f in run_layers(ctx):
            self.assertIn(f.severity, (ERROR, WARN, INFO))

    def test_findings_carry_a_message(self):
        ctx = make_ctx()
        for _lid, f in run_layers(ctx):
            self.assertTrue(str(f.message).strip(), f"{f.code} has an empty message")

    def test_no_module_mutates_ctx(self):
        """The runner loads the data once and every layer shares it."""
        ctx = make_ctx()
        before = json.dumps(ctx.cards, sort_keys=True, default=str)
        run_layers(ctx)
        self.assertEqual(json.dumps(ctx.cards, sort_keys=True, default=str), before)

    def test_module_list_matches_the_directory(self):
        on_disk = sorted(p.stem for p in CHECKS_DIR.glob("c[0-9]*.py"))
        self.assertEqual(sorted(MODULE_NAMES), on_disk)


class TestStdlibOnly(unittest.TestCase):
    """Nothing under tools/ may need pip. CI runs on a bare Python."""

    @staticmethod
    def _imported_roots(path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    roots.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:            # relative import, local by definition
                    continue
                if node.module:
                    roots.add(node.module.split(".")[0])
        return roots

    def _assert_stdlib(self, path: Path):
        local = {"checks", "validate_cards", "kredme", "pipeline", "base"}
        allowed = set(sys.stdlib_module_names) | local
        bad = sorted(self._imported_roots(path) - allowed)
        self.assertEqual(bad, [], f"{path.name} imports non-stdlib: {bad}")

    def test_every_check_module_is_stdlib_only(self):
        files = sorted(CHECKS_DIR.glob("*.py"))
        self.assertGreaterEqual(len(files), 10)     # base + nine layers
        for p in files:
            with self.subTest(file=p.name):
                self._assert_stdlib(p)

    def test_runner_is_stdlib_only(self):
        self._assert_stdlib(TOOLS / "validate_cards.py")

    def test_this_test_file_is_stdlib_only(self):
        self._assert_stdlib(Path(__file__))

    def test_checks_dir_has_no_stray_third_party_files(self):
        for p in CHECKS_DIR.iterdir():
            if p.is_file():
                self.assertIn(p.suffix, (".py", ".md", ".json", ""),
                              f"unexpected file in checks/: {p.name}")


class TestCleanCardHasNoFindings(unittest.TestCase):
    """The false-positive guard.

    A checker that reports a clean card teaches the founder to ignore it. Every
    layer must have nothing to say about a card with nothing wrong with it.
    """

    def setUp(self):
        self.ctx = make_ctx()

    def _card_level(self, name):
        mod = importlib.import_module(f"checks.{name}")
        return [f for f in mod.run(self.ctx)
                if isinstance(f, Finding)
                and f.severity in (ERROR, WARN) and f.card_id == CARD_ID]

    def test_L1_clean(self):
        self.assertEqual(self._card_level("c1_schema"), [])

    def test_L2_clean(self):
        self.assertEqual(self._card_level("c2_vocabulary"), [])

    def test_L3_clean(self):
        self.assertEqual(self._card_level("c3_referential"), [])

    def test_L4_clean(self):
        self.assertEqual(self._card_level("c4_numeric"), [])

    def test_L5_clean(self):
        self.assertEqual(self._card_level("c5_consistency"), [])

    def test_L6_clean(self):
        self.assertEqual(self._card_level("c6_reachability"), [])

    def test_L7_clean(self):
        self.assertEqual(self._card_level("c7_coherence"), [])

    def test_L8_clean(self):
        self.assertEqual(self._card_level("c8_provenance"), [])

    def test_L9_clean(self):
        self.assertEqual(self._card_level("c9_temporal"), [])

    def test_no_layer_reports_a_clean_card(self):
        offenders = [(lid, f.code) for lid, f in run_layers(self.ctx)
                     if f.severity in (ERROR, WARN) and f.card_id == CARD_ID]
        self.assertEqual(offenders, [], f"clean card was reported: {offenders}")

    def test_clean_cards_portfolio_findings_are_exactly_the_pinned_set(self):
        """Findings about the FILE, not the card. Pinned so drift is visible."""
        got = {f.code for _lid, f in run_layers(self.ctx)
               if f.severity in (ERROR, WARN) and f.card_id is None}
        self.assertEqual(got, CLEAN_PORTFOLIO_CODES)

    def test_clean_card_grades_A_on_provenance(self):
        graded = [f for _l, f in run_layers(self.ctx, ["c8_provenance"])
                  if f.code == "L8.CARD_GRADE"]
        self.assertEqual(len(graded), 1)
        self.assertIn("grade=A", graded[0].evidence)

    def test_clean_card_exits_zero_through_the_runner(self):
        vc.set_ctx(make_ctx())
        results, _sk, _t = vc.run_all(vc.load_checks())
        card_level = [(lid, f.code) for lid, f in results
                      if f.severity in (ERROR, WARN) and f.card_id == CARD_ID]
        self.assertEqual(card_level, [])


class TestInjectedDefects(unittest.TestCase):
    """One defect injected into an otherwise-clean card, one code asserted."""

    def assertFires(self, entry, code, names=None):
        got = codes_for(entry, names)
        self.assertIn(code, got, f"expected {code}; got {sorted(got - _BASELINE_CODES)}")

    # --- the nine classes named in the brief ------------------------------ #
    def test_string_row_in_a_block(self):
        self.assertFires(mutate(lambda e: e["reward_rules"].append("not a rule")),
                         "L1.ROW_NOT_AN_OBJECT")

    def test_string_row_also_stops_the_card_loading(self):
        self.assertFires(mutate(lambda e: e["reward_rules"].append("not a rule")),
                         "L6.CARD_NEVER_LOADS")

    def test_numeric_field_as_a_string(self):
        self.assertFires(mutate(lambda e: e["reward_rules"][1].__setitem__("cap_amount", "500")),
                         "L1.NUMERIC_FIELD_NOT_A_NUMBER")

    def test_numeric_field_as_a_string_loses_the_cap(self):
        self.assertFires(mutate(lambda e: e["reward_rules"][1].__setitem__("cap_amount", "500")),
                         "L4.CAP_NOT_A_NUMBER")

    def test_unresolvable_category_id(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("category_id", "nope")),
            "L3.RULE_CATEGORY_UNKNOWN")

    def test_unresolvable_category_id_drops_the_rule(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("category_id", "nope")),
            "L6.CATEGORY_BONUS_DROPPED")

    def test_cap_with_no_period(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("cap_period", None)),
            "L4.CAP_WITHOUT_PERIOD")

    def test_rate_above_forty_percent(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("reward_rate", 0.75)),
            "L4.RATE_ABOVE_HARD_CEILING")

    def test_text_number_mismatch(self):
        """Rule name says 5%, the field pays 1%."""
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("reward_rate", 0.01)),
            "L5.RATE_CONTRADICTS_NAME")

    def test_inert_exclusion_type(self):
        self.assertFires(
            mutate(lambda e: e["exclusion_rules"][0].__setitem__("exclusion_type", "other")),
            "L2.EXCLUSION_TYPE_INERT")

    def test_inert_exclusion_type_is_also_unreachable(self):
        self.assertFires(
            mutate(lambda e: e["exclusion_rules"][0].__setitem__("exclusion_type", "other")),
            "L6.EXCLUSION_TYPE_INERT")

    def test_aggregator_source_url(self):
        def m(e):
            for r in e["reward_rules"]:
                r["source_url"] = "https://www.cardinsider.com/hdfc-alpha/"
        self.assertFires(mutate(m), "L8.SOURCE_IS_AGGREGATOR")

    def test_missing_confidence(self):
        def m(e):
            for r in e["reward_rules"]:
                r.pop("confidence", None)
        self.assertFires(mutate(m), "L8.CONFIDENCE_DEFAULTS_TO_HIGH")

    # --- a few more that have actually shipped ---------------------------- #
    def test_cap_period_the_engine_cannot_count(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("cap_period", "daily")),
            "L2.CAP_PERIOD_COERCED")

    def test_unknown_rule_type(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("rule_type", "made_up")),
            "L2.RULE_TYPE_UNKNOWN")

    def test_duplicate_rule_name_shares_one_cap_bucket(self):
        def m(e):
            e["reward_rules"][1]["rule_name"] = e["reward_rules"][0]["rule_name"]
        self.assertFires(mutate(m), "L7.DUPLICATE_RULE_NAME")

    def test_quote_that_does_not_support_its_own_rate(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("reward_rate", 0.02)),
            "L8.QUOTE_DOES_NOT_SUPPORT_RATE")

    def test_date_that_is_not_a_real_day(self):
        self.assertFires(
            mutate(lambda e: e["reward_rules"][1].__setitem__("expiry_date", "2026-02-30")),
            "L9.DATE_NOT_A_REAL_DAY")

    def test_every_injected_defect_is_an_error_or_warning(self):
        """A defect reported only as a note would never reach the verdict."""
        cases = [
            lambda e: e["reward_rules"].append("not a rule"),
            lambda e: e["reward_rules"][1].__setitem__("cap_amount", "500"),
            lambda e: e["reward_rules"][1].__setitem__("category_id", "nope"),
            lambda e: e["reward_rules"][1].__setitem__("cap_period", None),
            lambda e: e["reward_rules"][1].__setitem__("reward_rate", 0.75),
        ]
        for i, fn in enumerate(cases):
            with self.subTest(case=i):
                found = run_layers(make_ctx([mutate(fn)]))
                new = {f.code for _l, f in found} - _BASELINE_CODES
                sev = {f.severity for _l, f in found if f.code in new}
                self.assertTrue(sev & {ERROR, WARN},
                                f"case {i} produced only notes: {new}")


class _FakeModule:
    """Stands in for a check module. Used to prove the runner survives bad ones."""
    LAYER = "L4 numeric plausibility & units"

    def __init__(self, behaviour):
        self._b = behaviour

    def run(self, ctx):
        if self._b == "raise":
            raise ZeroDivisionError("injected")
        if self._b == "not_a_list":
            return "I am not a list"
        if self._b == "junk_items":
            return [Finding(severity=ERROR, code="L4.X", message="real"), "junk"]
        if self._b == "keyboard":
            raise KeyboardInterrupt()
        if self._b == "sys_exit_zero":
            sys.exit(0)
        if self._b == "raise_system_exit_msg":
            raise SystemExit("coherence gave up")
        return []


class TestRunnerResilience(unittest.TestCase):
    """A broken check module must never be able to hide a broken file."""

    def setUp(self):
        vc.set_ctx(make_ctx())

    def test_crashing_module_returns_a_finding_instead_of_raising(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", _FakeModule("raise"))
        self.assertEqual([f.code for f in found], [vc.CRASH_CODE])

    def test_crash_finding_is_an_error(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", _FakeModule("raise"))
        self.assertEqual(found[0].severity, ERROR)

    def test_crash_finding_names_the_module_and_the_exception(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", _FakeModule("raise"))
        self.assertIn("c4_numeric", found[0].message)
        self.assertIn("ZeroDivisionError", found[0].evidence)
        self.assertIn("injected", found[0].evidence)

    def test_crash_finding_says_the_run_is_incomplete(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", _FakeModule("raise"))
        self.assertIn("INCOMPLETE", found[0].fix)

    def test_module_returning_a_non_list_is_caught(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", _FakeModule("not_a_list"))
        self.assertEqual([f.code for f in found], [vc.CRASH_CODE])

    def test_module_returning_junk_items_keeps_the_real_findings(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", _FakeModule("junk_items"))
        got = [f.code for f in found]
        self.assertIn("L4.X", got)
        self.assertIn(vc.CRASH_CODE, got)

    def test_module_that_failed_to_import_is_reported(self):
        found, _sk, _s = vc.run_layer("L4", "c4_numeric", None)
        self.assertEqual([f.code for f in found], [vc.CRASH_CODE])

    def test_keyboard_interrupt_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            vc.run_layer("L4", "c4_numeric", _FakeModule("keyboard"))

    def test_a_crash_does_not_stop_the_other_layers(self):
        # A card with a defect L1 and L9 both catch, so "this layer still ran"
        # is provable from its findings rather than from silence.
        def m(e):
            e["reward_rules"].append("not a rule")
            e["reward_rules"][1]["expiry_date"] = "2026-02-30"
        vc.set_ctx(make_ctx([mutate(m)]))
        broken = [(lid, name, _FakeModule("raise") if name == "c4_numeric" else mod, err)
                  for lid, name, mod, err in vc.load_checks()]
        results, _sk, timings = vc.run_all(broken)
        layers = {lid for lid, _f in results}
        self.assertIn("L1", layers)
        self.assertIn("L9", layers)
        self.assertEqual(len(timings), len(vc.LAYER_MODULES))
        self.assertIn(vc.CRASH_CODE, {f.code for _l, f in results})

    def test_a_crash_is_confined_to_its_own_layer(self):
        vc.set_ctx(make_ctx([mutate(lambda e: e["reward_rules"].append("not a rule"))]))
        broken = [(lid, name, _FakeModule("raise") if name == "c4_numeric" else mod, err)
                  for lid, name, mod, err in vc.load_checks()]
        results, _sk, _t = vc.run_all(broken)
        crashed = {lid for lid, f in results if f.code == vc.CRASH_CODE}
        self.assertEqual(crashed, {"L4"})

    def test_every_layer_is_timed(self):
        _results, _sk, timings = vc.run_all(vc.load_checks())
        for name in vc.LAYER_MODULES:
            self.assertIn(vc.layer_id(name), timings)
            self.assertGreaterEqual(timings[vc.layer_id(name)], 0.0)


class TestBaselineIsOptInOnly(unittest.TestCase):
    """Requirement 3: no grandfathering by default, and never a silent one."""

    def setUp(self):
        vc.set_ctx(make_ctx([mutate(lambda e: e["reward_rules"][1]
                                    .__setitem__("reward_rate", 0.75))]))
        self.results, _sk, _t = vc.run_all(vc.load_checks())

    def test_no_baseline_suppresses_nothing(self):
        kept, gone = vc.apply_baseline(self.results, None)
        self.assertEqual(len(kept), len(self.results))
        self.assertEqual(gone, [])

    def test_nothing_ever_opens_rate_baseline_json(self):
        """Behavioural, not a grep: watch every file the run actually opens.

        tools/rate_baseline.json is what tools/kredme.py grandfathers against.
        This tool must not touch it, by any path, ever.
        """
        import builtins
        opened = []
        real_open, real_read_text = builtins.open, Path.read_text

        def spy_open(file, *a, **kw):
            opened.append(str(file))
            return real_open(file, *a, **kw)

        def spy_read_text(self_, *a, **kw):
            opened.append(str(self_))
            return real_read_text(self_, *a, **kw)

        builtins.open, Path.read_text = spy_open, spy_read_text
        try:
            vc.set_ctx(make_ctx())
            vc.run_all(vc.load_checks())
        finally:
            builtins.open, Path.read_text = real_open, real_read_text
        self.assertEqual([p for p in opened if "rate_baseline" in p], [])

    def test_runner_source_declares_no_baseline_constant(self):
        names = set()
        tree = ast.parse((TOOLS / "validate_cards.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
        self.assertNotIn("RATE_BASELINE", names)

    def test_baseline_suppresses_only_exact_matches(self):
        target = next(f for _l, f in self.results
                      if f.code == "L4.RATE_ABOVE_HARD_CEILING")
        kept, gone = vc.apply_baseline(self.results, {vc.fingerprint(target)})
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0][1].code, "L4.RATE_ABOVE_HARD_CEILING")
        self.assertEqual(len(kept), len(self.results) - 1)

    def test_suppressed_findings_are_returned_not_discarded(self):
        every = {vc.fingerprint(f) for _l, f in self.results}
        kept, gone = vc.apply_baseline(self.results, every)
        self.assertEqual(len(kept) + len(gone), len(self.results))
        self.assertGreater(len(gone), 0)

    def test_a_crashed_check_can_never_be_suppressed(self):
        crash = vc.crash_finding("L4", "c4_numeric", ValueError("boom"), "crashed")
        results = self.results + [("L4", crash)]
        kept, _gone = vc.apply_baseline(results, {vc.fingerprint(crash)})
        self.assertIn(vc.CRASH_CODE, {f.code for _l, f in kept})

    def test_fingerprint_is_narrow(self):
        """Code alone must not be able to silence a different row."""
        a = Finding(severity=ERROR, code="L4.X", message="m", card_id="c1",
                    block="reward_rules", index=0, field="reward_rate")
        b = Finding(severity=ERROR, code="L4.X", message="m", card_id="c1",
                    block="reward_rules", index=1, field="reward_rate")
        self.assertNotEqual(vc.fingerprint(a), vc.fingerprint(b))

    def test_fingerprint_is_stable_across_message_rewording(self):
        a = Finding(severity=ERROR, code="L4.X", message="one wording", card_id="c1",
                    block="reward_rules", index=0, field="reward_rate")
        b = Finding(severity=ERROR, code="L4.X", message="a different wording",
                    card_id="c1", block="reward_rules", index=0, field="reward_rate")
        self.assertEqual(vc.fingerprint(a), vc.fingerprint(b))

    def test_baseline_round_trips_through_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "base.json"
            written = vc.write_baseline(p, self.results, "test")
            loaded = vc.load_baseline(p)
            self.assertEqual(written, len(loaded))
            kept, gone = vc.apply_baseline(self.results, loaded)
            self.assertEqual(len(gone), len(self.results))
            self.assertEqual(kept, [])

    def test_written_baseline_says_it_records_rather_than_fixes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "base.json"
            vc.write_baseline(p, self.results, "test")
            note = json.loads(p.read_text(encoding="utf-8"))["_note"]
            self.assertIn("does", note)
            self.assertIn("not fix", note)

    def test_a_bare_list_is_accepted_as_a_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "base.json"
            p.write_text(json.dumps(["L4.X|c1|reward_rules|0|reward_rate"]),
                         encoding="utf-8")
            self.assertEqual(len(vc.load_baseline(p)), 1)


class TestExitCodes(unittest.TestCase):
    def test_no_findings_is_zero(self):
        self.assertEqual(vc.exit_code_for([]), 0)

    def test_notes_only_is_still_zero(self):
        r = [("L1", Finding(severity=INFO, code="L1.X", message="m"))]
        self.assertEqual(vc.exit_code_for(r), 0)

    def test_a_warning_is_two(self):
        r = [("L1", Finding(severity=WARN, code="L1.X", message="m"))]
        self.assertEqual(vc.exit_code_for(r), 2)

    def test_an_error_is_one(self):
        r = [("L1", Finding(severity=ERROR, code="L1.X", message="m"))]
        self.assertEqual(vc.exit_code_for(r), 1)

    def test_an_error_outranks_a_warning(self):
        r = [("L1", Finding(severity=WARN, code="L1.X", message="m")),
             ("L4", Finding(severity=ERROR, code="L4.X", message="m"))]
        self.assertEqual(vc.exit_code_for(r), 1)


class TestScorecardHonesty(unittest.TestCase):
    """The scorecard describes the DATA. Only the exit code honours the ratchet."""

    def setUp(self):
        self.ctx = make_ctx([mutate(lambda e: e["reward_rules"][1]
                                    .__setitem__("reward_rate", 0.75))])
        vc.set_ctx(self.ctx)
        self.results, _sk, self.timings = vc.run_all(vc.load_checks())
        self.layers = [vc.layer_id(m) for m in vc.LAYER_MODULES]

    def test_suppressed_findings_still_count_in_the_scorecard(self):
        kept, gone = vc.apply_baseline(self.results,
                                       {vc.fingerprint(f) for _l, f in self.results})
        sc = vc.scorecard(self.ctx, kept, gone, self.layers, self.timings)
        self.assertEqual(sc["by_severity_new"][ERROR], 0)
        self.assertGreater(sc["by_severity"][ERROR], 0)
        self.assertEqual(sc["suppressed"], len(gone))

    def test_a_fully_suppressed_run_still_reports_the_card_as_unsafe(self):
        kept, gone = vc.apply_baseline(self.results,
                                       {vc.fingerprint(f) for _l, f in self.results})
        sc = vc.scorecard(self.ctx, kept, gone, self.layers, self.timings)
        self.assertEqual(sc["cards_clean"], 0)
        self.assertEqual(sc["cards_with_error"], 1)

    def test_headline_names_the_suppression(self):
        kept, gone = vc.apply_baseline(self.results,
                                       {vc.fingerprint(f) for _l, f in self.results})
        sc = vc.scorecard(self.ctx, kept, gone, self.layers, self.timings)
        self.assertTrue(any("suppressed" in line for line in vc.headline(sc, True)))

    def test_headline_flags_a_partial_layer_run(self):
        sc = vc.scorecard(self.ctx, self.results, [], ["L4"], self.timings)
        text = " ".join(vc.headline(sc, True))
        self.assertIn("PARTIAL", text)
        self.assertNotIn("are safe to show a user", text)

    def test_headline_flags_an_incomplete_run(self):
        sc = vc.scorecard(self.ctx, self.results, [], self.layers, self.timings)
        self.assertTrue(any("INCOMPLETE" in x for x in vc.headline(sc, False)))

    def test_scoped_run_uses_the_scoped_denominator(self):
        two = [_clean_entry(), _clean_entry()]
        two[1]["card"]["id"] = "hdfc_bank_beta"
        two[1]["card"]["card_name"] = "HDFC Bank Beta"
        two[1]["card"]["image_asset"] = "assets/images/cards/hdfc_bank_beta.png"
        ctx = make_ctx(two)
        vc.set_ctx(ctx)
        res, _sk, tim = vc.run_all(vc.load_checks())
        sc = vc.scorecard(ctx, res, [], self.layers, tim, scope={CARD_ID},
                          all_results=res)
        self.assertEqual(sc["cards"], 1)
        self.assertTrue(sc["scoped"])

    def test_grade_distribution_is_read_from_L8(self):
        sc = vc.scorecard(self.ctx, self.results, [], self.layers, self.timings)
        self.assertEqual(sc["graded_cards"], 1)
        self.assertEqual(sum(sc["grades"].values()), 1)

    def test_sourced_share_is_measured(self):
        sc = vc.scorecard(self.ctx, self.results, [], self.layers, self.timings)
        self.assertIsNotNone(sc["sourced_rules"])
        self.assertEqual(sc["total_rules_measured"], 2)

    def test_honesty_line_refuses_the_word_validated(self):
        self.assertIn("does not mean CORRECT", vc.HONESTY.replace("It ", ""))
        self.assertIn("CANNOT prove", vc.HONESTY)

    def test_block_counts_cover_every_block(self):
        cards, blocks = vc.block_counts(self.ctx)
        self.assertEqual(cards, 1)
        for b in vc.ROW_BLOCKS:
            self.assertIn(b, blocks)


class TestEndToEnd(unittest.TestCase):
    """main() against synthetic data on disk. No network, no real catalogue."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "seed").mkdir()
        (root / "news").mkdir()
        app = root / "app" / "assets" / "data" / "categories"
        app.mkdir(parents=True)
        (app / "categories.json").write_text(json.dumps(APP_CATEGORIES), encoding="utf-8")

        bad = mutate(lambda e: e["reward_rules"][1].__setitem__("reward_rate", 0.75))
        (root / "seed" / "cards.json").write_text(json.dumps([bad]), encoding="utf-8")
        (root / "seed" / "merchants.json").write_text(json.dumps(MERCHANTS), encoding="utf-8")
        (root / "seed" / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        (root / "news" / "feed.json").write_text(json.dumps(NEWS), encoding="utf-8")

        cls._seed, cls._news, cls._app = vc.LIVE_SEED, vc.LIVE_NEWS, vc.APP_ROOT_DEFAULT
        cls._mc, cls._mk = vc.MIRROR_CATEGORIES, vc.MIRROR_APP_KEYS
        vc.LIVE_SEED = root / "seed"
        vc.LIVE_NEWS = root / "news"
        vc.APP_ROOT_DEFAULT = root / "app"
        vc.MIRROR_CATEGORIES, vc.MIRROR_APP_KEYS = _write_mirror(root)
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        vc.LIVE_SEED, vc.LIVE_NEWS, vc.APP_ROOT_DEFAULT = cls._seed, cls._news, cls._app
        vc.MIRROR_CATEGORIES, vc.MIRROR_APP_KEYS = cls._mc, cls._mk
        cls.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = vc.main(["--today", TODAY, *argv])
        return code, out.getvalue()

    def test_summary_run_exits_one_on_an_error(self):
        code, text = self.run_cli("--summary")
        self.assertEqual(code, 1)
        self.assertIn("NOT safe to publish", text)

    def test_summary_prints_no_per_finding_lines(self):
        _c, text = self.run_cli("--summary")
        self.assertNotIn("L4.RATE_ABOVE_HARD_CEILING", text)

    def test_full_run_does_print_per_finding_lines(self):
        _c, text = self.run_cli()
        self.assertIn("L4.RATE_ABOVE_HARD_CEILING", text)

    def test_verdict_prints_the_exit_code_meaning(self):
        _c, text = self.run_cli("--summary")
        self.assertIn("exit 1", text)
        self.assertIn("0 = clean", text)
        self.assertIn("3 = could not run", text)

    def test_scorecard_and_honesty_line_are_printed(self):
        _c, text = self.run_cli("--summary")
        self.assertIn("Confidence scorecard", text)
        self.assertIn("CANNOT prove", text)

    def test_headline_sentence_is_printed(self):
        _c, text = self.run_cli("--summary")
        self.assertIn("safe to show a user without further checking", text)

    def test_severity_filter_hides_warnings_but_not_the_exit_code(self):
        code, text = self.run_cli("--severity", "error")
        self.assertEqual(code, 1)
        self.assertNotIn("  WARN ", text)

    def test_layer_subset_runs_only_those_layers(self):
        _c, text = self.run_cli("--layer", "L4", "--summary")
        self.assertIn("L4 numeric", text)
        self.assertNotIn("L1 schema", text)

    def test_layer_subset_refuses_to_call_a_card_safe(self):
        _c, text = self.run_cli("--layer", "L4", "--summary")
        self.assertIn("PARTIAL", text)

    def test_unknown_layer_exits_three(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                vc.main(["--layer", "L99"])
        self.assertEqual(cm.exception.code, 3)

    def test_unknown_card_exits_three(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                vc.main(["--card", "no_such_card"])
        self.assertEqual(cm.exception.code, 3)

    def test_card_filter_shows_full_detail(self):
        _c, text = self.run_cli("--card", CARD_ID)
        self.assertIn("evidence:", text)
        self.assertIn("fix:", text)

    def test_json_output_round_trips(self):
        p = self.root / "out.json"
        code, _t = self.run_cli("--json", str(p), "--quiet")
        self.assertEqual(code, 1)
        data = json.loads(p.read_text(encoding="utf-8"))
        for key in ("meta", "scorecard", "headline", "honesty", "findings", "suppressed"):
            self.assertIn(key, data)
        self.assertGreater(len(data["findings"]), 0)

    def test_json_findings_carry_a_layer(self):
        p = self.root / "out2.json"
        self.run_cli("--json", str(p), "--quiet")
        data = json.loads(p.read_text(encoding="utf-8"))
        for f in data["findings"]:
            self.assertTrue(f["layer"].startswith("L"))

    def test_html_report_is_self_contained(self):
        p = self.root / "out.html"
        self.run_cli("--html", str(p), "--quiet")
        s = p.read_text(encoding="utf-8")
        for bad in ("<link", "@import", 'src="http', "https://cdn", "fonts.googleapis"):
            self.assertNotIn(bad, s, f"HTML reaches out for {bad}")

    def test_html_report_supports_both_themes(self):
        p = self.root / "out.html"
        self.run_cli("--html", str(p), "--quiet")
        s = p.read_text(encoding="utf-8")
        self.assertIn("prefers-color-scheme:dark", s)
        self.assertIn(":root{", s)

    def test_html_report_carries_the_honesty_line(self):
        p = self.root / "out.html"
        self.run_cli("--html", str(p), "--quiet")
        self.assertIn("CANNOT prove", p.read_text(encoding="utf-8"))

    def test_html_report_escapes_its_data(self):
        p = self.root / "out.html"
        self.run_cli("--html", str(p), "--quiet")
        s = p.read_text(encoding="utf-8")
        body = s.split('<script id="data"', 1)[1]
        self.assertNotIn("</script>", body.split("</script>", 1)[0])

    def test_baseline_write_then_reuse_exits_zero_but_says_not_clean(self):
        p = self.root / "base.json"
        self.run_cli("--write-baseline", str(p), "--quiet")
        code, text = self.run_cli("--baseline", str(p), "--summary")
        self.assertEqual(code, 0)
        self.assertIn("NOT clean", text)
        self.assertIn("suppressed", text)

    def test_a_suppressed_run_never_claims_no_findings(self):
        """The exact sentence tools/kredme.py prints today, and must not appear here."""
        p = self.root / "base2.json"
        self.run_cli("--write-baseline", str(p), "--quiet")
        _c, text = self.run_cli("--baseline", str(p), "--summary")
        self.assertNotIn("no findings at all", text)
        self.assertNotIn("0 errors, 0 warning", text)

    def test_quiet_prints_nothing(self):
        _c, text = self.run_cli("--quiet")
        self.assertEqual(text, "")

    def test_a_clean_card_produces_no_errors_end_to_end(self):
        clean_root = Path(self.tmp.name) / "clean"
        (clean_root / "seed").mkdir(parents=True)
        (clean_root / "news").mkdir(parents=True)
        (clean_root / "seed" / "cards.json").write_text(
            json.dumps([_clean_entry()]), encoding="utf-8")
        (clean_root / "seed" / "merchants.json").write_text(
            json.dumps(MERCHANTS), encoding="utf-8")
        (clean_root / "seed" / "manifest.json").write_text(
            json.dumps(MANIFEST), encoding="utf-8")
        (clean_root / "news" / "feed.json").write_text(json.dumps(NEWS), encoding="utf-8")
        old_s, old_n = vc.LIVE_SEED, vc.LIVE_NEWS
        vc.LIVE_SEED, vc.LIVE_NEWS = clean_root / "seed", clean_root / "news"
        try:
            code, text = self.run_cli("--summary")
        finally:
            vc.LIVE_SEED, vc.LIVE_NEWS = old_s, old_n
        # Zero errors, and the card itself is graded A at 100% issuer-sourced.
        # The file still exits 2: two findings describe the FILE rather than the
        # card (nothing in the app reads `confidence`; the temp app checkout
        # ships no card images). A clean card is not the same thing as a clean
        # repository, and the tool is right to keep saying so.
        self.assertEqual(code, 2)
        self.assertIn("0 errors", text)
        self.assertIn("verification grades over 1 cards: A 1", text)
        self.assertIn("issuer-sourced 2 of 2 reward rates (100.0%)", text)
        self.assertIn("1 of 1 cards are safe to show a user", text)


class TestCliSurface(unittest.TestCase):
    def test_parse_layers_accepts_several_spellings(self):
        self.assertEqual(vc.parse_layers(["L4"]), {"L4"})
        self.assertEqual(vc.parse_layers(["l4"]), {"L4"})
        self.assertEqual(vc.parse_layers(["4"]), {"L4"})
        self.assertEqual(vc.parse_layers(["L4", "L8"]), {"L4", "L8"})

    def test_parse_layers_none_means_everything(self):
        self.assertIsNone(vc.parse_layers(None))
        self.assertIsNone(vc.parse_layers([]))

    def test_unknown_layer_is_rejected(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                vc.parse_layers(["L42"])

    def test_layer_id_derives_from_the_module_name(self):
        self.assertEqual(vc.layer_id("c4_numeric"), "L4")
        self.assertEqual(vc.layer_id("c9_temporal"), "L9")

    def test_load_checks_honours_a_subset(self):
        got = vc.load_checks({"L4", "L8"})
        self.assertEqual([lid for lid, _n, _m, _e in got], ["L4", "L8"])

    def test_load_checks_returns_every_layer_by_default(self):
        self.assertEqual(len(vc.load_checks()), len(vc.LAYER_MODULES))

    def test_help_mentions_no_grandfathering(self):
        self.assertIn("Nothing is grandfathered",
                      vc.build_parser().format_help().replace("\n", " "))

    def test_docstring_states_the_exit_codes(self):
        for line in ("0   no errors", "1   at least one error", "2   warnings but no errors"):
            self.assertIn(line, vc.__doc__)


# =========================================================================== #
# Regression tests for the fifteen critical/major defects and eight minors
# found by the three adversarial reviewers.
#
# One class per lens, one test per defect, each named for the defect it pins.
# Every one of these FAILS against the code as it was before the repair — that
# is the point. A comment on each says what the tool used to do.
# =========================================================================== #

class TestFalsePositivesNarrowed(unittest.TestCase):
    """Findings that were not real defects. Narrowed, never deleted."""

    # --- D1: the whole redemption block is invisible to the app ----------- #
    def test_redemption_findings_follow_whether_the_app_reads_the_block(self):
        """L4.REDEMPTION_VALUE_NULL was 128 WARNs whose stated impact ('on the
        Redemption tab the user is told these routes are worth zero') could not
        happen to anybody: the app parses `redemption_channels` and the string
        `redemption_rules` never appears in lib/. Same rows, honest severity."""
        entry = mutate(lambda e: e.__setitem__("redemption_rules", [
            {"channel_type": "cashback", "rule_name": "Statement credit",
             "point_value_inr": None}]))
        live = run_layers(make_ctx([entry], app_root=_app_reading("redemption_rules")))
        dead = run_layers(make_ctx([entry], app_root=_app_reading("redemption_channels")))
        self.assertEqual(_sev(live, "L4.REDEMPTION_VALUE_NULL"), WARN)
        self.assertEqual(_sev(dead, "L4.REDEMPTION_VALUE_NULL"), INFO)
        self.assertIn("Nobody sees this today", _one(dead, "L4.REDEMPTION_VALUE_NULL").impact)

    def test_block_key_mismatch_is_reported_in_its_own_right(self):
        entry = mutate(lambda e: e.__setitem__("redemption_rules", [
            {"channel_type": "cashback", "point_value_inr": 0.25}]))
        dead = run_layers(make_ctx([entry], app_root=_app_reading("redemption_channels")))
        f = _one(dead, "L6.REDEMPTION_BLOCK_NEVER_READ")
        self.assertEqual(f.severity, ERROR)
        self.assertIn("redemption_channels", f.message)

    def test_the_alias_claim_no_longer_says_the_rows_reach_a_user(self):
        """L6.REDEMPTION_ALIASES_NOW_READ asserted 'the ~884 rows once thought
        discarded do reach the Redemption tab'. It had verified the FIELD
        aliases inside RedemptionChannel.fromJson, never the BLOCK key that
        feeds it. It may only be emitted when the block genuinely arrives."""
        entry = mutate(lambda e: e.__setitem__("redemption_rules", [
            {"channel_type": "cashback", "point_value_inr": 0.25}]))
        dead = run_layers(make_ctx([entry], app_root=_app_reading("redemption_channels")))
        self.assertNotIn("L6.REDEMPTION_ALIASES_NOW_READ", codes(dead))
        live = run_layers(make_ctx([entry], app_root=_app_reading("redemption_rules")))
        self.assertIn("L6.REDEMPTION_ALIASES_NOW_READ", codes(live))

    # --- D2: cap unit heuristic on a card where 1 point = Rs 1 ------------ #
    def test_cap_unit_error_needs_the_point_value_to_matter(self):
        """L4.CAP_IN_RUPEES raised ERROR ('the cap is hit at the wrong moment')
        on cards with rp_value_standard = 1.0, where both readings bind at
        exactly the same spend. 8 of 19 card-level errors, 43 of 66 rows."""
        def points_cap(pv):
            return mutate(lambda e: (
                e["card"].update({"reward_currency": "cashpoints",
                                  "rp_value_standard": pv}),
                e["reward_rules"][1].update({
                    "rule_name": "5% cashback as CashPoints",
                    "reward_type": "cashback_pct", "reward_rate": 0.05,
                    "cap_amount": 1000.0, "cap_period": "cycle"})))
        at_one = codes_for(points_cap(1.0), ["c4_numeric"])
        at_quarter = codes_for(points_cap(0.25), ["c4_numeric"])
        self.assertNotIn("L4.CAP_IN_RUPEES", at_one)
        self.assertIn("L4.CAP_UNIT_LATENT", at_one)
        self.assertIn("L4.CAP_IN_RUPEES", at_quarter)

    def test_the_latent_cap_note_says_it_changes_nothing_today(self):
        e = mutate(lambda e: (
            e["card"].update({"reward_currency": "cashpoints", "rp_value_standard": 1.0}),
            e["reward_rules"][1].update({
                "rule_name": "5% cashback as CashPoints", "reward_type": "cashback_pct",
                "reward_rate": 0.05, "cap_amount": 1000.0, "cap_period": "cycle"})))
        f = _one(run_layers(make_ctx([e]), ["c4_numeric"]), "L4.CAP_UNIT_LATENT")
        self.assertEqual(f.severity, INFO)
        self.assertIn("No difference today", f.impact)

    # --- D3: one defect counted by two or three layers -------------------- #
    def test_cross_layer_duplicates_are_counted_once(self):
        """L2.EXCLUSION_TYPE_INERT (220 ERROR) and L6.EXCLUSION_TYPE_INERT (220
        WARN) were the same 220 keys at two different severities. Duplicated
        errors were 85 of 785, duplicated warnings 220 of 1,105."""
        e = mutate(lambda e: e["exclusion_rules"].append(
            {"exclusion_type": "merchant", "exclusion_value": "rent"}))
        vc.set_ctx(make_ctx([e]))
        raw, _sk, _t = vc.run_all(vc.load_checks())
        kept, n = vc.demote_duplicates(raw)
        by_code = {f.code: f.severity for _l, f in kept}
        self.assertGreaterEqual(n, 1)
        self.assertEqual(by_code["L6.EXCLUSION_TYPE_INERT"], ERROR)   # the owner
        self.assertEqual(by_code["L2.EXCLUSION_TYPE_INERT"], INFO)    # the duplicate
        blocking = [f for _l, f in kept
                    if f.code.endswith("EXCLUSION_TYPE_INERT") and f.severity != INFO]
        self.assertEqual(len(blocking), 1, "one defect, one blocking finding")

    def test_a_duplicate_keeps_its_evidence_and_names_its_owner(self):
        """Deduplicating must not delete coverage: the demoted row keeps its own
        message and evidence, so nothing a human could have read is lost."""
        e = mutate(lambda e: e["exclusion_rules"].append(
            {"exclusion_type": "merchant", "exclusion_value": "rent"}))
        vc.set_ctx(make_ctx([e]))
        kept, _n = vc.demote_duplicates(vc.run_all(vc.load_checks())[0])
        dup = _one(kept, "L2.EXCLUSION_TYPE_INERT")
        self.assertIn("merchant", dup.message + (dup.evidence or ""))
        self.assertIn("L6.EXCLUSION_TYPE_INERT", dup.message)

    def test_a_layer_run_alone_is_not_silently_softened(self):
        """The demotion may only fire when the OWNING layer actually ran and
        actually reported that key — otherwise `--layer L2` would quietly lose
        the defect altogether."""
        e = mutate(lambda e: e["exclusion_rules"].append(
            {"exclusion_type": "merchant", "exclusion_value": "rent"}))
        vc.set_ctx(make_ctx([e]))
        raw, _sk, _t = vc.run_all(vc.load_checks({"L2"}))
        kept, n = vc.demote_duplicates(raw)
        self.assertEqual(n, 0)
        self.assertEqual(_sev(kept, "L2.EXCLUSION_TYPE_INERT"), ERROR)

    # --- D4: category-lane shadow tiebreak names the wrong loser ---------- #
    def test_an_ancestor_does_not_shadow_its_child_on_a_dead_tie(self):
        """The tiebreak used the JSON array index. The engine appends the CHILD
        category's rules first and the ancestor's second, then sorts on priority
        then rate only, so on a tie the child stays ahead and the ANCESTOR
        loses. 3 of the 4 ancestor-shadow verdicts were this."""
        got = codes(run_layers(_ctx_with_child_category(rate_child=1.5, rate_parent=1.5),
                               ["c6_reachability"]))
        self.assertNotIn("L6.RULE_SHADOWED", got)

    def test_an_ancestor_that_genuinely_wins_is_still_reported(self):
        """Narrowed, not deleted: when the ancestor really does beat the child
        on rate, the child is still unreachable and still reported."""
        got = codes(run_layers(_ctx_with_child_category(rate_child=1.5, rate_parent=9.0),
                               ["c6_reachability"]))
        self.assertIn("L6.RULE_SHADOWED", got)

    def test_a_shadow_that_pays_the_same_number_is_not_a_wrong_number(self):
        """7 of 37 shadowed rows had identical priority AND identical rate, so
        whichever wins the user sees the same figure — yet the impact line said
        'the user is being shown — and paid — the worse rate of the two'."""
        e = _clean_entry()
        for tag in ("", " (duplicate row)"):
            e["reward_rules"].append(_rule(
                rule_name=f"5% cashback on dining{tag} [dining]",
                rule_type="category_bonus", category_id="dining", category_ref="dining",
                reward_rate=0.05, priority=10,
                source_quote="Earn 5% cashback on dining."))
        got = run_layers(make_ctx([e]), ["c6_reachability"])
        f = _one(got, "L6.RULE_SHADOWED_EQUAL")
        self.assertEqual(f.severity, INFO)
        self.assertIn("No user-visible difference", f.impact)
        self.assertNotIn("L6.RULE_SHADOWED", codes(got))

    # --- D5: top grade for a card the app cannot load --------------------- #
    def test_a_card_that_never_loads_cannot_hold_a_verification_grade(self):
        """yes_bank_uni_rupay held grade B — the top grade anything in the file
        achieved, shared by 6 of 383 cards — while L6 reported it crashes the
        app's parser and reaches nobody."""
        e = mutate(lambda e: e.__setitem__("exclusion_rules", "IDENTICAL to the card above"))
        got = run_layers(make_ctx([e]), ["c6_reachability", "c8_provenance"])
        self.assertIn("L6.CARD_NEVER_LOADS", codes(got))
        self.assertIn("grade=N/A", _one(got, "L8.CARD_GRADE").evidence)

    def test_a_loadable_card_still_earns_its_grade(self):
        got = run_layers(make_ctx(), ["c8_provenance"])
        self.assertIn("grade=A", _one(got, "L8.CARD_GRADE").evidence)

    # --- D6: the biggest warning family is a coverage metric -------------- #
    def test_no_number_in_a_base_rule_name_is_coverage_not_a_defect(self):
        """261 of 1,105 warnings. Its own fix text forbids the only edit that
        clears it, and it alone manufactured the headline '0 of 383 cards are
        safe' — removing it took safe cards from 0 to 29."""
        e = mutate(lambda e: e["reward_rules"][0].__setitem__("rule_name", "Base reward rate"))
        f = _one(run_layers(make_ctx([e]), ["c5_consistency"]), "L5.BASE_RULE_UNVERIFIABLE")
        self.assertEqual(f.severity, INFO)

    def test_a_card_whose_only_finding_is_missing_evidence_counts_as_safe(self):
        """The 'safe' denominator counts errors and warnings. A gap in evidence
        must not be able to make a card unsafe on its own."""
        e = mutate(lambda e: e["reward_rules"][0].__setitem__("rule_name", "Base reward rate"))
        vc.set_ctx(make_ctx([e]))
        results, _sk, timings = vc.run_all(vc.load_checks())
        sc = vc.scorecard(make_ctx([e]), results, [], [l for l, _n, _m, _e in vc.load_checks()],
                          timings)
        self.assertEqual(sc["cards_clean"], 1)

    # --- minor: a rate blamed for a point-value disagreement -------------- #
    def test_a_point_value_disagreement_is_not_reported_as_a_wrong_rate(self):
        """The guard that reroutes this only fired when BOTH the 'N points per
        Rs M' and the 'P%' were in the NAME. With the denominator in the
        reward_unit_spend FIELD it was blind, and both yes_bank_marquee rows
        were accused of a wrong rate."""
        e = mutate(lambda e: e["reward_rules"][1].update({
            "rule_name": "Earn 10 Reward Points on select categories such as "
                         "Recharge and Utility Bill Payments (1.25% reward rate) [dining]",
            "reward_type": "points_per_spend", "reward_rate": 10.0,
            "reward_unit_spend": 200.0, "category_id": "dining",
            "cap_amount": None, "cap_period": None}))
        e["card"]["rp_value_standard"] = 0.15
        got = codes(run_layers(make_ctx([e]), ["c5_consistency"]))
        self.assertIn("L5.NAME_IMPLIES_OTHER_POINT_VALUE", got)
        self.assertNotIn("L5.RATE_CONTRADICTS_NAME", got)

    # --- minor: a spend tier read as a cap -------------------------------- #
    def test_a_spend_tier_boundary_is_not_an_unenforced_cap(self):
        """'Base rate (up to Rs 1.5L monthly spend)' caps nothing — the rate
        changes at the threshold and the next rule states the rate above it.
        cap_amount is correctly null; the finding told the reader otherwise."""
        e = mutate(lambda e: e["reward_rules"][0].__setitem__(
            "rule_name", "Base rate (up to ₹1.5L monthly spend)"))
        got = codes(run_layers(make_ctx([e]), ["c5_consistency"]))
        self.assertIn("L5.SPEND_TIER_NOT_MODELLED", got)
        self.assertNotIn("L5.CAP_IN_TEXT_NOT_ENFORCED", got)

    def test_a_real_ceiling_in_the_text_is_still_reported(self):
        e = mutate(lambda e: e["reward_rules"][1].update(
            {"rule_name": "5X up to 5000 Reward Points per month [dining]",
             "cap_amount": None, "cap_period": None}))
        self.assertIn("L5.CAP_IN_TEXT_NOT_ENFORCED",
                      codes(run_layers(make_ctx([e]), ["c5_consistency"])))


class TestFalseNegativesClosed(unittest.TestCase):
    """Defect classes no layer covered. Injected, then asserted."""

    # --- D7: nothing noticed that data was MISSING ------------------------ #
    def test_a_deleted_card_is_caught_by_the_manifest_declaration(self):
        """Delete a card and every finding derived from rows that are PRESENT
        simply disappears with it — the report got shorter and cleaner. Deleting
        382 of 383 cards printed 'catalogue 1 cards' and exited 1, the same code
        as the pristine file, so a CI gate saw no change at all."""
        got = codes(run_layers(make_ctx([]), ["c3_referential"]))
        self.assertIn("L3.MANIFEST_STATS_SHORTFALL", got)

    def test_a_deleted_reward_rule_is_caught(self):
        e = mutate(lambda e: e["reward_rules"].pop())
        f = _one(run_layers(make_ctx([e]), ["c3_referential"]),
                 "L3.MANIFEST_STATS_SHORTFALL")
        self.assertEqual(f.severity, ERROR)
        self.assertIn("reward rules", f.message)

    def test_a_deleted_exclusion_row_is_caught(self):
        e = mutate(lambda e: e["exclusion_rules"].clear())
        self.assertIn("L3.MANIFEST_STATS_SHORTFALL",
                      codes(run_layers(make_ctx([e]), ["c3_referential"])))

    def test_a_complete_file_says_so_instead_of_staying_silent(self):
        self.assertIn("L3.MANIFEST_STATS_AGREE",
                      codes(run_layers(make_ctx(), ["c3_referential"])))

    def test_a_manifest_with_no_stats_cannot_check_for_deletions_and_says_so(self):
        man = copy.deepcopy(MANIFEST)
        man.pop("stats")
        self.assertIn("L3.MANIFEST_NO_STATS",
                      codes(run_layers(make_ctx(manifest=man), ["c3_referential"])))

    # --- D8: a card that excludes what it pays a bonus on ----------------- #
    def test_a_bonus_on_a_category_the_card_excludes_is_unreachable(self):
        """The engine tests exclusions FIRST and returns 'No rewards on this
        category' before it looks at any reward rule, so the bonus can never
        fire. 4 rules on 2 cards do this today and nine layers said nothing."""
        e = mutate(lambda e: e["exclusion_rules"].append(
            {"exclusion_type": "category", "exclusion_value": "dining"}))
        f = _one(run_layers(make_ctx([e]), ["c6_reachability"]),
                 "L6.RULE_EXCLUDED_BY_OWN_CARD")
        self.assertEqual(f.severity, ERROR)
        self.assertIn("dining", f.evidence)

    def test_a_bonus_at_a_merchant_in_an_excluded_category_is_unreachable(self):
        e = mutate(lambda e: (
            e["reward_rules"][1].update({"rule_type": "merchant_specific",
                                         "merchant_ref": "testmerch",
                                         "category_id": None, "category_ref": None}),
            e["exclusion_rules"].append({"exclusion_type": "category",
                                         "exclusion_value": "dining"})))
        self.assertIn("L6.RULE_EXCLUDED_BY_OWN_CARD",
                      codes(run_layers(make_ctx([e]), ["c6_reachability"])))

    def test_a_card_that_excludes_something_else_is_left_alone(self):
        self.assertNotIn("L6.RULE_EXCLUDED_BY_OWN_CARD",
                         codes(run_layers(make_ctx(), ["c6_reachability"])))

    # --- D9: no plausibility band on a redemption point value ------------- #
    def test_a_redemption_row_claiming_rs_300_a_point_is_reported(self):
        """The 0.05-2.0 band was applied only to card.rp_value_standard. The
        same number in a redemption row — 641 numeric rows — was never
        range-checked, and the only comparison that touched them filtered to
        cashback / statement_credit first, leaving 497 rows checked by nothing."""
        for channel in ("merchandise", "other", "partner_transfer", "travel",
                        "gift_card", "voucher_catalog"):
            with self.subTest(channel=channel):
                e = mutate(lambda e, c=channel: e.__setitem__("redemption_rules", [
                    {"channel_type": c, "rule_name": "Catalog", "point_value_inr": 300.0}]))
                self.assertIn("L4.REDEMPTION_POINT_VALUE_OUT_OF_BAND",
                              codes(run_layers(make_ctx([e]), ["c4_numeric"])))

    def test_a_negative_redemption_point_value_is_reported(self):
        e = mutate(lambda e: e.__setitem__("redemption_rules", [
            {"channel_type": "other", "rule_name": "Catalog", "point_value_inr": -0.25}]))
        f = _one(run_layers(make_ctx([e]), ["c4_numeric"]),
                 "L4.REDEMPTION_POINT_VALUE_IMPOSSIBLE")
        self.assertIn("negative", f.evidence)

    def test_a_plausible_redemption_point_value_is_left_alone(self):
        e = mutate(lambda e: e.__setitem__("redemption_rules", [
            {"channel_type": "other", "rule_name": "Catalog", "point_value_inr": 0.25}]))
        got = codes(run_layers(make_ctx([e]), ["c4_numeric"]))
        self.assertNotIn("L4.REDEMPTION_POINT_VALUE_OUT_OF_BAND", got)
        self.assertNotIn("L4.REDEMPTION_POINT_VALUE_IMPOSSIBLE", got)

    # --- minor: the merchant helper that always returned nothing ---------- #
    def test_the_shared_merchant_list_is_not_empty(self):
        """Ctx.merchant_slugs() looked for 'slug'/'merchant_slug'/'id'/
        'merchant_ref' and merchants.json keys its rows by 'merchant_name', so
        it returned an empty set for all 273 rows and c5's merchant branch was
        unreachable code."""
        self.assertEqual(make_ctx().merchant_slugs(), {"testmerch"})

    def test_a_tag_that_is_neither_a_category_nor_a_merchant_is_reported(self):
        e = mutate(lambda e: e["reward_rules"][1].update(
            {"rule_type": "merchant_specific", "merchant_ref": "testmerch",
             "category_id": None, "category_ref": None,
             "rule_name": "5% cashback at the shop [notamerchant]"}))
        self.assertIn("L5.CATEGORY_TAG_MISMATCH",
                      codes(run_layers(make_ctx([e]), ["c5_consistency"])))

    # --- minor: a sync-breaking mismatch reported at note level ----------- #
    def test_an_edit_after_the_manifest_was_written_is_a_warning(self):
        """On --target working — what a human types before a publish — a
        checksum mismatch was INFO, while its own impact line said the app
        rejects the sync and every user keeps stale card data."""
        with tempfile.TemporaryDirectory() as td:
            seed = Path(td) / "seed"
            seed.mkdir()
            man = copy.deepcopy(MANIFEST)
            man["files"] = [{"name": "cards.json", "path": "seed/cards.json",
                             "size_bytes": 999999, "checksum": "0" * 64}]
            (seed / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
            (seed / "cards.json").write_text("[]", encoding="utf-8")
            os.utime(seed / "cards.json", (10 ** 9 + 500, 10 ** 9 + 500))
            os.utime(seed / "manifest.json", (10 ** 9, 10 ** 9))
            got = run_layers(make_ctx(manifest=man, seed_dir=seed), ["c3_referential"])
            self.assertEqual(_sev(got, "L3.MANIFEST_CHECKSUM_MISMATCH"), WARN)


class TestRobustness(unittest.TestCase):
    """Ways to crash the tool, or make it lie."""

    # --- D10: a mistyped flag exited 2, the code for 'publishable' -------- #
    def test_a_mistyped_flag_exits_three_not_two(self):
        """argparse's own error path exits 2, and 2 is this tool's code for
        'warnings but no errors: nothing is broken'. A typo in a CI step made a
        gate that validated NOTHING report the second-greenest result."""
        for argv in (["--summry"], ["--target", "prd"], ["--severity", "critical"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as cm:
                        vc.main(argv)
                self.assertEqual(cm.exception.code, 3)

    def test_help_still_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                vc.main(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_the_documented_contract_names_exit_three_for_a_bad_flag(self):
        self.assertIn("3   could not run (bad flag", vc.__doc__)

    # --- D11: a module calling sys.exit(0) ended the run, silently green -- #
    def test_a_module_that_calls_sys_exit_is_a_crashed_module(self):
        """run_layer re-raised SystemExit, so sys.exit(0) inside any check ended
        the whole run at exit 0 with no layer results, no scorecard and no
        verdict. A silently green run is the worst failure shape this tool has."""
        found, _sk, _s = vc.run_layer("L7", "c7_coherence", _FakeModule("sys_exit_zero"))
        self.assertEqual([f.code for f in found], [vc.CRASH_CODE])
        self.assertEqual(found[0].severity, ERROR)
        self.assertIn("sys.exit", found[0].message)

    def test_raise_system_exit_with_a_message_is_also_a_crashed_module(self):
        found, _sk, _s = vc.run_layer("L7", "c7_coherence", _FakeModule("raise_system_exit_msg"))
        self.assertEqual([f.code for f in found], [vc.CRASH_CODE])
        self.assertIn("coherence gave up", found[0].evidence)

    def test_a_module_that_exits_does_not_stop_the_other_layers(self):
        vc.set_ctx(make_ctx())
        checks = [("L7", "c7_coherence", _FakeModule("sys_exit_zero"), None),
                  ("L4", "c4_numeric", _FakeModule("ok"), None)]
        results, _sk, timings = vc.run_all(checks)
        self.assertIn("L4", timings, "the layer after the one that exited must still run")
        self.assertEqual([f.code for _l, f in results], [vc.CRASH_CODE])
        self.assertEqual(vc.exit_code_for(results), 1)

    def test_ctrl_c_is_still_the_one_thing_that_ends_the_run(self):
        with self.assertRaises(KeyboardInterrupt):
            vc.run_layer("L4", "c4_numeric", _FakeModule("keyboard"))

    # --- D12: a baseline silenced brand-new portfolio findings ------------ #
    def test_a_portfolio_fingerprint_is_not_just_the_code(self):
        """fingerprint() is code|card|block|index|field, and a finding that
        names no card has none of the last four — so the key degenerated to the
        code alone, the exact looser key its own docstring rules out. 96 of
        2,582 keys in a real baseline were in that form, across 39 codes."""
        a = Finding(severity=WARN, code="L2.NETWORK_UNRECOGNISED", block="card",
                    field="network", message="network 'x' on 1 card", evidence="card_a")
        b = Finding(severity=WARN, code="L2.NETWORK_UNRECOGNISED", block="card",
                    field="network", message="network 'y' on 2 cards", evidence="card_a, card_b")
        self.assertNotEqual(vc.fingerprint(a), vc.fingerprint(b))

    def test_a_baseline_does_not_hide_a_new_defect_of_a_recorded_kind(self):
        """The end-to-end shape of the hole: record ONE unrecognised network in a
        baseline, then put a DIFFERENT card on a DIFFERENT unrecognised network.
        Under the code-only key the second one was already 'known' and the run
        went green."""
        code = "L2.CONFIDENCE_NOT_READ"          # names no card; counts a population
        vc.set_ctx(make_ctx())
        before, _sk, _t = vc.run_all(vc.load_checks({"L2"}))
        self.assertIn(code, {f.code for _l, f in before})
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "b.json"
            vc.write_baseline(path, before, "test")
            baseline = vc.load_baseline(path)

            # Same run again: correctly suppressed. A ratchet has to still ratchet.
            vc.set_ctx(make_ctx())
            same, _sk2, _t2 = vc.run_all(vc.load_checks({"L2"}))
            self.assertNotIn(code, {f.code for _l, f in
                                    vc.apply_baseline(same, baseline)[0]})

            # Now the population it describes GROWS. That is a new state of the
            # file, and it must not be covered by what was recorded before.
            wider = mutate(lambda e: e["reward_rules"].append(
                _rule(rule_name="2% cashback on travel [dining]",
                      rule_type="category_bonus", category_id="dining",
                      reward_rate=0.02, priority=12)))
            vc.set_ctx(make_ctx([wider]))
            after, _sk3, _t3 = vc.run_all(vc.load_checks({"L2"}))
            kept, _gone = vc.apply_baseline(after, baseline)
            self.assertIn(code, {f.code for _l, f in kept},
                          "a baseline entry recorded over one population silenced a "
                          "different, larger one")
            self.assertNotEqual(vc.exit_code_for(kept), 0)

    def test_a_card_level_fingerprint_is_unchanged(self):
        """The narrow key still is the narrow key: only card-less findings gain
        the population hash, so existing baselines keep working."""
        f = Finding(severity=WARN, code="L4.X", message="m", card_id="c",
                    block="reward_rules", index=2, field="cap_amount")
        self.assertEqual(vc.fingerprint(f), "L4.X|c|reward_rules|2|cap_amount")

    # --- D13: a non-list cards.json misdiagnosed as an empty file --------- #
    def test_a_wrapped_card_list_is_diagnosed_as_a_wrapper_not_an_empty_file(self):
        """build_ctx coerced a non-list to [] before any check saw it, so the
        two purpose-built findings were unreachable dead code and the operator
        was told to 'restore the card list' when every card was present."""
        for value in ({"cards": [_clean_entry()]}, "just a string", 42, None):
            with self.subTest(value=type(value).__name__):
                got = codes(run_layers(make_ctx(cards=value)))
                self.assertIn("L1.CARDS_FILE_NOT_A_LIST", got)
                self.assertIn("L6.CARDS_FILE_NOT_A_LIST", got)
                self.assertNotIn("L1.CARDS_FILE_EMPTY", got)

    def test_a_genuinely_empty_list_is_still_an_empty_file(self):
        got = codes(run_layers(make_ctx([])))
        self.assertIn("L1.CARDS_FILE_EMPTY", got)
        self.assertNotIn("L1.CARDS_FILE_NOT_A_LIST", got)

    def test_no_layer_crashes_on_a_non_list_card_file(self):
        for value in ({"cards": []}, "s", 42, None, 1.5, True):
            with self.subTest(value=repr(value)):
                vc.set_ctx(make_ctx(cards=value))
                results, _sk, _t = vc.run_all(vc.load_checks())
                self.assertEqual([f.code for _l, f in results if f.code == vc.CRASH_CODE], [])

    # --- D14: --card dropped file-wide findings from the exit code -------- #
    def test_a_scoped_run_still_fails_on_a_file_wide_error(self):
        """`--card X` stripped every card_id-less finding out of the verdict AND
        the exit code, and unlike --layer carried no caveat, so it printed
        'no errors and no warnings' and returned 0 on a file the same run had
        just found errors in."""
        # A card that is itself spotless, in a FILE that carries a portfolio-level
        # ERROR (L8.HEADLINE_VERIFIED_SHARE — the issuer-sourced share across the
        # whole catalogue). Scoping to that card used to return 0.
        second = mutate(lambda e: (e["card"].__setitem__("id", "hdfc_bank_beta"),
                                   e["reward_rules"][0].__setitem__("source_url", None),
                                   e["reward_rules"][1].__setitem__("source_url", None)))
        cards = [_clean_entry(), second]

        vc.set_ctx(make_ctx(cards))
        results, _sk, _t = vc.run_all(vc.load_checks({"L8"}))
        file_wide = [f for _l, f in results if f.card_id is None and f.severity == ERROR]
        self.assertTrue(file_wide, "this fixture must produce a file-wide ERROR")
        self.assertEqual(vc.exit_code_for(vc.filter_results(results, {CARD_ID}, None)), 0,
                         "and the scoped card itself must be spotless")

        # Through main(), which is where the exit code is actually decided.
        code, _text = _cli("--card", CARD_ID, "--quiet", cards=cards)
        self.assertEqual(code, 1)

    def test_a_scoped_verdict_discloses_what_it_left_out(self):
        e = mutate(lambda e: e["card"].__setitem__("network", "BOGUS RAIL"))
        code, text = _cli("--card", CARD_ID, "--summary", cards=[e])
        self.assertIn("SCOPED", text)
        self.assertIn("describe the whole file", text)
        self.assertNotEqual(code, 0)

    def test_an_unscoped_run_prints_no_scoped_caveat(self):
        _code, text = _cli("--summary")
        self.assertNotIn("SCOPED", text)

    # --- D15: header and scorecard disagreed on the card count ------------ #
    def test_the_header_and_the_scorecard_agree_on_the_card_count(self):
        """print_header counted ENTRIES, the scorecard counted DISTINCT IDS, and
        they printed twelve lines apart. Duplicate ids are live in this
        catalogue — L3.DUPLICATE_CARD_NAME already fires on it."""
        dupes = [_clean_entry() for _ in range(10)] + [
            mutate(lambda e: e["card"].__setitem__("id", "hdfc_bank_beta"))]
        _code, text = _cli("--summary", cards=dupes)
        lines = [l for l in text.splitlines() if "catalogue" in l or "in scope" in l]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].strip(), lines[1].strip())
        self.assertIn("11 entries under 2 distinct card ids", lines[0])

    def test_a_file_with_no_duplicate_ids_says_plain_cards(self):
        _code, text = _cli("--summary")
        self.assertIn("1 cards", text)
        self.assertNotIn("entries under", text)

    # --- minor: '(L8 did not run)' claimed in runs where L8 ran ----------- #
    def test_the_headline_does_not_claim_L8_skipped_when_it_ran(self):
        """The condition tested was `sourced_rules is None`, which is also true
        when L8 ran perfectly and found no reward rule to grade. This is the one
        sentence the tool's own comment says gets pasted into a deck."""
        sc = {"cards": 0, "cards_clean": 0, "cards_with_error": 0, "suppressed": 0,
              "sourced_rules": None, "total_rules_measured": None,
              "layers_run": list(vc.LAYER_MODULES) and
              [vc.layer_id(m) for m in vc.LAYER_MODULES]}
        line = " ".join(vc.headline(sc, True))
        self.assertIn("L8 ran but found no reward rule to grade", line)
        self.assertNotIn("L8 did not run", line)

    def test_the_headline_does_say_so_when_L8_really_did_not_run(self):
        sc = {"cards": 0, "cards_clean": 0, "cards_with_error": 0, "suppressed": 0,
              "sourced_rules": None, "total_rules_measured": None, "layers_run": ["L4"]}
        self.assertIn("L8 did not run", " ".join(vc.headline(sc, True)))

    # --- minor: an unwritable report path raised a bare traceback --------- #
    def test_an_unwritable_report_path_exits_three(self):
        """It raised an unhandled OSError and exited 1 — the code this tool
        defines as 'a user can be shown a wrong number'. Under --quiet the
        traceback was the only output."""
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / "blocker"
            blocker.write_text("i am a file, not a directory", encoding="utf-8")
            for flag in ("--json", "--html", "--write-baseline"):
                with self.subTest(flag=flag):
                    with self.assertRaises(SystemExit) as cm:
                        with contextlib.redirect_stdout(io.StringIO()):
                            vc.main(["--quiet", flag, str(blocker / "sub" / "out")])
                    self.assertEqual(cm.exception.code, 3)

    # --- minor: bidi overrides passed through into the HTML report -------- #
    def test_the_html_report_neutralises_bidi_overrides(self):
        """U+202E in a card_name or issuer visually reverses the rest of the
        rendered line, so what a reader sees is not what the data says."""
        for ch in ("‪", "‫", "‬", "‭", "‮",
                   "⁦", "⁧", "⁨", "⁩"):
            with self.subTest(ch=hex(ord(ch))):
                self.assertNotIn(ch, vc.debidi(f"Big {ch}reversed"))
        self.assertEqual(vc.debidi("a‮b"), "a[U+202E]b")

    def test_a_bidi_override_in_a_card_id_never_reaches_the_rendered_page(self):
        e = mutate(lambda e: e["card"].__setitem__("id", "hdfc_bank_alpha‮detrevni"))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.html"
            with contextlib.redirect_stdout(io.StringIO()):
                vc.main(["--today", TODAY, "--quiet", "--html", str(out)]) \
                    if False else None
            _code, _text = _cli("--quiet", "--html", str(out), cards=[e])
            html = out.read_text(encoding="utf-8")
        self.assertNotIn("‮", html)
        self.assertIn("[U+202E]", html)

    # --- minor: --target dev/prod left a 1.9 MB copy behind every run ----- #
    def test_a_materialised_branch_copy_is_registered_for_cleanup(self):
        import atexit as _atexit
        seen = []
        real = _atexit.register

        def spy(fn, *a, **kw):
            seen.append((fn, a))
            return real(fn, *a, **kw)
        _atexit.register = spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    vc.materialise("HEAD")
                except SystemExit:
                    self.skipTest("no git data files to materialise here")
        finally:
            _atexit.register = real
        self.assertTrue(any(fn is shutil.rmtree for fn, _a in seen),
                        "the temp copy must be scheduled for removal")
        for fn, a in seen:
            if fn is shutil.rmtree:
                fn(*a, ignore_errors=True)


# --------------------------------------------------------------------------- #
# A check that loses its inputs must SKIP, not invent.
#
# Measured on the real catalogue, 2026-08-18: run with the app checkout the
# validator reported 712 errors; run without it, 1,021. The extra 309 were not
# defects in the file — the same bytes were on disk both times. They were the
# output of checks that had lost the app's category list and carried on anyway:
# 191 L6.CATEGORY_BONUS_DROPPED, 109 L5.CATEGORY_TAG_MISMATCH, 7
# L6.CATEGORY_ID_UNRESOLVABLE and the L6.CATEGORIES_MISSING that should have
# stopped all three.
#
# That matters beyond tidiness: kredme-data is PUBLIC and the app repo is
# PRIVATE, so CI here can NEVER check the app out. The degraded run is not an
# edge case — it is every run that will ever gate a PR.
# --------------------------------------------------------------------------- #
_CATEGORY_PHANTOMS = (
    "L6.CATEGORY_BONUS_DROPPED",
    "L6.CATEGORY_ID_UNRESOLVABLE",
    "L5.CATEGORY_TAG_MISMATCH",
    "L6.CATEGORIES_MISSING",
)


def _blind_ctx(entries=None):
    """A ctx with NO category vocabulary at all: no app checkout, no mirror."""
    return make_ctx(entries, app_categories=None, app_root=None)


class TestBlindChecksSkipRatherThanInvent(unittest.TestCase):

    def test_no_category_phantom_is_ever_emitted_without_a_vocabulary(self):
        """The cascade, pinned. A card whose category rules are perfectly valid
        must not be accused of anything by a run that cannot see the app's
        category list."""
        found = codes(run_layers(_blind_ctx()))
        for code in _CATEGORY_PHANTOMS:
            with self.subTest(code=code):
                self.assertNotIn(code, found)

    def test_the_blind_run_declares_the_skips_instead(self):
        got = skip_codes(_blind_ctx())
        for code in ("L3.CATEGORY_REFERENCES", "L5.CATEGORY_TAG_CROSSCHECK",
                     "L6.CATEGORY_REACHABILITY"):
            with self.subTest(code=code):
                self.assertIn(code, got)

    def test_a_blind_run_raises_no_error_at_all_on_a_clean_card(self):
        """The strong form: losing the vocabulary must not change the SEVERITY
        of anything either. Silence is not enough if the run still fails."""
        errs = [f for _l, f in run_layers(_blind_ctx()) if f.severity == ERROR]
        self.assertEqual([f.code for f in errs], [])

    def test_every_skip_says_what_reason_restore(self):
        """A skip nobody can act on is only a nicer way of going quiet."""
        for _lid, s in run_skips(_blind_ctx()):
            with self.subTest(code=s.code):
                for field in ("what", "reason", "restore"):
                    self.assertTrue(getattr(s, field), f"{s.code} has no {field}")
                self.assertTrue(s.code.startswith("L"), s.code)

    def test_a_skip_never_reaches_the_exit_code(self):
        """Findings decide the exit code. Skips decide the WORDS around it."""
        self.assertEqual(vc.exit_code_for([]), 0)
        skips = [Skipped(code="L3.X", what="w", reason="r", restore="f")]
        sc = {"by_severity_verdict": {ERROR: 0, WARN: 0, INFO: 0},
              "by_severity_new": {ERROR: 0, WARN: 0, INFO: 0},
              "suppressed": 0, "skipped_count": len(skips)}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            vc.print_verdict([], sc, 0, True, None)
        text = out.getvalue()
        self.assertIn("exit 0", text)
        self.assertIn("DEGRADED", text)

    def test_a_skipped_run_is_never_described_as_clean_without_a_caveat(self):
        """The failure this whole change exists to prevent: reading a degraded
        run as a clean bill of health."""
        sc = {"by_severity_verdict": {ERROR: 0, WARN: 0, INFO: 0},
              "by_severity_new": {ERROR: 0, WARN: 0, INFO: 0},
              "suppressed": 0, "skipped_count": 2}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            vc.print_verdict([], sc, 0, True, None)
        text = out.getvalue()
        self.assertIn("no findings at all", text)      # still true of what ran
        self.assertIn("FLOOR", text)                    # and immediately qualified
        self.assertIn("2 check(s) could not run", text)

    def test_the_scorecard_counts_skips(self):
        ctx = _blind_ctx()
        sc = vc.scorecard(ctx, [], [], ["L3"], {"L3": 0.0},
                          skipped=[Skipped(code="L3.X", what="w", reason="r",
                                           restore="f")])
        self.assertEqual(sc["skipped_count"], 1)
        self.assertTrue(sc["degraded"])
        self.assertEqual(len(sc["skipped_checks"]), 1)

    def test_a_full_run_reports_zero_skips_and_is_not_degraded(self):
        sc = vc.scorecard(make_ctx(), [], [], ["L3"], {"L3": 0.0})
        self.assertEqual(sc["skipped_count"], 0)
        self.assertFalse(sc["degraded"])

    def test_skips_are_not_filtered_out_by_severity(self):
        """--severity is a display filter over FINDINGS. A skip has no severity
        and must survive every filter, or the loudest possible run — one asking
        only for errors — would be the one that hides them."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            vc.print_layer("L3", "L3 referential", [], 0.0, True, False,
                           skips=[Skipped(code="L3.X", what="nothing was checked",
                                          reason="r", restore="f")])
        text = out.getvalue()
        self.assertIn("SKIP", text)
        self.assertIn("L3.X", text)
        self.assertNotIn("  WARN ", text)   # must not read as a WARN finding
        self.assertNotIn("nothing to report", text.split("SKIP")[0])


class TestRealCatalogueInventsNothingWhenBlind(unittest.TestCase):
    """The one test that reads seed/cards.json, because this defect only shows
    its true size at scale: on two cards it is invisible, on 383 it was 309
    fabricated errors.

    The invariant, stated once:

        A run with fewer inputs may report FEWER findings. It may never report
        a finding a fully-sighted run does not — and every finding it does lose
        must be named by a skip.
    """

    @classmethod
    def setUpClass(cls):
        # The sighted reference must use the REAL vocabulary, not the two-entry
        # test fixture. With the fixture's 2 categories standing in for the
        # app's 25, the "full" run is itself blind to 23 of them and the
        # comparison quietly proves nothing — an earlier draft of this test
        # passed while the shadowing defect it exists to catch was live.
        cls.full = make_ctx(cls._cards(), app_categories=cls._real_categories())
        cls.blind = make_ctx(cls._cards(), app_categories=None, app_root=None)

    @staticmethod
    def _cards():
        p = REPO / "seed" / "cards.json"
        if not p.is_file():
            raise unittest.SkipTest("no seed/cards.json in this checkout")
        return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def _real_categories():
        if not vc.MIRROR_CATEGORIES.is_file():
            raise unittest.SkipTest("no vendored category mirror in this checkout")
        return json.loads(vc.MIRROR_CATEGORIES.read_text(encoding="utf-8"))["categories"]

    @staticmethod
    def _fingerprints(ctx):
        return {(f.code, f.card_id, f.block, f.index)
                for _lid, f in run_layers(ctx)}

    def test_a_blind_run_invents_no_finding_the_sighted_run_does_not_have(self):
        extra = self._fingerprints(self.blind) - self._fingerprints(self.full)
        self.assertEqual(sorted({c for c, *_ in extra}), [],
                         "a run with fewer inputs reported findings a full run does not")

    def test_a_blind_run_raises_no_error_the_sighted_run_does_not_have(self):
        def errs(ctx):
            return {(f.code, f.card_id, f.block, f.index)
                    for _lid, f in run_layers(ctx) if f.severity == ERROR}
        self.assertEqual(sorted({c for c, *_ in errs(self.blind) - errs(self.full)}), [])

    def test_every_code_the_blind_run_loses_is_named_by_a_skip(self):
        lost = {c for c, *_ in
                self._fingerprints(self.full) - self._fingerprints(self.blind)}
        declared = set()
        for _lid, s in run_skips(self.blind):
            declared |= set(s.codes)
        self.assertEqual(sorted(lost - declared), [],
                         "these findings vanished with no skip to account for them")


class TestVendoredMirrorRemovesTheAppDependency(unittest.TestCase):
    """kredme-data is PUBLIC, KredMe-main is PRIVATE. CI can never check the app
    out, so the vocabulary has to be vendored or CI is permanently blind."""

    def test_the_mirror_exists_and_parses(self):
        doc = json.loads(vc.MIRROR_CATEGORIES.read_text(encoding="utf-8"))
        self.assertIsInstance(doc.get("categories"), list)
        self.assertTrue(doc["categories"])

    def test_the_mirror_says_it_is_a_mirror_and_how_to_refresh(self):
        """Provenance is the whole difference between a mirror and a fork."""
        doc = json.loads(vc.MIRROR_CATEGORIES.read_text(encoding="utf-8"))
        self.assertIn("MIRROR", doc["_this_file_is"])
        for key in ("_mirrored_from_path", "_copied_on", "_source_sha256",
                    "_how_to_update", "_update_by_hand_when"):
            self.assertIn(key, doc)

    def test_the_mirror_is_not_in_the_publish_surface(self):
        """seed/ is what reaches users. An app-internal table must not sit
        where L3.MANIFEST_UNDECLARED_FILE will advise publishing it."""
        self.assertNotIn("seed", vc.MIRROR_CATEGORIES.parts[-3:])
        self.assertFalse((REPO / "seed" / "app_categories.json").exists())

    def test_a_run_with_no_app_falls_back_to_the_mirror(self):
        ctx, _notes = vc.build_ctx(REPO / "seed", REPO / "news", None, {})
        self.assertEqual(ctx.categories_origin, "mirror")
        self.assertTrue(ctx.have_categories())
        self.assertIn("mirror", ctx.categories_source())

    def test_the_app_checkout_wins_when_it_is_present(self):
        """A mirror is a copy and may be stale. It never outranks the real thing."""
        root = Path(tempfile.mkdtemp(prefix="kredme-test-appwins-"))
        _TMPDIRS.append(root)
        d = root / "assets" / "data" / "categories"
        d.mkdir(parents=True)
        (d / "categories.json").write_text(
            json.dumps([{"id": 9, "category_name": "only_in_the_app",
                         "parent_id": None}]), encoding="utf-8")
        ctx, _notes = vc.build_ctx(REPO / "seed", REPO / "news", root, {})
        self.assertEqual(ctx.categories_origin, "app")
        self.assertEqual(ctx.app_category_names(), {"only_in_the_app"})

    def test_drift_is_detected_when_both_are_readable(self):
        app = [{"id": 1, "category_name": "dining", "parent_id": None}]
        mirror = [{"id": 1, "category_name": "dining", "parent_id": None},
                  {"id": 2, "category_name": "fuel", "parent_id": None}]
        drift = vc.category_drift(app, mirror)
        self.assertEqual([d["slug"] for d in drift], ["fuel"])
        self.assertEqual(drift[0]["kind"], "gone_from_app")

    def test_a_renamed_id_is_drift_and_a_renamed_display_name_is_not(self):
        base = [{"id": 1, "category_name": "dining", "parent_id": None,
                 "display_name": "Dining"}]
        cosmetic = [dict(base[0], display_name="Dining & Restaurants")]
        self.assertEqual(vc.category_drift(base, cosmetic), [])
        reparented = [dict(base[0], parent_id=7)]
        self.assertEqual(len(vc.category_drift(base, reparented)), 1)

    def test_drift_on_a_slug_the_data_uses_is_an_error(self):
        """The severity follows the blast radius: a category our rules point at
        going missing is a wrong number on a phone."""
        ctx = make_ctx()
        ctx.categories_drift = [{"slug": "dining", "kind": "gone_from_app",
                                 "text": "the mirror still has 'dining'"}]
        ctx.categories_origin = "app"
        got = [f for f in importlib.import_module("checks.c3_referential").run(ctx)
               if isinstance(f, Finding) and f.code.startswith("L3.APP_CATEGORY_MIRROR")]
        self.assertEqual([f.code for f in got], ["L3.APP_CATEGORY_MIRROR_DRIFT"])
        self.assertEqual(got[0].severity, ERROR)

    def test_drift_on_a_slug_nothing_uses_is_only_a_warning(self):
        ctx = make_ctx()
        ctx.categories_drift = [{"slug": "not_referenced_anywhere",
                                 "kind": "missing_from_mirror", "text": "..."}]
        ctx.categories_origin = "app"
        got = [f for f in importlib.import_module("checks.c3_referential").run(ctx)
               if isinstance(f, Finding) and f.code.startswith("L3.APP_CATEGORY_MIRROR")]
        self.assertEqual([f.code for f in got], ["L3.APP_CATEGORY_MIRROR_STALE"])
        self.assertEqual(got[0].severity, WARN)

    def test_a_missing_mirror_is_flagged_when_the_app_is_present(self):
        """The one run that CAN see both is the only chance to notice that CI
        has been left blind. It must not pass quietly."""
        ctx = make_ctx()
        ctx.categories_drift = None
        ctx.categories_origin = "app"
        got = {f.code for f in importlib.import_module("checks.c3_referential").run(ctx)
               if isinstance(f, Finding)}
        self.assertIn("L3.APP_CATEGORY_MIRROR_MISSING", got)

    def test_the_key_census_mirror_records_the_redemption_finding(self):
        """The block-reach answer is mirrored too, or four checks silently
        change severity between a developer's run and CI's."""
        doc = json.loads(vc.MIRROR_APP_KEYS.read_text(encoding="utf-8"))
        keys = doc["keys_read_by_app"]
        self.assertFalse(keys["redemption_rules"])
        self.assertTrue(keys["redemption_channels"])

    def test_app_reads_json_key_uses_the_mirror_when_there_is_no_checkout(self):
        ctx = make_ctx(app_root=None, app_keys={"redemption_rules": False})
        self.assertIs(ctx.app_reads_json_key("redemption_rules"), False)

    def test_a_key_the_mirror_does_not_name_stays_unknown(self):
        """The mirror may only answer what it was actually measured on.
        Guessing False would silence a real defect."""
        ctx = make_ctx(app_root=None, app_keys={"redemption_rules": False})
        self.assertIsNone(ctx.app_reads_json_key("something_never_measured"))


class TestDegradedRunIsMachineReadable(unittest.TestCase):
    """CI reads the JSON, not the prose."""

    def _json_for(self, *argv):
        root = Path(tempfile.mkdtemp(prefix="kredme-test-json-"))
        _TMPDIRS.append(root)
        p = root / "out.json"
        code, _t = _cli("--json", str(p), "--quiet", *argv)
        return code, json.loads(p.read_text(encoding="utf-8"))

    def test_json_always_carries_the_skip_list_even_when_empty(self):
        _code, data = self._json_for()
        self.assertIn("skipped_checks", data)
        self.assertIn("degraded", data["meta"])
        self.assertIn("skipped_count", data["meta"])
        self.assertIn("categories_origin", data["meta"])

    def test_json_is_complete_on_a_failing_run(self):
        """A gate that exits 1 must still leave a full report behind, or the
        only run anybody needs to read is the one with no evidence."""
        bad = mutate(lambda e: e["reward_rules"][1].__setitem__("reward_rate", 9.0))
        root = Path(tempfile.mkdtemp(prefix="kredme-test-json-fail-"))
        _TMPDIRS.append(root)
        p = root / "fail.json"
        code, _t = _cli("--json", str(p), "--quiet", cards=[bad])
        self.assertEqual(code, 1)
        data = json.loads(p.read_text(encoding="utf-8"))
        for key in ("meta", "scorecard", "headline", "honesty", "findings",
                    "suppressed", "skipped_checks"):
            self.assertIn(key, data)
        self.assertEqual(data["meta"]["exit_code"], 1)
        self.assertTrue(data["findings"])

    def test_a_skip_is_not_smuggled_into_the_findings_list(self):
        _code, data = self._json_for()
        for f in data["findings"]:
            self.assertIn("severity", f)

    def test_a_display_filter_does_not_shrink_the_json(self):
        """--severity is documented as display-only. If it also truncated the
        machine-readable report, a CI step that shows errors on screen would be
        publishing a report with the warnings quietly deleted."""
        _c1, full = self._json_for()
        _c2, filtered = self._json_for("--severity", "error")
        self.assertEqual(len(full["findings"]), len(filtered["findings"]))

    def test_a_run_that_could_not_run_does_not_leave_the_last_run_s_report(self):
        """The stale-report trap. A run that exits 3 used to leave the previous
        run's file untouched, so a CI step publishing report.json presented a
        completed verdict from a run that never happened."""
        root = Path(tempfile.mkdtemp(prefix="kredme-test-stale-"))
        _TMPDIRS.append(root)
        p = root / "report.json"
        code, _t = _cli("--json", str(p), "--quiet")
        self.assertIn(code, (0, 1, 2))          # it ran; which verdict is not the point
        first = json.loads(p.read_text(encoding="utf-8"))
        self.assertTrue(first["findings"])
        self.assertTrue(first["meta"].get("ran", True))

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                vc.main(["--card", "no_such_card", "--json", str(p), "--quiet"])
        self.assertEqual(cm.exception.code, 3)

        after = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(after["meta"]["exit_code"], 3)
        self.assertFalse(after["meta"]["ran"])
        self.assertEqual(after["findings"], [])
        self.assertIn("error", after["meta"])
        # and it still has the shape a consumer parses, rather than a fragment
        for key in ("meta", "findings", "suppressed", "skipped_checks", "honesty"):
            self.assertIn(key, after)

    def test_the_stub_report_says_empty_findings_does_not_mean_clean(self):
        root = Path(tempfile.mkdtemp(prefix="kredme-test-stub-"))
        _TMPDIRS.append(root)
        p = root / "r.json"
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                vc.main(["--card", "no_such_card", "--json", str(p), "--quiet"])
        text = p.read_text(encoding="utf-8")
        self.assertIn("NOTHING WAS CHECKED", text)
        self.assertIn("meta.exit_code", text)

    def test_die_never_raises_while_invalidating_an_unwritable_report(self):
        """die() runs on the way out of a failure. It may not replace one error
        with another."""
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                vc.main(["--card", "no_such_card", "--quiet",
                         "--json", "/nonexistent-root-dir/x.json"])
        self.assertEqual(cm.exception.code, 3)


def tearDownModule():
    """The suite has just finished pinning a fix for a leaked temp directory.
    It would be a poor look for the suite itself to leave 40 of them behind."""
    for d in _TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)
    _TMPDIRS.clear()


# Codes a clean card produces anyway. Subtracted before asserting an injected
# defect, so a test failure names the missing code rather than the whole set.
_BASELINE_CODES = codes(run_layers(make_ctx()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
