#!/usr/bin/env python3
"""
Tests for pipeline/diff.py — the module that decides what may change a live card.

    python3 tests/test_diff.py            # run all
    python3 tests/test_diff.py -v         # per-test names

stdlib unittest only, no network, no pip installs. The last block runs the real
380-card seed/cards.json through apply_proposals to prove the traps hold on the
actual catalogue rather than only on a fixture.
"""
from __future__ import annotations

import copy
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import config as C  # noqa: E402
from pipeline import diff  # noqa: E402
from pipeline.diff import (  # noqa: E402
    Proposal,
    apply_proposals,
    gate,
    observations_to_proposals,
    render_markdown,
    summarise,
)

ISSUER_URL = "https://www.hdfcbank.com/personal/pay/cards/credit-cards/regalia"
GOOD_QUOTE = "The Card earns 4 Reward Points for every Rs 150 spent on retail purchases."


# ---------------------------------------------------------------------------
# Fixtures — the real nested shape, with rules that must survive untouched
# ---------------------------------------------------------------------------
def card_entry(card_id: str = "hdfc_bank_regalia", **card_over) -> dict:
    inner = {
        "id": card_id,
        "card_name": "HDFC Regalia Credit Card",
        "issuer": "HDFC Bank",
        "network": "visa",
        "card_tier": "premium",
        "annual_fee": 2500.0,
        "fee_waiver_spend": 300000.0,
        "base_reward_rate": 0.0266667,
        "reward_currency": "reward_points",
        "rp_value_standard": 0.5,
        "rp_value_travel": None,
        "rp_value_transfer": None,
        "forex_markup_pct": 2.0,
        "has_rupay_upi": 0,
        "image_asset": "assets/cards/hdfc_bank_regalia.png",
        "metadata": {"contactless": True, "material": "Metal",
                     "invite_only": False, "no_preset_limit": False},
        "is_active": 1,
        "is_travel": 0,
        "points_expiry_months": 36,
        "min_redemption_points": 500,
        "points_clawback_on_default": None,
    }
    inner.update(card_over)
    return {
        "card": inner,
        "reward_rules": [
            {
                "rule_name": "4 reward points on every Rs. 150 spent",
                "rule_type": "base_rate",
                "reward_type": "points_per_spend",
                "reward_rate": 4.0,
                "reward_unit_spend": 150.0,
                "cap_amount": None,
                "cap_period": None,
                "merchant_ref": None,
                "category_ref": None,
                "priority": 20,
            },
            {
                "rule_name": "5X Reward Points on SmartBuy",
                "rule_type": "category",
                "reward_type": "multiplier",
                "reward_rate": 5.0,
                "reward_unit_spend": None,
                "cap_amount": 2000.0,
                "cap_period": "cycle",
                "merchant_ref": "smartbuy",
                "category_ref": None,
                "priority": 10,
            },
        ],
        "exclusion_rules": [{"rule_name": "No points on fuel", "category": "fuel"}],
        "milestone_rules": [{"rule_name": "10,000 points at Rs 5 lakh spend"}],
        "redemption_rules": [{"rule_name": "Points to flights at Rs 0.50"}],
        "fuel_surcharge_rules": [{"rule_name": "1% waived, capped at Rs 500"}],
        "benefits": ["Airport lounge access", "Golf"],
        "sources": ["https://www.hdfcbank.com/regalia-mitc"],
        "changelog": [{"on": "2026-05-15", "what": "Hand-curated from the MITC"}],
    }


def obs(field: str = "base_reward_rate", **over) -> dict:
    o = {
        "field": field,
        "value": "3",
        "unit": "points",
        "per_spend_inr": "150",
        "category": "",
        "source_quote": GOOD_QUOTE,
        "effective_date": "",
        "confidence": "high",
    }
    o.update(over)
    return o


def one(entry: dict, observation: dict, url: str = ISSUER_URL) -> Proposal:
    proposals = observations_to_proposals(entry, [observation], url)
    assert len(proposals) == 1
    return proposals[0]


def proposal(**over) -> Proposal:
    """A bare Proposal, already in the shape observations_to_proposals emits."""
    base = dict(
        card_id="hdfc_bank_regalia",
        field="base_reward_rate",
        path="card.base_reward_rate",
        old_value=0.0266667,
        new_value=0.02,
        unit=diff.UNIT_POINTS_PER_RUPEE,
        source_url=ISSUER_URL,
        source_quote=GOOD_QUOTE,
        confidence="high",
        delta_pct=-25.0,
    )
    base.update(over)
    return Proposal(**base)


