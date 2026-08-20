#!/usr/bin/env python3
"""
Tests for pipeline/newsgen.py — the changes-to-news-feed builder.

    python3 tests/test_newsgen.py            # run all
    python3 tests/test_newsgen.py -v         # per-test names

Stdlib unittest only, no network, no pip installs. Anything touching disk uses a
TemporaryDirectory; the real seed/cards.json is only ever READ, and only by the
one test that asserts targeting works against the shipped catalogue.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline import config as C          # noqa: E402
from pipeline import newsgen              # noqa: E402

ID_CHARSET = re.compile(r"news_[a-z0-9_]+")

NOW = "2026-08-13T00:00:00Z"


# ------------------------------------------------------------- fixtures ----

def card_entry(cid: str, name: str, issuer: str) -> dict:
    """A catalogue entry in the real nested shape. The reward rule is here only
    so a test can prove newsgen never reads or rewrites rule_name."""
    return {
        "card": {
            "id": cid,
            "card_name": name,
            "issuer": issuer,
            "base_reward_rate": 0.0266667,
            "is_active": 1,
        },
        "reward_rules": [{"rule_name": "4 RP per Rs 150", "reward_rate": 4.0}],
        "exclusion_rules": [],
    }


CARDS = [
    card_entry("axis_bank_select", "Axis Bank SELECT Credit Card", "Axis Bank"),
    card_entry("axis_bank_ace", "Axis Bank ACE Credit Card", "Axis Bank"),
    card_entry("axis_bank_vistara", "Axis Bank Vistara Credit Card", "Axis Bank"),
    card_entry("axis_bank_vistara_infinite", "Axis Bank Vistara Infinite Credit Card", "Axis Bank"),
    card_entry("hdfc_bank_millennia", "HDFC Bank Millennia Credit Card", "HDFC Bank"),
    card_entry("idfc_first_bank_select", "IDFC FIRST Select Credit Card", "IDFC FIRST Bank"),
    card_entry("kotak_indianoil", "IndianOil Kotak Credit Card", "Kotak Mahindra Bank"),
]


def change(**over) -> dict:
    base = {
        "issuer": "Axis Bank",
        "headline": "Axis cuts Vistara transfer rate",
        "summary": "Axis will transfer fewer miles per point from 1 September.",
        "card_names": ["SELECT"],
        "severity": "negative",
        "effective_date": "2026-09-01",
        "source_quote": "Effective 1 September 2026, the transfer ratio changes.",
        "affects_rewards": True,
    }
    base.update(over)
    return base


def item(**over) -> dict:
    base = {
        "id": "news_axis_2026_09_01_x",
        "title": "Axis cuts Vistara transfer rate",
        "summary": "Fewer miles per point from 1 September.",
        "category": "devaluation",
        "severity": "negative",
        "source": "Axis Bank",
        "source_url": None,
        "published_at": NOW,
        "expiry_date": None,
        "affected_cards": ["axis_bank_select"],
    }
    base.update(over)
    return base


def run_main(argv: list[str]) -> tuple[int, str, str]:
    """Invoke the CLI in-process so exit codes are asserted without a subprocess."""
    saved = sys.argv
    sys.argv = ["newsgen.py", *argv]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = newsgen.main()
    finally:
        sys.argv = saved
    return code, out.getvalue(), err.getvalue()


# -------------------------------------------------------- next_version ----

class TestNextVersion(unittest.TestCase):
    def test_bumps_the_major(self):
        self.assertEqual(newsgen.next_version("1.0.0"), "2.0.0")
        self.assertEqual(newsgen.next_version("2.0.0"), "3.0.0")
        self.assertEqual(newsgen.next_version("10.0.0"), "11.0.0")
        self.assertEqual(newsgen.next_version("0.0.1"), "1.0.0")

    def test_accepts_short_and_padded_forms(self):
        self.assertEqual(newsgen.next_version("2"), "3.0.0")
        self.assertEqual(newsgen.next_version("  3.0.0  "), "4.0.0")
        self.assertEqual(newsgen.next_version("2.0.0-rc1"), "3.0.0")

    def test_never_produces_a_minor_bump(self):
        # A minor bump is invisible: the app compares only the leading integer.
        for current in ("1.0.0", "1.4.7", "2.1.0", "9.9.9", "12.3.4"):
            with self.subTest(current=current):
                got = newsgen.next_version(current)
                self.assertTrue(got.endswith(".0.0"), got)
                self.assertEqual(int(got.split(".")[0]), int(current.split(".")[0]) + 1)

    def test_rejects_leading_v(self):
        # "v3.0.0" parses to 0 in the app and the feed then loads for nobody.
        for bad in ("v3.0.0", "V3.0.0", "v10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    newsgen.next_version(bad)

    def test_rejects_unparseable(self):
        for bad in ("", "   ", "abc", "3abc", "-1.0.0", ".5", "..", "1_0_0"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    newsgen.next_version(bad)

    def test_rejects_non_strings(self):
        for bad in (None, 3, 3.0, True, ["2.0.0"], {"version": "2.0.0"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    newsgen.next_version(bad)

    def test_error_message_names_the_input(self):
        with self.assertRaises(ValueError) as ctx:
            newsgen.next_version("v3.0.0")
        self.assertIn("v3.0.0", str(ctx.exception))


# ---------------------------------------------------------- slugify_id ----

class TestSlugifyId(unittest.TestCase):
    def test_deterministic(self):
        a = newsgen.slugify_id("Axis Bank", "2026-09-01", "Axis cuts transfer rate")
        b = newsgen.slugify_id("Axis Bank", "2026-09-01", "Axis cuts transfer rate")
        self.assertEqual(a, b)

    def test_charset_and_prefix(self):
        messy = newsgen.slugify_id("HDFC Bank — Cards", "2026/09/01", "₹200 fee!! (all cards)")
        self.assertTrue(messy.startswith("news_"))
        self.assertIsNotNone(ID_CHARSET.fullmatch(messy), messy)

    def test_distinct_inputs_give_distinct_ids(self):
        base = newsgen.slugify_id("Axis Bank", "2026-09-01", "Axis cuts transfer rate")
        self.assertNotEqual(base, newsgen.slugify_id("HDFC Bank", "2026-09-01", "Axis cuts transfer rate"))
        self.assertNotEqual(base, newsgen.slugify_id("Axis Bank", "2026-10-01", "Axis cuts transfer rate"))
        self.assertNotEqual(base, newsgen.slugify_id("Axis Bank", "2026-09-01", "Axis raises the fee"))

    def test_long_headlines_stay_bounded_and_unique(self):
        shared = "Axis Bank announces a very long list of changes to its credit card programme "
        one = newsgen.slugify_id("Axis Bank", "2026-09-01", shared + "affecting fees")
        two = newsgen.slugify_id("Axis Bank", "2026-09-01", shared + "affecting rewards")
        self.assertLessEqual(len(one), len("news_") + newsgen.MAX_SLUG_CHARS + 1 + 10)
        self.assertNotEqual(one, two, "truncation must not collapse two changes onto one id")
        self.assertIsNotNone(ID_CHARSET.fullmatch(one), one)

    def test_empty_inputs_still_produce_a_valid_id(self):
        empty = newsgen.slugify_id("", "", "")
        self.assertTrue(empty.startswith("news_"))
        self.assertIsNotNone(ID_CHARSET.fullmatch(empty), empty)

    def test_undated_change_is_labelled(self):
        self.assertIn("undated", newsgen.slugify_id("Axis Bank", "", "Fee revision"))


# ------------------------------------------------------- change_to_item ----

class TestChangeToItem(unittest.TestCase):
    def test_emits_no_key_outside_news_valid_keys(self):
        built = newsgen.change_to_item(change(), card_ids=["axis_bank_select"], published_at=NOW)
        self.assertTrue(set(built) <= set(C.NEWS_VALID_KEYS), sorted(set(built) - set(C.NEWS_VALID_KEYS)))

    def test_never_emits_the_dead_targeting_key(self):
        # affected_issuers parses in the app and is read by nothing in lib/.
        built = newsgen.change_to_item(
            change(affected_issuers=["axis"]), card_ids=[], published_at=NOW
        )
        self.assertNotIn("affected_issuers", built)

    def test_targets_exactly_the_ids_given(self):
        ids = ["axis_bank_select", "axis_bank_ace"]
        built = newsgen.change_to_item(change(), card_ids=ids, published_at=NOW)
        self.assertEqual(built["affected_cards"], ids)

    def test_affected_cards_is_a_copy_not_an_alias(self):
        ids = ["axis_bank_select"]
        built = newsgen.change_to_item(change(), card_ids=ids, published_at=NOW)
        built["affected_cards"].append("hdfc_bank_millennia")
        self.assertEqual(ids, ["axis_bank_select"])

    def test_does_not_mutate_the_change(self):
        src = change()
        before = copy.deepcopy(src)
        newsgen.change_to_item(src, card_ids=[], published_at=NOW)
        self.assertEqual(src, before)

    def test_expiry_is_90_days_after_a_future_effective_date(self):
        built = newsgen.change_to_item(
            change(effective_date="2026-09-01"), card_ids=[], published_at=NOW
        )
        self.assertEqual(built["expiry_date"], "2026-11-30T00:00:00Z")

    def test_expiry_boundary_same_instant_is_not_upcoming(self):
        built = newsgen.change_to_item(
            change(effective_date="2026-09-01T00:00:00Z"),
            card_ids=[], published_at="2026-09-01T00:00:00Z",
        )
        self.assertIsNone(built["expiry_date"])

    def test_expiry_boundary_one_second_ahead_is_upcoming(self):
        built = newsgen.change_to_item(
            change(effective_date="2026-09-01T00:00:01Z"),
            card_ids=[], published_at="2026-09-01T00:00:00Z",
        )
        self.assertEqual(built["expiry_date"], "2026-11-30T00:00:01Z")

    def test_no_expiry_for_a_past_or_missing_or_junk_effective_date(self):
        for effective in ("2026-01-01", "", None, "sometime soon", 20260901):
            with self.subTest(effective=effective):
                built = newsgen.change_to_item(
                    change(effective_date=effective), card_ids=[], published_at=NOW
                )
                self.assertIsNone(built["expiry_date"])

    def test_drops_a_non_issuer_source_url(self):
        # The feed renders source_url as a tappable link; only issuer pages qualify.
        built = newsgen.change_to_item(
            change(source_url="https://www.cardexpert.in/axis-select/"),
            card_ids=[], published_at=NOW,
        )
        self.assertIsNone(built["source_url"])

    def test_keeps_an_issuer_source_url(self):
        url = "https://www.axisbank.com/support/terms-and-conditions/credit-card"
        built = newsgen.change_to_item(change(source_url=url), card_ids=[], published_at=NOW)
        self.assertEqual(built["source_url"], url)

    def test_severity_passes_through_so_validation_can_catch_it(self):
        built = newsgen.change_to_item(change(severity="critical"), card_ids=[], published_at=NOW)
        self.assertEqual(built["severity"], "critical")
        self.assertTrue(newsgen.validate_item(built))

    def test_missing_or_blank_severity_defaults_to_info(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                built = newsgen.change_to_item(
                    change(severity=value), card_ids=[], published_at=NOW
                )
                self.assertEqual(built["severity"], "info")

    def test_category_is_derived_from_severity_and_reward_impact(self):
        cases = {
            ("negative", True): "devaluation",
            ("warning", True): "devaluation",
            ("positive", True): "enhancement",
            ("info", True): "reward_change",
            ("negative", False): "announcement",
        }
        for (severity, affects), expected in cases.items():
            with self.subTest(severity=severity, affects=affects):
                built = newsgen.change_to_item(
                    change(severity=severity, affects_rewards=affects),
                    card_ids=[], published_at=NOW,
                )
                self.assertEqual(built["category"], expected)

    def test_explicit_category_wins(self):
        built = newsgen.change_to_item(change(category="promo"), card_ids=[], published_at=NOW)
        self.assertEqual(built["category"], "promo")

    def test_source_falls_back_to_kredme(self):
        self.assertEqual(
            newsgen.change_to_item(change(issuer="  "), card_ids=[], published_at=NOW)["source"],
            "KredMe",
        )
        self.assertEqual(
            newsgen.change_to_item(change(), card_ids=[], published_at=NOW)["source"],
            "Axis Bank",
        )

    def test_optional_keys_are_omitted_when_absent(self):
        built = newsgen.change_to_item(change(), card_ids=[], published_at=NOW)
        self.assertNotIn("tags", built)
        self.assertNotIn("action_text", built)

    def test_optional_keys_are_carried_when_present(self):
        built = newsgen.change_to_item(
            change(tags=["devaluation"], action_text="Review your Axis cards"),
            card_ids=[], published_at=NOW,
        )
        self.assertEqual(built["tags"], ["devaluation"])
        self.assertEqual(built["action_text"], "Review your Axis cards")

    def test_output_is_json_serialisable(self):
        built = newsgen.change_to_item(change(), card_ids=["axis_bank_select"], published_at=NOW)
        self.assertIsInstance(json.dumps(built), str)

    def test_rejects_wrong_types(self):
        with self.assertRaises(TypeError):
            newsgen.change_to_item("not a change", card_ids=[], published_at=NOW)
        with self.assertRaises(TypeError):
            newsgen.change_to_item(change(), card_ids="axis_bank_select", published_at=NOW)

    def test_a_well_formed_change_validates_clean(self):
        built = newsgen.change_to_item(change(), card_ids=["axis_bank_select"], published_at=NOW)
        self.assertEqual(newsgen.validate_item(built), [])


# ------------------------------------------------------- validate_item ----

class TestValidateItem(unittest.TestCase):
    def test_valid_item_has_no_errors(self):
        self.assertEqual(newsgen.validate_item(item()), [])

    def test_non_dict_is_reported_not_raised(self):
        errors = newsgen.validate_item(["not", "an", "object"])
        self.assertEqual(len(errors), 1)
        self.assertIn("not an object", errors[0])

    def test_key_the_app_never_reads(self):
        errors = newsgen.validate_item(item(is_pinned=True))
        self.assertTrue(any("is_pinned" in e for e in errors), errors)

    def test_required_strings_must_be_present_and_non_empty(self):
        for key in ("id", "title", "summary"):
            with self.subTest(key=key):
                missing = item()
                del missing[key]
                self.assertTrue(any(repr(key) in e for e in newsgen.validate_item(missing)))
                blank = item(**{key: "   "})
                self.assertTrue(any(repr(key) in e for e in newsgen.validate_item(blank)))
                wrong = item(**{key: 42})
                self.assertTrue(any(repr(key) in e for e in newsgen.validate_item(wrong)))

    def test_invalid_severity_is_caught(self):
        errors = newsgen.validate_item(item(severity="critical"))
        self.assertTrue(any("severity" in e for e in errors), errors)

    def test_missing_severity_is_caught(self):
        broken = item()
        del broken["severity"]
        self.assertTrue(any("severity" in e for e in newsgen.validate_item(broken)))

    def test_every_configured_severity_is_accepted(self):
        for severity in sorted(C.NEWS_SEVERITIES):
            with self.subTest(severity=severity):
                self.assertEqual(newsgen.validate_item(item(severity=severity)), [])

    def test_dates_must_be_iso_or_null(self):
        self.assertEqual(newsgen.validate_item(item(expiry_date=None)), [])
        self.assertEqual(newsgen.validate_item(item(expiry_date="2026-11-30T00:00:00Z")), [])
        self.assertTrue(newsgen.validate_item(item(published_at="13/08/2026")))
        self.assertTrue(newsgen.validate_item(item(expiry_date="next month")))
        self.assertTrue(newsgen.validate_item(item(published_at=20260813)))

    def test_affected_cards_must_be_a_list_of_strings(self):
        self.assertTrue(any("affected_cards" in e
                            for e in newsgen.validate_item(item(affected_cards="all"))))
        self.assertTrue(any("affected_cards[1]" in e
                            for e in newsgen.validate_item(item(affected_cards=["ok", 7]))))
        self.assertTrue(any("affected_cards[0]" in e
                            for e in newsgen.validate_item(item(affected_cards=[" "]))))
        self.assertEqual(newsgen.validate_item(item(affected_cards=[])), [])

    def test_affected_issuers_alone_is_not_targeting(self):
        errors = newsgen.validate_item(item(affected_cards=[], affected_issuers=["axis"]))
        self.assertTrue(any("affected_issuers" in e for e in errors), errors)

    def test_affected_issuers_alongside_real_targeting_is_tolerated(self):
        ok = item(affected_cards=["axis_bank_select"], affected_issuers=["axis"])
        self.assertEqual(newsgen.validate_item(ok), [])


# ------------------------------------------------------------ map_cards ----

class TestMapCards(unittest.TestCase):
    def test_matches_select_by_its_distinguishing_word(self):
        got = newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["SELECT"]}, CARDS)
        self.assertEqual(got, ["axis_bank_select"])

    def test_does_not_match_a_different_card(self):
        got = newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["ACE"]}, CARDS)
        self.assertEqual(got, ["axis_bank_ace"])
        self.assertNotIn("axis_bank_select", got)

    def test_tolerates_suffix_case_and_punctuation(self):
        for name in ("axis bank select credit card", "Axis Bank SELECT Credit Card!",
                     "AXIS-BANK  SELECT  (Credit Card)", "Select"):
            with self.subTest(name=name):
                self.assertEqual(
                    newsgen.map_cards({"issuer": "axis", "card_names": [name]}, CARDS),
                    ["axis_bank_select"],
                )

    def test_unknown_card_name_returns_empty_rather_than_guessing(self):
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["Magnus"]}, CARDS), []
        )

    def test_ambiguous_name_abandons_targeting_for_the_whole_change(self):
        # Two Vistara cards match "Vistara". Picking one would hide the change
        # from the other card's holders, so the item goes untargeted instead.
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["Vistara"]}, CARDS), []
        )
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["SELECT", "Vistara"]}, CARDS),
            [],
        )

    def test_a_name_only_generic_words_matches_nothing(self):
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["Credit Card"]}, CARDS), []
        )

    def test_issuer_scopes_the_search(self):
        # "SELECT" exists under two different issuers in the catalogue.
        self.assertEqual(
            newsgen.map_cards({"issuer": "IDFC FIRST Bank", "card_names": ["Select"]}, CARDS),
            ["idfc_first_bank_select"],
        )
        self.assertEqual(
            newsgen.map_cards({"issuer": "HDFC Bank", "card_names": ["SELECT"]}, CARDS), []
        )

    def test_issuer_tolerates_corporate_suffixes(self):
        self.assertEqual(
            newsgen.map_cards({"issuer": "Kotak", "card_names": ["IndianOil"]}, CARDS),
            ["kotak_indianoil"],
        )
        self.assertEqual(
            newsgen.map_cards({"issuer": "Kotak Mahindra Bank Limited", "card_names": ["IndianOil"]}, CARDS),
            ["kotak_indianoil"],
        )

    def test_unknown_or_blank_issuer_targets_nothing(self):
        for issuer in ("", "   ", "Barclays", None, 7):
            with self.subTest(issuer=issuer):
                self.assertEqual(
                    newsgen.map_cards({"issuer": issuer, "card_names": ["SELECT"]}, CARDS), []
                )
        self.assertEqual(newsgen.map_cards({"card_names": ["SELECT"]}, CARDS), [])

    def test_an_unnamed_change_targets_the_whole_issuer_range(self):
        got = newsgen.map_cards({"issuer": "Axis Bank"}, CARDS)
        self.assertEqual(got, [
            "axis_bank_select", "axis_bank_ace",
            "axis_bank_vistara", "axis_bank_vistara_infinite",
        ])

    def test_blank_card_names_behave_as_unnamed(self):
        got = newsgen.map_cards({"issuer": "HDFC Bank", "card_names": ["", "   "]}, CARDS)
        self.assertEqual(got, ["hdfc_bank_millennia"])

    def test_result_is_deduped(self):
        got = newsgen.map_cards(
            {"issuer": "Axis Bank", "card_names": ["SELECT", "Axis Bank SELECT Credit Card"]},
            CARDS,
        )
        self.assertEqual(got, ["axis_bank_select"])

    def test_malformed_input_returns_empty_instead_of_raising(self):
        self.assertEqual(newsgen.map_cards("nope", CARDS), [])
        self.assertEqual(newsgen.map_cards({"issuer": "Axis Bank"}, "nope"), [])
        self.assertEqual(newsgen.map_cards({"issuer": "Axis Bank"}, []), [])

    def test_malformed_catalogue_entries_are_skipped(self):
        junk = ["nope", None, {}, {"card": {"card_name": "No Id Card", "issuer": "Axis Bank"}},
                {"card": {"id": "", "card_name": "Blank", "issuer": "Axis Bank"}}, *CARDS]
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["SELECT"]}, junk),
            ["axis_bank_select"],
        )

    def test_flat_card_dicts_are_tolerated(self):
        flat = [{"id": "axis_bank_select", "card_name": "Axis Bank SELECT Credit Card",
                 "issuer": "Axis Bank"}]
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["SELECT"]}, flat),
            ["axis_bank_select"],
        )

    def test_never_mutates_the_catalogue(self):
        # Trap 2: the previous scraper overwrote card entries wholesale and lost
        # hand-curated data. Nothing in newsgen may write to a card at all.
        before = copy.deepcopy(CARDS)
        newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["SELECT"]}, CARDS)
        newsgen.map_cards({"issuer": "Axis Bank"}, CARDS)
        self.assertEqual(CARDS, before)
        self.assertEqual(CARDS[0]["reward_rules"][0]["rule_name"], "4 RP per Rs 150")

    @unittest.skipUnless(C.CARDS_JSON.exists(), "seed/cards.json not present")
    def test_against_the_shipped_catalogue(self):
        real = json.loads(C.CARDS_JSON.read_text(encoding="utf-8"))
        self.assertIsInstance(real, list)
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["SELECT"]}, real),
            ["axis_bank_select"],
        )
        self.assertEqual(
            newsgen.map_cards({"issuer": "Axis Bank", "card_names": ["ACE"]}, real),
            ["axis_bank_ace"],
        )
        # Every id we emit must exist, or the publish gate rejects the item as
        # "this alert would reach NOBODY".
        known = {e["card"]["id"] for e in real if isinstance(e.get("card"), dict)}
        for cid in newsgen.map_cards({"issuer": "Axis Bank"}, real):
            self.assertIn(cid, known)


# ----------------------------------------------------------- build_feed ----

class TestBuildFeed(unittest.TestCase):
    def test_bumps_the_major_and_echoes_updated_at(self):
        feed = newsgen.build_feed([], "2.0.0", updated_at=NOW)
        self.assertEqual(feed["version"], "3.0.0")
        self.assertEqual(feed["updated_at"], NOW)
        self.assertEqual(feed["items"], [])

    def test_drops_already_expired_items(self):
        feed = newsgen.build_feed(
            [item(id="stale", expiry_date="2026-01-01T00:00:00Z"),
             item(id="live", expiry_date="2026-12-01T00:00:00Z"),
             item(id="forever", expiry_date=None)],
            "2.0.0", updated_at=NOW,
        )
        self.assertEqual({i["id"] for i in feed["items"]}, {"live", "forever"})

    def test_expiry_boundary_equal_to_updated_at_still_ships(self):
        feed = newsgen.build_feed([item(id="edge", expiry_date=NOW)], "2.0.0", updated_at=NOW)
        self.assertEqual([i["id"] for i in feed["items"]], ["edge"])

    def test_expiry_boundary_one_second_earlier_is_dropped(self):
        feed = newsgen.build_feed(
            [item(id="edge", expiry_date="2026-08-12T23:59:59Z")], "2.0.0", updated_at=NOW
        )
        self.assertEqual(feed["items"], [])

    def test_sorts_newest_first(self):
        feed = newsgen.build_feed(
            [item(id="old", published_at="2026-01-01T00:00:00Z"),
             item(id="new", published_at="2026-08-01T00:00:00Z"),
             item(id="mid", published_at="2026-04-01T00:00:00Z")],
            "2.0.0", updated_at=NOW,
        )
        self.assertEqual([i["id"] for i in feed["items"]], ["new", "mid", "old"])

    def test_ties_break_on_id_for_a_stable_diff(self):
        feed = newsgen.build_feed(
            [item(id="b_second"), item(id="a_first")], "2.0.0", updated_at=NOW
        )
        self.assertEqual([i["id"] for i in feed["items"]], ["a_first", "b_second"])

    def test_undated_items_sort_last(self):
        feed = newsgen.build_feed(
            [item(id="undated", published_at=None), item(id="dated")], "2.0.0", updated_at=NOW
        )
        self.assertEqual([i["id"] for i in feed["items"]], ["dated", "undated"])

    def test_does_not_mutate_the_items_it_was_given(self):
        src = [item(id="one")]
        feed = newsgen.build_feed(src, "2.0.0", updated_at=NOW)
        feed["items"][0]["title"] = "rewritten"
        self.assertEqual(src[0]["title"], "Axis cuts Vistara transfer rate")

    def test_rejects_a_version_the_app_cannot_read(self):
        with self.assertRaises(ValueError):
            newsgen.build_feed([], "v3.0.0", updated_at=NOW)
        with self.assertRaises(ValueError):
            newsgen.build_feed([], None, updated_at=NOW)

    def test_rejects_bad_updated_at(self):
        for bad in ("", "yesterday", None, 20260813):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    newsgen.build_feed([], "2.0.0", updated_at=bad)

    def test_rejects_wrong_item_container_and_members(self):
        with self.assertRaises(TypeError):
            newsgen.build_feed({"items": []}, "2.0.0", updated_at=NOW)
        with self.assertRaises(ValueError):
            newsgen.build_feed([item(), "not an item"], "2.0.0", updated_at=NOW)


# ----------------------------------------------------------- merge_feed ----

class TestMergeFeed(unittest.TestCase):
    def test_new_item_wins_on_a_duplicate_id(self):
        existing = {"version": "2.0.0", "items": [item(id="dupe", title="old title")]}
        merged = newsgen.merge_feed(existing, [item(id="dupe", title="new title")], updated_at=NOW)
        self.assertEqual(len(merged["items"]), 1)
        self.assertEqual(merged["items"][0]["title"], "new title")

    def test_preserves_a_non_expired_existing_item(self):
        existing = {"version": "2.0.0",
                    "items": [item(id="keep", expiry_date="2026-12-01T00:00:00Z")]}
        merged = newsgen.merge_feed(existing, [item(id="fresh")], updated_at=NOW)
        self.assertEqual({i["id"] for i in merged["items"]}, {"keep", "fresh"})

    def test_drops_an_expired_existing_item(self):
        existing = {"version": "2.0.0",
                    "items": [item(id="stale", expiry_date="2026-02-01T00:00:00Z")]}
        merged = newsgen.merge_feed(existing, [item(id="fresh")], updated_at=NOW)
        self.assertEqual([i["id"] for i in merged["items"]], ["fresh"])

    def test_bumps_the_major_from_the_existing_version(self):
        merged = newsgen.merge_feed({"version": "7.0.0", "items": []}, [], updated_at=NOW)
        self.assertEqual(merged["version"], "8.0.0")

    def test_reads_the_articles_alias(self):
        existing = {"version": "2.0.0", "articles": [item(id="legacy")]}
        merged = newsgen.merge_feed(existing, [], updated_at=NOW)
        self.assertEqual([i["id"] for i in merged["items"]], ["legacy"])

    def test_missing_items_is_treated_as_empty(self):
        merged = newsgen.merge_feed({"version": "2.0.0"}, [item(id="only")], updated_at=NOW)
        self.assertEqual([i["id"] for i in merged["items"]], ["only"])

    def test_refuses_an_unreadable_existing_version(self):
        for version in (None, "v3.0.0", "", "abc"):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    newsgen.merge_feed({"version": version, "items": []}, [], updated_at=NOW)
        with self.assertRaises(ValueError):
            newsgen.merge_feed({"items": []}, [], updated_at=NOW)

    def test_rejects_wrong_types(self):
        with self.assertRaises(TypeError):
            newsgen.merge_feed([], [], updated_at=NOW)
        with self.assertRaises(TypeError):
            newsgen.merge_feed({"version": "2.0.0"}, "not a list", updated_at=NOW)
        with self.assertRaises(ValueError):
            newsgen.merge_feed({"version": "2.0.0", "items": "nope"}, [], updated_at=NOW)
        with self.assertRaises(ValueError):
            newsgen.merge_feed({"version": "2.0.0", "items": [None]}, [], updated_at=NOW)

    def test_items_without_ids_are_all_preserved(self):
        no_id_a = item()
        no_id_b = item()
        del no_id_a["id"], no_id_b["id"]
        merged = newsgen.merge_feed({"version": "2.0.0", "items": [no_id_a]}, [no_id_b],
                                    updated_at=NOW)
        self.assertEqual(len(merged["items"]), 2)

    def test_a_rerun_of_the_same_change_does_not_duplicate(self):
        # slugify_id determinism + dedupe-by-id is what makes the weekly job
        # idempotent against a notice page that keeps saying the same thing.
        built = newsgen.change_to_item(change(), card_ids=["axis_bank_select"], published_at=NOW)
        first = newsgen.merge_feed({"version": "2.0.0", "items": []}, [built], updated_at=NOW)
        again = newsgen.change_to_item(change(), card_ids=["axis_bank_select"], published_at=NOW)
        second = newsgen.merge_feed(first, [again], updated_at=NOW)
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["version"], "4.0.0")


# ------------------------------------------------------------------ CLI ----

class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.changes = self.tmp / "changes.json"
        self.cards = self.tmp / "cards.json"
        self.feed = self.tmp / "feed.json"
        self.out = self.tmp / "candidate" / "feed.json"
        self.cards.write_text(json.dumps(CARDS), encoding="utf-8")
        self.feed.write_text(
            json.dumps({"version": "3.0.0", "updated_at": "2026-08-01T00:00:00Z",
                        "items": [item(id="news_001", expiry_date=None)]}),
            encoding="utf-8",
        )
        self.changes.write_text(json.dumps({"changes": [change()]}), encoding="utf-8")

    def argv(self, *extra: str) -> list[str]:
        return ["--changes", str(self.changes), "--cards", str(self.cards),
                "--feed", str(self.feed), "--out", str(self.out),
                "--published-at", NOW, *extra]

    def test_happy_path(self):
        code, out, err = run_main(self.argv())
        self.assertEqual(code, 0, err)
        written = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(written["version"], "4.0.0")
        self.assertEqual(len(written["items"]), 2)
        new_item = [i for i in written["items"] if i["id"] != "news_001"][0]
        self.assertEqual(new_item["affected_cards"], ["axis_bank_select"])
        self.assertEqual(new_item["expiry_date"], "2026-11-30T00:00:00Z")
        self.assertIn("validation        clean", out)

    def test_never_writes_the_live_feed(self):
        before = self.feed.read_text(encoding="utf-8")
        run_main(self.argv())
        self.assertEqual(self.feed.read_text(encoding="utf-8"), before)

    def test_fresh_drops_existing_items(self):
        code, _, err = run_main(self.argv("--fresh"))
        self.assertEqual(code, 0, err)
        written = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(len(written["items"]), 1)

    def test_rewards_only_skips_process_changes(self):
        self.changes.write_text(
            json.dumps([change(affects_rewards=False)]), encoding="utf-8")
        code, out, err = run_main(self.argv("--rewards-only"))
        self.assertEqual(code, 0, err)
        self.assertIn("1 skipped", out)
        self.assertEqual(len(json.loads(self.out.read_text(encoding="utf-8"))["items"]), 1)

    def test_missing_changes_file_is_config_error(self):
        code, _, err = run_main(
            ["--changes", str(self.tmp / "nope.json"), "--cards", str(self.cards),
             "--feed", str(self.feed), "--out", str(self.out)]
        )
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_missing_cards_file_is_config_error(self):
        code, _, err = run_main(
            ["--changes", str(self.changes), "--cards", str(self.tmp / "nope.json"),
             "--feed", str(self.feed), "--out", str(self.out)]
        )
        self.assertEqual(code, 2)
        self.assertIn("catalogue", err)

    def test_unreadable_current_version_is_config_error(self):
        code, _, err = run_main(
            ["--changes", str(self.changes), "--cards", str(self.cards),
             "--feed", str(self.tmp / "absent.json"), "--out", str(self.out)]
        )
        self.assertEqual(code, 2)
        self.assertIn("--current-version", err)

    def test_current_version_override_works_without_a_feed(self):
        code, _, err = run_main(
            ["--changes", str(self.changes), "--cards", str(self.cards),
             "--feed", str(self.tmp / "absent.json"), "--out", str(self.out),
             "--current-version", "5.0.0", "--published-at", NOW]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8"))["version"], "6.0.0")

    def test_malformed_changes_json_is_data_error(self):
        self.changes.write_text("{not json", encoding="utf-8")
        code, _, err = run_main(self.argv())
        self.assertEqual(code, 1)
        self.assertIn("cannot read input", err)

    def test_changes_of_the_wrong_shape_is_data_error(self):
        self.changes.write_text(json.dumps({"changes": {"issuer": "Axis"}}), encoding="utf-8")
        code, _, err = run_main(self.argv())
        self.assertEqual(code, 1)
        self.assertIn("must be a list", err)

    def test_a_non_object_change_is_a_data_error(self):
        self.changes.write_text(json.dumps(["just a string"]), encoding="utf-8")
        self.assertEqual(run_main(self.argv())[0], 1)

    def test_feed_with_a_v_prefixed_version_refuses_to_publish(self):
        self.feed.write_text(json.dumps({"version": "v3.0.0", "items": []}), encoding="utf-8")
        code, _, err = run_main(self.argv())
        self.assertEqual(code, 1)
        self.assertIn("v3.0.0", err)

    def test_validation_failure_exits_one_but_still_writes_the_candidate(self):
        self.changes.write_text(json.dumps([change(severity="critical")]), encoding="utf-8")
        code, _, err = run_main(self.argv())
        self.assertEqual(code, 1)
        self.assertIn("severity", err)
        self.assertTrue(self.out.exists(), "the candidate is written so a human can read it")

    def test_untargeted_items_are_announced_as_such(self):
        self.changes.write_text(
            json.dumps([change(issuer="Barclays", card_names=["Anything"])]), encoding="utf-8")
        code, out, err = run_main(self.argv())
        self.assertEqual(code, 0, err)
        self.assertIn("EVERYONE (untargeted)", out)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
