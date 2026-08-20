#!/usr/bin/env python3
"""
Tests for pipeline/fetch.py.

Usage:
    python3 tests/test_fetch.py
    python3 tests/test_fetch.py -v

Stdlib unittest only — no pytest, no pip installs, and no network. Every
response the module sees comes from a fake opener defined in this file, and
pdftotext is faked too so the suite passes on a machine without poppler.
"""
from __future__ import annotations

import gzip
import io
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config as C          # noqa: E402
from pipeline import fetch as F           # noqa: E402
from pipeline import state as S           # noqa: E402


# ---------------------------------------------------------------------------
# Fakes. Nothing below opens a socket.
# ---------------------------------------------------------------------------
class FakeResponse:
    """The shape fetch.py needs from urlopen: read(n), headers.get, close()."""

    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8"} if headers is None else headers
        self.reads: list[int] = []
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        self.reads.append(n)
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self) -> None:
        self.closed = True


def opener_for(*items):
    """A fake urlopen that yields `items` in order; the last one repeats.

    An item may be a FakeResponse (returned) or an exception (raised), which is
    how a persistently failing server is modelled.
    """
    calls: list[tuple[str, object]] = []

    def _open(request, timeout=None):
        calls.append((request.full_url, timeout))
        item = items[min(len(calls) - 1, len(items) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    _open.calls = calls
    return _open


def router(mapping: dict, default=None):
    """A fake urlopen that answers per-URL. Unmapped URLs 404."""
    calls: list[str] = []

    def _open(request, timeout=None):
        url = request.full_url
        calls.append(url)
        item = mapping.get(url, default)
        if item is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))
        if isinstance(item, BaseException):
            raise item
        return item

    _open.calls = calls
    return _open


def html_response(body: str, headers: dict | None = None) -> FakeResponse:
    return FakeResponse(body.encode("utf-8"), 200, headers)


def completed(stdout: bytes, returncode: int = 0):
    return subprocess.CompletedProcess(args=["pdftotext"], returncode=returncode, stdout=stdout, stderr=b"")


PAGE = """<!doctype html>
<html><head><title>Regalia Gold</title><style>.a{color:red}</style></head>
<body>
  <nav><a href="/other-card">Other Card</a></nav>
  <h1>HDFC Bank Regalia Gold Credit Card</h1>
  <p>Earn 4 Reward Points per Rs 150 spent.</p>
  <script>var tracking = {"rate": 99};</script>
  <table><tr><td>Annual fee</td><td>Rs 2,500</td></tr></table>
  <footer><a href="docs/mitc.pdf">MITC</a></footer>
</body></html>"""


