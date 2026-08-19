"""f3_reach — the reachability family.

WHAT THIS FAMILY IS FOR
-----------------------
A rule in this file can be perfectly accurate and still reach nobody. The app
drops it at start-up, or files it under a channel it never tests, or the card
carrying it is one we withdrew. This module's whole job is: make a dead row
live where the file itself already says how, and otherwise leave it dead and
say so out loud. It never guesses a rate, a category or a card.

Codes handled — and, honestly, what each one gets:

    L6.EXCLUSION_TYPE_INERT        edits (the big one; most rows stay inert)
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


§1  THE EXCLUSION REMAP — why it is guarded four deep
-----------------------------------------------------
The app's engine (recommendation_engine.dart:486-497) has exactly two cases,
'mcc' and 'category', and no default. 983 of 1,488 exclusion rows are typed
'other' or 'txn_type' and therefore do nothing at all. Re-expressing them looks
like a search-and-replace and is not, for one reason:

    An exclusion is checked BEFORE any reward rule. Phase 0. A wrong exclusion
    does not show the user a wrong number — it silently removes the card from
    the pick screen entirely.

So the direction of error matters more than the count. Under-excluding leaves
things exactly as they are today. Over-excluding takes money off a real user.
Every gate below is built to fail towards "leave it alone".

    GATE 1  POISON      the value is conditional, scoped or narrative
                        ("excluded from the base rate", "reduced to 1%",
                        "non-BPCL fuel stations"). These are not flat
                        exclusions at all and re-typing one inverts its meaning.
    GATE 2  BRAND       the value names a specific merchant. Excluding the whole
                        category would take out every other merchant in it.
    GATE 3  MCC         "mcc 6513" — the value already IS the answer.
    GATE 4  CATEGORY    an ordered phrase map, and then three exactness tests:
                          - exactly ONE target matched (never two)
                          - no competing concept the app cannot express
                            ("wallet cash withdrawals" is about cash)
                          - the target is a name the APP actually ships, read
                            from ctx.app_category_names()
    GATE 5  GUARDRAIL   per row, never a regex over the file: does THIS card pay
                        anything on the category we are about to switch off?
    GATE 6  FAMILY      the same question asked of the whole category FAMILY,
                        because the app's categories are a tree. 'railways' is a
                        child of 'travel', so a card whose only earn is `travel`
                        must not have 'railways' switched on. GATE 5 compares
                        names and let exactly that through onto two live cards
                        before this gate existed; see _family_closed().

GATE 5 is the BPCL Octane rule, and it is written per-row because the near-miss
that created it was a sweep that looked at the file and not at the card. Octane
is protected twice over: its fuel row says "non-BPCL fuel stations", which GATE 1
kills, and the card ships fuel_surcharge_rules, which GATE 5 kills. Its other
row, "mobile wallet uploads", remaps cleanly — which is the point. The guardrail
is meant to be survivable, not to block everything.

Three mappings the taxonomy proposed are NOT taken here, because they fail the
"exactly and unambiguously" test once you look at what the app puts IN those
categories:

    tolls / transportation -> travel   the app's 'travel' is MakeMyTrip,
                                       Cleartrip, redbus. A card excluding toll
                                       plazas would stop earning on flights.
    hospitals -> pharmacy              'pharmacy' also holds Apollo Pharmacy,
                                       1mg, Netmeds and the diagnostic labs.
    movies -> entertainment            'entertainment' also holds Netflix,
                                       Spotify and ChatGPT subscriptions.

Same reasoning drops "wholesale clubs" from grocery, "bail and bond payments"
and "political donations" from government, and a bare "recharges" from telecom.
Measured: 23 rows are left inert by those six decisions. Leaving a defect
standing is the cheaper mistake, every time.

MEASURED OUTCOME (worktree at 43d54e4, app checkout visible, 2026-08-19)
    426 exclusion rows retyped, of 983 inert   -> L6.EXCLUSION_TYPE_INERT
                                                  220 -> 187 errors, 33 cards
                                                  clear, 187 do not
    557 rows left alone, and the census names every reason
     12 rows blocked by the per-row guardrail   -> manual review, never auto
        (was 428 retyped / 10 blocked before GATE 6. The two rows that moved are
        the railways-under-travel pair named there; they were applied to
        seed/cards.json by the run at f9081f6 and f5_exclusions puts them back.)
      1 dropped category bonus retyped
     13 inactive cards proposed for removal     -> 'likely', gated, see §5
    712 -> 679 errors from the data edits alone; 679 -> 647 if the 13 card
    removals are also approved, though 32 of that 32 is defects leaving with
    the cards rather than being fixed, and it adds 2 new errors: two live HDFC
    news alerts name four of the removed cards.


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
    "L6.EXCLUSION_TYPE_INERT",
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

LIVE_EXCLUSION_TYPES = ("mcc", "category")

# --------------------------------------------------------------------------- #
# GATE 1 — POISON. A value that is conditional, scoped, or narrative is not a
# flat exclusion, and re-typing it inverts its meaning. "Fuel excluded from the
# base rate" means fuel still earns the accelerated rate; switch it to a flat
# category exclusion and the card stops earning on fuel entirely.
# --------------------------------------------------------------------------- #
POISON = re.compile(
    r"reduced rate|reduced reward|reduced to|earns? (?:only|reduced|at reduced)|"
    r"capped at|\bexcept\b|other than|excluded from|not eligible for|"
    r"\bwhen\b|\bunless\b|\bif\b|\bonly\b|"
    r"below (?:rs\.?|₹|inr)?\s*\d|less than|contradicted|not listed|"
    r"specific categories|\(for |beyond|non-jio|non-bpcl|not made through|"
    r"do not earn (?:regular|accelerated|the)|up ?to \d|post facto|"
    r"from october|as per t&c|subject to",
    re.I)

# --------------------------------------------------------------------------- #
# GATE 2 — BRAND. The value names one merchant. "swiggy money wallet" is a
# wallet, but excluding the app's whole wallet_load category on the strength of
# it would switch off Paytm, PhonePe, Amazon Pay, MobiKwik and Ola Money too.
# An illustrative brand ("wallet loads (e.g. paytm)") is not a restriction, so
# an example marker cancels this gate.
# --------------------------------------------------------------------------- #
BRAND = re.compile(
    r"swiggy|freecharge|paytm|phonepe|amazon pay|smartbuy|bills2pay|\bjio\b|"
    r"bpcl|\bcred\b|myntra|flipkart|cleartrip|tata neu|indianoil|hpcl",
    re.I)
EXAMPLE_MARKER = re.compile(r"e\.g\.|such as|including|\blike\b", re.I)

# GATE 3 — the value already carries the answer.
MCC_LITERAL = re.compile(r"^mcc[ _]?(\d{4})$", re.I)

# --------------------------------------------------------------------------- #
# GATE 4 — phrase -> app category slug. Ordered; every target is checked against
# the app's real vocabulary before it is used, so this list can only ever be a
# candidate generator, never an authority.
#
# Deliberately absent, and why, because a later reader will want to add them
# back: travel (the app's travel is OTA bookings, not toll plazas), pharmacy
# (also holds the diagnostic labs), entertainment (also holds streaming),
# wholesale clubs (not in grocery's MCC set), bail-and-bond and political
# donations (not in government's), and a bare "recharges" (reads as either a
# telecom top-up or a wallet load).
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

# Concepts the app's merchant model has no field for. A value that touches one
# of these is not cleanly a category exclusion even when it also names a
# category — "wallet cash withdrawals" is about cash, not about wallets.
NO_APP_CONCEPT = [
    ("EMI / instalment conversion",
     r"\bemis?\b|easy ?emi|smart ?emi|dial an? emi|flexipay|instal?lment|"
     r"smartpay|splitn-?pay"),
    ("Cash / quasi-cash",
     r"cash.?withdraw|cash.?advance|\bcash\b|\batm\b|quasi.?cash|encash|"
     r"money transfer"),
    ("Card fees / balance transfer / loans",
     r"balance.?transfer|outstanding (?:balance|amount)|\bloan|credit card bill|"
     r"card fee|annual fee|joining fee|late payment|interest charge|"
     r"financial charge|finance charge|\bgst\b|\bdraft\b|\bplcc\b"),
    ("International / forex",
     r"international|foreign currency|forex|\bcrypto\b"),
    ("Gambling / gaming",
     r"gambl|gaming|casino|lotter"),
    ("Gift cards / vouchers",
     r"gift ?card|gift ?voucher|voucher purchase"),
]

# GATE 5 — the guardrail's vocabulary. Wider than CATEGORY_PHRASES on purpose:
# this side is looking for any hint that the card PAYS on the target, and a
# false positive here costs one skipped fix while a false negative costs a user
# their rewards.
PAYS_HINTS = {
    "rent": r"\brent|property manage|real ?estate",
    "wallet_load": r"wallet|prepaid",
    "government": r"governmen?t|\bgovt\b|\btax\b",
    "education": r"educat|school|college|universit",
    "jewellery": r"jewel|\bgold\b|\bsilver\b|precious metal",
    "railways": r"railway|irctc",
    "telecom": r"telecom|recharge|mobile bill|postpaid|prepaid",
    "insurance": r"insurance|premium",
    "utilities": r"utilit|electricity|\bgas\b|water bill|broadband",
    "fuel": r"\bfuel\b|petrol|diesel|\blpg\b|filling station|petrol pump",
    "grocery": r"grocer|supermarket|kirana|departmental",
}

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


# --------------------------------------------------------------------------- #
# the app's vocabulary, as authority
# --------------------------------------------------------------------------- #
def _mcc_owner(ctx) -> dict:
    """'6513' -> 'rent'. Built from the app's own categories.json mcc_ranges, so
    an MCC exclusion can be guarded against the same card paying on whatever
    category that MCC belongs to."""
    out = {}
    for c in (ctx.app_categories or []):
        if not isinstance(c, dict):
            continue
        name = c.get("category_name")
        if not name:
            continue
        for r in (c.get("mcc_ranges") or []):
            if not isinstance(r, dict):
                continue
            ex = r.get("exact")
            if isinstance(ex, (str, int)):
                out.setdefault(str(ex), name)
            lo, hi = r.get("from"), r.get("to")
            try:
                if lo is not None and hi is not None:
                    for n in range(int(lo), int(hi) + 1):
                        out.setdefault("%04d" % n, name)
            except (TypeError, ValueError):
                continue
    return out


def _merchant_index(ctx) -> dict:
    """merchant slug -> {'category': slug, 'mccs': {...}}"""
    m = ctx.merchants
    rows = m.get("merchants") if isinstance(m, dict) else m
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        mccs = {str(x) for x in (r.get("mcc_codes") or []) if x is not None}
        if r.get("mcc_primary") is not None:
            mccs.add(str(r["mcc_primary"]))
        rec = {"category": _low(r.get("category_id")), "mccs": mccs}
        for k in ("merchant_name", "slug", "merchant_slug", "merchant_ref"):
            key = _low(r.get(k))
            if key:
                out[key] = rec
    return out


# --------------------------------------------------------------------------- #
# GATE 5 — what does THIS card already pay on?
# --------------------------------------------------------------------------- #
def _card_pays_on(entry, merchants, mcc_owner) -> set:
    """Every app category this card looks like it rewards.

    Read wide and fail safe. A rule counts as paying unless its rate is
    explicitly zero or negative — an absent rate is unknown, and unknown is not
    the same as nothing. Four independent witnesses are consulted, because the
    Octane near-miss got past a check that only read one of them:

        category_id      the structured field
        category_ref     the prose that was never structured
        rule_name        the issuer's own sentence
        merchant_ref     resolved through merchants.json to its category and MCC

    Plus the blunt one: any card shipping fuel_surcharge_rules is treated as
    paying on fuel. 359 of 383 cards ship that block, so this single line sends
    every fuel remap to a human. That is deliberate. Fuel is the category the
    near-miss was in.
    """
    pays = set()
    for row in _rows(entry, "reward_rules"):
        if not isinstance(row, dict):
            continue
        rate = _num(row.get("reward_rate"))
        if rate is not None and rate <= 0:
            continue                     # this rule pays nothing; it guards nothing
        cat = _low(row.get("category_id"))
        if cat:
            pays.add(cat)
        prose = " ".join(x for x in (_low(row.get("category_ref")),
                                     _low(row.get("rule_name"))) if x)
        if prose:
            for target, pat in PAYS_HINTS.items():
                if re.search(pat, prose, re.I):
                    pays.add(target)
        ref = _low(row.get("merchant_ref"))
        if ref and ref in merchants:
            rec = merchants[ref]
            if rec["category"]:
                pays.add(rec["category"])
            for code in rec["mccs"]:
                owner = mcc_owner.get(code)
                if owner:
                    pays.add(owner)
    if _rows(entry, "fuel_surcharge_rules"):
        pays.add("fuel")
    return pays


# --------------------------------------------------------------------------- #
# GATE 6 — the FAMILY walk, and why GATE 5 alone was not enough
# --------------------------------------------------------------------------- #
def _family_closed(pays: set, ctx) -> set:
    """`pays`, widened to every category in the same family as anything the card
    earns on — its ancestors and its descendants in the app's own tree.

    GATE 5 compares category NAMES. The app's categories are a TREE: 'railways'
    is a child of 'travel', 'food_delivery' is a child of 'dining'. A card whose
    only reward rule is `category_id: travel` and whose exclusion list says
    'railways' passed GATE 5 because the two strings differ — and the engine
    then removes that card at every railway merchant, taking the travel rule
    with it, because _isExcluded runs at STEP 1 before any rule is matched.

    That is not hypothetical. Before this gate existed the sweep wrote exactly
    that row onto two live cards, kotak_mahindra_bank_royale_signature and
    rbl_bank_world_safari, and the validator could not see it either:
    L6.RULE_EXCLUDED_BY_OWN_CARD compares names for equality and never walks the
    tree.

    The walk itself lives in f5_exclusions.family_index, so there is ONE
    definition of "same family" and the two modules cannot drift apart. If f5 is
    unavailable this returns `pays` unchanged: a missing module must cost the
    extra protection, never the whole run.
    """
    try:
        from fixers.f5_exclusions import family_index
    except Exception:                                   # noqa: BLE001
        return pays
    fams = family_index(getattr(ctx, "app_categories", None))
    out = set(pays)
    for p in pays:
        out |= fams.get(p, set())
    return out


# --------------------------------------------------------------------------- #
# GATE 1-4 — classify one exclusion value
# --------------------------------------------------------------------------- #
def classify_exclusion(value: str, app_categories: set):
    """(verdict, payload, detail) for one exclusion_value.

    verdict is one of:
        'mcc'          payload = the 4-digit code
        'category'     payload = an app category_name
        'poison'       conditional/scoped text — not a flat exclusion
        'brand'        names one merchant
        'ambiguous'    two or more categories in one row
        'mixed'        a category plus a concept the app cannot express
        'no_concept'   payload = None, detail = the concept the app is missing
        'not_in_app'   the phrase maps somewhere the app does not ship
        'no_vocabulary' this run cannot see the app's category list, so it
                       refuses to decide — NOT the same answer as 'not_in_app'
        'unmapped'     nothing recognised it

    app_categories is the authority. An EMPTY set means "unknown on this run",
    never "the app has none", and the two must not collapse into one verdict:
    reporting a blind run's rows as "the app does not have this category" would
    be this module inventing a fact about the app, which is the exact defect
    that put 309 phantom errors in the validator.
    """
    v = (value or "").strip().lower()
    if not v:
        return "unmapped", None, "empty value"

    if POISON.search(v):
        return "poison", None, "conditional or scoped wording"

    m = MCC_LITERAL.match(v)
    if m:
        return "mcc", m.group(1), "the value is already an MCC"

    if BRAND.search(v) and not EXAMPLE_MARKER.search(v):
        return "brand", None, "names one merchant, not a category"

    targets = _hits(v, CATEGORY_PHRASES)
    missing = _hits(v, NO_APP_CONCEPT)

    if len(targets) > 1:
        return "ambiguous", None, " + ".join(targets)
    if targets and missing:
        return "mixed", None, f"{targets[0]} mixed with {missing[0]}"
    if targets:
        t = targets[0]
        if not app_categories:
            return "no_vocabulary", None, t
        if t not in app_categories:
            return "not_in_app", None, t
        return "category", t, f"'{v}' reads as {t}"
    if missing:
        return "no_concept", None, missing[0]
    return "unmapped", None, "no app category matches this wording"


# --------------------------------------------------------------------------- #
# A. L6.EXCLUSION_TYPE_INERT
# --------------------------------------------------------------------------- #
def _plan_exclusions(ctx, findings, tally):
    app_cats = ctx.app_category_names()
    have_cats = ctx.have_categories()
    mcc_owner = _mcc_owner(ctx) if have_cats else {}
    merchants = _merchant_index(ctx)
    wanted = _cards_with(findings, "L6.EXCLUSION_TYPE_INERT")
    edits = []

    if not have_cats:
        # No app vocabulary on this run. The MCC gate still stands on its own —
        # "mcc 6513" is the answer regardless of what the app ships — but a
        # category remap would be this module inventing a slug it cannot check.
        tally["exclusion.skipped_no_vocabulary"] += 1

    for _i, entry, inner, cid in ctx.entries():
        if cid not in wanted:
            continue
        # GATE 5 (what this card pays on) widened by GATE 6 (the family walk).
        pays = _family_closed(_card_pays_on(entry, merchants, mcc_owner), ctx)
        # Two different issuer sentences on one card can mean the same category
        # ("government transactions" and "tax payments"). Both are retyped and
        # both are kept: the engine stops at the first match either way, and each
        # row's _retyped_from preserves a different piece of the issuer's
        # wording. Merging them would throw evidence away to tidy a file. This
        # only counts them so a reviewer is not surprised by the diff.
        live_pairs = {(_low(r.get("exclusion_type")), _low(r.get("exclusion_value")))
                      for r in _rows(entry, "exclusion_rules")
                      if isinstance(r, dict)
                      and _low(r.get("exclusion_type")) in LIVE_EXCLUSION_TYPES}
        for j, row in enumerate(_rows(entry, "exclusion_rules")):
            if not isinstance(row, dict):
                tally["exclusion.row_not_an_object"] += 1
                continue
            etype = _low(row.get("exclusion_type"))
            if etype in LIVE_EXCLUSION_TYPES:
                continue                              # already live — idempotence
            raw = row.get("exclusion_value")
            value = _s(raw) or ""
            verdict, payload, detail = classify_exclusion(value, app_cats)

            def _note_duplicate(kind):
                pair = (kind, payload.lower())
                if pair in live_pairs:
                    tally["exclusion.duplicate_of_existing"] += 1
                live_pairs.add(pair)

            if verdict == "mcc":
                target = mcc_owner.get(payload)
                if target and target in pays:
                    tally["exclusion.guardrail_blocked"] += 1
                    tally["exclusion.guardrail_blocked." + target] += 1
                    continue
                if any(payload in rec["mccs"] for rec in merchants.values()
                       if rec["category"] and rec["category"] in pays):
                    tally["exclusion.guardrail_blocked"] += 1
                    continue
                _note_duplicate("mcc")
                new_row = dict(row)
                new_row["exclusion_type"] = "mcc"
                new_row["exclusion_value"] = payload
                new_row["_retyped_from"] = f"{etype or '(none)'}:{value}"
                edits.append(Edit(
                    card_id=cid, block="exclusion_rules", index=j, field=None,
                    old_value=dict(row), new_value=new_row,
                    code="L6.EXCLUSION_TYPE_INERT",
                    reason=("This exclusion already gives a merchant code, so it is "
                            "written as a merchant-code exclusion, which is one of "
                            "the only two kinds the app can actually act on."),
                    evidence=f"exclusion_value = {trunc(value)}",
                    confidence=CERTAIN, reversible=True, family=FAMILY,
                    notes={"gate": "mcc-literal", "mcc": payload,
                           "mcc_category": target},
                ))
                tally["exclusion.remapped_mcc"] += 1
                continue

            if verdict == "category":
                if payload in pays:
                    tally["exclusion.guardrail_blocked"] += 1
                    tally["exclusion.guardrail_blocked." + payload] += 1
                    continue
                _note_duplicate("category")
                new_row = dict(row)
                new_row["exclusion_type"] = "category"
                new_row["exclusion_value"] = payload
                new_row["_retyped_from"] = f"{etype or '(none)'}:{value}"
                edits.append(Edit(
                    card_id=cid, block="exclusion_rules", index=j, field=None,
                    old_value=dict(row), new_value=new_row,
                    code="L6.EXCLUSION_TYPE_INERT",
                    reason=(f"The issuer's wording here means {payload.replace('_', ' ')}, "
                            f"which is a spending category the app can switch off — "
                            f"today this row is written in a way the app ignores "
                            f"completely, and we tell people they earn rewards the "
                            f"issuer says they do not."),
                    evidence=f"exclusion_value = {trunc(value)}",
                    confidence=LIKELY, reversible=True, family=FAMILY,
                    notes={"gate": "phrase-map", "target": payload,
                           "card_pays_on": sorted(pays)},
                ))
                tally["exclusion.remapped_category"] += 1
                tally["exclusion.remapped." + payload] += 1
                continue

            tally["exclusion.left_" + verdict] += 1
            if verdict == "no_concept":
                tally["missing_concept." + detail] += 1
            elif verdict == "not_in_app":
                tally["not_in_app." + detail] += 1
    return edits


# --------------------------------------------------------------------------- #
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
    edits += _plan_exclusions(ctx, findings, tally)
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
    edits += _plan_exclusions(ctx, findings, tally)
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
