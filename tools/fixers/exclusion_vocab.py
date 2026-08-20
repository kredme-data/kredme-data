"""exclusion_vocab — ONE definition of the three things an exclusion decision
needs, shared by every module that touches an exclusion row.

WHY THIS FILE EXISTS
--------------------
Two modules used to answer the same three questions in two different ways:

    what does this wording mean?      f3 had a substring table, f5 had a
                                      whole-string table, and f3's ran first,
                                      so f5's 342-pattern table decided nothing
    what does this card earn on?      f3 read five witnesses, f5 read two, so
                                      the write gate and the undo gate
                                      disagreed on 63 live rows
    which categories are one family?  shared already — but through an OPTIONAL
                                      import that failed open and silent

All three now live here, are imported at MODULE SCOPE by both fixers, and
therefore cannot drift, cannot be half-present, and cannot fail open. If this
file is missing or broken, both fixers fail to import, the runner prints
`FAIL <module> (import)` and plans nothing for them. A safety gate that can
only lose protection must never degrade quietly; it must take the run with it.

Nothing here reads or writes a file. Every function is pure.


§1  THE TABLE IS WHOLE-STRING, AND IT IS THE ONLY TABLE
-------------------------------------------------------
Every pattern in SYNONYMS is matched with re.fullmatch against the normalised
value. No substring search, no edit distance, no fuzzy anything. A substring
matcher reading "wallet cash withdrawals" sees 'wallet' and maps the row to
wallet_load, and the row was about cash. A substring matcher reading "fuel
purchases at non-BPCL fuel stations" sees 'fuel' and switches off a fuel card
at every pump. Both are one regex away from a user losing rewards, and neither
shows up as a wrong number — the card simply stops appearing.

Every entry carries a comment naming the REAL phrasings it covers, counted off
seed/cards.json. Nothing here was invented; if a phrasing is not in the file it
is not in the table. The counts are of the whole exclusion population — the 983
rows that were inert before the first sweep, plus the 484 rows a sweep has
since made live, whose original wording is preserved verbatim in
`_retyped_from` and is re-checked against this table on every run.

THREE THINGS THIS TABLE DELIBERATELY REFUSES, each because refusing is the
cheaper mistake:

    gold / silver with no other noun. "gold purchases" on the Amazon Pay ICICI
    is the issuer's DIGITAL gold clause — the card's own rule_name says
    "excluding digital/physical gold and EMI purchases" on AMAZON purchases —
    and the app's jewellery category is MCC 5944/5094, eight physical
    jewellers. Mapping it fires at 100% of the merchants the clause does not
    cover and 0% of the ones it does. "gold/jewellery" and "gold and
    jewellery" DO map: they name the category.

    gift cards and prepaid cards. A gift card is the purchase of a stored-value
    instrument; a wallet load is a transfer into one. The issuers exclude them
    in separate sentences on the same page. "gift or prepaid card loads" names
    both and is refused for it.

    anything scoped, conditional or narrative — handled by POISON before the
    table is ever consulted.

AND ONE THING IT DELIBERATELY ACCEPTS, because the alternative was a phrasing
lottery: bare wallet and bare rental wording. On a CREDIT CARD's exclusion list
the only wallet transaction the card can perform is a load — spending FROM a
wallet does not touch the card at all — so "wallets", "wallet transactions" and
"wallets/service providers" are the load slot by elimination. Likewise "rental"
and "real estate" sit in the same slot, between Railways and Education, on all
13 RBL cards that ship one published exclusion list, and on one of them the
scraper wrote "rental payments" in that slot. Refusing the bare forms split one
issuer's single list across sibling cards: RBL Cookies lost its rewards at a
rent merchant and RBL Platinum Delight, identical T&C, kept them. Every row
activated on this reading is listed by card and wording in census() under
`activated_rows_for_review`, so the call is signed off rather than implied.
"""
from __future__ import annotations

import re

from fixers.base import CERTAIN, LIKELY

