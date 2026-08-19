#!/usr/bin/env python3
"""
One definition of "this card carries issuer evidence", and the weekly catch-up
that goes and gets it for the cards that do not.

Usage:
    python3 -m pipeline.provenance                  the evidence backlog, in words
    python3 -m pipeline.provenance --json           the same numbers, machine-readable

    from pipeline import provenance as P
    P.card_has_issuer_evidence(entry)               the predicate
    P.plan_catch_up(sources, cards, state, limit=40) what to read this week

WHY THIS MODULE EXISTS
----------------------
The weekly refresh is a CHANGE DETECTOR. It fetches every active card's issuer
document, hashes the text, and sends to the model only the cards whose bytes
moved since last week. That hash gate is the single reason a weekly run costs
rupees rather than thousands of rupees — and it has one blind spot that no amount
of running it will ever close:

    A card whose rates have NEVER been verified, whose issuer page simply did not
    change this week, is skipped. Next week it is skipped again. It can never
    acquire evidence, because acquiring evidence is not what the gate measures.

Measured on 2026-08-19 against seed/cards.json and the committed pipeline state:
of 370 active cards, 361 have no REWARD RULE citing a document; 301 of those are
still to read, 56 have already been read end to end at their current bytes and
yielded nothing citable, and 4 have no reachable issuer URL at all. The
validator's L8 layer reports the same catalogue at 2.0% issuer-sourced (26 of
1,279 reward rates), and that number has not moved through any amount of
internal-consistency repair work, because internal consistency is not evidence.
Only reading the issuer's own document moves it.

`--force` already bypasses the gate, but it bypasses it for EVERY card, which is a
full paid sweep ($94.55 billed on 2026-08-17, and up to $117.65 if every extracted
card also reaches verification). What was missing is a way to say "read exactly
the cards whose reward rules cite nothing and that we have not already read at
these bytes, and nobody else". That is `plan_catch_up`.

TWO THINGS THIS MODULE IS CAREFUL NOT TO CLAIM. It is not a gate bypass in the
common case — today every card it selects would also be fetched by the ordinary
refresh, and what the flag adds is the `--limit` that makes an over-budget sweep
affordable. And the backlog TERMINATES: a card read to completion leaves it, so
`--limit 40` clears 301 cards in 8 weeks with zero repeats rather than cycling
forever. Both facts are printed by the run, before the money is spent.

THE DEFINITION LIVES HERE AND NOWHERE ELSE
------------------------------------------
`tools/checks/c8_provenance.py` (L8, the validator layer that asks "who says so?")
imports `source_host` and `source_url_candidates` from this module rather than
keeping its own copies, and reports the card-level backlog using
`card_has_issuer_evidence`. So the number L8 prints in a validation report and the
number this pipeline puts in a batch are the same number, produced by the same
code. Two copies of "what counts as a source" would drift within a month, and the
first symptom would be paying to re-read cards we had already verified.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pipeline import config as C
from pipeline import sources as S
from pipeline import state as ST

# The block a card-level provenance stamp is written to. `diff.apply_proposals`
# appends one record per field it changes; a non-empty block therefore means at
# least one number on this card was taken from a document somebody read.
PROVENANCE_BLOCK = "_provenance"

# The keys a row may offer as a citation. `source_url` is the hand-authored
# single; `_sources` is the list form three earlier passes wrote. Both are read,
# because counting only one of them means paying to re-read a card that is
# already cited — au_small_finance_bank_ixigo_au cites its documents entirely
# through `_sources` (one au.bank.in PDF on three rules, and one ixigo.com page
# on a fourth; an earlier note here said "two au.bank.in PDFs and nothing else",
# which is not what the file says).
SOURCE_KEYS = ("source_url", "_sources")

# Keys a dict inside `_sources` may hide the URL under.
_NESTED_URL_KEYS = ("url", "source_url", "href")

_BARE_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")

# ---------------------------------------------------------------------------
# WHOSE document is it — moved here from tools/checks/c8_provenance.py on
# 2026-08-19 so there is one table, not two.
#
# The validator's L8 layer counted "reward rates backed by a link to the issuer's
# own website" and got 26 of 1,279; `evidence` counted "rules citing a document
# we can open" and got 27, and both were printed as THE citation percentage. One
# number, two answers, same bytes. The tables and the matching rules now live in
# this module and c8 imports them, so the strict count and the loose count are
# both computed here and neither can drift from the other.
# ---------------------------------------------------------------------------
# --------------------------------------------------------------------------- #
# Issuer -> the domains that issuer actually publishes on.
#
# Maintained by hand ON PURPOSE. There is no way to derive this from the data,
# and guessing is how you end up accepting a blog post as an issuer source.
# Keys are the issuer string NORMALISED (lowercase, alphanumerics only).
#
# TRAP: do NOT replace this with fuzzy matching. In this file 'AU Bank' and
# 'Axis Bank' score 0.750 similar and 'HDFC Bank' / 'IDFC Bank' score 0.889 —
# a similarity threshold would hand Axis cards an AU source and call it verified.
# A new issuer spelling must be added here by a human, and until it is, this
# layer says so out loud (L8.ISSUER_DOMAIN_UNMAPPED) instead of guessing.
# --------------------------------------------------------------------------- #
ISSUER_PUBLISHES_ON: dict[str, tuple[str, ...]] = {
    "sbicard":                  ("sbicard.com", "sbi.co.in", "onlinesbi.sbi"),
    "hdfcbank":                 ("hdfcbank.com", "hdfcbank.co.in"),
    "axisbank":                 ("axisbank.com", "axisbank.co.in"),
    "icicibank":                ("icicibank.com", "icicibank.co.in"),
    "rblbank":                  ("rblbank.com",),
    "kotakmahindrabank":        ("kotak.com", "kotak.bank.in", "kotakmahindrabank.com"),
    "kotakmahindra":            ("kotak.com", "kotak.bank.in", "kotakmahindrabank.com"),
    "indusindbank":             ("indusind.com", "indusindbank.com"),
    "yesbank":                  ("yesbank.in", "yes.bank.in"),
    "ausmallfinancebank":       ("aubank.in", "au.bank.in"),
    "aubank":                   ("aubank.in", "au.bank.in"),
    "aubankcobrandedwithadityabirlafinancelimited":
                                ("aubank.in", "au.bank.in", "adityabirlacapital.com"),
    "bobcard":                  ("bobcard.co.in", "bobfinancial.com",
                                 "bankofbaroda.in", "bankofbaroda.co.in"),
    "bobcardlimited":           ("bobcard.co.in", "bobfinancial.com",
                                 "bankofbaroda.in", "bankofbaroda.co.in"),
    "bobcardbankofbaroda":      ("bobcard.co.in", "bobfinancial.com",
                                 "bankofbaroda.in", "bankofbaroda.co.in"),
    "bobcardlimitedbankofbaroda": ("bobcard.co.in", "bobfinancial.com",
                                   "bankofbaroda.in", "bankofbaroda.co.in"),
    "bobcardbankofbarodainpartnershipwithuniapp":
                                ("bobcard.co.in", "bobfinancial.com",
                                 "bankofbaroda.in", "uni.cards"),
    "idfcfirstbank":            ("idfcfirstbank.com",),
    "idfcbank":                 ("idfcfirstbank.com",),
    "americanexpress":          ("americanexpress.com",),
    "hsbc":                     ("hsbc.co.in", "hsbc.com"),
    "hsbcbank":                 ("hsbc.co.in", "hsbc.com"),
    "idbibank":                 ("idbibank.in", "idbi.com"),
    "standardchartered":        ("sc.com", "standardchartered.co.in"),
    "standardcharteredbank":    ("sc.com", "standardchartered.co.in"),
    "federalbank":              ("federalbank.co.in",),
    "federalbankbobcardscapia": ("federalbank.co.in", "bobcard.co.in", "scapia.cards"),
    "equitassmallfinancebank":  ("equitasbank.com", "equitas.bank.in"),
    "cityunionbank":            ("cityunionbank.com", "cubdigital.in"),
    "csbbank":                  ("csb.co.in",),
    "sbmbank":                  ("sbmbank.co.in",),
    "slicebank":                ("sliceit.com", "slice.bank.in"),
    "unitysmallfinancebank":    ("theunitybank.com", "unitybank.co.in"),
    "fpltechnologiespvtltd":    ("onecard.app", "getonecard.app", "fplabs.tech"),
}

# --------------------------------------------------------------------------- #
# Domains that are NEVER an acceptable source, however good the article is.
# KredMe policy (settled): scrape the issuer's own URL, nothing else. These sites
# are secondary reporting — they carry the same devaluation lag we are trying to
# eliminate, and citing one launders a guess into a footnote.
# --------------------------------------------------------------------------- #
AGGREGATOR_DOMAINS: tuple[str, ...] = (
    # card-review sites named in KredMe's own sourcing policy
    "cardexpert.in", "cardinsider.com", "technofino.in", "cardmaven.in",
    "creditcardz.in", "paisabazaar.com", "bankbazaar.com", "wishfin.com",
    "mymoneymantra.com", "myloancare.in", "creditmantri.com", "indialends.com",
    "moneytap.com", "buddyloan.com", "fincash.com", "cardsdekho.com",
    # aggregators / marketplaces
    "groww.in", "zerodha.com", "cred.club", "onecard.app.link", "jupiter.money",
    "policybazaar.com", "etmoney.com", "5paisa.com", "angelone.in",
    # general news & finance press
    "moneycontrol.com", "economictimes.indiatimes.com", "indiatimes.com",
    "livemint.com", "business-standard.com", "financialexpress.com",
    "ndtv.com", "news18.com", "hindustantimes.com", "thehindu.com",
    "zeebiz.com", "cnbctv18.com", "goodreturns.in", "bankersadda.com",
    # user-generated / forum / video
    "reddit.com", "quora.com", "youtube.com", "youtu.be", "medium.com",
    "wikipedia.org", "desidime.com", "teamblind.com", "twitter.com", "x.com",
    "facebook.com", "linkedin.com", "instagram.com", "telegram.me", "t.me",
    "blogspot.com", "wordpress.com", "substack.com",
)

# Aggregators are not always written as a URL. This file cites several of them as
# a bare word ("cardinsider"), which has no scheme and no dot, so host parsing
# alone would file it as "not a URL" and miss the policy breach entirely. These
# are matched on the whole normalised token only — never as a substring, so a
# real issuer host can never collide with one.
AGGREGATOR_TOKENS: tuple[str, ...] = (
    "cardinsider", "cardexpert", "technofino", "cardmaven", "creditcardz",
    "paisabazaar", "bankbazaar", "wishfin", "mymoneymantra", "myloancare",
    "creditmantri", "indialends", "cardsdekho", "moneycontrol", "livemint",
    "economictimes", "financialexpress", "businessstandard", "goodreturns",
    "reddit", "quora", "youtube", "wikipedia", "desidime", "blogspot",
)


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_issuer(value: object) -> str:
    """An issuer string reduced to lowercase alphanumerics, for table lookup."""
    return _NON_ALNUM.sub("", value.lower()) if isinstance(value, str) else ""


def issuer_domains(issuer: object) -> tuple | None:
    """Domains this issuer publishes on, or None when we cannot say.

    Exact normalised match first. The containment fallback exists only so a new
    SPELLING of a known issuer ('Kotak Mahindra Bank Ltd.') still maps; it is
    longest-key-first so 'aubank' can never win inside 'axisbank' (it is not a
    substring) and short keys cannot shadow long ones.

    TRAP: do NOT replace this with fuzzy matching. In this file 'AU Bank' and
    'Axis Bank' score 0.750 similar and 'HDFC Bank' / 'IDFC Bank' score 0.889 — a
    similarity threshold would hand Axis cards an AU source and call it verified.
    """
    n = normalise_issuer(issuer)
    if not n:
        return None
    hit = ISSUER_PUBLISHES_ON.get(n)
    if hit:
        return hit
    for key in sorted(ISSUER_PUBLISHES_ON, key=len, reverse=True):
        if len(key) >= 6 and key in n:
            return ISSUER_PUBLISHES_ON[key]
    return None


def host_matches(host: object, domains) -> bool:
    if not host:
        return False
    for d in domains or ():
        if host == d or host.endswith("." + d):
            return True
    return False


def aggregator_of(host: str) -> str | None:
    for d in AGGREGATOR_DOMAINS:
        if host == d or host.endswith("." + d):
            return d
    return None


def aggregator_token(raw: object) -> str | None:
    """An aggregator named as a bare word rather than a link, or None."""
    if not isinstance(raw, str):
        return None
    tok = _NON_ALNUM.sub("", raw.strip().lower())
    return tok if tok in AGGREGATOR_TOKENS else None


def rule_cites_issuer_domain(entry: object, row: object) -> bool:
    """True when this row cites a document on THIS card's issuer's own domain.

    The stricter of the two questions this module answers, and the one the
    validator's headline counts. `row_document_urls` asks "does this name a
    document anyone can open"; this asks "and is that document the bank's".

    They differ, today, by exactly one rule — au_small_finance_bank_ixigo_au
    cites https://www.ixigo.com/travel-credit-card, which is a real page and is
    not an AU page. That one rule is the entire gap between the 27 the founder
    report used to print and the 26 the validator prints, and shipping two
    percentages under the same words is how a number stops meaning anything. Both
    are now computed here, from this function and `row_document_urls`, and both
    are printed with their own label.
    """
    inner = S._inner_card(entry) if isinstance(entry, dict) else None
    domains = issuer_domains((inner or {}).get("issuer"))
    if not domains:
        return False
    for _where, raw in source_url_candidates(row):
        host = source_host(raw)
        if host is None or aggregator_of(host):
            continue
        if host_matches(host, domains):
            return True
    return False



# ---------------------------------------------------------------------------
# What counts as naming a document
# ---------------------------------------------------------------------------
def source_host(url: object) -> str | None:
    """The host of a source string, or None when it names no document at all.

    Accepts a full http(s) URL and also a bare hostname such as `kotak.bank.in`,
    because the seed carries both shapes. It does NOT accept a bare word: seven
    rules in this catalogue cite the literal string "bank", which is a
    placeholder, not something a person can open and read. Treating a placeholder
    as provenance is how a file gets a flattering coverage number, so the dot is
    load-bearing.

    This is the function `tools/checks/c8_provenance.py` uses as `_host_of`. If
    it moves, that layer moves with it.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except (ValueError, AttributeError):
        # Same pair config.is_issuer_domain guards with, for the same call. A
        # validator check may never crash on a value in the data, and this one is
        # now shared with one.
        return None

    if parts.scheme:
        if parts.scheme.lower() not in ("http", "https"):
            return None
        host = (parts.netloc or "").lower().split("@")[-1].split(":")[0].strip(".")
    elif parts.netloc:
        # protocol-relative, '//sbicard.com/x' — still names a site
        host = parts.netloc.lower().split("@")[-1].split(":")[0].strip(".")
    else:
        host = raw.lower().split("/")[0].split("?")[0].strip(".")
        if not _BARE_HOST.match(host):
            return None
    if not host or "." not in host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def source_url_candidates(row: object) -> list[tuple[str, str]]:
    """Every string this row offers as a source, as (which_key, raw_value) pairs.

    Deliberately shape-tolerant, because the seed is not shape-consistent:
    `source_url` appears as a string and as a list, `_sources` as a string, a
    list of strings, a list of dicts and a bare dict. A reader that assumes one
    shape does not report less evidence — it silently reports none, for whichever
    cards used the other shape.

    Returns raw values, NOT validated ones. Whether a value names a real document
    is `source_host`'s question, and whether the document is acceptable (issuer
    vs aggregator) is L8's. Keeping the three questions separate is what lets one
    harvester serve both callers.

    This is the function `tools/checks/c8_provenance.py` uses as `_url_candidates`.
    """
    if not isinstance(row, dict):
        return []

    out: list[tuple[str, str]] = []
    direct = row.get("source_url")
    if isinstance(direct, str):
        out.append(("source_url", direct))
    elif isinstance(direct, list):
        out.extend(("source_url", x) for x in direct if isinstance(x, str))

    nested = row.get("_sources")
    if isinstance(nested, str):
        out.append(("_sources", nested))
    elif isinstance(nested, list):
        for item in nested:
            if isinstance(item, str):
                out.append(("_sources", item))
            elif isinstance(item, dict):
                for key in _NESTED_URL_KEYS:
                    if isinstance(item.get(key), str):
                        out.append(("_sources", item[key]))
                        break
    elif isinstance(nested, dict):
        for key in _NESTED_URL_KEYS:
            if isinstance(nested.get(key), str):
                out.append(("_sources", nested[key]))
                break
    return out


