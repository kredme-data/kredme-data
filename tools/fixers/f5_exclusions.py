"""f5_exclusions — make an inert exclusion row real, or prove it cannot be.

WHY THIS MODULE EXISTS SEPARATELY FROM f3_reach
-----------------------------------------------
The Flutter engine's exclusion switch reads exactly two values and has no
default (recommendation_engine.dart:486-497):

    switch (e.exclusionType) { case 'mcc': ... case 'category': ... }

Anything typed 'other' or 'txn_type' is INERT: the card claims an exclusion it
never enforces, so the app pays rewards the bank does not. f3_reach already
sweeps that class and, on this branch, has already applied 428 retypes. This
module is NOT a second sweep of the same rows. It exists for the one thing f3
cannot do, and for the damage that gap has already caused.

    f3's guardrail asks "does this card pay on a category with THIS NAME?"
    The right question is "does this card pay anywhere in this category's
    FAMILY?" — its ancestors and its descendants in the app's own tree.

'railways' is a child of 'travel'. A card whose only reward rule is
`category_id: travel` and whose exclusion list says 'railways' passes f3's
name-exact guardrail, because the strings differ. It should not. The engine
runs _isExcluded at STEP 1 (recommendation_engine.dart:308-309), BEFORE any
rule matching, so activating that exclusion does not lower the card's railway
rate — it removes the card from the pick screen at a railway merchant
entirely, including the 5-points-per-100 travel rule that is the whole reason
somebody carries it. That is the BPCL Octane failure mode with a different
noun.

MEASURED, on this worktree, 2026-08-19: f3's sweep has ALREADY written two
such rows into seed/cards.json —

    kotak_mahindra_bank_royale_signature   excl 'railway transactions' -> railways
    rbl_bank_world_safari                  excl 'railways'             -> railways

both on cards whose only earning category is 'travel', the parent. Neither is
visible to the validator: L6.RULE_EXCLUDED_BY_OWN_CARD compares category names
for equality and never walks the tree, so it reports 2 findings and neither of
them is one of these. Section §4 covers what this module does about that.


§1  THE TABLE IS EXPLICIT, WHOLE-STRING, AND WRITTEN OUT IN FULL
----------------------------------------------------------------
Every pattern in SYNONYMS is matched with re.fullmatch against the normalised
value. No substring search, no edit distance, no fuzzy anything. The reason is
not tidiness. A substring matcher reading "wallet cash withdrawals" sees
'wallet' and maps the row to wallet_load, and the row was about cash. A
substring matcher reading "fuel purchases at non-BPCL fuel stations" sees
'fuel' and switches off a fuel card at every pump. Both of those are one
regex away from a user losing rewards they earned, and neither shows up as a
wrong number anywhere — the card simply stops appearing.

Every entry carries a comment naming the REAL phrasings it covers, counted off
seed/cards.json. Nothing in the table was invented; if a phrasing is not in the
file it is not in the table.

Two deliberate narrowings, both measured, both worth stating because a later
reader will want to widen them:

    wallet_load is a category about LOADING a wallet. It is named
    'wallet_load', not 'wallet'. So 'wallet reloads', 'e-wallet loading' and
    'wallet uploads' map; a bare 'wallet', 'wallet transactions' or 'wallet
    spends' does NOT, because spending FROM a wallet is a different act from
    topping one up and the file gives no way to tell which the issuer meant.
    That is 36 rows left inert on purpose.

    rent is the app's category for house rent. 'rent payments', 'rental
    payments' and 'rent transactions' map; a bare 'rental', 'rentals', 'real
    estate' or 'property rental' does NOT — 'rental' with no noun is as easily
    a car or an equipment rental, and 'real estate' is a purchase, not a rent.
    21 rows left inert on purpose.

Concepts the app has no field for are never forced into a near-miss category.
CONCEPTS below names them, counts them and reports them, so the gap can be
raised as an app feature request instead of being re-derived every quarter:
EMI conversion, cash and quasi-cash, card fees and balance transfers, gambling,
forex, tolls, gift cards, B2B, financial institutions, charity, and the
catch-all 'miscellaneous'. 'gift cards' is NOT 'wallet_load' — a gift card is a
purchase of a stored-value instrument, a wallet load is a transfer into one,
and the issuer excludes them in separate sentences on the same page.


§2  THE GUARDRAIL, AND WHY IT IS FAMILY-AWARE
----------------------------------------------
Before emitting any edit, compute the set of categories the card actually EARNS
in, from two witnesses and only two, both structured:

    every reward rule's category_id
    every reward rule's merchant_ref, resolved through seed/merchants.json,
    whose row key is 'merchant_name' (there is no 'slug' field) and whose
    'category_id' IS the app category name string

Then expand the EXCLUSION's target into its family — itself, its ancestors, and
its descendants — using the app's own categories.json, where 'id' is an int and
'parent_id' points at another int id. If the family intersects what the card
earns, emit nothing and record the reason.

Rules that pay a rate of zero or less are not witnesses: a rule that pays
nothing cannot lose anything. Rules with no rate at all ARE witnesses, because
absent is not the same as zero and unknown must fail towards leaving the card
alone.

Deliberately NOT consulted here, though f3 consults them: rule_name prose and
the presence of fuel_surcharge_rules. Both are wider nets and both are wrong in
the same direction. A fuel-surcharge WAIVER is a fee waiver, not a reward — 359
of 383 cards ship that block, so treating it as "this card earns on fuel"
blocks every fuel exclusion in the file including the honest ones. The
structured fields say what the card pays on. They are enough, and they are
checkable.


§3  NOTHING HERE CAN DOUBLE-EDIT A ROW f3 ALREADY OWNS
-------------------------------------------------------
f3_reach also handles L6.EXCLUSION_TYPE_INERT. Two fixers writing the same row
in one run is not a merge conflict — the runner resolves collisions by position
and the second write silently wins, so the reviewed diff and the applied diff
would differ. This module therefore does not merely avoid f3's rows by
coincidence of having a stricter table. It ASKS f3, per row, and stands down:

    f3_reach.classify_exclusion(value, app_categories) returning 'mcc' or
    'category' means f3 will emit an edit for this row, and f5 emits none.

Counted as `forward.ceded_to_f3` in census(), so the number is visible rather
than implied. On the pre-sweep data f3 owns essentially the whole mappable set
and this module's forward half correctly emits nothing at all. That is the
contract working, not the module failing.

If f3_reach is absent or unimportable the cession is skipped and this module
maps on its own — the safety property is "never both", not "never f5".


§4  THE REPAIR HALF, AND THE FACT THAT IT MAKES THE COUNT WORSE
----------------------------------------------------------------
A row that is live 'category' TODAY, carries `_retyped_from` (so a previous
sweep wrote it), and fails the family guardrail is put back exactly as it was.
The old value is not guessed: `_retyped_from` records it verbatim, which is why
these edits are 'certain' and reversible.

Be honest about the arithmetic. Putting those 2 rows back adds 2
L6.EXCLUSION_TYPE_INERT errors to the validator's total. The error count goes
UP and the data gets BETTER, because an inert exclusion costs a user nothing
and a wrongly-active one removes their travel card at the station. A fix stage
that optimised the number instead of the user would leave them where they are.

Rows without `_retyped_from` are never reverted — there would be nothing to
restore them to, and inventing an old value is the same defect as inventing a
new one.


§5  rule_name IS NEVER WRITTEN
-------------------------------
Not read out of, not copied into, not touched. The app keys every user's saved
cap progress on that string, so changing it wipes their progress. Every edit
this module emits is a whole-row replacement of an exclusion_rules row, and
exclusion rows do not carry a rule_name at all. The runner's own forbidden()
guard checks this independently; the tests assert it a third time.
"""
from __future__ import annotations