# The only two exclusion_type values recommendation_engine.dart:486-497 can act
# on. Anything else is inert: the card claims an exclusion it never enforces.
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
# 2. THE GATES THAT RUN BEFORE THE TABLE
# --------------------------------------------------------------------------- #
# POISON — a value that is conditional, scoped, or narrative is not a flat
# exclusion, and re-typing one inverts its meaning. "Fuel excluded from the base
# rate" means fuel still earns the accelerated rate; switch it to a flat
# category exclusion and the card stops earning on fuel entirely.
#
# The four narrative forms that say "this category earns nothing" in a full
# sentence are listed in the table itself and are matched whole-string BEFORE
# poison is consulted, because they are flat exclusions written out longhand.
POISON = re.compile(
    r"reduced rate|reduced reward|reduced to|earns? (?:only|reduced|at reduced)|"
    r"capped at|\bexcept\b|other than|excluded from|not eligible for|"
    r"\bwhen\b|\bunless\b|\bif\b|\bonly\b|"
    r"below (?:rs\.?|₹|inr)?\s*\d|less than|contradicted|not listed|"
    r"specific categories|\(for |beyond|non-jio|non-bpcl|not made through|"
    r"do not earn (?:regular|accelerated|the)|up ?to \d|post facto|"
    r"from october|as per t&c|subject to",
    re.I)

# BRAND — the value names one merchant. "swiggy money wallet" is a wallet, but
# excluding the app's whole wallet_load category on the strength of it would
# switch off Paytm, PhonePe, Amazon Pay, MobiKwik and Ola Money too. An
# illustrative brand ("wallet loads (e.g. paytm)") is not a restriction, so an
# example marker cancels this gate.
BRAND = re.compile(
    r"swiggy|freecharge|paytm|phonepe|amazon pay|smartbuy|bills2pay|\bjio\b|"
    r"bpcl|\bcred\b|myntra|flipkart|cleartrip|tata neu|indianoil|hpcl",
    re.I)
EXAMPLE_MARKER = re.compile(r"e\.g\.|such as|including|\blike\b", re.I)

# The value already carries the answer.
MCC_LITERAL = re.compile(r"^mcc[ _]?(\d{4})$", re.I)