def row_document_urls(row: object) -> list[str]:
    """Just the values on `row` that actually name a readable document."""
    return [raw for _key, raw in source_url_candidates(row) if source_host(raw)]


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------
def _rules_of(entry: object) -> list[dict]:
    if not isinstance(entry, dict):
        return []
    rules = entry.get("reward_rules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def provenance_rows(entry: object) -> list[dict]:
    """The card-level `_provenance` stamps, as a list whatever shape they took."""
    if not isinstance(entry, dict):
        return []
    block = entry.get(PROVENANCE_BLOCK)
    if isinstance(block, dict):
        return [block]
    if isinstance(block, list):
        return [row for row in block if isinstance(row, dict)]
    return []


def card_evidence_urls(entry: object) -> list[str]:
    """Every document this card cites, from its rules and its `_provenance` block.

    Deduplicated, in the order they appear, so a caller can print "cited at" and
    a reader can go and check it.
    """
    out: list[str] = []
    for row in _rules_of(entry) + provenance_rows(entry):
        for url in row_document_urls(row):
            if url not in out:
                out.append(url)
    return out


def card_rule_evidence_urls(entry: object) -> list[str]:
    """Documents cited by this card's REWARD RULES, ignoring the card-level stamp."""
    out: list[str] = []
    for row in _rules_of(entry):
        for url in row_document_urls(row):
            if url not in out:
                out.append(url)
    return out


def card_has_issuer_evidence(entry: object) -> bool:
    """True when a REWARD RULE on this card names a document somebody can open.

    THE UNIT IS THE REWARD RULE, and that is the whole correction made on
    2026-08-19. This used to return True for a card whose only evidence was its
    card-level `_provenance` block — and that block records CARD FIELDS
    (annual_fee_inr, forex_markup_pct, base_reward_rate, point_value_inr), not
    reward rules. 25 active cards were exempted from the catch-up on a stamp
    about an annual fee while carrying 91 uncited reward rules between them. The
    validator graded 22 of those 25 F on the very metric this pipeline exists to
    move, so the pipeline was permanently skipping cards the validator still
    called unsourced — the forever-skip failure, moved one level up.

    So: a card is "sourced" when at least one of its reward rules carries a
    `source_url` or `_sources` value naming a document. Card-level `_provenance`
    is still read and still reported (`card_provenance_only`), because it is real
    evidence about real numbers — just not about the numbers this metric counts.

    WHAT DOES NOT COUNT, and why:

      * `source_quote` alone. A quote with no document behind it cannot be
        re-checked next quarter when the issuer devalues, which is the entire
        reason we want provenance.
      * the placeholder string "bank", which seven rules in this catalogue cite.
        Nobody can open that. `source_host` requires the dot for this reason.
      * whether the URL is on the ISSUER'S domain. That is the stricter question,
        and `rule_cites_issuer_domain` answers it in this same module. Folding it
        in here would put four cards (equitas_powermiles, equitas_selfe,
        equitas_tiga, unity_small_finance_bank_roarbank) into the backlog purely
        because equitas.bank.in and theunitybank.com are missing from
        config.ISSUER_DOMAINS — an allowlist gap, not an evidence gap.

    NOTE ON CARDS WITH NO REWARD RULES AT ALL. 28 active cards carry none, so
    they can never satisfy this predicate. They are not stuck in the backlog
    forever: a card leaves it once the pipeline has read its document end to end
    (state.completed_at_current_bytes), whatever the reading produced.
    """
    return bool(card_rule_evidence_urls(entry))


def card_provenance_only(entry: object) -> bool:
    """True for a card whose ONLY citation is the card-level `_provenance` stamp.

    Worth naming because these look sourced and are not, on the metric that is
    reported. Counted separately in the evidence report rather than folded into
    either column.
    """
    if card_has_issuer_evidence(entry):
        return False
    return any(row_document_urls(row) for row in provenance_rows(entry))


def _card_id(entry: object) -> str:
    inner = S._inner_card(entry)
    if inner is None:
        return ""
    return str(inner.get("id") or "").strip()


def unsourced_card_ids(cards: list[dict]) -> set[str]:
    """Every card id in the seed with no issuer evidence, active or not.

    Activity is deliberately NOT filtered here. `sources.resolve_sources` already
    owns the definition of an active card (an absent `is_active` means ACTIVE, on
    purpose), and re-deciding it in a second place is exactly the drift this
    module exists to prevent. Callers intersect this set with the resolved
    sources, which are active-only.
    """
    out: set[str] = set()
    for entry in cards:
        card_id = _card_id(entry)
        if card_id and not card_has_issuer_evidence(entry):
            out.add(card_id)
    return out


def rule_evidence_counts(
    cards: list[dict], *, only_card_ids: "set[str] | None" = None
) -> dict:
    """How many reward rules cite anything, and how many cite the issuer.

    Returns both counts over both populations, because one screen mixing two
    populations is how a percentage acquires a denominator nobody can act on:

      total            every reward rule in the file, all 383 card entries
      total_active     rules on cards switched on in the app — the only ones the
                       pipeline can ever fetch. 29 rules sit on the 13 inactive
                       cards and are counted by the validator headline while
                       being permanently outside this pipeline's reach.
      cited            rules naming a document anyone can open
      issuer_cited     rules naming a document on THAT CARD'S issuer's domain —
                       the validator's stricter test, reported side by side and
                       never merged with the looser one.

    `only_card_ids`, when given, is the active set; without it the active figures
    equal the totals.
    """
    total = cited = issuer_cited = 0
    total_active = cited_active = issuer_cited_active = 0
    for entry in cards:
        card_id = _card_id(entry)
        active = only_card_ids is None or card_id in only_card_ids
        for rule in _rules_of(entry):
            has_url = bool(row_document_urls(rule))
            has_issuer = has_url and rule_cites_issuer_domain(entry, rule)
            total += 1
            cited += 1 if has_url else 0
            issuer_cited += 1 if has_issuer else 0
            if active:
                total_active += 1
                cited_active += 1 if has_url else 0
                issuer_cited_active += 1 if has_issuer else 0
    return {
        "total": total,
        "cited": cited,
        "issuer_cited": issuer_cited,
        "total_active": total_active,
        "cited_active": cited_active,
        "issuer_cited_active": issuer_cited_active,
    }


# ---------------------------------------------------------------------------
# Selection — which unsourced cards to read this week
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CatchUp:
    """One week's evidence-gap plan.

    `selected` are the cards to fetch and extract. `unreachable` are unsourced
    cards we cannot extract at all because no issuer URL resolves for them — they
    are carried in the plan rather than dropped, because four cards silently
    vanishing is how a backlog appears to be shrinking when it is not.

    The counts either side of `selected` are the ones an operator needs BEFORE
    the money is spent, and each answers a question the earlier version could not:

      backlog          unsourced, reachable, and NOT already read at its current
                       bytes. This is the live queue and it counts down.
      exhausted        unsourced cards dropped from the queue because the
                       pipeline already read them end to end at these exact bytes
                       and they still cite nothing. Re-reading returns the same
                       nothing; they re-enter when their page moves.
      gate_blocked     of the selection, how many the content-hash gate would
                       actually have stopped. This is what the flag BUYS.
      also_due         of the selection, how many an ordinary unflagged refresh
                       would have fetched anyway this week. Running both in one
                       week pays for these twice.
      re_reads         of the selection, how many have been selected before and
                       still cite nothing. Anything above zero means the backlog
                       has stopped shrinking.
      on_shared_doc    of the selection, how many are read from a document shared
                       with another card — a landing page listing forty cards is
                       a weak answer to "what does THIS card earn".
    """

    selected: list = field(default_factory=list)        # list[S.Source]
    unreachable: list = field(default_factory=list)     # list[S.Source]
    backlog: int = 0            # unsourced ACTIVE cards with a URL, still readable
    exhausted: int = 0          # already read end-to-end at these bytes
    sourced: int = 0            # active cards that already carry evidence
    provenance_only: int = 0    # active cards cited only by a card-field stamp
    active: int = 0
    inactive_unsourced: int = 0
    gate_blocked: int = 0
    also_due: int = 0
    re_reads: int = 0
    never_read: int = 0
    on_shared_doc: int = 0
    shared_documents: dict = field(default_factory=dict)

    @property
    def deferred(self) -> int:
        """Live backlog cards this run's --limit left for a later week.

        `backlog` excludes cards already read at their current bytes, so this
        number falls as the queue is worked. The version that computed it from
        the whole unsourced set printed the identical figure in week 1, week 9
        and week 13 — an operator watching it could not tell a run making
        progress from one re-billing cards it had already read.
        """
        return max(0, self.backlog - len(self.selected))


def _attempt_key(state: dict, card_id: str) -> tuple[int, str, str]:
    """Sort key that makes a weekly `--limit N` walk the backlog instead of
    circling the same N cards.

    Ordering is (times already attempted, when last attempted, card id):

      * A card nobody has tried sorts first, so the first runs are pure progress.
      * Once a card has been read it sinks behind every card that has not.
      * The card id breaks ties, so two runs over identical state pick identical
        cards. Nothing here depends on dict ordering or on the clock.

    The attempt counter is incremented for every card the run SELECTED, including
    ones whose fetch failed. That is deliberate: BOBCARD's cards cannot be fetched
    at all (their server omits its intermediate certificate), and counting only
    successful reads would park those cards permanently at the front of the queue
    and starve every other issuer.
    """
    count, last = ST.evidence_attempts(state, card_id)
    return (count, last, card_id)


def _round_robin(items: list, key) -> list:
    """Deal `items` out one per group, cycling, preserving each group's order.

    Why the selection is not simply sorted by card id: card ids begin with the
    issuer name, so a sorted `--limit 40` would be 40 Axis cards. Three
    consequences, all bad. One issuer's outage wastes a whole week (BOBCARD's TLS
    chain, YES Bank's JavaScript-only listing). `fetch_many` gives each HOST a
    single worker with a polite delay between requests, so a one-issuer batch
    fetches strictly serially while seven workers idle. And a reviewer gets a PR
    that touches one bank instead of a cross-section they can sanity-check.

    Dealing round-robin across issuers fixes all three and stays deterministic.
    """
    groups: dict = {}
    for item in items:
        groups.setdefault(key(item), []).append(item)
    out: list = []
    while groups:
        for group_key in list(groups):
            bucket = groups[group_key]
            out.append(bucket.pop(0))
            if not bucket:
                del groups[group_key]
    return out


def shared_document_counts(sources: list) -> dict:
    """{url: how many of these sources are read from it}, for urls serving >1 card."""
    counts: dict = {}
    for src in sources:
        if src.url:
            counts[src.url] = counts.get(src.url, 0) + 1
    return {url: n for url, n in counts.items() if n > 1}


def catch_up_order(sources: list, state: dict) -> list:
    """Order unsourced sources for reading: least-attempted first, own page first,
    issuers spread.

    Three sorts, in this order of authority:

      1. TIER, from `_attempt_key`'s first two components — how often and how
         recently a card has been read. Tiers are what make progress across weeks.
      2. WITHIN a tier, a card read from a document of its OWN goes before a card
         read from a page shared with other cards. PIPELINE.md already states the
         rule this enforces: "asking the model for one card's mechanics out of a
         page listing forty is the difference between a refresh that works and
         one that merely runs". 172 of the backlog's cards resolve only to an
         issuer landing page, and the round-robin used to front-load exactly the
         issuers whose whole backlog sits on one — 30 of the first 40. That is
         roughly half the bill spent on documents that structurally cannot answer
         the question. `cli.py discover` fixes those offline and for free, so
         they go last, not first.
      3. WITHIN each of those halves, round-robin across issuers, so a week is a
         cross-section rather than one bank.
    """
    shared = shared_document_counts(sources)
    ranked = sorted(sources, key=lambda s: _attempt_key(state, s.card_id))

    def _emit(tier: list) -> list:
        own = [s for s in tier if s.url not in shared]
        pooled = [s for s in tier if s.url in shared]
        return (_round_robin(own, lambda s: s.issuer)
                + _round_robin(pooled, lambda s: s.issuer))

    out: list = []
    tier: list = []
    tier_key: tuple | None = None
    for src in ranked:
        count, last, _ = _attempt_key(state, src.card_id)
        key = (count, last)
        if tier_key is not None and key != tier_key:
            out.extend(_emit(tier))
            tier = []
        tier_key = key
        tier.append(src)
    if tier:
        out.extend(_emit(tier))
    return out


def plan_catch_up(
    sources: list, cards: list[dict], state: dict, *, limit: int = 0
) -> CatchUp:
    """This week's evidence-gap selection, from the resolved sources.

    `sources` is `sources.resolve_sources(...)` output, already restricted to
    ACTIVE cards and already filtered by any `--card-id` the operator passed.

    TWO SUBTRACTIONS the first version did not make, and both were money:

    1. A card the pipeline has already read END TO END at its current bytes is
       dropped, even though it still cites nothing. "Unsourced" was being used as
       a proxy for "never read" and the two are not the same — of 67 cards marked
       done, 31 acquired no citation, and every one of them would have been
       re-fetched and re-billed every cycle forever with no stop condition. They
       come back the moment their page moves, which is what the ordinary hash
       gate already does, with no flag and no special case.

    2. The plan now says how much of the selection the hash gate was actually
       blocking (`gate_blocked`) versus how much this week's ordinary refresh
       would have fetched anyway (`also_due`). Those are wildly different things
       to spend on, and running the flag in the same week as the cron pays for
       the second group twice, in two separate batches.
    """
    gap = unsourced_card_ids(cards)
    unsourced = [s for s in sources if s.card_id in gap]

    with_url = [s for s in unsourced if s.url]
    unreachable = [s for s in unsourced if not s.url]

    reachable = [s for s in with_url
                 if not ST.completed_at_current_bytes(state, s.card_id)]
    exhausted = len(with_url) - len(reachable)

    ordered = catch_up_order(reachable, state)
    selected = ordered[:limit] if limit and limit > 0 else ordered

    shared = shared_document_counts(reachable)
    by_id = {}
    for entry in cards:
        cid = _card_id(entry)
        if cid:
            by_id[cid] = entry

    active_ids = {s.card_id for s in sources}
    return CatchUp(
        selected=selected,
        unreachable=unreachable,
        backlog=len(reachable),
        exhausted=exhausted,
        sourced=len(sources) - len(unsourced),
        provenance_only=sum(1 for s in sources
                            if card_provenance_only(by_id.get(s.card_id))),
        active=len(sources),
        inactive_unsourced=len(gap - active_ids),
        # The gate stops a card only when it finished a cycle at these bytes.
        # Nothing here is `gate_blocked` today by construction — those cards are
        # exactly the ones subtraction 1 removed — so this reads 0 and says so,
        # rather than implying the flag bought a bypass it did not.
        gate_blocked=sum(1 for s in selected
                         if ST.completed_at_current_bytes(state, s.card_id)),
        also_due=sum(1 for s in selected
                     if not ST.completed_at_current_bytes(state, s.card_id)),
        re_reads=sum(1 for s in selected
                     if ST.evidence_attempts(state, s.card_id)[0] > 0),
        never_read=sum(1 for s in reachable
                       if ST.evidence_attempts(state, s.card_id)[0] == 0),
        on_shared_doc=sum(1 for s in selected if s.url in shared),
        shared_documents=shared,
    )


# ---------------------------------------------------------------------------
# Cost — planning figures only
#
# These price a PLAN, before a single page has been fetched, off the only real
# bill this pipeline has produced. They are not the number that gates a
# submission: that is still batch.estimate_cost() over the actual documents,
# checked inside batch.submit() against config.MAX_BATCH_USD. Two figures with
# two jobs, and the one with authority is the one computed from real bytes.
# ---------------------------------------------------------------------------
def forecast_usd(
    cards: int, *, both_passes: bool = True, basis: str = "ceiling"
) -> float:
    """What `cards` cards will probably cost, at the measured per-card rate.

    `basis` matters only for the two-pass figure, and the default is the
    pessimistic end on purpose:

      "ceiling"   every extracted card is also verified. Extraction and
                  verification are priced off their OWN request counts (371 and
                  223 on 17-Aug), so this is $0.3171 a card.
      "observed"  17-Aug's yield repeats and only 60.1% of extracted cards
                  produce something worth verifying. $0.2548 a card.

    The floor is quoted beside the ceiling, never instead of it. Which one a real
    run lands on is a property of the documents, not a discount anyone has been
    offered, and a spend decision has to be made against the number that can
    actually arrive on the bill.
    """
    if cards <= 0:
        return 0.0
    if not both_passes:
        return cards * C.USD_PER_CARD_EXTRACT
    per_card = (C.USD_PER_CARD_BOTH_PASSES_OBSERVED if basis == "observed"
                else C.USD_PER_CARD_BOTH_PASSES)
    return cards * per_card


def cards_within_budget(max_usd: float | None, *, both_passes: bool = False) -> int:
    """How many cards fit under a spend ceiling.

    `refresh` submits the EXTRACTION batch and that is what its pre-flight has to
    compare, so extraction is the default. But MAX_BATCH_USD is a limit PER
    BATCH, not per cycle: verification is a second batch, submitted later by
    `advance` and checked against the same ceiling separately, so a run that
    passes this can still cost close to twice it by the time the cycle ends.
    Pass both_passes=True for the number an operator budgeting a whole cycle
    wants — 78 cards at $25, not 155.
    """
    if max_usd is None or max_usd <= 0:
        return 0    # 0 means "no ceiling", matching --max-usd 0
    per_card = (C.USD_PER_CARD_BOTH_PASSES if both_passes
                else C.USD_PER_CARD_EXTRACT)
    return int(max_usd / per_card)


# ---------------------------------------------------------------------------
# The report — one command, one screen, no engineering vocabulary
# ---------------------------------------------------------------------------
def evidence_report(
    cards: list[dict],
    sources: list,
    state: "dict | None" = None,
    *,
    per_week: int = 40,
    max_usd: float | None = -1.0,
) -> dict:
    """Every number the `evidence` command prints. Pure; reads nothing.

    `max_usd` follows batch.submit's convention exactly: the sentinel -1.0 means
    "use config.MAX_BATCH_USD", and None means no ceiling. Those two must stay
    distinct — `--max-usd 0` is how an operator says "no limit", and silently
    reinstating the default there would report a ceiling that is not in force.

    `state` is optional so the module CLI can run without it, but passing it is
    what makes the backlog the LIVE one: cards already read end to end at their
    current bytes are counted separately rather than quoted as work outstanding.
    """
    if max_usd == -1.0:
        max_usd = C.MAX_BATCH_USD
    state = state if isinstance(state, dict) else {}
    gap = unsourced_card_ids(cards)

    by_id = {}
    for entry in cards:
        card_id = _card_id(entry)
        if card_id:
            by_id[card_id] = entry

    unsourced = [s for s in sources if s.card_id in gap]
    with_url = [s for s in unsourced if s.url]
    unreachable = [s for s in unsourced if not s.url]
    reachable = [s for s in with_url
                 if not ST.completed_at_current_bytes(state, s.card_id)]
    exhausted = [s for s in with_url if s not in reachable]

    # Grouped by the SLUG the scheduler uses, not the seed's raw issuer string.
    # seed/cards.json spells 21 banks 34 ways — BOBCARD six ways, AU three, IDFC
    # three — so grouping on the raw string printed a 10-card BOBCARD backlog
    # when the pipeline will work through 19, and pushed four of the ten largest
    # gaps off the list entirely. The raw spellings are kept as a sub-line,
    # because a reader checking this against the catalogue needs them.
    by_issuer: dict[str, int] = {}
    spellings: dict[str, dict[str, int]] = {}
    for src in unsourced:
        entry = by_id.get(src.card_id) or {}
        inner = S._inner_card(entry) or {}
        slug = src.issuer or "unknown"
        by_issuer[slug] = by_issuer.get(slug, 0) + 1
        name = str(inner.get("issuer") or "").strip() or f"(unnamed: {slug})"
        spellings.setdefault(slug, {})
        spellings[slug][name] = spellings[slug].get(name, 0) + 1

    active_ids = {s.card_id for s in sources}
    rules = rule_evidence_counts(cards, only_card_ids=active_ids)
    shared = shared_document_counts(reachable)
    landing = set(S.ISSUER_LANDING.values())
    n = len(reachable)
    per_week = max(1, int(per_week or 1))
    slice_n = min(per_week, n)

    def _pct(part: int, whole: int) -> float:
        return round(100.0 * part / whole, 2) if whole else 0.0

    return {
        "cards_total": len(cards),
        "cards_active": len(sources),
        "cards_sourced": len(sources) - len(unsourced),
        "cards_provenance_stamp_only": sum(
            1 for s in sources if card_provenance_only(by_id.get(s.card_id))
        ),
        "cards_unsourced": len(unsourced),
        "cards_unsourced_reachable": n,
        "cards_already_read_at_these_bytes": len(exhausted),
        "cards_unsourced_no_url": len(unreachable),
        "no_url_cards": [
            {"card_id": s.card_id, "card_name": s.card_name, "reason": s.reason}
            for s in unreachable
        ],
        "cards_inactive_unsourced": len(gap - active_ids),
        "rules_total": rules["total"],
        "rules_total_active": rules["total_active"],
        "rules_cited": rules["cited"],
        "rules_cited_pct": _pct(rules["cited"], rules["total"]),
        "rules_issuer_cited": rules["issuer_cited"],
        "rules_issuer_cited_pct": _pct(rules["issuer_cited"], rules["total"]),
        "rules_issuer_cited_active_pct": _pct(
            rules["issuer_cited_active"], rules["total_active"]),
        "rules_unreachable_inactive": rules["total"] - rules["total_active"],
        "distinct_urls": len({s.url for s in reachable}),
        "cards_on_shared_document": sum(1 for s in reachable if s.url in shared),
        "cards_on_issuer_landing_page": sum(1 for s in reachable if s.url in landing),
        "worst_shared_documents": sorted(
            shared.items(), key=lambda kv: (-kv[1], kv[0]))[:6],
        "by_issuer": by_issuer,
        "issuer_spellings": spellings,
        "cost_both_passes_usd": round(forecast_usd(n), 2),
        "cost_both_passes_floor_usd": round(forecast_usd(n, basis="observed"), 2),
        "cost_extract_only_usd": round(forecast_usd(n, both_passes=False), 2),
        "cost_both_passes_inr": round(forecast_usd(n) * C.INR_PER_USD),
        "cost_both_passes_floor_inr": round(
            forecast_usd(n, basis="observed") * C.INR_PER_USD),
        "max_batch_usd": max_usd,
        "cards_per_batch_ceiling": cards_within_budget(max_usd),
        "cards_per_cycle_ceiling": cards_within_budget(max_usd, both_passes=True),
        "per_week": per_week,
        "weeks_to_clear": (n + per_week - 1) // per_week if n else 0,
        "cost_per_week_usd": round(forecast_usd(slice_n), 2),
        "cost_per_week_floor_usd": round(forecast_usd(slice_n, basis="observed"), 2),
        "cost_per_week_inr": round(forecast_usd(slice_n) * C.INR_PER_USD),
    }


def _bar(label: str, value: object, note: str = "") -> str:
    return f"  {label:<38} {value:>7}   {note}".rstrip()


def render_evidence_report(rep: dict) -> str:
    """The report a non-engineer reads to decide whether to spend the money."""
    lines: list[str] = []
    active = rep["cards_active"] or 1

    lines.append("")
    lines.append("How much of the catalogue can we prove?")
    lines.append("")
    lines.append("Cards")
    lines.append(_bar("switched on in the app", rep["cards_active"]))
    lines.append(_bar("a reward rule cites a document", rep["cards_sourced"],
                      f"{100.0 * rep['cards_sourced'] / active:5.1f}%"))
    lines.append(_bar("no reward rule cites anything", rep["cards_unsourced"],
                      f"{100.0 * rep['cards_unsourced'] / active:5.1f}%"))
    if rep["cards_provenance_stamp_only"]:
        lines.append(_bar("  ... an annual-fee stamp only",
                          rep["cards_provenance_stamp_only"],
                          "card fields were sourced; no reward rule was"))
    lines.append(_bar("  ... of those, still to read", rep["cards_unsourced_reachable"],
                      f"across {rep['distinct_urls']} distinct issuer pages"))
    if rep["cards_already_read_at_these_bytes"]:
        lines.append(_bar("  ... already read at these bytes",
                          rep["cards_already_read_at_these_bytes"],
                          "the document yielded nothing citable — not requeued"))
    lines.append(_bar("  ... of those, NO issuer URL", rep["cards_unsourced_no_url"],
                      "cannot be extracted at all — listed below"))
    if rep["cards_inactive_unsourced"]:
        lines.append(_bar("  (switched off, so not counted)",
                          rep["cards_inactive_unsourced"]))

    lines.append("")
    lines.append("Reward rules — the numbers that pick a card for a real person")
    lines.append(_bar("in the file", f"{rep['rules_total']:,}"))
    lines.append(_bar("  ... on cards switched on in the app",
                      f"{rep['rules_total_active']:,}",
                      f"the other {rep['rules_unreachable_inactive']} are on switched-off "
                      f"cards this pipeline never fetches"))
    lines.append(_bar("citing a document we can open", rep["rules_cited"],
                      f"{rep['rules_cited_pct']:5.1f}%"))
    lines.append(_bar("citing the ISSUER'S own document", rep["rules_issuer_cited"],
                      f"{rep['rules_issuer_cited_pct']:5.1f}%  <- the validator's headline"))

    lines.append("")
    lines.append("Where the gap is")
    for slug, count in sorted(rep["by_issuer"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {count:>4}  {slug}")
        spelt = rep.get("issuer_spellings", {}).get(slug) or {}
        if len(spelt) > 1:
            detail = " + ".join(f"{name} {n}" for name, n in
                                sorted(spelt.items(), key=lambda kv: (-kv[1], kv[0])))
            lines.append(f"        spelt in the file as: {detail}")

    if rep["cards_on_shared_document"]:
        lines.append("")
        lines.append("How good the documents are")
        lines.append(_bar("read from a page shared with other cards",
                          rep["cards_on_shared_document"],
                          "one card's mechanics out of a list of forty"))
        lines.append(_bar("read from the issuer's landing page",
                          rep["cards_on_issuer_landing_page"]))
        for url, count in rep.get("worst_shared_documents") or []:
            lines.append(f"    {count:>3} cards  {url}")
        lines.append("  `python3 pipeline/cli.py discover --write` finds each card its own")
        lines.append("  page, offline and for free. Run it BEFORE spending on these.")

    if rep["no_url_cards"]:
        lines.append("")
        lines.append("Cards this pipeline can NEVER fix — no issuer URL resolves")
        for row in rep["no_url_cards"]:
            lines.append(f"  {row['card_id']:<46} {row['reason']}")
        lines.append("  These need a URL in pipeline/sources_overrides.json, or an issuer")
        lines.append("  that publishes rates at all. No amount of running the pipeline helps.")

    lines.append("")
    lines.append("What clearing it costs")
    lines.append(_bar("all of it, both passes",
                      f"${rep['cost_both_passes_usd']:,.2f}",
                      f"about Rs {rep['cost_both_passes_inr']:,}"))
    lines.append(_bar("  ... if 17-Aug's yield repeats",
                      f"${rep['cost_both_passes_floor_usd']:,.2f}",
                      f"about Rs {rep['cost_both_passes_floor_inr']:,} — the optimistic end"))
    lines.append(_bar("extraction only (no adversary)",
                      f"${rep['cost_extract_only_usd']:,.2f}"))
    lines.append(_bar(f"at --limit {rep['per_week']} a week",
                      f"{rep['weeks_to_clear']} wk",
                      f"about ${rep['cost_per_week_usd']:,.2f} a week "
                      f"(Rs {rep['cost_per_week_inr']:,})"))
    if rep["max_batch_usd"] is None:
        lines.append(_bar("most cards one BATCH may hold", "no cap",
                          "the spend ceiling is switched off (--max-usd 0)"))
    else:
        lines.append(_bar("most cards one BATCH may hold",
                          rep["cards_per_batch_ceiling"],
                          f"the ${rep['max_batch_usd']:,.2f} per-batch ceiling in "
                          f"config.MAX_BATCH_USD"))
        lines.append(_bar("most cards one CYCLE may hold",
                          rep["cards_per_cycle_ceiling"],
                          "extraction AND its verification batch inside the same "
                          "ceiling"))
    lines.append("")
    lines.append("  Priced off the only real bill this pipeline has produced, 2026-08-17:")
    lines.append(f"  extraction {C.MEASURED_SWEEP_EXTRACT_CARDS} requests "
                 f"${C.MEASURED_SWEEP_EXTRACT_USD:,.2f}, verification "
                 f"{C.MEASURED_SWEEP_VERIFY_CARDS} requests "
                 f"${C.MEASURED_SWEEP_VERIFY_USD:,.2f}. Each pass is divided by its own")
    lines.append("  request count. It is a forecast for planning; the number that actually")
    lines.append("  gates a submission is computed from the real documents at submit time.")
    lines.append("")
    lines.append("To start clearing it, without spending anything:")
    lines.append(f"    python3 pipeline/cli.py refresh --unsourced-only "
                 f"--limit {rep['per_week']} --dry-run")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline.provenance",
        description="How much of the card catalogue cites a document, and what "
                    "finishing the job costs.",
    )
    parser.add_argument("--json", action="store_true", help="the numbers only")
    parser.add_argument("--per-week", type=int, default=40,
                        help="cards per weekly run, for the schedule estimate")
    args = parser.parse_args(argv)

    try:
        cards = S.load_cards()
        overrides = S.load_overrides(S.OVERRIDES_JSON)
    except (OSError, ValueError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    rep = evidence_report(
        cards, S.resolve_sources(cards, overrides), ST.load_state(),
        per_week=args.per_week,
    )
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(render_evidence_report(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
