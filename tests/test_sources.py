#!/usr/bin/env python3
"""
Tests for pipeline/sources.py — issuer resolution and the allowlist gate.

Usage:
    python3 tests/test_sources.py            # run all
    python3 tests/test_sources.py -v         # per-test names

Fixtures are built inline: the real seed/cards.json is 380 cards and 1.7 MB, and
a unit test that reads it stops being a test of this module the moment someone
edits a card. The two exceptions are deliberate and named: ISSUER_LANDING and
pipeline/sources_overrides.json are checked against the live allowlist, because a
typo'd host there fails silently in production and nowhere else.

Nothing here touches the network. Stdlib only — unittest, no pytest.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import config as C  # noqa: E402
from pipeline import sources as S  # noqa: E402


# ------------------------------------------------------------- fixtures ----

def card(card_id: str, **over) -> dict:
    """A card entry in the real nested shape, with the fields sources.py reads."""
    inner = {
        "id": card_id,
        "card_name": card_id.replace("_", " ").title(),
        "issuer": "HDFC Bank",
        "base_reward_rate": 0.0266667,
        "rp_value_standard": 0.25,
        "is_active": 1,
    }
    inner.update(over.pop("card", {}))
    entry = {
        "card": inner,
        "reward_rules": [],
        "exclusion_rules": [],
        "milestone_rules": [],
        "redemption_rules": [],
        "fuel_surcharge_rules": [],
    }
    entry.update(over)
    return entry


def rule(name: str, url: str | None = None) -> dict:
    """A reward rule shaped like the seed's, optionally carrying a source_url."""
    out = {
        "rule_name": name,
        "reward_type": "points_per_spend",
        "reward_rate": 4.0,
        "reward_unit_spend": 150.0,
        "cap_amount": None,
        "cap_period": None,
    }
    if url is not None:
        out["source_url"] = url
    return out


def write_json(path: pathlib.Path, obj) -> pathlib.Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


@contextlib.contextmanager
def captured_stderr():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        yield buf


# ------------------------------------------------------------ issuer_of ----

