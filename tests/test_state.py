#!/usr/bin/env python3
"""
Tests for pipeline/state.py — the content-hash bookkeeping the whole cost model rests on.

Usage:
    python3 tests/test_state.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import state as ST  # noqa: E402


class TestHashing(unittest.TestCase):
    def test_stable_and_distinct(self):
        self.assertEqual(ST.sha256_text("abc"), ST.sha256_text("abc"))
        self.assertNotEqual(ST.sha256_text("abc"), ST.sha256_text("abd"))
        self.assertEqual(len(ST.sha256_text("abc")), 64)

    def test_bytes_and_text_agree(self):
        self.assertEqual(ST.sha256_text("héllo"), ST.sha256_bytes("héllo".encode("utf-8")))

    def test_empty_is_hashable(self):
        self.assertEqual(len(ST.sha256_text("")), 64)


class TestSourceState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "sources.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty_not_fatal(self):
        st = ST.load_state(self.path)
        self.assertEqual(st["sources"], {})
        self.assertEqual(st["schema_version"], ST.SCHEMA_VERSION)

    def test_corrupt_file_degrades_to_empty(self):
        # A crashed weekly job is worse than a needless re-extract: the re-extract
        # costs money, the crash means nobody notices the catalogue rotting.
        self.path.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(ST.load_state(self.path)["sources"], {})

    def test_wrong_shape_degrades_to_empty(self):
        for junk in ('["a","list"]', '{"sources": "not a dict"}', '"a string"', "42"):
            self.path.write_text(junk, encoding="utf-8")
            self.assertEqual(ST.load_state(self.path)["sources"], {}, junk)

    def test_round_trip(self):
        st = ST.load_state(self.path)
        ST.record_source(st, "hdfc_bank_regalia_gold", url="https://www.hdfc.bank.in/x",
                         content_sha256="a" * 64, fetched_at="2026-08-13T00:00:00Z", status="ok")
        ST.save_state(st, self.path)
        again = ST.load_state(self.path)
        entry = ST.get_source(again, "hdfc_bank_regalia_gold")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["content_sha256"], "a" * 64)
        self.assertEqual(entry["status"], "ok")

    def test_saved_file_is_byte_stable(self):
        # Deterministic output keeps the committed diff readable — otherwise every
        # weekly commit churns key order and nobody reviews it.
        st = ST.load_state(self.path)
        for cid in ("z_card", "a_card", "m_card"):
            ST.record_source(st, cid, url="https://www.hdfc.bank.in/x", content_sha256="b" * 64,
                             fetched_at="2026-08-13T00:00:00Z", status="ok")
        ST.save_state(st, self.path)
        first = self.path.read_bytes()
        ST.save_state(ST.load_state(self.path), self.path)
        self.assertEqual(first, self.path.read_bytes())

    def test_get_source_unknown_card(self):
        self.assertIsNone(ST.get_source(ST.load_state(self.path), "nope"))

    def test_get_source_tolerates_non_dict_entry(self):
        self.path.write_text(json.dumps({"sources": {"x": "corrupt"}}), encoding="utf-8")
        self.assertIsNone(ST.get_source(ST.load_state(self.path), "x"))


class TestChangeDetection(unittest.TestCase):
    """This is the money logic: a false 'unchanged' silently skips a real revision,
    and a false 'changed' pays for a full re-extract."""

    def setUp(self):
        self.st = {"schema_version": 1, "sources": {}}

    def test_unseen_card_counts_as_changed(self):
        self.assertTrue(ST.has_changed(self.st, "new_card", "a" * 64))

    def test_same_hash_is_unchanged(self):
        ST.record_source(self.st, "c", url="u", content_sha256="a" * 64,
                         fetched_at="t", status="ok")
        self.assertFalse(ST.has_changed(self.st, "c", "a" * 64))

    def test_different_hash_is_changed(self):
        ST.record_source(self.st, "c", url="u", content_sha256="a" * 64,
                         fetched_at="t", status="ok")
        self.assertTrue(ST.has_changed(self.st, "c", "b" * 64))

    def test_null_stored_hash_counts_as_changed(self):
        # A card whose last fetch failed has no hash. It must re-extract, not be
        # silently skipped forever.
        ST.record_source(self.st, "c", url="u", content_sha256=None,
                         fetched_at="t", status="fetch_failed")
        self.assertTrue(ST.has_changed(self.st, "c", "a" * 64))

    def test_empty_stored_hash_counts_as_changed(self):
        ST.record_source(self.st, "c", url="u", content_sha256="",
                         fetched_at="t", status="ok")
        self.assertTrue(ST.has_changed(self.st, "c", "a" * 64))


class TestBatchState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "batch.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_and_corrupt_degrade(self):
        self.assertEqual(ST.load_batch_state(self.path)["batches"], [])
        self.path.write_text("garbage", encoding="utf-8")
        self.assertEqual(ST.load_batch_state(self.path)["batches"], [])
        self.path.write_text('{"batches": {}}', encoding="utf-8")
        self.assertEqual(ST.load_batch_state(self.path)["batches"], [])

    def test_add_and_pending(self):
        st = ST.load_batch_state(self.path)
        ST.add_batch(st, batch_id="b1", kind="extract", submitted_at="t", count=12)
        ST.add_batch(st, batch_id="b2", kind="verify", submitted_at="t", count=5)
        self.assertEqual(len(ST.pending_batches(st)), 2)
        self.assertEqual(len(ST.pending_batches(st, kind="extract")), 1)
        self.assertEqual(ST.pending_batches(st, kind="extract")[0]["batch_id"], "b1")

    def test_mark_removes_from_pending(self):
        st = ST.load_batch_state(self.path)
        ST.add_batch(st, batch_id="b1", kind="extract", submitted_at="t", count=1)
        self.assertTrue(ST.mark_batch(st, "b1", "collected"))
        self.assertEqual(ST.pending_batches(st), [])

    def test_mark_unknown_batch_returns_false(self):
        st = ST.load_batch_state(self.path)
        self.assertFalse(ST.mark_batch(st, "nope", "collected"))

    def test_survives_a_round_trip(self):
        # The batch id MUST survive between GitHub Actions runs — that is the entire
        # reason the weekly sweep can outlive Actions' 6-hour job limit.
        st = ST.load_batch_state(self.path)
        ST.add_batch(st, batch_id="msgbatch_01X", kind="extract", submitted_at="t", count=40)
        ST.save_batch_state(st, self.path)
        again = ST.load_batch_state(self.path)
        self.assertEqual(ST.pending_batches(again)[0]["batch_id"], "msgbatch_01X")
        self.assertEqual(ST.pending_batches(again)[0]["request_count"], 40)


class TestMetricsHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "metrics.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_history(self):
        self.assertEqual(ST.read_metrics(self.path), [])

    def test_append_and_read_in_order(self):
        ST.append_metrics({"run_at": "1", "sourced_rules": 54}, self.path)
        ST.append_metrics({"run_at": "2", "sourced_rules": 61}, self.path)
        rows = ST.read_metrics(self.path)
        self.assertEqual([r["sourced_rules"] for r in rows], [54, 61])

    def test_one_bad_line_does_not_lose_the_history(self):
        ST.append_metrics({"run_at": "1", "sourced_rules": 54}, self.path)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("{corrupt\n")
        ST.append_metrics({"run_at": "3", "sourced_rules": 70}, self.path)
        rows = ST.read_metrics(self.path)
        self.assertEqual([r["sourced_rules"] for r in rows], [54, 70])

    def test_blank_lines_ignored(self):
        self.path.write_text('\n\n{"a":1}\n\n', encoding="utf-8")
        self.assertEqual(ST.read_metrics(self.path), [{"a": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
