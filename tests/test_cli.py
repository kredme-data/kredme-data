#!/usr/bin/env python3
"""
Tests for pipeline/cli.py — the integration layer.

Usage:
    python3 tests/test_cli.py

This file exists because cli.py shipped without tests and a CRITICAL slipped through:
stage 2 read the extracted documents from `.pipeline-work/`, which is gitignored scratch,
while stages 1 and 2 run as separate GitHub Actions jobs on separate runners. The file was
never there, so `advance` returned 1 every week — a pipeline that looks healthy and does
nothing, which is precisely how this repo's predecessor scanner failed 12 times in a row.

The first class below is the regression guard for that whole category.
"""
from __future__ import annotations

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
from pipeline import state as ST  # noqa: E402


class TestNoCrossStageScratchDependency(unittest.TestCase):
    """Nothing that must survive between CI stages may live in gitignored scratch."""

    def test_extractions_are_tracked_not_scratch(self):
        # Stage 2 writes this and stage 3 (a later cron run, different runner) reads it.
        self.assertTrue(
            str(C.EXTRACTIONS).startswith(str(C.STATE_DIR)),
            "extractions.json must live under pipeline/state/ so it is committed",
        )
        self.assertFalse(str(C.EXTRACTIONS).startswith(str(C.WORK_DIR)))

    def test_state_dir_is_not_gitignored(self):
        # Ask git, not the .gitignore text — the file has a comment mentioning
        # pipeline/state and a string match on that is testing the comment, not the rule.
        import subprocess

        def ignored(rel: str) -> bool:
            return subprocess.run(
                ["git", "check-ignore", "-q", rel], cwd=REPO, capture_output=True
            ).returncode == 0

        self.assertTrue(ignored(".pipeline-work/documents.json"), "scratch must be ignored")
        self.assertFalse(
            ignored("pipeline/state/extractions.json"),
            "state must be committed or the next CI stage cannot see it",
        )
        self.assertFalse(ignored("pipeline/state/sources.json"))
        self.assertFalse(ignored("pipeline/state/batch.json"))

    def test_cli_never_reads_documents_from_scratch(self):
        src = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn(
            'WORK_DIR / "documents.json"',
            src,
            "verification must re-fetch under a hash guard, not read a dead runner's scratch",
        )

    def test_scratch_is_only_used_within_a_single_job(self):
        # The only legitimate scratch users are the PR bodies, which are written and
        # consumed by the same job. Anything else crossing a stage boundary is the bug.
        src = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            if "_work_dir()" in line and "def " not in line:
                self.assertTrue(
                    any(k in line for k in ("pr_body.md", "news_pr_body.md", "extract_requests.json")),
                    f"unexpected scratch use that may cross a CI stage: {line.strip()}",
                )