class SleeplessTest(unittest.TestCase):
    """Backoff and the polite delay are real sleeps; the suite must not serve
    them. Patching also makes the sleep calls assertable."""

    def setUp(self) -> None:
        patcher = mock.patch("pipeline.fetch.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# normalise_text — the hash input
# ---------------------------------------------------------------------------
class NormaliseTextTest(unittest.TestCase):

    def test_whitespace_variants_hash_identically(self):
        """The whole point: a reflowed page must not read as a repriced card."""
        a = "Earn 4 Reward Points per Rs 150 spent."
        b = "Earn 4 Reward Points\n\n  per   Rs 150\tspent."
        c = "  Earn 4 Reward Points per Rs 150 spent.  \r\n"
        self.assertEqual(F.normalise_text(a), F.normalise_text(b))
        self.assertEqual(F.normalise_text(a), F.normalise_text(c))
        self.assertEqual(S.sha256_text(F.normalise_text(a)), S.sha256_text(F.normalise_text(b)))
        self.assertEqual(S.sha256_text(F.normalise_text(a)), S.sha256_text(F.normalise_text(c)))

    def test_non_breaking_space_is_whitespace(self):
        self.assertEqual(F.normalise_text("Rs 150"), "Rs 150")

    def test_zero_width_characters_dropped(self):
        self.assertEqual(F.normalise_text("Rs​1﻿50­"), "Rs150")

    def test_a_real_change_still_changes_the_hash(self):
        """Insensitive to whitespace, not to numbers."""
        self.assertNotEqual(
            S.sha256_text(F.normalise_text("4 Reward Points per Rs 150")),
            S.sha256_text(F.normalise_text("2 Reward Points per Rs 150")),
        )

    def test_block_size_survives_normalisation(self):
        """Trap 3: 'N points per Rs X' must reach the model intact. Nothing here
        may collapse it toward a percentage."""
        raw = "Earn  4   Reward   Points  per  Rs. 150  spent"
        out = F.normalise_text(raw)
        self.assertEqual(out, "Earn 4 Reward Points per Rs. 150 spent")
        self.assertIn("150", out)
        self.assertNotIn("%", out)

    def test_empty_and_wrong_type(self):
        self.assertEqual(F.normalise_text(""), "")
        self.assertEqual(F.normalise_text("   \n\t "), "")
        self.assertEqual(F.normalise_text(None), "")
        self.assertEqual(F.normalise_text(b"bytes"), "")
        self.assertEqual(F.normalise_text(150), "")


# ---------------------------------------------------------------------------
# extract_text — HTML
# ---------------------------------------------------------------------------
class ExtractHtmlTest(unittest.TestCase):

    def test_script_style_nav_and_footer_are_stripped(self):
        text, _ = F.extract_text(PAGE.encode("utf-8"), "text/html", "https://www.hdfc.bank.in/x")
        self.assertIn("HDFC Bank Regalia Gold Credit Card", text)
        self.assertIn("Earn 4 Reward Points per Rs 150 spent.", text)
        self.assertNotIn("var tracking", text)
        self.assertNotIn('"rate": 99', text)      # the JSON a regex stripper would leak
        self.assertNotIn("color:red", text)
        self.assertNotIn("Other Card", text)
        self.assertNotIn("MITC", text)

    def test_table_cells_do_not_run_together(self):
        text, _ = F.extract_text(PAGE.encode("utf-8"), "text/html", "https://x.example/")
        self.assertNotIn("Annual feeRs 2,500", text)
        self.assertIn("Annual fee", text)
        self.assertIn("Rs 2,500", text)

    def test_entities_are_decoded(self):
        raw = b"<html><body><p>Fees &amp; Charges&nbsp;Rs&nbsp;500</p></body></html>"
        text, _ = F.extract_text(raw, "text/html", "https://x.example/")
        self.assertIn("Fees & Charges", text)
        self.assertIn("Rs 500", F.normalise_text(text))

    def test_html_detected_without_content_type(self):
        raw = b"<html><body><p>Reward rate 1%</p></body></html>"
        text, _ = F.extract_text(raw, "", "https://x.example/")
        self.assertIn("Reward rate 1%", text)

    def test_malformed_html_does_not_raise(self):
        raw = b"<html><body><p>unclosed <div><span>Rs 500</p></nav></body>"
        text, pdfs = F.extract_text(raw, "text/html", "https://x.example/")
        self.assertIn("Rs 500", text)
        self.assertEqual(pdfs, [])

    def test_stray_close_tag_does_not_hide_the_page(self):
        raw = b"<html><body></nav><p>Annual fee Rs 500</p></body></html>"
        text, _ = F.extract_text(raw, "text/html", "https://x.example/")
        self.assertIn("Annual fee Rs 500", text)

    def test_empty_and_wrong_type_input(self):
        self.assertEqual(F.extract_text(b"", "text/html", "https://x.example/"), ("", []))
        self.assertEqual(F.extract_text(None, "text/html", "https://x.example/"), ("", []))
        self.assertEqual(F.extract_text("<html>", "text/html", "https://x.example/"), ("", []))
        self.assertEqual(F.extract_text(b"<html><body>hi</body></html>", None, None)[0], "hi")

    def test_latin1_charset_is_honoured(self):
        raw = "<html><body><p>café fee</p></body></html>".encode("latin-1")
        text, _ = F.extract_text(raw, "text/html; charset=iso-8859-1", "https://x.example/")
        self.assertIn("café fee", text)

    def test_undecodable_bytes_do_not_raise(self):
        text, _ = F.extract_text(b"<html><body>\xff\xfe fee</body></html>", "text/html", "u")
        self.assertIn("fee", text)

    def test_plain_text_passthrough(self):
        text, pdfs = F.extract_text(b"Annual fee Rs 500", "text/plain", "https://x.example/")
        self.assertEqual(text, "Annual fee Rs 500")
        self.assertEqual(pdfs, [])


# ---------------------------------------------------------------------------
# extract_text — linked PDFs
# ---------------------------------------------------------------------------
class LinkedPdfTest(unittest.TestCase):

    def test_relative_hrefs_are_resolved_and_footer_links_kept(self):
        _, pdfs = F.extract_text(PAGE.encode("utf-8"), "text/html",
                                 "https://www.hdfc.bank.in/personal/cards/index.html")
        self.assertEqual(pdfs, ["https://www.hdfc.bank.in/personal/cards/docs/mitc.pdf"])

    def test_absolute_root_relative_and_uppercase(self):
        raw = (b'<html><body>'
               b'<a href="/a/fees.PDF">a</a>'
               b'<a href="https://cdn.example/b.pdf">b</a>'
               b'<a href="../c.pdf?v=7">c</a>'
               b'</body></html>')
        _, pdfs = F.extract_text(raw, "text/html", "https://issuer.example/cards/gold/page.html")
        self.assertEqual(pdfs, [
            "https://issuer.example/a/fees.PDF",
            "https://cdn.example/b.pdf",
            "https://issuer.example/cards/c.pdf?v=7",
        ])

    def test_non_pdf_and_non_http_hrefs_ignored(self):
        raw = (b'<html><body>'
               b'<a href="/terms.html">t</a>'
               b'<a href="mailto:care@issuer.example">m</a>'
               b'<a href="javascript:void(0)">j</a>'
               b'<a href="/pdf/viewer">v</a>'
               b'<a href="">e</a>'
               b'</body></html>')
        _, pdfs = F.extract_text(raw, "text/html", "https://issuer.example/")
        self.assertEqual(pdfs, [])

    def test_fragments_deduped_and_order_preserved(self):
        raw = (b'<html><body>'
               b'<a href="/b.pdf">b</a>'
               b'<a href="/a.pdf#page=2">a2</a>'
               b'<a href="/a.pdf">a</a>'
               b'<a href="/b.pdf">b again</a>'
               b'</body></html>')
        _, pdfs = F.extract_text(raw, "text/html", "https://issuer.example/")
        self.assertEqual(pdfs, ["https://issuer.example/b.pdf", "https://issuer.example/a.pdf"])

    def test_capped_at_config_max(self):
        links = "".join(f'<a href="/d{i}.pdf">x</a>' for i in range(C.MAX_LINKED_PDFS + 3))
        raw = f"<html><body>{links}</body></html>".encode("utf-8")
        _, pdfs = F.extract_text(raw, "text/html", "https://issuer.example/")
        self.assertEqual(len(pdfs), C.MAX_LINKED_PDFS)
        self.assertEqual(pdfs[0], "https://issuer.example/d0.pdf")

    def test_cap_boundary_exact_and_over(self):
        with mock.patch.object(C, "MAX_LINKED_PDFS", 2):
            two = b'<html><body><a href="/1.pdf">1</a><a href="/2.pdf">2</a></body></html>'
            _, pdfs = F.extract_text(two, "text/html", "https://issuer.example/")
            self.assertEqual(len(pdfs), 2)

            three = two.replace(b"</body>", b'<a href="/3.pdf">3</a></body>')
            _, pdfs = F.extract_text(three, "text/html", "https://issuer.example/")
            self.assertEqual(pdfs, ["https://issuer.example/1.pdf", "https://issuer.example/2.pdf"])

    def test_empty_base_url_drops_relative_links(self):
        raw = b'<html><body><a href="/a.pdf">a</a></body></html>'
        _, pdfs = F.extract_text(raw, "text/html", "")
        self.assertEqual(pdfs, [])


# ---------------------------------------------------------------------------
# extract_text — PDF
# ---------------------------------------------------------------------------
class ExtractPdfTest(unittest.TestCase):

    def test_magic_bytes_beat_a_wrong_content_type(self):
        raw = b"%PDF-1.7\n<binary junk>"
        with mock.patch("pipeline.fetch.subprocess.run",
                        return_value=completed(b"Annual fee Rs 500")) as run:
            text, pdfs = F.extract_text(raw, "text/html", "https://issuer.example/mitc")
        self.assertEqual(text, "Annual fee Rs 500")
        self.assertEqual(pdfs, [])
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["pdftotext", "-layout", "-", "-"])
        self.assertEqual(run.call_args.kwargs["input"], raw)

    def test_content_type_pdf_without_magic_bytes(self):
        with mock.patch("pipeline.fetch.subprocess.run",
                        return_value=completed(b"Rs 150 block")) as run:
            text, _ = F.extract_text(b"not really a pdf", "application/pdf", "u")
        self.assertEqual(text, "Rs 150 block")
        self.assertTrue(run.called)

    def test_octet_stream_pdf_is_detected(self):
        with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"ok")):
            text, _ = F.extract_text(b"%PDF-1.4 x", "application/octet-stream", "u")
        self.assertEqual(text, "ok")

    def test_missing_pdftotext_degrades_quietly(self):
        """A tooling failure must not look like 'the issuer publishes no rate'."""
        with mock.patch("pipeline.fetch.subprocess.run", side_effect=FileNotFoundError("pdftotext")):
            self.assertEqual(F.extract_text(b"%PDF-1.7 x", "application/pdf", "u"), ("", []))

    def test_pdftotext_timeout_degrades_quietly(self):
        with mock.patch("pipeline.fetch.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("pdftotext", 120)):
            self.assertEqual(F.extract_text(b"%PDF-1.7 x", "application/pdf", "u"), ("", []))

    def test_pdftotext_permission_error_degrades_quietly(self):
        with mock.patch("pipeline.fetch.subprocess.run", side_effect=PermissionError("denied")):
            self.assertEqual(F.extract_text(b"%PDF-1.7 x", "application/pdf", "u"), ("", []))

    def test_pdftotext_failure_with_no_output(self):
        with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"", returncode=1)):
            self.assertEqual(F.extract_text(b"%PDF-1.7 x", "application/pdf", "u"), ("", []))

    def test_pdf_text_is_decoded_leniently(self):
        with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"fee \xff Rs 500")):
            text, _ = F.extract_text(b"%PDF-1.7 x", "application/pdf", "u")
        self.assertIn("Rs 500", text)

    def test_pdf_never_reports_linked_pdfs(self):
        with mock.patch("pipeline.fetch.subprocess.run",
                        return_value=completed(b'see also href="/other.pdf"')):
            _, pdfs = F.extract_text(b"%PDF-1.7 x", "application/pdf", "https://issuer.example/")
        self.assertEqual(pdfs, [])


