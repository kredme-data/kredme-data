#!/usr/bin/env python3
"""
Tests for fetch.fetch_many — concurrent fetching across issuers.

Usage:
    python3 tests/test_fetch_many.py            # run all
    python3 tests/test_fetch_many.py -v         # per-test names

The weekly refresh is built to submit a batch and exit quickly. When per-card
source discovery took the catalogue from 35 shared landing pages to 196
distinct URLs, the sequential fetch grew past an hour and `pipeline-advance` —
which runs every 2 hours in the same concurrency group — cancelled a refresh 60
minutes into its fetch. fetch_many is the fix.

The property under test is NOT "it is fast". It is that going faster did not
cost politeness: many hosts at once, never two requests to one host at once.
That is a structural guarantee here (one worker per host) rather than a lock,
so these tests pin the structure.

Nothing touches the network. Stdlib only — unittest, no pytest.
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest
import urllib.error

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import config as C  # noqa: E402
from pipeline import fetch as F  # noqa: E402

# The real 1.0s between same-host requests is correct in production and is 30
# seconds of dead wall-clock here. The tests assert the ORDERING guarantee — one
# request per host at a time — which the fake opener's own delay is enough to
# expose. Restored after the module so nothing else sees a rude fetcher.
_REAL_DELAY = C.POLITE_DELAY_S


def setUpModule() -> None:
    C.POLITE_DELAY_S = 0.0


def tearDownModule() -> None:
    C.POLITE_DELAY_S = _REAL_DELAY


# ------------------------------------------------------------- fixtures ----

class _Resp:
    def __init__(self, body: bytes = b"<html><body>hi</body></html>", status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self) -> None:
        pass


class HostRecorder:
    """A fake urlopen that proves how many requests a host sees at once."""

    def __init__(self, delay: float = 0.05, fail: set[str] | None = None):
        self.delay = delay
        self.fail = fail or set()
        self._lock = threading.Lock()
        self.active: dict[str, int] = {}       # host -> requests in flight
        self.max_active: dict[str, int] = {}   # host -> high-water mark
        self.max_hosts_at_once = 0
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        host = url.split("/")[2]
        with self._lock:
            self.calls.append(url)
            self.active[host] = self.active.get(host, 0) + 1
            self.max_active[host] = max(self.max_active.get(host, 0), self.active[host])
            live = sum(1 for v in self.active.values() if v > 0)
            self.max_hosts_at_once = max(self.max_hosts_at_once, live)
        try:
            time.sleep(self.delay)
            if url in self.fail:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return _Resp()
        finally:
            with self._lock:
                self.active[host] -= 1


def urls_for(host: str, n: int) -> list[str]:
    return [f"https://{host}/card-{i}" for i in range(n)]


# ------------------------------------------------------------ politeness ----

class TestPoliteness(unittest.TestCase):

    def test_one_host_never_sees_two_requests_at_once(self):
        """The guarantee that makes the speed-up acceptable.

        Concurrency is partitioned by host and each host gets exactly one
        worker, so this holds no matter how high max_workers goes — there is no
        lock that could be got wrong.
        """
        rec = HostRecorder()
        F.fetch_many(urls_for("a.test", 6), opener=rec, follow_pdfs=False, max_workers=8)
        self.assertEqual(rec.max_active["a.test"], 1)

    def test_raising_the_worker_count_adds_hosts_not_requests_per_host(self):
        rec = HostRecorder()
        many = urls_for("a.test", 4) + urls_for("b.test", 4) + urls_for("c.test", 4)
        F.fetch_many(many, opener=rec, follow_pdfs=False, max_workers=16)
        for host in ("a.test", "b.test", "c.test"):
            self.assertEqual(rec.max_active[host], 1, host)

    def test_different_hosts_do_run_at_the_same_time(self):
        """Without this the change would be politeness with no speed-up."""
        rec = HostRecorder(delay=0.15)
        many = urls_for("a.test", 2) + urls_for("b.test", 2) + urls_for("c.test", 2)
        F.fetch_many(many, opener=rec, follow_pdfs=False, max_workers=4)
        self.assertGreater(rec.max_hosts_at_once, 1)

    def test_a_single_worker_still_completes_everything(self):
        rec = HostRecorder()
        many = urls_for("a.test", 3) + urls_for("b.test", 3)
        got = F.fetch_many(many, opener=rec, follow_pdfs=False, max_workers=1)
        self.assertEqual(len(got), 6)


# ---------------------------------------------------------------- dedupe ----

class TestDedupe(unittest.TestCase):

    def test_a_shared_landing_page_is_fetched_once(self):
        """373 cards resolve to 196 URLs; the old loop paid for all 373."""
        rec = HostRecorder()
        shared = "https://a.test/credit-cards"
        got = F.fetch_many([shared] * 20, opener=rec, follow_pdfs=False)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(set(got), {shared})

    def test_every_requested_url_appears_in_the_result(self):
        rec = HostRecorder()
        many = urls_for("a.test", 3) + urls_for("b.test", 2)
        got = F.fetch_many(many, opener=rec, follow_pdfs=False)
        self.assertEqual(set(got), set(many))

    def test_empty_and_blank_input_is_not_an_error(self):
        self.assertEqual(F.fetch_many([], opener=HostRecorder()), {})
        self.assertEqual(F.fetch_many(["", ""], opener=HostRecorder()), {})


# ------------------------------------------------------------- failures ----

class TestFailuresAreData(unittest.TestCase):

    def test_one_dead_url_does_not_lose_the_others(self):
        rec = HostRecorder(fail={"https://a.test/card-1"})
        many = urls_for("a.test", 3)
        got = F.fetch_many(many, opener=rec, follow_pdfs=False)
        self.assertEqual(len(got), 3)
        self.assertFalse(got["https://a.test/card-1"].ok)
        self.assertTrue(got["https://a.test/card-0"].ok)

    def test_a_dead_host_does_not_stop_a_live_one(self):
        """The failure mode that killed the predecessor scanner around card 26."""
        rec = HostRecorder(fail=set(urls_for("dead.test", 3)))
        many = urls_for("dead.test", 3) + urls_for("live.test", 3)
        got = F.fetch_many(many, opener=rec, follow_pdfs=False)
        self.assertEqual(len(got), 6)
        self.assertTrue(all(got[u].ok for u in urls_for("live.test", 3)))

    def test_an_exception_escaping_a_worker_becomes_a_result_not_a_gap(self):
        """A URL missing from the map reads downstream as 'no cards changed'."""
        def boom(request, timeout=None):
            raise RuntimeError("something the fetcher did not anticipate")

        got = F.fetch_many(urls_for("a.test", 2), opener=boom, follow_pdfs=False)
        self.assertEqual(len(got), 2)
        for res in got.values():
            self.assertFalse(res.ok)
            self.assertTrue(res.error)


# ------------------------------------------------------------- progress ----

class TestProgress(unittest.TestCase):

    def test_progress_counts_every_url_exactly_once(self):
        seen: list[tuple[int, int]] = []
        many = urls_for("a.test", 3) + urls_for("b.test", 2)
        F.fetch_many(
            many, opener=HostRecorder(), follow_pdfs=False,
            on_progress=lambda done, total: seen.append((done, total)),
        )
        self.assertEqual(len(seen), 5)
        self.assertEqual([d for d, _ in seen], [1, 2, 3, 4, 5])
        self.assertTrue(all(t == 5 for _, t in seen))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