# --------------------------------------------------------------------------- #
# 3. THE SYNONYM TABLE — explicit, whole-string, every pattern commented with
#    the real phrasings it covers and how many rows carry each one.
# --------------------------------------------------------------------------- #
# (app_category, confidence, pattern)
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
    # "government spends like advance tax" 1 — the example marker cancels the
    # brand gate and the sentence still names one category and only one.
    ("government", CERTAIN, r"government spends? like advance tax"),
    # "tax payments" 5 · "tax" 1 · "taxes" 1 · "tax-related payments" 1.
    # Tax is the app's government category: advance tax, GST challans and
    # municipal dues all sit on the government MCCs. Nothing else in the app's
    # vocabulary could hold them.
    ("government", LIKELY, r"tax(?:es)?(?:[ -]related)?(?: payments?)?"),

    # ---------------- wallet_load ------------------------------------------
    # Explicit LOAD wording.
    # "wallet reloads" 26 · "wallet load" 7 · "wallet loading" 7 · "e-wallet
    # loading" 6 · "wallet reload" 3 · "e-wallet loading transactions" 3 ·
    # "e-wallet reloading" 2 · "e-wallet uploads" 2 · "wallet reloading" 2 ·
    # "e-wallet reloads" 1 · "wallet loading transactions" 1 · "wallet upload"
    # 1 · "wallet uploads" 1 · "mobile wallet uploads" 1
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
    # "digital wallet loading or top-up transactions" 1 — one issuer's long
    # form, spelled out because the pattern above stops at one verb.
    ("wallet_load", CERTAIN,
     r"digital wallet loading or top[- ]?up transactions?"),
    # "wallet loads (e.g. paytm)" 1 · "wallet reloads/payments" 1 · "wallet
    # reloads using credit card" 1 · "wallet or service provider loads" 1 —
    # four longer forms that all say LOAD in words.
    ("wallet_load", CERTAIN,
     r"wallet loads? \(e\.g\. paytm\)|"
     r"wallet reloads?/payments?|"
     r"wallet reloads? using credit cards?|"
     r"wallets? or service providers? loads?"),
    # The bare forms. "wallet" 18 · "wallet transactions" 5 · "wallets" 4 ·
    # "wallets/service providers" 3 · "e-wallet transactions" 2 · "e-wallets" 2
    # · "wallet/service providers" 2 · "wallet payments" 2 · "wallet spends" 2.
    # See §1: on a CREDIT CARD the only wallet transaction the card can perform
    # is a load, so the bare wording is the load slot by elimination. Kept at
    # LIKELY, because it is an elimination argument and not the issuer's word,
    # so a --confidence certain run will not newly activate one.
    ("wallet_load", LIKELY,
     r"(?:e[- ]?)?wallets?"
     r"(?:[ /](?:service providers?|transactions?|payments?|spends?))?"),

    # ---------------- rent --------------------------------------------------
    # "rent payments" 27 · "rental payments" 22 · "rent transactions" 4 ·
    # "rental transactions" 2. In Indian card T&Cs "rental payment" with no
    # other noun is house rent — it is the sentence that names CRED, NoBroker
    # and RedGirraffe.
    ("rent", CERTAIN, r"rent(?:al)? (?:payments?|transactions?|spends?)"),
    # "payments towards rent/property management" 2 · "rent/property
    # management" 1 · "rental commissions" 1 · "property management" 1 ·
    # "property management payments" 1 · "property management fees" 1 ·
    # "payment of property management fees, rental commissions, and rental
    # payments" 1. Property management and rental commission are the
    # letting-agent side of the same transaction and sit on the same MCC 6513
    # the app files under rent.
    ("rent", LIKELY,
     r"(?:payments? towards )?rent/property management|"
     r"property management(?: (?:payments?|fees?))?|"
     r"rental commissions?|"
     r"payment of property management fees, rental commissions, and "
     r"rental payments"),
    # "real estate agents and managers" — the published name of MCC 6513, which
    # is the MCC the app's own categories.json puts in `rent`. Refusing the
    # category's own definition was the clearest of the phrasing-lottery cases.
    ("rent", CERTAIN, r"real ?estate agents and managers"),
    # The bare forms. "rental" 6 · "rentals" 6 · "real estate" 4 · "real
    # estate/rental" 3 · "property rental" 3 · "real estate services" 1 ·
    # "rental or real-estate payments" 1 · "real estate/rentals" 1 · "rental
    # services" 1 · "real estate or rental transactions" 1. See §1: this is one
    # slot in one published list, spelled ten ways by our scraper. LIKELY, for
    # the same reason as the bare wallet forms.
    ("rent", LIKELY,
     r"rentals?|rental services?|property rentals?|"
     r"real ?estates?(?: services?)?|"
     r"real ?estate ?[/&] ?rentals?|"
     r"rentals? or real[ -]?estate payments?|"
     r"real ?estate or rental transactions?"),

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
    # of these is checked against the travel family before it is written — a
    # card whose only earn is `travel` must never have `railways` switched on.
    # (There is no bare "irctc" row in the file today; a pattern for one used to
    # sit here matching nothing, and it was deleted rather than left to imply a
    # count it did not have.)
    ("railways", CERTAIN,
     r"railways?(?: (?:transactions?|bookings?|payments?|spends?))?"),

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

    # ---------------- grocery -----------------------------------------------
    # "supermarkets" 1 · "supermarkets and retail stores" 1 · "supermarket and
    # retail store purchases" 1. The app's grocery category IS MCC 5411/5451/
    # 5499 — supermarkets. The "retail stores" half names something wider than
    # the app can express, so the row is activated on the half it can: a subset
    # of the issuer's clause, never a superset.
    ("grocery", CERTAIN, r"supermarkets?"),
    ("grocery", LIKELY,
     r"supermarkets? and retail stores?|"
     r"supermarket and retail store purchases?"),

    # ---------------- jewellery ---------------------------------------------
    # "jewelry" 7 · "jewellery purchases" 4 · "jewelry purchases" 4 ·
    # "jewellery items" 1 · "purchase of jewelry items" 1 · "jewelry & antique
    # items" 1 · "jewelry and antique item purchases" 1 · "clock, jewellery,
    # watch & silverware stores" 1 (the published name of MCC 5944). Both
    # spellings ship.
    ("jewellery", CERTAIN,
     r"jewell?ery|jewell?ry|"
     r"jewell?e?ry (?:purchases?|items?|spends?|transactions?)|"
     r"purchases? of jewell?e?ry items?|"
     r"jewell?e?ry ?[/&] ?antique items?|"
     r"jewell?e?ry (?:and|&) antique items?(?: purchases?)?|"
     r"clock, jewell?e?ry, watch ?[/&] ?silverware stores?"),
    # "gold/jewellery" 3 · "gold and jewellery" 1 — these name the category.
    # A bare "gold", "silver", "gold purchases", "gold spends", "purchase of
    # gold items" and "precious metal purchases" do NOT map; see §1.
    ("jewellery", LIKELY,
     r"gold ?[/&] ?jewell?e?ry|gold (?:and|&) jewell?e?ry"),

    # ---------------- fuel --------------------------------------------------
    # "fuel purchases" 26 · "fuel spends" 13 · "fuel & auto" 5 · "fuel and
    # auto" 2 · "fuel spending" 2 · "fuel expenditures" 1 · "petrol" 1.
    # "fuel & auto" activates on the half the app can express (fuel); the
    # automotive half has no field, and a subset of the issuer's clause is the
    # safe direction.
    ("fuel", CERTAIN,
     r"fuel(?: (?:purchases?|transactions?|spends?|spending|payments?|"
     r"expenditures?))?|"
     r"fuel ?(?:&|and) ?auto|"
     r"petrol|diesel|petrol/diesel"),
    # The longhand forms. "fuel transactions are not eligible for reward
    # points" 2 · "fuel transactions do not earn reward points" 2 · "fuel
    # transactions excluded from reward point earning" 1 · "fuel transactions
    # (no reward points)" 1 · "fuel purchases are not eligible for reward
    # points" 1 · "fuel transactions (no intermiles earned)" 1 · "fuel
    # purchases (0 intermiles earned)" 1. POISON refuses these on sight because
    # they read like scoped text; they are not, they are one flat exclusion
    # written as a sentence, and each is listed here whole so that the reading
    # is reviewed rather than inferred.
    ("fuel", CERTAIN,
     r"fuel (?:transactions?|purchases?|spends?) "
     r"(?:are not eligible for reward points|"
     r"do not earn reward points|"
     r"excluded from reward point earning)|"
     r"fuel (?:transactions?|purchases?) "
     r"\((?:no reward points|no intermiles earned|0 intermiles earned)\)"),

    # ---------------- telecom -----------------------------------------------
    # "telecom" 3 · "telecom bills" 1 · "telecom payments" 1 ·
    # "telecommunications" 1. A bare "recharges" is NOT here: a recharge is as
    # easily a wallet top-up as a mobile top-up, and the file cannot say which.
    ("telecom", CERTAIN,
     r"telecom(?:munications?)?"
     r"(?: (?:payments?|bills?|transactions?|services?|spends?))?"),
]

