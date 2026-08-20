#!/usr/bin/env python3
"""
Tests for pipeline/discover.py — per-card source URL resolution.

Usage:
    python3 tests/test_discover.py            # run all
    python3 tests/test_discover.py -v         # per-test names

The module's whole job is to decide which issuer page belongs to which card, and
the expensive failure is not "found nothing" — it is "found the wrong one". A
card pointed at a sibling card's page produces an extraction that the adversarial
verify pass will CONFIRM, because the quotes really are on the page it was given.
So most of what follows is about refusing to match, not about matching.

Nothing here touches the network: every fetch goes through a routed fake opener.
Stdlib only — unittest, no pytest.
"""
from __future__ import annotations

import pathlib
import sys
import unittest
import urllib.error

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import discover as D  # noqa: E402


# ------------------------------------------------------------- fixtures ----

class FakeResponse:
    """The shape fetch.py needs from urlopen: read(n), headers.get, close()."""

    def __init__(self, body: bytes, status: int = 200, content_type: str = "text/html"):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": content_type + "; charset=utf-8"}
        self.closed = False

    def read(self, n: int = -1) -> bytes:
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self) -> None:
        self.closed = True


def route(mapping: dict, default_status: int = 404):
    """A fake urlopen that serves a site: {url -> bytes | FakeResponse}."""
    def _open(request, timeout=None):
        url = request.full_url
        item = mapping.get(url)
        if item is None:
            raise urllib.error.HTTPError(url, default_status, "Not Found", {}, None)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)
    return _open


def listing(*hrefs: str) -> bytes:
    links = "".join(f'<a href="{h}">x</a>' for h in hrefs)
    return f"<html><body><h1>Cards</h1>{links}</body></html>".encode()


def card_page(name: str) -> bytes:
    return f"<html><body><h1>{name}</h1><p>Earn 5% back.</p></body></html>".encode()


def card(card_id: str, name: str, issuer: str = "HDFC Bank") -> dict:
    return {"card": {"id": card_id, "card_name": name, "issuer": issuer, "is_active": 1}}


HOST = "https://www.hdfc.bank.in"


# --------------------------------------------------------------- tokens ----

class TestKeys(unittest.TestCase):

    def test_issuer_words_stripped_only_for_their_own_issuer(self):
        """'first' is IDFC's issuer word and HDFC's product word.

        Stripping the global issuer set from both sides collapsed HDFC's
        'regalia-first' to {regalia}, colliding with plain Regalia and taking
        BOTH cards out as ambiguous. This is that regression.
        """
        hdfc = D._issuer_words_of("HDFC Bank")
        idfc = D._issuer_words_of("IDFC FIRST Bank")
        self.assertNotIn("first", hdfc)
        self.assertIn("first", idfc)

    def test_regalia_variants_have_distinct_keys(self):
        words = D._issuer_words_of("HDFC Bank")
        keys = [D._keys(n, words) for n in (
            "HDFC Bank Regalia Credit Card",
            "HDFC Bank Regalia First Credit Card",
            "HDFC Bank Regalia Gold Credit Card",
        )]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                self.assertFalse(keys[i] & keys[j], f"{i} and {j} share a key")

    def test_concatenation_bridges_a_word_boundary(self):
        """'MoneyBack' and 'money-back' are the same product, spelled two ways."""
        words = D._issuer_words_of("HDFC Bank")
        self.assertTrue(D._keys("HDFC MoneyBack Credit Card", words)
                        & D._keys("money-back-credit-card", words))

    def test_concatenation_survives_a_possessive(self):
        """Sorting the concatenation broke this: doctor+s+regalia -> doctorregalias."""
        words = D._issuer_words_of("HDFC Bank")
        self.assertTrue(D._keys("HDFC Bank Doctor's Regalia Credit Card", words)
                        & D._keys("doctors-regalia-credit-card", words))

    def test_token_key_is_order_insensitive(self):
        words = D._issuer_words_of("HDFC Bank")
        self.assertTrue(D._keys("Diners Club Black", words)
                        & D._keys("black-diners-club", words))

    def test_a_name_of_pure_issuer_words_is_not_reduced_to_nothing(self):
        """Otherwise 'HDFC Bank Credit Card' matches every slug on the site."""
        self.assertTrue(D._keys("HDFC Bank Credit Card", D._issuer_words_of("HDFC Bank")))


# ------------------------------------------------------- the URL filter ----

