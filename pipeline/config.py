#!/usr/bin/env python3
"""
Shared configuration for the weekly refresh + news pipelines.

Usage:
    from pipeline import config as C

Stdlib only. Nothing here touches the network or reads secrets at import time —
importing this module must stay free so the test suite runs on a bare Python.
"""
from __future__ import annotations

import os
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SEED_DIR = REPO / "seed"
CARDS_JSON = SEED_DIR / "cards.json"
MANIFEST_JSON = SEED_DIR / "manifest.json"
MERCHANTS_JSON = SEED_DIR / "merchants.json"
NEWS_FEED = REPO / "news" / "feed.json"

# Committed pipeline state. This IS tracked in git — the content hashes are what
# make the weekly run incremental, and a run that cannot read last week's hashes
# would re-extract all 380 cards and pay for it.
STATE_DIR = REPO / "pipeline" / "state"
SOURCE_STATE = STATE_DIR / "sources.json"        # card_id -> {url, sha256, fetched_at, status}
BATCH_STATE = STATE_DIR / "batch.json"           # in-flight Anthropic batch handles
METRICS_HISTORY = STATE_DIR / "metrics.jsonl"    # one line per weekly run

# Pass-1 output, tracked. It has to be: stage 2 (submit verification) and stage 3
# (apply verdicts) are separate cron runs on separate GitHub runners, so anything left
# in gitignored scratch is gone by the time the next stage looks for it. Committing it
# also gives an audit trail of what the model actually claimed, which is worth having
# the first time someone asks why a rate changed.
EXTRACTIONS = STATE_DIR / "extractions.json"

# Scratch. Gitignored — regenerated every run.
WORK_DIR = REPO / ".pipeline-work"
FETCH_CACHE = WORK_DIR / "fetched"
CANDIDATES = WORK_DIR / "candidates"

# ---------------------------------------------------------------------------
# Models
#
# claude-opus-5 everywhere. Both stages are correctness-critical: a wrong
# extraction becomes a wrong reward rate on a card a real person is holding, and
# a wrong verification lets it through. Batch pricing already halves the bill and
# the content-hash gate means we only pay for cards whose source actually moved.
# Downgrading is a cost/quality trade the founder should make explicitly, not one
# this file should make silently — flip EXTRACT_MODEL to claude-sonnet-5 if so.
# ---------------------------------------------------------------------------
EXTRACT_MODEL = os.environ.get("KREDME_EXTRACT_MODEL", "claude-opus-5")
VERIFY_MODEL = os.environ.get("KREDME_VERIFY_MODEL", "claude-opus-5")

EXTRACT_EFFORT = "high"
VERIFY_EFFORT = "xhigh"   # the refute pass is where being wrong is most expensive

EXTRACT_MAX_TOKENS = 16000
# 16000, was 8000. The first real verification batch came back with 154 of 223
# responses unusable, most of them "malformed JSON: Unterminated string" — the
# signature of a reply cut off mid-token, not of a model producing bad JSON.
# Measured output on that batch averaged ~5,500 tokens against an 8,000 ceiling,
# so the longer verdict lists ran into it. Every truncated reply was billed in
# full and then discarded.
VERIFY_MAX_TOKENS = 16000

# max_tokens is a ceiling, not a forecast. Pricing a batch at max_tokens overstates
# the bill by roughly an order of magnitude, which is misleading in the other
# direction: a founder deciding whether to approve a full sweep needs the number it
# will probably cost as well as the number it cannot exceed.
#
# RE-MEASURED 2026-08-18 against the first real bill, as the previous note asked.
# Was {extract: 2500, verify: 1200, news: 1500} — a pre-flight guess, and it under-
# reported by 38%: est_usd said $68.63 for the 17-Aug cycle, the console billed
# $94.55. Working back, $25.92 / $12.50-per-M-output = 2.07M more output tokens than
# assumed, over 594 requests — about 3,500 each.
#
# Prompt caching was ruled out as the cause arithmetically: the cached prefix is only
# ~1,327 tokens, so billing it in full on every request instead of reading it from
# cache costs $1.10 across the whole batch. The gap is output, nothing else.
#
# These figures make est_usd land near the real bill. They remain an estimate; the
# guarantee is still est_usd_ceiling, and MAX_BATCH_USD below is the hard stop.
TYPICAL_OUTPUT_TOKENS = {"extract": 5500, "verify": 5500, "news": 3000}
DEFAULT_TYPICAL_OUTPUT_TOKENS = 5000

