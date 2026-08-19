#!/usr/bin/env python3
"""
Tests for pipeline/report.py — the weekly health numbers.

Usage:
    python3 tests/test_report.py            # run all
    python3 tests/test_report.py -v         # per-test names

Every fixture here is hand-built so each expected number is known by
construction rather than copied out of a previous run. Nothing touches the
network; the only real file read is seed/cards.json, in one smoke test that
asserts invariants and never exact counts.

Stdlib only — unittest, not pytest.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import config as C          # noqa: E402
from pipeline import report as R          # noqa: E402

UTC = dt.timezone.utc

# A source has to name a real site. 'https://x' has no dot, so it names nothing a
# person could open, and the counted definition rejects it exactly as the
# validator's L8 layer always has.
SOURCE = "https://www.hdfc.bank.in/credit-cards/regalia"


# ---------------------------------------------------------------------------
# Fixtures — the real nested seed shape: {"card": {...}, "reward_rules": [...]}
# ---------------------------------------------------------------------------
def rule(name: str, **over) -> dict:
    r = {
        "rule_name": name,
        "rule_type": "category_bonus",
        "reward_type": "cashback_pct",
        "reward_rate": 0.02,
        "reward_unit_spend": None,
        "cap_amount": None,
        "cap_period": None,
    }
    r.update(over)
    return r


def card(cid: str, *, base=0.02, rp=0.25, active=1, rules=None, **over) -> dict:
    inner = {
        "id": cid,
        "card_name": cid.replace("_", " ").title(),
        "issuer": "test_bank",
        "base_reward_rate": base,
        "rp_value_standard": rp,
        "is_active": active,
    }
    inner.update(over)
    return {
        "card": inner,
        "reward_rules": list(rules or []),
        "exclusion_rules": [],
        "milestone_rules": [],
        "redemption_rules": [],
        "fuel_surcharge_rules": [],
    }


def source_state(**records) -> dict:
    return {"schema_version": 1, "sources": dict(records)}


def source_row(*, status="ok", sha="abc123", fetched_at="2026-08-01T00:00:00Z") -> dict:
    return {
        "url": "https://www.hdfc.bank.in/x",
        "content_sha256": sha,
        "fetched_at": fetched_at,
        "status": status,
        "note": "",
    }


EMPTY_SOURCES = source_state()


# ---------------------------------------------------------------------------
# compute_metrics — happy path, every number known by construction
# ---------------------------------------------------------------------------
class TestComputeMetricsHappyPath(unittest.TestCase):
    def setUp(self) -> None:
        self.cards = [
            # 3 rules: 1 sourced, 1 capped-with-unit, 1 capped-without-unit.
            card("bank_alpha", base=0.02, rules=[
                rule("Base reward rate", rule_type="base_rate",
                     source_url="https://www.hdfc.bank.in/alpha"),
                rule("5% on dining", reward_rate=0.05, cap_amount=1200, cap_kind="reward"),
                rule("2% on fuel", cap_amount=500.0),
            ]),
            # base_reward_rate KEY MISSING entirely -> renders 0.0% to a user.
            card("bank_beta", rules=[
                rule("Unlimited cashback", cap_amount="unlimited"),
            ], base=None),
            # explicit 0, switched off, and a genuine rule-name collision.
            card("bank_gamma", base=0, active=0, rules=[
                rule("10 points per Rs 150 on groceries", cap_amount=""),
                rule("10 points per Rs 150 on groceries", cap_amount=""),
            ]),
        ]
        del self.cards[1]["card"]["base_reward_rate"]
        self.m = R.compute_metrics(
            self.cards,
            source_state(bank_alpha=source_row(fetched_at="2026-08-06T00:00:00Z"),
                         bank_beta=source_row(status="fetch_failed", sha=None)),
            now=dt.datetime(2026, 8, 13, tzinfo=UTC),
        )

    def test_totals(self) -> None:
        self.assertEqual(self.m["total_cards"], 3)
        self.assertEqual(self.m["active_cards"], 2)
        self.assertEqual(self.m["total_reward_rules"], 6)

    def test_sourced(self) -> None:
        self.assertEqual(self.m["sourced_rules"], 1)
        self.assertEqual(self.m["sourced_rules_pct"], round(100 / 6, 2))

    def test_defect_counts(self) -> None:
        self.assertEqual(self.m["zero_rate_cards"], 2)      # missing key + explicit 0
        self.assertEqual(self.m["caps_without_unit"], 4)    # 1200 has cap_kind, the other 4 do not
        self.assertEqual(self.m["non_numeric_caps"], 3)     # 'unlimited' + two ''
        self.assertEqual(self.m["duplicate_rule_keys"], 1)  # 2 identical names = 1 shadowed rule
        self.assertEqual(self.m["rules_over_ceiling"], 0)

    def test_source_health(self) -> None:
        self.assertEqual(self.m["sources_resolved"], 1)
        self.assertEqual(self.m["sources_unresolved"], 1)
        self.assertEqual(self.m["sources_stale_days"], 7)

    def test_every_value_is_a_number(self) -> None:
        for key, value in self.m.items():
            self.assertIsInstance(value, (int, float), f"{key} must be flat and numeric")
            self.assertNotIsInstance(value, bool, f"{key} must not be a bool")

    def test_keys_match_the_direction_map(self) -> None:
        # If these drift, diff_metrics silently stops labelling a metric and the
        # trend table quietly loses a column.
        #
        # NOT_A_METRIC is subtracted rather than the assertion being loosened:
        # the row also carries `metric_definition_version`, which is bookkeeping
        # and has no direction because it is not a measurement. Every remaining
        # key still has to be labelled and directed.
        measured = set(self.m) - R.NOT_A_METRIC
        self.assertEqual(measured, set(R.GOOD_DIRECTION))
        self.assertEqual(measured, set(R.METRIC_LABELS))

    def test_input_is_not_mutated(self) -> None:
        before = copy.deepcopy(self.cards)
        R.compute_metrics(self.cards, EMPTY_SOURCES)
        self.assertEqual(self.cards, before)


# ---------------------------------------------------------------------------
# zero_rate_cards
# ---------------------------------------------------------------------------
class TestZeroRateCards(unittest.TestCase):
    def count(self, cards) -> int:
        return R.compute_metrics(cards, EMPTY_SOURCES)["zero_rate_cards"]

    def test_missing_key_counts(self) -> None:
        entry = card("a")
        del entry["card"]["base_reward_rate"]
        self.assertEqual(self.count([entry]), 1)

    def test_explicit_zero_counts(self) -> None:
        self.assertEqual(self.count([card("a", base=0)]), 1)
        self.assertEqual(self.count([card("a", base=0.0)]), 1)

    def test_null_counts(self) -> None:
        self.assertEqual(self.count([card("a", base=None)]), 1)

    def test_non_numeric_counts(self) -> None:
        self.assertEqual(self.count([card("a", base="0.02")]), 1)

    def test_real_rate_does_not_count(self) -> None:
        self.assertEqual(self.count([card("a", base=0.0266667)]), 0)

    def test_tiny_rate_does_not_count(self) -> None:
        self.assertEqual(self.count([card("a", base=0.0001)]), 0)


# ---------------------------------------------------------------------------
# caps_without_unit — a 1,200-POINT cap is not Rs 1,200
# ---------------------------------------------------------------------------
class TestCapsWithoutUnit(unittest.TestCase):
    def count(self, *rules) -> int:
        return R.compute_metrics([card("a", rules=rules)], EMPTY_SOURCES)["caps_without_unit"]

    def test_cap_with_kind_is_fine(self) -> None:
        self.assertEqual(self.count(rule("r", cap_amount=1200, cap_kind="reward")), 0)

    def test_cap_without_kind_counts(self) -> None:
        self.assertEqual(self.count(rule("r", cap_amount=1200)), 1)

    def test_empty_kind_counts(self) -> None:
        self.assertEqual(self.count(rule("r", cap_amount=1200, cap_kind="   ")), 1)
        self.assertEqual(self.count(rule("r", cap_amount=1200, cap_kind=None)), 1)

    def test_no_cap_is_not_a_missing_unit(self) -> None:
        self.assertEqual(self.count(rule("r", cap_amount=None)), 0)
        self.assertEqual(self.count(rule("r")), 0)

    def test_zero_cap_is_present_and_counts(self) -> None:
        # cap_amount 0 is a populated field, and the app will read it as a real
        # ceiling of zero. It needs a unit like any other cap.
        self.assertEqual(self.count(rule("r", cap_amount=0)), 1)


# ---------------------------------------------------------------------------
# non_numeric_caps — double.tryParse returns null == NO CAP == pays forever
# ---------------------------------------------------------------------------
class TestNonNumericCaps(unittest.TestCase):
    def count(self, value) -> int:
        return R.compute_metrics(
            [card("a", rules=[rule("r", cap_amount=value)])], EMPTY_SOURCES
        )["non_numeric_caps"]

    def test_prose_cap_counts(self) -> None:
        self.assertEqual(self.count("unlimited"), 1)
        self.assertEqual(self.count("2x base cashback earned in same statement month"), 1)
        self.assertEqual(self.count("1000 points per month"), 1)

    def test_empty_string_counts(self) -> None:
        self.assertEqual(self.count(""), 1)
        self.assertEqual(self.count("   "), 1)

    def test_structured_cap_counts(self) -> None:
        self.assertEqual(self.count({"monthly": 2500, "annual": 10000}), 1)
        self.assertEqual(self.count([2500, 10000]), 1)

    def test_bool_counts(self) -> None:
        self.assertEqual(self.count(True), 1)

    def test_numbers_do_not_count(self) -> None:
        self.assertEqual(self.count(1200), 0)
        self.assertEqual(self.count(1200.5), 0)
        self.assertEqual(self.count(0), 0)

    def test_numeric_string_does_not_count(self) -> None:
        # double.tryParse("1200") succeeds, so this is a live cap, not a defect.
        self.assertEqual(self.count("1200"), 0)
        self.assertEqual(self.count(" 1200.5 "), 0)

    def test_absent_cap_does_not_count(self) -> None:
        self.assertEqual(self.count(None), 0)


# ---------------------------------------------------------------------------
# duplicate_rule_keys — TRAP 1: the key is the RAW rule_name
# ---------------------------------------------------------------------------
class TestDuplicateRuleKeys(unittest.TestCase):
    def count(self, cards) -> int:
        return R.compute_metrics(cards, EMPTY_SOURCES)["duplicate_rule_keys"]

    def test_genuine_collision(self) -> None:
        entry = card("a", rules=[rule("5% on dining"), rule("5% on dining")])
        self.assertEqual(self.count([entry]), 1)

    def test_three_way_collision_counts_two_shadowed(self) -> None:
        entry = card("a", rules=[rule("x"), rule("x"), rule("x")])
        self.assertEqual(self.count([entry]), 2)

    def test_different_names_do_not_collide(self) -> None:
        entry = card("a", rules=[rule("5% on dining"), rule("5% on fuel")])
        self.assertEqual(self.count([entry]), 0)

    def test_same_name_on_different_cards_does_not_collide(self) -> None:
        cards = [card("a", rules=[rule("Base reward rate")]),
                 card("b", rules=[rule("Base reward rate")])]
        self.assertEqual(self.count(cards), 0)

    def test_names_differing_only_past_80_chars_are_distinct(self) -> None:
        # kredme.py's 80-char rule key merged 24 genuinely distinct rules. The
        # app keys cap progress on the FULL string, so this must count 0.
        prefix = "8 Reward Points on every Rs.100 spent on dining, travel and tax payments; capped"
        self.assertGreater(len(prefix), 79)
        entry = card("a", rules=[rule(prefix + " at 1,000 points"),
                                 rule(prefix + " at 5,000 points")])
        self.assertEqual(self.count([entry]), 0)

    def test_case_and_whitespace_are_not_normalised(self) -> None:
        # Renaming or folding a rule_name orphans a user's cap progress, so this
        # module must never treat two spellings as the same rule.
        entry = card("a", rules=[rule("5% on Dining"), rule("5% on dining"),
                                 rule("5% on dining ")])
        self.assertEqual(self.count([entry]), 0)


# ---------------------------------------------------------------------------
# rules_over_ceiling — boundary on the unwaivable 40%
# ---------------------------------------------------------------------------
class TestRulesOverCeiling(unittest.TestCase):
    def count(self, *rules, **card_over) -> int:
        return R.compute_metrics(
            [card("a", rules=rules, **card_over)], EMPTY_SOURCES
        )["rules_over_ceiling"]

    def test_ceiling_is_the_config_value(self) -> None:
        self.assertEqual(C.RATE_CEILING_PCT, 40.0)

    def test_exactly_at_the_ceiling_is_allowed(self) -> None:
        self.assertEqual(self.count(rule("r", reward_rate=0.40)), 0)

    def test_just_over_the_ceiling_counts(self) -> None:
        self.assertEqual(self.count(rule("r", reward_rate=0.4001)), 1)

    def test_just_under_the_ceiling_is_allowed(self) -> None:
        self.assertEqual(self.count(rule("r", reward_rate=0.3999)), 0)

    def test_points_per_spend_keeps_its_block_size(self) -> None:
        # TRAP 3: 24 points per Rs.150 at Rs.0.20/pt is 3.2%, not 24%. The block
        # size stays in the data; we only divide to measure.
        over = self.count(
            rule("24 reward points on every Rs. 150 spent",
                 reward_type="points_per_spend", reward_rate=24, reward_unit_spend=150),
            rp=0.20,
        )
        self.assertEqual(over, 0)

    def test_zero_unit_spend_falls_back_to_base(self) -> None:
        # The app guards its own division by zero by showing the base rate.
        self.assertEqual(
            self.count(rule("r", reward_type="points_per_spend",
                            reward_rate=24, reward_unit_spend=0)),
            0,
        )

    def test_multiplier_uses_the_card_base(self) -> None:
        # 100x a 0.0266667 base at Rs.1/pt renders 266%.
        self.assertEqual(
            self.count(rule("r", reward_type="multiplier", reward_rate=100),
                       base=0.0266667, rp=1.0),
            1,
        )

    def test_unknown_reward_type_uses_base(self) -> None:
        self.assertEqual(self.count(rule("r", reward_type="mystery"), base=0.02), 0)


# ---------------------------------------------------------------------------
# sourced_rules — the only metric that measures TRUTH
# ---------------------------------------------------------------------------
class TestSourcedRules(unittest.TestCase):
    def count(self, *rules) -> dict:
        return R.compute_metrics([card("a", rules=rules)], EMPTY_SOURCES)

    def test_a_rule_naming_a_document_counts(self) -> None:
        # Both key names, because three passes wrote provenance three ways.
        self.assertEqual(
            self.count(rule("r", source_url=SOURCE))["sourced_rules"], 1)
        self.assertEqual(
            self.count(rule("r", _sources=[{"url": SOURCE}]))["sourced_rules"], 1)

    def test_a_quote_with_no_document_does_not_count(self) -> None:
        """Tightened 2026-08-19, and this is the change that moved the number.

        A quote alone cannot be re-read when the issuer devalues next quarter,
        which is the entire purpose of a source. Counting it took the reported
        figure to 61 of 1,279 (4.8%) while the validator, reading the same file,
        said 26 (2.0%). One of those numbers had to go, and it was not the
        stricter one.
        """
        self.assertEqual(
            self.count(rule("r", source_quote="Earn 4 points"))["sourced_rules"], 0)

    def test_a_placeholder_that_names_no_site_does_not_count(self) -> None:
        # Seven rules in the real catalogue cite the literal string "bank".
        self.assertEqual(self.count(rule("r", _sources=["bank"]))["sourced_rules"], 0)

    def test_all_three_on_one_rule_counts_once(self) -> None:
        r = rule("r", source_url=SOURCE, source_quote="q", _sources=[{"url": SOURCE}])
        self.assertEqual(self.count(r)["sourced_rules"], 1)

    def test_empty_provenance_does_not_count(self) -> None:
        self.assertEqual(self.count(rule("r", source_url=""))["sourced_rules"], 0)
        self.assertEqual(self.count(rule("r", source_quote="   "))["sourced_rules"], 0)
        self.assertEqual(self.count(rule("r", _sources=[]))["sourced_rules"], 0)
        self.assertEqual(self.count(rule("r", source_url=None))["sourced_rules"], 0)

    def test_the_count_is_the_pipeline_s_own_predicate(self) -> None:
        """Not merely equal today — the same function.

        Three copies of "what counts as a source" produced three answers for one
        file (4.8%, 2.1%, 2.0%) before this was centralised.
        """
        from pipeline import provenance

        self.assertEqual(R._has_source(rule("r", source_url=SOURCE)),
                         bool(provenance.row_document_urls(rule("r", source_url=SOURCE))))

    def test_pct_is_rounded(self) -> None:
        m = self.count(rule("a", source_url=SOURCE), rule("b"), rule("c"))
        self.assertEqual(m["sourced_rules_pct"], 33.33)

    def test_no_rules_does_not_divide_by_zero(self) -> None:
        m = R.compute_metrics([card("a")], EMPTY_SOURCES)
        self.assertEqual(m["total_reward_rules"], 0)
        self.assertEqual(m["sourced_rules_pct"], 0.0)


# ---------------------------------------------------------------------------
# Malformed input — a bad card must not kill the weekly job
# ---------------------------------------------------------------------------
class TestMalformedInput(unittest.TestCase):
    def test_cards_must_be_a_list(self) -> None:
        for bad in ({"card": {}}, "cards.json", None, 7):
            with self.assertRaises(TypeError):
                R.compute_metrics(bad, EMPTY_SOURCES)

    def test_sources_state_must_be_a_dict(self) -> None:
        for bad in ([], "state.json", 0):
            with self.assertRaises(TypeError):
                R.compute_metrics([], bad)

    def test_empty_everything(self) -> None:
        m = R.compute_metrics([], {})
        self.assertEqual(m["total_cards"], 0)
        self.assertEqual(m["sourced_rules_pct"], 0.0)
        self.assertEqual(m["sources_stale_days"], 0)
        measured = {k: v for k, v in m.items() if k not in R.NOT_A_METRIC}
        self.assertEqual(sum(measured.values()), 0)

    def test_junk_entries_are_skipped_not_counted(self) -> None:
        cards = [None, "hdfc", 42, [], {}, {"card": "not a dict"}, {"card": {"no": "id"}},
                 card("real")]
        m = R.compute_metrics(cards, EMPTY_SOURCES)
        self.assertEqual(m["total_cards"], 1)

    def test_bare_card_dict_without_the_wrapper(self) -> None:
        m = R.compute_metrics([{"id": "a", "base_reward_rate": 0.02}], EMPTY_SOURCES)
        self.assertEqual(m["total_cards"], 1)
        self.assertEqual(m["zero_rate_cards"], 0)

    def test_bad_reward_rules_container(self) -> None:
        entry = card("a")
        entry["reward_rules"] = {"not": "a list"}
        self.assertEqual(R.compute_metrics([entry], EMPTY_SOURCES)["total_reward_rules"], 0)

    def test_non_dict_rules_are_skipped(self) -> None:
        entry = card("a", rules=[rule("ok")])
        entry["reward_rules"].extend([None, "5% on fuel", 3])
        self.assertEqual(R.compute_metrics([entry], EMPTY_SOURCES)["total_reward_rules"], 1)

    def test_rule_without_a_name_does_not_crash(self) -> None:
        entry = card("a", rules=[rule("x"), {"reward_type": "cashback_pct", "reward_rate": 0.01}])
        m = R.compute_metrics([entry], EMPTY_SOURCES)
        self.assertEqual(m["total_reward_rules"], 2)
        self.assertEqual(m["duplicate_rule_keys"], 0)

    def test_two_unnamed_rules_on_one_card_do_collide(self) -> None:
        entry = card("a", rules=[{"reward_rate": 0.01}, {"reward_rate": 0.02}])
        self.assertEqual(R.compute_metrics([entry], EMPTY_SOURCES)["duplicate_rule_keys"], 1)


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------
class TestSourceHealth(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 13, tzinfo=UTC)

    def metrics(self, state) -> dict:
        return R.compute_metrics([], state, now=self.NOW)

    def test_resolved_and_unresolved(self) -> None:
        m = self.metrics(source_state(
            a=source_row(status="ok"),
            b=source_row(status="unchanged"),
            c=source_row(status="fetch_failed", sha=None),
            d=source_row(status="no_url", sha=None),
            e=source_row(status="not_issuer_domain", sha=None),
        ))
        self.assertEqual(m["sources_resolved"], 2)
        self.assertEqual(m["sources_unresolved"], 3)

    def test_ok_without_a_hash_is_unresolved(self) -> None:
        m = self.metrics(source_state(a=source_row(status="ok", sha=""),
                                      b=source_row(status="ok", sha=None)))
        self.assertEqual(m["sources_resolved"], 0)
        self.assertEqual(m["sources_unresolved"], 2)

    def test_junk_record_is_unresolved(self) -> None:
        m = self.metrics({"sources": {"a": "ok", "b": None}})
        self.assertEqual(m["sources_unresolved"], 2)

    def test_missing_sources_key(self) -> None:
        m = self.metrics({"schema_version": 1})
        self.assertEqual((m["sources_resolved"], m["sources_unresolved"]), (0, 0))

    def test_sources_not_a_dict(self) -> None:
        m = self.metrics({"sources": ["a", "b"]})
        self.assertEqual((m["sources_resolved"], m["sources_unresolved"]), (0, 0))

    def test_staleness_is_the_oldest_resolved_fetch(self) -> None:
        m = self.metrics(source_state(
            fresh=source_row(fetched_at="2026-08-12T00:00:00Z"),
            old=source_row(fetched_at="2026-06-13T00:00:00Z"),
        ))
        self.assertEqual(m["sources_stale_days"], 61)

    def test_staleness_boundary_same_day(self) -> None:
        m = self.metrics(source_state(a=source_row(fetched_at="2026-08-13T00:00:00Z")))
        self.assertEqual(m["sources_stale_days"], 0)

    def test_future_stamp_never_goes_negative(self) -> None:
        m = self.metrics(source_state(a=source_row(fetched_at="2027-01-01T00:00:00Z")))
        self.assertEqual(m["sources_stale_days"], 0)

    def test_unparseable_stamp_is_ignored(self) -> None:
        m = self.metrics(source_state(a=source_row(fetched_at="last tuesday"),
                                      b=source_row(fetched_at=None)))
        self.assertEqual(m["sources_resolved"], 2)
        self.assertEqual(m["sources_stale_days"], 0)

    def test_naive_stamp_is_read_as_utc(self) -> None:
        m = self.metrics(source_state(a=source_row(fetched_at="2026-08-06T00:00:00")))
        self.assertEqual(m["sources_stale_days"], 7)

    def test_naive_now_is_accepted(self) -> None:
        m = R.compute_metrics([], source_state(a=source_row(fetched_at="2026-08-06T00:00:00Z")),
                              now=dt.datetime(2026, 8, 13))
        self.assertEqual(m["sources_stale_days"], 7)


# ---------------------------------------------------------------------------
# diff_metrics
# ---------------------------------------------------------------------------
class TestDiffMetrics(unittest.TestCase):
    def test_down_is_better_metric(self) -> None:
        d = R.diff_metrics({"zero_rate_cards": 106}, {"zero_rate_cards": 100})
        self.assertEqual(d["zero_rate_cards"]["direction"], "better")
        self.assertEqual(d["zero_rate_cards"]["delta"], -6)
        self.assertEqual(d["zero_rate_cards"]["prev"], 106)
        self.assertEqual(d["zero_rate_cards"]["cur"], 100)

        worse = R.diff_metrics({"zero_rate_cards": 100}, {"zero_rate_cards": 106})
        self.assertEqual(worse["zero_rate_cards"]["direction"], "worse")
        self.assertEqual(worse["zero_rate_cards"]["delta"], 6)

    def test_up_is_better_metric(self) -> None:
        d = R.diff_metrics({"sourced_rules": 42}, {"sourced_rules": 54})
        self.assertEqual(d["sourced_rules"]["direction"], "better")
        self.assertEqual(d["sourced_rules"]["delta"], 12)

        lost = R.diff_metrics({"sourced_rules": 54}, {"sourced_rules": 42})
        self.assertEqual(lost["sourced_rules"]["direction"], "worse")

    def test_every_defect_metric_wants_to_go_down(self) -> None:
        for metric in ("zero_rate_cards", "caps_without_unit", "rules_over_ceiling",
                       "duplicate_rule_keys", "non_numeric_caps", "sources_unresolved",
                       "sources_stale_days"):
            self.assertEqual(R.GOOD_DIRECTION[metric], -1, metric)
            d = R.diff_metrics({metric: 10}, {metric: 9})
            self.assertEqual(d[metric]["direction"], "better", metric)

    def test_neutral_metric_never_reads_as_a_regression(self) -> None:
        d = R.diff_metrics({"total_cards": 380}, {"total_cards": 376})
        self.assertEqual(d["total_cards"]["direction"], "flat")
        self.assertEqual(d["total_cards"]["delta"], -4)

    def test_unchanged_is_flat(self) -> None:
        d = R.diff_metrics({"sourced_rules": 54}, {"sourced_rules": 54})
        self.assertEqual(d["sourced_rules"]["direction"], "flat")
        self.assertEqual(d["sourced_rules"]["delta"], 0)

    def test_prev_none_is_all_flat(self) -> None:
        cur = R.compute_metrics([card("a", rules=[rule("x")])], EMPTY_SOURCES)
        d = R.diff_metrics(None, cur)
        self.assertEqual(set(d), set(cur) - R.NOT_A_METRIC)
        for metric, entry in d.items():
            self.assertEqual(entry["direction"], "flat", metric)
            self.assertIsNone(entry["prev"], metric)
            self.assertIsNone(entry["delta"], metric)
            self.assertEqual(entry["cur"], cur[metric])

    def test_new_metric_missing_from_prev_is_flat(self) -> None:
        d = R.diff_metrics({"sourced_rules": 1}, {"sourced_rules": 2, "brand_new": 5})
        self.assertEqual(d["brand_new"]["direction"], "flat")
        self.assertIsNone(d["brand_new"]["prev"])

    def test_float_delta_survives(self) -> None:
        d = R.diff_metrics({"sourced_rules_pct": 3.31}, {"sourced_rules_pct": 4.25})
        self.assertAlmostEqual(d["sourced_rules_pct"]["delta"], 0.94, places=6)
        self.assertEqual(d["sourced_rules_pct"]["direction"], "better")

    def test_non_numeric_values_are_skipped(self) -> None:
        d = R.diff_metrics({"run_at": "2026-08-06T00:00:00+00:00", "sourced_rules": 1},
                           {"run_at": "2026-08-13T00:00:00+00:00", "sourced_rules": 2})
        self.assertNotIn("run_at", d)
        self.assertIn("sourced_rules", d)

    def test_dropped_metric_does_not_haunt_the_table(self) -> None:
        d = R.diff_metrics({"retired_metric": 9, "sourced_rules": 1}, {"sourced_rules": 1})
        self.assertNotIn("retired_metric", d)

    def test_wrong_types_raise(self) -> None:
        with self.assertRaises(TypeError):
            R.diff_metrics({}, [])
        with self.assertRaises(TypeError):
            R.diff_metrics("last week", {})


# ---------------------------------------------------------------------------
# render_trend
# ---------------------------------------------------------------------------
class TestRenderTrend(unittest.TestCase):
    def test_empty_history(self) -> None:
        out = R.render_trend([], "sourced_rules")
        self.assertIn("no history yet", out)
        self.assertEqual(out.count("\n"), 0)

    def test_single_run_does_not_divide_by_zero(self) -> None:
        out = R.render_trend([{"sourced_rules": 54}], "sourced_rules")
        self.assertIn("54", out)
        self.assertTrue(any(ch in out for ch in R._SPARK))

    def test_all_equal_series(self) -> None:
        out = R.render_trend([{"zero_rate_cards": 106}] * 5, "zero_rate_cards")
        self.assertIn("106 → 106", out)
        self.assertIn(R._SPARK[len(R._SPARK) // 2] * 5, out)

    def test_rising_series_ends_higher_than_it_starts(self) -> None:
        history = [{"sourced_rules": v} for v in (0, 12, 30, 54)]
        out = R.render_trend(history, "sourced_rules")
        bar = "".join(ch for ch in out if ch in R._SPARK)
        self.assertEqual(len(bar), 4)
        self.assertEqual(bar[0], R._SPARK[0])
        self.assertEqual(bar[-1], R._SPARK[-1])
        self.assertIn("0 → 54", out)

    def test_width_keeps_the_last_runs_only(self) -> None:
        history = [{"sourced_rules": v} for v in range(100)]
        out = R.render_trend(history, "sourced_rules", width=10)
        bar = "".join(ch for ch in out if ch in R._SPARK)
        self.assertEqual(len(bar), 10)
        self.assertIn("90 → 99", out)

    def test_zero_and_negative_width_do_not_crash(self) -> None:
        history = [{"sourced_rules": v} for v in (1, 2, 3)]
        for width in (0, -5):
            bar = "".join(ch for ch in R.render_trend(history, "sourced_rules", width=width)
                          if ch in R._SPARK)
            self.assertEqual(len(bar), 1)

    def test_rows_missing_the_metric_are_skipped(self) -> None:
        history = [{"other": 1}, {"sourced_rules": 5}, {"sourced_rules": None}]
        out = R.render_trend(history, "sourced_rules")
        self.assertIn("5 → 5", out)

    def test_unknown_metric_is_not_an_error(self) -> None:
        self.assertIn("no history yet", R.render_trend([{"a": 1}], "not_a_metric"))

    def test_junk_history_does_not_raise(self) -> None:
        self.assertIn("no history yet", R.render_trend(["junk", None, 7], "sourced_rules"))
        self.assertIn("no history yet", R.render_trend("not a list", "sourced_rules"))
        self.assertIn("no history yet", R.render_trend([{"sourced_rules": "many"}], "sourced_rules"))


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------
class TestRenderReport(unittest.TestCase):
    def setUp(self) -> None:
        self.cur = R.compute_metrics(
            [card("a", rules=[rule("x", source_url="https://www.hdfc.bank.in/a")]),
             card("b", base=0)],
            source_state(a=source_row()),
            now=dt.datetime(2026, 8, 13, tzinfo=UTC),
        )

    def test_first_run_does_not_crash(self) -> None:
        out = R.render_report(self.cur, None)
        self.assertIn("First run", out)
        self.assertIn("# KredMe card data", out)
        self.assertIn("No previous run", out)
        self.assertNotIn("WORSE", out)

    def test_headline_is_the_first_thing_after_the_title(self) -> None:
        head = [ln for ln in R.render_report(self.cur, None).splitlines() if ln.strip()][1]
        self.assertTrue(head.startswith("**") and head.endswith("**"), head)

    def test_plain_words_not_metric_names(self) -> None:
        out = R.render_report(self.cur, None)
        self.assertIn("cite the bank's own document", out)
        self.assertIn("show a user 0.0%", out)
        self.assertNotIn("sourced_rules", out)
        self.assertNotIn("zero_rate_cards", out)

    def test_progress_leads_the_headline(self) -> None:
        prev = dict(self.cur, sourced_rules=0, sourced_rules_pct=0.0)
        out = R.render_report(self.cur, prev)
        self.assertIn("1 more reward rule now cites the bank's own document", out)

    def test_headline_pluralises(self) -> None:
        cur = dict(self.cur, sourced_rules=13, sourced_rules_pct=1.02)
        out = R.render_report(cur, dict(self.cur, sourced_rules=1))
        self.assertIn("12 more reward rules now cite the bank's own document", out)

    def test_regression_is_marked_and_listed(self) -> None:
        prev = dict(self.cur, zero_rate_cards=0)
        out = R.render_report(self.cur, prev)
        self.assertIn("WORSE", out)
        self.assertIn("Needs a human", out)
        self.assertIn("1 more card that shows a user 0.0%", out)

    def test_regression_pluralises(self) -> None:
        out = R.render_report(dict(self.cur, zero_rate_cards=9),
                              dict(self.cur, zero_rate_cards=0))
        self.assertIn("9 more cards that show a user 0.0%", out)

    def test_lost_citations_lead_over_everything(self) -> None:
        prev = dict(self.cur, sourced_rules=5, zero_rate_cards=0)
        out = R.render_report(self.cur, prev)
        self.assertIn("4 reward rules LOST the citation they had", out)

    def test_one_lost_citation_reads_as_one(self) -> None:
        out = R.render_report(self.cur, dict(self.cur, sourced_rules=2))
        self.assertIn("1 reward rule LOST the citation it had", out)

    def test_regression_headline_still_states_where_citations_stand(self) -> None:
        out = R.render_report(self.cur, dict(self.cur, zero_rate_cards=0))
        self.assertIn("Worse this week", out)
        self.assertIn("Citations unchanged at 1 of 1 (100.0%)", out)

    def test_worst_regression_leads(self) -> None:
        # A rate no card pays is what a user actually sees, so it outranks a
        # cap-unit gap even when the cap-unit gap is numerically larger.
        prev = dict(self.cur, rules_over_ceiling=0, caps_without_unit=0)
        cur = dict(self.cur, rules_over_ceiling=1, caps_without_unit=40)
        head = [ln for ln in R.render_report(cur, prev).splitlines() if ln.strip()][1]
        self.assertIn("rule displaying a rate no Indian card pays", head)
        self.assertNotIn("capped rule", head)

    def test_improvement_without_new_citations_still_leads(self) -> None:
        out = R.render_report(self.cur, dict(self.cur, zero_rate_cards=9))
        self.assertIn("Better this week", out)
        self.assertIn("8 fewer cards that show a user 0.0%", out)
        self.assertNotIn("Needs a human", out)

    def test_nothing_moved_is_said_plainly(self) -> None:
        out = R.render_report(self.cur, dict(self.cur))
        self.assertIn("Nothing moved this week", out)

    def test_applied_and_blocked_counts(self) -> None:
        out = R.render_report(self.cur, None, applied=3, blocked=1)
        self.assertIn("**3**", out)
        self.assertIn("**1**", out)
        self.assertIn("changed no card data", R.render_report(self.cur, None))

    def test_table_has_a_row_per_metric(self) -> None:
        out = R.render_report(self.cur, dict(self.cur))
        rows = [ln for ln in out.splitlines() if ln.startswith("| ") and "---" not in ln]
        measured = set(self.cur) - R.NOT_A_METRIC
        self.assertEqual(len(rows), len(measured) + 1)  # + the header row

    def test_history_row_with_a_timestamp_renders(self) -> None:
        prev = dict(self.cur, run_at="2026-08-06T00:00:00+00:00")
        out = R.render_report(dict(self.cur, run_at="2026-08-13T00:00:00+00:00"), prev)
        self.assertNotIn("run_at", out)

    def test_wrong_type_raises(self) -> None:
        with self.assertRaises(TypeError):
            R.render_report([], None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class TestCLI(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pipeline.report", *args],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )

    def test_write_appends_one_history_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            cards = tmpdir / "cards.json"
            cards.write_text(json.dumps([card("a", rules=[rule("x")])]), encoding="utf-8")
            history = tmpdir / "metrics.jsonl"

            first = self.run_cli("--cards", str(cards), "--sources", str(tmpdir / "none.json"),
                                 "--history", str(history), "--write")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("KredMe card data", first.stdout)

            second = self.run_cli("--cards", str(cards), "--sources", str(tmpdir / "none.json"),
                                  "--history", str(history), "--write")
            self.assertEqual(second.returncode, 0, second.stderr)

            rows = [json.loads(ln) for ln in history.read_text(encoding="utf-8").splitlines() if ln]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["total_cards"], 1)
            self.assertIn("run_at", rows[0])

    def test_default_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = pathlib.Path(tmp)
            cards = tmpdir / "cards.json"
            cards.write_text(json.dumps([card("a")]), encoding="utf-8")
            history = tmpdir / "metrics.jsonl"
            res = self.run_cli("--cards", str(cards), "--history", str(history))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(history.exists())

    def test_json_output_is_a_flat_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cards = pathlib.Path(tmp) / "cards.json"
            cards.write_text(json.dumps([card("a")]), encoding="utf-8")
            res = self.run_cli("--cards", str(cards), "--history",
                               str(pathlib.Path(tmp) / "h.jsonl"), "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            row = json.loads(res.stdout)
            self.assertEqual(set(row) - R.NOT_A_METRIC, set(R.GOOD_DIRECTION))

    def test_missing_cards_file_is_a_data_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            res = self.run_cli("--cards", str(pathlib.Path(tmp) / "nope.json"),
                               "--history", str(pathlib.Path(tmp) / "h.jsonl"))
            self.assertEqual(res.returncode, 1)
            self.assertIn("no card data", res.stderr)

    def test_malformed_cards_file_is_a_data_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cards = pathlib.Path(tmp) / "cards.json"
            cards.write_text("{not json", encoding="utf-8")
            res = self.run_cli("--cards", str(cards), "--history",
                               str(pathlib.Path(tmp) / "h.jsonl"))
            self.assertEqual(res.returncode, 1)
            self.assertIn("cannot read", res.stderr)

    def test_wrong_shape_cards_file_is_a_data_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cards = pathlib.Path(tmp) / "cards.json"
            cards.write_text('{"cards": []}', encoding="utf-8")
            res = self.run_cli("--cards", str(cards), "--history",
                               str(pathlib.Path(tmp) / "h.jsonl"))
            self.assertEqual(res.returncode, 1)
            self.assertIn("not a list", res.stderr)

    def test_trend_needs_no_card_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = pathlib.Path(tmp) / "metrics.jsonl"
            history.write_text("\n".join(json.dumps({"sourced_rules": v}) for v in (0, 12, 54)),
                               encoding="utf-8")
            res = self.run_cli("--history", str(history), "--trend", "sourced_rules",
                               "--cards", str(pathlib.Path(tmp) / "nope.json"))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("0 → 54", res.stdout)


# ---------------------------------------------------------------------------
# The real seed. Invariants only — the counts move every week by design.
# ---------------------------------------------------------------------------
class TestAgainstRealSeed(unittest.TestCase):
    @unittest.skipUnless(C.CARDS_JSON.exists(), "seed/cards.json not present")
    def test_real_catalogue(self) -> None:
        cards = json.loads(C.CARDS_JSON.read_text(encoding="utf-8"))
        m = R.compute_metrics(cards, S_load())
        self.assertGreater(m["total_cards"], 300)
        self.assertGreaterEqual(m["total_cards"], m["active_cards"])
        self.assertGreater(m["total_reward_rules"], m["total_cards"])
        self.assertLessEqual(m["sourced_rules"], m["total_reward_rules"])
        self.assertLessEqual(m["non_numeric_caps"], m["caps_without_unit"])
        self.assertEqual(m["rules_over_ceiling"], 0)  # the publish gate already enforces 40%
        for key, value in m.items():
            self.assertIsInstance(value, (int, float), key)
        # The report must render over real data without touching it.
        before = json.dumps(cards, sort_keys=True)
        text = R.render_report(m, None)
        self.assertIn("# KredMe card data", text)
        self.assertEqual(before, json.dumps(cards, sort_keys=True))


def S_load() -> dict:
    from pipeline import state as st
    return st.load_state()


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
