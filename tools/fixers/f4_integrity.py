"""F4 — schema, provenance & lifecycle repairs.

Structural, provenance and temporal defects: rows filed under key names the app
never reads, provenance placeholders that are not links, confidence values that
claim more than the file can support, and dates that contradict the row they sit
on.

THE HEADLINE FIX, AND THE VALUE IT ACTUALLY WRITES
--------------------------------------------------
credit_card.dart:413 reads

    confidence: json['confidence'] as String? ?? 'high'

and 1,218 of the 1,279 reward rules omit the key. So 95.2% of our rates ship
asserting high confidence that nobody earned. The repair is to write the value
down instead of letting a Dart default speak for us.

The brief proposed writing "unverified". I read the Dart before writing a line
of this, and "unverified" IS NOT IN THE APP'S VOCABULARY. Three measurements:

  1. lib/shared/models/credit_card.dart:280 documents the field as
     "Data confidence level — 'high' | 'medium' | 'low'." Those are the only
     three values the app's own author declared.
  2. tools/checks/c8_provenance.py:151 encodes the same three as
     _CONFIDENCE_VOCAB, and c8._check_confidence_values raises
     L8.CONFIDENCE_VALUE_UNKNOWN (WARN) on anything else. Shipping "unverified"
     would have traded one ERROR for ~370 new WARNings — a validator that goes
     louder is not a data file that got better.
  3. grep for `confidence` across the whole of lib/ returns the declaration, the
     constructor default and the parse — and NOTHING ELSE. No widget renders it,
     no switch branches on it, the recommendation engine never reads it. So an
     unrecognised value would not crash or mis-render today; it would simply be
     a word we invented, sitting in a public repo, outside the vocabulary our own
     app and our own validator agree on. That is a smaller harm than a crash and
     a bigger one than doing nothing, because it is uncheckable.

SO THIS MODULE WRITES "low", NOT "unverified" — see SAFE_CONFIDENCE_FOR_UNSOURCED
below. "low" is in the app's declared vocabulary, in the validator's vocabulary,
already in live use on 234 redemption rows, and it is the exact value both
c8's own fix text and the fix taxonomy prescribe. It means what "unverified"
was meant to mean, in a word every reader of this file already understands.

WHAT THIS MODULE REFUSES TO DO
------------------------------
Two of the codes it owns are owned precisely so that nothing else touches them:

  L7.DUPLICATE_RULE_NAME     The only repair is a rename, and the app keys each
                             user's saved cap progress on the rule NAME. A
                             rename silently resets every affected user's spend
                             bucket to zero. Reported for a human, never fixed.
  L3.CARD_ID_UNSAFE_CHARACTER  The only repair is renaming an id that is already
                             on handsets as the key of saved-card records.

They are listed in HANDLES so the runner routes them here and they stop. See
REFUSED for the machine-readable version.

FOUR THINGS THE RUNNER MUST KNOW
--------------------------------
1. TWO EDITS DO NOT TARGET cards.json. Every edit whose notes carry
   "not_a_card": True addresses another file, named in notes["target_file"] —
   news/feed.json (block "news", index = position in feed["items"]) and
   seed/manifest.json (block "manifest"). Both carry card_id None. A runner that
   only knows how to write cards.json must SKIP these and say so, never silently
   drop them.
2. ONE EDIT ADDRESSES A KEY ON THE CARD ENTRY, not on a row: block None,
   index None, field "exclusion_rules", notes {"target": "entry"}. It sets
   entry["exclusion_rules"], the sibling of entry["card"].
3. APPLYING THESE EDITS INVALIDATES seed/manifest.json. Its checksums, sizes and
   stats describe the file before the edit; leaving them is what turns a good
   fix into a failed publish. Regenerate the manifest after applying, with the
   publish tool rather than by hand.
4. ORDER MATTERS AGAINST OTHER MODULES, in one place only. The 543 redemption
   confidence edits are anchored at block "redemption_rules". If another module
   renames that block to "redemption_channels" (the L6.REDEMPTION_BLOCK_NEVER_READ
   repair), apply these first or re-anchor them — otherwise they land nowhere.

Stdlib only. plan() is PURE: it reads ctx and returns proposed edits, and it
opens nothing for writing.
"""
from __future__ import annotations

