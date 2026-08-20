"""f3_reach — the reachability family.

WHAT THIS FAMILY IS FOR
-----------------------
A rule in this file can be perfectly accurate and still reach nobody. The app
drops it at start-up, or files it under a channel it never tests, or the card
carrying it is one we withdrew. This module's whole job is: make a dead row
live where the file itself already says how, and otherwise leave it dead and
say so out loud. It never guesses a rate, a category or a card.

Codes handled — and, honestly, what each one gets:

    L6.CATEGORY_BONUS_DROPPED      edits (narrow: only where a channel is stated)
    L6.INACTIVE_CARD_STILL_RANKS   edits, 'likely' only — a product decision
    L6.CHANNEL_NEVER_MATCHES       NO EDITS — see §4
    L2.CHANNEL_NOT_IN_VOCAB        NO EDITS — same rows as above
    L2.CHANNEL_WRONG_LANE          NO EDITS — same rows as above
    L6.RULE_EXCLUDED_BY_OWN_CARD   NO EDITS — needs the issuer, not arithmetic
    L6.UPI_RULE_SWALLOWS_BASE_RATE NO EDITS — an engine bug, not a data defect

A code appearing in HANDLES with no edits behind it is not an oversight. It is
this module saying "I looked at all 75 of these and the right answer is to leave
them alone", and `census()` prints the count and the reason so that nobody has
to re-derive it later.

L6.EXCLUSION_TYPE_INERT USED TO BE HERE AND IS NOT ANY MORE. See §1.


§1  THE EXCLUSION REMAP MOVED OUT OF THIS MODULE
------------------------------------------------
This module used to sweep L6.EXCLUSION_TYPE_INERT with a substring phrase table
of its own, while f5_exclusions carried a reviewed whole-string table for the
same class. Both ran, this one ran first, and it recognised a strict superset
of everything the other did — so the reviewed table decided nothing at all, and
every mapping that reached seed/cards.json came from the looser matcher with no
trace of that in the diff.

Two tables for one decision guarantee drift, so there is now one. The wording
table, the family walk and the definition of what a card earns all live in
fixers/exclusion_vocab.py; every edit to an exclusion_rules row is made by
f5_exclusions; and this module does not touch one. Nothing was lost in the move
— GATE 1 POISON, GATE 2 BRAND and GATE 3 MCC went into
exclusion_vocab.map_exclusion_value ahead of the table, and the per-row
guardrail went in beside them, widened with a fifth witness (a co-brand named in
the card's own name) that neither module had.

What is left here is the rest of the reachability family: a category bonus the
app drops for want of a channel, and the withdrawn cards that still rank.

§2  WHY NOTHING IS WRITTEN TO rule_name, EVER
---------------------------------------------
Not in this module and not in any other. The name is the only independent
witness in the file — it is the issuer's own sentence, and it is what makes a
rate auditable a year from now. It is also the key the app buckets a user's
saved cap progress under, so changing the string wipes their progress. This
module reads names constantly and writes one never.


§3  THE APP'S CATEGORY LIST IS AN AUTHORITY, NOT A GUESS
--------------------------------------------------------
Every category target is checked against ctx.app_category_names(), which comes
from the app checkout when there is one and from tools/app_mirror/ in CI. If
neither is available the vocabulary is unknown, and this module emits ZERO
category remaps rather than falling back to its own list. A fixer that invents a
category when it cannot see the app is the same defect as a check that invents
an error when it cannot see the app, and that one cost 309 fabricated findings.


§4  WHY CHANNEL_NEVER_MATCHES GETS NO EDIT
------------------------------------------
75 findings, and the obvious fix is the wrong one. The validator's own advice
offers "delete the channel and let forex_markup_pct do the work". Do that to an
'international' rule and it starts firing on DOMESTIC spend: au_small_finance_
bank_xcite_ultra would pay "12 Reward Points on every international transaction
of Rs 100" at a Mumbai grocery. That is a rate going UP, on our own arithmetic,
on every user — the exact shape of mistake the down-only rule exists to stop.

The 'offline' sub-class has a tempting alternative — re-file as category_bonus,
where 'offline' IS honoured — and it is also wrong: not one of those 19 rules
carries a category_id, so taking it would mean inventing the category. Their
prose says "offline spends", "in-store card swipes", "POS transactions". That is
a channel, not a category.

And the cost of leaving them is bounded, which is why this is defensible rather
than lazy: effectiveRateForCategory (credit_card.dart:641-653) only admits
category_bonus, merchant_specific and base_rate rules, so a
channel_specific/international rule is not being printed as a live rate either.
These rows are dead, not wrong. 75 errors of wasted curation. It is app work.


§5  THE 13 WITHDRAWN CARDS ARE A FOUNDER'S DECISION, NOT A FIX
--------------------------------------------------------------
These are emitted, and they are emitted at 'likely' with reversible=False, so a
runner gated to 'certain' never touches them. Both halves of that are on
purpose. The data plainly says we withdrew the card and the app plainly has no
code that reads the flag, so the only lever the file has is deletion — but
deletion orphans the saved-card record of anyone already holding it, which is
the same harm that makes renaming a card_id forbidden. Measured, it also
orphans two live HDFC news alerts that name four of the thirteen; each affected
edit carries the item ids in its notes.

And the error count that comes off the total is mostly theatre: taking these
cards out drops the total by 32, but only 13 of that is this defect. Another 21
is zero base rates, empty rule lists and inert exclusions leaving with the
cards that carried them, and 2 goes the other way as the orphaned news alerts.
A fix stage that quotes the 32 is quoting a number it did not earn.
"""
from __future__ import annotations

