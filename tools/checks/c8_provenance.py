"""L8 — provenance & confidence: is the number actually TRUE?

Every other layer proves this file is internally consistent — that a rate agrees
with its own rule name, that a reference resolves, that a cap has a period. None
of them can tell you whether a number is correct, because a self-consistent lie
is still a lie. This layer is the only one that asks the founder's question:

    "Who says so, and can I read it for myself?"

It builds a VERIFICATION LEDGER over every reward rule — the numbers that decide
which card the app tells a real person to swipe — and grades every card A-F on
how much of its money is backed by a document at the issuer.

What it checks
  * per rule: source_url / _sources / source_quote / source_fetched_on / confidence
  * the URL is on the ISSUER'S OWN domain. KredMe policy is official issuer URL
    only — never CardExpert, CardInsider or any other aggregator. Aggregators are
    an ERROR, not a warning: a rate copied from a blog is a rumour with a citation.
  * the quote actually contains the number the rule claims. A quote that does not
    support its own rule is WORSE than no quote — it manufactures false confidence.
  * staleness: source_fetched_on older than `provenance_max_age_days`
    (ctx.config, default 90). Indian issuers devalue several times a year.
  * the 'confidence defaults to high' trap. credit_card.dart:463 reads
    `json['confidence'] ?? 'high'`, so a rule with NO confidence key ships to
    users asserting high confidence it never earned. One prominent ERROR counts
    exactly how many.

Configuration read (all optional, all from ctx.config):
    provenance_max_age_days   int    default 90
    today                     "YYYY-MM-DD"  default the system date
    provenance_min_sourced_pct float default 90.0 — below this the headline is an
                              ERROR rather than an INFO. It changes the SEVERITY
                              of one finding only; the measured number is always
                              reported truthfully either way.

Nothing here is grandfathered, nothing reads rate_baseline.json, nothing is
suppressed. The runner owns policy.
"""
from __future__ import annotations

import datetime
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .base import Ctx, Finding, ERROR, WARN, INFO, num, trunc, iso_ok, card_base_pct
from .c6_reachability import _cast_faults

# --------------------------------------------------------------------------- #
# "What counts as a citation" is imported, never copied.
#
# The weekly pipeline decides which cards still need reading at the issuer, and
# this layer decides how much of the file is verified. Those two have to agree on
# what a source IS, or the day they diverge we start paying to re-read cards this
# report already calls sourced. So the harvester and the URL parser live in
# pipeline/provenance.py and both callers import them; there is exactly one
# definition and it is the one below.
#
# Importing pipeline from tools/ is safe on a bare Python: the Anthropic SDK is
# the only third-party import anywhere under pipeline/ and it is loaded lazily,
# inside the functions that call the API. tests/run_all.py asserts that, and
# tools/test_validate_cards.py already allows `pipeline` in its stdlib-only check.
# --------------------------------------------------------------------------- #
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from pipeline.provenance import (           # noqa: E402
    AGGREGATOR_DOMAINS as _AGGREGATOR_DOMAINS,
    AGGREGATOR_TOKENS as _AGGREGATOR_TOKENS,
    ISSUER_PUBLISHES_ON as _ISSUER_DOMAINS,
    aggregator_of as _aggregator_of,
    aggregator_token as _aggregator_token,
    card_has_issuer_evidence as _card_has_evidence,
    host_matches as _host_matches,
    issuer_domains as _issuer_domains,
    normalise_issuer as _norm_issuer,
    source_host as _host_of,
    source_url_candidates as _url_candidates,
)

# The issuer -> domain table, the aggregator lists and the matching rules moved
# to pipeline/provenance.py on 2026-08-19. They were the last thing this layer
# and the pipeline each kept their own copy of, and they are why the same file
# reported 26 issuer-sourced rules here and 27 in `pipeline/cli.py evidence`.
# One table, imported twice.

LAYER = "L8 provenance & confidence"

# What credit_card.dart:463 will accept without complaint. It accepts anything —
# there is no vocabulary in the Dart at all — so this is OUR vocabulary, and a
# value outside it is a sign the field was hand-typed rather than generated.
_CONFIDENCE_VOCAB = ("high", "medium", "low")

# The exact string the Dart parser substitutes when the key is absent.
# credit_card.dart:463 —  confidence: json['confidence'] as String? ?? 'high'
_DART_CONFIDENCE_DEFAULT = "high"

# Blocks that also carry money-relevant numbers, checked for coverage only.
_OTHER_BLOCKS = ("exclusion_rules", "milestone_rules",
                 "fuel_surcharge_rules", "redemption_rules")

_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- #
# small local helpers
# --------------------------------------------------------------------------- #
def _s(v):
    """The value as a non-empty stripped string, else None."""
    return v.strip() if isinstance(v, str) and v.strip() else None


def _d(v):
    return v if isinstance(v, dict) else {}


def _cfg(ctx: Ctx) -> dict:
    return _d(getattr(ctx, "config", None))


def _today(ctx: Ctx) -> datetime.date:
    t = _cfg(ctx).get("today")
    if isinstance(t, str) and len(t) >= 10:
        try:
            return datetime.date.fromisoformat(t[:10])
        except Exception:
            pass
    return datetime.date.today()


def _max_age_days(ctx: Ctx) -> int:
    v = _cfg(ctx).get("provenance_max_age_days", 90)
    try:
        n = int(v)
        return n if n > 0 else 90
    except Exception:
        return 90


def _min_sourced_pct(ctx: Ctx) -> float:
    v = _cfg(ctx).get("provenance_min_sourced_pct", 90.0)
    try:
        f = float(v)
        return f if 0.0 <= f <= 100.0 else 90.0
    except Exception:
        return 90.0


def _as_date(v):
    if not iso_ok(v):
        return None
    try:
        return datetime.date.fromisoformat(v[:10])
    except Exception:
        return None