class TestIssuerOf(unittest.TestCase):
    # Every distinct spelling of `issuer` in the real seed, with the slug it must
    # produce. Regenerate with: jq -r '.[].card.issuer' seed/cards.json | sort -u
    SPELLINGS = {
        "SBI Card": "sbi",
        "HDFC Bank": "hdfc",
        "Axis Bank": "axis",
        "ICICI Bank": "icici",
        "RBL Bank": "rbl",
        "Kotak Mahindra Bank": "kotak",
        "Kotak Mahindra": "kotak",
        "IndusInd Bank": "indusind",
        "YES Bank": "yes",
        "Yes Bank": "yes",
        "AU Small Finance Bank": "au",
        "AU Bank": "au",
        "AU Bank (co-branded with Aditya Birla Finance Limited)": "au",
        "BOBCARD (Bank of Baroda)": "bobcard",
        "BOBCARD": "bobcard",
        "BOBCARD LIMITED": "bobcard",
        "BOBCARD Limited (Bank of Baroda)": "bobcard",
        "BOBCARD LIMITED (Bank of Baroda)": "bobcard",
        "BOBCARD (Bank of Baroda) in partnership with Uni App": "bobcard",
        "IDFC FIRST Bank": "idfc",
        "IDFC First Bank": "idfc",
        "IDFC Bank": "idfc",
        "American Express": "amex",
        "HSBC": "hsbc",
        "HSBC Bank": "hsbc",
        "IDBI Bank": "idbi",
        "Standard Chartered": "sc",
        "Standard Chartered Bank": "sc",
        "Federal Bank": "federal",
        "City Union Bank": "city_union",
        "CSB Bank": "csb",
        "SBM Bank": "sbm",
        "Slice Bank": "slice",
        "Unity Small Finance Bank": "unity",
        "FPL Technologies Pvt. Ltd.": "onecard",
        "Federal Bank / BOBCARD (Scapia)": "federal",
    }

    def test_every_seed_spelling_maps(self):
        self.assertEqual(len(self.SPELLINGS), 36, "the seed ships 36 distinct issuer strings")
        for text, slug in self.SPELLINGS.items():
            with self.subTest(issuer=text):
                self.assertEqual(S.issuer_of(card("x", card={"issuer": text})), slug)

    def test_dual_issuer_takes_the_first_named(self):
        # 'Federal Bank / BOBCARD (Scapia)' names two. The first publishes the T&C.
        self.assertEqual(
            S.issuer_of(card("scapia", card={"issuer": "Federal Bank / BOBCARD (Scapia)"})),
            "federal",
        )

    def test_falls_back_to_card_id_when_issuer_blank(self):
        for card_id, slug in (
            ("hdfc_bank_regalia_gold", "hdfc"),
            ("axis_bank_select", "axis"),
            ("sbi_card_phonepe_sbi_purple", "sbi"),
            ("bobcard_(bank_of_baroda)_card_eterna", "bobcard"),
            ("sc_digismart", "sc"),
            ("amex_platinum_travel", "amex"),
            ("fpl_technologies_pvt._ltd._onecard_metal", "onecard"),
            ("idfc_first_bank_mayura", "idfc"),
            ("au_small_finance_bank_addon", "au"),
            ("unity_small_finance_bank_roarbank", "unity"),
        ):
            with self.subTest(card_id=card_id):
                self.assertEqual(S.issuer_of(card(card_id, card={"issuer": ""})), slug)

    def test_accepts_a_bare_card_dict_as_well_as_an_entry(self):
        self.assertEqual(S.issuer_of({"id": "hdfc_bank_infinia", "issuer": "HDFC Bank"}), "hdfc")

    def test_unknown_when_nothing_identifies_the_issuer(self):
        self.assertEqual(S.issuer_of(card("mystery_card_9", card={"issuer": "Cosmic Trust Co"})), "unknown")

    def test_unknown_on_malformed_input(self):
        self.assertEqual(S.issuer_of({}), "unknown")
        self.assertEqual(S.issuer_of({"card": "not a dict"}), "unknown")
        self.assertEqual(S.issuer_of([]), "unknown")

    def test_substring_does_not_count_as_a_match(self):
        # Whole-word matching only. 'Discover' contains 's','c' adjacently and
        # would otherwise be filed under Standard Chartered; 'Bauhaus' contains
        # 'au'. Both must come back unknown rather than confidently wrong.
        for text in ("Discover Financial Services", "Bauhaus Finance", "Rescue Credit Union"):
            with self.subTest(issuer=text):
                self.assertEqual(S.issuer_of(card("b1", card={"issuer": text})), "unknown")

    def test_short_slug_markers_still_match_as_whole_words(self):
        # The other half of the same rule: 'sc_beyond' must still reach sc.
        self.assertEqual(S.issuer_of(card("sc_beyond", card={"issuer": ""})), "sc")
        self.assertEqual(S.issuer_of(card("au_bank_zenith", card={"issuer": ""})), "au")


# ------------------------------------------------------------ load_cards ----

