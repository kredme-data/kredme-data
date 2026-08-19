#!/usr/bin/env python3
"""
Tests for the evidence-gap catch-up: pipeline/provenance.py and the
`refresh --unsourced-only` path in pipeline/cli.py.

Usage:
    python3 tests/test_provenance.py

What is being protected here, in one sentence: the weekly refresh is a change
detector, and a card that has never been verified will never change enough to be
noticed, so it needs a second way in — one that opens the content-hash gate for
exactly those cards and for nobody else. Everything below is a way for that
sentence to stop being true.

Three failures are specifically guarded against, because each of them would be
expensive and silent:

  * the gate opening for cards it should not (that is a $94.55 full sweep wearing
    a different flag),
  * `--limit N` picking the same N cards every week (a backlog that never moves
    while the log says it is working),
  * a dry run writing state or submitting anything.

No network, no pip, stdlib unittest.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import cli  # noqa: E402
from pipeline import config as C  # noqa: E402
from pipeline import provenance as P  # noqa: E402
from pipeline import sources as S  # noqa: E402
from pipeline import state as ST  # noqa: E402

ISSUER_URL = "https://www.hdfc.bank.in/credit-cards/regalia"


def card(card_id="c1", *, rules=None, provenance=None, active=1, issuer="HDFC Bank"):
    """A seed entry in the shape cards.json actually uses."""
    entry = {
        "card": {"id": card_id, "card_name": card_id, "issuer": issuer, "is_active": active},
        "reward_rules": list(rules or []),
    }
    if provenance is not None:
        entry["_provenance"] = provenance
    return entry


def src(card_id, url=ISSUER_URL, issuer="hdfc", reason=""):
    return S.Source(card_id=card_id, card_name=card_id, issuer=issuer, url=url, reason=reason)


def _snapshot_tracked_state() -> dict:
    """The bytes of the tracked state files as this module is imported.

    Compared against at the end of the run. See TestTestsDoNotWriteTrackedState
    for why this is a snapshot and not `git diff`.
    """
    out = {}
    for rel in ("pipeline/state/batch.json", "pipeline/state/sources.json"):
        path = REPO / rel
        out[rel] = path.read_bytes() if path.exists() else None
    return out


_TRACKED_STATE_AT_IMPORT = _snapshot_tracked_state()


class TestWhatCountsAsEvidence(unittest.TestCase):
    """The predicate, on every shape the seed actually contains.

    seed/cards.json is not shape-consistent: source_url appears as a string and as
    a list, _sources as a string, a list of strings, a list of dicts and a bare
    dict. A reader that assumes one shape does not report less evidence — it
    reports none for whichever cards used the other shape, and we pay to re-read
    them.
    """

    def test_rule_level_source_url_is_evidence(self):
        self.assertTrue(P.card_has_issuer_evidence(
            card(rules=[{"rule_name": "r", "source_url": ISSUER_URL}])))

    def test_a_card_field_stamp_is_NOT_reward_rule_evidence(self):
        """REGRESSION. The predicate has to count the unit the metric counts.

        `_provenance` records CARD FIELDS — annual_fee_inr, forex_markup_pct,
        point_value_inr. Treating it as evidence exempted 25 active cards from
        the catch-up on a stamp about an annual fee while they carried 91 uncited
        reward rules, and the validator graded 22 of those 25 F on exactly the
        metric this pipeline exists to move. A card the pipeline permanently
        skips while the validator still calls it unsourced is the forever-skip
        failure this whole change was built to remove.
        """
        entry = card(rules=[{"rule_name": "r"}],
                     provenance=[{"field": "annual_fee_inr", "source_url": ISSUER_URL}])
        self.assertFalse(P.card_has_issuer_evidence(entry))
        self.assertTrue(P.card_provenance_only(entry))

    def test_a_rule_citation_beats_the_stamp(self):
        entry = card(rules=[{"rule_name": "r", "source_url": ISSUER_URL}],
                     provenance=[{"source_url": ISSUER_URL}])
        self.assertTrue(P.card_has_issuer_evidence(entry))
        self.assertFalse(P.card_provenance_only(entry))

    def test_neither_is_not_evidence(self):
        self.assertFalse(P.card_has_issuer_evidence(
            card(rules=[{"rule_name": "r", "reward_rate": 0.01}])))

    def test_source_url_as_a_list_counts(self):
        self.assertTrue(P.card_has_issuer_evidence(
            card(rules=[{"source_url": [ISSUER_URL]}])))

    def test_sources_list_of_strings_counts(self):
        # au_small_finance_bank_ixigo_au cites its documents entirely through
        # else, entirely through _sources. Reading only source_url would put a
        # card we HAVE verified back in the queue.
        self.assertTrue(P.card_has_issuer_evidence(
            card(rules=[{"_sources": ["https://www.au.bank.in/pdf/x.pdf"]}])))

    def test_sources_list_of_dicts_counts(self):
        self.assertTrue(P.card_has_issuer_evidence(
            card(rules=[{"_sources": [{"url": ISSUER_URL, "quote": "x"}]}])))

    def test_sources_bare_dict_counts(self):
        self.assertTrue(P.card_has_issuer_evidence(
            card(rules=[{"_sources": {"href": ISSUER_URL}}])))

    def test_a_bare_host_counts(self):
        # The seed carries both shapes; a bare host still names a site somebody
        # can open.
        self.assertTrue(P.card_has_issuer_evidence(card(rules=[{"source_url": "sbicard.com"}])))

    def test_the_placeholder_word_bank_is_not_evidence(self):
        # Seven rules in the real catalogue cite the literal string "bank".
        # Counting that would flatter the coverage number and, worse, skip cards
        # nobody has ever read.
        self.assertFalse(P.card_has_issuer_evidence(card(rules=[{"_sources": ["bank"]}])))

    def test_an_empty_string_is_not_evidence(self):
        self.assertFalse(P.card_has_issuer_evidence(card(rules=[{"source_url": "   "}])))

    def test_a_quote_with_no_document_is_not_evidence(self):
        # A quote nobody can re-check next quarter is not provenance; the whole
        # point of a source is that the issuer's devaluation can be diffed.
        self.assertFalse(P.card_has_issuer_evidence(
            card(rules=[{"source_quote": "Earn 5% back on dining"}])))

    def test_an_empty_provenance_block_is_not_evidence(self):
        for empty in ([], {}, None):
            with self.subTest(block=empty):
                self.assertFalse(P.card_has_issuer_evidence(
                    card(rules=[{"rule_name": "r"}], provenance=empty)))

    def test_a_provenance_block_with_no_url_is_not_evidence(self):
        self.assertFalse(P.card_has_issuer_evidence(
            card(provenance=[{"field": "annual_fee_inr", "old_value": 1}])))

    def test_malformed_rows_do_not_raise(self):
        # One card in this repo ships exclusion_rules as a prose STRING. Any
        # shape-assuming reader crashes on it, and a crashed weekly job is how a
        # catalogue rots unnoticed.
        for entry in (
            card(rules=["not a rule", 7, None]),
            {"card": {"id": "x"}, "reward_rules": "a sentence, not a list"},
            {"card": {"id": "x"}, "_provenance": "a sentence"},
            {"reward_rules": [{"source_url": ISSUER_URL}]},   # no card block
            "not an entry at all",
            None,
        ):
            with self.subTest(entry=repr(entry)[:40]):
                self.assertIsInstance(P.card_has_issuer_evidence(entry), bool)

    def test_a_malformed_row_beside_a_good_one_still_finds_the_good_one(self):
        self.assertTrue(P.card_has_issuer_evidence(
            card(rules=["junk", {"source_url": ISSUER_URL}, None])))

    def test_non_http_schemes_are_not_documents(self):
        for junk in ("mailto:someone@hdfcbank.com", "ftp://hdfcbank.com/x", "javascript:void"):
            with self.subTest(value=junk):
                self.assertFalse(P.card_has_issuer_evidence(card(rules=[{"source_url": junk}])))

    def test_evidence_does_not_require_the_issuer_allowlist(self):
        """A source we would not ACCEPT is still a source we have already read.

        equitas.bank.in and theunitybank.com are missing from
        config.ISSUER_DOMAINS. Folding that allowlist into this predicate would
        queue four already-cited cards for a paid re-read because of a gap in a
        hand-maintained list, which is not an evidence gap. Judging the QUALITY of
        a source is L8's job (L8.SOURCE_NOT_ISSUER_DOMAIN), not this one's.
        """
        url = "https://equitas.bank.in/personal-banking/pay/credit-cards/selfe-credit-card/"
        self.assertFalse(C.is_issuer_domain(url))
        self.assertTrue(P.card_has_issuer_evidence(card(rules=[{"source_url": url}])))


class TestTheDefinitionIsSharedWithTheValidator(unittest.TestCase):
    """L8 and this pipeline must be the same code, not merely agree today.

    Two copies of "what counts as a source" drift within a month, and the first
    symptom is paying to re-read cards a validation report already calls sourced.
    """

    def _c8(self):
        sys.path.insert(0, str(REPO / "tools"))
        from checks import c8_provenance  # noqa: PLC0415

        return c8_provenance

    def _ctx(self):
        sys.path.insert(0, str(REPO / "tools"))
        from checks.base import Ctx  # noqa: PLC0415

        return Ctx(
            seed_dir=REPO / "seed", news_dir=REPO / "news",
            cards=json.loads((REPO / "seed" / "cards.json").read_text(encoding="utf-8")),
            merchants={}, manifest={}, news=None,
            app_categories=None, app_root=None,
        )

    def test_l8_imports_the_harvester_rather_than_copying_it(self):
        c8 = self._c8()
        self.assertIs(c8._url_candidates, P.source_url_candidates)
        self.assertIs(c8._host_of, P.source_host)
        self.assertIs(c8._card_has_evidence, P.card_has_issuer_evidence)

    def test_l8_no_longer_carries_its_own_copy(self):
        body = (REPO / "tools" / "checks" / "c8_provenance.py").read_text(encoding="utf-8")
        self.assertNotIn("def _url_candidates(", body)
        self.assertNotIn("def _host_of(", body)
        # The issuer -> domain table and the aggregator lists moved too, on
        # 2026-08-19. They were the last thing the two sides each kept a copy of,
        # and they are why the same file read 26 here and 27 in `evidence`.
        self.assertNotIn("def _issuer_domains(", body)
        self.assertNotIn("def _host_matches(", body)
        self.assertNotIn("def _aggregator_of(", body)

    def test_l8_imports_the_issuer_domain_table_too(self):
        c8 = self._c8()
        self.assertIs(c8._ISSUER_DOMAINS, P.ISSUER_PUBLISHES_ON)
        self.assertIs(c8._issuer_domains, P.issuer_domains)
        self.assertIs(c8._host_matches, P.host_matches)

    def test_metrics_counts_the_same_rules_as_the_evidence_command(self):
        """One file, one number — this repo briefly had three.

        `metrics` said 61 of 1,279 (4.8%), `evidence` said 27 (2.1%) and the
        validator said 26 (2.0%), all of the same bytes on the same day. The
        founder-facing command was the most generous of the three, which is the
        worst possible place for the loosest definition to live.
        """
        from pipeline import report

        cards = S.load_cards()
        metrics = report.compute_metrics(cards, {"sources": {}})
        counts = P.rule_evidence_counts(cards)
        self.assertEqual(metrics["sourced_rules"], counts["cited"])

    def test_the_evidence_report_prints_the_validator_s_number_too(self):
        """REGRESSION. Two rule-level percentages under the same words.

        `evidence` printed 27 of 1,279 (2.1%) and the validator printed 26 (2.0%)
        for the same bytes on the same day, both labelled as THE citation share.
        The single divergent rule is on au_small_finance_bank_ixigo_au, which
        cites https://www.ixigo.com/travel-credit-card — a real page, and not an
        AU page. Both counts are now computed in this module, from the same
        tables the validator imports, and both are printed with their own label.
        """
        cards = S.load_cards()
        counts = P.rule_evidence_counts(cards)
        recs = [r for c in self._c8()._ledger(self._ctx())[0] for r in c["rules"]
                if r["ok"]]
        self.assertEqual(counts["issuer_cited"],
                         sum(1 for r in recs if r["issuer_url"]))
        self.assertGreaterEqual(counts["cited"], counts["issuer_cited"])

    def test_l8_counts_the_same_backlog_the_pipeline_would_queue(self):
        cards = S.load_cards()
        pipeline_gap = {
            cid for cid in P.unsourced_card_ids(cards)
            if any(s.card_id == cid for s in _real_sources(cards))
        }
        c8 = self._c8()
        seen = set()
        for entry in cards:
            inner = S._inner_card(entry) or {}
            cid = str(inner.get("id") or "")
            if cid and c8._truthy_active(inner) and not c8._card_has_evidence(entry):
                seen.add(cid)
        self.assertEqual(seen, pipeline_gap)


def _real_sources(cards):
    return S.resolve_sources(cards, S.load_overrides(S.OVERRIDES_JSON))


class TestSelection(unittest.TestCase):
    """Which cards a catch-up run picks, and which it must never pick."""

    def setUp(self):
        self.state = {"schema_version": 1, "sources": {}}

    def test_only_unsourced_cards_are_selected(self):
        cards = [
            card("sourced", rules=[{"source_url": ISSUER_URL}]),
            card("bare", rules=[{"rule_name": "r"}]),
        ]
        plan = P.plan_catch_up([src("sourced"), src("bare")], cards, self.state)
        self.assertEqual([s.card_id for s in plan.selected], ["bare"])
        self.assertEqual(plan.sourced, 1)

    def test_inactive_cards_are_excluded(self):
        """Activity is decided ONCE, by sources.resolve_sources.

        An inactive card is absent from the resolved source list, so it cannot be
        selected however unsourced it is — and this module never re-decides
        activity, because a second copy of that default (absent means ACTIVE) is
        exactly the drift being designed out.
        """
        cards = [card("live", rules=[{}]), card("dead", rules=[{}], active=0)]
        resolved = S.resolve_sources(cards)
        self.assertEqual([s.card_id for s in resolved], ["live"])
        plan = P.plan_catch_up(resolved, cards, self.state)
        self.assertEqual([s.card_id for s in plan.selected], ["live"])
        self.assertIn("dead", P.unsourced_card_ids(cards))   # counted, just not queued
        self.assertEqual(plan.inactive_unsourced, 1)

    def test_cards_with_no_url_are_reported_not_dropped(self):
        """Four real cards resolve to no issuer URL at all.

        Dropping them silently makes the backlog look 4 cards smaller than it is,
        every week, forever — a shrinking number that represents no work done.
        """
        cards = [card("reachable", rules=[{}]), card("nowhere", rules=[{}])]
        plan = P.plan_catch_up(
            [src("reachable"), src("nowhere", url="", reason="no_issuer_landing: csb")],
            cards, self.state,
        )
        self.assertEqual([s.card_id for s in plan.selected], ["reachable"])
        self.assertEqual([s.card_id for s in plan.unreachable], ["nowhere"])
        self.assertEqual(plan.unreachable[0].reason, "no_issuer_landing: csb")

    def test_limit_slices_the_ordered_backlog(self):
        cards = [card(f"c{i}", rules=[{}]) for i in range(10)]
        plan = P.plan_catch_up([src(f"c{i}") for i in range(10)], cards, self.state, limit=4)
        self.assertEqual(len(plan.selected), 4)
        self.assertEqual(plan.backlog, 10)
        self.assertEqual(plan.deferred, 6)

    def test_a_limit_of_zero_means_everything(self):
        cards = [card(f"c{i}", rules=[{}]) for i in range(5)]
        plan = P.plan_catch_up([src(f"c{i}") for i in range(5)], cards, self.state, limit=0)
        self.assertEqual(len(plan.selected), 5)

    def test_selection_is_deterministic(self):
        cards = [card(f"c{i}", rules=[{}]) for i in range(20)]
        srcs = [src(f"c{i}", issuer=f"bank{i % 4}") for i in range(20)]
        first = [s.card_id for s in P.plan_catch_up(srcs, cards, self.state, limit=7).selected]
        second = [s.card_id for s in P.plan_catch_up(srcs, cards, self.state, limit=7).selected]
        self.assertEqual(first, second)

    def test_a_slice_spreads_across_issuers(self):
        """Card ids begin with the issuer name, so a sorted --limit 40 is 40 Axis
        cards: one issuer's outage wastes a whole week, fetch_many's per-host
        worker runs them serially while seven workers idle, and the reviewer gets
        a single-bank PR instead of a cross-section.
        """
        cards, srcs = [], []
        for bank in ("axis", "hdfc", "sbi", "icici"):
            for i in range(10):
                cards.append(card(f"{bank}_{i}", rules=[{}]))
                srcs.append(src(f"{bank}_{i}", issuer=bank))
        picked = P.plan_catch_up(srcs, cards, self.state, limit=8).selected
        self.assertEqual(len({s.issuer for s in picked}), 4)


class TestLimitPaginatesAcrossRuns(unittest.TestCase):
    """The failure this whole ordering exists to prevent.

    A `--limit 40` that re-picks the same 40 cards is worse than doing nothing: it
    bills roughly $10 a week, prints a healthy-looking log, and the backlog never
    moves. This repo has already had one loop that ran 12 times and produced
    nothing while every run showed green.
    """

    def setUp(self):
        self.cards = [card(f"c{i:03d}", rules=[{}]) for i in range(25)]
        self.srcs = [src(f"c{i:03d}", issuer=f"bank{i % 5}") for i in range(25)]
        self.state = {"schema_version": 1, "sources": {}}

    def _run(self, when, limit=10):
        picked = [s.card_id for s in
                  P.plan_catch_up(self.srcs, self.cards, self.state, limit=limit).selected]
        for cid in picked:
            ST.note_evidence_attempt(self.state, cid, when)
        return picked

    def test_consecutive_runs_do_not_repeat(self):
        first = self._run("2026-08-19T00:00:00Z")
        second = self._run("2026-08-26T00:00:00Z")
        self.assertEqual(set(first) & set(second), set())

    def test_the_whole_backlog_is_read_before_anything_is_read_twice(self):
        seen = []
        for week in range(3):
            seen += self._run(f"2026-09-{week + 1:02d}T00:00:00Z")
        self.assertEqual(len(set(seen)), 25)
        self.assertEqual(sorted(set(seen)), sorted(s.card_id for s in self.srcs))

    def test_a_second_pass_starts_only_after_the_first_finishes(self):
        seen = []
        for week in range(4):
            seen += self._run(f"2026-09-{week + 1:02d}T00:00:00Z")
        self.assertEqual(len(seen), 40)
        self.assertEqual(len(set(seen)), 25)   # the 4th week re-reads, as designed

    def test_an_unfetchable_card_does_not_block_the_queue(self):
        """Attempts are counted for every card SELECTED, not every card read.

        BOBCARD's server omits its intermediate certificate, so none of its cards
        can be fetched at all. If only successful reads counted, those cards would
        sit at attempts=0 and be picked first every single week, and no other
        issuer would ever advance.
        """
        first = self._run("2026-08-19T00:00:00Z")
        # Nothing "succeeded" here — note_evidence_attempt was called anyway.
        second = self._run("2026-08-26T00:00:00Z")
        self.assertEqual(set(first) & set(second), set())

    def test_a_card_that_gains_evidence_leaves_the_queue(self):
        self._run("2026-08-19T00:00:00Z")
        self.cards[13]["reward_rules"] = [{"source_url": ISSUER_URL}]
        remaining = P.plan_catch_up(self.srcs, self.cards, self.state).selected
        self.assertNotIn("c013", [s.card_id for s in remaining])


class TestEvidenceAttemptState(unittest.TestCase):
    def test_a_never_selected_card_sorts_first(self):
        self.assertEqual(ST.evidence_attempts({"sources": {}}, "c1"), (0, ""))

    def test_attempts_accumulate(self):
        st = {"sources": {}}
        ST.note_evidence_attempt(st, "c1", "2026-08-19T00:00:00Z")
        ST.note_evidence_attempt(st, "c1", "2026-08-26T00:00:00Z")
        self.assertEqual(ST.evidence_attempts(st, "c1"), (2, "2026-08-26T00:00:00Z"))

    def test_record_source_cannot_clobber_the_counter(self):
        """record_source REPLACES a card's whole dict every week.

        Any rotation marker parked on that record would be wiped the following
        Monday and the backlog would silently restart from the top, which is why
        this lives in its own section.
        """
        st = {"sources": {}}
        ST.note_evidence_attempt(st, "c1", "2026-08-19T00:00:00Z")
        ST.record_source(st, "c1", url=ISSUER_URL, content_sha256="abc",
                         fetched_at="2026-08-26T00:00:00Z", status="fetched")
        self.assertEqual(ST.evidence_attempts(st, "c1")[0], 1)

    def test_a_corrupt_section_degrades_to_zero_rather_than_raising(self):
        for junk in ("nonsense", [], {"c1": "nonsense"}, {"c1": {"attempts": "many"}}):
            with self.subTest(junk=junk):
                self.assertEqual(ST.evidence_attempts({"evidence_runs": junk}, "c1"), (0, ""))

    def test_the_counter_survives_a_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sources.json"
            st = ST.load_state(path)
            ST.note_evidence_attempt(st, "c1", "2026-08-19T00:00:00Z")
            ST.save_state(st, path)
            self.assertEqual(ST.evidence_attempts(ST.load_state(path), "c1"),
                             (1, "2026-08-19T00:00:00Z"))


class TestTheHashGate(unittest.TestCase):
    """Opened for the selected cards. Shut for everything else.

    `--force` already opens it for all 370 active cards and bills a measured
    $94.55 (up to $117.65 if every extracted card also reaches verification). If
    this flag ever becomes a synonym for that, the saving that makes the weekly
    run affordable is gone and the log will not say so.
    """

    def setUp(self):
        self.st = {"schema_version": 1, "sources": {}}
        for cid in ("gap", "settled"):
            ST.record_source(self.st, cid, url=ISSUER_URL, content_sha256="same",
                             fetched_at="t", status=ST.STATUS_DONE)

    def test_a_selected_card_is_read_even_though_its_page_did_not_move(self):
        extract, why = cli._gate_decision(self.st, "gap", "same",
                                          force=False, evidence_gap=True)
        self.assertTrue(extract)
        self.assertEqual(why, "evidence gap")

    def test_an_unselected_card_is_still_gated_in_the_same_run(self):
        extract, why = cli._gate_decision(self.st, "settled", "same",
                                          force=False, evidence_gap=False)
        self.assertFalse(extract)
        self.assertEqual(why, "unchanged")

    def test_a_moved_page_is_read_for_the_ordinary_reason(self):
        extract, why = cli._gate_decision(self.st, "settled", "different",
                                          force=False, evidence_gap=False)
        self.assertTrue(extract)
        self.assertEqual(why, "changed")

    def test_changed_wins_over_evidence_gap_so_the_reason_is_not_misreported(self):
        _extract, why = cli._gate_decision(self.st, "gap", "different",
                                           force=False, evidence_gap=True)
        self.assertEqual(why, "changed")

    def test_force_still_opens_it_for_everyone(self):
        extract, why = cli._gate_decision(self.st, "settled", "same",
                                          force=True, evidence_gap=False)
        self.assertTrue(extract)
        self.assertEqual(why, "force")

    def test_a_card_fetched_but_never_finished_is_still_read(self):
        # STATUS 'fetched' means the bytes were paid for and never turned into a
        # verdict, so the work still needs doing — this predates the flag and
        # must not have been weakened by it.
        ST.record_source(self.st, "stalled", url=ISSUER_URL, content_sha256="same",
                         fetched_at="t", status="fetched")
        extract, why = cli._gate_decision(self.st, "stalled", "same",
                                          force=False, evidence_gap=False)
        self.assertTrue(extract)
        self.assertEqual(why, "changed")

    def test_the_gate_is_bypassed_for_exactly_the_selected_set(self):
        selected = {"a", "c"}
        for cid in ("a", "b", "c", "d"):
            ST.record_source(self.st, cid, url=ISSUER_URL, content_sha256="same",
                             fetched_at="t", status=ST.STATUS_DONE)
        opened = {
            cid for cid in ("a", "b", "c", "d")
            if cli._gate_decision(self.st, cid, "same",
                                  force=False, evidence_gap=cid in selected)[0]
        }
        self.assertEqual(opened, selected)


class TestCostGuard(unittest.TestCase):
    """One spend ceiling, checked earlier — never a second ceiling."""

    @staticmethod
    def _args(**kw):
        base = dict(max_usd=None, i_accept_usd=None, dry_run=False,
                    allow_concurrent_batch=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_each_pass_is_priced_off_its_own_request_count(self):
        """REGRESSION. Verification was divided by the EXTRACTION denominator.

        The 17-Aug bill is one bill with two batches of different sizes: extract
        371 requests / $59.74, verify 223 requests / $34.81. build_verify_request
        is one request per CARD, so a card that reaches verification costs
        $34.81/223 = $0.1561, not $34.81/371 = $0.0938. Dividing both by 371
        understated every both-passes forecast by 24.4% — in the dangerous
        direction, on the exact numbers an operator reads to decide whether to
        spend.
        """
        self.assertAlmostEqual(C.USD_PER_CARD_EXTRACT, 59.74 / 371, places=6)
        self.assertAlmostEqual(C.USD_PER_CARD_VERIFY, 34.81 / 223, places=6)
        self.assertAlmostEqual(
            C.USD_PER_CARD_BOTH_PASSES, 59.74 / 371 + 34.81 / 223, places=6)
        self.assertAlmostEqual(P.forecast_usd(371, both_passes=False), 59.74, places=2)
        # Every extracted card verified: the ceiling, and the default.
        self.assertAlmostEqual(P.forecast_usd(371), 117.65, places=2)
        # 17-Aug's own yield, quoted as the floor and never on its own.
        self.assertAlmostEqual(P.forecast_usd(371, basis="observed"), 94.55, places=2)
        self.assertGreater(P.forecast_usd(155), P.forecast_usd(155, basis="observed"))

    def test_the_forecast_is_never_quoted_below_what_the_cycle_can_bill(self):
        for n in (1, 40, 155, 332):
            self.assertGreaterEqual(
                P.forecast_usd(n), P.forecast_usd(n, both_passes=False))
            self.assertGreaterEqual(P.forecast_usd(n),
                                    P.forecast_usd(n, basis="observed"))

    def test_a_full_backlog_is_refused_before_anything_is_fetched(self):
        with mock.patch.object(cli.F, "fetch_many") as fetched:
            rc = cli._preflight_budget(332, self._args(), dry_run=False)
        self.assertEqual(rc, 2)
        fetched.assert_not_called()

    def test_a_weekly_slice_is_allowed(self):
        self.assertEqual(cli._preflight_budget(40, self._args(), dry_run=False), 0)

    def test_a_dry_run_is_never_blocked_because_it_spends_nothing(self):
        self.assertEqual(cli._preflight_budget(332, self._args(), dry_run=True), 0)

    def test_removing_the_ceiling_needs_the_amount_typed_out(self):
        """REGRESSION. One flag on one command line removed the only spend guard.

        `--max-usd 0` mapped to None, the pre-flight printed "no spend ceiling on
        this run" and returned 0, and submit() skipped its check — so
        `refresh --unsourced-only --max-usd 0` submitted the whole backlog with
        no confirmation anywhere in pipeline/. Removing the ceiling now costs a
        second flag carrying the number, and the number has to cover the run.
        """
        with mock.patch.object(cli.F, "fetch_many") as fetched:
            self.assertEqual(
                cli._preflight_budget(332, self._args(max_usd=0), dry_run=False), 2)
        fetched.assert_not_called()
        # Too small an acknowledgement is still a refusal.
        self.assertEqual(
            cli._preflight_budget(332, self._args(max_usd=0, i_accept_usd=10.0),
                                  dry_run=False), 2)
        # Naming a number that covers it goes through.
        self.assertEqual(
            cli._preflight_budget(332, self._args(max_usd=0, i_accept_usd=200.0),
                                  dry_run=False), 0)

    def test_a_negative_ceiling_is_rejected_not_read_as_no_ceiling(self):
        """REGRESSION. `--max-usd -1` looked tighter and removed the limit."""
        with self.assertRaises(argparse.ArgumentTypeError):
            cli._nonneg_usd("-1")
        self.assertEqual(cli._nonneg_usd("25"), 25.0)

    def test_the_default_ceiling_is_the_repo_s_existing_one(self):
        self.assertEqual(
            cli._max_usd(argparse.Namespace(max_usd=None)), C.MAX_BATCH_USD)
        self.assertIsNone(cli._max_usd(argparse.Namespace(max_usd=0)))

    def test_the_ceiling_is_per_batch_and_a_cycle_figure_exists(self):
        """A run that clears a $25 batch ceiling can still bill ~$50 a cycle."""
        per_batch = P.cards_within_budget(C.MAX_BATCH_USD)
        per_cycle = P.cards_within_budget(C.MAX_BATCH_USD, both_passes=True)
        self.assertLess(per_cycle, per_batch)
        self.assertLessEqual(P.forecast_usd(per_cycle), C.MAX_BATCH_USD)
        self.assertGreater(P.forecast_usd(per_batch), C.MAX_BATCH_USD)

    def test_the_suggested_limit_actually_fits_under_the_ceiling(self):
        room = P.cards_within_budget(C.MAX_BATCH_USD)
        self.assertLessEqual(P.forecast_usd(room, both_passes=False), C.MAX_BATCH_USD)
        self.assertGreater(P.forecast_usd(room + 1, both_passes=False), C.MAX_BATCH_USD)

    def test_the_ceiling_stops_a_sweep_and_clears_a_normal_week(self):
        self.assertGreater(P.forecast_usd(332, both_passes=False), C.MAX_BATCH_USD)
        self.assertLess(P.forecast_usd(40, both_passes=False), C.MAX_BATCH_USD)


class TestRefreshEndToEnd(unittest.TestCase):
    """cmd_refresh over a fake catalogue and a fake network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = pathlib.Path(self.tmp.name) / "sources.json"
        self.cards = [
            card("gap_a", rules=[{"rule_name": "r"}]),
            card("gap_b", rules=[{"rule_name": "r"}]),
            card("cited", rules=[{"rule_name": "r", "source_url": ISSUER_URL}]),
        ]

    def _args(self, **kw):
        base = dict(card_id="", limit=0, force=False, unsourced_only=False,
                    dry_run=True, max_usd=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _fetched(self, text="the document"):
        class F:
            pass

        f = F()
        f.ok, f.text, f.error = True, text, ""
        f.text_sha256 = ST.sha256_text(text)
        return f

    def _run(self, args, state=None):
        state = state if state is not None else {"schema_version": 1, "sources": {}}
        srcs = [src(c["card"]["id"]) for c in self.cards]
        # BATCH_STATE is redirected into the temp dir, not merely mocked at the
        # call site: `pipeline/state/batch.json` is TRACKED, and a test that
        # submits a fake batch would otherwise append 'msgbatch_fake' to the real
        # file — a committed lie that the next `advance` would try to poll.
        with mock.patch.object(cli.S, "load_cards", return_value=copy.deepcopy(self.cards)), \
             mock.patch.object(cli.S, "load_overrides", return_value={}), \
             mock.patch.object(cli.S, "resolve_sources", return_value=srcs), \
             mock.patch.object(cli.ST, "load_state", return_value=state), \
             mock.patch.object(cli.ST, "save_state") as saved, \
             mock.patch.object(C, "BATCH_STATE",
                               pathlib.Path(self.tmp.name) / "batch.json"), \
             mock.patch.object(cli.B, "submit", return_value="msgbatch_fake") as submitted, \
             mock.patch.object(cli.C, "WORK_DIR", pathlib.Path(self.tmp.name) / "work"), \
             mock.patch.object(cli.F, "fetch_many",
                               return_value={ISSUER_URL: self._fetched()}):
            rc = cli.cmd_refresh(args)
        return rc, state, saved, submitted

    def test_the_run_is_restricted_to_unsourced_cards(self):
        rc, _state, _saved, _submitted = self._run(self._args(unsourced_only=True))
        self.assertEqual(rc, 0)
        requests = json.loads(
            (pathlib.Path(self.tmp.name) / "work" / "extract_requests.json").read_text()
        )
        self.assertEqual(len(requests), 2)   # gap_a, gap_b — never `cited`

    def test_a_dry_run_submits_nothing_and_writes_no_state(self):
        rc, _state, saved, submitted = self._run(self._args(unsourced_only=True))
        self.assertEqual(rc, 0)
        submitted.assert_not_called()
        saved.assert_not_called()

    def test_a_dry_run_does_not_advance_the_rotation(self):
        state = {"schema_version": 1, "sources": {}}
        self._run(self._args(unsourced_only=True), state)
        self.assertEqual(ST.evidence_attempts(state, "gap_a"), (0, ""))

    def test_a_real_run_records_an_attempt_for_every_selected_card(self):
        state = {"schema_version": 1, "sources": {}}
        rc, state, saved, submitted = self._run(
            self._args(unsourced_only=True, dry_run=False), state)
        self.assertEqual(rc, 0)
        submitted.assert_called_once()
        saved.assert_called_once()
        self.assertEqual(ST.evidence_attempts(state, "gap_a")[0], 1)
        self.assertEqual(ST.evidence_attempts(state, "gap_b")[0], 1)
        self.assertEqual(ST.evidence_attempts(state, "cited"), (0, ""))

    def test_the_cost_is_printed_before_the_batch_is_submitted(self):
        """A number that arrives after the money is spent is not a guardrail."""
        printed: list[str] = []
        order: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k: printed.append(
                " ".join(str(x) for x in a))):
            with mock.patch.object(cli.B, "submit",
                                   side_effect=lambda *a, **k: (order.append("submit"),
                                                               "msgbatch_fake")[1]):
                rc, *_ = self._run(self._args(unsourced_only=True, dry_run=False))
        self.assertEqual(rc, 0)
        joined = "\n".join(printed)
        self.assertIn("to extract", joined)
        self.assertLess(joined.index("to extract"), joined.index("Fetching"))

    def test_an_ordinary_run_is_unchanged_and_reads_every_card(self):
        rc, _state, _saved, _submitted = self._run(self._args())
        self.assertEqual(rc, 0)
        requests = json.loads(
            (pathlib.Path(self.tmp.name) / "work" / "extract_requests.json").read_text()
        )
        self.assertEqual(len(requests), 3)

    def test_card_id_composes_with_the_flag(self):
        rc, _state, _saved, _submitted = self._run(
            self._args(unsourced_only=True, card_id="gap_a"))
        self.assertEqual(rc, 0)
        requests = json.loads(
            (pathlib.Path(self.tmp.name) / "work" / "extract_requests.json").read_text()
        )
        self.assertEqual(len(requests), 1)

    def test_naming_an_already_cited_card_reports_nothing_to_do(self):
        # Not an error: the operator asked a reasonable question and the honest
        # answer is "that card already has a source".
        rc, _state, _saved, submitted = self._run(
            self._args(unsourced_only=True, card_id="cited"))
        self.assertEqual(rc, 0)
        submitted.assert_not_called()

    def test_an_unsourced_but_unreachable_card_is_not_reported_as_done(self):
        """"Nothing to read" and "nothing left to fix" are different answers.

        A card that is unsourced AND has no resolvable issuer URL needs a human
        with a URL, not another pipeline run. Printing the same cheerful line for
        both would retire it from the backlog in the reader's head.
        """
        printed: list[str] = []
        srcs = [src("gap_a", url="", reason="no_issuer_landing: csb")]
        with mock.patch.object(cli.S, "load_cards", return_value=copy.deepcopy(self.cards)), \
             mock.patch.object(cli.S, "load_overrides", return_value={}), \
             mock.patch.object(cli.S, "resolve_sources", return_value=srcs), \
             mock.patch.object(cli.ST, "load_state",
                               return_value={"schema_version": 1, "sources": {}}), \
             mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(" ".join(map(str, a)))):
            rc = cli.cmd_refresh(self._args(unsourced_only=True))
        self.assertEqual(rc, 0)
        joined = "\n".join(printed)
        self.assertIn("no_issuer_landing: csb", joined)
        self.assertNotIn("already cites a document", joined)

    def test_limit_composes_with_the_flag(self):
        rc, _state, _saved, _submitted = self._run(
            self._args(unsourced_only=True, limit=1))
        self.assertEqual(rc, 0)
        requests = json.loads(
            (pathlib.Path(self.tmp.name) / "work" / "extract_requests.json").read_text()
        )
        self.assertEqual(len(requests), 1)