def _numbers_in(text) -> set:
    out = set()
    if not isinstance(text, str):
        return out
    for m in _NUM_TOKEN.finditer(text):
        try:
            out.add(float(m.group(0).replace(",", "")))
        except Exception:
            pass
    return out


def _close(a, b) -> bool:
    try:
        return abs(a - b) <= max(1e-9, abs(a) * 0.005)
    except Exception:
        return False


def _claimed_rate_numbers(row) -> set:
    """Every way this rule's headline rate could legitimately be written down.

    Mirrors credit_card.dart:655-678 so we look for the number a human would
    actually find in the issuer's sentence — '5%' for cashback_pct 0.05,
    '3 points per 150' for points_per_spend, '10X' for a multiplier.
    """
    out = set()
    rate = num(row.get("reward_rate"))
    unit = num(row.get("reward_unit_spend"))
    rt = _s(row.get("reward_type")) or "points_per_spend"
    if rate is None:
        return out
    out.add(rate)
    if rt == "cashback_pct":
        out.add(rate * 100.0)
    elif rt == "points_per_spend" and unit:
        out.add(rate / unit * 100.0)
    return {x for x in out if x}          # a claim of zero is not a claim


def _rule_name(row, j) -> str:
    return _s(row.get("rule_name")) or "(unnamed rule #%d)" % j


# --------------------------------------------------------------------------- #
# pass 1 — build the ledger
# --------------------------------------------------------------------------- #
def _ledger(ctx: Ctx):
    """One record per reward rule, plus one per card. Reads only; mutates nothing."""
    today = _today(ctx)
    max_age = _max_age_days(ctx)
    cards = []

    for _i, entry, inner, cid in ctx.entries():
        key = cid or "(card with no id)"
        issuer = _s(inner.get("issuer"))
        domains = _issuer_domains(issuer)
        rows = entry.get("reward_rules")
        rows = rows if isinstance(rows, list) else []
        recs = []

        for j, r in enumerate(rows):
            rec = {
                "index": j, "name": "(unreadable row #%d)" % j, "ok": False,
                "rate": None, "unit": None, "rtype": None,
                "urls": [], "issuer_url": False, "bad_url": [], "aggregator": [],
                "foreign": [], "has_quote": False, "quote": None,
                "quote_supports": None,        # True / False / None = untestable
                "unit_missing": False, "cap_unsourced": False, "cap_value": None,
                "fetched_raw": None, "fetched": None, "fresh": False,
                "age": None, "conf_key": False, "conf": None,
                "doc_type": None,
            }
            try:
                if not isinstance(r, dict):
                    recs.append(rec)          # row shape is L1's problem
                    continue
                rec["ok"] = True
                rec["name"] = _rule_name(r, j)
                rec["rate"] = num(r.get("reward_rate"))
                rec["unit"] = num(r.get("reward_unit_spend"))
                rec["rtype"] = _s(r.get("reward_type"))

                for where, raw in _url_candidates(r):
                    host = _host_of(raw)
                    rec["urls"].append(raw)
                    if host is None:
                        tok = _aggregator_token(raw)
                        if tok:
                            rec["aggregator"].append((where, tok, raw))
                        else:
                            rec["bad_url"].append((where, raw))
                        continue
                    agg = _aggregator_of(host)
                    if agg:
                        rec["aggregator"].append((where, host, raw))
                    elif domains and _host_matches(host, domains):
                        rec["issuer_url"] = True
                    elif domains:
                        rec["foreign"].append((where, host, raw))
                    # issuer unmapped -> counted once per issuer, not per row

                q = _s(r.get("source_quote"))
                rec["has_quote"] = q is not None
                rec["quote"] = q
                if q is not None:
                    qn = _numbers_in(q)
                    claims = _claimed_rate_numbers(r)
                    if not claims:
                        rec["quote_supports"] = None     # rule claims no number
                    else:
                        rec["quote_supports"] = any(
                            _close(c, x) for c in claims for x in qn)
                    unit = num(r.get("reward_unit_spend"))
                    if unit and not any(_close(unit, x) for x in qn):
                        rec["unit_missing"] = True
                    cap = num(r.get("cap_amount"))
                    if cap and cap > 0:
                        rec["cap_value"] = cap
                        rec["cap_unsourced"] = not any(_close(cap, x) for x in qn)

                rec["fetched_raw"] = r.get("source_fetched_on")
                d = _as_date(rec["fetched_raw"])
                rec["fetched"] = d
                if d is not None:
                    rec["age"] = (today - d).days
                    rec["fresh"] = 0 <= rec["age"] <= max_age

                rec["conf_key"] = "confidence" in r
                rec["conf"] = r.get("confidence")
                rec["doc_type"] = _s(r.get("source_doc_type"))
            except Exception as exc:
                rec["error"] = "%s: %s" % (type(exc).__name__, exc)
            recs.append(rec)

        cards.append({
            "card_id": key, "issuer": issuer, "domains": domains,
            "issuer_mapped": domains is not None, "rules": recs,
            # Imported from L6 rather than re-implemented, for the same reason
            # the runner reads grades out of L8 instead of re-deriving them: two
            # copies of "does this card load" would drift, and then the two
            # layers would give the founder two different answers about the same
            # card. L6 owns that question; this is the only cross-layer import
            # in checks/, and it is a one-way edge (c8 -> c6, no cycle).
            "loads": not _cast_faults(entry, inner),
        })
    return cards, today, max_age


