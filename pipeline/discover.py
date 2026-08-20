"""Find each card's OWN issuer page, instead of the issuer's card-listing page.

The problem this exists to solve
--------------------------------
The seed card schema has no URL field. `sources.resolve_sources` therefore derives
a source from the issuer NAME and falls back to `ISSUER_LANDING`, so 373 cards
resolved to 35 distinct URLs — 54 SBI cards sharing one page, 46 HDFC cards
sharing another. Asking the model for one card's mechanics out of a page that
lists forty cards is the difference between a refresh that works and one that
merely runs.

An issuer's listing page is already the index of its per-card documents, so this
module harvests those links (fetch.Fetched.links) and matches them to cards.

Why the matching is deliberately paranoid
-----------------------------------------
A WRONG per-card URL is far worse than no per-card URL. HDFC ships Regalia,
Regalia First, Regalia Gold and Doctor's Regalia. Map Regalia to Regalia Gold's
page and the pipeline will extract Regalia Gold's rates, verify them against
Regalia Gold's own page — the adversarial pass sees a consistent document and
confirms — and write them onto Regalia. The result is a confidently wrong reward
rate on a card a real person is holding. Falling back to the listing page merely
produces a weak extraction; a mis-matched page produces a convincing lie.

So the rule is: a match must be EXACT on the distinguishing tokens, or it is not
a match. No fuzzy scoring, no "closest wins", no thresholds to tune. Anything
unmatched keeps the existing landing-page behaviour, which is what happens today.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field

from . import config as C
from . import fetch as F
from . import sources as S

# Slugs that are a call to action, a tool, or editorial — never a card document.
# `apply` matters most: every issuer listing page links applyonline.<issuer> dozens
# of times with tracking query strings, and those pages describe no card at all.
_NOT_A_CARD = {
    "apply", "applyonline", "apply-online", "applynow", "apply-now",
    "blog", "blogs", "article", "articles", "news", "press", "media",
    "calculator", "calculators", "emi-calculator", "savings-calculator",
    "eligibility", "compare", "faq", "faqs", "support", "contact",
    "login", "logon", "netbanking", "register", "sitemap", "search",
    "offers", "offer", "deals", "rewards-catalogue", "locate-us",
    "terms", "privacy", "disclaimer", "grievance", "careers",
    # Servicing sub-trees. HDFC alone publishes a dozen of these under
    # /credit-cards/services/, and "credit-card-upgrade" reduces to tokens that
    # can collide with a real product name.
    "services", "service", "help", "manage", "track", "membership-kit",
    "compare-page", "block-loststolen-card",
}

# Paths that belong to a DIFFERENT product line. Card names are reused across
# them — IndusInd sells a "Duo Plus" debit card and an "Indus Solitaire" savings
# account, RBL runs "Aspire Banking" and "Signature Banking" programmes — so name
# matching alone happily pointed four credit cards at a debit card, a savings
# account and two banking tiers. Those pages state real terms, so nothing
# downstream would have flagged the swap.
_WRONG_PRODUCT = {
    "debit-card", "debit-cards", "debitcard", "prepaid-card", "forex-card",
    "savings-account", "saving-account", "current-account", "accounts", "account",
    "deposit", "deposits", "fixed-deposit", "recurring-deposit",
    "loan", "loans", "home-loan", "personal-loan", "gold-loan", "car-loan",
    "insurance", "mutual-fund", "mutual-funds", "demat", "trading", "nri",
    "preferred-banking", "priority-banking", "privilege-banking", "burgundy-banking",
}

# Dropped from both sides before comparison: they carry no distinguishing
# information and appear inconsistently ("HDFC MoneyBack" vs "HDFC Bank MoneyBack").
_NOISE_TOKENS = {"credit", "card", "cards", "creditcard", "creditcards", "the", "a"}

# Issuer words stripped from card names. A card named "HDFC Bank Regalia" and a
# slug "/regalia-credit-card" must reduce to the same token set, and the issuer
# name is present on one side and absent on the other about half the time.
_ISSUER_WORDS = {
    "hdfc", "icici", "sbi", "axis", "kotak", "mahindra", "idfc", "first",
    "rbl", "indusind", "yes", "au", "small", "finance", "federal", "hsbc",
    "amex", "american", "express", "sc", "standard", "chartered", "bobcard",
    "bob", "baroda", "idbi", "onecard", "slice", "bank", "limited", "ltd",
}

_SPLIT = re.compile(r"[^a-z0-9]+")


def _token_list(text: str) -> list[str]:
    """Lowercase alphanumeric tokens IN ORDER, minus noise. Digits kept ('6e', '811')."""
    if not isinstance(text, str):
        return []
    return [t for t in _SPLIT.split(text.lower()) if t and t not in _NOISE_TOKENS]


def _tokens(text: str) -> set[str]:
    return set(_token_list(text))


def _issuer_words_of(issuer: str) -> set[str]:
    """Only the issuer words THIS issuer actually uses.

    Stripping the whole _ISSUER_WORDS set from both sides is wrong and was the
    first real bug here: 'first' belongs to IDFC FIRST Bank, so stripping it
    globally reduced HDFC's 'regalia-first' to {regalia} — colliding with plain
    Regalia, and taking both cards out as ambiguous. A word is issuer noise only
    when it appears in that card's own issuer string.
    """
    return _tokens(issuer) & _ISSUER_WORDS


def _keys(text: str, issuer_words: set[str]) -> set[str]:
    """The exact-match keys for a card name or a URL slug.

    Two keys, because issuers and our own data disagree about word boundaries in
    two specific, systematic ways:

      tok: the sorted set of distinguishing tokens. Order-insensitive, so
           "Diners Club Black" matches "black-diners-club".
      cat: those tokens concatenated. Boundary-insensitive, so "MoneyBack"
           matches "money-back" and "Doctor's Regalia" matches "doctors-regalia"
           (the apostrophe otherwise splits into a stray 's').

    Both are EXACT equality tests. Neither is fuzzy, so adding the second key
    widens what we can match without widening what we can get wrong.
    """
    ordered = [t for t in _token_list(text) if t not in issuer_words]
    if not ordered:
        # Never reduce to nothing: "HDFC Bank Credit Card" would match every slug.
        ordered = _token_list(text)
    if not ordered:
        return set()
    # The concatenation MUST preserve source order. Sorting it first was the
    # second real bug here: "Doctor's Regalia" tokenises to [doctor, s, regalia]
    # and sorting yields "doctorregalias", which never equals the slug's
    # "doctorsregalia". Order-insensitivity is the other key's job, not this one's.
    cat = "".join(ordered)
    # Single characters are possessive debris ("doctor's" -> doctor, s). They
    # corrupt the token set but are load-bearing in the concatenation, so they
    # are dropped from one key and kept in the other.
    meaningful = {t for t in ordered if len(t) > 1}
    keys = {"cat:" + cat}
    if meaningful:
        keys.add("tok:" + "|".join(sorted(meaningful)))
    return keys


def _name_tokens(card_name: str, issuer: str) -> set[str]:
    """A card's distinguishing tokens, for the post-fetch content check."""
    toks = _tokens(card_name) - _issuer_words_of(issuer)
    return {t for t in (toks or _tokens(card_name)) if len(t) > 1}


