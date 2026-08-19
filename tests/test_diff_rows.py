#!/usr/bin/env python3
"""
Tests for the half of pipeline/diff.py that writes INSIDE the rule arrays.

    python3 tests/test_diff_rows.py            # run all
    python3 tests/test_diff_rows.py -v         # per-test names

stdlib unittest only, no network, no pip installs, no seed/ writes.

Every test here defends one of the rules that made this change safe to make:
which row an observation belongs to, that a row is never duplicated, that a
rule_name is never edited, that a quote must contain the number it is cited for,
that an exclusion is never switched on where the card earns, and that running the
writer twice changes nothing the second time.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import diff  # noqa: E402
from pipeline import taxonomy as T  # noqa: E402
from pipeline.diff import apply_proposals, observations_to_proposals  # noqa: E402

ISSUER_URL = "https://www.hdfc.bank.in/credit-cards/indianoil-hdfc-bank-credit-card"
FETCHED = "2026-08-17T16:04:49Z"


# ---------------------------------------------------------------------------
# A taxonomy small enough to read, shaped exactly like the real files.
# ---------------------------------------------------------------------------
MERCHANTS = {
    "categories": [
        {"id": "dining", "display_name": "Dining & Restaurants", "parent_id": None},
        {"id": "grocery", "display_name": "Grocery & Supermarkets", "parent_id": None},
        {"id": "fuel", "display_name": "Fuel Stations", "parent_id": None},
        {"id": "travel", "display_name": "Travel & Transport", "parent_id": None},
        {"id": "hotels", "display_name": "Hotels & Lodging", "parent_id": "travel"},
        {"id": "rent", "display_name": "Rent Payments", "parent_id": None},
        {"id": "utilities", "display_name": "Utility Bill Payments", "parent_id": None},
        {"id": "jewellery", "display_name": "Jewellery", "parent_id": None},
        {"id": "wallet_load", "display_name": "Wallet Loads", "parent_id": None},
        {"id": "education", "display_name": "Education & Tuition", "parent_id": None},
    ],
    "merchants": [
        {"merchant_name": "indian_oil", "display_name": "IndianOil",
         "category_id": "fuel"},
        {"merchant_name": "bigbasket", "display_name": "BigBasket",
         "category_id": "grocery"},
    ],
}

APP_CATEGORIES = {
    "categories": [
        {"id": 1, "category_name": "dining", "display_name": "Dining & Restaurants",
         "parent_id": None},
        {"id": 2, "category_name": "grocery", "display_name": "Grocery & Supermarkets",
         "parent_id": None},
        {"id": 3, "category_name": "fuel", "display_name": "Fuel Stations",
         "parent_id": None},
        {"id": 4, "category_name": "travel", "display_name": "Travel & Transport",
         "parent_id": None},
        {"id": 5, "category_name": "hotels", "display_name": "Hotels & Lodging",
         "parent_id": 4},
        {"id": 6, "category_name": "rent", "display_name": "Rent Payments",
         "parent_id": None},
        {"id": 7, "category_name": "utilities",
         "display_name": "Utility Bill Payments", "parent_id": None},
        {"id": 8, "category_name": "jewellery", "display_name": "Jewellery",
         "parent_id": None},
        {"id": 9, "category_name": "wallet_load", "display_name": "Wallet Loads",
         "parent_id": None},
        {"id": 10, "category_name": "education",
         "display_name": "Education & Tuition", "parent_id": None},
    ],
}

TAX = T.build_taxonomy(MERCHANTS, APP_CATEGORIES)


# ---------------------------------------------------------------------------
# Fixtures — the real nested shape
# ---------------------------------------------------------------------------
def rule(name, **over):
    row = {
        "rule_name": name,
        "rule_type": "category_bonus",
        "merchant_ref": None,
        "category_ref": None,
        "category_id": None,
        "channel": None,
        "reward_type": "cashback_pct",
        "reward_rate": 0.05,
        "reward_unit_spend": None,
        "cap_amount": None,
        "cap_period": None,
        "min_txn_amount": None,
        "priority": 50,
        "effective_date": None,
        "expiry_date": None,
        "conditions_json": None,
        "confidence": "low",
    }
    row.update(over)
    return row


def card(card_id="hdfc_bank_indianoil_hdfc_bank", **over):
    entry = {
        "card": {
            "id": card_id,
            "card_name": "IndianOil HDFC Bank Credit Card",
            "issuer": "HDFC Bank",
            "network": "visa",
            "card_tier": "entry",
            "annual_fee": 500.0,
            "fee_waiver_spend": None,
            "base_reward_rate": 0.0333,
            "reward_currency": "reward_points",
            "rp_value_standard": 0.2,
            "rp_value_travel": None,
            "rp_value_transfer": None,
            "forex_markup_pct": 3.5,
            "has_rupay_upi": 0,
            "image_asset": "assets/cards/x.png",
            "metadata": {},
            "is_active": 1,
            "is_travel": 0,
            "points_expiry_months": None,
            "min_redemption_points": None,
            "points_clawback_on_default": None,
        },
        "reward_rules": [
            rule("5% Fuel Points on grocery spending", category_id="grocery",
                 category_ref="groceries"),
            rule("24 reward points on every Rs. 150 spent at IndianOil outlets",
                 merchant_ref="indian_oil", reward_type="points_per_spend",
                 reward_rate=24.0, reward_unit_spend=150.0),
        ],
        "exclusion_rules": [
            {"exclusion_type": "other", "exclusion_value": "rent payments",
             "also_excludes_from_threshold": 0},
        ],
        "milestone_rules": [
            {"milestone_name": "Welcome Benefit", "spend_target": 500.0,
             "period": "first 30 days", "bonus_type": "points", "bonus_value": 1000.0,
             "bonus_description": "1,000 Reward Points on the first transaction",
             "is_progressive": 0, "conditions_json": None},
        ],
        "fuel_surcharge_rules": [
            {"waiver_pct": 1.0, "min_txn_amount": 400.0, "max_txn_amount": 5000.0,
             "monthly_cap": 250.0},
        ],
        "redemption_rules": [],
    }
    for key, value in over.items():
        if key in entry["card"]:
            entry["card"][key] = value
        else:
            entry[key] = value
    return entry


def obs(field, value, **over):
    o = {"field": field, "value": value, "unit": "percent", "category": "",
         "source_quote": "", "confidence": "high"}
    o.update(over)
    return o


def propose(entry, observation, url=ISSUER_URL):
    ps = observations_to_proposals(entry, [observation], url, taxonomy=TAX,
                                   fetched_on=FETCHED)
    assert len(ps) == 1, ps
    return ps[0]


def place(entry, observations, url=ISSUER_URL):
    """Propose and apply against a one-card catalogue; return the new entry."""
    ps = observations_to_proposals(entry, observations, url, taxonomy=TAX,
                                   fetched_on=FETCHED)
    new, applied = apply_proposals([entry], ps)
    return new[0], applied, ps


# ---------------------------------------------------------------------------
class TestWhatIsPlaceable(unittest.TestCase):
    """The table itself, because the whole point was widening it."""

    def test_every_row_field_now_has_a_target(self):
        for field in ("category_rate", "category_cap", "excluded_category",
                      "reward_unit_spend", "milestone_spend_inr",
                      "fuel_surcharge_waiver_pct"):
            with self.subTest(field=field):
                self.assertIn(field, diff._TARGETS)
                self.assertIn(field, diff._ROW_TARGETS)

    def test_the_two_new_card_scalars_have_targets(self):
        self.assertEqual(diff._TARGETS["fee_waiver_spend_inr"][0],
                         "card.fee_waiver_spend")
        self.assertEqual(diff._TARGETS["points_expiry_months"][0],
                         "card.points_expiry_months")

    def test_fields_with_nowhere_to_go_are_still_reported_not_dropped(self):
        for field in diff.UNMAPPABLE_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(field, diff._TARGETS)
                p = propose(card(), obs(field, "1000", unit="inr",
                                        source_quote="Joining fee of Rs 1,000 applies."))
                self.assertFalse(p.auto_applicable)
                self.assertEqual(p.blocked_reason, "unmapped_field: " + field)

    def test_rule_name_is_not_writable_on_any_block(self):
        for block, keys in diff.WRITABLE_ROW_KEYS.items():
            with self.subTest(block=block):
                self.assertNotIn("rule_name", keys)
                self.assertNotIn("milestone_name", keys)


# ---------------------------------------------------------------------------
class TestCategoryRate(unittest.TestCase):

    def test_a_percent_lands_as_a_fraction_on_a_cashback_row(self):
        p = propose(card(), obs("category_rate", "5", category="groceries",
                                source_quote="Earn 5% Fuel Points on Groceries."))
        self.assertEqual(p.block, "reward_rules")
        self.assertEqual(p.rows, (0,))
        self.assertEqual(p.new_value, 0.05)
        self.assertEqual(p.unit, diff.UNIT_FRACTION)

    def test_points_and_a_block_land_together_on_a_points_row(self):
        p = propose(card(), obs(
            "category_rate", "24", unit="points", per_spend_inr="150",
            category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent at IndianOil."))
        self.assertEqual(p.rows, (1,))
        self.assertEqual(p.new_value, 24.0)
        self.assertIn(("reward_unit_spend", 150.0), p.writes)

    def test_a_percent_is_refused_on_a_points_row_rather_than_converted(self):
        # Converting needs a point value the page does not state. That is the unit
        # bug that put 0.02% on Axis Neo.
        p = propose(card(), obs(
            "category_rate", "4", category="indianoil fuel",
            source_quote="Save 4% as Reward Points on IndianOil fuel spends at all."))
        self.assertEqual(p.blocked_reason, diff.REASON_ROW_UNIT)

    def test_points_are_refused_on_a_cashback_row(self):
        p = propose(card(), obs(
            "category_rate", "3", unit="points", per_spend_inr="150",
            category="groceries",
            source_quote="Earn 3 Reward Points for every Rs 150 spent on groceries."))
        self.assertEqual(p.blocked_reason, diff.REASON_ROW_UNIT)

    def test_a_rate_that_goes_up_still_waits_for_a_person(self):
        entry = card(reward_rules=[
            rule("Grocery bonus", category_id="grocery", reward_rate=0.05)])
        p = propose(entry, obs("category_rate", "10", category="groceries",
                               source_quote="Earn 10% Fuel Points on Groceries now."))
        self.assertEqual(p.blocked_reason, diff.REASON_UPWARD)


# ---------------------------------------------------------------------------
class TestRowMatching(unittest.TestCase):

    def test_a_category_id_identifies_the_row(self):
        p = propose(card(), obs("category_rate", "5", category="grocery",
                                source_quote="Earn 5% Fuel Points on Groceries."))
        self.assertEqual(p.rows, (0,))

    def test_a_merchant_ref_identifies_the_row_through_merchants_json(self):
        # 'indian_oil' is not a category; seed/merchants.json says it is fuel.
        p = propose(card(), obs(
            "category_cap", "1200", unit="points", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                         "1200 Reward Points per statement cycle."))
        self.assertEqual(p.rows, (1,))

    def test_the_issuers_phrase_matches_the_rules_own_prose(self):
        entry = card()
        entry["reward_rules"][0]["category_id"] = None
        entry["reward_rules"][0]["category_ref"] = "groceries"
        p = propose(entry, obs("category_rate", "5", category="groceries",
                               source_quote="Earn 5% Fuel Points on Groceries."))
        self.assertEqual(p.rows, (0,))

    def test_an_empty_category_cannot_identify_a_row(self):
        p = propose(card(), obs("category_rate", "5", category="",
                                source_quote="Earn 5% Fuel Points on Groceries."))
        self.assertEqual(p.blocked_reason, diff.REASON_NO_ROW)

    def test_a_category_this_card_has_no_rule_for_is_reported(self):
        p = propose(card(), obs("category_rate", "5", category="jewellery",
                                source_quote="Earn 5% Fuel Points on jewellery."))
        self.assertEqual(p.blocked_reason, diff.REASON_NO_ROW)

    def test_two_candidate_rules_that_disagree_are_ambiguous(self):
        entry = card()
        entry["reward_rules"].append(
            rule("3% on groceries at BigBasket", category_id="grocery",
                 category_ref="groceries", reward_rate=0.03))
        p = propose(entry, obs("category_rate", "4", category="groceries",
                               source_quote="Earn 4% Fuel Points on Groceries."))
        self.assertEqual(p.blocked_reason, diff.REASON_AMBIGUOUS_ROW)

    def test_one_rule_fanned_out_across_categories_is_written_as_one(self):
        # 8 rows on IDFC Hello Cashback share a single rule_name because they
        # share a single cap bucket. Writing one and not the others would make
        # the card contradict itself.
        name = "Cashback on essential spends"
        entry = card(reward_rules=[
            rule(name, category_id="rent", category_ref="essentials",
                 reward_rate=0.01),
            rule(name, category_id="utilities", category_ref="essentials",
                 reward_rate=0.01),
        ])
        p = propose(entry, obs("category_rate", "0.5", category="essentials",
                               source_quote="Earn 0.5% cashback on essential spends."))
        self.assertEqual(p.rows, (0, 1))
        self.assertEqual(p.new_value, 0.005)

    def test_a_confirmation_may_cover_several_rules_at_once(self):
        # Nothing numeric moves, so which row was 'really' meant does not matter —
        # only the citation lands.
        entry = card(reward_rules=[
            rule("5% on groceries", category_id="grocery", reward_rate=0.05),
            rule("5% on utility bills", category_id="utilities", reward_rate=0.05),
        ])
        p = propose(entry, obs(
            "category_rate", "5", category="groceries and bill payments",
            source_quote="Earn 5% Fuel Points on Groceries & Bill Payments."))
        self.assertEqual(p.rows, (0, 1))
        self.assertTrue(p.auto_applicable, p.blocked_reason)


# ---------------------------------------------------------------------------
class TestCaps(unittest.TestCase):

    def test_a_points_cap_lands_on_a_points_card(self):
        # The cap travels with the rate: one sentence proving both is what the
        # row ends up quoting, and what L8 later grades it on.
        entry, applied, _ = place(card(), [obs(
            "category_cap", "1200", unit="points", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                         "1200 Reward Points per statement cycle.")])
        self.assertEqual(entry["reward_rules"][1]["cap_amount"], 1200.0)
        self.assertEqual(entry["reward_rules"][1]["cap_period"], "cycle")

    def test_a_rupee_cap_is_refused_on_a_points_card(self):
        # Caps go in the issuer's unit. A rupee cap on a points card corrupts
        # every time somebody corrects the point value — the 5x IndianOil
        # regression.
        p = propose(card(), obs(
            "category_cap", "1200", unit="inr", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                         "Rs 1200 of cashback per statement cycle."))
        self.assertEqual(p.blocked_reason, diff.REASON_ROW_UNIT)

    def test_a_points_cap_is_refused_on_a_rupee_card(self):
        entry = card(reward_currency="cashback_inr")
        p = propose(entry, obs(
            "category_cap", "1000", unit="points", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                         "1000 points per statement cycle."))
        self.assertEqual(p.blocked_reason, diff.REASON_ROW_UNIT)

    def test_a_points_cap_is_refused_where_the_engine_counts_rupees(self):
        # RewardRule.usedAgainstCap counts RUPEES for a cashback_pct rule even on
        # a card that pays in points. 100 points written there is read as ₹100,
        # and at ₹0.20 a point the cap binds five times too early —
        # L4.CAP_IN_RUPEES, an ERROR. 29 of 185 capped rules are already like
        # this; the writer must not add the 30th.
        p = propose(card(), obs(
            "category_cap", "100", unit="points", category="groceries",
            source_quote="Groceries earn 5% Fuel Points – Monthly Max cap: 100 FP."))
        self.assertEqual(p.blocked_reason, diff.REASON_ROW_UNIT)

    def test_a_points_cap_is_fine_where_the_engine_counts_points(self):
        p = propose(card(), obs(
            "category_cap", "1200", unit="points", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                         "1200 Reward Points per statement cycle."))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_a_cap_with_no_period_anywhere_is_refused(self):
        # _checkCap returns null unless both are set, so the rule would pay its
        # bonus rate for ever.
        p = propose(card(), obs(
            "category_cap", "1200", unit="points", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 150 spent. Accrual is "
                         "capped at 1200 Reward Points."))
        self.assertEqual(p.blocked_reason, diff.REASON_CAP_NO_PERIOD)

    def test_the_period_is_read_from_the_issuers_own_words(self):
        for phrase, expected in (("per statement cycle", "cycle"),
                                 ("per calendar month", "month"),
                                 ("per quarter", "quarter"),
                                 ("per annum", "year")):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    diff._cap_period_from_quote("Capped at 1200 points %s." % phrase),
                    expected)

    def test_a_cap_the_rule_name_spells_out_differently_is_refused(self):
        entry = card(reward_rules=[
            rule("24 points per Rs 150, capped at 1200 points per cycle",
                 merchant_ref="indian_oil", reward_type="points_per_spend",
                 reward_rate=24.0, reward_unit_spend=150.0, cap_amount=1200.0,
                 cap_period="cycle"),
        ])
        p = propose(entry, obs(
            "category_cap", "800", unit="points", category="indianoil fuel",
            source_quote="Capped at 800 Reward Points per statement cycle."))
        self.assertEqual(p.blocked_reason, diff.REASON_NAME_DISAGREES)


# ---------------------------------------------------------------------------
class TestTheRuleNameIsNeverTouched(unittest.TestCase):

    def test_a_number_that_contradicts_the_name_is_held_not_written(self):
        # The name says 5%. Writing 3% would make the rule disagree with itself,
        # and editing the name to match would wipe every user's cap progress AND
        # destroy the only independent record of what the rule was meant to be.
        p = propose(card(), obs("category_rate", "3", category="groceries",
                                source_quote="Earn 3% Fuel Points on Groceries."))
        self.assertEqual(p.blocked_reason, diff.REASON_NAME_DISAGREES)

    def test_a_name_with_no_number_cannot_contradict_one(self):
        entry = card(reward_rules=[
            rule("Grocery bonus", category_id="grocery", reward_rate=0.05)])
        p = propose(entry, obs("category_rate", "3", category="groceries",
                               source_quote="Earn 3% Fuel Points on Groceries."))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_applying_never_moves_a_rule_name(self):
        before = card()
        after, applied, _ = place(copy.deepcopy(before), [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries.")])
        self.assertTrue(applied)
        self.assertEqual([r["rule_name"] for r in after["reward_rules"]],
                         [r["rule_name"] for r in before["reward_rules"]])

    def test_the_guard_raises_if_a_name_ever_does_move(self):
        entry = card()
        ps = observations_to_proposals(entry, [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries.")],
            ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        renamed = [diff.replace(ps[0], writes=ps[0].writes + (("rule_name", "x"),))]
        # WRITABLE_ROW_KEYS refuses it at the writer, so nothing is applied at all
        # rather than applied and then caught.
        new, applied = apply_proposals([entry], renamed)
        self.assertEqual(applied, [])
        self.assertEqual(new[0]["reward_rules"][0]["rule_name"],
                         entry["reward_rules"][0]["rule_name"])


# ---------------------------------------------------------------------------
class TestQuotesMustProveTheNumber(unittest.TestCase):

    def test_a_quote_without_the_number_is_refused_and_counted(self):
        p = propose(card(), obs(
            "category_cap", "1200", unit="points", category="indianoil fuel",
            source_quote="Fuel Points are capped every statement cycle, see terms."))
        self.assertEqual(p.blocked_reason, diff.REASON_QUOTE_LACKS_NUMBER)

    def test_the_issuers_indian_grouping_still_counts_as_the_number(self):
        p = propose(card(), obs(
            "fee_waiver_spend_inr", "2,00,000", unit="inr",
            source_quote="Spend ₹2,00,000 or more in a year and the fee is reversed."))
        self.assertEqual(p.new_value, 200000.0)
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_a_zero_stated_as_a_word_is_backed_by_the_word(self):
        # "Nil" is a stated figure with no digit in it, so the WORD is what the
        # sentence has to contain.
        p = propose(card(annual_fee=0.0), obs(
            "annual_fee_inr", "Nil", unit="inr",
            source_quote="Annual Membership Fee: Nil for this card, for ever."))
        self.assertEqual(p.new_value, 0.0)
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_a_zero_the_sentence_does_not_say_is_refused(self):
        p = propose(card(annual_fee=0.0), obs(
            "annual_fee_inr", "Nil", unit="inr",
            source_quote="There is a membership fee, see the schedule of charges."))
        self.assertEqual(p.blocked_reason, diff.REASON_QUOTE_LACKS_NUMBER)

    def test_a_spend_block_must_be_in_the_quote_too(self):
        # '3 points per 150' and '3 points per 100' pay 50% differently and read
        # identically in a report.
        p = propose(card(), obs(
            "category_rate", "24", unit="points", per_spend_inr="150",
            category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every rupee spent at IndianOil."))
        self.assertEqual(p.blocked_reason, diff.REASON_QUOTE_LACKS_NUMBER)

    def test_a_cap_sentence_alone_may_not_cite_a_rule_whose_rate_it_omits(self):
        # "5X is capped at 600 EDGE Miles" proves the 600 and says nothing about
        # the rate. Stamped alone it becomes L8.QUOTE_DOES_NOT_SUPPORT_RATE, an
        # ERROR, and caps the card at grade C.
        entry = card(reward_rules=[
            rule("6X EDGE Miles at IndianOil outlets", merchant_ref="indian_oil",
                 reward_type="multiplier", reward_rate=6.0),
        ])
        p = propose(entry, obs(
            "category_cap", "600", unit="points", category="indianoil fuel",
            source_quote="5X is capped at 600 EDGE Miles per statement month."))
        self.assertEqual(p.blocked_reason, diff.REASON_QUOTE_LACKS_RATE)

    def test_two_sentences_together_may_cover_rate_and_cap(self):
        entry, applied, _ = place(card(), [
            obs("category_rate", "24", unit="points", per_spend_inr="150",
                category="indianoil fuel",
                source_quote="Earn 24 Reward Points on every INR 150 spent at IndianOil."),
            obs("category_cap", "1200", unit="points", category="indianoil fuel",
                source_quote="Fuel Points on 24-point transactions are capped at 1200 "
                             "Reward Points per statement cycle."),
        ])
        row = entry["reward_rules"][1]
        self.assertEqual(row["cap_amount"], 1200.0)
        self.assertEqual(row["cap_period"], "cycle")
        self.assertIn("every INR 150", row["source_quote"])
        self.assertIn("1200 Reward Points", row["source_quote"])


# ---------------------------------------------------------------------------
class TestExclusions(unittest.TestCase):

    def test_an_inert_other_row_is_retyped_so_the_engine_reads_it(self):
        entry, applied, _ = place(card(), [obs(
            "excluded_category", "Rent", unit="category_slug", category="rent",
            source_quote="Rent transactions will not earn any Reward Points.")])
        row = entry["exclusion_rules"][0]
        self.assertEqual(row["exclusion_type"], "category")
        self.assertEqual(row["exclusion_value"], "rent")
        self.assertEqual(row["_retyped_from"], "other:rent payments")

    def test_a_missing_exclusion_is_appended_with_its_evidence(self):
        entry, applied, _ = place(card(), [obs(
            "excluded_category", "Jewellery", unit="category_slug",
            category="jewellery",
            source_quote="Purchase of jewellery or gold coins earns no points.")])
        added = [r for r in entry["exclusion_rules"]
                 if r.get("exclusion_value") == "jewellery"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["exclusion_type"], "category")
        self.assertEqual(added[0]["also_excludes_from_threshold"], 0)
        self.assertEqual(added[0]["source_url"], ISSUER_URL)

    def test_an_exclusion_is_never_appended_twice(self):
        entry = card()
        o = obs("excluded_category", "Jewellery", unit="category_slug",
                category="jewellery",
                source_quote="Purchase of jewellery or gold coins earns no points.")
        once, _a, _p = place(entry, [o])
        twice, applied, _p = place(once, [o])
        self.assertEqual(len(twice["exclusion_rules"]),
                         len(once["exclusion_rules"]))
        self.assertEqual(sum(1 for r in twice["exclusion_rules"]
                             if r.get("exclusion_value") == "jewellery"), 1)

    def test_the_same_reading_twice_in_one_batch_appends_one_row(self):
        # _propose_exclusion reads the card as it was BEFORE the run, so both
        # readings propose a creation and only the writer can tell they are one.
        o = obs("excluded_category", "Jewellery", unit="category_slug",
                category="jewellery",
                source_quote="Purchase of jewellery or gold coins earns no points.")
        second = dict(o)
        second["source_quote"] = "Gold coin purchases do not earn Reward Points."
        entry, applied, _ = place(card(), [o, second])
        jewellery = [r for r in entry["exclusion_rules"]
                     if r.get("exclusion_value") == "jewellery"]
        self.assertEqual(len(jewellery), 1)
        # The second sentence is not thrown away — it lands on the row the first
        # one created, which is also what stops next week's run producing a diff
        # with no issuer change behind it.
        self.assertEqual(len(applied), 2)
        self.assertIn("Gold coin", jewellery[0]["source_quote"])
        self.assertIn("jewellery or gold coins", jewellery[0]["source_quote"])

    def test_two_different_exclusions_are_not_mistaken_for_one_disagreement(self):
        ps = observations_to_proposals(card(), [
            obs("excluded_category", "Jewellery", unit="category_slug",
                category="jewellery",
                source_quote="Purchase of jewellery or gold coins earns no points."),
            obs("excluded_category", "Wallet load", unit="category_slug",
                category="wallet_load",
                source_quote="Wallet load transactions earn no Reward Points."),
        ], ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        self.assertTrue(all(p.auto_applicable for p in ps),
                        [p.blocked_reason for p in ps])
        self.assertNotEqual(ps[0].path, ps[1].path)

    def test_the_guardrail_refuses_to_switch_off_a_category_the_card_earns_in(self):
        # recommendation_engine.dart:308 runs exclusions BEFORE rule matching, so
        # this would zero the card for every grocery merchant, bonus rules and all.
        p = propose(card(), obs(
            "excluded_category", "Groceries", unit="category_slug",
            category="grocery",
            source_quote="Grocery transactions will not earn any Reward Points."))
        self.assertEqual(p.blocked_reason, diff.REASON_CARD_EARNS)

    def test_the_guardrail_looks_at_ancestors_and_descendants_too(self):
        entry = card(reward_rules=[
            rule("5% on hotels", category_id="hotels", reward_rate=0.05)])
        p = propose(entry, obs(
            "excluded_category", "Travel", unit="category_slug", category="travel",
            source_quote="Travel transactions will not earn any Reward Points."))
        self.assertEqual(p.blocked_reason, diff.REASON_CARD_EARNS)

    def test_a_spend_type_the_app_has_no_category_for_is_refused(self):
        for value, category in (("EMI", "emi"),
                                ("ATM Cash withdrawals", "atm_cash_withdrawal"),
                                ("UPI via other apps", "upi_third_party")):
            with self.subTest(value=value):
                p = propose(card(), obs(
                    "excluded_category", value, unit="category_slug",
                    category=category,
                    source_quote="%s transactions are excluded from rewards." % value))
                self.assertEqual(p.blocked_reason, diff.REASON_UNTYPEABLE)

    def test_a_sentence_naming_two_categories_at_once_is_refused(self):
        p = propose(card(), obs(
            "excluded_category", "Rent and Wallet loads", unit="category_slug",
            category="rent_and_wallet",
            source_quote="Rent and wallet load transactions earn no Reward Points."))
        self.assertEqual(p.blocked_reason, diff.REASON_UNTYPEABLE)

    def test_an_exclusion_scoped_to_a_payment_route_is_refused(self):
        # HDFC's own sentence, on both Tata Neu cards and on Millennia. Stored as
        # `category: education` it would stop the card earning on a fee paid
        # straight to the school — which the card DOES pay on — and it would do
        # that before any bonus rule ran. The bank excluded a route, not a
        # category, and an exclusion row has nowhere to put the route.
        p = propose(card(), obs(
            "excluded_category", "third-party-education-payments",
            unit="category_slug", category="education",
            source_quote="Education payments made through third-party apps like "
                         "(but not limited to) CRED, Cheq, MobiKwik, and others "
                         "will NOT earn NeuCoins."))
        self.assertEqual(p.blocked_reason, diff.REASON_EXCLUSION_SCOPED)

    def test_the_route_test_reads_the_value_even_when_the_quote_is_bare(self):
        # The extractor recorded the scope in its own value and the wider word in
        # `category`. Taking the wider of the two is the whole mistake.
        p = propose(card(), obs(
            "excluded_category", "education payments via third party apps",
            unit="category_slug", category="education",
            source_quote="Such education payments will NOT earn Reward Points."))
        self.assertEqual(p.blocked_reason, diff.REASON_EXCLUSION_SCOPED)

    def test_an_inverted_route_scope_is_refused_rather_than_stored_backwards(self):
        # SBI's PhonePe cards exclude utilities only when they are NOT paid on
        # PhonePe. A plain `category: utilities` row states the opposite of that.
        p = propose(card(), obs(
            "excluded_category", "Utilities (non-PhonePe App spends)",
            unit="category_slug", category="utilities",
            source_quote="Utilities (non-PhonePe App spends) 4900, 4814, 4899"))
        self.assertEqual(p.blocked_reason, diff.REASON_EXCLUSION_SCOPED)

    def test_one_route_scoped_item_does_not_veto_the_rest_of_the_list(self):
        # Flipkart Super Elite's exclusion list carries "Third party integrated
        # purchase like Flipkart Health" beside six plain items. Refusing the
        # whole sentence would throw away six good exclusions to catch one bad
        # one, so the test is per clause, not per sentence.
        p = propose(card(), obs(
            "excluded_category", "wallet load", unit="category_slug",
            category="wallet load",
            source_quote="Supercoins shall not be eligible for Fuel Spends, EMI "
                         "transactions, Wallet loading transactions, Purchase of "
                         "Jewellery items, Third party integrated purchase like "
                         "Flipkart Health"))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_a_new_row_records_whether_the_waiver_total_is_hit_too(self):
        # Axis's Atlas sentence puts "spend based fee waiver" in the heading and
        # the categories in the list under it, so the test reads the sentence.
        entry, _applied, _ps = place(card(), [obs(
            "excluded_category", "Jewellery", unit="category_slug",
            category="jewellery",
            source_quote="Excluded spend categories for reward earns / spend "
                         "based fee waiver: Gold/ Jewellery, Rent, Fuel.")])
        row = [r for r in entry["exclusion_rules"]
               if r.get("exclusion_value") == "jewellery"][0]
        self.assertEqual(row["also_excludes_from_threshold"], 1)

    def test_a_rewards_only_exclusion_still_counts_towards_the_waiver(self):
        entry, _applied, _ps = place(card(), [obs(
            "excluded_category", "Jewellery", unit="category_slug",
            category="jewellery",
            source_quote="Purchase of jewellery or gold coins earns no points.")])
        row = [r for r in entry["exclusion_rules"]
               if r.get("exclusion_value") == "jewellery"][0]
        self.assertEqual(row["also_excludes_from_threshold"], 0)

    def test_a_plain_unconditional_exclusion_is_untouched_by_the_route_test(self):
        p = propose(card(), obs(
            "excluded_category", "Jewellery", unit="category_slug",
            category="jewellery",
            source_quote="Purchase of jewellery or gold coins earns no points."))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_an_already_correct_exclusion_only_gains_its_citation(self):
        entry = card(exclusion_rules=[
            {"exclusion_type": "category", "exclusion_value": "rent",
             "also_excludes_from_threshold": 0}])
        after, applied, _ = place(entry, [obs(
            "excluded_category", "Rent", unit="category_slug", category="rent",
            source_quote="Rent transactions will not earn any Reward Points.")])
        self.assertEqual(len(after["exclusion_rules"]), 1)
        self.assertEqual(after["exclusion_rules"][0]["exclusion_type"], "category")
        self.assertEqual(after["exclusion_rules"][0]["source_url"], ISSUER_URL)


# ---------------------------------------------------------------------------
class TestOtherBlocks(unittest.TestCase):

    def test_a_milestone_spend_lands_on_its_own_row(self):
        entry, applied, _ = place(card(), [obs(
            "milestone_spend_inr", "500", unit="inr", category="welcome benefit",
            source_quote="Perform one transaction of INR 500 or above within 30 "
                         "days of card issuance.")])
        self.assertEqual(entry["milestone_rules"][0]["spend_target"], 500.0)
        self.assertEqual(entry["milestone_rules"][0]["source_url"], ISSUER_URL)

    def test_a_milestone_that_matches_nothing_is_reported(self):
        p = propose(card(), obs(
            "milestone_spend_inr", "300000", unit="inr", category="annual milestone",
            source_quote="Spend Rs 300000 in a year for 2,500 bonus miles."))
        self.assertEqual(p.blocked_reason, diff.REASON_NO_ROW)

    def test_a_fuel_surcharge_waiver_lands_when_there_is_one_row(self):
        entry, applied, _ = place(card(), [obs(
            "fuel_surcharge_waiver_pct", "1", unit="percent",
            source_quote="1% Fuel Surcharge waiver at all Indian fuel stations.")])
        self.assertEqual(entry["fuel_surcharge_rules"][0]["waiver_pct"], 1.0)
        self.assertEqual(entry["fuel_surcharge_rules"][0]["confidence"], "high")

    def test_two_surcharge_regimes_are_ambiguous(self):
        entry = card(fuel_surcharge_rules=[
            {"waiver_pct": 1.0, "min_txn_amount": 400.0, "max_txn_amount": 5000.0,
             "monthly_cap": 250.0},
            {"waiver_pct": 1.0, "min_txn_amount": 100.0, "max_txn_amount": 4000.0,
             "monthly_cap": 100.0},
        ])
        p = propose(entry, obs(
            "fuel_surcharge_waiver_pct", "1", unit="percent",
            source_quote="1% Fuel Surcharge waiver at all Indian fuel stations."))
        self.assertEqual(p.blocked_reason, diff.REASON_AMBIGUOUS_ROW)

    def test_a_spend_block_correction_lands_on_the_points_row(self):
        p = propose(card(), obs(
            "reward_unit_spend", "100", unit="inr", category="indianoil fuel",
            source_quote="Reward Points accrue on every INR 100 of fuel spend."))
        # A smaller block means the cardholder earns MORE, so it waits for a
        # person exactly as a rate increase does.
        self.assertEqual(p.blocked_reason, diff.REASON_UPWARD)

    def test_a_bigger_spend_block_is_a_devaluation_and_applies(self):
        p = propose(card(), obs(
            "reward_unit_spend", "200", unit="inr", category="indianoil fuel",
            source_quote="Earn 24 Reward Points on every INR 200 spent on fuel."))
        self.assertTrue(p.auto_applicable, p.blocked_reason)


# ---------------------------------------------------------------------------
class TestConflictingReadings(unittest.TestCase):

    def test_two_numbers_for_one_row_hold_each_other_back(self):
        ps = observations_to_proposals(card(), [
            obs("category_cap", "1200", unit="points", category="indianoil fuel",
                source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                             "1200 Reward Points per statement cycle."),
            obs("category_cap", "800", unit="points", category="indianoil fuel",
                source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                             "800 Reward Points per statement cycle."),
        ], ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        self.assertEqual([p.blocked_reason for p in ps],
                         [diff.REASON_CONFLICT, diff.REASON_CONFLICT])

    def test_the_same_reading_twice_is_agreement_not_conflict(self):
        ps = observations_to_proposals(card(), [
            obs("category_cap", "1200", unit="points", category="indianoil fuel",
                source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                             "1200 Reward Points per statement cycle."),
            obs("category_cap", "1200", unit="points", category="indianoil fuel",
                source_quote="Earn 24 Reward Points on every INR 150 spent. The cap is "
                             "1200 Reward Points per statement cycle."),
        ], ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        self.assertTrue(all(p.auto_applicable for p in ps),
                        [p.blocked_reason for p in ps])

    def test_a_discarded_marketing_sentence_gets_no_vote(self):
        # "up to 3%" is never a reading, so it must not hold back the 5% the bank
        # states plainly beside it.
        entry = card(reward_rules=[
            rule("Grocery bonus", category_id="grocery", reward_rate=0.05)])
        ps = observations_to_proposals(entry, [
            obs("category_rate", "3", category="groceries",
                source_quote="Earn up to 3% Fuel Points on Groceries this season."),
            obs("category_rate", "5", category="groceries",
                source_quote="Earn 5% Fuel Points on Groceries at any store."),
        ], ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        self.assertEqual(ps[0].blocked_reason, diff.REASON_WEASEL)
        self.assertTrue(ps[1].auto_applicable, ps[1].blocked_reason)

    def test_two_card_level_readings_that_disagree_are_held_too(self):
        ps = observations_to_proposals(card(), [
            obs("fee_waiver_spend_inr", "1,00,000", unit="inr",
                source_quote="Spend ₹1,00,000 in a year to get 50% reversed."),
            obs("fee_waiver_spend_inr", "2,00,000", unit="inr",
                source_quote="Spend ₹2,00,000 in a year to get 100% reversed."),
        ], ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        self.assertEqual([p.blocked_reason for p in ps],
                         [diff.REASON_CONFLICT, diff.REASON_CONFLICT])


# ---------------------------------------------------------------------------
class TestProvenanceLandsOnTheRow(unittest.TestCase):
    """The entire point of the change: evidence a rule can be graded on."""

    def test_all_four_keys_are_written_on_the_row_itself(self):
        entry, applied, _ = place(card(), [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries at any store.")])
        row = entry["reward_rules"][0]
        self.assertEqual(row["source_url"], ISSUER_URL)
        self.assertEqual(row["source_fetched_on"], "2026-08-17")
        self.assertEqual(row["confidence"], "high")
        self.assertIn("5% Fuel Points", row["source_quote"])

    def test_the_fetch_date_comes_from_the_fetch_not_from_today(self):
        entry, _a, _p = place(card(), [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries at any store.")])
        self.assertNotEqual(entry["reward_rules"][0]["source_fetched_on"],
                            diff._today())

    def test_the_quote_is_a_string_never_an_object(self):
        # credit_card.dart:415 hard-casts it. A Map there throws inside
        # RewardRule.fromJson, utils.dart:222 swallows it, and the card vanishes
        # from the catalogue with no error surface.
        entry, _a, _p = place(card(), [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries at any store.")])
        self.assertIsInstance(entry["reward_rules"][0]["source_quote"], str)

    def test_no_bare_token_is_written_into__sources(self):
        # L8 reads every _sources entry as a source candidate. The word "bank" has
        # no host, so it files as SOURCE_URL_NOT_A_URL and caps the card at B.
        entry, _a, _p = place(card(), [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries at any store.")])
        self.assertNotIn("_sources", entry["reward_rules"][0])
        self.assertNotIn("_sources", diff.PROVENANCE_ROW_KEYS)

    def test_the_weakest_citation_sets_the_rows_confidence(self):
        # Two sentences, one row, one of them only 'medium'. The gate holds a
        # medium reading back on its own, so this is the only_auto=False path a
        # person takes when they have read both.
        entry = card()
        ps = observations_to_proposals(entry, [
            obs("category_rate", "24", unit="points", per_spend_inr="150",
                category="indianoil fuel",
                source_quote="Earn 24 Reward Points on every INR 150 spent at IndianOil."),
            obs("category_cap", "1200", unit="points", category="indianoil fuel",
                confidence="medium",
                source_quote="Earn 24 Reward Points on every INR 150 spent, capped at "
                             "1200 Reward Points per statement cycle."),
        ], ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        new, applied = apply_proposals([entry], ps, only_auto=False)
        self.assertEqual(len(applied), 2)
        self.assertEqual(new[0]["reward_rules"][1]["confidence"], "medium")


# ---------------------------------------------------------------------------
class TestNothingElseMoves(unittest.TestCase):

    def test_running_it_twice_changes_nothing_the_second_time(self):
        observations = [
            obs("category_rate", "5", category="groceries",
                source_quote="Earn 5% Fuel Points on Groceries at any store."),
            obs("category_cap", "100", unit="points", category="groceries",
                source_quote="Spends on Groceries – Monthly Max cap: 100 FP."),
            obs("excluded_category", "Rent", unit="category_slug", category="rent",
                source_quote="Rent transactions will not earn any Reward Points."),
            obs("fuel_surcharge_waiver_pct", "1", unit="percent",
                source_quote="1% Fuel Surcharge waiver at all Indian fuel stations."),
        ]
        once, _a, _p = place(card(), observations)
        twice, applied, _p = place(copy.deepcopy(once), observations)
        self.assertEqual(json.dumps(once.get("reward_rules"), sort_keys=True),
                         json.dumps(twice.get("reward_rules"), sort_keys=True))
        self.assertEqual(json.dumps(once.get("exclusion_rules"), sort_keys=True),
                         json.dumps(twice.get("exclusion_rules"), sort_keys=True))
        self.assertEqual(json.dumps(once.get("fuel_surcharge_rules"), sort_keys=True),
                         json.dumps(twice.get("fuel_surcharge_rules"), sort_keys=True))

    def test_untouched_rules_come_out_byte_identical(self):
        before = card()
        after, _a, _p = place(copy.deepcopy(before), [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries at any store.")])
        self.assertEqual(json.dumps(after["reward_rules"][1], sort_keys=True),
                         json.dumps(before["reward_rules"][1], sort_keys=True))
        self.assertEqual(json.dumps(after["milestone_rules"], sort_keys=True),
                         json.dumps(before["milestone_rules"], sort_keys=True))

    def test_no_row_is_ever_removed_or_reordered(self):
        before = card()
        after, _a, _p = place(copy.deepcopy(before), [
            obs("excluded_category", "Jewellery", unit="category_slug",
                category="jewellery",
                source_quote="Purchase of jewellery or gold coins earns no points."),
        ])
        self.assertEqual(after["exclusion_rules"][0]["exclusion_value"],
                         before["exclusion_rules"][0]["exclusion_value"])
        self.assertGreaterEqual(len(after["exclusion_rules"]),
                                len(before["exclusion_rules"]))

    def test_the_writer_refuses_a_key_outside_the_whitelist(self):
        entry = card()
        ps = observations_to_proposals(entry, [obs(
            "category_rate", "5", category="groceries",
            source_quote="Earn 5% Fuel Points on Groceries at any store.")],
            ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        tampered = [diff.replace(ps[0], writes=(("priority", 99),))]
        new, applied = apply_proposals([entry], tampered)
        self.assertEqual(applied, [])
        self.assertEqual(new[0]["reward_rules"][0]["priority"], 50)

    def test_a_row_may_only_be_created_in_exclusion_rules(self):
        entry = card()
        ps = observations_to_proposals(entry, [obs(
            "excluded_category", "Jewellery", unit="category_slug",
            category="jewellery",
            source_quote="Purchase of jewellery or gold coins earns no points.")],
            ISSUER_URL, taxonomy=TAX, fetched_on=FETCHED)
        moved = [diff.replace(ps[0], block="reward_rules")]
        new, applied = apply_proposals([entry], moved)
        self.assertEqual(applied, [])
        self.assertEqual(len(new[0]["reward_rules"]), len(entry["reward_rules"]))


# ---------------------------------------------------------------------------
class TestTaxonomy(unittest.TestCase):
    """The category resolver, on this repo's own files rather than a fixture."""

    def test_the_real_files_resolve_the_issuers_words(self):
        tax = T.default_taxonomy()
        self.assertEqual(tax.resolve("groceries"), ("grocery",))
        self.assertEqual(tax.resolve("fuel_indianoil_outlets"), ("fuel",))
        self.assertEqual(tax.category_of_merchant("indian_oil"), "fuel")

    def test_a_word_it_does_not_know_resolves_to_nothing(self):
        self.assertEqual(T.default_taxonomy().resolve("emi"), ())
        self.assertEqual(T.default_taxonomy().resolve("atm_cash_withdrawal"), ())

    def test_the_family_reaches_up_and_down_the_tree(self):
        self.assertIn("hotels", TAX.family("travel"))
        self.assertIn("travel", TAX.family("hotels"))

    def test_missing_files_resolve_nothing_rather_than_crashing(self):
        empty = T.build_taxonomy(None, None)
        self.assertEqual(empty.resolve("groceries"), ())
        self.assertEqual(empty.family("travel"), frozenset({"travel"}))

    def test_indian_digit_grouping_survives_tokenising(self):
        self.assertIn("10000", T.match_tokens("online spends above ₹10,000"))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