import re

from fixers.base import CERTAIN, LIKELY, Edit, trunc

FAMILY = "exclusions"

HANDLES = [
    # The inert class itself — the forward half of this module.
    "L6.EXCLUSION_TYPE_INERT",
    # The repair half. The validator's own check for this code compares
    # category NAMES for equality, so the family-level case (excluding a child
    # while earning the parent) produces no finding today. The edits ride under
    # this code because it is the defect they describe; the missing check is
    # noted in census() as a validator gap.
    "L6.RULE_EXCLUDED_BY_OWN_CARD",
]

# The only two exclusion_type values the engine's switch can act on.
LIVE_EXCLUSION_TYPES = ("mcc", "category")


# --------------------------------------------------------------------------- #
# 1. NORMALISATION
# --------------------------------------------------------------------------- #
def normalise(value) -> str:
    """Lowercase, strip, collapse whitespace, drop trailing punctuation.

    Curly apostrophes are folded to straight ones because the file contains
    both. Nothing else is rewritten: a normaliser that also stripped brackets
    would quietly turn "utility bill payments (reduced rate)" — a rate change,
    not an exclusion — into a flat exclusion of every utility bill.
    """
    if not isinstance(value, str):
        return ""
    s = value.replace("’", "'").replace("‘", "'").lower()
    s = " ".join(s.split())
    return s.strip(" \t.;:,")


