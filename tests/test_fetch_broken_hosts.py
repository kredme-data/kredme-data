#!/usr/bin/env python3
"""
Tests for the two ways an issuer disappears from the refresh without saying so.

Usage:
    python3 tests/test_fetch_broken_hosts.py            # run all
    python3 tests/test_fetch_broken_hosts.py -v         # per-test names

A dry run on 2026-08-17 reported "fetched 373: 335 changed, 0 unchanged, 38
failed" and printed ten warnings, all the same host. The other 28 were hidden
by a `if failed <= 10` cap. The 38 turned out to be TWO WHOLE ISSUERS:

    www.bobcard.co.in   19 cards   TLS: server omits its intermediate cert
    www.yes.bank.in     17 cards   200 OK, full page, zero server-rendered text

Neither bank was down. Had the batch been submitted, ~$42 would have bought a
refresh of 335 cards while 36 silently stayed stale.

Both original bugs were near-misses that LOOKED like working code, so each is
pinned here directly:

  - `_ca_issuer_urls` returned [] on a real certificate, which is
    indistinguishable from "this cert has no AIA extension". Cause: the URL
    regex is greedy and DER bytes after the URI fall inside the legal-URL
    character class, so the match ran past `.crt` and `endswith` never fired.

  - `_looks_like_js_shell` returned False on a real JS shell, which downgraded
    to the generic "no text extracted" and read as a parser bug worth chasing.
    Cause: the <noscript> marker sits past byte 200,000 of a 300 KB page and
    the check only scanned a 200 KB prefix.

Nothing here touches the network. Stdlib only — unittest, no pytest.
"""
from __future__ import annotations

import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline import cli as CLI          # noqa: E402
from pipeline import fetch as F          # noqa: E402


# ── AIA: mining the CA-issuer URL out of raw DER ─────────────────────────────
class CaIssuerUrls(unittest.TestCase):
    def test_url_is_cut_at_the_certificate_suffix_not_run_past_it(self):
        # The bug in one assertion. Real DER continues straight into binary
        # after the URI, and those bytes match the URL character class, so a
        # greedy match swallows them. Cutting at the suffix is what makes this
        # work; `endswith` on the whole match silently yields [].
        der = (b"\x30\x82" + b"http://secure.globalsign.com/cacert/x.crt"
               + b"0\x82\x01\x22garbage,;=trailing")
        self.assertEqual(
            F._ca_issuer_urls(der),
            ["http://secure.globalsign.com/cacert/x.crt"],
        )

    def test_ocsp_url_in_the_same_extension_is_not_mistaken_for_a_cert(self):
        # AIA carries the OCSP responder alongside CA Issuers. Fetching the
        # responder as if it were a certificate would fail confusingly.
        der = b"http://ocsp.globalsign.com/gsrsaovsslca2018\x30\x82"
        self.assertEqual(F._ca_issuer_urls(der), [])

    def test_a_certificate_with_no_aia_yields_nothing(self):
        self.assertEqual(F._ca_issuer_urls(b"\x30\x82\x01\x22no urls here"), [])
        self.assertEqual(F._ca_issuer_urls(b""), [])

    def test_duplicate_urls_are_reported_once(self):
        one = b"http://ca.example/i.crt\x00"
        self.assertEqual(F._ca_issuer_urls(one + one), ["http://ca.example/i.crt"])


class CertVerifyErrorDetection(unittest.TestCase):
    def test_recognised_through_the_urlerror_that_wraps_it(self):
        # urllib raises URLError with the SSL error as .reason, so testing the
        # exception type alone misses every real occurrence.
        import ssl
        import urllib.error
        inner = ssl.SSLCertVerificationError("unable to get local issuer certificate")
        self.assertTrue(F._is_cert_verify_error(urllib.error.URLError(inner)))
        self.assertTrue(F._is_cert_verify_error(inner))

    def test_an_ordinary_timeout_is_not_treated_as_a_chain_problem(self):
        self.assertFalse(F._is_cert_verify_error(TimeoutError("timed out")))
        self.assertFalse(F._is_cert_verify_error(OSError("connection reset")))