class TestLoadCards(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_happy_path(self):
        path = write_json(self.tmp / "cards.json", [card("hdfc_bank_infinia")])
        loaded = S.load_cards(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["card"]["id"], "hdfc_bank_infinia")

    def test_empty_list_is_valid(self):
        path = write_json(self.tmp / "cards.json", [])
        self.assertEqual(S.load_cards(path), [])

    def test_missing_file_raises_with_the_path_in_the_message(self):
        missing = self.tmp / "nope.json"
        with self.assertRaises(FileNotFoundError) as ctx:
            S.load_cards(missing)
        self.assertIn(str(missing), str(ctx.exception))

    def test_not_a_list_raises_value_error(self):
        path = write_json(self.tmp / "cards.json", {"cards": []})
        with self.assertRaises(ValueError) as ctx:
            S.load_cards(path)
        self.assertIn("expected a JSON list", str(ctx.exception))
        self.assertIn("dict", str(ctx.exception))

    def test_malformed_json_raises_value_error(self):
        path = self.tmp / "cards.json"
        path.write_text("[{unclosed", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            S.load_cards(path)
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_accepts_a_string_path(self):
        path = write_json(self.tmp / "cards.json", [card("axis_bank_neo")])
        self.assertEqual(len(S.load_cards(str(path))), 1)


# -------------------------------------------------------- load_overrides ----

class TestLoadOverrides(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(S.load_overrides(self.tmp / "absent.json"), {})

    def test_happy_path(self):
        path = write_json(
            self.tmp / "o.json",
            {"axis_bank_neo": "https://campaign.axis.bank.in/generic/terms-and-conditions-neo.pdf"},
        )
        self.assertEqual(
            S.load_overrides(path),
            {"axis_bank_neo": "https://campaign.axis.bank.in/generic/terms-and-conditions-neo.pdf"},
        )

    def test_comment_keys_are_documentation_not_overrides(self):
        path = write_json(
            self.tmp / "o.json",
            {"_comment": "read me", "_note": "https://www.cardinsider.com/x", "hdfc_bank_infinia": "https://www.hdfc.bank.in/x"},
        )
        with captured_stderr() as errbuf:
            self.assertEqual(list(S.load_overrides(path)), ["hdfc_bank_infinia"])
        # Skipped in silence, not warned about: a WARN on every run for a key
        # that is doing its job trains the operator to ignore WARNs.
        self.assertEqual(errbuf.getvalue(), "")

    def test_aggregator_url_is_warned_and_skipped(self):
        path = write_json(self.tmp / "o.json", {"hdfc_bank_infinia": "https://www.cardinsider.com/infinia"})
        with captured_stderr() as errbuf:
            self.assertEqual(S.load_overrides(path), {})
        self.assertIn("WARN", errbuf.getvalue())
        self.assertIn("www.cardinsider.com", errbuf.getvalue())

    def test_lookalike_domain_is_warned_and_skipped(self):
        path = write_json(self.tmp / "o.json", {"hdfc_bank_infinia": "https://hdfcbank.com.evil.tld/infinia"})
        with captured_stderr() as errbuf:
            self.assertEqual(S.load_overrides(path), {})
        self.assertIn("hdfcbank.com.evil.tld", errbuf.getvalue())

    def test_non_string_and_blank_values_are_skipped(self):
        path = write_json(
            self.tmp / "o.json",
            {"a": None, "b": 42, "c": "   ", "d": ["https://www.hdfc.bank.in/x"], "e": "https://www.hdfc.bank.in/x"},
        )
        with captured_stderr() as errbuf:
            self.assertEqual(list(S.load_overrides(path)), ["e"])
        self.assertEqual(errbuf.getvalue().count("WARN"), 4)

    def test_surrounding_whitespace_is_stripped(self):
        path = write_json(self.tmp / "o.json", {"a": "  https://www.hdfc.bank.in/x  "})
        self.assertEqual(S.load_overrides(path), {"a": "https://www.hdfc.bank.in/x"})

    def test_not_an_object_raises(self):
        path = write_json(self.tmp / "o.json", ["https://www.hdfc.bank.in/x"])
        with self.assertRaises(ValueError) as ctx:
            S.load_overrides(path)
        self.assertIn("expected a JSON object", str(ctx.exception))

    def test_malformed_json_raises_rather_than_silently_dropping_pins(self):
        path = self.tmp / "o.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            S.load_overrides(path)
        self.assertIn("unreadable overrides file", str(ctx.exception))


# ------------------------------------------------------- resolve_sources ----

class TestResolveSources(unittest.TestCase):
    def test_embedded_issuer_url_wins_over_landing_page(self):
        deep = "https://www.hdfc.bank.in/personal/pay/cards/credit-cards/infinia"
        entry = card("hdfc_bank_infinia", reward_rules=[rule("Base earn", deep)])
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, deep)
        self.assertEqual(src.reason, "")
        self.assertEqual(src.issuer, "hdfc")
        self.assertEqual(src.card_name, "Hdfc Bank Infinia")

    def test_landing_page_used_when_the_card_carries_no_url(self):
        [src] = S.resolve_sources([card("hdfc_bank_millennia")])
        self.assertEqual(src.url, S.ISSUER_LANDING["hdfc"])
        self.assertEqual(src.reason, "")

    def test_override_beats_the_embedded_url(self):
        embedded = "https://www.hdfc.bank.in/personal/pay/cards/credit-cards/regalia"
        pinned = "https://www.hdfc.bank.in/personal/pay/cards/credit-cards/regalia-gold"
        entry = card("hdfc_bank_regalia_gold", reward_rules=[rule("Base earn", embedded)])
        [src] = S.resolve_sources([entry], {"hdfc_bank_regalia_gold": pinned})
        self.assertEqual(src.url, pinned)

    def test_override_for_another_card_does_not_leak(self):
        entry = card("hdfc_bank_millennia")
        [src] = S.resolve_sources([entry], {"hdfc_bank_regalia_gold": "https://www.hdfc.bank.in/x"})
        self.assertEqual(src.url, S.ISSUER_LANDING["hdfc"])

    def test_blank_override_falls_through(self):
        [src] = S.resolve_sources([card("hdfc_bank_millennia")], {"hdfc_bank_millennia": "   "})
        self.assertEqual(src.url, S.ISSUER_LANDING["hdfc"])

    def test_aggregator_url_on_the_card_is_rejected(self):
        entry = card("hdfc_bank_infinia", reward_rules=[rule("Base earn", "https://www.cardinsider.com/hdfc-infinia")])
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, "")
        self.assertEqual(src.reason, "not_issuer_domain: www.cardinsider.com")

    def test_aggregator_does_not_silently_fall_back_to_the_landing_page(self):
        # Substituting the landing page here would hide an aggregator URL sitting
        # in the seed, which is the whole point of the gate.
        entry = card("hdfc_bank_infinia", reward_rules=[rule("Base earn", "https://www.cardexpert.in/hdfc-infinia")])
        [src] = S.resolve_sources([entry])
        self.assertNotEqual(src.url, S.ISSUER_LANDING["hdfc"])
        self.assertEqual(src.url, "")

    def test_lookalike_domain_is_rejected(self):
        entry = card("hdfc_bank_infinia", reward_rules=[rule("Base earn", "https://hdfcbank.com.evil.tld/infinia")])
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, "")
        self.assertEqual(src.reason, "not_issuer_domain: hdfcbank.com.evil.tld")

    def test_true_subdomain_is_accepted(self):
        # The mirror image of the lookalike case: suffix segments, not substrings.
        url = "https://offers.smartbuy.hdfc.bank.in/diners-black"
        entry = card("hdfc_bank_diners_club_black", reward_rules=[rule("SmartBuy 10X", url)])
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, url)

    def test_bad_override_is_rejected_even_when_it_bypasses_load_overrides(self):
        # load_overrides screens the file, but resolve_sources is callable with a
        # hand-built dict, so the gate is enforced at the point of use too.
        [src] = S.resolve_sources(
            [card("hdfc_bank_infinia")], {"hdfc_bank_infinia": "https://www.cardinsider.com/infinia"}
        )
        self.assertEqual(src.url, "")
        self.assertEqual(src.reason, "not_issuer_domain: www.cardinsider.com")

    def test_first_issuer_url_wins_when_the_card_stores_several(self):
        issuer_url = "https://www.hdfc.bank.in/personal/pay/cards/credit-cards/infinia"
        entry = card(
            "hdfc_bank_infinia",
            reward_rules=[rule("Aggregator sourced", "https://www.cardinsider.com/x"), rule("Issuer sourced", issuer_url)],
        )
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, issuer_url)

    def test_issuer_without_a_landing_page_reports_the_gap(self):
        entry = card("csb_bank_edge_plus", card={"issuer": "CSB Bank"})
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, "")
        self.assertEqual(src.reason, "no_issuer_landing: csb")

    def test_unknown_issuer_and_no_url_reports_unknown_issuer(self):
        entry = card("mystery_card", card={"issuer": "Cosmic Trust Co"})
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, "")
        self.assertEqual(src.issuer, "unknown")
        self.assertEqual(src.reason, "unknown_issuer")

    def test_unknown_issuer_still_resolves_if_it_carries_an_issuer_url(self):
        url = "https://www.sbicard.com/en/personal/credit-cards/x.page"
        entry = card("mystery_card", card={"issuer": "Cosmic Trust Co"}, reward_rules=[rule("Base", url)])
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, url)
        self.assertEqual(src.issuer, "unknown")

    def test_urls_are_found_on_every_rule_section(self):
        url = "https://www.kotak.bank.in/en/personal-banking/cards/credit-cards/indian-oil-credit-card.html"
        for section in ("reward_rules", "exclusion_rules", "milestone_rules", "fuel_surcharge_rules"):
            with self.subTest(section=section):
                entry = card("kotak_mahindra_bank_indianoil_kotak", card={"issuer": "Kotak Mahindra Bank"})
                entry[section] = [rule("IndianOil fuel", url)]
                [src] = S.resolve_sources([entry])
                self.assertEqual(src.url, url)

    def test_url_inside_a_nested_sources_list_is_found(self):
        url = "https://www.yes.bank.in/personal-banking/cards/uni"
        entry = card("yes_bank_uni", card={"issuer": "YES Bank"})
        entry["redemption_rules"] = [{"rule_name": "Cash redeem", "_sources": ["cardinsider", url]}]
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, url)

    def test_trailing_sentence_punctuation_is_trimmed(self):
        entry = card("hdfc_bank_infinia")
        entry["reward_rules"] = [
            {"rule_name": "Base", "source_quote": "See https://www.hdfc.bank.in/infinia.html."}
        ]
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, "https://www.hdfc.bank.in/infinia.html")

    def test_string_where_a_rule_list_should_be_does_not_crash(self):
        # yes_bank_uni_rupay really does ship exclusion_rules as a prose string.
        entry = card("yes_bank_uni_rupay", card={"issuer": "YES Bank"})
        entry["exclusion_rules"] = "IDENTICAL to yes_bank_uni above — copy the array verbatim."
        [src] = S.resolve_sources([entry])
        self.assertEqual(src.url, S.ISSUER_LANDING["yes"])

    # --- is_active, every boundary the seed could hand us -------------------

    def test_inactive_cards_are_skipped(self):
        cards = [card("live_one"), card("dead_one", card={"is_active": 0})]
        got = S.resolve_sources(cards)
        self.assertEqual([s.card_id for s in got], ["live_one"])

    def test_is_active_boundaries(self):
        for value, expected_active in (
            (1, True), (0, False), (True, True), (False, False),
            ("1", True), ("0", False), ("false", False), ("no", False),
            (1.0, True), (0.0, False), (-1, True), (None, True), ("", False),
        ):
            with self.subTest(is_active=repr(value)):
                got = S.resolve_sources([card("c", card={"is_active": value})])
                self.assertEqual(bool(got), expected_active)

    def test_absent_is_active_counts_as_shipping(self):
        entry = card("c")
        del entry["card"]["is_active"]
        self.assertEqual(len(S.resolve_sources([entry])), 1)

    # --- malformed entries --------------------------------------------------

    def test_malformed_entries_are_skipped_with_a_warning(self):
        cards = ["not a card", 7, None, {"card": "also not a card"}, card("good_one")]
        with captured_stderr() as errbuf:
            got = S.resolve_sources(cards)
        self.assertEqual([s.card_id for s in got], ["good_one"])
        self.assertEqual(errbuf.getvalue().count("WARN"), 4)

    def test_entry_without_an_id_is_skipped(self):
        entry = card("x")
        entry["card"]["id"] = "   "
        with captured_stderr() as errbuf:
            self.assertEqual(S.resolve_sources([entry]), [])
        self.assertIn("no id", errbuf.getvalue())

    def test_empty_input_is_empty_output(self):
        self.assertEqual(S.resolve_sources([]), [])

    def test_order_follows_the_seed(self):
        cards = [card("a_one"), card("b_two"), card("c_three")]
        self.assertEqual([s.card_id for s in S.resolve_sources(cards)], ["a_one", "b_two", "c_three"])

    # --- the traps ----------------------------------------------------------

    def test_resolution_never_mutates_the_card_data(self):
        """Trap 2: the previous scraper overwrote card entries and lost curation."""
        cards = [
            card("hdfc_bank_regalia_gold", reward_rules=[rule("5X on SmartBuy", "https://www.hdfc.bank.in/x")]),
            card("csb_bank_edge_plus", card={"issuer": "CSB Bank"}),
            card("dead_one", card={"is_active": 0}),
        ]
        before = copy.deepcopy(cards)
        S.resolve_sources(cards, {"hdfc_bank_regalia_gold": "https://www.hdfc.bank.in/y"})
        self.assertEqual(cards, before)

    def test_rule_names_are_never_touched(self):
        """Trap 1: the app keys cap progress on the raw rule_name string."""
        raw = "10X Rewards on SmartBuy  (up to 15,000 points/month)"
        entry = card("hdfc_bank_infinia", reward_rules=[rule(raw, "https://www.hdfc.bank.in/x")])
        S.resolve_sources([entry])
        self.assertEqual(entry["reward_rules"][0]["rule_name"], raw)

    def test_rate_fields_are_never_touched(self):
        """Trap 3: 'N points per Rs X' must keep its block size."""
        entry = card("hdfc_bank_infinia", reward_rules=[rule("Base earn", "https://www.hdfc.bank.in/x")])
        S.resolve_sources([entry])
        self.assertEqual(entry["reward_rules"][0]["reward_rate"], 4.0)
        self.assertEqual(entry["reward_rules"][0]["reward_unit_spend"], 150.0)
        self.assertEqual(entry["card"]["base_reward_rate"], 0.0266667)

    def test_source_is_frozen(self):
        [src] = S.resolve_sources([card("hdfc_bank_infinia")])
        with self.assertRaises(Exception):
            src.url = "https://www.cardinsider.com/x"  # type: ignore[misc]


