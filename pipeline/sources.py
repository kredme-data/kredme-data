#!/usr/bin/env python3
"""
Resolve every active card to the one issuer URL the weekly refresh should fetch.

Usage:
    python3 -m pipeline.sources                     human coverage table
    python3 -m pipeline.sources --unresolved        + every card we cannot reach
    python3 -m pipeline.sources --json              machine-readable source list
    python3 -m pipeline.sources --cards seed/cards.json --overrides pipeline/sources_overrides.json

This module is READ-ONLY over seed/cards.json. It never writes a card, never
renames a rule and never converts points into a percentage — the scraper it
replaces did all three and the catalogue is still carrying the repairs.

Coverage is expected to be partial. Only about half of Indian issuers publish a
reward rate on a fetchable page at all, so "unresolved" here is a measurement of
the source landscape, not a bug to be papered over with an aggregator URL.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from pipeline import config as C

OVERRIDES_JSON = C.REPO / "pipeline" / "sources_overrides.json"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Source:
    """One card's fetch target. `url` is "" exactly when `reason` explains why."""

    card_id: str
    card_name: str
    issuer: str
    url: str
    reason: str


# ---------------------------------------------------------------------------
# Issuer slugs
#
# seed/cards.json spells 26 issuers 36 different ways: 'YES Bank' and 'Yes Bank',
# four spellings of BOBCARD, 'AU Bank' beside 'AU Small Finance Bank', and
# co-brand suffixes like '(co-branded with Aditya Birla Finance Limited)'.
# Matching whole words instead of exact strings means the 37th spelling lands on
# the right slug next week rather than on "unknown".
#
# Order is load-bearing. 'Federal Bank / BOBCARD (Scapia)' names two issuers, and
# the first one named is the one whose site publishes the T&C, so 'federal bank'
# is tested before 'bobcard'. Likewise 'sbi card' before the bare 'sc' fallback
# that catches the sc_* card ids.
# ---------------------------------------------------------------------------
ISSUER_MARKERS: tuple[tuple[str, str], ...] = (
    ("american express", "amex"),
    ("amex", "amex"),
    ("standard chartered", "sc"),
    ("hdfc", "hdfc"),
    ("icici", "icici"),
    ("sbi card", "sbi"),
    ("state bank of india", "sbi"),
    ("sbi", "sbi"),
    ("axis", "axis"),
    ("kotak", "kotak"),
    ("indusind", "indusind"),
    ("idfc", "idfc"),
    ("idbi", "idbi"),
    ("rbl", "rbl"),
    ("hsbc", "hsbc"),
    ("yes bank", "yes"),
    ("federal bank", "federal"),
    ("bobcard", "bobcard"),
    ("bank of baroda", "bobcard"),
    ("au small finance", "au"),
    ("au bank", "au"),
    ("city union", "city_union"),
    ("csb bank", "csb"),
    ("sbm bank", "sbm"),
    ("slice", "slice"),
    ("unity small finance", "unity"),
    ("fpl technologies", "onecard"),
    ("onecard", "onecard"),
    ("sc", "sc"),
)