# Compiled once, in table order. Order does not decide anything — a value that
# matches two different targets is refused as ambiguous rather than resolved by
# position — but a fixed order keeps two runs byte-identical.
_SYNONYMS = [(cat, conf, re.compile(pat)) for cat, conf, pat in SYNONYMS]

# The longhand fuel sentences above are flat exclusions spelled out, so they are
# tried before POISON rather than after it. Nothing else may join this list
# without the same argument being made in writing.
_POISON_EXEMPT = re.compile(
    r"fuel (?:transactions?|purchases?|spends?) "
    r"(?:are not eligible for reward points|"
    r"do not earn reward points|"
    r"excluded from reward point earning)|"
    r"fuel (?:transactions?|purchases?) "
    r"\((?:no reward points|no intermiles earned|0 intermiles earned)\)")


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
     r"gift ?card|gift ?voucher|voucher purchase|novelty|"
     r"gift or prepaid card|prepaid cards?\b"),
    ("Digital or physical gold as an instrument",
     r"^gold$|^silver$|^gold (?:purchases?|spends?|items?|transactions?)$|"
     r"^silver (?:purchases?|spends?|items?|transactions?)$|"
     r"^purchases? of (?:gold|silver) items?$|precious metal"),
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
# 4. THE MAPPER — the only thing in this repo that decides what a wording means
# --------------------------------------------------------------------------- #
def map_exclusion_value(value, app_categories):
    """(verdict, target, confidence, detail) for one exclusion_value.

    verdict:
      'mcc'           target is a 4-digit code, taken off the value itself
      'category'      target is an app category name, confidence is set
      'poison'        conditional, scoped or narrative — not a flat exclusion
      'brand'         names one merchant, not a category
      'ambiguous'     two different targets matched the same string — refused
      'not_in_app'    the phrase maps somewhere this app does not ship
      'no_vocabulary' this run cannot see the app's category list, so it
                      refuses to decide. NOT the same answer as 'not_in_app':
                      collapsing the two would be inventing a fact about the
                      app, which is the defect that once put 309 phantom errors
                      in the validator.
      'concept'       detail names a concept the app cannot express
      'unmapped'      nothing in the table matched the whole string

    PURE, and independent of any card — so a table change can be tested on
    strings alone.
    """
    v = normalise(value)
    if not v:
        return "unmapped", None, None, "empty value"

    m = MCC_LITERAL.match(v)
    if m:
        return "mcc", m.group(1), CERTAIN, "the value is already an MCC"

    if POISON.search(v) and not _POISON_EXEMPT.fullmatch(v):
        return "poison", None, None, "conditional or scoped wording"

    if BRAND.search(v) and not EXAMPLE_MARKER.search(v):
        return "brand", None, None, "names one merchant, not a category"

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