class TestRedocumentHashGuard(unittest.TestCase):
    """Verification must judge quotes against the same bytes extraction read."""

    def setUp(self):
        self.state = {"schema_version": 1, "sources": {}}
        ST.record_source(
            self.state, "c1", url="https://www.hdfc.bank.in/x",
            content_sha256=ST.sha256_text("the document"), fetched_at="t", status="ok",
        )

    def _fetched(self, *, ok=True, text="the document", error=""):
        class F:
            pass

        f = F()
        f.ok = ok
        f.text = text
        f.text_sha256 = ST.sha256_text(text) if text else ""
        f.error = error
        return f

    def test_prefetched_map_is_used_and_no_fetch_happens(self):
        """The fix for the stage-2 timeout, pinned.

        _redocument fetching for itself is one serial request per card. On 371
        cards that ran past pipeline-advance's job timeout and the run was
        killed mid-loop, discarding 371 extractions already paid for and
        submitting nothing. cmd_advance now pre-fetches concurrently with
        fetch_many and passes the map in; if this ever fetches again the
        timeout comes back.
        """
        url = "https://www.hdfc.bank.in/x"
        with mock.patch.object(cli.F, "fetch_source") as spy:
            text, why = cli._redocument("c1", self.state, {url: self._fetched()})
        self.assertEqual(text, "the document")
        self.assertEqual(why, "")
        spy.assert_not_called()

    def test_falls_back_to_fetching_when_no_map_is_supplied(self):
        # Single-card debugging calls still work.
        with mock.patch.object(cli.F, "fetch_source", return_value=self._fetched()) as spy:
            text, _ = cli._redocument("c1", self.state)
        self.assertEqual(text, "the document")
        spy.assert_called_once()

    def test_a_url_missing_from_the_map_falls_back_rather_than_failing(self):
        # fetch_many drops nothing today, but a miss must not silently skip a
        # card — that would read downstream as "this card did not change".
        with mock.patch.object(cli.F, "fetch_source", return_value=self._fetched()) as spy:
            text, _ = cli._redocument("c1", self.state, {"https://other.test/y": None})
        self.assertEqual(text, "the document")
        spy.assert_called_once()

    def test_matching_hash_returns_the_text(self):
        with mock.patch.object(cli.F, "fetch_source", return_value=self._fetched()):
            text, why = cli._redocument("c1", self.state)
        self.assertEqual(text, "the document")
        self.assertEqual(why, "")

    def test_changed_source_is_deferred_not_verified(self):
        # The dangerous case: verifying a quote against a page that no longer contains it.
        with mock.patch.object(cli.F, "fetch_source", return_value=self._fetched(text="something else")):
            text, why = cli._redocument("c1", self.state)
        self.assertEqual(text, "")
        self.assertIn("changed", why)

    def test_failed_refetch_is_skipped(self):
        with mock.patch.object(
            cli.F, "fetch_source", return_value=self._fetched(ok=False, text="", error="HTTP 503")
        ):
            text, why = cli._redocument("c1", self.state)
        self.assertEqual(text, "")
        self.assertIn("503", why)

    def test_unknown_card_is_skipped(self):
        text, why = cli._redocument("never_seen", self.state)
        self.assertEqual(text, "")
        self.assertIn("url", why)

    def test_card_with_no_recorded_hash_is_skipped(self):
        ST.record_source(self.state, "c2", url="https://www.hdfc.bank.in/y",
                         content_sha256=None, fetched_at="t", status="fetch_failed")
        text, why = cli._redocument("c2", self.state)
        self.assertEqual(text, "")
        self.assertIn("hash", why)

    def test_does_not_fetch_when_there_is_nothing_to_compare(self):
        # No recorded hash means no way to prove sameness, so do not spend a fetch.
        called = []
        with mock.patch.object(cli.F, "fetch_source", side_effect=lambda *a, **k: called.append(1)):
            cli._redocument("never_seen", self.state)
        self.assertEqual(called, [])