# ---------------------------------------------------------------------------
class TestMapping(unittest.TestCase):
    """Observations land on the field seed/cards.json actually stores."""

    def test_points_per_block_becomes_points_per_rupee(self):
        p = one(card_entry(), obs(value="4", per_spend_inr="150"))
        self.assertEqual(p.path, "card.base_reward_rate")
        self.assertEqual(p.unit, diff.UNIT_POINTS_PER_RUPEE)
        # 4/150, NOT a percentage: the block size is preserved by the denominator.
        self.assertAlmostEqual(p.new_value, 0.02666667, places=8)
        self.assertAlmostEqual(p.old_value, 0.0266667, places=7)

    def test_points_with_no_block_is_not_a_rate(self):
        p = one(card_entry(), obs(value="4", per_spend_inr=""))
        self.assertIsNone(p.new_value)
        self.assertEqual(p.blocked_reason, diff.REASON_UNPARSEABLE)

    def test_points_with_zero_block_does_not_divide_by_zero(self):
        p = one(card_entry(), obs(value="4", per_spend_inr="0"))
        self.assertIsNone(p.new_value)
        self.assertEqual(p.blocked_reason, diff.REASON_UNPARSEABLE)

    def test_percent_base_rate_accepted_only_when_a_point_is_a_rupee(self):
        cashback = card_entry("au_bank_spont", rp_value_standard=1.0,
                              base_reward_rate=0.01, reward_currency="cashback_inr")
        p = one(cashback, obs(value="2", unit="percent", per_spend_inr=""))
        self.assertAlmostEqual(p.new_value, 0.02, places=8)

    def test_percent_base_rate_refused_on_a_points_card(self):
        # rp_value_standard is 0.5 here, so storing 0.02 would render 1%, not 2%.
        p = one(card_entry(), obs(value="2", unit="percent", per_spend_inr=""))
        self.assertIsNone(p.new_value)
        self.assertEqual(p.blocked_reason, diff.REASON_UNPARSEABLE)

    def test_point_value_maps_to_rp_value_standard(self):
        p = one(card_entry(), obs("point_value_inr", value="0.20", unit="inr",
                                  per_spend_inr=""))
        self.assertEqual(p.path, "card.rp_value_standard")
        self.assertEqual(p.new_value, 0.2)
        self.assertEqual(p.old_value, 0.5)

    def test_point_value_above_the_app_maximum_is_not_storable(self):
        # The app collapses anything over 1.5 to Rs 0.25, so writing it is worse
        # than doing nothing.
        p = one(card_entry(), obs("point_value_inr", value="5.0", unit="inr",
                                  per_spend_inr=""))
        self.assertIsNone(p.new_value)
        self.assertEqual(p.blocked_reason, diff.REASON_UNPARSEABLE)

    def test_point_value_at_the_app_maximum_is_storable(self):
        p = one(card_entry(), obs("point_value_inr", value="1.5", unit="inr",
                                  per_spend_inr=""))
        self.assertEqual(p.new_value, 1.5)

    def test_annual_fee_parses_rupee_formatting(self):
        p = one(card_entry(), obs("annual_fee_inr", value="Rs 1,500", unit="inr",
                                  per_spend_inr="",
                                  source_quote="The annual membership fee is Rs 1,500 plus taxes."))
        self.assertEqual(p.path, "card.annual_fee")
        self.assertEqual(p.new_value, 1500.0)
        self.assertTrue(p.auto_applicable)

    def test_nil_annual_fee_reads_as_zero(self):
        p = one(card_entry(), obs("annual_fee_inr", value="Nil", unit="inr",
                                  per_spend_inr="",
                                  source_quote="Annual membership fee: Nil for this variant."))
        self.assertEqual(p.new_value, 0.0)

    def test_negative_fee_is_unparseable(self):
        p = one(card_entry(), obs("annual_fee_inr", value="-500", unit="inr",
                                  per_spend_inr="",
                                  source_quote="The annual membership fee is Rs 500 plus taxes."))
        self.assertIsNone(p.new_value)
        self.assertEqual(p.blocked_reason, diff.REASON_UNPARSEABLE)

    def test_forex_markup_maps_and_keeps_percent_units(self):
        p = one(card_entry(), obs("forex_markup_pct", value="3.5", unit="percent",
                                  per_spend_inr="",
                                  source_quote="A markup of 3.5% applies on all foreign currency transactions."))
        self.assertEqual(p.path, "card.forex_markup_pct")
        self.assertEqual(p.unit, diff.UNIT_PERCENT)
        self.assertEqual(p.new_value, 3.5)

    def test_discontinued_becomes_is_active_zero_as_an_int(self):
        p = one(card_entry(), obs("card_discontinued", value="true", unit="boolean",
                                  per_spend_inr="",
                                  source_quote="This card has been withdrawn and is no longer open to new applicants."))
        self.assertEqual(p.path, "card.is_active")
        self.assertEqual(p.new_value, 0)
        self.assertIsInstance(p.new_value, int)
        self.assertNotIsInstance(p.new_value, bool)
        self.assertIsNone(p.delta_pct)  # a flag has no relative change
        self.assertTrue(p.auto_applicable)

    def test_unmapped_field_is_surfaced_not_dropped(self):
        p = one(card_entry(), obs("lounge_domestic_visits", value="8", unit="visits",
                                  per_spend_inr=""))
        self.assertEqual(len([p]), 1)
        self.assertFalse(p.auto_applicable)
        self.assertEqual(p.blocked_reason, "unmapped_field: lounge_domestic_visits")

    def test_joining_fee_has_nowhere_to_go_and_says_so(self):
        # Verified against seed/cards.json: no card carries a joining-fee key.
        p = one(card_entry(), obs("joining_fee_inr", value="1000", unit="inr",
                                  per_spend_inr=""))
        self.assertEqual(p.blocked_reason, "unmapped_field: joining_fee_inr")

    def test_malformed_observations_do_not_kill_the_batch(self):
        proposals = observations_to_proposals(
            card_entry(), ["not an object", {}, obs()], ISSUER_URL)
        self.assertEqual(len(proposals), 3)
        self.assertEqual(proposals[0].blocked_reason, diff.REASON_MALFORMED)
        self.assertEqual(proposals[1].blocked_reason, diff.REASON_MALFORMED)
        self.assertTrue(proposals[2].auto_applicable)


