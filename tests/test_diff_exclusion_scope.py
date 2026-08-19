#!/usr/bin/env python3
"""
What an exclusion row actually HITS, and the three guards that read it.

    python3 tests/test_diff_exclusion_scope.py
    python3 tests/test_diff_exclusion_scope.py -v

stdlib unittest only, no network, no pip installs, no seed/ writes.

Every test here defends one thing the 19-Aug provenance pass got wrong:

  * an exclusion typed 'mcc' used to carry an EMPTY category family, so the
    guardrail under it ("never switch on an exclusion where the card earns") and
    the route-scope test both returned False without reading a single reward
    rule. Two rows typed mcc:5816 shipped that way and took Steam, PlayStation
    Store and Xbox from 1.5% / 1.0% to "No rewards on this category" on the two
    Tata Neu cards, for a sentence about Online Skill-Based Gaming.
  * a row that excludes nothing an existing row does not already exclude is not
    protection, and it is not free either: recommendation_engine.dart:288-291
    ranks by how MANY exclusion rows a card carries, so an inert row demotes the
    card behind every equal-scoring rival.
  * a quote spliced out of two non-adjacent sentences, or carrying scraper
    residue, cannot be found on the issuer's page — which is the only thing a
    quote is for.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import diff  # noqa: E402
from pipeline import taxonomy as T  # noqa: E402
from pipeline.diff import apply_proposals, observations_to_proposals  # noqa: E402

ISSUER_URL = "https://www.hdfc.bank.in/credit-cards/tata-neu-plus-hdfc-bank-credit-card"
FETCHED = "2026-08-17T16:04:49Z"

# Shaped exactly like seed/merchants.json, including the mcc_primary field the
# engine matches on. These are the real codes and the real category tagging:
# 5816 is three video-game storefronts in this catalogue and nothing else.
MERCHANTS = {
    "categories": [
        {"id": "entertainment", "display_name": "Entertainment", "parent_id": None},
        {"id": "fuel", "display_name": "Fuel Stations", "parent_id": None},
        {"id": "rent", "display_name": "Rent Payments", "parent_id": None},
        {"id": "grocery", "display_name": "Grocery & Supermarkets", "parent_id": None},
    ],
    "merchants": [
        {"merchant_name": "steam", "display_name": "Steam",
         "category_id": "entertainment", "mcc_primary": "5816"},
        {"merchant_name": "playstation", "display_name": "PlayStation Store",
         "category_id": "entertainment", "mcc_primary": "5816"},
        {"merchant_name": "xbox", "display_name": "Xbox",
         "category_id": "entertainment", "mcc_primary": "5816"},
        {"merchant_name": "nobroker", "display_name": "NoBroker",
         "category_id": "rent", "mcc_primary": "6513"},
        {"merchant_name": "magicbricks", "display_name": "Magicbricks",
         "category_id": "rent", "mcc_primary": "6513"},
        {"merchant_name": "indian_oil", "display_name": "IndianOil",
         "category_id": "fuel", "mcc_primary": "5541"},
        {"merchant_name": "bpcl", "display_name": "BPCL",
         "category_id": "fuel", "mcc_primary": "5541"},
        {"merchant_name": "bigbasket", "display_name": "BigBasket",
         "category_id": "grocery", "mcc_primary": "5411"},
    ],
}

APP_CATEGORIES = {
    "categories": [
        {"id": 1, "category_name": "entertainment", "display_name": "Entertainment",
         "parent_id": None},
        {"id": 2, "category_name": "fuel", "display_name": "Fuel Stations",
         "parent_id": None},
        {"id": 3, "category_name": "rent", "display_name": "Rent Payments",
         "parent_id": None},
        {"id": 4, "category_name": "grocery",
         "display_name": "Grocery & Supermarkets", "parent_id": None},
    ],
}

TAX = T.build_taxonomy(MERCHANTS, APP_CATEGORIES)


def card(exclusions=(), rules=()):
    return {
        "card": {
            "id": "hdfc_bank_tata_neu_plus_hdfc_bank",
            "card_name": "Tata Neu Plus HDFC Bank Credit Card",
            "issuer": "HDFC Bank", "network": "rupay", "card_tier": "entry",
            "annual_fee": 499.0, "fee_waiver_spend": None,
            "base_reward_rate": 0.01, "reward_currency": "neucoins",
            "rp_value_standard": 1.0, "rp_value_travel": None,
            "rp_value_transfer": None, "forex_markup_pct": 3.5,
            "has_rupay_upi": 1, "image_asset": "assets/cards/x.png", "metadata": {},
            "is_active": 1, "is_travel": 0, "points_expiry_months": 12,
            "min_redemption_points": None, "points_clawback_on_default": None,
        },
        "reward_rules": list(rules),
        "exclusion_rules": [dict(r) for r in exclusions],
        "milestone_rules": [], "fuel_surcharge_rules": [], "redemption_rules": [],
    }


def obs(value, **over):
    o = {"field": "excluded_category", "value": value, "unit": "mcc",
         "category": "", "source_quote": "", "confidence": "high"}
    o.update(over)
    return o


def propose(entry, observation):
    ps = observations_to_proposals(entry, [observation], ISSUER_URL, taxonomy=TAX,
                                   fetched_on=FETCHED)
    assert len(ps) == 1, ps
    return ps[0]


# ---------------------------------------------------------------------------
class TestWhatAnMccMeansHere(unittest.TestCase):
    """seed/merchants.json is the only thing that can answer this, and it does."""

    def test_an_mcc_resolves_to_the_categories_it_actually_lands_on(self):
        self.assertEqual(TAX.categories_of_mcc("5816"), frozenset({"entertainment"}))
        self.assertEqual(TAX.categories_of_mcc("6513"), frozenset({"rent"}))
        self.assertEqual(TAX.categories_of_mcc("5541"), frozenset({"fuel"}))

    def test_an_mcc_nothing_carries_resolves_to_nothing_not_to_a_guess(self):
        self.assertEqual(TAX.categories_of_mcc("7995"), frozenset())
        self.assertEqual(TAX.categories_of_mcc(None), frozenset())

    def test_merchants_hit_matches_the_engine_exactly(self):
        # recommendation_engine.dart:484-495 compares mccPrimary and categoryName
        # with ==. No family walk, no aliasing.
        self.assertEqual(TAX.merchants_hit("mcc", "5816"),
                         frozenset({"steam", "playstation", "xbox"}))
        self.assertEqual(TAX.merchants_hit("category", "rent"),
                         frozenset({"nobroker", "magicbricks"}))
        self.assertEqual(TAX.merchants_hit("mcc", "6513"),
                         TAX.merchants_hit("category", "rent"))

    def test_an_inert_row_type_hits_nothing(self):
        # 'other' and 'txn_type' are exactly what the engine does not read.
        self.assertEqual(TAX.merchants_hit("other", "emi transactions"), frozenset())
        self.assertEqual(TAX.merchants_hit("txn_type", "cash_advance"), frozenset())


class TestTheBanksWordsMustAgreeWithOurTagging(unittest.TestCase):
    """The 5816 defect, as a test."""

    def test_skill_gaming_does_not_ship_as_a_bare_video_game_mcc(self):
        p = propose(card(), obs(
            "5816", category="online-skill-based-gaming",
            source_quote="No NeuCoins will be earned on Online Skill-Based Gaming "
                         "transactions (MCC-5816)"))
        self.assertFalse(p.auto_applicable)
        self.assertEqual(p.blocked_reason, diff.REASON_MCC_SCOPE_MISMATCH)

    def test_an_mcc_the_issuer_scopes_the_same_way_we_do_still_ships(self):
        p = propose(card(), obs(
            "6513", category="rent / management fees / rental payments",
            source_quote="W.e.f. March 15th, 2022, supercoin reward shall not be "
                         "eligible for payments made towards MCC 6513 (payment of "
                         "management fees, rental commissions, rental payments or "
                         "any such payments through MCC 6513)"))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_an_mcc_no_merchant_carries_is_not_second_guessed(self):
        # Nothing in the catalogue is tagged 7995, so there is no tagging to
        # disagree with. Refusing here would be inventing a reason.
        p = propose(card(), obs(
            "7995", category="gambling",
            source_quote="No NeuCoins are earned on transactions at MCC 7995."))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_the_guardrail_can_now_see_a_card_earning_under_an_mcc(self):
        # Before the fix `family` was frozenset() on every mcc row and
        # _card_earns_in returned False without reading a rule.
        earns = [{"rule_name": "5% NeuCoins on entertainment", "rule_type":
                  "category_bonus", "category_id": "entertainment",
                  "category_ref": None, "merchant_ref": None, "channel": None,
                  "reward_type": "cashback_pct", "reward_rate": 0.05,
                  "reward_unit_spend": None, "cap_amount": None, "cap_period": None,
                  "min_txn_amount": None, "priority": 55, "effective_date": None,
                  "expiry_date": None, "conditions_json": None,
                  "confidence": "low"}]
        p = propose(card(rules=earns), obs(
            "5816", category="entertainment",
            source_quote="No NeuCoins will be earned on transactions at MCC 5816."))
        self.assertFalse(p.auto_applicable)
        self.assertEqual(p.blocked_reason, diff.REASON_CARD_EARNS)


class TestARowThatExcludesNothingNewIsNotAppended(unittest.TestCase):
    """Atlas category:fuel over mcc:5541, and Flipkart mcc:6513 over category:rent."""

    def test_a_fully_covered_exclusion_lands_on_the_row_that_already_does_it(self):
        entry = card(exclusions=[{"exclusion_type": "mcc", "exclusion_value": "5541",
                                  "also_excludes_from_threshold": 1}])
        before = len(entry["exclusion_rules"])
        p = propose(entry, obs(
            "Fuel", unit="category_slug", category="fuel",
            source_quote="Excluded spend categories for reward earns / spend based "
                         "fee waiver: Transactions made on Gold/ Jewellery, Rent, "
                         "Insurance, Wallet, Government Institutions, Utilities, Fuel."))
        self.assertTrue(p.auto_applicable, p.blocked_reason)
        new, _applied = apply_proposals([entry], [p])
        rows = new[0]["exclusion_rules"]
        self.assertEqual(len(rows), before, "a redundant row was appended")
        self.assertIn("Fuel.", rows[0]["source_quote"])
        self.assertEqual(rows[0]["exclusion_type"], "mcc")

    def test_an_mcc_covered_by_a_category_row_lands_on_that_row(self):
        entry = card(exclusions=[{"exclusion_type": "category",
                                  "exclusion_value": "rent",
                                  "also_excludes_from_threshold": 0}])
        p = propose(entry, obs(
            "6513", category="rent / management fees / rental payments",
            source_quote="W.e.f. March 15th, 2022, supercoin reward shall not be "
                         "eligible for payments made towards MCC 6513 (payment of "
                         "management fees, rental commissions, rental payments or "
                         "any such payments through MCC 6513)"))
        new, _applied = apply_proposals([entry], [p])
        rows = new[0]["exclusion_rules"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exclusion_value"], "rent")
        self.assertIn("MCC 6513", rows[0]["source_quote"])

    def test_a_row_that_covers_something_new_is_still_appended(self):
        # The protection must not be lost to over-eager de-duplication: rent does
        # not cover entertainment.
        entry = card(exclusions=[{"exclusion_type": "category",
                                  "exclusion_value": "rent",
                                  "also_excludes_from_threshold": 0}])
        p = propose(entry, obs(
            "5816", category="entertainment",
            source_quote="No NeuCoins will be earned on transactions at MCC 5816."))
        new, _applied = apply_proposals([entry], [p])
        self.assertEqual(len(new[0]["exclusion_rules"]), 2)

    def test_a_partly_covered_row_is_still_appended(self):
        # An older row must cover ALL of the new row's merchants, not merely
        # overlap it, or real protection would be dropped.
        entry = card(exclusions=[{"exclusion_type": "mcc", "exclusion_value": "5541",
                                  "also_excludes_from_threshold": 0}])
        p = propose(entry, obs(
            "5816", category="entertainment",
            source_quote="No NeuCoins will be earned on transactions at MCC 5816."))
        new, _applied = apply_proposals([entry], [p])
        self.assertEqual(len(new[0]["exclusion_rules"]), 2)


class TestAQuoteMustBeFindableOnTheIssuersPage(unittest.TestCase):

    def test_scraper_residue_is_refused(self):
        self.assertFalse(diff._is_verbatim(
            "Save 4% as Reward Points on IndianOil Fuel Spends. Earn 24 Reward "
            "Points on every INR 150 spent###"))

    def test_two_sentences_joined_by_an_ellipsis_are_refused(self):
        self.assertFalse(diff._is_verbatim(
            "5% back as NeuCoins on Non-EMI Spends on Tata Neu and partner Tata "
            "Brands. … Earn additional 5% back as NeuCoins on selected "
            "categories on Tata Neu App/Website."))
        self.assertFalse(diff._is_verbatim("One sentence. ... Another sentence."))

    def test_an_issuer_sentence_that_merely_trails_off_is_kept(self):
        # Real issuer copy does end mid-thought. Only a JOIN is refused.
        self.assertTrue(diff._is_verbatim(
            "Reward points are capped at 500 per statement cycle..."))
        self.assertTrue(diff._is_verbatim(
            "Rent and government-related transactions will not earn Reward Points."))

    def test_the_gate_blocks_a_spliced_quote(self):
        p = propose(card(), obs(
            "Rent", unit="category_slug", category="rent",
            source_quote="Rent transactions earn no NeuCoins. … Wallet loads "
                         "earn no NeuCoins either."))
        self.assertFalse(p.auto_applicable)
        self.assertEqual(p.blocked_reason, diff.REASON_QUOTE_NOT_VERBATIM)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