def _grade(card) -> str:
    """A-F on verification. 'N/A' when the card ships no reward rule at all —
    a card with no money numbers has nothing to source, and calling that F would
    bury the 352 cards that really are unsourced."""
    # A card the app cannot read has no verification status. Without this, the
    # single card in this catalogue that crashes CreditCardData.fromOtaJson —
    # yes_bank_uni_rupay, which L6 reports as reaching nobody — was awarded
    # grade B, the top grade anything in the file achieves, held by 6 of 383
    # cards. A scorecard that ranks a card nobody can load above 343 working
    # ones teaches the reader to ignore the scorecard.
    if card.get("loads") is False:
        return "N/A"
    recs = [r for r in card["rules"] if r["ok"]]
    n = len(recs)
    if n == 0:
        return "N/A"
    sourced = sum(1 for r in recs if r["issuer_url"])
    if sourced == 0:
        return "F"
    quoted = sum(1 for r in recs if r["has_quote"] and r["quote_supports"] is not False)
    fresh = sum(1 for r in recs if r["fresh"])
    all_sourced = sourced == n
    all_quoted = quoted == n
    if all_sourced and all_quoted and fresh == n:
        g = "A"
    elif all_sourced and all_quoted:
        g = "B"
    elif sourced * 2 >= n:
        g = "C"
    else:
        g = "D"

    # Caps. A card cannot be graded on coverage alone while the provenance it does
    # carry is wrong — that is how a file gets an A for citing a blog.
    #   hard: the citation itself is invalid (aggregator, a quote that proves a
    #         different number, a fetch date that has not happened) -> never above C
    #   soft: a genuine issuer source exists but the record around it is sloppy
    #         (a placeholder in _sources, an unparseable date)      -> never above B
    hard = any(r["aggregator"] or r["quote_supports"] is False
               or (r["age"] is not None and r["age"] < 0) for r in recs)
    soft = any(r["bad_url"]
               or (r["fetched_raw"] not in (None, "") and r["fetched"] is None)
               for r in recs)
    order = ["A", "B", "C", "D", "F"]
    if hard and order.index(g) < order.index("C"):
        g = "C"
    elif soft and order.index(g) < order.index("B"):
        g = "B"
    return g


_GRADE_MEANING = {
    "A": "every reward rule carries an issuer link, a supporting quote and a recent fetch date",
    "B": "every reward rule is sourced and quoted, but the fetch dates are old or missing",
    "C": "some reward rules are sourced at the issuer, the rest are unverified",
    "D": "a source exists somewhere on this card, but most reward rules are bare",
    "F": "no reward rule on this card carries a link to the issuer",
    "N/A": "there is nothing to verify — the card ships no reward rules, or the app "
           "cannot load it at all",
}


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def _check_headline(ctx: Ctx, cards, out: list) -> None:
    """The one sentence the founder can quote."""
    recs = [r for c in cards for r in c["rules"] if r["ok"]]
    total = len(recs)
    if not total:
        out.append(Finding(
            severity=WARN, code="L8.NO_REWARD_RULES",
            message="This file contains no readable reward rules, so nothing could "
                    "be checked for provenance.",
            impact="The app has no rates to rank cards with.",
            fix="Check that seed/cards.json still carries a reward_rules list on "
                "each card.",
        ))
        return

    issuer_sourced = sum(1 for r in recs if r["issuer_url"])
    any_url = sum(1 for r in recs if r["urls"])
    quoted = sum(1 for r in recs if r["has_quote"])
    supported = sum(1 for r in recs if r["quote_supports"] is True)
    dated = sum(1 for r in recs if r["fetched"] is not None)
    pct = issuer_sourced * 100.0 / total
    floor = _min_sourced_pct(ctx)

    out.append(Finding(
        severity=ERROR if pct < floor else INFO,
        code="L8.HEADLINE_VERIFIED_SHARE",
        message=("Only %d of the %d reward rates in this file (%.1f%%) are backed by a "
                 "link to the issuer's own website. The other %d — the numbers the app "
                 "uses to tell a real person which card to swipe — are things we believe "
                 "but cannot show anyone."
                 % (issuer_sourced, total, pct, total - issuer_sourced)),
        block="reward_rules", field="source_url",
        evidence=("issuer-sourced %d/%d (%.1f%%) | any URL at all %d | quoted %d | "
                  "quote proves the number %d | carries a fetch date %d"
                  % (issuer_sourced, total, pct, any_url, quoted, supported, dated)),
        impact="If an issuer changes a rate, we have no document to diff against and no "
               "way to tell a user why we said what we said. It is also the answer to "
               "'how do you know?' from an investor, a partner bank or a regulator.",
        fix="Treat issuer-sourced coverage as the headline data metric. The weekly "
            "pipeline already fetches issuer pages — make it write source_url, "
            "source_quote and source_fetched_on onto every rule it touches, and count "
            "this number every week.",
    ))


def _check_implicit_high_confidence(ctx: Ctx, cards, out: list) -> None:
    """The single most misleading thing in this file."""
    recs = [r for c in cards for r in c["rules"] if r["ok"]]
    total = len(recs)
    if not total:
        return
    missing = [r for r in recs if not r["conf_key"]]
    n = len(missing)
    if not n:
        return
    unsourced_and_implicit = sum(1 for r in missing if not r["issuer_url"])
    worst = Counter()
    for c in cards:
        k = sum(1 for r in c["rules"] if r["ok"] and not r["conf_key"])
        if k:
            worst[c["card_id"]] = k
    top = ", ".join("%s (%d)" % (k, v) for k, v in worst.most_common(5))

    out.append(Finding(
        severity=ERROR, code="L8.CONFIDENCE_DEFAULTS_TO_HIGH",
        message=("%d of %d reward rules (%.1f%%) carry no confidence value, and the app "
                 "treats a missing value as 'high'. So %d rules present themselves to "
                 "users as high-confidence facts that nobody ever verified — %d of them "
                 "with no issuer link either."
                 % (n, total, n * 100.0 / total, n, unsourced_and_implicit)),
        block="reward_rules", field="confidence",
        evidence=("credit_card.dart:463 reads json['confidence'] ?? '%s'; "
                  "%d/%d rules omit the key. Largest cards: %s"
                  % (_DART_CONFIDENCE_DEFAULT, n, total, top or "n/a")),
        impact="Every unverified rate is indistinguishable at runtime from a rate we "
               "read off the issuer's own page. Nothing in the app can warn a user, "
               "grey out a number, or rank a verified card above a guessed one.",
        fix="Two moves, in this order. (1) Write an explicit confidence on every reward "
            "rule — 'low' is the honest default for a rule with no source_url. "
            "(2) Change the Dart default from 'high' to 'low' so silence stops meaning "
            "certainty. Note the app currently has NO consumer for confidence at all, "
            "so step 2 alone changes nothing a user sees until the UI reads it.",
    ))