# ---------------------------------------------------------------------------
class TestMappingErrors(unittest.TestCase):

    def test_card_entry_must_be_a_dict(self):
        with self.assertRaises(TypeError):
            observations_to_proposals(["not", "a", "dict"], [obs()], ISSUER_URL)

    def test_card_entry_must_carry_a_card_object(self):
        with self.assertRaises(ValueError):
            observations_to_proposals({"reward_rules": []}, [obs()], ISSUER_URL)

    def test_card_object_must_be_a_dict(self):
        with self.assertRaises(ValueError):
            observations_to_proposals({"card": "hdfc"}, [obs()], ISSUER_URL)

    def test_card_must_have_an_id(self):
        entry = card_entry()
        entry["card"]["id"] = ""
        with self.assertRaises(ValueError):
            observations_to_proposals(entry, [obs()], ISSUER_URL)

    def test_observations_must_be_a_list(self):
        with self.assertRaises(TypeError):
            observations_to_proposals(card_entry(), obs(), ISSUER_URL)

    def test_empty_observations_is_not_an_error(self):
        self.assertEqual(observations_to_proposals(card_entry(), [], ISSUER_URL), [])

    def test_gate_rejects_a_non_proposal(self):
        with self.assertRaises(TypeError):
            gate({"card_id": "x"})


# ---------------------------------------------------------------------------
class TestGate(unittest.TestCase):
    """The asymmetry, the weasels, the domain, the ceiling and the boundaries."""

    def test_downward_high_confidence_with_a_real_quote_applies(self):
        p = gate(proposal(new_value=0.02, delta_pct=-25.0))
        self.assertTrue(p.auto_applicable)
        self.assertEqual(p.blocked_reason, "")

    def test_upward_rate_revision_is_always_blocked(self):
        p = gate(proposal(old_value=0.02, new_value=0.03, delta_pct=+50.0))
        self.assertFalse(p.auto_applicable)
        self.assertEqual(p.blocked_reason, diff.REASON_UPWARD)
        self.assertEqual(p.confidence, "high")  # blocked despite full confidence

    def test_upward_point_value_is_blocked_too(self):
        # Raising the point value raises every rate on the card at once.
        p = gate(proposal(field="point_value_inr", path="card.rp_value_standard",
                          unit=diff.UNIT_INR_PER_POINT, old_value=0.2,
                          new_value=0.25, delta_pct=+25.0))
        self.assertEqual(p.blocked_reason, diff.REASON_UPWARD)

    def test_downward_point_value_applies(self):
        p = gate(proposal(field="point_value_inr", path="card.rp_value_standard",
                          unit=diff.UNIT_INR_PER_POINT, old_value=0.5,
                          new_value=0.4, delta_pct=-20.0))
        self.assertTrue(p.auto_applicable)

    def test_filling_a_zero_base_rate_is_upward_and_needs_a_person(self):
        # 106 of 380 cards store 0 here. Filling one is the most valuable thing this
        # pipeline does and the one most worth reading before it ships.
        p = one(card_entry(base_reward_rate=0.0), obs(value="4", per_spend_inr="150"))
        self.assertIsNone(p.delta_pct)
        self.assertEqual(p.blocked_reason, diff.REASON_UPWARD)

    def test_fee_increase_is_not_an_upward_revision(self):
        p = gate(proposal(field="annual_fee_inr", path="card.annual_fee",
                          unit=diff.UNIT_INR, old_value=2500.0, new_value=3000.0,
                          delta_pct=+20.0))
        self.assertTrue(p.auto_applicable)

    def test_weasel_quote_blocks(self):
        p = gate(proposal(source_quote="Cardholders can earn up to 10% back on every spend."))
        self.assertEqual(p.blocked_reason, diff.REASON_WEASEL)

    def test_weasel_beats_confidence_in_the_reason_shown(self):
        p = gate(proposal(confidence="low",
                          source_quote="Save up to Rs 10,000 every single year on this card."))
        self.assertEqual(p.blocked_reason, diff.REASON_WEASEL)

    def test_non_issuer_domain_blocks(self):
        p = gate(proposal(source_url="https://www.cardexpert.in/hdfc-regalia-review/"))
        self.assertEqual(p.blocked_reason, diff.REASON_NOT_ISSUER)

    def test_lookalike_domain_blocks(self):
        p = gate(proposal(source_url="https://hdfcbank.com.rewards-india.top/regalia"))
        self.assertEqual(p.blocked_reason, diff.REASON_NOT_ISSUER)

    def test_empty_source_url_blocks(self):
        self.assertEqual(gate(proposal(source_url="")).blocked_reason,
                         diff.REASON_NOT_ISSUER)

    def test_bank_in_subdomain_is_an_issuer(self):
        p = gate(proposal(source_url="https://www.hdfc.bank.in/personal/regalia"))
        self.assertTrue(p.auto_applicable)

    def test_rate_above_the_ceiling_blocks(self):
        # 0.45 points per rupee is 45% at any point value we could assume.
        p = gate(proposal(old_value=0.5, new_value=0.45, delta_pct=-10.0))
        self.assertEqual(p.blocked_reason, diff.REASON_CEILING)

    def test_ceiling_boundary_exactly_at_the_limit_passes(self):
        limit = C.RATE_CEILING_PCT / 100.0          # 0.40 -> renders at most 40%
        p = gate(proposal(old_value=0.5, new_value=limit, delta_pct=-20.0))
        self.assertTrue(p.auto_applicable, "exactly at the ceiling must not block")

    def test_ceiling_boundary_just_over_blocks(self):
        over = (C.RATE_CEILING_PCT + 0.1) / 100.0
        p = gate(proposal(old_value=0.5, new_value=over, delta_pct=-19.0))
        self.assertEqual(p.blocked_reason, diff.REASON_CEILING)

    def test_floor_is_mirrored_from_config(self):
        # config.RATE_FLOOR_PCT is 0.0 today, so this only fires below zero — and
        # negatives are already refused at parse time. The check exists so that
        # raising the config floor to the 0.1 the publish gate enforces needs no
        # change here. Asserted against the constant, never against a literal.
        at_floor = gate(proposal(old_value=0.5, delta_pct=-20.0,
                                 new_value=C.RATE_FLOOR_PCT / 100.0))
        self.assertNotEqual(at_floor.blocked_reason, diff.REASON_FLOOR)
        below = gate(proposal(old_value=0.5, delta_pct=-20.0,
                              new_value=(C.RATE_FLOOR_PCT - 1.0) / 100.0))
        self.assertEqual(below.blocked_reason, diff.REASON_FLOOR)

    def test_percent_field_over_the_ceiling_blocks(self):
        p = gate(proposal(field="forex_markup_pct", path="card.forex_markup_pct",
                          unit=diff.UNIT_PERCENT, old_value=3.5, new_value=99.0,
                          delta_pct=+10.0))
        self.assertEqual(p.blocked_reason, diff.REASON_CEILING)

    def test_quote_shorter_than_the_minimum_blocks(self):
        short = "x" * (diff.MIN_QUOTE_CHARS - 1)
        self.assertEqual(gate(proposal(source_quote=short)).blocked_reason,
                         diff.REASON_NO_QUOTE)

    def test_quote_exactly_at_the_minimum_passes(self):
        exact = "x" * diff.MIN_QUOTE_CHARS
        self.assertTrue(gate(proposal(source_quote=exact)).auto_applicable)

    def test_whitespace_only_quote_blocks(self):
        self.assertEqual(gate(proposal(source_quote="   " * 20)).blocked_reason,
                         diff.REASON_NO_QUOTE)

    def test_medium_confidence_blocks(self):
        self.assertEqual(gate(proposal(confidence="medium")).blocked_reason,
                         diff.REASON_LOW_CONFIDENCE)

    def test_missing_confidence_blocks(self):
        self.assertEqual(gate(proposal(confidence="")).blocked_reason,
                         diff.REASON_LOW_CONFIDENCE)

    def test_delta_exactly_at_the_limit_passes(self):
        p = gate(proposal(field="annual_fee_inr", path="card.annual_fee",
                          unit=diff.UNIT_INR, old_value=1000.0, new_value=1500.0,
                          delta_pct=C.MAX_AUTO_DELTA_PCT))
        self.assertTrue(p.auto_applicable)

    def test_delta_just_over_the_limit_blocks(self):
        p = gate(proposal(field="annual_fee_inr", path="card.annual_fee",
                          unit=diff.UNIT_INR, old_value=1000.0, new_value=1600.0,
                          delta_pct=C.MAX_AUTO_DELTA_PCT + 0.1))
        self.assertEqual(p.blocked_reason, diff.REASON_LARGE_DELTA)

    def test_large_downward_delta_blocks(self):
        p = gate(proposal(old_value=0.04, new_value=0.01, delta_pct=-75.0))
        self.assertEqual(p.blocked_reason, diff.REASON_LARGE_DELTA)

    def test_unparseable_new_value_blocks(self):
        self.assertEqual(gate(proposal(new_value=None)).blocked_reason,
                         diff.REASON_UNPARSEABLE)
        self.assertEqual(gate(proposal(new_value="lots")).blocked_reason,
                         diff.REASON_UNPARSEABLE)
        self.assertEqual(gate(proposal(new_value=True)).blocked_reason,
                         diff.REASON_UNPARSEABLE)

    def test_flag_must_be_zero_or_one(self):
        p = gate(proposal(field="card_discontinued", path="card.is_active",
                          unit=diff.UNIT_FLAG, old_value=1, new_value=2,
                          delta_pct=None))
        self.assertEqual(p.blocked_reason, diff.REASON_UNPARSEABLE)

    def test_gate_cannot_overturn_a_structural_block(self):
        p = gate(proposal(blocked_reason="unmapped_field: lounge_domestic_visits",
                          new_value=8, unit="visits", path="<unmapped>.x"))
        self.assertFalse(p.auto_applicable)
        self.assertEqual(p.blocked_reason, "unmapped_field: lounge_domestic_visits")

    def test_gate_does_not_mutate_its_argument(self):
        p = proposal(source_quote="Earn up to 10% back on all your spends this year.")
        out = gate(p)
        self.assertIsNot(out, p)
        self.assertEqual(p.blocked_reason, "")
        self.assertEqual(out.blocked_reason, diff.REASON_WEASEL)

    def test_gate_is_idempotent(self):
        once = gate(proposal())
        self.assertEqual(gate(once), once)