# ------------------------------------------------------- coverage_report ----

class TestCoverageReport(unittest.TestCase):
    def test_empty(self):
        report = S.coverage_report([])
        self.assertEqual(report, {"total": 0, "resolved": 0, "unresolved": 0, "by_reason": {}, "by_issuer": {}})

    def test_arithmetic_sums_to_total(self):
        cards = [
            card("hdfc_bank_infinia"),                                            # landing
            card("hdfc_bank_regalia_gold"),                                       # landing
            card("axis_bank_neo", card={"issuer": "Axis Bank"}),                  # landing
            card("csb_bank_edge_plus", card={"issuer": "CSB Bank"}),              # no landing
            card("sbm_bank_sbm_zet", card={"issuer": "SBM Bank"}),                # no landing
            card("mystery", card={"issuer": "Cosmic Trust Co"}),                  # unknown issuer
            card("bad_source", reward_rules=[rule("Base", "https://www.cardinsider.com/x")]),
            card("dead_one", card={"is_active": 0}),                              # skipped entirely
        ]
        sources = S.resolve_sources(cards)
        report = S.coverage_report(sources)

        self.assertEqual(report["total"], 7)
        self.assertEqual(report["resolved"] + report["unresolved"], report["total"])
        self.assertEqual(report["resolved"], 3)
        self.assertEqual(report["unresolved"], 4)
        self.assertEqual(sum(report["by_reason"].values()), report["unresolved"])
        self.assertEqual(
            sum(v["resolved"] + v["unresolved"] for v in report["by_issuer"].values()), report["total"]
        )
        self.assertEqual(sum(v["resolved"] for v in report["by_issuer"].values()), report["resolved"])
        self.assertEqual(report["by_issuer"]["hdfc"], {"resolved": 2, "unresolved": 1})
        self.assertEqual(report["by_reason"]["no_issuer_landing: csb"], 1)
        self.assertEqual(report["by_reason"]["unknown_issuer"], 1)
        self.assertEqual(report["by_reason"]["not_issuer_domain: www.cardinsider.com"], 1)

    def test_resolved_counts_a_url_not_an_empty_reason(self):
        # The second Source has neither a URL nor an explanation. Counting it as
        # resolved would report a card as fetchable with nothing to fetch, so the
        # tally must key off the URL.
        sources = [
            S.Source("a", "A", "hdfc", "https://www.hdfc.bank.in/x", ""),
            S.Source("b", "B", "hdfc", "", ""),
            S.Source("c", "C", "csb", "", "no_issuer_landing: csb"),
        ]
        report = S.coverage_report(sources)
        self.assertEqual((report["total"], report["resolved"], report["unresolved"]), (3, 1, 2))
        self.assertEqual(report["by_issuer"]["hdfc"], {"resolved": 1, "unresolved": 1})