def _check_url_quality(ctx: Ctx, cards, out: list) -> None:
    """Where a URL exists, is it the RIGHT url?"""
    unmapped = defaultdict(list)          # issuer string -> card ids

    for c in cards:
        if not c["issuer_mapped"]:
            unmapped[c["issuer"] or "(no issuer)"].append(c["card_id"])

        agg = []
        foreign = defaultdict(list)
        bad = []
        for r in c["rules"]:
            if not r["ok"]:
                continue
            for _where, host, raw in r["aggregator"]:
                agg.append((host, raw, r["name"]))
            for _where, host, raw in r["foreign"]:
                foreign[host].append(r["name"])
            for where, raw in r["bad_url"]:
                bad.append((where, raw, r["name"]))

        if agg:
            hosts = sorted({h for h, _u, _n in agg})
            out.append(Finding(
                severity=ERROR, code="L8.SOURCE_IS_AGGREGATOR",
                message=("%d reward rule(s) on this card cite a review site or news "
                         "article instead of the issuer: %s."
                         % (len(agg), ", ".join(hosts))),
                card_id=c["card_id"], block="reward_rules", field="source_url",
                evidence=trunc("; ".join("%s <- %s" % (n, u) for _h, u, n in agg[:3])),
                impact="These sites lag every devaluation and copy each other. A rate "
                       "cited from one is a rumour with a footnote, and if a user "
                       "challenges it we have nothing to stand on.",
                fix="Replace with the issuer's own product page, MITC or fee schedule "
                    "PDF, and re-read the number from there. KredMe policy is official "
                    "issuer URL only.",
            ))

        for host, names in sorted(foreign.items()):
            out.append(Finding(
                severity=WARN, code="L8.SOURCE_NOT_ISSUER_DOMAIN",
                message=("%d reward rule(s) on this card are sourced from '%s', which "
                         "is not a %s website." % (len(names), host, c["issuer"] or "?")),
                card_id=c["card_id"], block="reward_rules", field="source_url",
                evidence=trunc("expected one of: %s | rules: %s"
                               % (", ".join(c["domains"] or ()), "; ".join(names[:3]))),
                impact="A co-brand partner's marketing page and the issuer's terms often "
                       "disagree, and it is the issuer's terms that decide what a user "
                       "actually earns.",
                fix="If this is a legitimate co-brand page, find the same number on the "
                    "issuer's own site and cite that instead. If the domain genuinely "
                    "belongs to this issuer, add it to _ISSUER_DOMAINS in "
                    "tools/checks/c8_provenance.py.",
            ))

        if bad:
            out.append(Finding(
                severity=ERROR, code="L8.SOURCE_URL_NOT_A_URL",
                message=("%d reward rule(s) on this card claim a source that is not a "
                         "web address at all." % len(bad)),
                card_id=c["card_id"], block="reward_rules", field="source_url/_sources",
                evidence=trunc("; ".join("%s=%r (rule: %s)" % (w, u, n)
                                         for w, u, n in bad[:3])),
                impact="It reads as provenance in every report and in the app's data, "
                       "but there is nothing anyone can open. It is worse than an empty "
                       "field because it counts as sourced.",
                fix="Replace the placeholder with the full https:// address of the "
                    "issuer page the number was read from, or delete the field.",
            ))

    for issuer, cids in sorted(unmapped.items()):
        out.append(Finding(
            severity=WARN, code="L8.ISSUER_DOMAIN_UNMAPPED",
            message=("The issuer '%s' is not in this validator's issuer-to-website map, "
                     "so sources on its %d card(s) cannot be checked for being official."
                     % (issuer, len(cids))),
            block="card", field="issuer", evidence=trunc(", ".join(sorted(cids)[:8])),
            impact="A source on these cards could be an aggregator and this check would "
                   "not notice.",
            fix="Add '%s' and its official domain(s) to _ISSUER_DOMAINS in "
                "tools/checks/c8_provenance.py. Do this by hand — never by string "
                "similarity, which scores 'AU Bank' and 'Axis Bank' 0.750 alike."
                % issuer,
        ))