import re

from fixers.base import CERTAIN, LIKELY, Edit, trunc

FAMILY = "reachability"

HANDLES = [
    "L6.CHANNEL_NEVER_MATCHES",
    "L2.CHANNEL_NOT_IN_VOCAB",
    "L2.CHANNEL_WRONG_LANE",
    "L6.INACTIVE_CARD_STILL_RANKS",
    "L6.CATEGORY_BONUS_DROPPED",
    "L6.RULE_EXCLUDED_BY_OWN_CARD",
    "L6.UPI_RULE_SWALLOWS_BASE_RATE",
]

# Codes this module owns but deliberately never edits, with the one-line reason
# census() reports. Kept as data so the "why" cannot drift from the behaviour.
NO_EDIT_BY_DESIGN = {
    "L6.CHANNEL_NEVER_MATCHES": (
        "Dropping the channel would make an international or offline rule fire on "
        "ordinary domestic spend, which raises a rate on our own arithmetic. The "
        "app has no international channel and no offline base lane; this is app "
        "work, and until it lands the rows are dead but not wrong."),
    "L2.CHANNEL_NOT_IN_VOCAB": (
        "The same rows as L6.CHANNEL_NEVER_MATCHES, reported from the vocabulary "
        "side. Nothing to fix in the data."),
    "L2.CHANNEL_WRONG_LANE": (
        "The same rows again: 'offline' on a base-lane rule. Re-filing them as "
        "category bonuses would mean inventing a category none of them has."),
    "L6.RULE_EXCLUDED_BY_OWN_CARD": (
        "The card both pays and excludes the same category. Deleting either row "
        "is a claim about what the issuer does, and the file holds no third "
        "witness to settle it. Needs the issuer's page."),
    "L6.UPI_RULE_SWALLOWS_BASE_RATE": (
        "The engine's UPI phase tests whether the CARD is RuPay, not whether the "
        "payment was UPI, so a UPI rule beats the base rate on every purchase. "
        "That is an engine bug; no edit to this file fixes it."),
}

# --------------------------------------------------------------------------- #
# phrase -> app category slug. This is NOT an exclusion table any more — the one
# that decides an exclusion lives in fixers/exclusion_vocab.py. It survives here
# for one much narrower job: _plan_dropped_bonus asks whether a rule's own prose
# names ANY spending category at all, to tell a category bonus apart from a
# channel rule. It is a detector, never an authority.
# --------------------------------------------------------------------------- #
CATEGORY_PHRASES = [
    ("rent", r"\brent(?:s|al|als|ing)?\b|property manage|real ?estate|"
             r"rental commission"),
    ("wallet_load", r"\bwallet|e-?wallet|prepaid card|prepaid instrument"),
    ("government", r"governmen?t|\bgovt\b|\btax(?:es)?\b"),
    ("education", r"educat|\bschool|\bcollege|universit"),
    ("jewellery", r"jewel|\bgold\b|\bsilver\b|precious metal|antique"),
    ("railways", r"railway|\birctc\b"),
    ("telecom", r"telecom|telecommunicat"),
    ("insurance", r"insurance"),
    ("utilities", r"utilit"),
    ("fuel", r"\bfuel\b|petrol|\bdiesel\b|\blpg\b"),
    ("grocery", r"supermarket|grocer"),
]