class AiaRetryScope(unittest.TestCase):
    def test_injected_opener_never_triggers_a_network_repair(self):
        # Tests inject an opener and must stay hermetic: the repair path opens
        # a real TLS socket, so it has to be off whenever a caller supplied one.
        import ssl
        import urllib.error
        calls = []

        def fake_opener(request, timeout=None):
            calls.append(getattr(request, "full_url", request))
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("unable to get local issuer certificate")
            )

        got = F.fetch_url("https://example.invalid/x", opener=fake_opener)
        self.assertFalse(got.ok)
        self.assertIn("unable to get local issuer certificate", got.error)
        # The point of the test: retried per FETCH_RETRIES and never more. An
        # extra call would mean the repair path opened a real socket despite an
        # injected opener, which would make the suite non-hermetic.
        self.assertEqual(len(calls), max(1, int(F.C.FETCH_RETRIES) + 1))


# ── JavaScript shells ────────────────────────────────────────────────────────
class JsShellDetection(unittest.TestCase):
    def test_marker_beyond_200kb_is_still_found(self):
        # The exact shape of www.yes.bank.in: a large inlined bundle, with the
        # <noscript> fallback only at the very end. A prefix-window check
        # reports False here and the failure loses its name.
        raw = (b"<html><body>" + b"x" * 250_000
               + b"<noscript>This site requires JavaScript to be enabled.</noscript>"
               + b"</body></html>")
        self.assertTrue(F._looks_like_js_shell(raw))

    def test_ordinary_page_merely_mentioning_javascript_is_not_a_shell(self):
        # A real article about JavaScript must not be reported as unreadable.
        raw = b"<html><body><p>We love javascript is required reading.</p></body></html>"
        self.assertFalse(F._looks_like_js_shell(raw))

    def test_noscript_alone_is_not_enough(self):
        raw = b"<html><noscript><img src=/px.gif></noscript><p>Real content.</p></html>"
        self.assertFalse(F._looks_like_js_shell(raw))

    def test_empty_body_is_not_a_shell(self):
        self.assertFalse(F._looks_like_js_shell(b""))

    def test_finish_names_the_shell_instead_of_blaming_the_parser(self):
        raw = (b"<html><body><noscript>This site requires JavaScript to be enabled."
               b"</noscript></body></html>")
        got = F._finish("https://bank.example/cards", 200, "text/html", "", raw, False)
        self.assertTrue(got.ok)          # the transport genuinely worked
        self.assertEqual(got.text, "")
        self.assertIn("JavaScript shell", got.error)
        self.assertNotEqual(got.error, "no text extracted")

    def test_a_page_with_real_text_is_untouched(self):
        raw = b"<html><body><h1>Cashback Card</h1><p>5% on groceries.</p></body></html>"
        got = F._finish("https://bank.example/c", 200, "text/html", "", raw, False)
        self.assertIn("Cashback Card", got.text)
        self.assertEqual(got.error, "")


# ── Reporting: the cap that hid 28 of 38 failures ────────────────────────────
class FailureReporting(unittest.TestCase):
    def test_faults_are_grouped_by_class_not_by_raw_message(self):
        self.assertEqual(
            CLI._reason_of("URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>"),
            "TLS chain incomplete (server omits its intermediate)",
        )
        self.assertEqual(
            CLI._reason_of("page is a JavaScript shell — no server-rendered text"),
            "JavaScript-rendered page — no static text to read",
        )
        self.assertEqual(CLI._reason_of("HTTP 404"), "HTTP 404")

    def test_every_failure_is_reported_not_just_the_first_ten(self):
        # 38 failures across 2 hosts used to print 10 lines from 1 host.
        failures = (
            [(f"bob_{i}", "https://www.bobcard.co.in/x",
              "URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] ...>")
             for i in range(19)]
            + [(f"yes_{i}", "https://www.yes.bank.in/y",
                "page is a JavaScript shell — no server-rendered text")
               for i in range(17)]
        )
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            CLI._report_fetch_failures(failures)
        out = buf.getvalue()

        self.assertIn("36 source(s)", out)      # the true total, not 10
        self.assertIn("2 host(s)", out)
        self.assertIn("19 cards", out)          # the shape: a whole issuer
        self.assertIn("17 cards", out)
        self.assertIn("www.bobcard.co.in", out)
        self.assertIn("www.yes.bank.in", out)

    def test_no_failures_prints_nothing(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            CLI._report_fetch_failures([])
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