# --------------------------------------------------------------------------- #
# 5. THE CATEGORY FAMILY
# --------------------------------------------------------------------------- #
class FamilyTreeUnreadable(Exception):
    """The app's categories.json cannot be walked as a tree on this run.

    Raised rather than shrugged off, because the failure mode is invisible: a
    tree that will not resolve degrades the family gate to plain name equality —
    exactly the behaviour the gate exists to replace — and every caller would
    carry on emitting edits with no error and no counter. Changing nothing but
    the JSON *type* of `id` (int -> string, parent_id left an int) used to do
    that silently.
    """


def _cid(v):
    """Ids are compared as strings on BOTH sides, so a file that types `id` as
    a string and `parent_id` as an int still resolves."""
    return None if v is None else str(v)


def family_index(app_categories) -> dict:
    """category_name -> {every name in its family}: itself, its ancestors and
    its descendants, from the app's own categories.json.

    Siblings are deliberately NOT family: 'airlines' and 'railways' share a
    parent, and a card that pays on flights has no claim on train tickets.

    A cycle in the file cannot hang this — both walks carry a seen-set.
    Raises FamilyTreeUnreadable when the parent links do not resolve.
    """
    rows = [c for c in (app_categories or []) if isinstance(c, dict)
            and c.get("category_name")]
    name = {_cid(c.get("id")): c["category_name"] for c in rows
            if c.get("id") is not None}
    parent = {_cid(c.get("id")): _cid(c.get("parent_id")) for c in rows
              if c.get("id") is not None}

    if rows and not name:
        raise FamilyTreeUnreadable("no category row carries an id")
    linked = [pid for pid in parent.values() if pid is not None]
    if rows and not linked:
        raise FamilyTreeUnreadable(
            "not one category row has a parent_id — the app's own file nests "
            "railways under travel, so a flat read means the file was not "
            "understood, not that the tree is flat")
    dangling = sorted({pid for pid in linked if pid not in name})
    if dangling:
        raise FamilyTreeUnreadable(
            f"parent_id {dangling[0]!r} names no category row "
            f"({len(dangling)} dangling in all)")

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


def family_closed(cats: set, families: dict) -> set:
    """`cats`, widened to every category in the same family as anything in it."""
    out = set(cats)
    for c in cats:
        out |= families.get(c, set())
    return out


# --------------------------------------------------------------------------- #
# 6. THE APP'S OWN INDEXES
# --------------------------------------------------------------------------- #
def _low(v):
    return v.strip().lower() if isinstance(v, str) and v.strip() else ""


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _rows(entry, block):
    v = entry.get(block) if isinstance(entry, dict) else None
    return v if isinstance(v, list) else []


def _merchant_rows(ctx):
    m = getattr(ctx, "merchants", None)
    rows = m.get("merchants") if isinstance(m, dict) else m
    return [r for r in (rows or []) if isinstance(r, dict)]


def mcc_owner(ctx) -> dict:
    """'6513' -> 'rent'. Built from the app's own categories.json mcc_ranges, so
    an MCC exclusion can be guarded against the same card paying on whatever
    category that MCC belongs to."""
    out = {}
    for c in (getattr(ctx, "app_categories", None) or []):
        if not isinstance(c, dict):
            continue
        nm = _low(c.get("category_name"))
        if not nm:
            continue
        for rng in (c.get("mcc_ranges") or []):
            if not isinstance(rng, dict):
                continue
            if rng.get("exact") is not None:
                out.setdefault(str(rng["exact"]), nm)
            lo, hi = rng.get("from"), rng.get("to")
            if lo is not None and hi is not None:
                try:
                    for code in range(int(lo), int(hi) + 1):
                        out.setdefault(f"{code:04d}", nm)
                except (TypeError, ValueError):
                    continue
    return out