def _check_quotes(ctx: Ctx, cards, out: list) -> None:
    """A quote that does not support its own rule is worse than no quote."""
    for c in cards:
        unsupported = []
        no_url = []
        url_no_quote = []
        cap_unsourced = []
        unit_missing = []
        for r in c["rules"]:
            if not r["ok"]:
                continue
            if r["quote_supports"] is False:
                unsupported.append(r)
            if r["has_quote"] and not r["issuer_url"] and not r["urls"]:
                no_url.append(r)
            if r["urls"] and not r["has_quote"]:
                url_no_quote.append(r)
            if r["cap_unsourced"]:
                cap_unsourced.append(r)
            if r["has_quote"] and r["quote_supports"] is not False and r["unit_missing"]:
                unit_missing.append(r)

        if unsupported:
            out.append(Finding(
                severity=ERROR, code="L8.QUOTE_DOES_NOT_SUPPORT_RATE",
                message=("%d reward rule(s) on this card quote the issuer, but the "
                         "sentence quoted does not contain the number the rule claims."
                         % len(unsupported)),
                card_id=c["card_id"], block="reward_rules", field="source_quote",
                index=unsupported[0]["index"],
                evidence=trunc("rule: %s | claims %s=%s per %s | quote says: %s"
                               % (unsupported[0]["name"],
                                  unsupported[0]["rtype"] or "?",
                                  unsupported[0]["rate"],
                                  unsupported[0]["unit"] if unsupported[0]["unit"]
                                  else "n/a",
                                  unsupported[0]["quote"] or ""), 240),
                impact="This is the most dangerous state in the file. The rule looks "
                       "verified in every report and to anyone reviewing the data, and "
                       "the evidence attached to it is about something else.",
                fix="Open the cited page, find the sentence that states this rule's own "
                    "rate, and paste that. If the issuer does not state it, delete the "
                    "quote and set confidence to 'low' — an honest blank beats a "
                    "misleading citation.",
            ))

        if no_url:
            out.append(Finding(
                severity=WARN, code="L8.QUOTE_WITHOUT_SOURCE_URL",
                message=("%d reward rule(s) on this card carry a quote from the issuer "
                         "but no link, so nobody can check the quote is real or still "
                         "current." % len(no_url)),
                card_id=c["card_id"], block="reward_rules", field="source_quote",
                evidence=trunc("; ".join(r["name"] for r in no_url[:3])),
                impact="The quote reads as proof but cannot be re-verified after the "
                       "issuer edits the page, which they do without notice.",
                fix="Add source_url pointing at the page the sentence was copied from, "
                    "plus source_fetched_on with the date it was read.",
            ))

        if url_no_quote:
            out.append(Finding(
                severity=WARN, code="L8.SOURCE_URL_WITHOUT_QUOTE",
                message=("%d reward rule(s) on this card link to a source page but do "
                         "not record what it actually said." % len(url_no_quote)),
                card_id=c["card_id"], block="reward_rules", field="source_url",
                evidence=trunc("; ".join(r["name"] for r in url_no_quote[:3])),
                impact="When the issuer rewrites that page the link still resolves and "
                       "still looks like proof, but the sentence we relied on is gone "
                       "and nothing records that it changed.",
                fix="Copy the exact sentence stating the rate into source_quote. That "
                    "sentence is what makes the next devaluation detectable.",
            ))

        if cap_unsourced:
            caps = sorted({r["cap_value"] for r in cap_unsourced})
            out.append(Finding(
                severity=WARN, code="L8.CAP_NOT_IN_QUOTE",
                message=("%d reward rule(s) on this card have a spending cap that does "
                         "not appear anywhere in the quote cited as their source."
                         % len(cap_unsourced)),
                card_id=c["card_id"], block="reward_rules", field="cap_amount",
                evidence=trunc("cap values with no supporting text: %s | e.g. %s"
                               % (", ".join("%g" % x for x in caps[:8]),
                                  cap_unsourced[0]["name"])),
                impact="The cap is the number that decides where a user's reward stops. "
                       "Quoting a headline rate and inventing the cap makes the card "
                       "look better than it is, right up to the month it stops paying.",
                fix="Quote the issuer sentence that states the cap too, or split the "
                    "quote so the rate and the cap each cite the text that proves them.",
            ))

        if unit_missing:
            out.append(Finding(
                severity=WARN, code="L8.SPEND_BLOCK_NOT_IN_QUOTE",
                message=("%d reward rule(s) on this card earn per block of spend, but "
                         "the block size is not in the quoted source." % len(unit_missing)),
                card_id=c["card_id"], block="reward_rules", field="reward_unit_spend",
                evidence=trunc("; ".join(r["name"] for r in unit_missing[:3])),
                impact="The block size is exactly what made the IndianOil Kotak numbers "
                       "wrong: '3 points per 150' and '3 points per 100' look identical "
                       "in a report and pay 33% differently.",
                fix="Quote the sentence that names both the points and the rupee block, "
                    "e.g. 'Earn 3 Reward Points on every INR 150 spent'.",
            ))


def _check_dates(ctx: Ctx, cards, out: list, today, max_age) -> None:
    """Staleness — and the bigger problem, sources with no date at all."""
    for c in cards:
        stale = []
        unreadable = []
        future = []
        undated = []
        for r in c["rules"]:
            if not r["ok"]:
                continue
            raw = r["fetched_raw"]
            if raw is None or raw == "":
                if r["urls"] or r["has_quote"]:
                    undated.append(r)
                continue
            if r["fetched"] is None:
                unreadable.append((r, raw))
                continue
            if r["age"] is not None and r["age"] < 0:
                future.append((r, raw))
            elif r["age"] is not None and r["age"] > max_age:
                stale.append((r, raw))

        if stale:
            oldest = max(x[0]["age"] for x in stale)
            out.append(Finding(
                severity=WARN, code="L8.SOURCE_STALE",
                message=("%d reward rule(s) on this card were last checked against the "
                         "issuer more than %d days ago — the oldest is %d days old."
                         % (len(stale), max_age, oldest)),
                card_id=c["card_id"], block="reward_rules", field="source_fetched_on",
                evidence=trunc("; ".join("%s (%s)" % (r["name"], raw)
                                         for r, raw in stale[:3])),
                impact="Indian issuers devalue several times a year and rarely announce "
                       "it. A rate last confirmed months ago is a guess wearing a "
                       "citation.",
                fix="Re-fetch the issuer page, re-read the number, and update both the "
                    "rate and source_fetched_on. Raise or lower the window with "
                    "provenance_max_age_days in the validator config.",
            ))

        if unreadable:
            out.append(Finding(
                severity=ERROR, code="L8.SOURCE_DATE_UNREADABLE",
                message=("%d reward rule(s) on this card have a source date that is not "
                         "a YYYY-MM-DD date." % len(unreadable)),
                card_id=c["card_id"], block="reward_rules", field="source_fetched_on",
                evidence=trunc("; ".join("%s = %r" % (r["name"], raw)
                                         for r, raw in unreadable[:3])),
                impact="Nothing can tell whether this source is a week or a year old, so "
                       "staleness is invisible for these rules.",
                fix="Write the date the page was read as YYYY-MM-DD.",
            ))

        if future:
            out.append(Finding(
                severity=ERROR, code="L8.SOURCE_DATE_IN_FUTURE",
                message=("%d reward rule(s) on this card claim to have been checked "
                         "against the issuer on a date that has not happened yet."
                         % len(future)),
                card_id=c["card_id"], block="reward_rules", field="source_fetched_on",
                evidence=trunc("; ".join("%s = %s (today %s)" % (r["name"], raw, today)
                                         for r, raw in future[:3])),
                impact="The date was typed, not recorded, which means the verification "
                       "it stands for probably did not happen either.",
                fix="Correct the date to the day the page was actually read.",
            ))

        if undated:
            out.append(Finding(
                severity=WARN, code="L8.SOURCE_UNDATED",
                message=("%d reward rule(s) on this card cite a source but never record "
                         "when it was read, so this check cannot tell whether the number "
                         "is current." % len(undated)),
                card_id=c["card_id"], block="reward_rules", field="source_fetched_on",
                evidence=trunc("; ".join(r["name"] for r in undated[:3])),
                impact="Provenance without a date cannot expire. These rules will look "
                       "verified forever, including years after the issuer changed them.",
                fix="Add source_fetched_on = the date the page was read, every time a "
                    "source_url or source_quote is written.",
            ))