# ------------------------------------------- the two real-file guardrails ----

class TestShippedConfiguration(unittest.TestCase):
    def test_every_landing_page_is_on_the_allowlist(self):
        self.assertTrue(S.ISSUER_LANDING, "landing map must not be empty")
        for issuer, url in S.ISSUER_LANDING.items():
            with self.subTest(issuer=issuer):
                self.assertTrue(C.is_issuer_domain(url), f"{issuer} landing page {url} is off-allowlist")

    def test_shipped_overrides_file_loads_and_is_all_issuer_domains(self):
        overrides = S.load_overrides(S.OVERRIDES_JSON)
        self.assertGreaterEqual(len(overrides), 14)
        raw = json.loads(S.OVERRIDES_JSON.read_text(encoding="utf-8"))
        self.assertIn("_comment", raw, "the file must explain itself to the next operator")
        # Nothing was dropped on the way in — a WARN-skipped entry would show up
        # here as a shorter dict than the file.
        self.assertEqual(len(overrides), len([k for k in raw if not k.startswith("_")]))
        for card_id, url in overrides.items():
            with self.subTest(card_id=card_id):
                self.assertTrue(C.is_issuer_domain(url))

    def test_landing_pages_cover_the_issuers_that_have_an_allowlisted_host(self):
        for issuer in ("hdfc", "sbi", "axis", "icici", "kotak", "idfc", "rbl", "indusind",
                       "yes", "au", "hsbc", "amex", "sc", "bobcard", "idbi", "federal"):
            with self.subTest(issuer=issuer):
                self.assertIn(issuer, S.ISSUER_LANDING)