# --------------------------------------------------------------------------- #
# 2. THE SYNONYM TABLE — explicit, whole-string, every pattern commented with
#    the real phrasings it covers and how many rows carry each one.
#    Counts are off seed/cards.json before f3's sweep (983 inert rows), which is
#    the state where every phrasing is still visible.
# --------------------------------------------------------------------------- #
# (app_category, confidence, pattern, the real strings this pattern covers)
SYNONYMS = [
    # ---------------- government -------------------------------------------
    # "government transactions" 27 · "government services" 23 · "government
    # payments" 9 · "government institutions" 9 · "government-related
    # transactions" 8 · "government spends" 6 · "government related
    # transactions" 4 · "government spending" 3 · "government-related payments"
    # 2 · "governmental spends" 2 · "government charges" 1 · "government
    # institution spends" 1 · "government related" 1
    ("government", CERTAIN,
     r"government(?:al)?(?:[ -]related)?"
     r"(?: (?:transactions?|payments?|services?|spends?|spending|"
     r"institutions?|institution spends?|charges))?"),
    # "govt" 1 · "govt. institutions" 1 — the abbreviation, same word.
    ("government", CERTAIN, r"govt\.?(?: institutions?)?"),
    # "payment made to government services" 1 — one issuer's long form.
    ("government", CERTAIN, r"payments? made to government services?"),
    # "tax payments" 5 · "tax" 1 · "taxes" 1 · "tax-related payments" 1.
    # Tax is the app's government category: advance tax, GST challans and
    # municipal dues all sit on the government MCCs. Nothing else in the app's
    # vocabulary could hold them.
    ("government", LIKELY, r"tax(?:es)?(?:[ -]related)?(?: payments?)?"),

    # ---------------- wallet_load ------------------------------------------
    # The category is 'wallet_load' — the act of LOADING. Only phrasings that
    # say so map. See §1 for the 36 rows deliberately left inert.
    # "wallet reloads" 26 · "wallet load" 7 · "wallet loading" 7 · "e-wallet
    # loading" 6 · "wallet reload" 3 · "e-wallet loading transactions" 3 ·
    # "e-wallet reloading" 2 · "e-wallet uploads" 2 · "wallet reloading" 2 ·
    # "e-wallet reloads" 1 · "wallet loading transactions" 1 · "wallet upload"
    # 1 · "wallet uploads" 1
    ("wallet_load", CERTAIN,
     r"(?:e[- ]?|mobile |digital )?wallets?"
     r"[ /]?(?:re)?(?:load|loading|loads|reload|reloads|reloading|"
     r"upload|uploads|uploading|top[- ]?up|top[- ]?ups)"
     r"(?: transactions?)?"),
    # "loading of e-wallets" 1 · "wallet load/reloading" 1 — same act, inverted
    # or doubled wording.
    ("wallet_load", CERTAIN,
     r"(?:re)?loading of (?:e[- ]?)?wallets?|"
     r"wallet load/reloading"),
    # "mobile wallet uploads" 1 · "digital wallet loading or top-up
    # transactions" 1 — the qualified forms one issuer each uses.
    ("wallet_load", CERTAIN,
     r"digital wallet loading or top[- ]?up transactions?"),

    # ---------------- rent --------------------------------------------------
    # "rent payments" 27 · "rental payments" 22 · "rent transactions" 4 ·
    # "rental transactions" 2. In Indian card T&Cs "rental payment" with no
    # other noun is house rent — it is the sentence that names CRED, NoBroker
    # and RedGirraffe. A bare "rental"/"rentals"/"real estate" is NOT taken;
    # see §1.
    ("rent", CERTAIN, r"rent(?:al)? (?:payments?|transactions?|spends?)"),
    # "payments towards rent/property management" 2 · "rent/property
    # management" 1 · "rental commissions" 1 · "payment of property management
    # fees, rental commissions, and rental payments" 1. Property management and
    # rental commission are the letting-agent side of the same transaction and
    # sit on the same MCC 6513 the app files under rent.
    ("rent", LIKELY,
     r"(?:payments? towards )?rent/property management|"
     r"rental commissions?|"
     r"payment of property management fees, rental commissions, and "
     r"rental payments"),

    # ---------------- education ---------------------------------------------
    # "education payments" 9 · "educational institutions" 6 · "education fees"
    # 3 · "education spends" 2 · "education transactions" 2 · "education-related
    # payments" 2 · "education institution fees" 1 · "education services" 1 ·
    # "education-related transactions" 1 · "educational" 1 · "educational
    # expenses" 1
    # The app's education category is MCC 8211/8220/8241/8244/8249/8299 —
    # schools, colleges and universities — so "educational institutions" is the
    # category by its own definition, not a near-miss.
    ("education", CERTAIN,
     r"education(?:al)?(?:[ -]related)?"
     r"(?: (?:payments?|fees?|spends?|transactions?|services?|expenses|"
     r"institutions?|institution fees?))?"),
    # "school and education" 2 · "school and educational services" 1 ·
    # "schools & education" 1 — schools named alongside the word education.
    ("education", CERTAIN,
     r"schools? (?:and|&) education(?:al)?(?: services?)?"),

    # ---------------- railways ----------------------------------------------
    # "railways" 14 · "railway transactions" 3 · "railway bookings" 2. Every one
    # of these is checked against the travel family before it is written; see
    # §2 and the two cards §4 names.
    ("railways", CERTAIN,
     r"railways?(?: (?:transactions?|bookings?|payments?|spends?))?"),
    # "irctc" — the railway booking portal, which is the category by any name.
    ("railways", CERTAIN, r"irctc(?: (?:transactions?|bookings?))?"),

    # ---------------- utilities ---------------------------------------------
    # "utility payments" 7 · "utility bills" 4 · "utility transactions" 1 ·
    # "utility services" 1 · "utility spends" 1 · "utilities bill payment" 1
    ("utilities", CERTAIN,
     r"utilit(?:y|ies)"
     r"(?: (?:payments?|bills?|bill payments?|transactions?|services?|spends?))?"),

    # ---------------- insurance ---------------------------------------------
    # "insurance services" 4 · "insurance transactions" 3 · "insurance spends" 2
    ("insurance", CERTAIN,
     r"insurance"
     r"(?: (?:payments?|premiums?|premium payments?|transactions?|"
     r"services?|spends?))?"),

    # ---------------- jewellery ---------------------------------------------
    # "jewelry" 7 · "jewellery purchases" 4 · "jewelry purchases" 4 ·
    # "jewellery items" 1 · "purchase of jewelry items" 1. Both spellings ship.
    ("jewellery", CERTAIN,
     r"jewell?ery|jewell?ry|"
     r"jewell?e?ry (?:purchases?|items?|spends?|transactions?)|"
     r"purchases? of jewell?e?ry items?"),
    # "gold" 2 · "gold purchases" 2 · "gold spends" 1 · "purchase of gold items"
    # 2 · "silver purchases" 1 · "gold/jewellery" 3 · "gold and jewellery" 1.
    # Derived, not assumed: the app files jewellery under MCC 5944 AND 5094,
    # and 5094 is literally "precious stones and metals, watches and jewelry".
    # Bullion is inside this category by the app's own definition. Kept at
    # 'likely' anyway, because "gold" alone could name a card tier, so a default
    # --confidence certain run leaves these 12 rows alone.
    ("jewellery", LIKELY,
     r"gold|silver|"
     r"(?:gold|silver)(?: (?:purchases?|spends?|items?|transactions?))|"
     r"purchases? of (?:gold|silver) items?|"
     r"gold ?[/&] ?jewell?e?ry|gold and jewell?e?ry"),

    # ---------------- fuel --------------------------------------------------
    # "fuel purchases" 1 · "petrol" 1 · "fuel transactions (no reward points on
    # fuel spends)" — that last one is scoped wording and is NOT matched by a
    # whole-string pattern, which is the point of matching whole strings.
    # Every fuel row is checked against what the card earns first; the card the
    # near-miss was about, and every card carrying a real fuel reward rule, is
    # blocked by §2 rather than by a special case here.
    ("fuel", CERTAIN,
     r"fuel(?: (?:purchases?|transactions?|spends?|payments?))?|"
     r"petrol|diesel|petrol/diesel"),

    # ---------------- telecom -----------------------------------------------
    # "telecom" / "telecom payments" — the app ships a 'telecom' category and
    # the word is unarguable. A bare "recharges" is NOT here: a recharge is as
    # easily a wallet top-up as a mobile top-up, and the file cannot say which.
    ("telecom", CERTAIN,
     r"telecom(?:munications?)?"
     r"(?: (?:payments?|bills?|transactions?|services?|spends?))?"),
]