# ---------------------------------------------------------------------------
# Issuer landing pages
#
# The last resort when a card carries no source of its own: the issuer's own
# credit-card index. It is a weak source — a landing page often states no rate at
# all — but it is an ISSUER page, and both prompts in schema.py are built to
# answer "this document says nothing" rather than to guess. An aggregator would
# always answer, which is precisely why none appears here.
#
# An issuer missing from this dict is an honest gap: CSB, City Union, SBM and
# Unity have no host on the config allowlist, so there is no URL we are permitted
# to fetch for them. They show up in the coverage report as no_issuer_landing.
#
# Every URL here is asserted against config.is_issuer_domain() by the test suite;
# a typo'd host would otherwise fail silently, one whole issuer at a time.
# ---------------------------------------------------------------------------
ISSUER_LANDING: dict[str, str] = {
    # RBI opened the restricted .bank.in TLD and the big issuers DID migrate — the
    # legacy .com hosts 301-redirect to .bank.in and serve byte-identical content.
    # So the 129 cards that could never be fetched were never a host problem: the
    # HOSTS were right and the PATHS were wrong. Paths did not survive the move
    # (/personal/pay/cards/credit-cards -> /credit-cards), and a wrong path on a
    # live host returns a 404 that looks exactly like a dead domain.
    #
    # Canonical .bank.in is used below to avoid a redirect hop. RBL is the exception:
    # www.rbl.bank.in/credit-cards 404s, so its listing stays on rblbank.com.
    #
    # Every URL here was fetched with pipeline.fetch before being written. Re-probe
    # before changing one, and never infer "alive" from a non-zero byte count —
    # Axis's 404 shell is LARGER than its real content page. Check the status code.
    "hdfc": "https://www.hdfc.bank.in/credit-cards",
    "icici": "https://www.icicibank.com/personal-banking/cards/credit-card",
    "sbi": "https://www.sbicard.com/en/personal/credit-cards.page",
    "axis": "https://www.axis.bank.in/retail/cards/credit-card",
    "kotak": "https://www.kotak.com/en/personal-banking/cards/credit-cards.html",
    "idfc": "https://www.idfcfirst.bank.in/credit-card",
    "rbl": "https://www.rblbank.com/category/credit-cards",   # plural; the singular 404s
    "indusind": "https://www.indusind.bank.in/in/personal/cards/credit-card.html",
    "yes": "https://www.yes.bank.in/personal-banking/yes-individual/cards/credit-cards",
    "au": "https://www.au.bank.in/personal-banking/credit-cards",
    "federal": "https://www.federalbank.co.in/credit-card",
    "hsbc": "https://www.hsbc.co.in/credit-cards/",
    "amex": "https://www.americanexpress.com/in/credit-cards/",
    "sc": "https://www.sc.com/in/credit-cards/",
    "bobcard": "https://www.bobcard.co.in/credit-card",
    "idbi": "https://www.idbibank.in/credit-card.aspx",
    "onecard": "https://www.onecard.app/",
    "slice": "https://www.sliceit.com/",
}

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
# Stops at the characters that end a URL inside JSON prose. Trailing sentence
# punctuation is trimmed separately because '...credit-card.html.' is common in
# hand-written source notes.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]}]+", re.IGNORECASE)
_URL_TRAILING = ".,;:!?"


def _warn(msg: str) -> None:
    """WARN goes to stderr so `--json` keeps stdout parseable by the next stage."""
    print(f"WARN  {msg}", file=sys.stderr)


def _words(text: str) -> str:
    """Lowercase, punctuation-free, space-padded — so `in` is a whole-word test."""
    return " " + _WORD_SPLIT.sub(" ", text.lower()).strip() + " "


def _match_issuer(text: str) -> str:
    if not text:
        return ""
    padded = _words(text)
    for phrase, slug in ISSUER_MARKERS:
        if f" {phrase} " in padded:
            return slug
    return ""


def _inner_card(entry: object) -> dict | None:
    """Accept either a full entry {"card": {...}, ...} or a bare card dict."""
    if not isinstance(entry, dict):
        return None
    inner = entry.get("card")
    if isinstance(inner, dict):
        return inner
    return entry if "id" in entry else None


def _is_active(inner: dict) -> bool:
    """Absent or non-zero means the card ships.

    Defaulting an absent flag to ACTIVE is deliberate: the failure mode of
    guessing wrong is a card we quietly stop refreshing, which nobody sees until
    its rate is a year stale.
    """
    value = inner.get("is_active", 1)
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("0", "false", "no", "")


