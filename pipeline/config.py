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
VERIFY_MAX_TOKENS = 8000

# max_tokens is a ceiling, not a forecast. Pricing a batch at max_tokens overstates
# the bill by roughly an order of magnitude, which is misleading in the other
# direction: a founder deciding whether to approve a full sweep needs the number it
# will probably cost as well as the number it cannot exceed.
#
# These are the observed size of a real schema-constrained response — an extraction
# carrying 5-15 observations with their verbatim quotes, and a verdict list of the
# same length. Re-measure them from a real batch's usage and update; they are an
# estimate and the ceiling remains the guarantee.
TYPICAL_OUTPUT_TOKENS = {"extract": 2500, "verify": 1200, "news": 1500}
DEFAULT_TYPICAL_OUTPUT_TOKENS = 2000

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
    "onecard.app", "slice.it", "jupiter.money",
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
    low = text.lower()
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
WATCH_PAGES = (
    ("axis", "https://www.axis.bank.in/support/terms-and-conditions/credit-card"),
    ("hdfc", "https://www.hdfc.bank.in/personal/pay/cards/credit-cards"),
    ("sbi", "https://www.sbicard.com/en/personal/customer-care/important-information.page"),
    ("icici", "https://www.icicibank.com/personal-banking/cards/credit-card/upcoming-changes"),
    ("idfc", "https://www.idfcfirst.bank.in/personal-banking/cards/credit-card"),
    ("indusind", "https://www.indusind.bank.in/in/personal/cards/credit-card.html"),
    ("yes", "https://www.yes.bank.in/personal-banking/yes-first/cards/credit-card"),
    ("bobcard", "https://www.bobcard.co.in/service-charges"),
    ("au", "https://www.au.bank.in/credit-card"),
    ("kotak", "https://www.kotak.com/en/personal-banking/cards/credit-cards.html"),
    ("rbl", "https://www.rblbank.com/category/service-charges"),
    ("hsbc", "https://www.hsbc.co.in/credit-cards/"),
)