# Compiled once, in table order. Order does not decide anything — a value that
# matches two different targets is refused as ambiguous rather than resolved by
# position — but a fixed order keeps two runs byte-identical.
_SYNONYMS = [(cat, conf, re.compile(pat)) for cat, conf, pat in SYNONYMS]


# --------------------------------------------------------------------------- #
#    CONCEPTS the app's model has no field for. These are never mapped to a
#    near-miss category; they are counted and reported so the gap becomes an app
#    feature request with a number on it instead of a hunch.
#    Substring patterns are correct HERE and only here: this table's job is to
#    NAME what was refused, not to decide anything. A false positive costs a row
#    a better label in a report; it can never cost a user a reward.
# --------------------------------------------------------------------------- #
CONCEPTS = [
    ("EMI / instalment conversion",
     r"\bemis?\b|easy ?emi|smart ?emi|dial an? emi|flexipay|instal?lment|"
     r"smartpay|split[ -]?pay"),
    ("Cash & quasi-cash",
     r"cash.?withdraw|cash.?advance|\bcash\b|\batm\b|quasi.?cash|encash|"
     r"money transfer|\bdraft\b"),
    ("Card fees / interest / balance transfer",
     r"balance.?transfer|outstanding (?:balance|amount)|\bloan|"
     r"credit card bill|card fee|annual fee|joining fee|late payment|"
     r"interest charge|financial charge|finance charge|\bgst\b|\bplcc\b|"
     r"^fees|^charges|fees and other charges|fees or charges|fees/charges"),
    ("Gambling & skill gaming",
     r"gambl|gaming|casino|lotter|betting"),
    ("Forex / international",
     r"international|foreign currency|forex|\bcrypto\b"),
    ("Tolls & road transport",
     r"\btolls?\b|bridge fee|transportation"),
    ("Gift cards & vouchers",
     r"gift ?card|gift ?voucher|voucher purchase|novelty"),
    ("B2B / commercial",
     r"\bb2b\b|business[- ]to[- ]business|commercial purposes|"
     r"business services|contracted services|contractor services"),
    ("Financial institutions & brokers",
     r"financial institution|security broker|collection agenc|investments?"),
    ("Charity & donations",
     r"charit|donation"),
    ("Refunds, disputes & reversals",
     r"refunded|reversed|cancel|disputed|cashback transactions"),
    ("Unclassified 'miscellaneous'",
     r"miscellaneous"),
]
_CONCEPTS = [(label, re.compile(pat, re.I)) for label, pat in CONCEPTS]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _s(v):
    return v.strip() if isinstance(v, str) else None