def _embedded_urls(node: object, out: list[str] | None = None) -> list[str]:
    """Every http(s) URL anywhere in a card entry, in document order, deduped.

    A full walk rather than a fixed key list, because the seed stores source URLs
    on rules (`reward_rules[].source_url`), on exclusions, on milestones and
    inside `redemption_rules[]._sources` — and one card ships `exclusion_rules`
    as a prose string instead of a list, so any shape-assuming reader crashes.
    """
    if out is None:
        out = []
    if isinstance(node, str):
        for raw in _URL_RE.findall(node):
            url = raw.rstrip(_URL_TRAILING)
            if url and url not in out:
                out.append(url)
    elif isinstance(node, dict):
        for value in node.values():
            _embedded_urls(value, out)
    elif isinstance(node, list):
        for value in node:
            _embedded_urls(value, out)
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_cards(path: pathlib.Path | None = None) -> list[dict]:
    """Read seed/cards.json. Raises ValueError when it is not a list of entries."""
    path = pathlib.Path(path) if path is not None else C.CARDS_JSON
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"{path}: card seed not found") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: not valid JSON ({exc})") from None
    if not isinstance(data, list):
        raise ValueError(
            f"{path}: expected a JSON list of card entries, got {type(data).__name__}"
        )
    return data


def load_overrides(path: pathlib.Path) -> dict[str, str]:
    """Read the hand-pinned card_id -> URL map.

    A missing file is normal (the map is optional). A present but unreadable one
    is not: silently returning {} there would drop every verified deep link and
    quietly downgrade 14 cards to their issuer's landing page.
    """
    path = pathlib.Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: unreadable overrides file ({exc})") from None
    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: expected a JSON object of card_id -> url, got {type(data).__name__}"
        )

    out: dict[str, str] = {}
    for card_id, url in data.items():
        if card_id.startswith("_"):
            continue  # _comment and friends are documentation, not overrides
        if not isinstance(url, str) or not url.strip():
            _warn(f"override {card_id}: not a URL string, skipped")
            continue
        url = url.strip()
        if not C.is_issuer_domain(url):
            host = urlparse(url).hostname or url
            _warn(f"override {card_id}: {host} is not an issuer domain, skipped")
            continue
        out[card_id] = url
    return out


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def issuer_of(card: dict) -> str:
    """Stable lowercase issuer slug for a card entry, or "unknown"."""
    inner = _inner_card(card)
    if inner is None:
        return "unknown"
    # The issuer field is curated; the id is derived from it and survives the
    # occasional blank field, so it is the fallback rather than the primary.
    for field in ("issuer", "bank", "issuer_name", "id"):
        slug = _match_issuer(str(inner.get(field) or ""))
        if slug:
            return slug
    return "unknown"


def _pick_url(card_id: str, entry: dict, issuer: str, overrides: dict[str, str]) -> str:
    override = overrides.get(card_id)
    if isinstance(override, str) and override.strip():
        return override.strip()

    urls = _embedded_urls(entry)
    for url in urls:
        if C.is_issuer_domain(url):
            return url
    if urls:
        # The card stores sources, but none of them is an issuer. Return the
        # first anyway so the allowlist gate names it in the coverage report.
        # Falling through to the landing page here would hide an aggregator URL
        # sitting in the seed, which is the exact failure this gate exists for.
        return urls[0]

    return ISSUER_LANDING.get(issuer, "")


def resolve_sources(cards: list[dict], overrides: dict[str, str] | None = None) -> list[Source]:
    """One Source per ACTIVE card, in seed order.

    Resolution order is override -> URL already on the card -> issuer landing
    page. Whichever wins must clear config.is_issuer_domain(); if it does not the
    card comes back unresolved rather than resolved to a second-best URL.
    """
    overrides = overrides or {}
    out: list[Source] = []

    for entry in cards:
        inner = _inner_card(entry)
        if inner is None:
            _warn(f"skipped malformed card entry: {type(entry).__name__}")
            continue
        card_id = str(inner.get("id") or "").strip()
        if not card_id:
            _warn(f"skipped card entry with no id: {inner.get('card_name')!r}")
            continue
        if not _is_active(inner):
            continue

        card_name = str(inner.get("card_name") or "")
        issuer = issuer_of(entry)
        url = _pick_url(card_id, entry, issuer, overrides)

        if not url:
            reason = (
                "unknown_issuer" if issuer == "unknown" else f"no_issuer_landing: {issuer}"
            )
        elif not C.is_issuer_domain(url):
            host = urlparse(url).hostname or url
            reason = f"not_issuer_domain: {host}"
            url = ""
        else:
            reason = ""

        out.append(
            Source(card_id=card_id, card_name=card_name, issuer=issuer, url=url, reason=reason)
        )

    return out