# ---------------------------------------------------------------------------
class TestDelta(unittest.TestCase):

    def test_zero_old_value_gives_none_not_a_zero_division(self):
        p = one(card_entry(base_reward_rate=0), obs(value="4", per_spend_inr="150"))
        self.assertIsNone(p.delta_pct)

    def test_missing_old_value_gives_none(self):
        p = one(card_entry(rp_value_standard=None),
                obs("point_value_inr", value="0.25", unit="inr", per_spend_inr=""))
        self.assertIsNone(p.delta_pct)
        self.assertTrue(p.auto_applicable)  # a gap fill, not a revision

    def test_delta_is_relative_to_the_old_value(self):
        p = one(card_entry(annual_fee=1000.0),
                obs("annual_fee_inr", value="1500", unit="inr", per_spend_inr="",
                    source_quote="The annual membership fee is Rs 1,500 plus taxes."))
        self.assertAlmostEqual(p.delta_pct, 50.0, places=4)

    def test_delta_is_negative_when_the_number_falls(self):
        p = one(card_entry(forex_markup_pct=4.0),
                obs("forex_markup_pct", value="2", unit="percent", per_spend_inr="",
                    source_quote="A markup of 2% applies on all foreign currency spends."))
        self.assertAlmostEqual(p.delta_pct, -50.0, places=4)