def _low(v):
    s = _s(v)
    return s.lower() if s else ""


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _get(f, key):
    """Findings arrive as dicts from the JSON report or as Finding objects."""
    if isinstance(f, dict):
        return f.get(key)
    return getattr(f, key, None)


def _rows(entry, block):
    v = entry.get(block)
    return v if isinstance(v, list) else []


def _cards_with(findings, code) -> set:
    return {_get(f, "card_id") for f in findings
            if _get(f, "code") == code and _get(f, "card_id")}


# --------------------------------------------------------------------------- #
# THE CATEGORY FAMILY — the whole point of this module
# --------------------------------------------------------------------------- #
def family_index(app_categories) -> dict:
    """category_name -> {every name in its family}: itself, its ancestors and
    its descendants, from the app's own categories.json.

    'id' is an INT and 'parent_id' points at another int id, so the tree is
    walked over ids and only rendered back into names at the end. Siblings are
    deliberately NOT family: 'airlines' and 'railways' share a parent, and a
    card that pays on flights has no claim on train tickets.

    A cycle in the file cannot hang this — both walks carry a seen-set.
    """
    rows = [c for c in (app_categories or []) if isinstance(c, dict)
            and c.get("category_name")]
    name = {c.get("id"): c["category_name"] for c in rows if c.get("id") is not None}
    parent = {c.get("id"): c.get("parent_id") for c in rows if c.get("id") is not None}
    kids = {}
    for cid, pid in parent.items():
        if pid is not None:
            kids.setdefault(pid, []).append(cid)

    out = {}
    for cid, nm in name.items():
        ids, seen = set(), set()
        cur = cid
        while cur is not None and cur not in seen:      # ancestors
            seen.add(cur)
            ids.add(cur)
            cur = parent.get(cur)
        stack, seen2 = [cid], set()                     # descendants
        while stack:
            k = stack.pop()
            if k in seen2:
                continue
            seen2.add(k)
            ids.add(k)
            stack.extend(kids.get(k, []))
        out.setdefault(nm, set()).update(name[i] for i in ids if i in name)
    return out


def merchant_categories(ctx) -> dict:
    """merchant_name -> app category name string.

    seed/merchants.json rows key on 'merchant_name'; there is no 'slug' field,
    and 'category_id' on a merchant row IS the app category NAME, not an id —
    {"merchant_name": "swiggy", "category_id": "food_delivery"}. The other key
    spellings are accepted defensively in case a future row carries one, but
    'merchant_name' is the one that exists today.
    """
    m = getattr(ctx, "merchants", None)
    rows = m.get("merchants") if isinstance(m, dict) else m
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        cat = _low(r.get("category_id"))
        if not cat:
            continue
        for k in ("merchant_name", "slug", "merchant_slug", "merchant_ref"):
            key = _low(r.get(k))
            if key:
                out.setdefault(key, cat)
    return out