class TestPlausibleCardPage(unittest.TestCase):

    def test_rejects_tracking_query_strings(self):
        self.assertFalse(D._is_plausible_card_page(
            f"{HOST}/cards/apply.html?CHANNELSOURCE=BIZC&utm_content=MKTG"))

    def test_rejects_apply_and_servicing_and_editorial(self):
        for path in ("/credit-cards/services/forgot-pin", "/blogs/credit-cards/tips",
                     "/credit-cards/compare-page", "/credit-cards/services"):
            with self.subTest(path=path):
                self.assertFalse(D._is_plausible_card_page(HOST + path))

    def test_rejects_the_apply_subdomain(self):
        self.assertFalse(D._is_plausible_card_page(
            "https://applyonline.hdfc.bank.in/cards/credit-cards.html"))

    def test_rejects_pdfs_and_accepts_a_card_page(self):
        self.assertFalse(D._is_plausible_card_page(f"{HOST}/mitc.pdf"))
        self.assertTrue(D._is_plausible_card_page(f"{HOST}/credit-cards/regalia-credit-card"))

    def test_rejects_a_different_product_line(self):
        """Card names are reused across products, and those pages state real terms.

        IndusInd sells a 'Duo Plus' DEBIT card and an 'Indus Solitaire' SAVINGS
        account; RBL runs 'Aspire Banking'. All three matched credit cards by
        name on an earlier pass. Nothing downstream would have caught it.
        """
        for path in ("/in/en/personal/cards/debit-card/duo-plus.html",
                     "/in/en/personal/accounts/saving-account/indus-solitaire.html",
                     "/preferred-banking/aspire-banking",
                     "/personal-banking/loans/personal-loan"):
            with self.subTest(path=path):
                self.assertFalse(D._is_plausible_card_page(HOST + path))


# -------------------------------------------------------- the tie-break ----

class TestPreferCatalogue(unittest.TestCase):

    def test_catalogue_path_beats_a_campaign_microsite(self):
        got = D._prefer_catalogue({
            "https://www.sbicard.com/en/personal/credit-cards/cashback-sbi-card.html",
            "https://www.sbicard.com/sprint/cashback",
        })
        self.assertEqual(len(got), 1)
        self.assertIn("credit-cards", next(iter(got)))

    def test_two_catalogue_paths_stay_ambiguous(self):
        """The tie-break narrows or it abstains; it never picks arbitrarily."""
        urls = {
            "https://www.sbicard.com/en/personal/credit-cards/a.html",
            "https://www.sbicard.com/en/personal/credit-cards/b.html",
        }
        self.assertEqual(D._prefer_catalogue(urls), urls)


# -------------------------------------------------------------- matching ----

class TestMatch(unittest.TestCase):

    def test_each_regalia_gets_its_own_page(self):
        cards = [
            card("hdfc_regalia", "HDFC Bank Regalia Credit Card"),
            card("hdfc_regalia_first", "HDFC Bank Regalia First Credit Card"),
            card("hdfc_regalia_gold", "HDFC Bank Regalia Gold Credit Card"),
        ]
        links = [f"{HOST}/credit-cards/regalia-credit-card",
                 f"{HOST}/credit-cards/regalia-first-credit-card",
                 f"{HOST}/credit-cards/regalia-gold-credit-card"]
        got = {c.card_id: c.url for c in D.match(cards, links)}
        self.assertEqual(got["hdfc_regalia"], links[0])
        self.assertEqual(got["hdfc_regalia_first"], links[1])
        self.assertEqual(got["hdfc_regalia_gold"], links[2])

    def test_a_card_with_no_link_is_left_on_the_landing_page(self):
        got = D.match([card("hdfc_infinia", "HDFC Bank INFINIA Metal Credit Card")],
                      [f"{HOST}/credit-cards/regalia-credit-card"])
        self.assertEqual(got[0].status, "unmatched")
        self.assertEqual(got[0].url, "")

    def test_two_links_for_one_card_refuses_rather_than_guesses(self):
        got = D.match([card("hdfc_regalia", "HDFC Bank Regalia Credit Card")],
                      [f"{HOST}/a/regalia-credit-card", f"{HOST}/b/regalia-credit-card"])
        self.assertEqual(got[0].status, "ambiguous")
        self.assertEqual(got[0].url, "")

    def test_two_cards_cannot_claim_the_same_url(self):
        """The reverse collision: one slug, two cards. The second must not inherit it."""
        cards = [card("a", "Regalia Credit Card"), card("b", "Regalia Credit Card")]
        got = D.match(cards, [f"{HOST}/credit-cards/regalia-credit-card"])
        self.assertEqual(got[0].status, "matched")
        self.assertEqual(got[1].status, "ambiguous")
        self.assertIn("already claimed", got[1].reason)

    def test_issuer_spelled_two_ways_still_resolves(self):
        """The seed carries both 'IDFC FIRST Bank' and 'IDFC First Bank'."""
        cards = [card("idfc_wealth", "IDFC FIRST Wealth Credit Card", "IDFC FIRST Bank"),
                 card("idfc_select", "IDFC First Select Credit Card", "IDFC First Bank")]
        links = ["https://www.idfcfirst.bank.in/credit-card/wealth",
                 "https://www.idfcfirst.bank.in/credit-card/select"]
        got = {c.card_id: c.status for c in D.match(cards, links)}
        self.assertEqual(got["idfc_wealth"], "matched")
        self.assertEqual(got["idfc_select"], "matched")