class TestEvidenceReport(unittest.TestCase):
    """The one command a non-engineer runs to see the gap and its price."""

    def setUp(self):
        self.cards = S.load_cards()
        self.srcs = _real_sources(self.cards)
        self.rep = P.evidence_report(self.cards, self.srcs)

    def test_it_reports_the_real_catalogue(self):
        self.assertEqual(self.rep["cards_active"], 370)
        self.assertEqual(
            self.rep["cards_sourced"] + self.rep["cards_unsourced"],
            self.rep["cards_active"],
        )
        self.assertEqual(
            self.rep["cards_unsourced_reachable"] + self.rep["cards_unsourced_no_url"],
            self.rep["cards_unsourced"],
        )

    def test_the_four_unreachable_cards_are_named(self):
        self.assertEqual(self.rep["cards_unsourced_no_url"], 4)
        self.assertEqual(len(self.rep["no_url_cards"]), 4)
        for row in self.rep["no_url_cards"]:
            self.assertTrue(row["reason"], f"{row['card_id']} has no stated reason")

    def test_the_issuer_breakdown_adds_up(self):
        self.assertEqual(sum(self.rep["by_issuer"].values()), self.rep["cards_unsourced"])

    def test_it_prices_the_backlog_and_a_weekly_slice(self):
        self.assertGreater(self.rep["cost_both_passes_usd"], self.rep["cost_extract_only_usd"])
        self.assertGreater(self.rep["weeks_to_clear"], 1)
        self.assertLess(self.rep["cost_per_week_usd"], self.rep["cost_both_passes_usd"])

    def test_the_rendered_report_names_the_cards_nothing_can_fix(self):
        text = P.render_evidence_report(self.rep)
        for row in self.rep["no_url_cards"]:
            self.assertIn(row["card_id"], text)

    def test_the_report_is_json_serialisable_for_a_job_step(self):
        json.dumps(self.rep)

    def test_the_command_exits_zero(self):
        args = argparse.Namespace(per_week=40, json=False, max_usd=None)
        with mock.patch("builtins.print"):
            self.assertEqual(cli.cmd_evidence(args), 0)

    def test_an_explicit_max_usd_zero_is_not_silently_replaced_by_the_default(self):
        """`--max-usd 0` means "no ceiling", exactly as it does for submit().

        The sentinel matters: if None were read as "unset" the report would print
        a $25 ceiling that is not in force, which is a guardrail lying about
        itself.
        """
        rep = P.evidence_report(self.cards, self.srcs, max_usd=None)
        self.assertIsNone(rep["max_batch_usd"])
        self.assertIn("switched off", P.render_evidence_report(rep))
        default = P.evidence_report(self.cards, self.srcs)
        self.assertEqual(default["max_batch_usd"], C.MAX_BATCH_USD)


