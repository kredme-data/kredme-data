"""scope_vocab — the scopes a reward rule can DESCRIBE but the engine cannot HOLD.

WHY THIS FILE EXISTS
--------------------
Every other L6 code asks whether a row UNDER-fires: the engine never indexes it,
never matches its channel, or always picks something else first. This table
exists for the other half — a row that fires WIDER than the evidence beside it.

The shape is always the same. Somebody reads an issuer sentence that says the
rate applies only on PhonePe, or only above Rs 1,000, or only on Wednesdays,
writes that sentence into `rule_name`, tags the row with the nearest spending
category, and ships it. The engine has no field for "on PhonePe", so the row
becomes a plain category bonus and fires on every merchant in that category and
every category beneath it. The user is quoted 10% for paying an Airtel bill
directly, is ranked on it, and earns 1%.

The row is not malformed. Every existing layer passes it: the schema is right
(L1), the words are in vocabulary (L2), the references resolve (L3), the number
is plausible (L4), the English agrees with the fields (L5 — it does, the fields
just say less than the English), the engine fires it (L6). Nothing looks at the
GAP between what the prose claims and what the fields can enforce. That gap is
what this table names.

WHAT MAY GO IN THE TABLE
------------------------
One test, and it is a test about the ENGINE, not about wording:

    Is there any field on a reward_rules row that could hold this restriction
    and that the engine actually reads?

If the answer is no, or the answer is a field the row has left null, the token
belongs here. If the answer is yes and the row used it, the row is fine and the
token must never fire on it — which is why the caller supplies the row's own
narrowing fields and refuses to call this function at all when one is set.

Authority for "the engine actually reads it", against nous/master:
    channel            _channelMatches, recommendation_engine.dart:560-573 —
                       null / online / offline / upi / app, and nothing else.
    merchant_ref       the merchant lane, engine:143-200 — and the engine
                       forbids merchant_ref and category_id on the same row, so
                       "PhonePe groceries" is not expressible at all today.
    conditions_json    _passesConditions, engine:592-681 — a switch over eleven
                       fields with NO default branch. calendar.* IS in that
                       list, so a weekday restriction is expressible and simply
                       was not written. user.* is three flags and no more.
    min_txn_amount     PARSED at credit_card.dart:456 and referenced nowhere.
                       Setting it is not a defence; it is decoration.
    spend_threshold_*  CUMULATIVE spend for the period (engine:716-731), never
                       the size of the transaction in hand.

WHAT MAY NOT GO IN THE TABLE, AND WHY
-------------------------------------
    Any word the app's category vocabulary already carries. 'wallet', 'fuel',
    'rent', 'education', 'insurance', 'travel' are CATEGORIES. The dominant
    honest shape in seed/cards.json is one issuer sentence covering N
    categories, split into N rows, each tagged with one of them — 'Insurance,
    Utilities, Rent, Government, Wallet Load, Railways, FASTag and Education'
    is nine rows on one IDFC card and every one of them is correct. A category
    name in this table would flag all nine.

    'domestic' and 'in India'. The engine has no geography at all, so a
    domestic-only rate fires on international spend too — but the app never
    sees international spend, which it handles as a forex mark-up and not as a
    transaction. Flagging it would be a finding no user can feel. The reverse,
    an INTERNATIONAL-only rate on a spending category, does over-fire on
    domestic spend and IS in the table.

    'select', 'eligible', 'certain'. Issuer prose is made of them. 'select MCC
    categories such as utility bill payments' on the IndusInd Solitaire is
    correctly modelled as a utilities row; the word carries no scope.

    'prime member', 'Swiggy One', 'Amazon Pay balance'. These three ARE
    expressible — _inferUserPrefGate (credit_card.dart:390-431) synthesises a
    gate from the rule's name — and L6.GATE_INFERRED_FROM_RULE_NAME already
    owns them. One defect, one owner.

Nothing here was invented. Every pattern is counted off seed/cards.json at
origin/main and the count is in the comment beside it. If a phrasing is not in
the file it is not in the table.

Stdlib only. Nothing here reads or writes a file. Every function is pure.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# 1. THE TABLE — (bucket, the engine field that is missing, pattern)
# --------------------------------------------------------------------------- #
SCOPE_TOKENS = [
    # A named third-party app, portal or site. The only field that could hold
    # this is merchant_ref, and the engine refuses merchant_ref together with
    # category_id — so today these rules cannot be written correctly at all and
    # the finding is an app ticket, not a data edit.
    # 36 rows: PhonePe 16, Paytm 10, intermiles.com 10 (3 as 'the InterMiles
    # website'), Google Pay 2. The domain form is FIRST in the alternation on
    # purpose: at an equal match start Python takes the earlier alternative, so
    # 'intermiles.com' is read as a site and not as the card's points currency.
    ("platform", "merchant_ref",
     r"\b[a-z0-9][a-z0-9-]*\.(?:com|in|co\.in)\b|"
     r"\bphonepe\b|\bpaytm\b|\bcred\b|mobikwik|freecharge|google ?pay|\bgpay\b|"
     r"bharatpe|tata ?neu|jiomart|smart ?buy|\bgyftr\b|gift ?edge|"
     r"edge ?portal|travel ?edge|eazydiner|\bixigo\b|cleartrip|makemytrip|"
     r"goibibo|easemytrip|\byatra\b|intermiles|\bpincode\b"),

    # The payment instrument. `channel` knows five values and none of them is a
    # standing instruction, a card network or an EMI conversion.
    # 2 rows, both 'standing instruction' (SBI IRCTC Premier, YES Prosperity).
    ("rail", "channel",
     r"\brupay\b|\bupi\b|scan ?(?:and|&|n) ?pay|\bqr\b|contactless|"
     r"tap ?(?:and|&|n) ?pay|\bnfc\b|no[- ]?cost emi|\bemis?\b|net ?banking|"
     r"\bbbps\b|auto ?pay|standing instruction|e-?mandate|\bnach\b"),

    # Where the spend happened. There is no field. 0 rows today — every
    # 'international' in the file is an enumeration member or a negation and is
    # refused by the guards in §2. Kept because the 67 channel_specific rows
    # already carrying channel='international' show how the catalogue writes it,
    # and the day one of them loses its channel this is the code that catches it.
    ("geography", "(nothing — the engine has no notion of where a spend happened)",
     r"international|overseas|\babroad\b|foreign currency|\bforex\b|"
     r"cross[- ]border|non[- ]metro|tier[- ]?2\b|tier[- ]?ii\b"),

    # The size or count of the transaction in hand. min_txn_amount is parsed and
    # never read; spend_threshold_* is CUMULATIVE spend, a different question.
    # 8 rows: SC Manhattan 2, SC Super Value 1, YES Prosperity 2, Kotak Essentia
    # 2, BOBCARD Cashback 1 ('at least 4 transactions in the statement cycle').
    ("transaction size or count", "min_txn_amount (parsed, then never read)",
     r"minimum (?:transaction|txn|purchase|spend of)|min\.? ?(?:transaction|txn)|"
     r"transactions? (?:of|above|over|between) (?:rs\.?|₹|inr)|single transaction|"
     r"per transaction of|at least \d+ (?:transactions?|txns?)|"
     r"minimum \d+ (?:transactions?|txns?)|"
     r"\d+ (?:transactions?|txns?) in (?:the|a|each|every)"),

    # Which cardholder, or which physical card. conditions_json has exactly
    # three user.* flags and the card's `network` is not per-rule.
    # 24 rows: IndusInd Platinum Aura Edge 12 (the holder picks ONE of Shop /
    # Home / Travel / Party and all twelve rules fire at once), Kotak Privy
    # League 5, and 7 'AmEx variant' / 'VISA variant' rows on the ICICI British
    # Airways and InterMiles co-brands, where the two network variants pay
    # different rates and both rows fire on both cards.
    ("membership or eligibility", "conditions_json user.* (three flags, no more)",
     r"invite[- ]only|by invitation|select customers|existing customers|\bnri\b|"
     r"salaried|priority banking|burgundy|imperia|\bprive\b|privé|private banking|"
     r"opt(?:ed)?[- ]?in\b|enrol|registered (?:customer|user)|add[- ]on card|"
     r"(?:under|opted for|chosen|selected) (?:the )?[\w']+ plan\b|"
     r"\b[\w']+ plan\s*[-–—]|\bvariant\b"),

    # When. This one IS expressible — calendar.day_of_week / calendar.month /
    # calendar.quarter are in _passesConditions — so the fix is a data edit and
    # the finding says so. 1 row: RBL Titanium Delight, '5% on grocery purchases
    # on Wednesdays', which today pays 5% on a Tuesday.
    # 'calendar month' and 'calendar year' are cap periods, not scopes, and are
    # deliberately not matched; only 'calendar quarter' is.
    ("calendar", "conditions_json calendar.* (which the engine DOES understand)",
     r"weekend|weekday|saturdays?\b|sundays?\b|mondays?\b|tuesdays?\b|"
     r"wednesdays?\b|thursdays?\b|fridays?\b|birthday|anniversary|festive|"
     r"festival|happy hours?|\bq[1-4]\b|calendar quarter"),
]
_SCOPE = [(bucket, gap, re.compile(pat, re.I)) for bucket, gap, pat in SCOPE_TOKENS]

# Fields we WROTE about this row, and fields we QUOTED from somewhere else.
# The bar is different for each; see §2.
AUTHORED_FIELDS = ("rule_name", "category_ref")
QUOTED_FIELDS = ("_note", "source_quote")
EVIDENCE_FIELDS = AUTHORED_FIELDS + QUOTED_FIELDS


# --------------------------------------------------------------------------- #
# 2. THE GATES THAT RUN BEFORE A TOKEN COUNTS
# --------------------------------------------------------------------------- #
# A scope word in a sentence is not the same as a scope on a rule. Four things
# have to be true before one counts, and each of these guards was written
# against rows that really are in the file.

# The sentence says the rate does NOT apply there. 'Utility and insurance
# spends made outside PhonePe earn base rewards' is the correctly-broad
# complement row on two SBI PhonePe cards; without 'outside' here it was
# flagged as the very defect it is the fix for. 4 rows.
NEGATION = re.compile(
    r"\b(?:not|no|non|excluding|excluded|except|other than|outside|apart from|"
    r"besides|away from|carve[ds]? out|ineligible|does not|doesn't|never)\b", re.I)

# The token is one item in a list of things the rate covers, not the scope of
# this row. This is the single biggest false-positive source in the file: an
# issuer writes 'dining, entertainment, grocery, and international', we split it
# into one row per category, and every one of those rows carries the whole
# sentence in source_quote. 40 of the first 128 candidates were this.
ENUMERATION = re.compile(
    r"\b(?:including|includes|such as|e\.g\.|like|and|or)\s*$|[,/&]\s*$", re.I)

# For QUOTED fields only: the token must be governed by a word that makes it a
# restriction rather than a mention. 'spends done on your birthday' in an IDFC
# Millennia quote describes a 10x birthday rate we have not written down at all;
# it is not a restriction on the 1.25% dining row that carries the quote.
EXCLUSIVITY = re.compile(
    r"\b(?:only|exclusively|solely|must be|restricted to|limited to|"
    r"applicable (?:only|when)|requires?|required|made (?:via|through|at)|"
    r"done (?:via|through)|booked (?:via|through)|when (?:paid|booked)|"
    r"through the|via the|subject to)\b", re.I)

# A brand word that is ALSO the name of the points this card pays is ambiguous.
# 'a maximum of 2,000 InterMiles per transaction' is a cap in a currency;
# 'bookings on intermiles.com' is a platform. The currency reading is refused
# only when nothing resolves it — no domain, no governing preposition, no
# platform noun after it. 2 rows.
GOVERNOR = re.compile(r"\b(?:on|at|via|through|using|with|from)\s+(?:the\s+|your\s+)?$", re.I)
PLATFORM_NOUN = re.compile(
    r"\b(?:app|application|web ?site|portal|platform|marketplace|mall)\b|"
    r"\.(?:com|in)\b", re.I)
_DOMAIN = re.compile(r"\.(?:com|in|co\.in)$", re.I)

# The token names the card, not a place to spend. 'using your EazyDiner Credit
# Card' is the card in the user's hand. 1 row.
NAMES_THE_CARD = re.compile(r"^\s*(?:bank\s+)?(?:credit\s+)?card\b", re.I)


def _window(text, m, before=48, after=48):
    return text[max(0, m.start() - before):m.start()], text[m.end():m.end() + after]


def scope_hits(text, *, authored: bool, currency_words=()) -> list:
    """[(bucket, engine_gap, token)] — the scopes this text restricts a rule to.

    `authored` True for rule_name / category_ref, which we wrote about THIS row
    and which therefore speak for it; False for _note / source_quote, which are
    somebody else's sentence and have to prove they are talking about this row.

    `currency_words` is the card's reward_currency and point_currency, lowered.

    Never raises. At most one hit per bucket — the point is to name the KIND of
    scope that went missing, not to count every synonym for it.
    """
    out = []
    if not isinstance(text, str) or not text:
        return out
    for bucket, gap, rx in _SCOPE:
        for m in rx.finditer(text):
            tok = m.group(0)
            pre, post = _window(text, m)
            if NEGATION.search(pre[-26:]) or NEGATION.search(post[:26]):
                continue
            if ENUMERATION.search(pre):
                continue
            if not authored and not (EXCLUSIVITY.search(pre) or EXCLUSIVITY.search(post)):
                continue
            if bucket == "platform":
                if NAMES_THE_CARD.match(post):
                    continue
                if (tok.lower() in currency_words
                        and not _DOMAIN.search(tok)
                        and not GOVERNOR.search(pre)
                        and not PLATFORM_NOUN.search(post[:30])):
                    continue
            out.append((bucket, gap, tok))
            break
    return out