def card_earns_in(entry, merch_cats: dict) -> set:
    """Every app category this card actually pays on, from structured fields.

    Two witnesses, both structured, per the guardrail spec:
        reward_rules[].category_id
        reward_rules[].merchant_ref, resolved through merchants.json

    A rule whose reward_rate is <= 0 pays nothing and therefore guards nothing.
    A rule with NO rate is still a witness: absent is not zero, and an unknown
    rate has to fail towards leaving the card alone.
    """
    earns = set()
    for row in _rows(entry, "reward_rules"):
        if not isinstance(row, dict):
            continue
        rate = _num(row.get("reward_rate"))
        if rate is not None and rate <= 0:
            continue
        cat = _low(row.get("category_id"))
        if cat:
            earns.add(cat)
        ref = _low(row.get("merchant_ref"))
        if ref and ref in merch_cats:
            earns.add(merch_cats[ref])
    return earns


def guardrail_conflict(target: str, earns: set, families: dict):
    """The category the card earns in that makes `target` unsafe, or None.

    Returns the offending name so the refusal can say WHICH earning category
    blocked it — "excludes railways, earns travel" is auditable; "blocked" is
    not.
    """
    fam = families.get(target) or {target}
    hit = sorted(fam & earns)
    return hit[0] if hit else None


# --------------------------------------------------------------------------- #
# THE MAPPER
# --------------------------------------------------------------------------- #
def map_exclusion_value(value, app_categories: set):
    """(verdict, target, confidence, detail) for one exclusion_value.

    verdict:
      'category'      target is an app category name, confidence is set
      'ambiguous'     two different targets matched the same string — refused
      'not_in_app'    the phrase maps somewhere this app does not ship
      'no_vocabulary' this run cannot see the app's category list, so it
                      refuses to decide. NOT the same answer as 'not_in_app':
                      collapsing the two would be this module inventing a fact
                      about the app, which is the defect that once put 309
                      phantom errors in the validator.
      'concept'       detail names a concept the app cannot express
      'unmapped'      nothing in the table matched the whole string

    PURE, and independent of any card — so a table change can be tested on
    strings alone.
    """
    v = normalise(value)
    if not v:
        return "unmapped", None, None, "empty value"

    hits = []
    for cat, conf, rx in _SYNONYMS:
        if rx.fullmatch(v):
            hits.append((cat, conf))
    targets = {c for c, _ in hits}

    if len(targets) > 1:
        return "ambiguous", None, None, " + ".join(sorted(targets))

    if targets:
        target = hits[0][0]
        # If one string matched at two confidences, keep the lower one.
        conf = LIKELY if any(c == LIKELY for t, c in hits if t == target) else CERTAIN
        if not app_categories:
            return "no_vocabulary", None, None, target
        if target not in app_categories:
            return "not_in_app", None, None, target
        return "category", target, conf, f"'{v}' is {target}"

    for label, rx in _CONCEPTS:
        if rx.search(v):
            return "concept", None, None, label
    return "unmapped", None, None, "no app category matches this wording"


def _f3_owns(value, app_categories: set) -> bool:
    """True when f3_reach will emit an edit for this row, so f5 must not.

    See §3. Imported lazily and defensively: if f3 is gone or broken this
    module still runs, because the safety property is 'never both', not
    'never f5'.
    """
    try:
        from fixers import f3_reach
    except Exception:                                   # noqa: BLE001
        return False
    classify = getattr(f3_reach, "classify_exclusion", None)
    if not callable(classify):
        return False
    try:
        verdict = classify(value or "", app_categories)[0]
    except Exception:                                   # noqa: BLE001
        return False
    return verdict in LIVE_EXCLUSION_TYPES