def merchant_index(ctx) -> dict:
    """merchant key -> {'category': app category name, 'mccs': {codes}}.

    seed/merchants.json rows key on 'merchant_name'; there is no 'slug' field,
    and 'category_id' on a merchant row IS the app category NAME, not an id.
    The other key spellings are accepted defensively in case a future row
    carries one.
    """
    out = {}
    for r in _merchant_rows(ctx):
        mccs = {str(x) for x in (r.get("mcc_codes") or []) if x is not None}
        if r.get("mcc_primary") is not None:
            mccs.add(str(r["mcc_primary"]))
        rec = {"category": _low(r.get("category_id")), "mccs": mccs}
        for k in ("merchant_name", "slug", "merchant_slug", "merchant_ref"):
            key = _low(r.get(k))
            if key:
                out.setdefault(key, rec)
    return out


# A merchant brand whose name is also an ordinary English word cannot be
# recognised in prose. Every one of these is a real row in seed/merchants.json;
# the list exists so that "nothing is earned on X" does not read as the brand
# 'Nothing' and hand the card an electronics earning it does not have.
_BRAND_STOPWORDS = {
    "nothing", "noise", "central", "district", "steam", "more", "prime",
    "apple", "google", "microsoft", "minimalist", "crossword", "kindle",
    "landmark", "savana", "titan", "libas", "soch",
}

# The merchant rows whose key is <brand>_<instrument>. The brand alone is what a
# card is co-branded with — "PhonePe SBI Card SELECT BLACK" never says "PhonePe
# Wallet" — so the leading token is taken as a name in its own right, but only
# when the trailing token is one of these three instruments.
_INSTRUMENT_SUFFIX = ("wallet", "money", "pay")


def _brand_tokens(row) -> set:
    """Every string that names this merchant's brand, lowercased.

    Parentheses are dropped rather than split: "Westside (Tata)" is Westside,
    and taking 'Tata' out of it made every Tata Neu rule look like an apparel
    earning. Slashes ARE split: "Apollo Pharmacy / Apollo 24|7" is two real
    names for one merchant.
    """
    out = set()
    mn = _low(row.get("merchant_name"))
    if mn:
        out.add(mn.replace("_", " "))
        if "_" in mn:
            head, _, tail = mn.rpartition("_")
            if tail in _INSTRUMENT_SUFFIX and "_" not in head and len(head) >= 5:
                out.add(head)
    disp = row.get("display_name")
    if isinstance(disp, str):
        stripped = re.sub(r"\([^)]*\)", " ", disp)
        for part in stripped.split("/"):
            out.add(_low(part))
    return {t for t in out
            if len(t) >= 4 and re.search(r"[a-z]", t)
            and t not in _BRAND_STOPWORDS}


def _ambiguous_short_tokens(rows) -> set:
    """Short tokens derived from a <brand>_<instrument> key that are ALSO the
    full name of a different merchant, and so cannot say which one prose meant.

    'amazon' derived from `amazon_pay` is exactly the merchant `amazon`, so a
    rule reading "3% cashback on Amazon" would have handed the card a
    wallet_load earning and blocked every wallet exclusion on it. 'phonepe' from
    `phonepe_wallet` names nothing else, which is why it survives and does the
    job it was added for.
    """
    full, derived = set(), set()
    for r in rows:
        mn = _low(r.get("merchant_name"))
        if not mn:
            continue
        full.add(mn.replace("_", " "))
        disp = r.get("display_name")
        if isinstance(disp, str):
            stripped = re.sub(r"\([^)]*\)", " ", disp)
            for part in stripped.split("/"):
                full.add(_low(part))
        head, _, tail = mn.rpartition("_")
        if tail in _INSTRUMENT_SUFFIX and "_" not in head and len(head) >= 5:
            derived.add(head)
    return {t for t in derived if t in full}