# ---------------------------------------------------------------------------
class TestApply(unittest.TestCase):
    """What apply_proposals must not do, proved on a fixture with real rules."""

    def setUp(self):
        self.cards = [card_entry("hdfc_bank_regalia"), card_entry("axis_bank_magnus")]
        self.snapshot = copy.deepcopy(self.cards)
        self.p = gate(proposal(new_value=0.02, delta_pct=-25.0))

    def test_happy_path_sets_exactly_one_field(self):
        new, applied = apply_proposals(self.cards, [self.p])
        self.assertEqual(len(applied), 1)
        self.assertEqual(new[0]["card"]["base_reward_rate"], 0.02)
        self.assertEqual(new[1]["card"]["base_reward_rate"], 0.0266667)

    def test_input_is_never_mutated(self):
        apply_proposals(self.cards, [self.p])
        self.assertEqual(self.cards, self.snapshot)

    def test_deep_copy_is_real(self):
        new, _ = apply_proposals(self.cards, [self.p])
        new[0]["reward_rules"][0]["rule_name"] = "MUTATED"
        new[0]["card"]["metadata"]["material"] = "Plastic"
        new[0]["benefits"].append("Nonsense")
        self.assertEqual(self.cards[0]["reward_rules"][0]["rule_name"],
                         "4 reward points on every Rs. 150 spent")
        self.assertEqual(self.cards[0]["card"]["metadata"]["material"], "Metal")
        self.assertEqual(self.cards[0]["benefits"], ["Airport lounge access", "Golf"])

    def test_every_rule_name_is_byte_identical(self):
        new, _ = apply_proposals(self.cards, [self.p])
        for before, after in zip(self.snapshot, new):
            for arr in diff.RULE_ARRAYS:
                self.assertEqual(
                    [r["rule_name"] for r in before[arr]],
                    [r["rule_name"] for r in after[arr]],
                    f"{arr}: a rule_name moved — users' cap progress is keyed on it",
                )

    def test_every_rule_array_survives_byte_identical(self):
        new, _ = apply_proposals(self.cards, [self.p])
        for before, after in zip(self.snapshot, new):
            for arr in diff.RULE_ARRAYS:
                self.assertEqual(json.dumps(before[arr], sort_keys=True),
                                 json.dumps(after[arr], sort_keys=True))

    def test_no_sibling_key_is_dropped_and_only_the_target_moved(self):
        new, _ = apply_proposals(self.cards, [self.p])
        before, after = copy.deepcopy(self.snapshot[0]), copy.deepcopy(new[0])
        # Curated content that the old scraper's copyfile() destroyed.
        self.assertEqual(after["benefits"], before["benefits"])
        self.assertEqual(after["sources"], before["sources"])
        self.assertEqual(after["changelog"], before["changelog"])
        # The entry round-trips exactly, once the one field and the new provenance
        # record are put back.
        after["card"]["base_reward_rate"] = before["card"]["base_reward_rate"]
        after.pop(diff.PROVENANCE_KEY)
        self.assertEqual(after, before)
        self.assertEqual(sorted(after["card"]), sorted(before["card"]))

    def test_provenance_uses_the_exact_key_names_the_app_parses(self):
        new, _ = apply_proposals(self.cards, [self.p])
        records = new[0][diff.PROVENANCE_KEY]
        self.assertEqual(len(records), 1)
        r = records[0]
        for key in ("source_url", "source_quote", "confidence", "source_fetched_on"):
            self.assertIn(key, r)
        self.assertEqual(r["confidence"], "high")
        self.assertEqual(r["source_url"], ISSUER_URL)
        self.assertEqual(r["source_quote"], GOOD_QUOTE)
        self.assertEqual(r["source_fetched_on"], diff._today())
        self.assertEqual(r["path"], "card.base_reward_rate")
        self.assertEqual(r["old_value"], 0.0266667)
        self.assertEqual(r["new_value"], 0.02)

    def test_provenance_never_claims_high_on_a_forced_low_confidence_change(self):
        # only_auto=False is an operator override. The app reads a missing confidence
        # as 'high', so the stamp must carry the truth rather than the default.
        forced = gate(proposal(confidence="low", new_value=0.02, delta_pct=-25.0))
        new, applied = apply_proposals(self.cards, [forced], only_auto=False)
        self.assertEqual(len(applied), 1)
        self.assertEqual(new[0][diff.PROVENANCE_KEY][0]["confidence"], "low")

    def test_provenance_for_the_same_field_replaces_rather_than_grows(self):
        once, _ = apply_proposals(self.cards, [self.p])
        twice, _ = apply_proposals(once, [gate(proposal(old_value=0.02,
                                                        new_value=0.015,
                                                        delta_pct=-25.0))])
        self.assertEqual(len(twice[0][diff.PROVENANCE_KEY]), 1)
        self.assertEqual(twice[0][diff.PROVENANCE_KEY][0]["new_value"], 0.015)

    def test_provenance_for_a_second_field_is_appended(self):
        fee = gate(proposal(field="annual_fee_inr", path="card.annual_fee",
                            unit=diff.UNIT_INR, old_value=2500.0, new_value=3000.0,
                            delta_pct=20.0))
        new, applied = apply_proposals(self.cards, [self.p, fee])
        self.assertEqual(len(applied), 2)
        self.assertEqual(len(new[0][diff.PROVENANCE_KEY]), 2)

    def test_only_auto_skips_blocked_proposals(self):
        blocked = gate(proposal(source_quote="Earn up to 10% back on everything."))
        new, applied = apply_proposals(self.cards, [blocked])
        self.assertEqual(applied, [])
        self.assertEqual(new, self.snapshot)

    def test_only_auto_false_applies_a_blocked_proposal(self):
        blocked = gate(proposal(old_value=0.02, new_value=0.03, delta_pct=50.0))
        self.assertEqual(blocked.blocked_reason, diff.REASON_UPWARD)
        new, applied = apply_proposals(self.cards, [blocked], only_auto=False)
        self.assertEqual(len(applied), 1)
        self.assertEqual(new[0]["card"]["base_reward_rate"], 0.03)

    def test_unknown_card_id_is_skipped_not_created(self):
        stray = gate(proposal(card_id="does_not_exist"))
        new, applied = apply_proposals(self.cards, [stray])
        self.assertEqual(applied, [])
        self.assertEqual(len(new), 2)

    def test_an_unmapped_path_is_never_written(self):
        rogue = Proposal(card_id="hdfc_bank_regalia", field="lounge_domestic_visits",
                         path="<unmapped>.lounge_domestic_visits", old_value=None,
                         new_value=8, unit="visits", source_url=ISSUER_URL,
                         source_quote=GOOD_QUOTE, confidence="high", delta_pct=None,
                         auto_applicable=True, blocked_reason="")
        new, applied = apply_proposals(self.cards, [rogue], only_auto=False)
        self.assertEqual(applied, [])
        self.assertEqual(new, self.snapshot)

    def test_a_path_the_card_does_not_have_is_never_invented(self):
        thin = [{"card": {"id": "thin_card"}, "reward_rules": []}]
        new, applied = apply_proposals(thin, [gate(proposal(card_id="thin_card"))],
                                       only_auto=False)
        self.assertEqual(applied, [])
        self.assertNotIn("base_reward_rate", new[0]["card"])

    def test_empty_proposals_is_a_clean_no_op(self):
        new, applied = apply_proposals(self.cards, [])
        self.assertEqual(applied, [])
        self.assertEqual(new, self.snapshot)
        self.assertIsNot(new, self.cards)

    def test_cards_must_be_a_list(self):
        with self.assertRaises(TypeError):
            apply_proposals({"card": {}}, [self.p])

    def test_proposals_must_be_a_list(self):
        with self.assertRaises(TypeError):
            apply_proposals(self.cards, self.p)

    def test_proposals_must_hold_proposals(self):
        with self.assertRaises(TypeError):
            apply_proposals(self.cards, [{"path": "card.annual_fee"}])

    def test_malformed_entries_do_not_crash_the_apply(self):
        messy = [None, {"no_card_key": True}, card_entry("hdfc_bank_regalia")]
        new, applied = apply_proposals(messy, [self.p])
        self.assertEqual(len(applied), 1)
        self.assertEqual(new[2]["card"]["base_reward_rate"], 0.02)

    def test_the_rule_name_guard_actually_fires(self):
        # Prove the assertion is load-bearing rather than decorative: make the
        # before/after fingerprints disagree and confirm apply_proposals raises
        # rather than handing back a quietly corrupted catalogue.
        original = diff._rule_names
        calls = []

        def drifting(cards):
            calls.append(cards)
            return [("rule_name", len(calls))]

        diff._rule_names = drifting
        try:
            with self.assertRaises(AssertionError):
                apply_proposals(self.cards, [self.p])
        finally:
            diff._rule_names = original
        self.assertEqual(len(calls), 2, "the guard must compare before against after")


