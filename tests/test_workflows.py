#!/usr/bin/env python3
"""
Structural validity of every GitHub Actions workflow in this repo.

Usage:
    python3 tests/test_workflows.py

WHY THIS FILE EXISTS
--------------------
The other workflow tests read these files as RAW TEXT and assert that certain strings
appear in them. That catches "did somebody delete the base check" and structurally
cannot catch "is this file a valid workflow at all".

It missed a duplicate step id. `pipeline-advance.yml` carried `id: gate` twice — the
pre-existing "Is anything pending?" step and a newly added gate step. GitHub rejects
that at workflow-PARSE time ("The identifier 'gate' may not be used more than once
within the same scope"), so every one of the 84 scheduled runs a week would have
failed at startup: stages 2 and 3 never execute, Monday submits and pays for the
extraction batch, nothing ever collects it, and because cmd_refresh leaves changed
cards at status `fetched` they are all re-billed the following Monday.

The 738-test suite was green throughout. So this file parses the YAML instead of
grepping it, and asserts the properties a parse failure would otherwise reveal only in
production.

No third-party YAML parser is required — the loader below understands the subset these
workflows use, and falls back to PyYAML when it is installed.
"""
from __future__ import annotations

import collections
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

WORKFLOW_DIR = REPO / ".github" / "workflows"


def workflow_files() -> list[pathlib.Path]:
    return sorted(
        list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml"))
    )


def step_ids_by_job(path: pathlib.Path) -> dict[str, list[str]]:
    """{job name -> [step ids, in file order]}, without needing PyYAML.

    Workflows are indentation-structured, and a step id is always `id:` at the step's
    own key indentation under a `steps:` list. Rather than write a YAML parser, this
    walks jobs by their indentation and collects `id:` lines that sit inside a steps
    block — which is exactly the scope GitHub enforces uniqueness over.

    Cross-checked against PyYAML when it is available (test_matches_pyyaml below), so a
    bug in this reader cannot quietly weaken the assertion.
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    job: str | None = None
    in_steps = False
    job_indent = 0
    in_jobs = False

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if indent == 0:
            in_jobs = stripped.startswith("jobs:")
            job = None
            in_steps = False
            continue
        if not in_jobs:
            continue

        m = re.match(r"^([A-Za-z0-9_.-]+):\s*$", stripped)
        if m and (job is None or indent <= job_indent):
            job = m.group(1)
            job_indent = indent
            in_steps = False
            out.setdefault(job, [])
            continue

        if job is None:
            continue
        if re.match(r"^steps:\s*$", stripped):
            in_steps = True
            continue
        if in_steps:
            m = re.match(r"^-?\s*id:\s*(\S+)\s*$", stripped)
            if m:
                out[job].append(m.group(1).strip("'\""))
    return out


class TestEveryWorkflowParses(unittest.TestCase):
    def test_there_are_workflows_to_check(self):
        self.assertTrue(workflow_files(), "no workflow files found — wrong path?")

    def test_step_ids_are_unique_within_a_job(self):
        """The defect this file was written for.

        GitHub scopes step ids to the JOB. Two steps sharing one id is a parse error,
        and the run fails before any step executes — which on a cron-driven paid
        pipeline means the batch is submitted and never collected.
        """
        for path in workflow_files():
            for job, ids in step_ids_by_job(path).items():
                dupes = [k for k, n in collections.Counter(ids).items() if n > 1]
                with self.subTest(workflow=path.name, job=job):
                    self.assertEqual(
                        dupes, [],
                        f"{path.name} job '{job}' defines step id(s) {dupes} more than "
                        f"once. GitHub rejects the whole workflow at parse time, so "
                        f"every scheduled run fails at startup. Rename one of them.",
                    )

    def test_every_step_reference_names_a_step_that_exists(self):
        """`steps.<id>.outcome` on an id no step owns silently evaluates to empty.

        An empty `GATE` compares unequal to 'success', so a typo here fails CLOSED for
        the auto-merge check — but the same expression drives the published commit
        status, where an empty value would publish `failure` on a passing gate. Either
        way it is a lie about the gate, so it is asserted rather than trusted.
        """
        for path in workflow_files():
            text = path.read_text(encoding="utf-8")
            ids_here = {i for ids in step_ids_by_job(path).values() for i in ids}
            # Only look at expression contexts, not prose in comments.
            referenced = set()
            for line in text.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                referenced.update(re.findall(r"steps\.([A-Za-z0-9_-]+)\.", line))
            missing = sorted(referenced - ids_here)
            with self.subTest(workflow=path.name):
                self.assertEqual(
                    missing, [],
                    f"{path.name} references steps.{missing} but no step carries "
                    f"that id.",
                )

    def test_reader_matches_pyyaml_where_pyyaml_exists(self):
        """Belt and braces on the hand-rolled reader above."""
        try:
            import yaml  # noqa: PLC0415
        except ImportError:
            self.skipTest("PyYAML not installed — the hand-rolled reader stands alone")
        for path in workflow_files():
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            expected = {
                job: [s["id"] for s in (spec.get("steps") or []) if isinstance(s, dict)
                      and "id" in s]
                for job, spec in (doc.get("jobs") or {}).items()
            }
            with self.subTest(workflow=path.name):
                self.assertEqual(step_ids_by_job(path), expected)


class TestTheGateIsNotAdvisory(unittest.TestCase):
    """pipeline-advance's gate must run the REAL validator, not only the weak one."""

    def setUp(self):
        self.wf = (WORKFLOW_DIR / "pipeline-advance.yml").read_text(encoding="utf-8")

    def test_the_gate_step_runs_the_full_validator(self):
        gate = self.wf.split("name: Gate the proposed change", 1)[1]
        gate = gate.split("- name: Open PR", 1)[0]
        self.assertIn("tools/validate_cards.py", gate,
                      "the gate that arms auto-merge must run the nine-layer validator; "
                      "tools/kredme.py validate returns 0 on a tree it scores 554 errors")
        self.assertIn("validated_baseline.json", gate,
                      "the gate must compare against the recorded baseline (a ratchet), "
                      "not against zero")

    def test_the_gate_step_does_not_share_an_id_with_the_pending_check(self):
        ids = step_ids_by_job(WORKFLOW_DIR / "pipeline-advance.yml")["advance"]
        self.assertEqual(len(ids), len(set(ids)), f"duplicate step ids: {ids}")

    def test_state_is_committed_even_when_advance_fails(self):
        """A refusal that does not persist `collected` re-refuses every 2 hours forever."""
        block = self.wf.split("- name: Commit state", 1)[1].split("- name:", 1)[0]
        self.assertIn("always()", block,
                      "Commit state must run on failure too — it carries the handle to "
                      "money already spent")

    def test_the_state_push_is_not_swallowed(self):
        block = self.wf.split("- name: Commit state", 1)[1].split("- name:", 1)[0]
        self.assertNotIn("git push origin HEAD:dev || true", block,
                         "a failed push loses a paid-for batch handle; it must go red")