class TestTheMetricsDefinitionChangeIsNotReportedAsALoss(unittest.TestCase):
    """Tightening a definition is not a regression, and must not read as one.

    `sourced_rules` drops 61 -> 27 the first time this ships. Left alone, the
    weekly PR body would lead with "Careful: 34 reward rules LOST the citation
    they had" — the most alarming sentence this report can produce, and false.
    """

    def _cur(self):
        from pipeline import report

        return report.compute_metrics(S.load_cards(), {"sources": {}})

    def test_an_old_history_row_loses_the_comparison_rather_than_faking_one(self):
        from pipeline import report

        old = {"sourced_rules": 61, "sourced_rules_pct": 4.77, "zero_rate_cards": 105}
        diffs = report.diff_metrics(old, self._cur())
        self.assertIsNone(diffs["sourced_rules"]["delta"])
        self.assertIsNone(diffs["sourced_rules"]["prev"])
        # A metric whose meaning did NOT change still compares normally.
        self.assertIsNotNone(diffs["zero_rate_cards"]["delta"])

    def test_the_headline_explains_it_instead_of_crying_regression(self):
        from pipeline import report

        old = {"sourced_rules": 61, "sourced_rules_pct": 4.77, "zero_rate_cards": 105}
        body = report.render_report(self._cur(), old)
        self.assertNotIn("LOST the citation", body)
        self.assertIn("not comparable", body)

    def test_two_rows_on_the_same_definition_compare_normally(self):
        from pipeline import report

        cur = self._cur()
        prev = dict(cur, sourced_rules=cur["sourced_rules"] - 3)
        diffs = report.diff_metrics(prev, cur)
        self.assertEqual(diffs["sourced_rules"]["delta"], 3)
        self.assertEqual(diffs["sourced_rules"]["direction"], "better")

    def test_the_version_is_stamped_into_the_row_so_next_week_can_tell(self):
        from pipeline import report

        self.assertEqual(self._cur()["metric_definition_version"],
                         report.METRIC_DEFINITION_VERSION)