# ---------------------------------------------------------------------------
class TestRender(unittest.TestCase):

    def test_empty_list_does_not_crash(self):
        md = render_markdown([])
        self.assertIsInstance(md, str)
        self.assertIn("Nothing to review", md)

    def test_counts_lead_the_body(self):
        proposals = [
            gate(proposal()),
            gate(proposal(source_quote="Earn up to 10% back on everything you buy.")),
            gate(proposal(card_id="axis_bank_magnus", old_value=0.02,
                          new_value=0.05, delta_pct=150.0)),
        ]
        md = render_markdown(proposals)
        first_para = md.split("\n\n")[1]
        self.assertIn("1 change applied", first_para)
        self.assertIn("2 changes held back", first_para)
        self.assertIn("2 cards", first_para)

    def test_applied_section_carries_the_quote_and_the_link(self):
        md = render_markdown([gate(proposal())])
        self.assertIn("## Applied", md)
        self.assertIn(GOOD_QUOTE, md)
        self.assertIn(ISSUER_URL, md)
        self.assertIn("base earn rate", md)

    def test_blocked_sections_are_grouped_by_reason_with_plain_english(self):
        md = render_markdown([
            gate(proposal(old_value=0.02, new_value=0.03, delta_pct=50.0)),
            gate(proposal(source_url="https://www.cardinsider.com/hdfc-regalia/")),
        ])
        self.assertIn("## Held back", md)
        self.assertIn("earns MORE than we have on file", md)
        self.assertIn("Not from the bank's own website", md)
        # No JSON key names in the founder-facing prose.
        self.assertNotIn("rp_value_standard", md)
        self.assertNotIn("auto_applicable", md)

    def test_unmapped_reason_names_the_field(self):
        p = one(card_entry(), obs("lounge_domestic_visits", value="8", unit="visits",
                                  per_spend_inr=""))
        md = render_markdown([p])
        self.assertIn("We do not store this: lounge_domestic_visits", md)

    def test_rupees_are_grouped_the_indian_way(self):
        p = gate(proposal(field="annual_fee_inr", path="card.annual_fee",
                          unit=diff.UNIT_INR, old_value=50000.0, new_value=100000.0,
                          delta_pct=100.0))
        md = render_markdown([p])
        self.assertIn("₹50,000", md)
        self.assertIn("₹1,00,000", md)

    def test_a_points_rate_reads_per_hundred_rupees(self):
        # 0.0266667 points per rupee is the stored unit and unreadable as one.
        md = render_markdown([gate(proposal(old_value=0.0266667, new_value=0.02,
                                            delta_pct=-25.0))])
        self.assertIn("2.667 points per ₹100 → 2 points per ₹100", md)

    def test_a_flag_reads_as_words_not_a_number(self):
        p = gate(proposal(field="card_discontinued", path="card.is_active",
                          unit=diff.UNIT_FLAG, old_value=1, new_value=0,
                          delta_pct=None))
        md = render_markdown([p])
        self.assertIn("still offered → no longer offered", md)

    def test_a_multiline_quote_cannot_break_out_of_its_blockquote(self):
        p = gate(proposal(source_quote="Line one of the bank's sentence.\n\n"
                                       "# Not a heading, part of the same quote."))
        md = render_markdown([p])
        self.assertNotIn("\n# Not a heading", md)

    def test_a_very_long_quote_is_truncated(self):
        p = gate(proposal(source_quote="Reward points are credited monthly. " * 40))
        md = render_markdown([p])
        self.assertIn("…", md)
        self.assertLess(max(len(line) for line in md.splitlines()), 600)

    def test_render_rejects_a_non_list(self):
        with self.assertRaises(TypeError):
            render_markdown(gate(proposal()))