# ---------------------------------------------------------------------------
# fetch_url — transport
# ---------------------------------------------------------------------------
class FetchUrlTest(SleeplessTest):

    def test_happy_path(self):
        body = PAGE.encode("utf-8")
        op = opener_for(FakeResponse(body))
        got = F.fetch_url("https://www.hdfc.bank.in/cards", opener=op)

        self.assertTrue(got.ok)
        self.assertEqual(got.status, 200)
        self.assertEqual(got.content_type, "text/html; charset=utf-8")
        self.assertEqual(got.raw_sha256, S.sha256_bytes(body))
        self.assertEqual(got.text_sha256, S.sha256_text(F.normalise_text(got.text)))
        self.assertEqual(got.error, "")
        self.assertIn("Earn 4 Reward Points per Rs 150 spent.", got.text)
        self.assertEqual(len(op.calls), 1)
        self.assertEqual(op.calls[0][1], C.FETCH_TIMEOUT_S)

    def test_request_carries_the_configured_user_agent(self):
        seen = {}

        def _open(request, timeout=None):
            seen["ua"] = request.get_header("User-agent")
            seen["ae"] = request.get_header("Accept-encoding")
            return FakeResponse(b"<html><body>hi</body></html>")

        F.fetch_url("https://issuer.example/", opener=_open)
        self.assertEqual(seen["ua"], C.USER_AGENT)
        self.assertIn("gzip", seen["ae"])

    def test_whitespace_reflow_does_not_change_text_sha(self):
        """The cheap-week guarantee, end to end through the fetcher."""
        a = "<html><body><p>Earn 4 Reward Points per Rs 150.</p></body></html>"
        b = "<html><body>\n\n  <p>Earn 4   Reward Points\n per Rs 150.</p>\n</body>\n</html>"
        one = F.fetch_url("https://issuer.example/", opener=opener_for(html_response(a)))
        two = F.fetch_url("https://issuer.example/", opener=opener_for(html_response(b)))
        self.assertNotEqual(one.raw_sha256, two.raw_sha256)
        self.assertEqual(one.text_sha256, two.text_sha256)

    def test_gzip_body_is_decoded(self):
        raw = gzip.compress(b"<html><body><p>Annual fee Rs 2,500</p></body></html>")
        resp = FakeResponse(raw, 200, {"Content-Type": "text/html", "Content-Encoding": "gzip"})
        got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertTrue(got.ok)
        self.assertIn("Annual fee Rs 2,500", got.text)
        self.assertEqual(got.raw_sha256, S.sha256_bytes(raw))

    def test_gzip_detected_by_magic_bytes_when_header_missing(self):
        raw = gzip.compress(b"<html><body><p>Rs 500</p></body></html>")
        resp = FakeResponse(raw, 200, {"Content-Type": "text/html"})
        got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertIn("Rs 500", got.text)

    def test_deflate_body_is_decoded(self):
        raw = zlib.compress(b"<html><body><p>Forex markup 3.5%</p></body></html>")
        resp = FakeResponse(raw, 200, {"Content-Type": "text/html", "Content-Encoding": "deflate"})
        got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertIn("Forex markup 3.5%", got.text)

    def test_raw_deflate_body_is_decoded(self):
        comp = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        raw = comp.compress(b"<html><body><p>Rs 199</p></body></html>") + comp.flush()
        resp = FakeResponse(raw, 200, {"Content-Type": "text/html", "Content-Encoding": "deflate"})
        got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertIn("Rs 199", got.text)

    def test_mislabelled_encoding_returns_the_original_bytes(self):
        body = b"<html><body><p>Rs 750</p></body></html>"
        resp = FakeResponse(body, 200, {"Content-Type": "text/html", "Content-Encoding": "deflate"})
        got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertIn("Rs 750", got.text)

    def test_lowercase_header_keys(self):
        resp = FakeResponse(b"Rs 100", 200, {"content-type": "text/plain"})
        got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertEqual(got.content_type, "text/plain")
        self.assertEqual(got.text, "Rs 100")

    # --- status handling ---------------------------------------------------

    def test_404_is_not_retried(self):
        op = opener_for(FakeResponse(b"nope", 404))
        got = F.fetch_url("https://issuer.example/gone", opener=op)
        self.assertFalse(got.ok)
        self.assertEqual(got.status, 404)
        self.assertEqual(got.error, "HTTP 404")
        self.assertEqual(len(op.calls), 1)
        self.assertEqual(self.sleep.call_count, 0)

    def test_raised_http_error_404_is_not_retried(self):
        exc = urllib.error.HTTPError("https://issuer.example/gone", 404, "Not Found", {}, io.BytesIO(b""))
        op = opener_for(exc)
        got = F.fetch_url("https://issuer.example/gone", opener=op)
        self.assertFalse(got.ok)
        self.assertEqual(got.status, 404)
        self.assertEqual(len(op.calls), 1)

    def test_500_is_retried_config_many_times(self):
        op = opener_for(FakeResponse(b"boom", 500))
        got = F.fetch_url("https://issuer.example/", opener=op)
        self.assertFalse(got.ok)
        self.assertEqual(got.status, 500)
        self.assertEqual(len(op.calls), C.FETCH_RETRIES + 1)
        self.assertEqual(self.sleep.call_count, C.FETCH_RETRIES)

    def test_raised_http_error_503_is_retried(self):
        exc = urllib.error.HTTPError("https://issuer.example/", 503, "Busy", {}, io.BytesIO(b""))
        op = opener_for(exc)
        got = F.fetch_url("https://issuer.example/", opener=op)
        self.assertFalse(got.ok)
        self.assertEqual(got.status, 503)
        self.assertEqual(len(op.calls), C.FETCH_RETRIES + 1)

    def test_500_then_200_succeeds(self):
        op = opener_for(FakeResponse(b"boom", 500), html_response("<html><body>Rs 1</body></html>"))
        got = F.fetch_url("https://issuer.example/", opener=op)
        self.assertTrue(got.ok)
        self.assertEqual(got.status, 200)
        self.assertEqual(len(op.calls), 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_status_boundaries(self):
        for status, ok, calls in (
            (200, True, 1),
            (299, True, 1),
            (302, False, 1),     # an unfollowed redirect is not worth a retry
            (400, False, 1),
            (499, False, 1),
            (500, False, C.FETCH_RETRIES + 1),
        ):
            with self.subTest(status=status):
                op = opener_for(FakeResponse(b"<html><body>x</body></html>", status))
                got = F.fetch_url("https://issuer.example/", opener=op)
                self.assertEqual(got.ok, ok)
                self.assertEqual(got.status, status)
                self.assertEqual(len(op.calls), calls)

    # --- transport failure -------------------------------------------------

    def test_transport_exception_becomes_ok_false(self):
        op = opener_for(urllib.error.URLError("dns is down"))
        got = F.fetch_url("https://issuer.example/", opener=op)
        self.assertFalse(got.ok)
        self.assertEqual(got.status, 0)
        self.assertIn("URLError", got.error)
        self.assertEqual(got.text, "")
        self.assertEqual(got.text_sha256, "")
        self.assertEqual(len(op.calls), C.FETCH_RETRIES + 1)

    def test_timeout_becomes_ok_false(self):
        op = opener_for(TimeoutError("timed out"))
        got = F.fetch_url("https://issuer.example/", opener=op)
        self.assertFalse(got.ok)
        self.assertIn("TimeoutError", got.error)

    def test_read_failure_becomes_ok_false(self):
        class Exploding(FakeResponse):
            def read(self, n=-1):
                raise OSError("connection reset mid-body")

        got = F.fetch_url("https://issuer.example/", opener=opener_for(Exploding(b"")))
        self.assertFalse(got.ok)
        self.assertIn("read failed", got.error)

    def test_response_is_closed(self):
        resp = FakeResponse(b"<html><body>x</body></html>")
        F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertTrue(resp.closed)

    # --- bad input ---------------------------------------------------------

    def test_bad_urls_never_reach_the_opener(self):
        op = opener_for(FakeResponse(b"should not be used"))
        for url in ("", "   ", None, 42, "ftp://issuer.example/x", "mailto:a@b.c", "/relative/path"):
            with self.subTest(url=url):
                got = F.fetch_url(url, opener=op)
                self.assertFalse(got.ok)
                self.assertEqual(got.status, 0)
                self.assertTrue(got.error)
        self.assertEqual(len(op.calls), 0)

    # --- caps --------------------------------------------------------------

    def test_read_is_capped_at_the_configured_byte_limit(self):
        resp = FakeResponse(b"<html><body>x</body></html>")
        F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertEqual(resp.reads[0], C.MAX_FETCH_BYTES + 1)

    def test_oversized_body_is_truncated_and_reported(self):
        with mock.patch.object(C, "MAX_FETCH_BYTES", 10):
            resp = FakeResponse(b"0123456789ABCDEF", 200, {"Content-Type": "text/plain"})
            got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertTrue(got.ok)
        self.assertEqual(got.text, "0123456789")
        self.assertIn("truncated", got.error)
        self.assertEqual(got.raw_sha256, S.sha256_bytes(b"0123456789"))

    def test_body_exactly_at_the_byte_limit_is_not_truncated(self):
        with mock.patch.object(C, "MAX_FETCH_BYTES", 10):
            resp = FakeResponse(b"0123456789", 200, {"Content-Type": "text/plain"})
            got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertEqual(got.text, "0123456789")
        self.assertEqual(got.error, "")

    def test_truncated_gzip_stream_still_yields_text(self):
        full = gzip.compress(b"<html><body><p>" + b"Rs 500 " * 200 + b"</p></body></html>")
        with mock.patch.object(C, "MAX_FETCH_BYTES", len(full) - 5):
            resp = FakeResponse(full, 200, {"Content-Type": "text/html", "Content-Encoding": "gzip"})
            got = F.fetch_url("https://issuer.example/", opener=opener_for(resp))
        self.assertTrue(got.ok)
        self.assertIn("Rs 500", got.text)
        self.assertIn("truncated", got.error)

    def test_page_text_capped_at_max_page_chars(self):
        body = "<html><body><p>" + ("A" * 500) + "</p></body></html>"
        with mock.patch.object(C, "MAX_PAGE_CHARS", 20):
            got = F.fetch_url("https://issuer.example/", opener=opener_for(html_response(body)))
        self.assertEqual(len(got.text), 20)
        self.assertEqual(got.text_sha256, S.sha256_text(F.normalise_text(got.text)))

    def test_page_text_exactly_at_the_char_limit_is_untouched(self):
        with mock.patch.object(C, "MAX_PAGE_CHARS", 5):
            got = F.fetch_url("https://issuer.example/",
                              opener=opener_for(FakeResponse(b"12345", 200, {"Content-Type": "text/plain"})))
        self.assertEqual(got.text, "12345")

    def test_pdf_text_uses_the_pdf_char_budget(self):
        resp = FakeResponse(b"%PDF-1.7 x", 200, {"Content-Type": "application/pdf"})
        with mock.patch.object(C, "MAX_PAGE_CHARS", 5), mock.patch.object(C, "MAX_PDF_CHARS", 12):
            with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"A" * 100)):
                got = F.fetch_url("https://issuer.example/mitc.pdf", opener=opener_for(resp))
        self.assertEqual(len(got.text), 12)

    def test_unreadable_pdf_is_ok_but_flagged(self):
        resp = FakeResponse(b"%PDF-1.7 x", 200, {"Content-Type": "application/pdf"})
        with mock.patch("pipeline.fetch.subprocess.run", side_effect=FileNotFoundError()):
            got = F.fetch_url("https://issuer.example/mitc.pdf", opener=opener_for(resp))
        self.assertTrue(got.ok)                      # the fetch worked
        self.assertEqual(got.text, "")               # the extraction did not
        self.assertIn("pdftotext", got.error)