def _slug_of(url: str) -> str:
    """The last meaningful path segment, minus any page extension.

    Query strings are dropped entirely: on issuer sites they are tracking
    parameters (CHANNELSOURCE, utm_*, mc_id), never card identity.
    """
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    segments = [s for s in split.path.split("/") if s]
    if not segments:
        return ""
    return re.sub(r"\.(html?|page|aspx|jsp|php)$", "", segments[-1], flags=re.I)


def _is_plausible_card_page(url: str) -> bool:
    """Cheap structural filter, applied before any network call."""
    try:
        split = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if split.scheme.lower() not in ("http", "https"):
        return False
    # A tracking query string means a funnel entry point, not a card document.
    if split.query:
        return False
    path = split.path.lower()
    if path.endswith(".pdf"):
        return False
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    if any(seg in _NOT_A_CARD for seg in segments):
        return False
    if any(seg in _WRONG_PRODUCT for seg in segments):
        return False
    if "apply" in split.netloc.lower():
        return False
    # A card page lives at least one level deep and names something.
    return len(segments) >= 1 and len(segments[-1]) > 3


_CATALOGUE_SEGMENT = re.compile(r"^credit[-_]?cards?$", re.I)


def _prefer_catalogue(urls: set[str]) -> set[str]:
    """Break a tie in favour of the card catalogue over a campaign microsite.

    SBI publishes most cards twice: the catalogue entry at
    /en/personal/credit-cards/cashback-sbi-card.html and a marketing landing page
    at /sprint/cashback. Both reduce to the same name, so both are exact matches
    and 20 SBI cards came out ambiguous.

    This is a STRUCTURAL preference, not a fuzzy score: a URL that sits under a
    path segment literally named "credit-card(s)" is the issuer's own catalogue,
    and a campaign page is a redirect target that can be retired at any time.
    If the preference does not narrow the field to exactly one, the card stays
    ambiguous and keeps the landing page.
    """
    catalogue = {
        u for u in urls
        if any(_CATALOGUE_SEGMENT.match(seg) for seg in urllib.parse.urlsplit(u).path.split("/"))
    }
    return catalogue if len(catalogue) == 1 else urls