# The plausibility ceiling any derived rendered rate must stay under. Above this
# the number stops being a reward and starts being a typo somebody has to read.
MAX_DERIVED_PCT = 15.0
# Point value must be the card's OWN, and in a band a real issuer uses. Outside
# it the arithmetic would be resting on the app's invented 0.25 default.
RP_VALUE_BAND = (0.05, 2.0)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _s(v):
    return v.strip() if isinstance(v, str) else None


def _low(v):
    s = _s(v)
    return s.lower() if s else ""


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _get(f, key):
    """findings arrive as dicts from the JSON report or as Finding objects."""
    if isinstance(f, dict):
        return f.get(key)
    return getattr(f, key, None)


def _cards_with(findings, code) -> set:
    return {_get(f, "card_id") for f in findings
            if _get(f, "code") == code and _get(f, "card_id")}


def _rows(entry, block):
    v = entry.get(block)
    return v if isinstance(v, list) else []


def _hits(text, table):
    """Every (label) in `table` whose pattern occurs in text."""
    return sorted({label for label, pat in table if re.search(pat, text, re.I)})


# B. L6.INACTIVE_CARD_STILL_RANKS
# --------------------------------------------------------------------------- #
def _news_references(ctx) -> dict:
    """card_id -> the news items that name it.

    Measured, not assumed: taking the 13 inactive cards out of cards.json
    orphans two live HDFC news alerts that name four of them, which turns one
    error class into another. A reviewer should see that on the same line as
    the removal, not discover it after the merge.
    """
    n = ctx.news
    items = n.get("items") if isinstance(n, dict) else n
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        ident = _s(it.get("id")) or "(unnamed news item)"
        for key in ("affected_cards", "cards", "card_ids"):
            refs = it.get(key)
            if isinstance(refs, list):
                for c in refs:
                    if isinstance(c, str):
                        out.setdefault(c, set()).add(ident)
    return out


def _plan_inactive(ctx, findings, tally):
    """A card we withdrew, that the app still loads, ranks and recommends.

    The app has no code that reads is_active — there is no flag to set, no
    date to fill in, no rate to correct. The file's only lever is whether the
    card is in it. So the edit is the removal of the entry, and it is emitted
    at 'likely' with reversible=False, because two things are true at once:

        the data plainly says we withdrew this card, AND
        removing it orphans the saved-card record of anyone already holding it,
        which is the same harm that makes renaming a card_id forbidden.

    That is a founder's call, not a validator's. Emitting it means it appears on
    a reviewable diff with the rule count attached; the runner's confidence gate
    is what stops it applying on its own.
    """
    wanted = _cards_with(findings, "L6.INACTIVE_CARD_STILL_RANKS")
    news_refs = _news_references(ctx)
    edits = []
    for i, entry, inner, cid in ctx.entries():
        if cid not in wanted:
            continue
        if inner.get("is_active") not in (0, False, "0"):
            continue                                   # already resolved
        n_rules = len([r for r in _rows(entry, "reward_rules") if isinstance(r, dict)])
        name = _s(inner.get("card_name")) or cid
        cited_by = sorted(news_refs.get(cid, ()))
        if cited_by:
            tally["inactive.cited_by_news"] += 1
        edits.append(Edit(
            card_id=cid, block=None, index=i, field=None,
            old_value=entry, new_value=None,
            code="L6.INACTIVE_CARD_STILL_RANKS",
            reason=(f"Our own data marks {name} as withdrawn, but the app has no code "
                    f"that reads that flag, so it keeps ranking and recommending the "
                    f"card — taking it out of the file is the only way to stop that."),
            evidence=f"card.is_active = {inner.get('is_active')!r}; "
                     f"{n_rules} reward rule(s) currently live",
            confidence=LIKELY, reversible=False, family=FAMILY,
            # Anchored on the ONE field the decision rests on, not on the whole
            # entry. With the whole-entry anchor, any other edit landing on this
            # card in the same run moved it: 10 of these 13 were refused on pass
            # one and applied on pass two, so an IRREVERSIBLE deletion had a
            # different outcome depending on how many times you ran the tool, and
            # a workflow_dispatch at --confidence likely opened a PR deleting 3
            # cards or 13 depending on nothing a reviewer could see.
            anchor_fields={
                ("card.is_active" if isinstance(entry.get("card"), dict) else "is_active"):
                    inner.get("is_active")},
            notes={"reward_rules": n_rules,
                   "cited_by_news_items": cited_by,
                   "warning": ("Removing a card orphans the saved-card record of any "
                               "user already holding it, and any news alert that "
                               "names it. Needs a human yes.")},
        ))
        tally["inactive.card_removed"] += 1
        tally["inactive.rules_removed"] += n_rules
    return edits