import copy
import datetime
import re

from fixers.base import CERTAIN, LIKELY, Edit, trunc

# The definition of "is this row sourced to the issuer?" is imported, never
# re-implemented. Two copies of that question would drift, and then the checker
# and the fixer would disagree about which rules deserve to keep 'high' — the
# same argument c8_provenance makes for importing card-loads from c6 rather than
# re-deriving it. This is a one-way edge (fixers -> checks) with no cycle.
from checks import c1_schema as _c1
from checks import c8_provenance as _c8
from checks import c9_temporal as _c9

FAMILY = "schema, provenance & lifecycle"

HANDLES = [
    # -- provenance & confidence (the headline) ---------------------------- #
    "L8.CONFIDENCE_DEFAULTS_TO_HIGH",
    "L8.CONFIDENCE_HIGH_UNSOURCED",
    "L8.AGGREGATOR_SOURCE_IN_DATA",
    "L8.SOURCE_URL_NOT_A_URL",
    # -- schema / key names ------------------------------------------------ #
    "L1.BLOCK_NOT_A_LIST",
    "L1.UNREAD_ALIAS_KEY",
    "L6.PAYLOAD_UNDER_UNREAD_KEY",
    "L4.MILESTONE_TARGET_MISSING",
    "L4.FUEL_MIN_ABOVE_MAX",
    # -- lifecycle / temporal ---------------------------------------------- #
    "L9.NEWS_DATE_CONTRADICTS_ITS_OWN_ID",
    "L9.NEWS_ONE_TIMESTAMP_FOR_MANY_STORIES",
    "L9.NEWS_VERSION_DISAGREES_WITH_MANIFEST",
    # -- owned so that nothing else attempts them -------------------------- #
    "L7.DUPLICATE_RULE_NAME",
    "L3.CARD_ID_UNSAFE_CHARACTER",
    "L9.MERCHANT_STAMP_NOT_MAINTAINED",
]

# Codes this module owns and deliberately does not repair. The runner may print
# these verbatim; each one is a refusal with a stated cost, not an oversight.
REFUSED: dict[str, str] = {
    "L7.DUPLICATE_RULE_NAME":
        "The only repair is renaming a rule, and the app buckets each user's "
        "saved cap progress under the rule name — a rename silently resets "
        "every affected user's spend-so-far to zero. Needs a human decision "
        "about whether the issuer really does pool these caps.",
    "L3.CARD_ID_UNSAFE_CHARACTER":
        "The only repair is renaming a card id that is already the key of "
        "saved-card records on handsets in the wild. Renaming it orphans them. "
        "Needs a migration, not a data edit.",
    "L9.MERCHANT_STAMP_NOT_MAINTAINED":
        "Nothing in the file says when seed/merchants.json actually last "
        "changed. Copying the manifest's date onto it would manufacture the "
        "very claim the finding says is untrustworthy, and deleting the stamp "
        "throws away a date that may well be correct.",
}

# --------------------------------------------------------------------------- #
# The one value this module writes into `confidence`.
# --------------------------------------------------------------------------- #
# Held in the app's vocabulary (credit_card.dart:280 — 'high' | 'medium' | 'low'),
# in the validator's vocabulary (c8_provenance._CONFIDENCE_VOCAB), and already in
# use on 234 redemption rows. Do NOT change this to a word outside those three
# without changing the Dart, the validator and this comment in the same commit.
SAFE_CONFIDENCE_FOR_UNSOURCED = "low"

# A rule that carries a real link to the issuer's own site may legitimately say
# 'high'. Those are left exactly as they are — this module never raises a
# confidence, only writes down one that was being assumed.
_EARNED = "high"

_ROW_BLOCKS = ("reward_rules", "exclusion_rules", "milestone_rules",
               "fuel_surcharge_rules", "redemption_rules")