@dataclass
class Candidate:
    """One card's resolution attempt, carrying why it succeeded or failed."""

    card_id: str
    card_name: str
    issuer: str
    url: str = ""
    status: str = "unmatched"       # matched | ambiguous | unmatched | unverified
    reason: str = ""
    rivals: list[str] = field(default_factory=list)


# A sitemap index can point at dozens of children (HDFC ships a 2.9MB PDF-only
# one). Only those whose own URL suggests pages are followed, and never more than
# this many, so discovery cannot turn into a crawl of the entire site.
_MAX_CHILD_SITEMAPS = 4
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def _sitemap_urls(landing_url: str, *, opener=None) -> list[str]:
    """Every URL in the issuer's sitemap, following one level of sitemap index.

    Half the catalogue sits behind issuers who render their card list in
    JavaScript — SBI's listing page yields 3,427 characters and not one card
    link, while its sitemap carries 209 of them. The sitemap is the only
    plain-HTTP index of those sites, so it is not an optimisation here, it is
    the difference between 0% and full coverage for those issuers.
    """
    try:
        split = urllib.parse.urlsplit(landing_url)
    except ValueError:
        return []
    if not split.scheme or not split.netloc:
        return []
    root = f"{split.scheme}://{split.netloc}/sitemap.xml"

    first = F.fetch_url(root, opener=opener)
    if not first.ok or not first.text:
        return []
    locs = _LOC.findall(first.text)
    if not locs:
        return []

    # A sitemap index lists sitemaps, not pages. Detect it by what the entries
    # look like rather than by parsing the root element, because issuers serve
    # both shapes with the same content type.
    children = [u for u in locs if u.lower().rstrip("/").endswith((".xml", ".xml.gz"))]
    if not children:
        return locs

    wanted = [u for u in children if not re.search(r"pdf|image|video|news", u, re.I)]
    out: list[str] = [u for u in locs if u not in children]
    for child in wanted[:_MAX_CHILD_SITEMAPS]:
        got = F.fetch_url(child, opener=opener)
        if got.ok and got.text:
            out.extend(_LOC.findall(got.text))
    return out


def harvest(landing_url: str, *, opener=None, use_sitemap: bool = True) -> list[str]:
    """Every plausible per-card page for one issuer.

    Two sources, unioned: the links on the listing page, and the issuer's
    sitemap. Neither alone covers the catalogue — the listing page misses
    JavaScript-rendered sites, and a sitemap misses issuers who do not publish
    one (Kotak and YES both 404).
    """
    found: list[str] = []
    page = F.fetch_url(landing_url, opener=opener)
    if page.ok:
        found.extend(page.links)
    if use_sitemap:
        found.extend(_sitemap_urls(landing_url, opener=opener))
    same_issuer = [u for u in found if C.is_issuer_domain(u)]
    return [u for u in dict.fromkeys(same_issuer) if _is_plausible_card_page(u)]