# Hard spend ceiling per batch. submit() refuses above this and exits rather than
# asking; a run that silently bills $95 twice is the failure being prevented, and it
# has already happened once. Raise it deliberately with --max-usd, never by editing
# this in passing. None disables the check.
#
# 25, priced off the real 17-Aug batch rather than guessed. Extraction of the full
# 371-card catalogue estimates $59.74 (input $34.23 + output $25.51), and the two
# passes together billed $94.55. Per-card that is ~$0.16 to extract:
#
#      20 cards  ~$3.22        150 cards  ~$24.15
#      50 cards  ~$8.05        371 cards  ~$59.74   <- a full sweep
#
# So 25 clears an ordinary incremental week by roughly 3x and stops a full sweep
# dead. 60 was the first value here and it was wrong in the way thresholds usually
# are — it sat ON the number it was meant to catch, and would not have fired.
MAX_BATCH_USD = 25.0

# Published list prices, $/1M tokens. Batch halves both.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
BATCH_DISCOUNT = 0.5
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

# ---------------------------------------------------------------------------
# Issuer domain allowlist.
#
# This is the rule that stops CardExpert/CardInsider numbers re-entering the
# catalogue. It is enforced by validate_sources() and by CI, so "issuer sources
# only" is a gate rather than something a future operator has to remember.
#
# Indian banks migrated to the RBI-restricted .bank.in TLD during 2025-26, so
# both the legacy and the .bank.in host are listed where the migration happened.
# ---------------------------------------------------------------------------
ISSUER_DOMAINS = frozenset({
    "hdfcbank.com", "hdfc.bank.in", "smartbuy.hdfcbank.com", "offers.smartbuy.hdfc.bank.in",
    "icicibank.com", "icici.bank.in",
    "sbicard.com", "sbi.bank.in",
    "axisbank.com", "axis.bank.in", "campaign.axis.bank.in",
    "kotak.com", "kotak.bank.in",
    "idfcfirstbank.com", "idfcfirst.bank.in",
    "rblbank.com", "rbl.bank.in",
    "indusind.com", "indusind.bank.in",
    "yesbank.in", "yes.bank.in",
    "aubank.in", "au.bank.in",
    "federalbank.co.in", "federal.bank.in",
    "hsbc.co.in",
    "americanexpress.com",
    "sc.com",
    "bobcard.co.in", "media.bobcard.co.in", "bankofbaroda.in",
    "idbibank.in", "idbi.bank.in",
    "pnbindia.in", "unionbankofindia.co.in", "canarabank.com",
    "bankofindia.co.in", "centralbankofindia.co.in", "indianbank.in",
    "citibank.co.in", "dbs.com", "aubankcards.com",
    # slice.it no longer resolves; the live host is sliceit.com. Both are listed so
    # an old override or a card carrying the legacy URL still clears the guard.
    "onecard.app", "slice.it", "sliceit.com", "jupiter.money",
    "rbi.org.in", "npci.org.in",
})