# --------------------------------------------------------------------------- #
# C. L6.CATEGORY_BONUS_DROPPED
# --------------------------------------------------------------------------- #
def _synth_gate(rule_name):
    """Mirror of credit_card.dart:390-431 via checks/c6_reachability.py — a
    condition the app conjures from the rule's NAME. A rule with one of these is
    not dropped at start-up, so it is none of this section's business."""
    n = (rule_name or "").lower()
    if not n:
        return None
    negated = any(w in n for w in
                  ("non-prime", "non prime", "without prime", "not a prime"))
    if not negated and any(w in n for w in ("prime member", "for prime", "(prime)")):
        return "user.is_prime_member"
    if "swiggy one" in n or "with swiggy" in n:
        return "user.has_swiggy_one"
    if "amazon pay balance" in n or "amazon pay wallet" in n:
        return "user.has_amazon_pay_balance"
    return None


def _rendered_pct(row, inner):
    """What the app's rateForRule would put on the screen, or None if it cannot
    be worked out from THIS card's own numbers."""
    rtype = _low(row.get("reward_type"))
    rate = _num(row.get("reward_rate"))
    if rate is None:
        return None
    if rtype in ("cashback_pct", "cashback"):
        return rate * 100.0
    unit = _num(row.get("reward_unit_spend"))
    pv = _num(row.get("point_value"))
    if pv is None:
        pv = _num(inner.get("rp_value_standard"))
    if pv is None or not (RP_VALUE_BAND[0] <= pv <= RP_VALUE_BAND[1]):
        return None                      # would rest on the app's invented 0.25
    if not unit or unit <= 0:
        return None
    return rate * pv / unit * 100.0