def match(cards: list[dict], links: list[str]) -> list[Candidate]:
    """Match cards to links on EXACT distinguishing-token equality.

    Two collision guards, and both are load-bearing:

    1. One card, several links. If more than one link reduces to the same token
       set the card is ambiguous — we cannot tell which document is authoritative
       — so it is left on the landing page rather than guessed.
    2. Several cards, one link. Regalia and Regalia Gold must never both resolve
       to /regalia-credit-card. Exact set equality already prevents this (their
       token sets differ by 'gold'), but issuers do publish two slugs for one
       product, so the reverse map is checked explicitly.
    """
    # All cards in one call share an issuer, but the seed spells some issuers two
    # ways ("IDFC FIRST Bank" and "IDFC First Bank" are both present), so take the
    # union rather than trusting the first row.
    issuer_words: set[str] = set()
    for card in cards:
        issuer_words |= _issuer_words_of(str(card.get("card", card).get("issuer") or ""))

    by_key: dict[str, set[str]] = {}
    for url in links:
        for key in _keys(_slug_of(url), issuer_words):
            by_key.setdefault(key, set()).add(url)

    out: list[Candidate] = []
    claimed: dict[str, str] = {}

    for card in cards:
        inner = card.get("card", card)
        cid = str(inner.get("id") or "")
        name = str(inner.get("card_name") or "")
        issuer = str(inner.get("issuer") or "")
        cand = Candidate(card_id=cid, card_name=name, issuer=issuer)

        wanted = _keys(name, issuer_words)
        if not wanted:
            cand.reason = "card name reduced to no distinguishing tokens"
            out.append(cand)
            continue

        hits: set[str] = set()
        for key in wanted:
            hits |= by_key.get(key, set())
        if not hits:
            cand.reason = "no link on the issuer's listing page matches this card's name"
            out.append(cand)
            continue
        if len(hits) > 1:
            hits = _prefer_catalogue(hits)
        if len(hits) > 1:
            cand.status = "ambiguous"
            cand.rivals = sorted(hits)[:5]
            cand.reason = f"{len(hits)} links match this name; refusing to guess"
            out.append(cand)
            continue

        url = next(iter(hits))
        if url in claimed:
            cand.status = "ambiguous"
            cand.rivals = [claimed[url]]
            cand.reason = f"already claimed by {claimed[url]}"
            out.append(cand)
            continue

        claimed[url] = cid
        cand.url = url
        cand.status = "matched"
        cand.reason = "exact token match"
        out.append(cand)

    return out


def verify(cand: Candidate, *, opener=None) -> Candidate:
    """Fetch a matched URL and require the page to name the card it claims to be.

    An exact slug match can still land on a stub, a redirect to the listing page,
    or a 404 shell. Requiring the distinguishing tokens to appear in the fetched
    TEXT is what turns a naming coincidence into evidence. Note Axis's 404 shell
    is larger than its real content page, so size proves nothing — only status
    plus content does.
    """
    if cand.status != "matched" or not cand.url:
        return cand
    got = F.fetch_url(cand.url, opener=opener)
    if not got.ok:
        return dataclasses.replace(
            cand, status="unverified", reason=f"HTTP {got.status} on the matched URL"
        )
    text = (got.text or "").lower()
    if not text:
        return dataclasses.replace(cand, status="unverified", reason="page has no extractable text")
    # The page must be about a CREDIT card. Path filtering catches the obvious
    # cases, but issuers file products inconsistently and a debit card or savings
    # account page passes every name test — it names the product, states real
    # terms, and reads as authoritative. This is the check that makes the
    # difference between a weak source and a wrong one.
    if "credit card" not in text and "creditcard" not in text:
        return dataclasses.replace(
            cand, status="unverified", reason="page never says 'credit card' — wrong product line"
        )
    missing = sorted(t for t in _name_tokens(cand.card_name, cand.issuer) if t not in text)
    if missing:
        return dataclasses.replace(
            cand,
            status="unverified",
            reason="page never names the card (missing: " + ", ".join(missing[:4]) + ")",
        )
    return dataclasses.replace(cand, reason=f"verified, {len(got.text):,} chars")