# The set of URLs that are an issuer's card-LISTING page rather than any one card's
# terms. Computed once, from the same dict resolve_sources falls back to.
_LANDING_URLS = frozenset(ISSUER_LANDING.values())


def is_card_specific(state_sources: dict, card_id: str) -> bool:
    """Is the URL we hashed for this card that card's OWN page?

    False for an issuer landing page, and false for any URL shared with another card —
    which is the same thing seen from the other side, because the only reason two cards
    share a URL is that neither was resolved to its own terms page.

    This is what decides whether a `done` verdict is allowed to suppress a re-read
    indefinitely. 108 of 139 retired cards were pinned to a page shared with up to 20
    others; for those, "the bytes have not moved" is a statement about the issuer's
    navigation, not about the card.
    """
    entry = state_sources.get(card_id)
    if not isinstance(entry, dict):
        return False
    url = entry.get("url") or ""
    if not url or url in _LANDING_URLS:
        return False
    users = sum(
        1
        for cid, v in state_sources.items()
        if not cid.startswith("__watch__")
        and isinstance(v, dict)
        and v.get("url") == url
    )
    return users <= 1


def coverage_report(sources: list[Source]) -> dict:
    """How much of the catalogue we can actually reach, and what blocks the rest."""
    by_reason: dict[str, int] = {}
    by_issuer: dict[str, dict[str, int]] = {}
    resolved = 0

    for src in sources:
        bucket = by_issuer.setdefault(src.issuer, {"resolved": 0, "unresolved": 0})
        if src.url:
            resolved += 1
            bucket["resolved"] += 1
        else:
            bucket["unresolved"] += 1
            by_reason[src.reason] = by_reason.get(src.reason, 0) + 1

    return {
        "total": len(sources),
        "resolved": resolved,
        "unresolved": len(sources) - resolved,
        "by_reason": by_reason,
        "by_issuer": by_issuer,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_table(sources: list[Source], report: dict, show_unresolved: bool) -> None:
    total = report["total"]
    pct = (100.0 * report["resolved"] / total) if total else 0.0
    print(f"\nSource coverage — {total} active cards")
    print(f"  resolved     {report['resolved']:4d}   {pct:5.1f}%")
    print(f"  unresolved   {report['unresolved']:4d}   {100.0 - pct:5.1f}%")

    if report["by_reason"]:
        print("\n  why unresolved")
        for reason, count in sorted(report["by_reason"].items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {count:4d}  {reason}")

    print("\n  by issuer")
    print(f"    {'issuer':<12} {'resolved':>8} {'unresolved':>11}")
    for issuer, counts in sorted(report["by_issuer"].items()):
        print(f"    {issuer:<12} {counts['resolved']:>8} {counts['unresolved']:>11}")

    if show_unresolved:
        print("\n  unresolved cards")
        for src in sources:
            if not src.url:
                print(f"    {src.card_id:<48} {src.reason}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline/sources.py",
        description="Resolve each active card to the issuer URL the weekly refresh fetches.",
    )
    parser.add_argument("--json", action="store_true", help="dump the resolved list as JSON")
    parser.add_argument("--unresolved", action="store_true", help="list every unresolved card")
    parser.add_argument("--cards", type=pathlib.Path, default=None, help="alternate cards.json")
    parser.add_argument(
        "--overrides", type=pathlib.Path, default=OVERRIDES_JSON, help="alternate overrides file"
    )
    args = parser.parse_args()

    # Low coverage is information, so it exits 0. An unreadable seed is a data
    # error and exits 1 — at that point there is no report to be informed by.
    try:
        cards = load_cards(args.cards)
        overrides = load_overrides(args.overrides)
    except (OSError, ValueError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    sources = resolve_sources(cards, overrides)
    report = coverage_report(sources)

    if args.json:
        print(json.dumps([asdict(s) for s in sources], indent=2, ensure_ascii=False))
    else:
        _print_table(sources, report, args.unresolved)

    return 0


if __name__ == "__main__":
    sys.exit(main())