def _check_confidence_values(ctx: Ctx, cards, out: list) -> None:
    """Explicit confidence values: are they in our vocabulary, and are they earned?"""
    for c in cards:
        odd = []
        unearned = []
        for r in c["rules"]:
            if not r["ok"] or not r["conf_key"]:
                continue
            v = r["conf"]
            sv = v.strip().lower() if isinstance(v, str) else None
            if sv is None or sv not in _CONFIDENCE_VOCAB:
                odd.append((r, v))
                continue
            if sv == "high" and not r["issuer_url"]:
                unearned.append(r)

        if odd:
            out.append(Finding(
                severity=WARN, code="L8.CONFIDENCE_VALUE_UNKNOWN",
                message=("%d reward rule(s) on this card carry a confidence value that "
                         "is not high, medium or low." % len(odd)),
                card_id=c["card_id"], block="reward_rules", field="confidence",
                evidence=trunc("; ".join("%s = %r" % (r["name"], v) for r, v in odd[:3])),
                impact="The app has no vocabulary for this field either, so an unexpected "
                       "value is stored and ignored rather than rejected.",
                fix="Use high / medium / low. 'low' is the correct value for anything "
                    "without an issuer link.",
            ))

        if unearned:
            out.append(Finding(
                severity=ERROR, code="L8.CONFIDENCE_HIGH_UNSOURCED",
                message=("%d reward rule(s) on this card explicitly declare high "
                         "confidence while carrying no link to the issuer."
                         % len(unearned)),
                card_id=c["card_id"], block="reward_rules", field="confidence",
                evidence=trunc("; ".join(
                    "%s%s" % (r["name"], " (has a quote but no link)" if r["has_quote"]
                              else " (nothing at all)") for r in unearned[:3])),
                impact="Someone typed 'high' by hand. That is a stronger claim than the "
                       "missing-key default, and there is still nothing behind it.",
                fix="Either add the issuer source_url that justifies 'high', or lower "
                    "the value to 'low'.",
            ))


def _check_grades(ctx: Ctx, cards, out: list) -> None:
    """The ledger: a grade for every single card, plus the portfolio picture."""
    dist = Counter()
    by_issuer = defaultdict(Counter)

    for c in cards:
        g = _grade(c)
        dist[g] += 1
        by_issuer[c["issuer"] or "(no issuer)"][g] += 1
        recs = [r for r in c["rules"] if r["ok"]]
        n = len(recs)
        sourced = sum(1 for r in recs if r["issuer_url"])
        quoted = sum(1 for r in recs if r["has_quote"])
        supported = sum(1 for r in recs if r["quote_supports"] is True)
        fresh = sum(1 for r in recs if r["fresh"])
        dated = sum(1 for r in recs if r["fetched"] is not None)
        conf = sum(1 for r in recs if r["conf_key"])

        out.append(Finding(
            severity=INFO, code="L8.CARD_GRADE",
            message=("Verification grade %s — %s." % (g, _GRADE_MEANING.get(g, ""))),
            card_id=c["card_id"], block="reward_rules",
            field="source_url",
            evidence=("grade=%s rules=%d issuer_sourced=%d quoted=%d quote_proves_rate=%d "
                      "dated=%d fetched_within_window=%d confidence_stated=%d issuer=%s"
                      % (g, n, sourced, quoted, supported, dated, fresh, conf,
                         c["issuer"] or "?")),
            impact=("Nothing on this card can be defended to a user or a partner."
                    if g == "F" else
                    "This card has nothing to verify — it shows no rewards at all."
                    if g == "N/A" else
                    "Part of what this card promises a user is unverified."
                    if g in ("C", "D") else
                    "This card's rewards can be traced back to the issuer."),
            fix=("Fetch this card's page at %s and record source_url, source_quote and "
                 "source_fetched_on on its reward rules."
                 % (", ".join(c["domains"] or ()) or "the issuer's website")
                 if g in ("C", "D", "F") else
                 "Add a card and its reward rules, or remove it from the file."
                 if g == "N/A" else
                 "Re-fetch to refresh the dates." if g == "B" else
                 "Keep it this way — re-check when the issuer next updates the page."),
        ))

    total = sum(dist.values())
    ordered = [g for g in ("A", "B", "C", "D", "F", "N/A") if dist.get(g)]
    summary = " | ".join("%s: %d (%.1f%%)" % (g, dist[g], dist[g] * 100.0 / total)
                         for g in ordered) if total else "no cards"
    good = dist.get("A", 0) + dist.get("B", 0)

    worst_issuers = sorted(
        ((iss, cnt.get("F", 0), sum(cnt.values())) for iss, cnt in by_issuer.items()),
        key=lambda t: (-t[1], t[0]))[:5]

    out.append(Finding(
        # WARN, deliberately not ERROR: L8.HEADLINE_VERIFIED_SHARE already carries
        # the alarm for this same underlying gap, measured per rule instead of per
        # card. Two ERRORs for one defect is the double-counting this layer bans.
        severity=WARN if total and good * 2 < total else INFO,
        code="L8.PORTFOLIO_VERIFICATION_GRADES",
        message=("Across all %d cards, %d are fully verified (grade A or B) and %d have "
                 "no issuer source on any reward rule (grade F). Distribution: %s."
                 % (total, good, dist.get("F", 0), summary)),
        block="reward_rules",
        evidence=trunc("worst issuers by F-graded cards: "
                       + "; ".join("%s %d/%d" % (i, f, n) for i, f, n in worst_issuers),
                       220),
        impact="Grade F means we cannot show anyone where that card's numbers came from. "
               "At this ratio, 'our card data is verified' is not a claim the company "
               "can make in a pitch, a store listing or a partner conversation.",
        fix="Work the grades in order: F cards with the most users first, then D, then C. "
            "The weekly pipeline already fetches most of these pages — the missing step "
            "is writing the source fields back onto each rule it confirms.",
    ))