def cobrand_index(ctx) -> list:
    """[(compiled name pattern, merchant key, app category)] for every merchant
    that has a category, used to spot a co-brand by name.

    THIS IS THE WITNESS THE GUARDRAIL WAS MISSING. A co-brand's earning is
    expressed through the categories its partner's spends fall into — the
    PhonePe SBI SELECT BLACK's rules are named "10 Reward Points per 100 spent
    on eligible PhonePe and Pincode spends" and are filed under telecom,
    utilities, insurance and travel. Not one of them says merchant_ref
    'phonepe_wallet', so a guardrail that only reads structured fields cannot
    see that `wallet_load` — the one row in merchants.json that IS PhonePe — is
    the sole home of the card's own partner. Switching it off removes all three
    PhonePe cards from the pick screen at the one merchant they are best at.
    Same shape, different noun: the ICICI HPCL Coral, dead at HPCL, BPCL and
    IndianOil because its only rule is "Base reward rate" and its exclusion
    says fuel.
    """
    rows = _merchant_rows(ctx)
    ambiguous = _ambiguous_short_tokens(rows)
    out = []
    for r in rows:
        cat = _low(r.get("category_id"))
        if not cat:
            continue
        key = _low(r.get("merchant_name")) or cat
        for tok in _brand_tokens(r) - ambiguous:
            out.append((re.compile(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])"),
                        key, cat))
    return sorted(out, key=lambda t: (t[1], t[2], t[0].pattern))


# The guardrail's prose vocabulary. Wider than the mapping table on purpose:
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
    # LPG moved off `fuel` and onto `utilities`, from the app's own MCC map:
    # fuel is 5541/5542, service stations. A cooking-gas cylinder is MCC 4900,
    # which categories.json files under utilities, and both LPG merchants the
    # app ships (Indane Gas, Bharat Gas) sit there. Left where it was, the
    # Axis ACE — whose utilities rule reads "electricity, gas, water, internet,
    # LPG, broadband" — counted as a card that earns at petrol pumps, and the
    # repair half put its fuel exclusion back on the strength of it.
    "utilities": r"utilit|electricity|\bgas\b|\blpg\b|water bill|broadband",
    "fuel": r"\bfuel\b|petrol|diesel|filling station|petrol pump",
    "grocery": r"grocer|supermarket|kirana|departmental",
}
_PAYS_HINTS = {k: re.compile(v, re.I) for k, v in PAYS_HINTS.items()}


# --------------------------------------------------------------------------- #
# 7. WHAT DOES THIS CARD EARN ON? — one definition, used by every gate
# --------------------------------------------------------------------------- #
def card_earns_in(entry, merchants, mccs, cobrands, *, wide=False) -> set:
    """Every app category this card looks like it rewards.

    Read wide and fail safe. A rule counts as paying unless its rate is
    explicitly zero or negative — an absent rate is unknown, and unknown is not
    the same as nothing. Five independent witnesses, because the Octane
    near-miss got past a check that read one of them and the PhonePe case got
    past a check that read two:

        category_id      the structured field
        category_ref     the prose that was never structured
        rule_name        the issuer's own sentence, through PAYS_HINTS
        merchant_ref     resolved through merchants.json to its category and MCC
        co-brand         a merchant named in the CARD's own name or in a rule's
                         name, resolved to that merchant's category

    `wide` adds the blunt one: any card shipping fuel_surcharge_rules is treated
    as paying on fuel. 359 of 383 cards ship that block, so this single line
    sends every fuel remap to a human. That is right for a gate that decides
    whether to ACTIVATE an exclusion, where the cost of a false positive is one
    skipped fix. It is wrong for the gate that decides whether to REVERT one,
    where a false positive puts an exclusion back that the issuer really does
    apply — so the repair half runs with wide=False and reports the difference
    instead of acting on it.
    """
    earns = set()
    card = entry.get("card") if isinstance(entry, dict) else None
    card_name = _low(card.get("card_name")) if isinstance(card, dict) else ""

    for rx, _key, cat in cobrands:
        if card_name and rx.search(card_name):
            earns.add(cat)

    for row in _rows(entry, "reward_rules"):
        if not isinstance(row, dict):
            continue
        rate = _num(row.get("reward_rate"))
        if rate is not None and rate <= 0:
            continue                 # this rule pays nothing; it guards nothing
        cat = _low(row.get("category_id"))
        if cat:
            earns.add(cat)
        prose = " ".join(x for x in (_low(row.get("category_ref")),
                                     _low(row.get("rule_name"))) if x)
        if prose:
            for target, rx in _PAYS_HINTS.items():
                if rx.search(prose):
                    earns.add(target)
            for rx, _key, mcat in cobrands:
                if rx.search(prose):
                    earns.add(mcat)
        ref = _low(row.get("merchant_ref"))
        rec = merchants.get(ref) if ref else None
        if rec:
            if rec["category"]:
                earns.add(rec["category"])
            for code in rec["mccs"]:
                owner = mccs.get(code)
                if owner:
                    earns.add(owner)

    if wide and _rows(entry, "fuel_surcharge_rules"):
        earns.add("fuel")
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