class TestACompletedCycleClosesTheGate(unittest.TestCase):
    """REGRESSION, the expensive one: --unsourced-only permanently un-gating cards.

    Stage 1 writes status='fetched'. `ST.mark_done` sat BELOW a
    `if entry is None or not kept: continue`, so a card whose observations were
    all refuted or unverified was never marked done — and `has_changed` returns
    True for any status that is not done. From that week on the ORDINARY,
    unflagged, scheduled Monday refresh re-extracted that card every single week,
    forever, with no byte having moved. The repo's own measurement of the 17-Aug
    run is that 232 of 371 extractions were discarded, so most cards land here.
    At the measured rate the endpoint was ~$85 a week against a pipeline
    justified at Rs 180-530.
    """

    def _state(self):
        return {
            "schema_version": ST.SCHEMA_VERSION,
            "sources": {
                "gap_a": {"url": ISSUER_URL, "content_sha256": "abc",
                          "fetched_at": "2026-08-19T00:00:00Z", "status": "fetched"},
            },
        }

    def test_a_cycle_that_kept_nothing_still_marks_the_card_done(self):
        state = self._state()
        # What _advance_verify does for a card whose observations were all
        # refuted: no proposals, nothing applied — but the cycle finished.
        self.assertTrue(ST.mark_done(state, "gap_a"))
        self.assertEqual(state["sources"]["gap_a"]["status"], ST.STATUS_DONE)
        self.assertFalse(ST.has_changed(state, "gap_a", "abc"))

    def test_the_next_unflagged_refresh_sees_no_change(self):
        state = self._state()
        self.assertTrue(ST.has_changed(state, "gap_a", "abc"),
                        "before the cycle completes, the card is still due")
        ST.mark_done(state, "gap_a")
        self.assertFalse(ST.has_changed(state, "gap_a", "abc"),
                         "a completed cycle at unmoved bytes must close the gate")
        self.assertTrue(ST.has_changed(state, "gap_a", "different"),
                        "and must reopen the moment the page moves")

    def test_mark_done_is_reached_before_the_kept_guard_in_the_source(self):
        body = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        marked = body.index("ST.mark_done(sources_state, card_id)")
        guard = body.index("if entry is None or not kept:")
        self.assertLess(
            marked, guard,
            "mark_done must run for every completed cycle, not only when "
            "something survived verification")