# ---------------------------------------------------------- verification ----

class TestVerify(unittest.TestCase):

    def test_a_page_that_never_names_the_card_is_rejected(self):
        cand = D.Candidate("hdfc_regalia", "HDFC Bank Regalia Credit Card", "HDFC Bank",
                           url=f"{HOST}/credit-cards/regalia-credit-card", status="matched")
        op = route({cand.url: card_page("Millennia Credit Card")})
        got = D.verify(cand, opener=op)
        self.assertEqual(got.status, "unverified")
        self.assertIn("never names the card", got.reason)

    def test_a_page_that_names_the_card_is_kept(self):
        cand = D.Candidate("hdfc_regalia", "HDFC Bank Regalia Credit Card", "HDFC Bank",
                           url=f"{HOST}/credit-cards/regalia-credit-card", status="matched")
        op = route({cand.url: card_page("HDFC Bank Regalia Credit Card")})
        self.assertEqual(D.verify(cand, opener=op).status, "matched")

    def test_a_page_about_another_product_line_is_rejected(self):
        """The last line of defence when a wrong-product URL slips the path filter."""
        cand = D.Candidate("indusind_duo_plus", "IndusInd Bank Duo Plus Credit Card",
                           "IndusInd Bank", url=f"{HOST}/duo-plus", status="matched")
        page = b"<html><body><h1>Duo Plus</h1><p>A debit card with rewards.</p></body></html>"
        got = D.verify(cand, opener=route({cand.url: FakeResponse(page)}))
        self.assertEqual(got.status, "unverified")
        self.assertIn("wrong product line", got.reason)

    def test_a_404_on_the_matched_url_is_not_a_match(self):
        cand = D.Candidate("x", "Regalia Credit Card", "HDFC Bank",
                           url=f"{HOST}/credit-cards/gone", status="matched")
        self.assertEqual(D.verify(cand, opener=route({})).status, "unverified")

    def test_size_is_never_the_liveness_signal(self):
        """Axis's 404 shell is LARGER than its real content page.

        A verifier that trusted byte count would pass this; only naming the card
        counts as evidence.
        """
        cand = D.Candidate("x", "Regalia Credit Card", "HDFC Bank",
                           url=f"{HOST}/credit-cards/regalia-credit-card", status="matched")
        huge = b"<html><body>" + b"<p>Page not found. </p>" * 5000 + b"</body></html>"
        got = D.verify(cand, opener=route({cand.url: FakeResponse(huge)}))
        self.assertEqual(got.status, "unverified")


# -------------------------------------------------------------- harvest ----