# ---------------------------------------------------------------------------
class TestSummarise(unittest.TestCase):

    def test_counts_split_auto_from_blocked(self):
        proposals = [
            gate(proposal()),
            gate(proposal(card_id="axis_bank_magnus",
                          source_quote="Earn up to 10% back on all spends this year.")),
            gate(proposal(card_id="axis_bank_magnus", old_value=0.02,
                          new_value=0.03, delta_pct=50.0)),
        ]
        s = summarise(proposals)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["auto"], 1)
        self.assertEqual(s["blocked"], 2)
        self.assertEqual(s["by_reason"],
                         {diff.REASON_UPWARD: 1, diff.REASON_WEASEL: 1})
        self.assertEqual(s["by_card"],
                         {"axis_bank_magnus": 2, "hdfc_bank_regalia": 1})

    def test_empty_summary(self):
        self.assertEqual(summarise([]),
                         {"total": 0, "auto": 0, "blocked": 0,
                          "by_reason": {}, "by_card": {}})

    def test_summary_is_json_serialisable(self):
        json.dumps(summarise([gate(proposal())]))

    def test_summarise_rejects_a_non_list(self):
        with self.assertRaises(TypeError):
            summarise(gate(proposal()))


# ---------------------------------------------------------------------------
class TestCli(unittest.TestCase):
    """The CLI is what the workflow calls, so its exit codes are a contract."""

    def _run(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(REPO / "pipeline" / "diff.py"), *args],
            capture_output=True, text=True, cwd=str(cwd or REPO),
        )

    def test_happy_path_prints_markdown_and_writes_the_pr_body(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            (tmp / "cards.json").write_text(json.dumps([card_entry()]), encoding="utf-8")
            (tmp / "verified.json").write_text(json.dumps({"cards": [{
                "card_id": "hdfc_bank_regalia",
                "source_url": ISSUER_URL,
                "observations": [obs(value="3", per_spend_inr="150")],
            }]}), encoding="utf-8")
            out = tmp / "body.md"
            summary = tmp / "summary.json"
            r = self._run("--observations", str(tmp / "verified.json"),
                          "--cards", str(tmp / "cards.json"),
                          "--out", str(out), "--summary-json", str(summary))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("## Applied", r.stdout)
            self.assertIn("## Applied", out.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["auto"], 1)

    def test_missing_observations_file_is_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            r = self._run("--observations", str(pathlib.Path(td) / "nope.json"))
            self.assertEqual(r.returncode, 2)
            self.assertIn("no verified observations file", r.stderr)

    def test_malformed_json_is_exit_1(self):
        with tempfile.TemporaryDirectory() as td:
            bad = pathlib.Path(td) / "verified.json"
            bad.write_text("{not json", encoding="utf-8")
            r = self._run("--observations", str(bad))
            self.assertEqual(r.returncode, 1)

    def test_wrong_top_level_type_is_exit_1(self):
        with tempfile.TemporaryDirectory() as td:
            bad = pathlib.Path(td) / "verified.json"
            bad.write_text('"just a string"', encoding="utf-8")
            r = self._run("--observations", str(bad))
            self.assertEqual(r.returncode, 1)

    def test_unknown_card_id_is_reported_not_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            (tmp / "cards.json").write_text(json.dumps([card_entry()]), encoding="utf-8")
            (tmp / "verified.json").write_text(json.dumps([{
                "card_id": "hdfc_bank_ghost",
                "source_url": ISSUER_URL,
                "observations": [obs()],
            }]), encoding="utf-8")
            r = self._run("--observations", str(tmp / "verified.json"),
                          "--cards", str(tmp / "cards.json"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("hdfc_bank_ghost", r.stdout)
            self.assertIn("not in our catalogue", r.stdout)

    def test_module_imports_without_the_anthropic_sdk(self):
        # Nothing in this module may reach the network or a vendor SDK.
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.modules['anthropic'] = None; "
             "sys.path.insert(0, %r); import pipeline.diff; print('ok')" % str(REPO)],
            capture_output=True, text=True, cwd=str(REPO),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)


# ---------------------------------------------------------------------------
class TestAgainstTheRealCatalogue(unittest.TestCase):
    """The fixtures above are ours. This one is what actually ships to users."""

    @classmethod
    def setUpClass(cls):
        if not C.CARDS_JSON.exists():
            raise unittest.SkipTest("seed/cards.json not present")
        cls.cards = json.loads(C.CARDS_JSON.read_text(encoding="utf-8"))

    def test_the_catalogue_is_the_shape_this_module_assumes(self):
        self.assertIsInstance(self.cards, list)
        self.assertGreater(len(self.cards), 300)
        for entry in self.cards[:50]:
            self.assertIsInstance(entry["card"], dict)
            self.assertIn("base_reward_rate", entry["card"])
            self.assertIn("rp_value_standard", entry["card"])

    def test_no_card_stores_a_joining_fee_so_the_field_is_correctly_unmapped(self):
        self.assertFalse(any("joining_fee" in e["card"] for e in self.cards))

    def test_a_real_card_takes_a_real_downward_correction_and_nothing_else_moves(self):
        target = next(e for e in self.cards
                      if e["card"]["base_reward_rate"] and e["reward_rules"])
        card_id = target["card"]["id"]
        old = target["card"]["base_reward_rate"]
        p = gate(Proposal(
            card_id=card_id, field="base_reward_rate", path="card.base_reward_rate",
            old_value=old, new_value=round(old * 0.8, 8),
            unit=diff.UNIT_POINTS_PER_RUPEE, source_url=ISSUER_URL,
            source_quote=GOOD_QUOTE, confidence="high", delta_pct=-20.0,
        ))
        self.assertTrue(p.auto_applicable, p.blocked_reason)

        before = json.dumps(self.cards, sort_keys=True)
        new, applied = apply_proposals(self.cards, [p])
        self.assertEqual(len(applied), 1)
        self.assertEqual(json.dumps(self.cards, sort_keys=True), before,
                         "the live catalogue must not be mutated in place")

        changed = [i for i, (a, b) in enumerate(zip(self.cards, new)) if a != b]
        self.assertEqual(len(changed), 1, "exactly one card entry may differ")
        i = changed[0]
        self.assertEqual(new[i]["card"]["base_reward_rate"], round(old * 0.8, 8))
        for arr in diff.RULE_ARRAYS:
            self.assertEqual(json.dumps(self.cards[i].get(arr), sort_keys=True),
                             json.dumps(new[i].get(arr), sort_keys=True))

    def test_every_rule_name_in_the_live_catalogue_survives_a_sweep(self):
        proposals = []
        for entry in self.cards[:120]:
            fee = entry["card"].get("annual_fee")
            if not isinstance(fee, (int, float)) or fee <= 0:
                continue
            proposals.append(gate(Proposal(
                card_id=entry["card"]["id"], field="annual_fee_inr",
                path="card.annual_fee", old_value=fee, new_value=float(fee) + 100.0,
                unit=diff.UNIT_INR, source_url=ISSUER_URL, source_quote=GOOD_QUOTE,
                confidence="high", delta_pct=1.0,
            )))
        self.assertGreater(len(proposals), 10)
        new, applied = apply_proposals(self.cards, proposals)
        self.assertEqual(len(applied), len(proposals))
        self.assertEqual(diff._rule_names(new), diff._rule_names(self.cards))


# ---------------------------------------------------------------------------
# Regression: the null point value. 71 of 380 live cards store rp_value_standard
# null, and the app renders those at Rs 0.25 — so null is not a gap waiting to be
# filled, it is a number the user already sees. Treating it as a gap let a proposal
# of Rs 1.50 auto-apply with no ceiling, delta or upward check (all three
# short-circuit on a None old value), and because the app multiplies EVERY rule on
# the card by this one field, a single write moved the whole card and produced a
# 60% rendered rate — past the ceiling tools/kredme.py calls unwaivable.
# ---------------------------------------------------------------------------
def _null_rp_card():
    return {
        "card": {"id": "t_null_rp", "card_name": "Test", "is_active": 1,
                 "base_reward_rate": 0.04, "rp_value_standard": None},
        "reward_rules": [],
    }


def _rp_obs(value):
    return [{
        "field": "point_value_inr", "value": str(value), "unit": "inr",
        "source_quote": (
            "Each Reward Point is worth Rs %s when redeemed against the statement." % value
        ),
        "confidence": "high",
    }]


class TestNullPointValueIsNotAGap(unittest.TestCase):
    URL = "https://www.hdfc.bank.in/x"

    def _gate(self, value):
        ps = diff.observations_to_proposals(_null_rp_card(), _rp_obs(value), self.URL)
        self.assertTrue(ps, "expected a proposal")
        return [diff.gate(p) for p in ps]

    def test_raising_a_null_point_value_is_blocked_as_upward(self):
        for value in ("1.5", "1.0", "0.5", "0.26"):
            with self.subTest(value=value):
                for p in self._gate(value):
                    self.assertFalse(p.auto_applicable, f"Rs {value} auto-applied")
                    self.assertEqual(p.blocked_reason, "upward_revision")

    def test_lowering_below_the_app_default_still_applies(self):
        # Rs 0.20 is genuinely below the Rs 0.25 the user sees today, so it is a
        # downward correction and the asymmetry lets it through by design.
        for p in self._gate("0.20"):
            self.assertTrue(p.auto_applicable, p.blocked_reason)

    def test_the_constant_matches_the_publish_gate(self):
        # If tools/kredme.py and this module disagree about the app's default, the
        # pipeline can pass its own gate and then fail the publish gate — which
        # config.py explicitly promises cannot happen.
        src = (pathlib.Path(__file__).resolve().parent.parent / "tools" / "kredme.py").read_text()
        self.assertIn("APP_POINT_VALUE_DEFAULT = 0.25", src)
        self.assertEqual(diff.APP_POINT_VALUE_DEFAULT, 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