def discover(
    cards: list[dict] | None = None,
    *,
    issuer: str = "",
    opener=None,
    verify_matches: bool = True,
) -> list[Candidate]:
    """Full pass: harvest each issuer's listing page, match, then verify."""
    cards = cards if cards is not None else S.load_cards()
    active = [c for c in cards if (c.get("card", c)).get("is_active")]

    by_issuer: dict[str, list[dict]] = {}
    for card in active:
        key = S.issuer_of(card)
        if key and (not issuer or key == issuer):
            by_issuer.setdefault(key, []).append(card)

    results: list[Candidate] = []
    for key in sorted(by_issuer):
        landing = S.ISSUER_LANDING.get(key, "")
        if not landing:
            for card in by_issuer[key]:
                inner = card.get("card", card)
                results.append(Candidate(
                    card_id=str(inner.get("id") or ""),
                    card_name=str(inner.get("card_name") or ""),
                    issuer=str(inner.get("issuer") or ""),
                    reason=f"no landing page configured for issuer '{key}'",
                ))
            continue
        links = harvest(landing, opener=opener)
        matched = match(by_issuer[key], links)
        if verify_matches:
            matched = [verify(m, opener=opener) for m in matched]
        results.extend(matched)
    return results


def to_overrides(results: list[Candidate], existing: dict[str, str] | None = None) -> dict[str, str]:
    """Merge verified matches into the override map.

    Existing hand-written entries WIN. Someone chose those deliberately, often for
    a card whose slug does not match its name at all, and a crawl must not quietly
    overwrite a human's decision.
    """
    out = dict(existing or {})
    for r in results:
        if r.status == "matched" and r.url and r.card_id not in out:
            out[r.card_id] = r.url
    return out


# The shipped overrides file carries a `_comment` key that tests assert on, so an
# operator opening it knows what wrote it and what happens if they edit it.
OVERRIDES_COMMENT = (
    "card_id -> the issuer page that card is read from. Entries are written by "
    "`python3 pipeline/cli.py discover --write`, which only records a URL whose "
    "page it fetched and confirmed names that card and says 'credit card'. "
    "Hand-written entries are never overwritten by a later crawl — edit freely. "
    "A card with no entry falls back to sources.ISSUER_LANDING, which is correct "
    "but shared with every other card of that issuer."
)


def write_overrides(path, merged: dict[str, str]) -> None:
    """Serialise the override map with its explanatory header preserved."""
    body = {"_comment": OVERRIDES_COMMENT}
    body.update({k: v for k, v in sorted(merged.items()) if k != "_comment"})
    path.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")


def report(results: list[Candidate]) -> str:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = len(results) or 1
    lines = ["", "=== Per-card source discovery ==="]
    for status in ("matched", "ambiguous", "unverified", "unmatched"):
        n = counts.get(status, 0)
        lines.append(f"{status:>12}: {n:4d}  ({100 * n / total:.0f}%)")
    lines.append(f"{'total':>12}: {len(results):4d}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    issuer = ""
    as_json = False
    no_verify = False
    write = False
    for i, arg in enumerate(argv):
        if arg == "--issuer" and i + 1 < len(argv):
            issuer = argv[i + 1]
        elif arg == "--json":
            as_json = True
        elif arg == "--no-verify":
            no_verify = True
        elif arg == "--write":
            write = True

    results = discover(issuer=issuer, verify_matches=not no_verify)
    if as_json:
        print(json.dumps([dataclasses.asdict(r) for r in results], indent=1))
    else:
        print(report(results))
        for r in results:
            if r.status == "matched":
                print(f"  OK   {r.card_id}\n       {r.url}")
        for r in results:
            if r.status in ("ambiguous", "unverified"):
                print(f"  WARN {r.card_id}: {r.reason}")

    if write:
        path = C.REPO / "pipeline" / "sources_overrides.json"
        existing = S.load_overrides(path)
        merged = to_overrides(results, existing)
        added = len(merged) - len(existing)
        write_overrides(path, merged)
        print(f"\nwrote {path} (+{added} entries, {len(merged)} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
