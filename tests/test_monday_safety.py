#!/usr/bin/env python3
"""
The four things that have to hold before the Monday cron is allowed to run itself.

Usage:
    python3 tests/test_monday_safety.py

Each class below pins one of them:

  TestFinishingWithNothingIsFinishing   a card we read and got nothing from is DONE,
                                        so we stop paying for it every week
  TestAChangedSourceIsAlwaysReread      ...and none of that can strand a card whose
                                        bytes actually moved
  TestOnlyJudgedCardsAreRetired         a card nobody judged stays unfinished
  TestTheSpendCeiling                   the run refuses above the ceiling, loudly,
                                        on the SCHEDULED path, and exits non-zero
  TestAutoMergeTargetsDevOnly           the pipeline may merge itself into dev and
                                        can never reach main

The bug these exist for cost about $13 a week, every week, silently: stage 3 skipped
mark_done whenever a card produced no surviving observation, so "we read the bank's
page and nothing survived verification" was stored identically to "we have never
processed this card". 304 of 373 cards were sitting in that state.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import batch as B    # noqa: E402
from pipeline import cli           # noqa: E402
from pipeline import config as C   # noqa: E402
from pipeline import fetch as F    # noqa: E402
from pipeline import sources as S  # noqa: E402
from pipeline import state as ST   # noqa: E402

DOC = "y" * 400


# ---------------------------------------------------------------------------
# A stage-3 harness.
#
# Runs the REAL _advance_verify against a temp state file and fixture extractions,
# so what is asserted is the state the pipeline would actually commit — not a
# reimplementation of its branching in the test.
# ---------------------------------------------------------------------------
class StageThree(unittest.TestCase):
    CARDS = ["card_alpha", "card_beta"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.sources_path = root / "sources.json"
        self.extractions_path = root / "extractions.json"

        patches = [
            mock.patch.object(C, "SOURCE_STATE", self.sources_path),
            mock.patch.object(C, "EXTRACTIONS", self.extractions_path),
            # Proposal rendering is not what these tests are about; returning nothing
            # makes _advance_verify stop at "no proposals this week", which is well
            # after every mark_done decision has been made and saved.
            mock.patch.object(cli.D, "observations_to_proposals", return_value=[]),
            mock.patch.object(
                cli.S, "load_cards",
                return_value=[{"card": {"id": c, "name": c}} for c in self.CARDS],
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- fixture builders --------------------------------------------------
    def seed_sources(self, **card_to_status):
        state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}
        for card_id, status in card_to_status.items():
            ST.record_source(
                state, card_id,
                url=f"https://issuer.test/{card_id}",
                content_sha256=ST.sha256_text(card_id),
                fetched_at="2026-08-17T16:04:49Z",
                status=status,
            )
        ST.save_state(state, self.sources_path)

    def write_extractions(self, per_card):
        """per_card: {card_id: observations-list or None for a failed extraction}."""
        out = {}
        for card_id, obs in per_card.items():
            key = B.build_extract_request(card_id, "x", "https://x.invalid", "x" * 200)["custom_id"]
            if obs is None:
                out[key] = {"ok": False, "error": "overloaded"}
            else:
                out[key] = {"ok": True, "data": {"observations": obs}}
        self.extractions_path.write_text(json.dumps(out), encoding="utf-8")

    def verdicts_for(self, per_card):
        """per_card: {card_id: [bool, ...]} — one 'did it survive' flag per observation."""
        out = {}
        for card_id, flags in per_card.items():
            key = B.build_verify_request(card_id, DOC, [])["custom_id"]
            out[key] = {"ok": True, "data": {"verdicts": [
                {"index": i, "refuted": not keep,
                 "quote_found_verbatim": keep, "supports_value": keep}
                for i, keep in enumerate(flags)
            ]}}
        return out

    def run_stage_three(self, verdicts, *, request_count=None):
        pending = {"batch_id": "msgbatch_v",
                   "request_count": request_count if request_count is not None else len(verdicts)}
        bst = {"schema_version": 1, "batches": [
            {"batch_id": "msgbatch_v", "kind": "verify", "status": "submitted",
             "request_count": pending["request_count"], "submitted_at": "2026-08-17T18:32:28Z"},
        ]}
        args = argparse.Namespace(dry_run=True, max_usd=None, recollect="")
        buf = io.StringIO()
        with mock.patch.object(cli.B, "poll", return_value="ended"), \
                mock.patch.object(cli.B, "collect", return_value=verdicts), \
                mock.patch.object(cli.ST, "save_batch_state"), \
                redirect_stdout(buf):
            rc = cli._advance_verify(pending, bst, args)
        return rc, buf.getvalue(), ST.load_state(self.sources_path)


# ---------------------------------------------------------------------------
class TestFinishingWithNothingIsFinishing(StageThree):
    """THE BUG. A card we read and got nothing from is finished, not unstarted."""

    def test_a_card_whose_every_observation_was_refuted_is_marked_done(self):
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": [{"field": "annual_fee", "value": 500}]})
        rc, out, state = self.run_stage_three(self.verdicts_for({"card_alpha": [False]}))
        self.assertEqual(rc, 0, out)
        self.assertEqual(state["sources"]["card_alpha"]["status"], ST.STATUS_DONE,
                         "every observation refuted is a completed cycle, not a retry")

    def test_the_reason_says_all_refuted(self):
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": [{"field": "annual_fee", "value": 500}]})
        _, _, state = self.run_stage_three(self.verdicts_for({"card_alpha": [False]}))
        self.assertEqual(state["sources"]["card_alpha"]["done_reason"], ST.DONE_ALL_REFUTED)

    def test_a_card_the_extractor_found_nothing_in_is_marked_done(self):
        # 139 of the 304 stuck cards are this case: the bank publishes nothing we can
        # use. That is an ANSWER, and it was invisible because it was stored as "never
        # processed".
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": []})
        _, _, state = self.run_stage_three({})
        self.assertEqual(state["sources"]["card_alpha"]["status"], ST.STATUS_DONE)
        self.assertEqual(state["sources"]["card_alpha"]["done_reason"], ST.DONE_NO_OBSERVATIONS)

    def test_a_surviving_observation_is_recorded_as_verified(self):
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": [{"field": "annual_fee", "value": 500}]})
        _, _, state = self.run_stage_three(self.verdicts_for({"card_alpha": [True]}))
        self.assertEqual(state["sources"]["card_alpha"]["done_reason"], ST.DONE_VERIFIED)

    def test_partial_survival_still_counts_as_verified(self):
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": [{"field": "a", "value": 1},
                                               {"field": "b", "value": 2}]})
        _, _, state = self.run_stage_three(self.verdicts_for({"card_alpha": [False, True]}))
        self.assertEqual(state["sources"]["card_alpha"]["done_reason"], ST.DONE_VERIFIED)

    def test_a_card_that_left_the_catalogue_is_retired_not_re_billed(self):
        self.seed_sources(card_ghost="fetched")
        self.write_extractions({"card_ghost": [{"field": "annual_fee", "value": 500}]})
        _, _, state = self.run_stage_three(self.verdicts_for({"card_ghost": [True]}))
        self.assertEqual(state["sources"]["card_ghost"]["done_reason"], ST.DONE_CARD_GONE,
                         "a card no longer in seed/cards.json must not be re-read forever")

    def test_the_run_says_out_loud_how_many_finished_with_nothing(self):
        self.seed_sources(card_alpha="fetched", card_beta="fetched")
        self.write_extractions({"card_alpha": [], "card_beta": [{"field": "a", "value": 1}]})
        _, out, _ = self.run_stage_three(self.verdicts_for({"card_beta": [False]}))
        self.assertIn("finished with nothing to propose", out)


# ---------------------------------------------------------------------------
class TestAChangedSourceIsAlwaysReread(unittest.TestCase):
    """The non-negotiable. Nothing above may strand a card whose bytes moved.

    'done' is a statement about a specific set of bytes, never about the card. Every
    one of the finished reasons has to lose to a hash change.
    """

    def _state_with(self, reason):
        state = {"schema_version": ST.SCHEMA_VERSION, "sources": {}}
        ST.record_source(state, "c", url="https://issuer.test/c",
                         content_sha256=ST.sha256_text("old bytes"),
                         fetched_at="2026-08-17T16:04:49Z", status="fetched")
        ST.mark_done(state, "c", reason)
        return state

    def test_every_finished_reason_still_yields_to_a_hash_change(self):
        for reason in sorted(ST.DONE_REASONS):
            with self.subTest(reason=reason):
                state = self._state_with(reason)
                self.assertTrue(
                    ST.has_changed(state, "c", ST.sha256_text("new bytes")),
                    f"a card marked done/{reason} must still be re-read when its "
                    f"source bytes move",
                )

    def test_unchanged_bytes_on_a_finished_card_are_skipped(self):
        state = self._state_with(ST.DONE_ALL_REFUTED)
        self.assertFalse(ST.has_changed(state, "c", ST.sha256_text("old bytes")),
                         "this saving is the entire point of the repair")

    def test_a_card_we_have_never_seen_is_always_changed(self):
        self.assertTrue(ST.has_changed({"sources": {}}, "brand_new", "abc"))

    def test_the_weekly_unchanged_path_keeps_both_the_status_and_the_reason(self):
        # cmd_refresh re-records every unchanged card each week, and record_source
        # REPLACES the entry. Without carrying the reason across, one quiet week erases
        # the record of why the card finished.
        state = self._state_with(ST.DONE_NO_OBSERVATIONS)
        ST.record_source(state, "c", url="https://issuer.test/c",
                         content_sha256=ST.sha256_text("old bytes"),
                         fetched_at="2026-08-24T03:00:00Z",
                         status=ST.STATUS_DONE,
                         done_reason=ST.done_reason(state, "c"))
        self.assertEqual(state["sources"]["c"]["status"], ST.STATUS_DONE)
        self.assertEqual(state["sources"]["c"]["done_reason"], ST.DONE_NO_OBSERVATIONS)

    def test_cli_actually_carries_the_reason_on_that_path(self):
        src = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("done_reason=ST.done_reason(st, s.card_id)", src,
                      "the unchanged-card path must hand the old reason back to "
                      "record_source or it is erased every week")

    def test_an_unknown_reason_raises_rather_than_retiring_a_card(self):
        state = self._state_with(ST.DONE_VERIFIED)
        with self.assertRaises(ValueError):
            ST.mark_done(state, "c", "definitely_not_a_reason")


# ---------------------------------------------------------------------------
class TestOnlyJudgedCardsAreRetired(StageThree):
    """The other half of the fix: unfinished work must stay unfinished."""

    def test_a_card_whose_extraction_failed_is_not_marked_done(self):
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": None})
        _, _, state = self.run_stage_three({})
        self.assertEqual(state["sources"]["card_alpha"]["status"], "fetched",
                         "no document was ever read; that work is still owed")
        self.assertNotIn("done_reason", state["sources"]["card_alpha"])

    def test_a_card_the_adversary_never_judged_is_not_marked_done(self):
        # The 9-card case from the 2026-08-17 run: the page moved between stage 1 and
        # stage 2, so verification was deferred and no verdict exists. Zero survivors
        # here means "unchecked", not "refuted", and the two must not be conflated.
        self.seed_sources(card_alpha="fetched")
        self.write_extractions({"card_alpha": [{"field": "annual_fee", "value": 500}]})
        _, out, state = self.run_stage_three({}, request_count=0)
        self.assertEqual(state["sources"]["card_alpha"]["status"], "fetched")
        self.assertIn("never judged", out)

    def test_one_card_finishing_does_not_finish_its_neighbour(self):
        self.seed_sources(card_alpha="fetched", card_beta="fetched")
        self.write_extractions({"card_alpha": [{"field": "a", "value": 1}],
                                "card_beta": [{"field": "b", "value": 2}]})
        # Only alpha gets a verdict; beta was never judged.
        _, _, state = self.run_stage_three(self.verdicts_for({"card_alpha": [False]}),
                                           request_count=1)
        self.assertEqual(state["sources"]["card_alpha"]["status"], ST.STATUS_DONE)
        self.assertEqual(state["sources"]["card_beta"]["status"], "fetched")


# ---------------------------------------------------------------------------
class TestTheStateRepair(unittest.TestCase):
    """tools/repair_pipeline_state.py — conservative by construction."""

    def setUp(self):
        import importlib.util as ilu
        spec = ilu.spec_from_file_location(
            "repair_mod", REPO / "tools" / "repair_pipeline_state.py")
        self.R = ilu.module_from_spec(spec)
        spec.loader.exec_module(self.R)

    def _sources(self, **card_to_status):
        return {c: {"url": f"https://x.test/{c}", "content_sha256": "a" * 64,
                    "fetched_at": "2026-08-17T16:04:49Z", "status": s, "note": ""}
                for c, s in card_to_status.items()}

    def _extractions(self, **card_to_obs):
        return {B.build_extract_request(c, "x", "https://x.invalid", "x" * 200)["custom_id"]:
                {"ok": True, "data": {"observations": obs}}
                for c, obs in card_to_obs.items()}

    def test_zero_observations_is_repaired(self):
        groups = self.R.classify(self._sources(a="fetched"), self._extractions(a=[]))
        self.assertEqual(groups["repair_no_observations"], ["a"])

    def test_a_card_with_observations_is_left_alone(self):
        groups = self.R.classify(self._sources(a="fetched"),
                                 self._extractions(a=[{"field": "x", "value": 1}]))
        self.assertEqual(groups["repair_no_observations"], [])
        self.assertEqual(groups["leave_unproven"], ["a"],
                         "without committed verdicts there is no per-card proof the "
                         "adversary saw this card")

    def test_a_failed_extraction_is_left_alone(self):
        ex = self._extractions(a=[])
        for v in ex.values():
            v.update({"ok": False, "error": "overloaded"})
        groups = self.R.classify(self._sources(a="fetched"), ex)
        self.assertEqual(groups["repair_no_observations"], [])
        self.assertEqual(groups["extraction_failed"], ["a"])

    def test_a_fetch_failure_is_never_repaired(self):
        groups = self.R.classify(self._sources(a="fetch_failed"), self._extractions(a=[]))
        self.assertEqual(groups["repair_no_observations"], [])

    def test_it_never_touches_seed_or_news(self):
        src = (REPO / "tools" / "repair_pipeline_state.py").read_text(encoding="utf-8")
        for forbidden in ("CARDS_JSON", "MANIFEST_JSON", "seed/", "news/"):
            self.assertNotIn(forbidden, src.split("Stdlib only.")[-1],
                             f"the repair must not reach {forbidden}")


# ---------------------------------------------------------------------------
class TestTheSpendCeiling(unittest.TestCase):
    """Deliverable 3. $15, enforced on the run nobody is watching."""

    def _reqs(self, n, doc=133_000):
        return [B.build_extract_request(f"c{i}", "C", "https://x.test/a", "y" * doc)
                for i in range(n)]

    # -- the number, and where it lives ------------------------------------
    def test_the_ceiling_is_fifteen_dollars(self):
        self.assertEqual(C.MAX_BATCH_USD, 15.0)

    def test_the_number_appears_exactly_once_in_the_pipeline(self):
        # "One obvious place to change it" is only true if there is one place. A second
        # copy in a workflow file would be the one that silently disagreed.
        hits = []
        for path in sorted((REPO / "pipeline").glob("*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip().startswith("MAX_BATCH_USD ="):
                    hits.append(f"{path.name}:{i}")
        self.assertEqual(len(hits), 1,
                         f"MAX_BATCH_USD must be assigned in exactly one place; found {hits}")
        self.assertTrue(hits[0].startswith("config.py:"), hits)

    def test_no_workflow_hard_codes_a_competing_ceiling(self):
        for wf in sorted((REPO / ".github" / "workflows").glob("*.yml")):
            body = wf.read_text(encoding="utf-8")
            self.assertNotIn("--max-usd", body,
                             f"{wf.name} passes its own ceiling; the scheduled path must "
                             f"take the default from pipeline/config.py")

    # -- it refuses -------------------------------------------------------
    def test_submit_refuses_above_the_ceiling(self):
        with self.assertRaises(ValueError) as ctx:
            B.submit(self._reqs(371), dry_run=True)
        self.assertIn("refusing to submit", str(ctx.exception))

    def test_the_verdict_helper_refuses_and_never_offers_to_trim(self):
        est = B.estimate_cost(self._reqs(371), C.EXTRACT_MODEL)
        msg = cli._ceiling_verdict(est, 15.0, stage="extraction")
        self.assertIn("REFUSING to submit", msg)
        self.assertIn("Nothing has been spent", msg)
        self.assertNotIn("trim", msg.lower())

    def test_the_verdict_carries_both_the_estimate_and_the_bound(self):
        est = B.estimate_cost(self._reqs(371), C.EXTRACT_MODEL)
        msg = cli._ceiling_verdict(est, 15.0, stage="extraction")
        self.assertIn(f"${est['est_usd']:.2f}", msg)
        self.assertIn(f"${est['est_usd_ceiling']:.2f}", msg)
        self.assertIn("MAX_BATCH_USD", msg)

    def test_an_affordable_batch_produces_no_refusal(self):
        est = B.estimate_cost(self._reqs(10), C.EXTRACT_MODEL)
        self.assertEqual(cli._ceiling_verdict(est, 15.0, stage="extraction"), "")

    # -- on the scheduled path, and exiting non-zero -----------------------
    def _run_refresh(self, n_cards, argv_extras=None, env=None):
        """cmd_refresh with the network and the catalogue stubbed out."""
        srcs = [S.Source(card_id=f"c{i}", card_name=f"C{i}", issuer="test",
                         url=f"https://issuer.test/{i}", reason="") for i in range(n_cards)]
        fetched = {
            s.url: F.Fetched(url=s.url, ok=True, status=200, text="y" * 133_000,
                             text_sha256=ST.sha256_text(f"body-{s.url}"))
            for s in srcs
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_path = pathlib.Path(tmp.name) / "sources.json"
        batch_path = pathlib.Path(tmp.name) / "batch.json"

        # EXACTLY what weekly-refresh.yml passes on the SCHEDULED run: the workflow
        # builds ARGS from workflow_dispatch inputs, all of which are empty on a cron.
        # So no --max-usd, no --limit, no --dry-run — the ceiling has to come from the
        # default or it does not apply at all.
        args = cli.build_parser().parse_args(["refresh"] + (argv_extras or []))

        buf = io.StringIO()
        with mock.patch.object(C, "SOURCE_STATE", state_path), \
                mock.patch.object(C, "WORK_DIR", pathlib.Path(tmp.name) / "work"), \
                mock.patch.object(C, "BATCH_STATE", batch_path), \
                mock.patch.object(cli.S, "load_cards", return_value=[]), \
                mock.patch.object(cli.S, "resolve_sources", return_value=srcs), \
                mock.patch.object(cli.F, "fetch_many", return_value=fetched), \
                mock.patch.object(cli.B, "submit", return_value="msgbatch_test") as submit, \
                mock.patch.dict(os.environ, env or {}, clear=False), \
                redirect_stdout(buf):
            rc = cli.cmd_refresh(args)
        return rc, buf.getvalue(), submit

    def test_the_scheduled_path_refuses_and_exits_non_zero(self):
        rc, out, submit = self._run_refresh(371)
        self.assertNotEqual(rc, 0, "a ceiling breach that returns 0 sits in a green run")
        self.assertIn("SPEND CEILING REFUSED", out)
        submit.assert_not_called()

    def test_nothing_is_submitted_when_the_ceiling_trips(self):
        _, out, submit = self._run_refresh(371)
        self.assertEqual(submit.call_count, 0)
        self.assertIn("Nothing has been spent", out)

    def test_an_ordinary_week_still_submits(self):
        rc, out, submit = self._run_refresh(20)
        self.assertEqual(rc, 0, out)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(submit.call_args.kwargs["max_usd"], C.MAX_BATCH_USD,
                         "the scheduled run must carry the config ceiling into submit()")

    def test_the_refusal_is_written_to_the_job_summary(self):
        tmp = tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        self._run_refresh(371, env={"GITHUB_ACTIONS": "true",
                                    "GITHUB_STEP_SUMMARY": tmp.name})
        summary = pathlib.Path(tmp.name).read_text(encoding="utf-8")
        self.assertIn("Spend ceiling exceeded", summary)
        self.assertIn("MAX_BATCH_USD", summary)

    def test_the_refusal_raises_a_red_annotation_on_the_run(self):
        _, out, _ = self._run_refresh(371, env={"GITHUB_ACTIONS": "true"})
        self.assertIn("::error title=Spend ceiling exceeded", out)

    def test_a_dry_run_forecasts_the_refusal_instead_of_hiding_it(self):
        # A dry run is how a person finds out what Monday will do. Reporting a cost the
        # scheduled run will then refuse is exactly the surprise being removed.
        rc, out, submit = self._run_refresh(371, argv_extras=["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(submit.call_count, 0)
        self.assertIn("WOULD REFUSE", out)

    def test_a_manual_run_can_still_raise_the_ceiling_deliberately(self):
        rc, out, submit = self._run_refresh(371, argv_extras=["--max-usd", "500"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(submit.call_count, 1)

    def test_the_workflow_shows_the_ceiling_before_it_can_spend(self):
        wf = (REPO / ".github" / "workflows" / "weekly-refresh.yml").read_text(encoding="utf-8")
        self.assertIn("MAX_BATCH_USD", wf,
                      "the job summary must name the ceiling and where to change it")


# ---------------------------------------------------------------------------
class TestAutoMergeTargetsDevOnly(unittest.TestCase):
    """Deliverable 4. The pipeline may land on dev by itself. It may never reach main."""

    def _wf(self, name="pipeline-advance.yml"):
        return (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def _code(self, name="pipeline-advance.yml"):
        """The workflow with its comment lines removed.

        These files carry long comments explaining what NOT to do — "never `--admin`",
        "promoting is a separate act". Asserting against the raw text makes the
        explanation trip the rule it is explaining, so the rules read the code.
        """
        return "\n".join(line for line in self._wf(name).splitlines()
                          if not line.lstrip().startswith("#"))

    def test_auto_merge_is_armed(self):
        self.assertIn("gh pr merge", self._wf())
        self.assertIn("--auto", self._wf(),
                      "must use GitHub's own auto-merge so protection and checks still gate it")

    def test_it_is_never_a_force_merge(self):
        code = self._code()
        for forbidden in ("--admin", "--force"):
            self.assertNotIn(forbidden, code,
                             f"{forbidden!r} steps over the gates auto-merge exists to respect")

    def test_every_merge_call_is_the_queued_kind(self):
        # `gh pr merge` without --auto merges NOW, ignoring checks. Every call has to
        # be the queued form, not just one of them.
        calls = [ln for ln in self._code().splitlines() if "gh pr merge" in ln]
        self.assertTrue(calls, "no merge call found at all")
        for line in calls:
            self.assertIn("--auto", line, f"immediate merge: {line.strip()}")

    def test_the_pr_base_is_dev(self):
        self.assertIn("base: dev", self._code())
        self.assertNotIn("base: main", self._code())

    def test_the_base_is_re_checked_before_anything_merges(self):
        body = self._code()
        self.assertIn("baseRefName", body,
                      "read the base back from the API before merging — the cost of "
                      "being wrong here is card data on the branch the app reads")
        self.assertIn("Refusing to auto-merge", body)

    def test_no_workflow_pushes_or_merges_to_main(self):
        for name in ("weekly-refresh.yml", "pipeline-advance.yml", "news-watch.yml"):
            body = self._code(name)
            with self.subTest(workflow=name):
                self.assertNotIn("HEAD:main", body)
                self.assertNotIn("ref: main", body)
                self.assertNotIn("base: main", body)

    def test_a_failed_gate_leaves_the_pr_open(self):
        body = self._code()
        self.assertIn("steps.gate.outcome", body)
        self.assertIn("left open", body,
                      "a PR that fails the gate must stay open for a human, not merge")

    def test_the_gate_runs_the_same_checks_as_validate_yml(self):
        # The bot's PR gets no `on: pull_request` run at all, so these have to run here
        # or "CI gates it" is a claim about a PR with zero checks.
        gate = self._code()
        validate = self._code("validate.yml")
        for cmd in ("tools/test_pipeline.py", "tools/test_validate_cards.py",
                    "tools/test_fix_cards.py", "kredme.py validate --target working"):
            with self.subTest(command=cmd):
                self.assertIn(cmd, validate, "this test is pinned to validate.yml's list")
                self.assertIn(cmd, gate, f"the auto-merge gate must also run {cmd}")

    def test_the_gate_result_is_published_as_a_visible_check(self):
        body = self._code()
        self.assertIn("statuses/", body)
        self.assertIn("statuses: write", body)
        self.assertIn("pipeline gate", body)

    def test_nothing_in_the_pipeline_publishes_to_users(self):
        # Reaching users is `kredme.py promote`, and no scheduled workflow may call it.
        for name in ("weekly-refresh.yml", "pipeline-advance.yml", "news-watch.yml"):
            with self.subTest(workflow=name):
                self.assertNotIn("promote", self._code(name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