# ---------------------------------------------------------------------------
# fetch_source — page plus linked PDFs
# ---------------------------------------------------------------------------
class FetchSourceTest(SleeplessTest):

    def setUp(self) -> None:
        super().setUp()
        self.page = ('<html><body><p>Regalia Gold</p>'
                     '<a href="/docs/mitc.pdf">MITC</a>'
                     '<a href="/docs/fees.pdf">Fees</a>'
                     '</body></html>')
        self.pdf_headers = {"Content-Type": "application/pdf"}

    def _routes(self, mitc=b"MITC: 4 Reward Points per Rs 150", fees=b"Fees: Rs 2,500"):
        return router({
            "https://issuer.example/card": html_response(self.page),
            "https://issuer.example/docs/mitc.pdf": FakeResponse(b"%PDF-1.7 mitc", 200, self.pdf_headers),
            "https://issuer.example/docs/fees.pdf": FakeResponse(b"%PDF-1.7 fees", 200, self.pdf_headers),
        }), {b"%PDF-1.7 mitc": mitc, b"%PDF-1.7 fees": fees}

    def test_linked_pdfs_are_appended_under_a_banner(self):
        op, pdf_text = self._routes()
        with mock.patch("pipeline.fetch.subprocess.run",
                        side_effect=lambda argv, **kw: completed(pdf_text[kw["input"]])):
            got = F.fetch_source("https://issuer.example/card", opener=op)

        self.assertTrue(got.ok)
        self.assertIn("Regalia Gold", got.text)
        self.assertIn("=== PDF: https://issuer.example/docs/mitc.pdf ===", got.text)
        self.assertIn("=== PDF: https://issuer.example/docs/fees.pdf ===", got.text)
        self.assertIn("4 Reward Points per Rs 150", got.text)
        self.assertIn("Fees: Rs 2,500", got.text)
        self.assertEqual(len(op.calls), 3)
        self.assertEqual(got.text_sha256, S.sha256_text(F.normalise_text(got.text)))

    def test_appending_pdfs_changes_the_text_hash_not_the_raw_hash(self):
        op, pdf_text = self._routes()
        page_only = F.fetch_source("https://issuer.example/card", opener=op, follow_pdfs=False)
        op2, _ = self._routes()
        with mock.patch("pipeline.fetch.subprocess.run",
                        side_effect=lambda argv, **kw: completed(pdf_text[kw["input"]])):
            with_pdfs = F.fetch_source("https://issuer.example/card", opener=op2)

        self.assertEqual(page_only.raw_sha256, with_pdfs.raw_sha256)
        self.assertNotEqual(page_only.text_sha256, with_pdfs.text_sha256)

    def test_follow_pdfs_false_fetches_only_the_page(self):
        op, _ = self._routes()
        got = F.fetch_source("https://issuer.example/card", opener=op, follow_pdfs=False)
        self.assertEqual(len(op.calls), 1)
        self.assertNotIn("=== PDF:", got.text)
        self.assertEqual(got.linked_pdfs, [
            "https://issuer.example/docs/mitc.pdf",
            "https://issuer.example/docs/fees.pdf",
        ])

    def test_polite_delay_between_same_host_requests(self):
        op, pdf_text = self._routes()
        with mock.patch("pipeline.fetch.subprocess.run",
                        side_effect=lambda argv, **kw: completed(pdf_text[kw["input"]])):
            F.fetch_source("https://issuer.example/card", opener=op)
        self.assertEqual(self.sleep.call_args_list,
                         [mock.call(C.POLITE_DELAY_S), mock.call(C.POLITE_DELAY_S)])

    def test_no_delay_when_the_pdf_is_on_another_host(self):
        page = '<html><body><a href="https://cdn.other.example/a.pdf">a</a></body></html>'
        op = router({
            "https://issuer.example/card": html_response(page),
            "https://cdn.other.example/a.pdf": FakeResponse(b"%PDF-1.7 x", 200, self.pdf_headers),
        })
        with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"Rs 500")):
            got = F.fetch_source("https://issuer.example/card", opener=op)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertIn("Rs 500", got.text)

    def test_a_failing_pdf_is_noted_but_the_page_survives(self):
        op = router({"https://issuer.example/card": html_response(self.page)})   # PDFs 404
        got = F.fetch_source("https://issuer.example/card", opener=op)
        self.assertTrue(got.ok)
        self.assertIn("Regalia Gold", got.text)
        self.assertNotIn("=== PDF:", got.text)
        self.assertIn("pdf skipped", got.error)
        self.assertIn("HTTP 404", got.error)

    def test_an_unreadable_pdf_is_noted_not_appended_empty(self):
        op, _ = self._routes()
        with mock.patch("pipeline.fetch.subprocess.run", side_effect=FileNotFoundError()):
            got = F.fetch_source("https://issuer.example/card", opener=op)
        self.assertTrue(got.ok)
        self.assertNotIn("=== PDF:", got.text)
        self.assertIn("pdftotext", got.error)

    def test_failed_page_is_returned_untouched(self):
        op = router({})
        got = F.fetch_source("https://issuer.example/card", opener=op)
        self.assertFalse(got.ok)
        self.assertEqual(got.status, 404)
        self.assertEqual(len(op.calls), 1)

    def test_pdf_url_fetched_directly_needs_no_following(self):
        op = router({"https://issuer.example/mitc.pdf":
                     FakeResponse(b"%PDF-1.7 x", 200, self.pdf_headers)})
        with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"Rs 500 fee")):
            got = F.fetch_source("https://issuer.example/mitc.pdf", opener=op)
        self.assertTrue(got.ok)
        self.assertEqual(got.text, "Rs 500 fee")
        self.assertEqual(got.linked_pdfs, [])
        self.assertEqual(len(op.calls), 1)


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------
class ModuleContractTest(SleeplessTest):

    def test_public_api(self):
        for name in ("Fetched", "fetch_url", "extract_text", "normalise_text", "fetch_source", "main"):
            self.assertTrue(hasattr(F, name), name)

    def test_fetched_defaults_are_not_shared(self):
        one, two = F.Fetched(url="a"), F.Fetched(url="b")
        one.linked_pdfs.append("x")
        self.assertEqual(two.linked_pdfs, [])

    def test_fetching_writes_nothing_to_disk(self):
        """Trap 2 in its strongest form: this module never writes a card file,
        or any file. Nothing it produces can overwrite hand-curated data."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, cwd)
            op = router({"https://issuer.example/card": html_response(PAGE)})
            with mock.patch("pipeline.fetch.subprocess.run", return_value=completed(b"x")):
                F.fetch_source("https://issuer.example/card", opener=op)
            self.assertEqual(sorted(Path(tmp).iterdir()), [])

    def test_main_exit_codes(self):
        good = F.Fetched(url="https://issuer.example/", ok=True, status=200,
                         content_type="text/html", text="Rs 500", text_sha256="abc")
        bad = F.Fetched(url="https://issuer.example/", ok=False, status=404, error="HTTP 404")
        pdf_gone = F.Fetched(url="https://issuer.example/m.pdf", ok=True, status=200,
                             content_type="application/pdf", text="", error="no text extracted")

        argv = ["fetch.py", "--url", "https://issuer.example/"]
        # which() -> None models a machine with no poppler, which is the only
        # thing that turns an empty PDF into a config error rather than a data one.
        for result, expected in ((good, 0), (bad, 1), (pdf_gone, 2)):
            with self.subTest(expected=expected):
                with mock.patch.object(sys, "argv", argv), \
                     mock.patch("pipeline.fetch.fetch_source", return_value=result), \
                     mock.patch("pipeline.fetch.shutil.which", return_value=None), \
                     mock.patch("sys.stdout", new=io.StringIO()):
                    self.assertEqual(F.main(), expected)

    def test_main_reports_a_pdf_with_poppler_present_as_a_data_error(self):
        pdf_empty = F.Fetched(url="https://issuer.example/m.pdf", ok=True, status=200,
                              content_type="application/pdf", text="")
        with mock.patch.object(sys, "argv", ["fetch.py", "--url", "https://issuer.example/m.pdf"]), \
             mock.patch("pipeline.fetch.fetch_source", return_value=pdf_empty), \
             mock.patch("pipeline.fetch.shutil.which", return_value="/usr/bin/pdftotext"), \
             mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(F.main(), 1)

    def test_main_prints_status_hash_and_preview(self):
        result = F.Fetched(url="https://www.hdfc.bank.in/x", ok=True, status=200,
                           content_type="text/html", raw_sha256="r" * 64,
                           text="Earn 4 Reward Points per Rs 150 spent.", text_sha256="t" * 64,
                           linked_pdfs=["https://www.hdfc.bank.in/a.pdf"])
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["fetch.py", "--url", "https://www.hdfc.bank.in/x"]), \
             mock.patch("pipeline.fetch.fetch_source", return_value=result), \
             mock.patch("sys.stdout", new=out):
            code = F.main()
        printed = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("status       200", printed)
        self.assertIn("t" * 64, printed)
        self.assertIn("chars        38", printed)
        self.assertIn("https://www.hdfc.bank.in/a.pdf", printed)
        self.assertIn("Earn 4 Reward Points per Rs 150 spent.", printed)

    def test_main_flags_a_source_off_the_issuer_allowlist(self):
        result = F.Fetched(url="https://www.cardexpert.in/review", ok=True, status=200,
                           content_type="text/html", text="10% cashback!")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["fetch.py", "--url", "https://www.cardexpert.in/review"]), \
             mock.patch("pipeline.fetch.fetch_source", return_value=result), \
             mock.patch("sys.stdout", new=out):
            F.main()
        self.assertIn("not on the issuer allowlist", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