class TestManifestRegeneration(unittest.TestCase):
    def test_regen_matches_real_bytes_and_bumps_patch(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            seed = pathlib.Path(tmp) / "seed"
            seed.mkdir()
            (seed / "cards.json").write_text('[{"card": {"id": "a"}}]', encoding="utf-8")
            (seed / "merchants.json").write_text('{"merchants": []}', encoding="utf-8")
            man = seed / "manifest.json"
            man.write_text(json.dumps({
                "version": "5.1.15",
                "files": [
                    {"name": "cards.json", "checksum": "stale", "size_bytes": 0},
                    {"name": "merchants.json", "checksum": "stale", "size_bytes": 0},
                ],
            }), encoding="utf-8")

            with mock.patch.object(C, "SEED_DIR", seed), mock.patch.object(C, "MANIFEST_JSON", man):
                cli._regen_manifest()

            out = json.loads(man.read_text(encoding="utf-8"))
            self.assertEqual(out["version"], "5.1.16")
            for f in out["files"]:
                raw = (seed / f["name"]).read_bytes()
                # A stale checksum is exactly what the app surfaces as "Sync failed".
                self.assertEqual(f["checksum"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(f["size_bytes"], len(raw))
            self.assertIn("updated_at", out)


class TestSeedFormattingPreserved(unittest.TestCase):
    """A one-field change must produce a one-field diff.

    seed/cards.json is 1.78 MB stored at indent=1. Writing it back at indent=2
    re-indents all 131,664 lines, and the PR — the only place a person sees what the
    model decided — becomes unreviewable.
    """

    def test_seed_files_round_trip_at_the_indent_the_writer_uses(self):
        for name, indent in (("cards.json", 1), ("manifest.json", 1)):
            with self.subTest(file=name):
                raw = (C.SEED_DIR / name).read_bytes()
                rebuilt = (
                    json.dumps(json.loads(raw), indent=indent, ensure_ascii=False).encode()
                    + b"\n"
                )
                self.assertEqual(raw, rebuilt, f"{name} is not indent={indent} on disk")

    def test_news_feed_is_indent_two(self):
        raw = C.NEWS_FEED.read_bytes()
        rebuilt = json.dumps(json.loads(raw), indent=2, ensure_ascii=False).encode() + b"\n"
        self.assertEqual(raw, rebuilt)

    def test_writer_uses_the_matching_indent(self):
        src = (REPO / "pipeline" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("json.dumps(new_cards, indent=1", src)
        self.assertIn("json.dumps(man, indent=1", src)


class TestCliEntrypoints(unittest.TestCase):
    """Bad input must exit with a code, never a traceback — a traceback in CI is opaque."""

    def _run(self, argv):
        with mock.patch.object(sys, "argv", ["cli.py", *argv]):
            try:
                return cli.main()
            except SystemExit as exc:
                return exc.code

    def test_unknown_card_id_exits_one(self):
        self.assertEqual(self._run(["refresh", "--card-id", "does_not_exist", "--dry-run"]), 1)

    def test_unknown_issuer_exits_one(self):
        self.assertEqual(self._run(["news-watch", "--issuer", "not_a_bank", "--dry-run"]), 1)

    def test_advance_with_nothing_pending_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = pathlib.Path(tmp) / "batch.json"
            with mock.patch.object(C, "BATCH_STATE", empty):
                self.assertEqual(self._run(["advance"]), 0)

    def test_advance_with_corrupt_batch_state_exits_zero(self):
        # A corrupt state file must not wedge the cron; it degrades to "nothing pending".
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "batch.json"
            bad.write_text("{ not json", encoding="utf-8")
            with mock.patch.object(C, "BATCH_STATE", bad):
                self.assertEqual(self._run(["advance"]), 0)

    def test_metrics_runs_on_the_real_catalogue(self):
        self.assertEqual(self._run(["metrics"]), 0)

    def test_no_subcommand_exits_nonzero(self):
        self.assertNotEqual(self._run([]), 0)


class TestEveryFetchingWorkflowInstallsPoppler(unittest.TestCase):
    """Any workflow that fetches an issuer source must be able to read a PDF.

    pipeline-advance.yml did not install poppler-utils, and the symptom pointed
    everywhere except the cause. fetch_source appends the text of every linked
    PDF, and verification requires the re-read to hash IDENTICALLY to what
    extraction read — so a missing pdftotext does not report a missing tool, it
    reports "source changed since extraction", i.e. it looks like the bank
    rewrote its page.

    On the 17-Aug run that had 371 paid-for extractions in hand: 205 cards
    "changed", 27 more failed outright, and the cycle proposed nothing. One
    apt-get line. This test is cheaper than finding it again.
    """

    WORKFLOWS = ("weekly-refresh.yml", "news-watch.yml", "pipeline-advance.yml")

    def test_all_of_them(self):
        wf_dir = REPO / ".github" / "workflows"
        for name in self.WORKFLOWS:
            path = wf_dir / name
            with self.subTest(workflow=name):
                self.assertTrue(path.exists(), f"{name} is missing")
                body = path.read_text(encoding="utf-8")
                self.assertIn(
                    "poppler-utils", body,
                    f"{name} runs a fetch but never installs pdftotext; linked-PDF "
                    f"text will vanish and hashes will silently stop matching",
                )


class TestWorkflowContract(unittest.TestCase):
    """The CI files reference CLI commands and paths — assert they still line up."""

    def _wf(self, name):
        return (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")

    def test_workflows_call_commands_that_exist(self):
        for name, cmd in (
            ("weekly-refresh.yml", "cli.py refresh"),
            ("pipeline-advance.yml", "cli.py advance"),
            ("news-watch.yml", "cli.py news-watch"),
        ):
            self.assertIn(cmd, self._wf(name))

    def test_pr_bodies_are_written_by_the_same_job_that_reads_them(self):
        # body-path points at scratch, which is fine ONLY because the write and the PR
        # step share a job. If either moves to another job this assertion should fail.
        self.assertIn("body-path: .pipeline-work/pr_body.md", self._wf("pipeline-advance.yml"))
        self.assertIn('_work_dir() / "pr_body.md"', (REPO / "pipeline" / "cli.py").read_text())

    def test_refresh_runs_the_test_suite_before_spending(self):
        self.assertIn("tests/run_all.py", self._wf("weekly-refresh.yml"))

    def test_every_workflow_targets_dev_never_main(self):
        for name in ("weekly-refresh.yml", "pipeline-advance.yml", "news-watch.yml"):
            body = self._wf(name)
            self.assertIn("ref: dev", body, name)
            self.assertNotIn("ref: main", body, name)
            self.assertNotIn("HEAD:main", body, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