def _classify_source(raw, domains):
    """('issuer' | 'aggregator' | 'foreign' | 'placeholder', detail)."""
    host = _host_of(raw)
    if host is None:
        tok = _aggregator_token(raw)
        return ("aggregator", tok) if tok else ("placeholder", raw)
    agg = _aggregator_of(host)
    if agg:
        return ("aggregator", host)
    if domains and _host_matches(host, domains):
        return ("issuer", host)
    if not domains:
        return ("foreign", host)          # issuer unmapped — cannot claim issuer
    return ("foreign", host)


def _check_other_blocks(ctx: Ctx, cards, out: list) -> None:
    """The four non-reward blocks. They also move a user's money — an exclusion
    decides when a card earns nothing, a redemption value decides what a point is
    worth — so the same bar applies.

    Sources here are counted the HONEST way: only a value that names a real
    website counts as sourced. Counting a placeholder as provenance is exactly the
    failure this layer exists to catch, and this check must not commit it.
    """
    domains_by_card = {c["card_id"]: c["domains"] for c in cards}
    stats = {}
    agg_rows = Counter()                  # aggregator name -> rows
    agg_cards = defaultdict(set)          # aggregator name -> card ids
    agg_blocks = defaultdict(set)
    agg_sample = {}

    for block in _OTHER_BLOCKS:
        rows = issuer = foreign = aggregator = placeholder = quotes = conf = 0
        for cid, _inner, _j, r in ctx.rules(block):
            if not isinstance(r, dict):
                continue
            rows += 1
            key = cid or "(card with no id)"
            kinds = set()
            for _where, raw in _url_candidates(r):
                kind, detail = _classify_source(raw, domains_by_card.get(key))
                kinds.add(kind)
                if kind == "aggregator":
                    agg_rows[detail] += 1
                    agg_cards[detail].add(key)
                    agg_blocks[detail].add(block)
                    agg_sample.setdefault(detail, "%s / %s = %s"
                                          % (key, block, trunc(raw, 60)))
            if "issuer" in kinds:
                issuer += 1
            elif "aggregator" in kinds:
                aggregator += 1
            elif "foreign" in kinds:
                foreign += 1
            elif kinds:
                placeholder += 1
            if _s(r.get("source_quote")) or r.get("_source_quotes"):
                quotes += 1
            if "confidence" in r:
                conf += 1
        stats[block] = (rows, issuer, aggregator, foreign, placeholder, quotes, conf)

    # One finding per aggregator, not per card. This is deliberate and is the ONE
    # place this layer collapses across cards: a single token repeated on hundreds
    # of rows is one authoring decision, not hundreds of independent mistakes, and
    # the fix is one sweep. The message carries the card count so the scale is not
    # hidden, and the reward-rule aggregator check above stays strictly per-card.
    for name, n in agg_rows.most_common():
        out.append(Finding(
            severity=ERROR, code="L8.AGGREGATOR_SOURCE_IN_DATA",
            message=("%d rows across %d cards cite '%s' as their source. That is a "
                     "card-review site, not the issuer, and KredMe policy is issuer "
                     "pages only." % (n, len(agg_cards[name]), name)),
            block=", ".join(sorted(agg_blocks[name])), field="_sources/source_url",
            evidence=trunc("e.g. %s" % agg_sample.get(name, name), 160),
            impact="These numbers were copied from a site that copies other sites. They "
                   "carry that site's mistakes and its lag behind every devaluation, and "
                   "we cannot show a user, a partner bank or an investor where any of it "
                   "came from.",
            fix="Re-source these from the issuer's own page or MITC and replace the "
                "token with the full https:// address. Until then treat every one of "
                "these values as unverified and set their confidence to 'low'.",
        ))

    parts = []
    for block, (rows, issuer, aggregator, foreign, placeholder,
                quotes, conf) in stats.items():
        if not rows:
            continue
        parts.append("%s %d rows (issuer %d, aggregator %d, other site %d, "
                     "not a website %d, quoted %d, confidence stated %d)"
                     % (block, rows, issuer, aggregator, foreign, placeholder,
                        quotes, conf))
    if not parts:
        return

    red = stats.get("redemption_rules", (0, 0, 0, 0, 0, 0, 0))
    note = ""
    if red[0] and red[6] == red[0]:
        note = (" One thing here is done right: all %d redemption rows state an explicit "
                "confidence, so a shaky point value is visible as shaky. reward_rules "
                "could be filled the same way." % red[0])

    out.append(Finding(
        severity=INFO, code="L8.OTHER_BLOCK_PROVENANCE",
        message=("Provenance outside the reward rules: %s.%s"
                 % ("; ".join(parts), note)),
        evidence=trunc("; ".join("%s %d/%d at the issuer" % (b, v[1], v[0])
                                 for b, v in stats.items() if v[0]), 200),
        impact="Exclusions decide when a card earns nothing and redemption values decide "
               "what a point is worth. Both change a user's money.",
        fix="Extend whatever writes provenance onto reward rules to cover these blocks. "
            "Note the file uses two conventions — source_quote (a string) on most blocks "
            "and _source_quotes (an object) on redemption_rules; pick one.",
    ))