def _plan_dropped_bonus(ctx, findings, tally):
    """A 'category bonus' with no category and no condition is thrown away the
    moment the app starts. The check's advice is to set a category_id — and
    where the prose names one, that is right and a human should do it, because
    reading a category out of prose is a judgement call.

    This section only takes the case where the prose names no category at all
    and the rule states a CHANNEL. Then the rule is not a category bonus at
    all; it is channel_specific, which is a type the app indexes. Two hard
    limits keep it honest:

      - the channel must be one the base lane can actually match ('online', or
        'upi' on a RuPay-UPI card). Retyping an 'offline' rule would move it
        from one graveyard to another and let this module claim a fix it did
        not make.
      - a channel of null is refused. Without a channel the rule would become
        an unconditional base rule paying its bonus rate on EVERYTHING.

    This is the one place in the family where a rate goes up — from 0.00%, the
    "Rate not published" the user sees today, to the number written in the
    issuer's own sentence. So it also carries the two guardrails from the rate
    work: the card must have its own rp_value_standard in band, and the
    resulting rate must land under the plausibility ceiling.
    """
    wanted = _cards_with(findings, "L6.CATEGORY_BONUS_DROPPED")
    edits = []
    if not ctx.have_categories():
        # Without the vocabulary this run cannot tell a dropped bonus from a
        # perfectly good one — that is exactly the cascade the checks now skip.
        if wanted:
            tally["dropped_bonus.skipped_no_vocabulary"] += 1
        return edits
    app_cats = ctx.app_category_names()
    for _i, entry, inner, cid in ctx.entries():
        if cid not in wanted:
            continue
        has_upi = inner.get("has_rupay_upi") in (1, True, "1")
        for j, row in enumerate(_rows(entry, "reward_rules")):
            if not isinstance(row, dict):
                continue
            if _low(row.get("rule_type")) != "category_bonus":
                continue                                   # idempotence
            cat = row.get("category_id")
            if isinstance(cat, int) and not isinstance(cat, bool):
                continue
            if isinstance(cat, str) and cat.strip():
                continue                                   # it has a category
            if isinstance(row.get("conditions_json"), dict) or \
                    _synth_gate(_s(row.get("rule_name"))):
                continue                                   # not dropped
            prose = " ".join(x for x in (_low(row.get("category_ref")),
                                         _low(row.get("rule_name"))) if x)
            if _hits(prose, CATEGORY_PHRASES):
                tally["dropped_bonus.left_prose_names_a_category"] += 1
                continue        # a human should set category_id; not a guess
            chan = _low(row.get("channel"))
            if chan not in ("online",) and not (chan == "upi" and has_upi):
                tally["dropped_bonus.left_channel_" + (chan or "none")] += 1
                continue
            pct = _rendered_pct(row, inner)
            if pct is None:
                tally["dropped_bonus.left_rate_not_derivable"] += 1
                continue
            if pct <= 0 or pct > MAX_DERIVED_PCT:
                tally["dropped_bonus.left_rate_implausible"] += 1
                continue
            edits.append(Edit(
                card_id=cid, block="reward_rules", index=j, field="rule_type",
                old_value=row.get("rule_type"), new_value="channel_specific",
                code="L6.CATEGORY_BONUS_DROPPED",
                reason=(f"This rule is not about a spending category at all — it is "
                        f"about {chan} payments — so filing it as a channel rule is "
                        f"what stops the app throwing it away at start-up and lets "
                        f"the {pct:.2f}% it actually pays reach the user."),
                evidence=f"rule_name = {trunc(_s(row.get('rule_name')) or '(unnamed)')}"
                         f" | channel = {chan} | category_id is empty",
                confidence=LIKELY, reversible=True, family=FAMILY,
                notes={"rendered_pct": round(pct, 4), "channel": chan,
                       "app_categories_known": len(app_cats)},
            ))
            tally["dropped_bonus.retyped"] += 1
    return edits


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
def plan(ctx, findings) -> list:
    """PURE. Reads ctx and findings, returns proposed Edits, mutates nothing.

    Every row this module hands back is a copy; the entries inside ctx are never
    touched. Run it twice on the same data and the second run returns the same
    list; run it on data the first run's edits were applied to and it returns
    nothing, because every gate is written against the state that makes the
    defect true.
    """
    findings = [f for f in (findings or []) if _get(f, "code") in HANDLES]
    tally = _Tally()
    edits = []
    edits += _plan_dropped_bonus(ctx, findings, tally)
    edits += _plan_inactive(ctx, findings, tally)
    # L6.CHANNEL_NEVER_MATCHES, L2.CHANNEL_NOT_IN_VOCAB, L2.CHANNEL_WRONG_LANE,
    # L6.RULE_EXCLUDED_BY_OWN_CARD and L6.UPI_RULE_SWALLOWS_BASE_RATE produce no
    # edits by design. See NO_EDIT_BY_DESIGN and §4 of the module docstring.
    return edits


class _Tally(dict):
    """A Counter without importing one, so census() and plan() count identically.
    Reading a key that was never touched gives 0 and does not create it, so the
    census only ever lists dispositions that actually happened."""
    def __missing__(self, k):
        return 0


def census(ctx, findings) -> dict:
    """PURE, and optional. What plan() did, and — the part that matters — what
    it refused to do and why.

    The refusals are the deliverable here as much as the edits are. 80% of the
    exclusion class cannot be closed from this file at all: the rows name EMI
    conversions, cash withdrawals, card fees and gift-card purchases, and the
    app's merchant model has no field that carries any of them. That is an app
    feature request, not a data defect, and it needs a number attached or it
    will be re-derived from scratch every quarter.
    """
    findings = [f for f in (findings or []) if _get(f, "code") in HANDLES]
    tally = _Tally()
    edits = []
    edits += _plan_dropped_bonus(ctx, findings, tally)
    edits += _plan_inactive(ctx, findings, tally)

    counts = {}
    for f in findings:
        code = _get(f, "code")
        counts[code] = counts.get(code, 0) + 1

    missing = {k.split(".", 1)[1]: v for k, v in tally.items()
               if k.startswith("missing_concept.")}
    return {
        "family": FAMILY,
        "findings_in": counts,
        "edits_out": len(edits),
        "edits_by_code": _by(edits, lambda e: e.code),
        "edits_by_confidence": _by(edits, lambda e: e.confidence),
        "categories_source": ctx.categories_source(),
        "app_categories_known": len(ctx.app_category_names()),
        "tally": dict(sorted(tally.items())),
        "app_cannot_express": dict(sorted(missing.items(),
                                          key=lambda kv: -kv[1])),
        "no_edit_by_design": {c: NO_EDIT_BY_DESIGN[c] for c in NO_EDIT_BY_DESIGN
                              if c in counts},
    }


def _by(edits, key):
    out = {}
    for e in edits:
        k = key(e)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