# --------------------------------------------------------------------------- #
# small local helpers — reading only
# --------------------------------------------------------------------------- #
def _s(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def _codes(findings) -> set:
    out = set()
    for f in findings or ():
        c = (f or {}).get("code") if isinstance(f, dict) else getattr(f, "code", None)
        if c:
            out.add(c)
    return out


def _cards_for(findings, code) -> set:
    """The card ids the validator actually flagged under this code.

    Per-card stages are gated on this so a fixer can never wander onto a card
    the checker did not name. An aggregate finding (one row covering the whole
    file) carries no card_id and is gated on the code alone.
    """
    out = set()
    for f in findings or ():
        d = f if isinstance(f, dict) else getattr(f, "__dict__", {})
        if d.get("code") == code and d.get("card_id"):
            out.add(d["card_id"])
    return out


def _rule_name(row, j) -> str:
    return _s(row.get("rule_name")) or _s(row.get("milestone_name")) or f"row #{j}"


def _issuer_url_on(row, domains) -> bool:
    """True when this row cites a page on the ISSUER's own website.

    Exactly c8_provenance's definition, via c8's own helpers — an aggregator
    host, a bare word like 'bank', and a co-brand partner's domain all count as
    NOT issuer-sourced, which is the whole point.
    """
    if not domains:
        return False
    for _where, raw in _c8._url_candidates(row):
        host = _c8._host_of(raw)
        if host is None or _c8._aggregator_of(host):
            continue
        if _c8._host_matches(host, domains):
            return True
    return False


# `_sources` is a provenance TAG list, not a URL list. These tokens are the only
# machine-readable marker of the 26 issuer-sourced reward rates in the file, and
# they are exactly the field the non-negotiable rule "a rate correction that is
# not issuer-sourced may only go DOWN" has to be evaluated against. Deleting them
# for not being web addresses cost 8 issuer-sourced rules their only provenance
# marker and made the down-only rule unenforceable on the next run — a fixer must
# never remove the evidence another fixer needs in order to obey a data rule.
PROVENANCE_TOKENS = frozenset({"bank", "issuer", "official", "issuer_site"})


def _placeholder_sources(row) -> list:
    """Values in `_sources` that name no website, no aggregator and no provenance.

    Empty in the current file, and that is the correct answer: every value this
    stage used to delete was the provenance tag 'bank'. Kept as a function
    rather than deleted because the defect it was written for — a genuinely
    meaningless string sitting where a source should be — is still worth
    catching, and now it catches only that.
    """
    raw = row.get("_sources")
    vals = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    out = []
    for v in vals:
        if not isinstance(v, str):
            return []                       # a shape this stage does not own
        if _c8._host_of(v) is not None:     # names a real site — keep it
            return []
        if _c8._aggregator_token(v):        # names an aggregator — a policy
            return []                       # breach L8 reports separately
        if v.strip().lower() in PROVENANCE_TOKENS:
            return []                       # provenance, not a broken URL
        out.append(v)
    return out


# --------------------------------------------------------------------------- #
# S1 — the headline: write the confidence down instead of defaulting it
# --------------------------------------------------------------------------- #
def _s1_implicit_confidence(ctx, findings, out):
    """1,218 rules omit `confidence`; the Dart fills the gap with 'high'."""
    if "L8.CONFIDENCE_DEFAULTS_TO_HIGH" not in _codes(findings):
        return
    for _i, entry, inner, cid in ctx.entries():
        domains = _c8._issuer_domains(_s(inner.get("issuer")))
        rows = entry.get("reward_rules")
        if not isinstance(rows, list):
            continue
        for j, r in enumerate(rows):
            if not isinstance(r, dict) or "confidence" in r:
                continue
            # The brief says leave issuer-sourced rules alone, and they are left
            # alone. Measured on this file: 0 of the 1,218 key-less rules carry
            # an issuer link, so the exception is real but currently empty.
            if _issuer_url_on(r, domains):
                continue
            out.append(Edit(
                card_id=cid, block="reward_rules", index=j, field="confidence",
                old_value=None, new_value=SAFE_CONFIDENCE_FOR_UNSOURCED,
                code="L8.CONFIDENCE_DEFAULTS_TO_HIGH", confidence=CERTAIN,
                reversible=True,
                reason=("This rule carries no link to the issuer's own page, and with "
                        "the confidence field left blank the app treats it as one of "
                        "our most reliable numbers — so we write down that it is one "
                        "of our least reliable instead."),
                evidence=(f"rule '{_rule_name(r, j)}' has no confidence key and no "
                          f"issuer source_url; credit_card.dart:413 substitutes "
                          f"'{_c8._DART_CONFIDENCE_DEFAULT}'"),
                notes={"dart_default": _c8._DART_CONFIDENCE_DEFAULT,
                       "app_vocabulary": list(_c8._CONFIDENCE_VOCAB)},
            ))


# --------------------------------------------------------------------------- #
# S2 — explicit 'high' with nothing behind it
# --------------------------------------------------------------------------- #
def _s2_unearned_high(ctx, findings, out):
    """Someone typed 'high' by hand on a rule with no issuer link."""
    flagged = _cards_for(findings, "L8.CONFIDENCE_HIGH_UNSOURCED")
    if not flagged:
        return
    for _i, entry, inner, cid in ctx.entries():
        if cid not in flagged:
            continue
        domains = _c8._issuer_domains(_s(inner.get("issuer")))
        rows = entry.get("reward_rules")
        if not isinstance(rows, list):
            continue
        for j, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            v = r.get("confidence")
            if not isinstance(v, str) or v.strip().lower() != _EARNED:
                continue
            if _issuer_url_on(r, domains):
                continue                     # earned — leave it
            quoted = _s(r.get("source_quote")) is not None
            # Say what is ACTUALLY in the row. "issuer link: none" was printed on
            # au_small_finance_bank_ixigo_au[2], whose _sources holds
            # https://www.ixigo.com/travel-credit-card — the co-brand partner's
            # own product page for a card named 'ixigo AU'. A reviewer told a
            # link does not exist while it is sitting in the row has no reason to
            # trust the other 1,252 confidence edits either.
            links = sorted({raw for _w, raw in _c8._url_candidates(r)
                            if _c8._host_of(raw)})
            if links:
                seen = ", ".join(_c8._host_of(u) for u in links[:3])
                detail = (f"linked to {seen}, which is not the issuer's own domain"
                          + (f" ({', '.join(domains)})" if domains else ""))
            else:
                detail = "no link of any kind"
            out.append(Edit(
                card_id=cid, block="reward_rules", index=j, field="confidence",
                old_value=v, new_value=SAFE_CONFIDENCE_FOR_UNSOURCED,
                code="L8.CONFIDENCE_HIGH_UNSOURCED", confidence=CERTAIN,
                reversible=True,
                reason=("This rule claims high confidence but carries no link to the "
                        "issuer, so the claim is lowered rather than the link "
                        "invented."),
                evidence=(f"rule '{_rule_name(r, j)}' confidence={v!r}, {detail}"
                          + (" (it does carry a quote, but a quote with no issuer page "
                             "to check it against is not a source)" if quoted else "")),
            ))


# --------------------------------------------------------------------------- #
# S3 — redemption values copied from a card-review site, filed as 'high'
# --------------------------------------------------------------------------- #
def _s3_aggregator_confidence(ctx, findings, out):
    """The AUTO half of L8.AGGREGATOR_SOURCE_IN_DATA.

    Re-sourcing 869 rows from the issuer needs a bank's website and is RESEARCH.
    Demoting the ones that also declare 'high' needs nothing but the row itself:
    the row names an aggregator, KredMe policy is issuer pages only, so 'high'
    is a claim the row cannot support. This clears NO error — the finding is
    about the source token, not the confidence — and it is emitted anyway
    because the number it corrects is the one a reader would trust.
    """
    if "L8.AGGREGATOR_SOURCE_IN_DATA" not in _codes(findings):
        return
    for _i, entry, _inner, cid in ctx.entries():
        rows = entry.get("redemption_rules")
        if not isinstance(rows, list):
            continue
        for j, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            v = r.get("confidence")
            if not isinstance(v, str) or v.strip().lower() != _EARNED:
                continue
            aggs = []
            for _where, raw in _c8._url_candidates(r):
                host = _c8._host_of(raw)
                hit = _c8._aggregator_of(host) if host else _c8._aggregator_token(raw)
                if hit:
                    aggs.append(hit)
            if not aggs:
                continue
            out.append(Edit(
                card_id=cid, block="redemption_rules", index=j, field="confidence",
                old_value=v, new_value=SAFE_CONFIDENCE_FOR_UNSOURCED,
                code="L8.AGGREGATOR_SOURCE_IN_DATA", confidence=CERTAIN,
                reversible=True,
                reason=("What a point is worth here was copied from a card-review "
                        "site rather than the bank, so calling it high confidence "
                        "is a claim we have not earned."),
                evidence=f"redemption row '{_rule_name(r, j)}' cites {aggs[0]!r}, "
                         f"confidence={v!r}",
                notes={"still_unsourced": True,
                       "clears_error": False,
                       "research_remaining": "re-source from the issuer's own page"},
            ))


# --------------------------------------------------------------------------- #
# S4 — a provenance placeholder that is not a web address
# --------------------------------------------------------------------------- #
def _s4_placeholder_source(ctx, findings, out):
    """`_sources: ['bank']` sitting next to the issuer's real URL.

    Deleting the placeholder is only safe where the row already carries a real
    issuer link — otherwise deleting it would throw away the only hint anyone
    ever wrote about where the number came from. That guardrail holds on every
    row in the current file; a row that fails it is left alone.
    """
    flagged = _cards_for(findings, "L8.SOURCE_URL_NOT_A_URL")
    if not flagged:
        return
    for _i, entry, inner, cid in ctx.entries():
        if cid not in flagged:
            continue
        domains = _c8._issuer_domains(_s(inner.get("issuer")))
        for block in _ROW_BLOCKS:
            rows = entry.get(block)
            if not isinstance(rows, list):
                continue
            for j, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                ph = _placeholder_sources(r)
                if not ph:
                    continue
                if not _issuer_url_on(r, domains):
                    continue                 # guardrail: it is the only provenance
                out.append(Edit(
                    card_id=cid, block=block, index=j, field="_sources",
                    old_value=r.get("_sources"), new_value=None,
                    code="L8.SOURCE_URL_NOT_A_URL", confidence=CERTAIN,
                    reversible=True,
                    reason=("The word here is not a web address anyone can open, and "
                            "this row already carries the issuer's real page, so the "
                            "placeholder is deleted rather than replaced."),
                    evidence=(f"{block}[{j}] '{_rule_name(r, j)}' _sources={ph!r}; "
                              f"source_url={trunc(r.get('source_url'), 80)}"),
                ))


# --------------------------------------------------------------------------- #
# S5 — milestone content filed under key names the app never reads
# --------------------------------------------------------------------------- #
def _s5_milestone_aliases(ctx, findings, out):
    """benefit_type -> bonus_type, benefit_value -> bonus_value,
    spend_threshold -> spend_target.

    The alias map is imported from c1_schema so the fixer can never rename to a
    spelling the checker does not recognise. Emitted as two edits per key — set
    the name the app reads, then delete the one it does not — with the value
    baked into the 'set' edit, so the two are order-independent and applying
    only one never destroys the value.
    """
    codes = _codes(findings)
    flagged = (_cards_for(findings, "L1.UNREAD_ALIAS_KEY")
               | _cards_for(findings, "L6.PAYLOAD_UNDER_UNREAD_KEY")
               | _cards_for(findings, "L4.MILESTONE_TARGET_MISSING"))
    if not flagged or not (codes & {"L1.UNREAD_ALIAS_KEY",
                                    "L6.PAYLOAD_UNDER_UNREAD_KEY",
                                    "L4.MILESTONE_TARGET_MISSING"}):
        return
    for block, pairs in _c1.UNREAD_ALIASES.items():
        for _i, entry, _inner, cid in ctx.entries():
            if cid not in flagged:
                continue
            rows = entry.get(block)
            if not isinstance(rows, list):
                continue
            for j, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                for alias, target in sorted(pairs.items()):
                    if alias not in r or target in r:
                        continue             # nothing to move, or already moved
                    val = r[alias]
                    if val is None:
                        continue             # a null carries nothing to rescue
                    name = _rule_name(r, j)
                    clears_target = (target == "spend_target")
                    out.append(Edit(
                        card_id=cid, block=block, index=j, field=target,
                        old_value=None, new_value=val,
                        code="L1.UNREAD_ALIAS_KEY", confidence=CERTAIN,
                        reversible=True,
                        reason=(f"The app only ever looks for '{target}', so the value "
                                f"already written under '{alias}' is moved to the name "
                                f"the app reads — the number itself does not change."
                                + (" This is also what makes the milestone's spending "
                                   "target visible instead of showing as zero."
                                   if clears_target else "")),
                        evidence=f"{block}[{j}] '{name}' {alias}={val!r}, "
                                 f"'{target}' absent",
                        notes={"renamed_from": alias,
                               "also_clears": ("L4.MILESTONE_TARGET_MISSING"
                                               if clears_target else None)},
                    ))
                    out.append(Edit(
                        card_id=cid, block=block, index=j, field=alias,
                        old_value=val, new_value=None,
                        code="L1.UNREAD_ALIAS_KEY", confidence=CERTAIN,
                        reversible=True,
                        reason=(f"'{alias}' is a name nothing in the app reads; now the "
                                f"value lives under '{target}' the duplicate is removed "
                                f"so the row has one answer, not two."),
                        evidence=f"{block}[{j}] '{name}' {alias}={val!r} "
                                 f"(moved to '{target}')",
                        notes={"renamed_to": target},
                    ))


# --------------------------------------------------------------------------- #
# S6 — a rules block that is a sentence instead of a list
# --------------------------------------------------------------------------- #
_DONOR = re.compile(r"identical to\s+([a-z0-9_]+)", re.I)


def _s6_block_not_a_list(ctx, findings, out):
    """yes_bank_uni_rupay's exclusion_rules is a 140-character note naming the
    card whose list it should hold. The donor is in this file, so the value is
    derived from the data and not from anywhere else.

    ADDRESSING, AND WHY IT IS NOT AN ENTRY EDIT
    -------------------------------------------
    The target is one key on the card ENTRY (the object that holds `card` and
    the five row blocks), not a row and not the inner card. base.Edit's declared
    shapes cover a row, a field on a row, and the whole entry — so the obvious
    reading was ENTRY EDIT, replacing the entry with a copy carrying the fixed
    block. I wrote it that way, ran it, and it silently undid another repair:
    the entry snapshot is taken inside plan(), before anything is applied, so
    replacing the entry rolled back the `_sources` deletion this module had
    already proposed for the same card. Measured, not theorised — the
    L8.SOURCE_URL_NOT_A_URL finding for that card survived a run that had
    proposed its fix.

    A whole-object snapshot can never be order-independent with respect to other
    edits on the same object, and that failure is silent and destructive. So
    this is a FIELD edit with block=None, addressing the key on the entry:
    one key changes, nothing else is carried, and any ordering is safe. If a
    runner resolves block=None to the inner card object instead, the fault is
    loud and harmless — the finding simply does not clear and an unknown key
    appears — which is the failure mode to prefer.
    """
    flagged = _cards_for(findings, "L1.BLOCK_NOT_A_LIST")
    if not flagged:
        return
    donors = {}
    for _i, entry, _inner, cid in ctx.entries():
        donors[cid] = entry
    for _i, entry, _inner, cid in ctx.entries():
        if cid not in flagged:
            continue
        for block in _ROW_BLOCKS:
            v = entry.get(block)
            if not isinstance(v, str):
                continue
            m = _DONOR.search(v)
            if not m:
                continue                     # no donor named: nothing to derive
            donor_id = m.group(1)
            donor = donors.get(donor_id)
            if donor is None or donor_id == cid:
                continue
            rows = donor.get(block)
            if not isinstance(rows, list) or not rows:
                continue
            if not all(isinstance(x, dict) for x in rows):
                continue                     # do not copy a donor that is itself broken
            out.append(Edit(
                card_id=cid, block=None, index=None, field=block,
                old_value=v, new_value=copy.deepcopy(rows),
                code="L1.BLOCK_NOT_A_LIST", confidence=LIKELY, reversible=True,
                reason=(f"This card's exclusion list is a note saying it is word for "
                        f"word the same as {donor_id}'s, so that card's {len(rows)} "
                        f"exclusions are copied in exactly as the note instructs — "
                        f"until this is a list, the whole card fails to load and "
                        f"reaches nobody."),
                evidence=f"{block} was the text {trunc(v, 150)!r}; donor "
                         f"{donor_id}.{block} holds {len(rows)} rows",
                notes={"target": "entry",
                       "entry_key": block,
                       "donor_card": donor_id,
                       "rows_copied": len(rows),
                       "replaced_text": v,
                       "why_likely": ("the donor is named in prose and matched with a "
                                      "regular expression; the rows themselves are "
                                      "copied verbatim, nothing is generated")},
            ))


# --------------------------------------------------------------------------- #
# S7 — a fuel window no purchase can fall inside
# --------------------------------------------------------------------------- #
def _s7_fuel_window(ctx, findings, out):
    """min_txn_amount > max_txn_amount. Emitted as a ROW edit so the two numbers
    move together; swapping only one leaves a window of exactly zero width."""
    flagged = _cards_for(findings, "L4.FUEL_MIN_ABOVE_MAX")
    if not flagged:
        return
    for _i, entry, _inner, cid in ctx.entries():
        if cid not in flagged:
            continue
        rows = entry.get("fuel_surcharge_rules")
        if not isinstance(rows, list):
            continue
        for j, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            mn, mx = r.get("min_txn_amount"), r.get("max_txn_amount")
            if not isinstance(mn, (int, float)) or isinstance(mn, bool):
                continue
            if not isinstance(mx, (int, float)) or isinstance(mx, bool):
                continue
            if not mn > mx:
                continue
            fixed = dict(r)
            fixed["min_txn_amount"], fixed["max_txn_amount"] = mx, mn
            out.append(Edit(
                card_id=cid, block="fuel_surcharge_rules", index=j, field=None,
                old_value=r, new_value=fixed,
                code="L4.FUEL_MIN_ABOVE_MAX", confidence=CERTAIN, reversible=True,
                reason=("The smallest and largest qualifying fuel purchase are the "
                        "wrong way round, so as written no purchase can ever qualify; "
                        "the two numbers are swapped back."),
                evidence=f"fuel_surcharge_rules[{j}] min={mn:g} > max={mx:g} "
                         f"-> min={mx:g}, max={mn:g}",
                notes={"swapped": ["min_txn_amount", "max_txn_amount"]},
            ))


# --------------------------------------------------------------------------- #
# S8 — every news story stamped with the moment the feed was generated
# --------------------------------------------------------------------------- #
def _s8_news_dates(ctx, findings, out):
    """The story id already carries the day the issuer announced the change.

    31 of 32 stories share one timestamp to the second, and 29 of them name a
    date in their own id up to 924 days earlier. The repaired value is read out
    of the row's own id — nothing is fetched, nothing is guessed. c9's regular
    expression is imported so the fixer reads the id exactly the way the checker
    does.
    """
    codes = _codes(findings)
    if not (codes & {"L9.NEWS_DATE_CONTRADICTS_ITS_OWN_ID",
                     "L9.NEWS_ONE_TIMESTAMP_FOR_MANY_STORIES"}):
        return
    items = _c9._news_items(ctx.news)
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        sid = _s(it.get("id"))
        m = _c9.NEWS_ID_DATE.search(sid or "")
        if not m:
            continue                         # 'undated' in the id: nothing to derive
        try:
            event = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        cur = _c9._as_moment(it.get("published_at"))
        if cur is not None and cur.date() == event:
            continue                         # already right
        new = event.isoformat()
        if _s(it.get("effective_date")) == new:
            continue                         # already recorded
        gap = (cur.date() - event).days if cur is not None else None
        out.append(Edit(
            card_id=None, block="news", index=i, field="effective_date",
            old_value=it.get("effective_date"), new_value=new,
            code="L9.NEWS_DATE_CONTRADICTS_ITS_OWN_ID", confidence=LIKELY,
            reversible=True,
            reason=("This story's own id says the issuer's change took effect on "
                    f"{event.isoformat()}, which is a different fact from when we "
                    "published the story, so the date is recorded alongside rather "
                    "than written over the one the app sorts and badges on."),
            evidence=f"news item [{i}] id={sid!r} encodes {event.isoformat()}; "
                     f"published_at={it.get('published_at')!r}"
                     + (f" ({gap} days later)" if gap is not None else ""),
            notes={"target_file": "news/feed.json", "container": "items",
                   "not_a_card": True,
                   "why_not_published_at": (
                       "news_feed_service.dart:150 sorts the feed on publishedAt "
                       "and :176 computes the unread badge as "
                       "publishedAt.isAfter(_lastSeen). Back-dating 29 of 32 items "
                       "— one to 2024-02-05 — reorders the whole feed and zeroes "
                       "the badge for every existing user, on a bell that only just "
                       "shipped and is still at 0% reach."),
                   "app_change_needed": (
                       "The app has no field to fall back on, so until it reads "
                       "effective_date this key is recorded and unused.")},
        ))


# --------------------------------------------------------------------------- #
# S9 — the manifest and the feed disagree about which version the feed is
# --------------------------------------------------------------------------- #
def _s9_news_version(ctx, findings, out):
    """The app reads the FEED's own version, so the feed is the fact and the
    manifest is the stale index. The direction is forced: lowering the feed to
    match the manifest would be a version regression, and every handset would
    then decide the feed it already has is newer and stop updating."""
    if "L9.NEWS_VERSION_DISAGREES_WITH_MANIFEST" not in _codes(findings):
        return
    man = ctx.manifest if isinstance(ctx.manifest, dict) else {}
    feed = ctx.news if isinstance(ctx.news, dict) else {}
    man_v, feed_v = _s(man.get("news_version")), _s(feed.get("version"))
    if not man_v or not feed_v or man_v == feed_v:
        return
    out.append(Edit(
        card_id=None, block="manifest", index=None, field="news_version",
        old_value=man_v, new_value=feed_v,
        code="L9.NEWS_VERSION_DISAGREES_WITH_MANIFEST", confidence=CERTAIN,
        reversible=True,
        reason=(f"The index says the news feed is version {man_v} while the feed "
                f"itself says {feed_v}; the app goes by the feed, so the index is "
                f"corrected to match it rather than the other way round."),
        evidence=f"seed/manifest.json news_version={man_v!r}, "
                 f"news/feed.json version={feed_v!r}",
        notes={"target_file": "seed/manifest.json", "not_a_card": True},
    ))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
_STAGES = (
    _s1_implicit_confidence,
    _s2_unearned_high,
    _s3_aggregator_confidence,
    _s4_placeholder_source,
    _s5_milestone_aliases,
    _s6_block_not_a_list,
    _s7_fuel_window,
    _s8_news_dates,
    _s9_news_version,
)


def plan(ctx, findings) -> list[Edit]:
    """Propose every repair this family can derive from the file itself.

    PURE. Reads ctx and findings, returns Edits, writes nothing and mutates
    nothing — the two entry/row edits that carry a whole object build a deep
    copy first, so the caller's data is never touched.
    """
    out: list[Edit] = []
    for stage in _STAGES:
        try:
            stage(ctx, findings, out)
        except Exception as exc:                       # noqa: BLE001
            # A stage that trips must not take the other eight with it, and must
            # never be mistaken for a stage that found nothing to fix.
            out.append(_stage_failed(stage, exc))
    out = _dedupe(out)
    # This module never emits a whole-object edit. An Edit whose new_value is a
    # snapshot of a card taken inside plan() rolls back every other edit on that
    # card, whichever module proposed them, and does it silently. That bug was
    # real here once; this is the tripwire that stops it coming back.
    bad = [e for e in out if e.shape == "entry"]
    if bad:
        raise AssertionError(
            "f4_integrity emitted %d whole-entry edit(s) (%s). An entry snapshot "
            "clobbers sibling edits on the same card — address the single key "
            "instead." % (len(bad), ", ".join(e.anchor() for e in bad[:3])))
    return out


def _stage_failed(stage, exc) -> Edit:
    """A no-op Edit that says a stage crashed.

    old_value == new_value, so a runner that applies it changes nothing; a
    runner that prints it tells a human that this family reported less than it
    should have. Silence would read exactly like a clean pass.
    """
    return Edit(
        card_id=None, block=None, index=None, field="_fixer_stage_error",
        old_value="", new_value="",
        code="F4.STAGE_FAILED", confidence=LIKELY, reversible=True,
        reason=(f"The {stage.__name__} repair stage stopped with an error, so any "
                f"defect it would have fixed is still in the file and was not "
                f"counted."),
        evidence=f"{stage.__name__}: {type(exc).__name__}: {exc}",
        notes={"no_op": True, "stage": stage.__name__},
    )


def _dedupe(edits) -> list[Edit]:
    """One edit per address. Two stages proposing the same field is a bug, and a
    runner applying both would make the second overwrite the first silently."""
    seen = set()
    out = []
    for e in edits:
        key = (e.card_id, e.block, e.index, e.field)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out