def _check_evidence_backlog(ctx: Ctx, cards, out: list) -> None:
    """How many switched-on cards cite nothing at all — the pipeline's work queue.

    L8.HEADLINE_VERIFIED_SHARE already counts RULES, and rules are the right unit
    for "how true is this file". This counts CARDS, because a card is the unit the
    weekly pipeline reads: one card, one issuer document, one model call, one line
    on the bill. A founder asking "what would it cost to fix this" needs the card
    number, and it is not derivable from the rule number.

    The predicate is `pipeline.provenance.card_has_issuer_evidence`, which is the
    same function `refresh --unsourced-only` uses to build its batch. That is the
    point of importing it: this finding and that selection are the same integer,
    so a report can never say 336 while the pipeline queues something else.

    INFO, not WARN: nothing here is a defect in the file. It is a measurement of
    work not yet done, and HEADLINE_VERIFIED_SHARE already carries the severity
    for the portfolio being unverified. Two ERRORs for one condition trains a
    reader to skim both.
    """
    unsourced, active, no_id = [], 0, 0
    for _i, entry, inner, cid in ctx.entries():
        if not _truthy_active(inner):
            continue
        active += 1
        if not cid:
            no_id += 1
            continue
        if not _card_has_evidence(entry):
            unsourced.append(cid)

    if not active or not unsourced:
        return

    # Bucketed on the SLUG the pipeline's scheduler uses, not the seed's raw
    # issuer string. This file spells 21 banks 34 ways — BOBCARD six ways, AU
    # three, IDFC three, YES two — so grouping on the raw string split BOBCARD's
    # 19-card gap into 10+4+2+1+1+1 and pushed four of the ten largest gaps off
    # this list entirely. Same key as the report, so the two agree.
    from pipeline.sources import issuer_of as _issuer_slug   # noqa: PLC0415

    gap = set(unsourced)
    by_issuer = Counter()
    for _i, entry, inner, cid in ctx.entries():
        if cid in gap:
            by_issuer[_issuer_slug(entry) or "(no issuer named)"] += 1
    worst = ", ".join("%s %d" % (name, n) for name, n in by_issuer.most_common(5))

    out.append(Finding(
        severity=INFO, code="L8.CARDS_WITH_NO_EVIDENCE",
        message=("%d of the %d cards switched on in the app have no REWARD RULE "
                 "citing a document. Some carry a card-level provenance stamp about "
                 "an annual fee or a forex rate — that is evidence about a card "
                 "field, not about the numbers this layer counts."
                 % (len(unsourced), active)),
        block="reward_rules", field="source_url",
        evidence="unsourced %d/%d | worst: %s" % (len(unsourced), active, worst),
        impact="These cards cannot be diffed against the issuer when a rate changes, "
               "and their numbers cannot be shown to anyone who asks how we know.",
        fix="Run `python3 pipeline/cli.py evidence` for the per-issuer backlog and what "
            "clearing it costs, then `python3 pipeline/cli.py refresh --unsourced-only "
            "--limit N` to read a slice of it. That command selects on this exact "
            "predicate — it is the same function this check just called.",
    ))


def _truthy_active(inner) -> bool:
    """is_active as sources.resolve_sources reads it: absent means ACTIVE.

    Matching that default matters. If this layer counted an absent flag as
    inactive it would report a smaller backlog than the pipeline queues, and the
    two numbers would disagree for reasons nobody could see in the data.
    """
    v = _d(inner).get("is_active", 1)
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() not in ("0", "false", "no", "")


def _check_unreadable_rows(ctx: Ctx, cards, out: list) -> None:
    """Rows this layer could not inspect, so nobody mistakes silence for a pass."""
    for c in cards:
        bad = [r for r in c["rules"] if not r["ok"] or r.get("error")]
        if not bad:
            continue
        out.append(Finding(
            severity=WARN, code="L8.ROW_NOT_INSPECTABLE",
            message=("%d reward rule row(s) on this card could not be read, so their "
                     "sources were not checked." % len(bad)),
            card_id=c["card_id"], block="reward_rules", index=bad[0]["index"],
            evidence=trunc(bad[0].get("error") or "row is not an object"),
            impact="These rows are excluded from this card's verification grade, so the "
                   "grade is more generous than the truth.",
            fix="Fix the row's shape first (that is layer 1's finding), then re-run.",
        ))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    try:
        cards, today, max_age = _ledger(ctx)
    except Exception as exc:
        return [Finding(
            severity=WARN, code="L8.LEDGER_ABORTED",
            message="The verification ledger could not be built, so no card was checked "
                    "for provenance.",
            evidence=trunc("%s: %s" % (type(exc).__name__, exc)),
            impact="This run says nothing about whether our numbers are verified.",
            fix="Report this to whoever maintains tools/checks/c8_provenance.py.",
        )]

    steps = (
        (_check_headline, (ctx, cards, out)),
        (_check_evidence_backlog, (ctx, cards, out)),
        (_check_implicit_high_confidence, (ctx, cards, out)),
        (_check_url_quality, (ctx, cards, out)),
        (_check_quotes, (ctx, cards, out)),
        (_check_dates, (ctx, cards, out, today, max_age)),
        (_check_confidence_values, (ctx, cards, out)),
        (_check_other_blocks, (ctx, cards, out)),
        (_check_unreadable_rows, (ctx, cards, out)),
        (_check_grades, (ctx, cards, out)),
    )
    for step, args in steps:
        try:
            step(*args)
        except Exception as exc:
            out.append(Finding(
                severity=WARN, code="L8.CHECK_ABORTED",
                message="Part of the provenance check could not finish, so some "
                        "unverified numbers may be unreported.",
                evidence=trunc("%s: %s: %s" % (step.__name__, type(exc).__name__, exc)),
                impact="This run is not a clean bill of health for provenance.",
                fix="Report this to whoever maintains tools/checks/c8_provenance.py.",
            ))
    return out
