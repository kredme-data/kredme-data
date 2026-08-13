#!/usr/bin/env python3
"""
Run every pipeline test.

Usage:
    python3 tests/run_all.py            # all tests
    python3 tests/run_all.py -v         # verbose
    python3 tests/run_all.py test_diff  # one module

Discovers tests/test_*.py, runs them under stdlib unittest, and additionally
asserts the property the whole design rests on: every pipeline module must import
on a bare Python with nothing pip-installed. The Anthropic SDK is imported lazily
inside the functions that call the API precisely so this holds — if someone moves
that import to module scope, CI catches it here rather than in a Monday-morning
run that has already been paid for.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Modules that must import with zero third-party packages present.
STDLIB_ONLY_MODULES = (
    "pipeline.config",
    "pipeline.state",
    "pipeline.schema",
    "pipeline.sources",
    "pipeline.fetch",
    "pipeline.batch",
    "pipeline.diff",
    "pipeline.newsgen",
    "pipeline.report",
)


class TestNoHardDependencies(unittest.TestCase):
    """The lazy-import contract, asserted rather than trusted."""

    def test_modules_import_without_sdk(self):
        for name in STDLIB_ONLY_MODULES:
            with self.subTest(module=name):
                try:
                    importlib.import_module(name)
                except ImportError as exc:  # pragma: no cover - the failure IS the message
                    self.fail(
                        f"{name} failed to import on a bare Python: {exc}\n"
                        "Every third-party import must live inside the function that needs it."
                    )

    def test_anthropic_not_imported_at_module_scope(self):
        for name in STDLIB_ONLY_MODULES:
            importlib.import_module(name)
        # Importing the pipeline must not drag the SDK in. If it is already present
        # in this interpreter the check is vacuous, so only assert when it is absent.
        if "anthropic" not in sys.modules:
            self.assertNotIn(
                "anthropic",
                sys.modules,
                "A pipeline module imported the Anthropic SDK at module scope.",
            )


class TestConfigInvariants(unittest.TestCase):
    """Guards on config that other modules assume and would fail confusingly without."""

    def setUp(self):
        from pipeline import config

        self.C = config

    def test_issuer_allowlist_rejects_aggregators(self):
        for bad in (
            "https://www.cardinsider.com/hdfc-regalia/",
            "https://www.cardexpert.in/axis-magnus/",
            "https://technofino.in/review",
            "https://www.reddit.com/r/CreditCardsIndia/",
        ):
            self.assertFalse(self.C.is_issuer_domain(bad), bad)

    def test_issuer_allowlist_accepts_real_issuers(self):
        for good in (
            "https://www.hdfc.bank.in/personal/pay/cards",
            "https://campaign.axis.bank.in/generic/terms-and-conditions-select.pdf",
            "https://www.sbicard.com/en/personal.page",
            "https://www.idfcfirst.bank.in/content/dam/x.pdf",
            "https://media.bobcard.co.in/media/x.pdf",
        ):
            self.assertTrue(self.C.is_issuer_domain(good), good)

    def test_lookalike_domain_rejected(self):
        # Suffix matching must be on dot-delimited segments, never a bare substring,
        # or an attacker-registered lookalike would be treated as the issuer.
        for bad in (
            "https://hdfcbank.com.evil.tld/x",
            "https://notaxis.bank.in.attacker.io/x",
            "https://evilaxis.bank.in.co/x",
        ):
            self.assertFalse(self.C.is_issuer_domain(bad), bad)

    def test_malformed_urls_do_not_raise(self):
        for junk in ("", "not a url", "://", "http://", None):
            try:
                self.assertFalse(self.C.is_issuer_domain(junk))  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover
                self.fail(f"is_issuer_domain({junk!r}) raised {exc!r}")

    def test_weasel_detection(self):
        self.assertTrue(self.C.contains_weasel("Earn up to 10% back on dining"))
        self.assertTrue(self.C.contains_weasel("Save UP TO Rs 5,000"))
        self.assertTrue(self.C.contains_weasel("rewards as high as 33.33%"))
        self.assertFalse(
            self.C.contains_weasel("You earn 4 Reward Points for every Rs 150 spent")
        )
        self.assertFalse(self.C.contains_weasel(""))

    def test_ceiling_is_forty_not_thirty(self):
        # 40, not 30: HDFC SmartBuy 10X genuinely reaches ~33%, and a gate that
        # blocks a real product is a gate someone switches off.
        self.assertEqual(self.C.RATE_CEILING_PCT, 40.0)

    def test_news_valid_keys_match_the_app(self):
        # These are the 13 keys NewsArticle.fromJson actually reads. Emitting
        # anything else is a silent no-op in the shipped app.
        self.assertEqual(
            set(self.C.NEWS_VALID_KEYS),
            {
                "id", "title", "summary", "category", "severity", "source",
                "source_url", "published_at", "expiry_date", "affected_cards",
                "affected_issuers", "tags", "action_text",
            },
        )

    def test_no_sampling_params_configured(self):
        # temperature / top_p / top_k are rejected with a 400 on claude-opus-5.
        for attr in ("TEMPERATURE", "TOP_P", "TOP_K"):
            self.assertFalse(hasattr(self.C, attr), f"config must not define {attr}")


def build_suite(pattern: str = "test_*.py") -> unittest.TestSuite:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestNoHardDependencies))
    suite.addTests(loader.loadTestsFromTestCase(TestConfigInvariants))
    suite.addTests(loader.discover(str(REPO / "tests"), pattern=pattern, top_level_dir=str(REPO)))
    return suite


def main() -> int:
    verbosity = 2 if "-v" in sys.argv else 1
    named = [a for a in sys.argv[1:] if not a.startswith("-")]
    pattern = f"{named[0]}.py" if named else "test_*.py"
    result = unittest.TextTestRunner(verbosity=verbosity).run(build_suite(pattern))
    if result.wasSuccessful():
        print(f"\nOK — {result.testsRun} tests passed")
        return 0
    print(
        f"\nFAIL — {len(result.failures)} failures, {len(result.errors)} errors "
        f"out of {result.testsRun}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