class TestACompletedCycleClosesTheGateEndToEnd(unittest.TestCase):
    """The whole loop: catch-up run -> advance keeps nothing -> next refresh is quiet.

    This is the shape the defect actually had. Unit-testing mark_done proves the
    contract; this proves the pipeline honours it, because the bug was a `continue`
    placed one line too early and only a run through both stages catches that.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.cards = [card("gap_a", rules=[{"rule_name": "r"}])]
        self.state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}
        self.doc = "the document"
        self.sha = ST.sha256_text(self.doc)

    def _fetched(self):
        class F:
            pass
        f = F()
        f.ok, f.text, f.error, f.text_sha256 = True, self.doc, "", self.sha
        return f

    def _refresh(self, **kw):
        args = argparse.Namespace(card_id="", limit=0, force=False,
                                  unsourced_only=False, dry_run=False, max_usd=None)
        for k, v in kw.items():
            setattr(args, k, v)
        with mock.patch.object(cli.S, "load_cards",
                               return_value=copy.deepcopy(self.cards)), \
             mock.patch.object(cli.S, "load_overrides", return_value={}), \
             mock.patch.object(cli.S, "resolve_sources", return_value=[src("gap_a")]), \
             mock.patch.object(cli.ST, "load_state", return_value=self.state), \
             mock.patch.object(cli.ST, "save_state"), \
             mock.patch.object(C, "BATCH_STATE", self.dir / "batch.json"), \
             mock.patch.object(cli.C, "WORK_DIR", self.dir / "work"), \
             mock.patch.object(cli.B, "submit", return_value="msgbatch_x") as sub, \
             mock.patch.object(cli.F, "fetch_many",
                               return_value={ISSUER_URL: self._fetched()}):
            rc = cli.cmd_refresh(args)
        # What actually reached the model, not what a dry run would have written.
        submitted = sub.call_args[0][0] if sub.call_args else []
        return rc, submitted

    def _advance_with_nothing_surviving(self):
        """Stage 3 where every observation is refuted — the 62.5% case on 17-Aug."""
        extract_id = cli.B.build_extract_request(
            "gap_a", "x", "https://x.invalid", "x" * 200)["custom_id"]
        (self.dir / "extractions.json").write_text(json.dumps({
            extract_id: {"ok": True, "data": {"observations": [{"path": "p"}]}},
        }), encoding="utf-8")
        bst = {"schema_version": 1, "batches": [
            {"batch_id": "msgbatch_v", "kind": "verify", "submitted_at": "t",
             "request_count": 1, "status": "submitted"}]}
        (self.dir / "batch.json").write_text(json.dumps(bst), encoding="utf-8")

        verify_id = cli.B.build_verify_request("gap_a", "x" * 200, [])["custom_id"]
        verdicts = {verify_id: {"ok": True, "data": {"verdicts": [
            {"index": 0, "refuted": True, "quote_found_verbatim": False,
             "supports_value": False}]}}}
        args = argparse.Namespace(dry_run=False, max_usd=None, recollect="")
        with mock.patch.object(C, "BATCH_STATE", self.dir / "batch.json"), \
             mock.patch.object(cli.C, "EXTRACTIONS", self.dir / "extractions.json"), \
             mock.patch.object(cli.C, "WORK_DIR", self.dir / "work"), \
             mock.patch.object(cli.S, "load_cards",
                               return_value=copy.deepcopy(self.cards)), \
             mock.patch.object(cli.ST, "load_state", return_value=self.state), \
             mock.patch.object(cli.ST, "save_state"), \
             mock.patch.object(cli.B, "poll", return_value="ended"), \
             mock.patch.object(cli.B, "collect", return_value=verdicts):
            return cli.cmd_advance(args)

    def test_a_refuted_cycle_still_stops_next_week_re_extracting(self):
        rc, requests = self._refresh(unsourced_only=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.state["sources"]["gap_a"]["status"], "fetched")

        self.assertEqual(self._advance_with_nothing_surviving(), 0)
        self.assertEqual(
            self.state["sources"]["gap_a"]["status"], ST.STATUS_DONE,
            "nothing survived verification, but the CYCLE completed — the card "
            "must be marked done or the ordinary refresh re-extracts it forever")

        # Next Monday. No flags. Not one byte has moved. (The first batch has been
        # collected by now, so the in-flight guard is not what makes this quiet.)
        (self.dir / "batch.json").write_text(
            json.dumps({"schema_version": 1, "batches": []}), encoding="utf-8")
        rc, requests = self._refresh()
        self.assertEqual(rc, 0)
        self.assertEqual(requests, [],
                         "the unflagged weekly refresh must extract nothing")

    def test_and_the_catch_up_does_not_pick_it_up_again_either(self):
        self._refresh(unsourced_only=True)
        self._advance_with_nothing_surviving()
        plan = P.plan_catch_up([src("gap_a")], self.cards, self.state)
        self.assertEqual(plan.selected, [])
        self.assertEqual(plan.exhausted, 1)


class TestTheBacklogHasATerminationCondition(unittest.TestCase):
    """REGRESSION: 'unsourced' was used as a proxy for 'never read'.

    A card the pipeline fetched, extracted and marked done — but which yielded no
    citable number — was indistinguishable from a card nobody had ever touched.
    Of 67 cards marked done in the committed state, 31 still cite nothing, and
    every one of them would have been re-fetched and re-billed every cycle
    forever with no stop condition.
    """

    def setUp(self):
        self.cards = [card("read_and_empty", rules=[{"rule_name": "r"}]),
                      card("never_read", rules=[{"rule_name": "r"}])]
        self.srcs = [src("read_and_empty"), src("never_read", url=ISSUER_URL + "/2")]

    def _state(self, status):
        return {
            "schema_version": ST.SCHEMA_VERSION,
            "sources": {
                "read_and_empty": {"url": ISSUER_URL, "content_sha256": "abc",
                                   "fetched_at": "2026-08-19T00:00:00Z",
                                   "status": status},
            },
        }

    def test_a_card_read_to_completion_at_these_bytes_leaves_the_queue(self):
        plan = P.plan_catch_up(self.srcs, self.cards, self._state(ST.STATUS_DONE))
        self.assertEqual([s.card_id for s in plan.selected], ["never_read"])
        self.assertEqual(plan.exhausted, 1)
        self.assertEqual(plan.backlog, 1)

    def test_a_card_only_fetched_is_still_in_the_queue(self):
        plan = P.plan_catch_up(self.srcs, self.cards, self._state("fetched"))
        self.assertEqual(sorted(s.card_id for s in plan.selected),
                         ["never_read", "read_and_empty"])
        self.assertEqual(plan.exhausted, 0)

    def test_it_comes_back_when_the_page_moves(self):
        """The ordinary hash gate reopens it — no flag, no special case."""
        state = self._state(ST.STATUS_DONE)
        self.assertTrue(ST.has_changed(state, "read_and_empty", "moved"))

    def test_deferred_counts_down_as_the_queue_is_worked(self):
        """REGRESSION: the only progress line an operator sees was a constant.

        `deferred` was `whole_backlog - limit`, so it printed 292 in week 1, 292
        in week 9 when 12 unread cards remained, and 292 in week 13 when every
        card selected was a re-read.
        """
        cards = [card(f"c{i}", rules=[{"rule_name": "r"}]) for i in range(10)]
        srcs = [src(f"c{i}", url=f"{ISSUER_URL}/{i}") for i in range(10)]
        state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}

        seen = []
        for _week in range(4):
            plan = P.plan_catch_up(srcs, cards, state, limit=3)
            seen.append(plan.deferred)
            for chosen in plan.selected:
                # a completed cycle, whatever it produced
                ST.record_source(state, chosen.card_id, url=chosen.url,
                                 content_sha256="sha", fetched_at="t", status="fetched")
                ST.mark_done(state, chosen.card_id)
        self.assertEqual(seen, [7, 4, 1, 0])

    def test_a_re_read_is_counted_and_never_silent(self):
        cards = [card("c1", rules=[{"rule_name": "r"}])]
        srcs = [src("c1")]
        state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}
        self.assertEqual(P.plan_catch_up(srcs, cards, state).re_reads, 0)
        ST.note_evidence_attempt(state, "c1", "2026-08-19T00:00:00Z")
        self.assertEqual(P.plan_catch_up(srcs, cards, state).re_reads, 1)


class TestTheRunSaysWhatTheMoneyBuys(unittest.TestCase):
    """REGRESSION: the selection ignored what the hash gate would have done.

    The flag charged for 332 cards to gain gate-bypass on 31; the other 301 were
    already selected and paid for by the plain weekly refresh. Nothing anywhere
    warned that it was a duplicate.
    """

    def test_the_plan_splits_gate_blocked_from_already_due(self):
        cards = [card("blocked", rules=[{"rule_name": "r"}]),
                 card("pending", rules=[{"rule_name": "r"}])]
        srcs = [src("blocked"), src("pending", url=ISSUER_URL + "/2")]
        state = {
            "schema_version": ST.SCHEMA_VERSION,
            "sources": {
                # 'done' at a hash that has since moved -> the gate is what stops it
                "blocked": {"url": ISSUER_URL, "content_sha256": "abc",
                            "fetched_at": "t", "status": ST.STATUS_DONE},
                "pending": {"url": ISSUER_URL + "/2", "content_sha256": "abc",
                            "fetched_at": "t", "status": "fetched"},
            },
        }
        # 'blocked' has been read at these bytes, so it is exhausted, not queued.
        plan = P.plan_catch_up(srcs, cards, state)
        self.assertEqual([s.card_id for s in plan.selected], ["pending"])
        self.assertEqual(plan.also_due, 1)
        self.assertEqual(plan.gate_blocked, 0)
        self.assertEqual(plan.exhausted, 1)

    def test_the_split_is_printed_before_the_cost_line(self):
        printed: list[str] = []
        cards = [card("pending", rules=[{"rule_name": "r"}])]
        srcs = [src("pending")]
        state = {"schema_version": ST.SCHEMA_VERSION,
                 "sources": {"pending": {"url": ISSUER_URL, "content_sha256": "abc",
                                         "fetched_at": "t", "status": "fetched"}}}
        args = argparse.Namespace(card_id="", limit=0, force=False,
                                  unsourced_only=True, dry_run=True, max_usd=None)
        with mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(
                            " ".join(str(x) for x in a))), \
             mock.patch.object(cli.S, "load_cards", return_value=copy.deepcopy(cards)), \
             mock.patch.object(cli.S, "load_overrides", return_value={}), \
             mock.patch.object(cli.S, "resolve_sources", return_value=srcs), \
             mock.patch.object(cli.ST, "load_state", return_value=state), \
             mock.patch.object(cli.C, "WORK_DIR", pathlib.Path(tempfile.mkdtemp())), \
             mock.patch.object(cli.F, "fetch_many", return_value={}):
            cli.cmd_refresh(args)
        joined = "\n".join(printed)
        self.assertIn("would ALSO be fetched by this week's ordinary refresh", joined)
        self.assertLess(joined.index("would ALSO be fetched"),
                        joined.index("to extract"))

    def test_the_blanket_never_been_read_claim_is_gone(self):
        """It was false for 301 of 332 selected cards, on the line that sells the spend."""
        body = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("BECAUSE they have never been read", body)


class TestNothingSubmitsOverAPaidBatch(unittest.TestCase):
    """REGRESSION: no command path checked for an in-flight batch before submitting.

    cmd_refresh loaded batch.json only AFTER submitting, and stage 2 overwrote
    extractions.json wholesale — so a second extract batch collected in the same
    window deleted the first batch's results while the first batch's verification
    was already billed. ~$50 paid for and discarded, silently.
    """

    def _args(self, **kw):
        base = dict(dry_run=False, allow_concurrent_batch=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def _with_pending(self, tmp):
        path = pathlib.Path(tmp) / "batch.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "batches": [{"batch_id": "msgbatch_open", "kind": "extract",
                         "submitted_at": "t", "request_count": 155,
                         "status": "submitted"}],
        }), encoding="utf-8")
        return path

    def test_a_pending_batch_refuses_the_next_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(C, "BATCH_STATE", self._with_pending(tmp)):
                self.assertEqual(cli._in_flight_guard(self._args()), 2)

    def test_a_dry_run_is_never_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(C, "BATCH_STATE", self._with_pending(tmp)):
                self.assertEqual(cli._in_flight_guard(self._args(dry_run=True)), 0)

    def test_the_override_exists_and_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(C, "BATCH_STATE", self._with_pending(tmp)):
                self.assertEqual(
                    cli._in_flight_guard(self._args(allow_concurrent_batch=True)), 0)

    def test_nothing_pending_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "batch.json"
            with mock.patch.object(C, "BATCH_STATE", path):
                self.assertEqual(cli._in_flight_guard(self._args()), 0)

    def test_collecting_a_batch_merges_rather_than_overwrites(self):
        body = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("merged.update(results)", body)
        self.assertNotIn("C.EXTRACTIONS.write_text(\n        json.dumps(results,", body)


class TestASubmittedBatchSaysHowToCollectIt(unittest.TestCase):
    """REGRESSION: the documented local run had no step that commits the state.

    pipeline-advance.yml checks out `dev` and gates on the COMMITTED
    pipeline/state/batch.json. A local run writes it into the operator's working
    tree only; unless they remember to push, the collector prints "No batch state
    file." forever, the extraction is billed and never collected, and Anthropic
    drops the results at 29 days.
    """

    def test_the_git_commands_are_printed_at_submit_time(self):
        printed: list[str] = []
        with mock.patch("builtins.print",
                        side_effect=lambda *a, **k: printed.append(
                            " ".join(str(x) for x in a))):
            cli._say_push_the_state("msgbatch_x")
        joined = "\n".join(printed)
        self.assertIn("git add pipeline/state", joined)
        self.assertIn("git push origin HEAD:dev", joined)
        self.assertIn("29 days", joined)
        self.assertIn("msgbatch_x", joined)

    def test_the_doc_no_longer_leaves_it_to_memory(self):
        body = (REPO / "PIPELINE.md").read_text(encoding="utf-8")
        self.assertIn("git push origin HEAD:dev", body)


class TestTwoContradictoryFlagsAreAnError(unittest.TestCase):
    """REGRESSION: the --force warning stated a falsehood about money.

    It said --force "opens the gate for all 370 active cards, not just these" —
    but by the time it printed, the selection had already been narrowed, so the
    run was bit-for-bit the same cost. An operator was warned about a $94 sweep
    that was not happening; an operator who genuinely wanted the sweep typed both
    flags, believed the warning, and got 40 cards.
    """

    def test_force_with_unsourced_only_is_refused(self):
        args = argparse.Namespace(card_id="", limit=0, force=True,
                                  unsourced_only=True, dry_run=True, max_usd=None)
        with mock.patch.object(cli.S, "load_cards", return_value=[card("a")]), \
             mock.patch.object(cli.S, "load_overrides", return_value={}), \
             mock.patch.object(cli.S, "resolve_sources", return_value=[src("a")]), \
             mock.patch.object(cli.ST, "load_state", return_value={"sources": {}}), \
             mock.patch.object(cli.F, "fetch_many") as fetched:
            self.assertEqual(cli.cmd_refresh(args), 2)
        fetched.assert_not_called()

    def test_the_false_warning_is_gone(self):
        body = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("--force is redundant here", body)


class TestOneRunOneAttemptTimestamp(unittest.TestCase):
    """REGRESSION: hash-randomised set order plus a per-card clock read.

    `evidence_gap` is a set, so its iteration order is randomised per process,
    and `_now()` has one-second resolution — a run crossing a second boundary
    stamped a random subset of its cards a second later. `_attempt_key` tiers on
    (count, last_attempt_at), so that split one week's selection into two tiers
    at random and changed which cards later weeks picked.
    """

    def test_the_source_reads_the_clock_once_and_walks_in_order(self):
        body = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("attempted_at = _now()", body)
        self.assertIn("for card_id in sorted(evidence_gap):", body)
        self.assertNotIn("ST.note_evidence_attempt(st, card_id, _now())", body)

    def test_every_card_in_one_run_gets_the_same_timestamp(self):
        cards = [card(f"c{i}", rules=[{"rule_name": "r"}]) for i in range(6)]
        srcs = [src(f"c{i}", url=f"{ISSUER_URL}/{i}") for i in range(6)]
        state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}
        args = argparse.Namespace(card_id="", limit=0, force=False,
                                  unsourced_only=True, dry_run=False, max_usd=None)
        ticks = iter([f"2026-08-19T00:00:{n:02d}Z" for n in range(60)])
        with mock.patch.object(cli, "_now", side_effect=lambda: next(ticks)), \
             mock.patch.object(cli.S, "load_cards", return_value=copy.deepcopy(cards)), \
             mock.patch.object(cli.S, "load_overrides", return_value={}), \
             mock.patch.object(cli.S, "resolve_sources", return_value=srcs), \
             mock.patch.object(cli.ST, "load_state", return_value=state), \
             mock.patch.object(cli.ST, "save_state"), \
             mock.patch.object(cli.F, "fetch_many", return_value={}):
            cli.cmd_refresh(args)
        stamps = {ST.evidence_attempts(state, f"c{i}")[1] for i in range(6)}
        self.assertEqual(len(stamps), 1, f"one run must write one timestamp: {stamps}")


class TestACorruptSectionDoesNotBurnTheLedger(unittest.TestCase):
    """REGRESSION: load_state emptied the whole file when `sources` was wrong.

    `evidence_runs` is a record of money already spent. Discarding it resets the
    rotation to zero, so the same first N cards are re-selected and re-paid.
    """

    def test_a_broken_sources_section_keeps_the_evidence_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sources.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "sources": "not a dict",
                "evidence_runs": {"c1": {"attempts": 3,
                                         "last_attempt_at": "2026-08-19T00:00:00Z"}},
            }), encoding="utf-8")
            state = ST.load_state(path)
        self.assertEqual(state["sources"], {})
        self.assertEqual(ST.evidence_attempts(state, "c1"),
                         (3, "2026-08-19T00:00:00Z"))

    def test_an_unparseable_file_is_still_empty_and_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sources.json"
            path.write_text("{not json", encoding="utf-8")
            state = ST.load_state(path)
        self.assertEqual(state["sources"], {})

    def test_the_committed_file_round_trips_byte_identically(self):
        real = REPO / "pipeline" / "state" / "sources.json"
        before = real.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "sources.json"
            ST.save_state(ST.load_state(real), out)
            self.assertEqual(out.read_bytes(), before)

    def test_the_schema_version_moved_when_the_file_shape_did(self):
        self.assertGreaterEqual(ST.SCHEMA_VERSION, 2)


class TestTheReportSpeaksOnePopulationAndOneBank(unittest.TestCase):
    """The founder-facing screen, on the two things it used to mix."""

    def _rep(self):
        cards = S.load_cards()
        return P.evidence_report(cards, _real_sources(cards), ST.load_state())

    def test_both_rule_denominators_are_printed(self):
        """REGRESSION: card counts were active-only while rule counts were not.

        29 reward rules sit on the 13 switched-off cards. They were inside the
        percentage the report offered as the thing to move, and outside anything
        this pipeline can ever fetch.
        """
        rep = self._rep()
        self.assertGreater(rep["rules_total"], rep["rules_total_active"])
        self.assertEqual(rep["rules_unreachable_inactive"],
                         rep["rules_total"] - rep["rules_total_active"])
        body = P.render_evidence_report(rep)
        self.assertIn("on cards switched on in the app", body)

    def test_both_citation_counts_are_printed_and_labelled(self):
        rep = self._rep()
        body = P.render_evidence_report(rep)
        self.assertIn("citing a document we can open", body)
        self.assertIn("citing the ISSUER'S own document", body)
        self.assertLessEqual(rep["rules_issuer_cited"], rep["rules_cited"])

    def test_the_gap_table_groups_by_bank_not_by_spelling(self):
        """REGRESSION: 21 banks printed as 34 rows.

        BOBCARD's real backlog appeared as six rows of 10+4+2+1+1+1, AU's as
        three, IDFC's as three — so four of the ten largest gaps were invisible,
        and the report understated the backlog for exactly the issuers it flagged
        as problematic. The scheduler's round-robin groups by the slug; the
        report now uses the same key, with the raw spellings as a sub-line.
        """
        rep = self._rep()
        self.assertLess(len(rep["by_issuer"]), 25)
        self.assertIn("bobcard", rep["by_issuer"])
        self.assertGreaterEqual(rep["by_issuer"]["bobcard"], 15)
        body = P.render_evidence_report(rep)
        self.assertIn("spelt in the file as:", body)
        self.assertIn("BOBCARD (Bank of Baroda)", body)

    def test_the_weak_document_count_is_reported_before_the_price(self):
        """REGRESSION: half the forecast was going on shared landing pages.

        161 of the backlog resolved only to an issuer landing page — a category
        index shared by up to 27 cards — and neither the code, the report nor the
        docs mentioned that `discover` fixes it offline and for free.
        """
        rep = self._rep()
        self.assertGreater(rep["cards_on_shared_document"], 0)
        self.assertGreater(rep["cards_on_issuer_landing_page"], 0)
        body = P.render_evidence_report(rep)
        self.assertIn("read from a page shared with other cards", body)
        self.assertIn("discover --write", body)
        self.assertLess(body.index("read from a page shared"),
                        body.index("What clearing it costs"))

    def test_the_ceiling_is_labelled_per_batch_and_a_cycle_row_exists(self):
        body = P.render_evidence_report(self._rep())
        self.assertIn("most cards one BATCH may hold", body)
        self.assertIn("most cards one CYCLE may hold", body)

    def test_both_ends_of_the_cost_range_are_shown(self):
        rep = self._rep()
        self.assertGreater(rep["cost_both_passes_usd"],
                           rep["cost_both_passes_floor_usd"])
        body = P.render_evidence_report(rep)
        self.assertIn("if 17-Aug's yield repeats", body)


class TestTheOrderingPrefersACardsOwnPage(unittest.TestCase):
    """A page listing forty cards cannot answer "what does THIS card earn"."""

    def test_a_card_with_its_own_document_is_read_first(self):
        cards = [card(f"c{i}", rules=[{"rule_name": "r"}], issuer=f"Bank{i}")
                 for i in range(4)]
        shared = ISSUER_URL + "/all-cards"
        srcs = [src("c0", url=shared, issuer="b0"),
                src("c1", url=shared, issuer="b1"),
                src("c2", url=ISSUER_URL + "/own-2", issuer="b2"),
                src("c3", url=ISSUER_URL + "/own-3", issuer="b3")]
        state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}
        order = [s.card_id for s in P.catch_up_order(srcs, state)]
        self.assertEqual(order[:2], ["c2", "c3"])
        plan = P.plan_catch_up(srcs, cards, state, limit=2)
        self.assertEqual(plan.on_shared_doc, 0)

    def test_shared_documents_are_counted_not_silently_paid_for(self):
        srcs = [src("a", url="u1"), src("b", url="u1"), src("c", url="u2")]
        self.assertEqual(P.shared_document_counts(srcs), {"u1": 2})


class TestTestsDoNotWriteTrackedState(unittest.TestCase):
    """A test that submits a fake batch must not append it to the real state.

    pipeline/state/batch.json is tracked on purpose, and `advance` polls whatever
    it finds there. A stray 'msgbatch_fake' committed by a test run would make the
    next scheduled advance chase a batch that never existed.

    MEASURED AGAINST A SNAPSHOT, not against git HEAD. This used to shell out to
    `git diff --quiet`, which cannot tell "a test wrote this" from "a real run
    legitimately wrote this" — and pipeline/state/sources.json is written by every
    real `refresh`, which is the normal step immediately before running the suite.
    The suite went red and blamed the tests for a change the operator's own
    pipeline had made. A snapshot taken when the module loads measures what this
    run actually did, and is immune to a dirty working tree.
    """

    def test_running_this_module_leaves_tracked_state_clean(self):
        for tracked, before in _TRACKED_STATE_AT_IMPORT.items():
            with self.subTest(file=tracked):
                path = REPO / tracked
                after = path.read_bytes() if path.exists() else None
                self.assertEqual(
                    after, before,
                    f"{tracked} was modified by a test run — redirect the path with "
                    f"mock.patch.object(C, ...) instead of writing the real file",
                )


class TestNoHardDependency(unittest.TestCase):
    """The lazy-import contract, extended to the new module and its new caller."""

    def test_the_new_module_is_in_the_bare_python_list(self):
        from tests import run_all

        self.assertIn("pipeline.provenance", run_all.STDLIB_ONLY_MODULES)

    def test_importing_it_does_not_drag_in_the_sdk(self):
        import subprocess

        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r)\n"
             "import pipeline.provenance\n"
             "sys.path.insert(0, %r)\n"
             "from checks import c8_provenance\n"
             "print('LEAKED' if 'anthropic' in sys.modules else 'clean')"
             % (str(REPO), str(REPO / "tools"))],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