# --------------------------------------------------------------------------- #
# A. FORWARD — retype an inert row onto a category the app can enforce
# --------------------------------------------------------------------------- #
def _plan_forward(ctx, findings, tally):
    app_cats = ctx.app_category_names()
    if not ctx.have_categories():
        # No app vocabulary on this run. Every target would be unverifiable, so
        # nothing is emitted and the run says so out loud.
        tally["forward.skipped_no_vocabulary"] += 1
        return []

    families = family_index(ctx.app_categories)
    merch_cats = merchant_categories(ctx)
    wanted = _cards_with(findings, "L6.EXCLUSION_TYPE_INERT")
    edits = []

    for _i, entry, _inner, cid in ctx.entries():
        if cid not in wanted:
            continue
        earns = card_earns_in(entry, merch_cats)
        for j, row in enumerate(_rows(entry, "exclusion_rules")):
            if not isinstance(row, dict):
                tally["forward.row_not_an_object"] += 1
                continue
            etype = _low(row.get("exclusion_type"))
            if etype in LIVE_EXCLUSION_TYPES:
                tally["forward.already_live"] += 1
                continue                     # idempotence: nothing to do
            raw = row.get("exclusion_value")
            verdict, target, conf, detail = map_exclusion_value(raw, app_cats)

            if verdict != "category":
                tally["forward.left_" + verdict] += 1
                if verdict == "concept":
                    tally["cannot_express." + detail] += 1
                elif verdict == "unmapped":
                    tally["unmapped_wording." + normalise(raw)] += 1
                continue

            tally["forward.would_map"] += 1
            tally["forward.would_map." + target] += 1

            # GUARDRAIL BEFORE CESSION, and the order is load-bearing.
            # Ceding first would make f5 silent about exactly the rows f3 is
            # about to write against the family guardrail — which is how the two
            # railways rows in §4 got into the file in the first place. Checking
            # first costs nothing (the outcome is 'emit nothing' either way) and
            # makes census() name every row f3 is about to get wrong.
            blocker = guardrail_conflict(target, earns, families)
            if blocker:
                tally["forward.guardrail_blocked"] += 1
                tally["forward.guardrail_blocked." + target] += 1
                tally["guardrail_case." + f"{cid} excl {target} earns {blocker}"] += 1
                if _f3_owns(raw, app_cats):
                    tally["forward.guardrail_blocked_but_f3_will_write_it"] += 1
                continue

            if _f3_owns(raw, app_cats):
                # f3_reach will write this row. Two fixers must never both.
                tally["forward.ceded_to_f3"] += 1
                tally["forward.ceded_to_f3." + target] += 1
                continue

            new_row = dict(row)                          # a copy; ctx is untouched
            new_row["exclusion_type"] = "category"
            new_row["exclusion_value"] = target
            new_row["_retyped_from"] = f"{etype or '(none)'}:{_s(raw) or ''}"
            edits.append(Edit(
                card_id=cid, block="exclusion_rules", index=j, field=None,
                old_value=dict(row), new_value=new_row,
                code="L6.EXCLUSION_TYPE_INERT",
                reason=(
                    f"The issuer's own wording here means "
                    f"{target.replace('_', ' ')}, which is one of the two kinds "
                    f"of exclusion the app can actually act on. Written the way "
                    f"it is today the app ignores it completely, so we promise "
                    f"people rewards on spending the bank pays nothing for."),
                evidence=f"exclusion_value = {trunc(_s(raw) or '')}",
                confidence=conf, reversible=True, family=FAMILY,
                notes={"target": target,
                       "matched": detail,
                       "card_earns_in": sorted(earns),
                       "target_family": sorted(families.get(target) or {target})},
            ))
            tally["forward.mapped"] += 1
            tally["forward.mapped." + target] += 1
    return edits