def is_issuer_domain(url: str) -> bool:
    """True when `url`'s host is an issuer/regulator domain on the allowlist.

    Subdomains of an allowlisted host pass (`www.hdfc.bank.in` -> `hdfc.bank.in`),
    but a lookalike registered domain does not: `hdfcbank.com.evil.tld` fails
    because we match on suffix *segments*, never on a bare substring.
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    if host in ISSUER_DOMAINS:
        return True
    return any(host.endswith("." + d) for d in ISSUER_DOMAINS)


# ---------------------------------------------------------------------------
# Fetch behaviour
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT_S = 45
FETCH_RETRIES = 2
MAX_FETCH_BYTES = 12 * 1024 * 1024      # an issuer PDF over 12 MB is a scan, not a T&C
POLITE_DELAY_S = 1.0                    # between requests to the same host

# How many issuer HOSTS to fetch from at once. Concurrency is per host, never
# within one: `fetch_many` gives each host a single worker that walks its own
# URLs in order with POLITE_DELAY_S between them, so raising this never
# increases the request rate any one issuer sees.
#
# Why this exists at all: the design assumed the refresh "submits and exits in
# minutes", which was true when 373 cards resolved to 35 shared landing pages.
# Per-card source discovery took that to 196 distinct URLs, the sequential
# fetch grew past an hour, and `pipeline-advance` — which runs every 2 hours in
# the same concurrency group — cancelled the weekly refresh mid-fetch.
#
# 20 hosts exist today and the busiest holds 38 distinct URLs, so that host is
# the critical path and more workers than that buys nothing.
MAX_FETCH_WORKERS = int(os.environ.get("KREDME_FETCH_WORKERS", "8"))

# ---------------------------------------------------------------------------
# Extraction limits (these bound the per-card token bill)
# ---------------------------------------------------------------------------
MAX_PAGE_CHARS = 60_000
MAX_PDF_CHARS = 90_000
MAX_LINKED_PDFS = 4

# ---------------------------------------------------------------------------
# Numeric guards. These mirror tools/kredme.py so a pipeline-proposed change
# cannot pass here and then fail the publish gate.
# ---------------------------------------------------------------------------
RATE_CEILING_PCT = 40.0      # hard, unwaivable — HDFC SmartBuy 10X genuinely ~33%
RATE_FLOOR_PCT = 0.0
MAX_AUTO_DELTA_PCT = 50.0    # a proposed change moving a rate >50% relatively needs a human

# Marketing weasels. A number lifted from a sentence containing one of these is
# an aspiration, not a mechanic. This is the "never source a rate from an
# 'up to' sentence" rule from the handover, made executable.
WEASEL_PHRASES = (
    "up to", "upto", "as high as", "as much as", "maximum of",
    "can earn up", "earn up to", "save up to", "worth up to",
)


def contains_weasel(text: str) -> bool:
    """True if `text` reads like marketing rather than a stated mechanic."""
    if not text:
        return False
    # Collapse every run of whitespace to one space BEFORE matching. Issuer PDFs reach
    # us through `pdftotext -layout`, which preserves line wraps and column gaps, so a
    # quote spanning "...can get up\nto 0.8% cashback..." would otherwise walk straight
    # past this guard and auto-apply a marketing number to a live card. A tab, a double
    # space or an NBSP does the same. str.split() with no argument already treats NBSP
    # and every other Unicode space as whitespace.
    low = " ".join(text.split()).lower()
    return any(p in low for p in WEASEL_PHRASES)


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
# Keys the shipped app's NewsArticle.fromJson actually reads. Anything outside
# this set is silently dropped by the app, so emitting it is a no-op.
NEWS_VALID_KEYS = frozenset({
    "id", "title", "summary", "category", "severity", "source", "source_url",
    "published_at", "expiry_date", "affected_cards", "affected_issuers",
    "tags", "action_text",
})
NEWS_SEVERITIES = frozenset({"info", "positive", "warning", "negative"})

# The app compares only the leading integer of the version string and refetches
# on a strict increase (news_feed_service.dart:126-127). Minor bumps are invisible
# and a "v" prefix parses to 0, which stops the feed loading for everyone.
NEWS_MAJOR_BUMP_REQUIRED = True

# Pages that announce changes. The news watcher polls these, not the card pages —
# issuers publish revisions on a small number of stable notice URLs.
# Probed 16-Aug-2026 with pipeline.fetch (NOT curl — the two disagree, see below).
# Nine of the twelve entries here were dead: eight 404s and a YES page that returned
# 200 with zero extractable text. A dead watch page is the worst kind of defect in
# this pipeline because it fails silently — the poller finds no change, forever, and
# reports success every morning.
#
# Two traps worth keeping:
#  - Axis's 404 shell is ~735KB, LARGER than its real 734KB announcements page. Any
#    liveness check based on response size passes on a page serving nothing. Check
#    the status code, and grep for a content marker like "revision is effective from".
#  - curl and urllib disagree in BOTH directions. AU and RBL bot-block curl with a
#    403 but serve urllib fine; bobcard serves curl but fails urllib. Verify with the
#    fetcher CI actually uses, not with whatever is on your shell.
#
# Several issuers publish revisions only inside a MITC PDF and have no HTML notice
# index at all — those entries point straight at the PDF, which fetch.py reads.
WATCH_PAGES = (
    ("axis", "https://www.axis.bank.in/important-links/credit-card/important-announcement-on-credit-card"),
    ("hdfc", "https://www.hdfc.bank.in/content/dam/hdfcbankpws/in/en/personal-banking/discover-products/cards/credit-cards/personal-mitc/mitc-in-english.pdf"),
    ("sbi", "https://www.sbicard.com/en/customer-notices.page"),
    ("icici", "https://www.icici.bank.in/personal-banking/cards/credit-card/upcoming-changes-features-and-charges"),
    ("idfc", "https://www.idfcfirst.bank.in/credit-card/mitc"),
    ("indusind", "https://www.indusind.bank.in/in/personal/cards/credit-card.html"),
    ("yes", "https://www.yes.bank.in/sites/web/content/published/api/v1.1/assets/CONTAA57595B2EF245259C4C623B1F7D33B3/native/ybl_mitc_pdf.pdf"),
    ("au", "https://www.au.bank.in/notice-board"),
    ("kotak", "https://www.kotak.com/en/personal-banking/cards/credit-cards.html"),
    ("rbl", "https://www.rbl.bank.in/service-charges"),
    ("hsbc", "https://www.hsbc.co.in/credit-cards/"),
    # bobcard is deliberately absent. Its server sends the leaf certificate without
    # the GlobalSign intermediate, so the chain cannot be verified. Browsers and curl
    # recover by chasing the AIA extension; urllib does not, and neither will CI.
    # Watching it would mean disabling certificate verification for every fetch.
    # Its 19 cards are unwatched until BOBCARD fixes their chain.
)