class TestNoWorkflowSwallowsAStatePush(unittest.TestCase):
    def test_no_state_push_ends_in_or_true(self):
        for path in workflow_files():
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn(
                    "git push origin HEAD:dev || true", text,
                    f"{path.name} swallows a failed push of pipeline state. What that "
                    f"pushes is the record of money already spent.",
                )


class TestBothWorkflowsCanRaiseTheCeilingTogether(unittest.TestCase):
    """The escape hatch has to exist on BOTH halves or it strands money."""

    def test_weekly_refresh_accepts_max_usd(self):
        wf = (WORKFLOW_DIR / "weekly-refresh.yml").read_text(encoding="utf-8")
        self.assertIn("max_usd:", wf, "no way to raise the ceiling from the UI")
        self.assertIn("--max-usd", wf, "the input is never appended to the CLI args")

    def test_pipeline_advance_accepts_max_usd(self):
        wf = (WORKFLOW_DIR / "pipeline-advance.yml").read_text(encoding="utf-8")
        self.assertIn("max_usd:", wf,
                      "stage 2 must be able to take the same override, or raising the "
                      "ceiling on stage 1 pays for extraction that is never verified")
        self.assertIn("--max-usd", wf)


class TestTheOtherBotLaneHasChecksToo(unittest.TestCase):
    def test_data_quality_publishes_a_gate_status(self):
        wf = (WORKFLOW_DIR / "data-quality.yml").read_text(encoding="utf-8")
        self.assertIn("statuses: write", wf)
        self.assertIn("context='pipeline gate'", wf,
                      "data-quality.yml opens a PR that rewrites seed/cards.json with "
                      "GITHUB_TOKEN, so on: pull_request never fires on it — it needs "
                      "the same self-published status pipeline-advance.yml uses")


if __name__ == "__main__":
    unittest.main(verbosity=2)