# --------------------------------------------------------------------------- #
# B. REPAIR — put back a row a previous sweep activated against the guardrail
# --------------------------------------------------------------------------- #
def _plan_repair(ctx, findings, tally):
    """See §4. Only rows carrying `_retyped_from` are eligible, because that
    field is the only record of what the row said before, and a revert with no
    recorded original would be an invented value.

    Scanned across every card rather than only cards named by a finding: the
    validator's L6.RULE_EXCLUDED_BY_OWN_CARD check compares category names for
    equality and never walks the tree, so the family-level case this repairs
    produces no finding to key off. That missing check is reported by census().
    """
    if not ctx.have_categories():
        tally["repair.skipped_no_vocabulary"] += 1
        return []
    families = family_index(ctx.app_categories)
    merch_cats = merchant_categories(ctx)
    edits = []

    for _i, entry, _inner, cid in ctx.entries():
        earns = card_earns_in(entry, merch_cats)
        for j, row in enumerate(_rows(entry, "exclusion_rules")):
            if not isinstance(row, dict):
                continue
            if _low(row.get("exclusion_type")) != "category":
                continue
            stamp = row.get("_retyped_from")
            if not isinstance(stamp, str) or ":" not in stamp:
                continue                     # nothing to restore it to
            target = _low(row.get("exclusion_value"))
            blocker = guardrail_conflict(target, earns, families)
            if not blocker:
                continue
            old_type, old_value = stamp.split(":", 1)
            new_row = {k: v for k, v in row.items() if k != "_retyped_from"}
            if old_type == "(none)":
                new_row.pop("exclusion_type", None)
            else:
                new_row["exclusion_type"] = old_type
            new_row["exclusion_value"] = old_value
            edits.append(Edit(
                card_id=cid, block="exclusion_rules", index=j, field=None,
                old_value=dict(row), new_value=new_row,
                code="L6.RULE_EXCLUDED_BY_OWN_CARD",
                reason=(
                    f"This card is switched off entirely at "
                    f"{target.replace('_', ' ')} merchants, and it is one of the "
                    f"cards that pays a reward on {blocker.replace('_', ' ')} — "
                    f"the family {target.replace('_', ' ')} belongs to. The app "
                    f"checks exclusions before it looks at any reward rule, so "
                    f"this does not lower the rate, it removes the card from the "
                    f"list. Putting the row back exactly as it was costs a user "
                    f"nothing; leaving it costs them the card."),
                evidence=f"_retyped_from = {trunc(stamp)}",
                confidence=CERTAIN, reversible=True, family=FAMILY,
                notes={"excluded": target, "card_earns_in": sorted(earns),
                       "conflicting_earn": blocker,
                       "target_family": sorted(families.get(target) or {target})},
            ))
            tally["repair.reverted"] += 1
            tally["repair.reverted." + target] += 1
            tally["repair_case." + f"{cid} excl {target} earns {blocker}"] += 1
    return edits


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #
def plan(ctx, findings) -> list:
    """PURE. Reads ctx and findings, returns proposed Edits, writes nothing.

    Every row handed back is a fresh dict; nothing inside ctx is touched. Run it
    twice on the same bytes and it returns the same list, because both halves
    are written against the state that makes the defect true: a retyped row is
    'category' and skipped by the forward half, and a reverted row has lost its
    `_retyped_from` and is skipped by the repair half.
    """
    findings = [f for f in (findings or []) if _get(f, "code") in HANDLES]
    tally = _Tally()
    return _plan_forward(ctx, findings, tally) + _plan_repair(ctx, findings, tally)


class _Tally(dict):
    """A Counter without importing one, so plan() and census() count alike.
    Reading a key that was never touched gives 0 and does not create it, so the
    census only lists dispositions that actually happened."""
    def __missing__(self, k):
        return 0


def census(ctx, findings) -> dict:
    """PURE, and optional. What plan() did — and, the part that matters, what it
    refused to do and why.

    The refusals are the deliverable as much as the edits are. Most of the inert
    class cannot be closed from this file at all: the rows name EMI conversions,
    cash withdrawals, card fees, tolls and gift-card purchases, and the app's
    merchant model has no field that carries any of them. That is an app feature
    request, not a data defect, and it needs a number attached or somebody will
    re-derive it from scratch next quarter.
    """
    findings = [f for f in (findings or []) if _get(f, "code") in HANDLES]
    tally = _Tally()
    edits = _plan_forward(ctx, findings, tally) + _plan_repair(ctx, findings, tally)

    counts = {}
    for f in findings:
        code = _get(f, "code")
        counts[code] = counts.get(code, 0) + 1

    def _slice(prefix):
        return dict(sorted(((k[len(prefix):], v) for k, v in tally.items()
                            if k.startswith(prefix)), key=lambda kv: (-kv[1], kv[0])))

    return {
        "family": FAMILY,
        "findings_in": counts,
        "edits_out": len(edits),
        "edits_by_code": _by(edits, lambda e: e.code),
        "edits_by_confidence": _by(edits, lambda e: e.confidence),
        "categories_source": ctx.categories_source(),
        "app_categories_known": len(ctx.app_category_names()),
        "tally": dict(sorted(k_v for k_v in tally.items()
                             if not k_v[0].startswith(("unmapped_wording.",
                                                       "cannot_express.",
                                                       "guardrail_case.",
                                                       "repair_case.")))),
        # The app feature request, with a number on it.
        "app_cannot_express": _slice("cannot_express."),
        # Wording nothing recognised — the honest residue, not a failure.
        "unrecognised_wording": _slice("unmapped_wording."),
        # Named, so a human can review each one instead of trusting a count.
        "guardrail_blocked_cases": sorted(_slice("guardrail_case.")),
        "guardrail_repairs": sorted(_slice("repair_case.")),
        "validator_gap": (
            "L6.RULE_EXCLUDED_BY_OWN_CARD compares category NAMES for equality "
            "and never walks categories.json, so a card that excludes a child "
            "category while earning its parent produces no finding at all. The "
            "repair half of this module scans every card rather than only the "
            "cards a finding names, because there is no finding to name them."),
    }


def _by(edits, key):
    out = {}
    for e in edits:
        k = key(e)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