# ------------------------------------------------------------------ CLI ----

class TestMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._argv = sys.argv
        self.addCleanup(lambda: setattr(sys, "argv", self._argv))

    def _run(self, *args: str) -> tuple[int, str]:
        sys.argv = ["sources.py", *args]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = S.main()
        return code, buf.getvalue()

    def test_table_output(self):
        cards_path = write_json(self.tmp / "cards.json", [card("hdfc_bank_infinia"), card("csb", card={"issuer": "CSB Bank"})])
        code, out = self._run("--cards", str(cards_path), "--overrides", str(self.tmp / "absent.json"))
        self.assertEqual(code, 0)
        self.assertIn("Source coverage — 2 active cards", out)
        self.assertIn("no_issuer_landing: csb", out)

    def test_json_output_is_parseable(self):
        cards_path = write_json(self.tmp / "cards.json", [card("hdfc_bank_infinia")])
        code, out = self._run("--json", "--cards", str(cards_path), "--overrides", str(self.tmp / "absent.json"))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload[0]["card_id"], "hdfc_bank_infinia")
        self.assertEqual(sorted(payload[0]), ["card_id", "card_name", "issuer", "reason", "url"])

    def test_low_coverage_still_exits_zero(self):
        cards_path = write_json(self.tmp / "cards.json", [card("csb", card={"issuer": "CSB Bank"})])
        code, _ = self._run("--cards", str(cards_path), "--overrides", str(self.tmp / "absent.json"))
        self.assertEqual(code, 0)

    def test_unreadable_seed_exits_one(self):
        code, _ = self._run("--cards", str(self.tmp / "absent.json"))
        self.assertEqual(code, 1)

    def test_broken_overrides_file_exits_one(self):
        cards_path = write_json(self.tmp / "cards.json", [card("hdfc_bank_infinia")])
        bad = self.tmp / "o.json"
        bad.write_text("{nope", encoding="utf-8")
        code, _ = self._run("--cards", str(cards_path), "--overrides", str(bad))
        self.assertEqual(code, 1)

    def test_unresolved_listing(self):
        cards_path = write_json(self.tmp / "cards.json", [card("csb_bank_edge_plus", card={"issuer": "CSB Bank"})])
        code, out = self._run("--unresolved", "--cards", str(cards_path), "--overrides", str(self.tmp / "absent.json"))
        self.assertEqual(code, 0)
        self.assertIn("csb_bank_edge_plus", out)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