class TestHarvest(unittest.TestCase):

    def test_links_off_the_issuer_domain_are_dropped(self):
        page = listing(f"{HOST}/credit-cards/regalia-credit-card",
                       "https://cardinsider.example/hdfc-regalia")
        got = D.harvest(f"{HOST}/credit-cards",
                        opener=route({f"{HOST}/credit-cards": page}), use_sitemap=False)
        self.assertEqual(got, [f"{HOST}/credit-cards/regalia-credit-card"])

    def test_a_sitemap_supplies_cards_a_javascript_listing_page_hides(self):
        """SBI's listing page yields no card links; its sitemap yields 209."""
        host = "https://www.sbicard.com"
        sitemap = (b'<?xml version="1.0"?><urlset>'
                   b"<url><loc>https://www.sbicard.com/en/personal/credit-cards/cashback-sbi-card.html</loc></url>"
                   b"</urlset>")
        op = route({
            f"{host}/en/personal/credit-cards.page": listing(),
            f"{host}/sitemap.xml": FakeResponse(sitemap, content_type="application/xml"),
        })
        got = D.harvest(f"{host}/en/personal/credit-cards.page", opener=op)
        self.assertIn(f"{host}/en/personal/credit-cards/cashback-sbi-card.html", got)

    def test_a_sitemap_index_is_followed_one_level(self):
        host = "https://www.sbicard.com"
        index = (b"<sitemapindex><sitemap><loc>https://www.sbicard.com/pages.xml</loc>"
                 b"</sitemap></sitemapindex>")
        child = (b"<urlset><url><loc>https://www.sbicard.com/en/personal/credit-cards/x.html"
                 b"</loc></url></urlset>")
        op = route({
            f"{host}/cards": listing(),
            f"{host}/sitemap.xml": FakeResponse(index, content_type="application/xml"),
            f"{host}/pages.xml": FakeResponse(child, content_type="application/xml"),
        })
        self.assertIn(f"{host}/en/personal/credit-cards/x.html",
                      D.harvest(f"{host}/cards", opener=op))

    def test_a_missing_sitemap_is_not_an_error(self):
        """Kotak and YES both 404 on /sitemap.xml; the listing page must still work."""
        page = listing(f"{HOST}/credit-cards/regalia-credit-card")
        got = D.harvest(f"{HOST}/credit-cards", opener=route({f"{HOST}/credit-cards": page}))
        self.assertEqual(got, [f"{HOST}/credit-cards/regalia-credit-card"])


# ------------------------------------------------------------ overrides ----

class TestToOverrides(unittest.TestCase):

    def test_a_hand_written_override_is_never_overwritten(self):
        results = [D.Candidate("hdfc_regalia", "Regalia", "HDFC Bank",
                               url=f"{HOST}/crawled", status="matched")]
        got = D.to_overrides(results, {"hdfc_regalia": f"{HOST}/chosen-by-a-human"})
        self.assertEqual(got["hdfc_regalia"], f"{HOST}/chosen-by-a-human")

    def test_only_matched_candidates_are_written(self):
        results = [
            D.Candidate("a", "A", "HDFC Bank", url=f"{HOST}/a", status="matched"),
            D.Candidate("b", "B", "HDFC Bank", url=f"{HOST}/b", status="ambiguous"),
            D.Candidate("c", "C", "HDFC Bank", url=f"{HOST}/c", status="unverified"),
            D.Candidate("d", "D", "HDFC Bank", status="unmatched"),
        ]
        self.assertEqual(D.to_overrides(results, {}), {"a": f"{HOST}/a"})

    def test_written_file_keeps_its_explanatory_comment(self):
        """tests/test_sources.py asserts on `_comment`; a crawl must not drop it."""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sources_overrides.json"
            D.write_overrides(path, {"hdfc_regalia": f"{HOST}/regalia-credit-card"})
            body = json.loads(path.read_text())
        self.assertIn("_comment", body)
        self.assertEqual(body["hdfc_regalia"], f"{HOST}/regalia-credit-card")

    def test_a_comment_key_is_never_written_as_a_card(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "o.json"
            D.write_overrides(path, {"_comment": "stale text", "a": f"{HOST}/a"})
            body = json.loads(path.read_text())
        self.assertEqual(body["_comment"], D.OVERRIDES_COMMENT)


class TestCliEntryPoint(unittest.TestCase):
    """The CLI is EXECUTED, not imported, and that difference hides real bugs.

    `cmd_discover` first shipped with `from . import discover`, which works from
    every test in this file and raises ImportError the moment anyone runs
    `python3 pipeline/cli.py discover` — because a script has no parent package.
    Importing the module can never catch that; only running it can.

    `--issuer __none__` matches no card, so discover() returns before it opens a
    socket. The subprocess is therefore network-free and safe in CI.
    """

    def test_discover_runs_as_a_script(self):
        import subprocess
        got = subprocess.run(
            [sys.executable, "pipeline/cli.py", "discover", "--issuer", "__none__", "--no-verify"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(got.returncode, 0, f"stderr:\n{got.stderr}")
        self.assertNotIn("ImportError", got.stderr)
        self.assertIn("Per-card source discovery", got.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
