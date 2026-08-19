#!/usr/bin/env python3
"""
taxonomy.py — what a spend category IS, so the writer can find the row it means.

    from pipeline.taxonomy import Taxonomy, default_taxonomy

An issuer observation says `category: "groceries_and_bill_payments"`. A reward
rule says `category_id: "grocery"`, or nothing at all and `merchant_ref:
"indian_oil"`, or nothing at all and a prose `category_ref`. Turning the first
into the second is the whole job of this module, and it is deliberately NOT in
diff.py: "what a category is" is a question about the catalogue, and "what the
writer may do with it" is a question about safety. Keeping them apart is what
lets the guardrail be tested on its own.

Three things live here:

  * `resolve(text)`   issuer prose -> the app's own category slugs. Never a guess:
                      every alias is either the slug itself, a word out of the
                      app's own display name, or a merchant name out of
                      seed/merchants.json. A word we do not recognise resolves to
                      nothing, and the caller must then refuse to place anything.
  * `family(slug)`    the slug, its ancestors and its descendants. Used by the
                      exclusion guardrail: excluding "travel" kills "hotels", and
                      excluding "hotels" makes a "travel" bonus partly dead.
  * `category_of_merchant(name)`   'indian_oil' -> 'fuel'.

★ THE TREE IS A UNION OF TWO TREES, ON PURPOSE. seed/merchants.json and the app's
own categories.json disagree on three parent edges today (apparel and electronics
sit under online_shopping in the app and at the root in the seed; cabs sits under
travel in the app and at the root in the seed). For the guardrail a WIDER family
is the safe direction — it can only make us refuse to switch an exclusion on, and
refusing costs nothing while wrongly switching one on zeroes a card for a whole
merchant. So both sets of edges are kept.

Stdlib only. No network. Reads two JSON files and nothing else, and tolerates
both of them being absent (an empty taxonomy resolves nothing, which makes the
writer refuse rather than invent).
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from dataclasses import dataclass, field

if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline import config as C  # noqa: E402

# The app's category vocabulary, mirrored under tools/ because kredme-data cannot
# check the private app repo out. tools/app_mirror/categories.json carries the
# copy and the date it was taken.
APP_CATEGORIES = C.REPO / "tools" / "app_mirror" / "categories.json"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# "₹10,000" and "2,00,000" must survive as ONE token. Without this the Indian
# grouping splits them into '10'/'000' and '2'/'00'/'000', and a rule whose only
# distinguishing feature is its threshold ("online spends above ₹10,000" vs "up
# to ₹10,000") becomes indistinguishable from its sibling — which is how a 5%
# tier and a 3% tier end up looking like the same rule.
_DIGIT_GROUP = re.compile(r"(\d),(?=\d)")

# Words that carry no category meaning. They are dropped before matching so
# "groceries and bill payments" cannot match a category through the word "and".
# 'spend'/'spends' are here because every issuer sentence contains them.
_STOPWORDS = frozenset({
    "", "a", "an", "and", "at", "all", "by", "for", "from", "in", "of", "on",
    "or", "per", "the", "to", "via", "with", "your", "you",
    "spend", "spends", "spending", "transaction", "transactions", "txn",
    "payment", "payments", "purchase", "purchases", "category", "categories",
    "other", "others", "card", "credit", "monthly", "month", "cycle", "annual",
    "statement", "first", "post", "up", "above", "below", "max", "maximum",
    "rs", "inr", "amount", "value", "outlets", "outlet", "store", "stores",
    "done", "made", "new", "bank", "app", "portal", "online", "offline",
})

# A much shorter list, used only when comparing an observation's category with a
# rule's own prose to decide WHICH ROW is meant. That question is about textual
# identity, not about category meaning, and the words dropped above are exactly
# the ones that tell two rules on the same card apart: 'first 6 months' vs 'post
# 6 months', 'up to ₹10,000' vs 'above ₹10,000', 'online' vs 'in-store'. Two
# vocabularies because there are two questions.
_MATCH_STOPWORDS = frozenset({
    "", "a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or",
    "per", "the", "to", "with", "your", "you", "spend", "spends", "spending",
    "amp", "rs", "inr",
})

# Issuer prose -> app slug, by hand and kept SHORT. Every entry here is a word an
# Indian issuer actually printed on one of the ten cards' own pages; nothing is
# added speculatively. Anything not in this table, not a slug, not a word of the
# app's display name and not a merchant name simply does not resolve — and an
# observation that does not resolve is reported, never guessed at.
_PROSE_ALIASES = {
    "groceries": "grocery",
    "grocer": "grocery",
    "supermarket": "grocery",
    "supermarkets": "grocery",
    "departmental": "departmental_store",
    "restaurant": "dining",
    "restaurants": "dining",
    "dine": "dining",
    "food": "dining",
    "petrol": "fuel",
    "diesel": "fuel",
    "utility": "utilities",
    "utilities": "utilities",
    "bill": "utilities",
    "bills": "utilities",
    "electricity": "utilities",
    "rental": "rent",
    "rents": "rent",
    "govt": "government",
    "tax": "government",
    "taxes": "government",
    "tuition": "education",
    "school": "education",
    "college": "education",
    "gold": "jewellery",
    "jewelry": "jewellery",
    "wallet": "wallet_load",
    "wallets": "wallet_load",
    "railway": "railways",
    "irctc": "railways",
    "train": "railways",
    "flight": "airlines",
    "flights": "airlines",
    "airline": "airlines",
    "air": "airlines",
    "hotel": "hotels",
    "lodging": "hotels",
    "cab": "cabs",
    "taxi": "cabs",
    "movie": "entertainment",
    "movies": "entertainment",
    "medicine": "pharmacy",
    "medicines": "pharmacy",
    "chemist": "pharmacy",
    "healthcare": "pharmacy",
    "recharge": "telecom",
    "recharges": "telecom",
    "mobile": "telecom",
    "apparel": "apparel",
    "fashion": "apparel",
    "clothing": "apparel",
    "ecommerce": "online_shopping",
    "shopping": "online_shopping",
    "electronic": "electronics",
    "appliances": "electronics",
    "insurances": "insurance",
    "premium": "insurance",
}

# Display-name words that must NOT become aliases. "Payments" appears in three
# display names, so left alone it would make "Government Payments" and "Utility
# Bill Payments" both match any sentence containing the word.
_DISPLAY_WORD_BLOCKLIST = frozenset({
    "payments", "payment", "services", "service", "stations", "station",
    "loads", "load", "and", "amp", "metals", "precious", "transport",
    # 'online' is a CHANNEL, not a category. Left in, "Online Shopping" would
    # make every "online spends" sentence resolve to the online_shopping
    # category, and an exclusion typed off that would zero the card for a whole
    # merchant family it really does pay on.
    "online",
})


def tokens(text: object) -> list[str]:
    """Lowercase alphanumeric words, stopwords dropped, order preserved."""
    if not isinstance(text, str):
        return []
    raw = _NON_ALNUM.sub(" ", _DIGIT_GROUP.sub(r"\1", text.lower())).split()
    return [t for t in raw if t not in _STOPWORDS]


def match_tokens(text: object) -> list[str]:
    """Words kept for deciding WHICH ROW a sentence is about. See _MATCH_STOPWORDS."""
    if not isinstance(text, str):
        return []
    raw = _NON_ALNUM.sub(" ", _DIGIT_GROUP.sub(r"\1", text.lower())).split()
    return [t for t in raw if t not in _MATCH_STOPWORDS]


def _phrases(words: list[str]) -> list[str]:
    """Single words plus adjacent pairs joined by '_'.

    The pair form is what lets 'departmental store' and 'wallet load' resolve,
    because that is how the app spells those two slugs.
    """
    out = list(words)
    out += ["%s_%s" % (words[i], words[i + 1]) for i in range(len(words) - 1)]
    return out


@dataclass(frozen=True)
class Taxonomy:
    """The category tree, the merchant table and the alias index, resolved once."""

    slugs: frozenset = frozenset()
    parents: dict = field(default_factory=dict)        # slug -> set of parent slugs
    children: dict = field(default_factory=dict)       # slug -> set of child slugs
    merchant_category: dict = field(default_factory=dict)   # merchant name -> slug
    aliases: dict = field(default_factory=dict)        # token/phrase -> slug
    # What an MCC MEANS IN THIS CATALOGUE, and which merchants a row would hit.
    #
    # An exclusion row typed 'mcc' used to carry no category at all, so the
    # guardrail below it — "an exclusion may only be activated on a card that does
    # not EARN in that family" — received an empty family and returned False
    # without reading a single reward rule. Two rows typed mcc:5816 shipped that
    # way and zeroed Steam, PlayStation Store and Xbox on both Tata Neu cards,
    # for a sentence about Online Skill-Based Gaming.
    #
    # These three indexes are built from seed/merchants.json, which is the same
    # table recommendation_engine.dart:484-495 matches against, so what they say a
    # row hits is what the app will actually do.
    mcc_category: dict = field(default_factory=dict)   # mcc code -> frozenset(slugs)
    merchants_by_mcc: dict = field(default_factory=dict)       # mcc  -> frozenset(ids)
    merchants_by_category: dict = field(default_factory=dict)  # slug -> frozenset(ids)

    # -- resolution -------------------------------------------------------
    def resolve(self, *texts: object) -> tuple:
        """Every app category slug the given text(s) name, sorted and deduped.

        Returns () when nothing resolves. That is a real answer — the caller must
        treat it as "we do not know which row this is about" and place nothing.
        """
        found = []
        for text in texts:
            for phrase in _phrases(tokens(text)):
                slug = self.aliases.get(phrase)
                if slug and slug not in found:
                    found.append(slug)
        return tuple(sorted(found))

    def categories_of_mcc(self, code: object) -> frozenset:
        """Every app category the merchants carrying this MCC belong to.

        Empty means "no merchant in this catalogue carries that MCC", which is a
        real answer and not the same as "no categories": the caller must treat it
        as "we cannot see what this row would hit".
        """
        return self.mcc_category.get(str(code or "").strip(), frozenset())

    def merchants_hit(self, etype: object, evalue: object) -> frozenset:
        """Exactly the merchants recommendation_engine.dart:484-495 would exclude.

        The engine compares merchant.mccPrimary and merchant.categoryName to the
        row's value with ==, no family walk and no aliasing, so this does the
        same. Any other type ('other', 'txn_type') is inert in the app and hits
        nothing, which is what the empty set here means.
        """
        v = str(evalue or "").strip()
        t = str(etype or "").strip().lower()
        if t == "mcc":
            return self.merchants_by_mcc.get(v, frozenset())
        if t == "category":
            return self.merchants_by_category.get(v, frozenset())
        return frozenset()

    def category_of_merchant(self, name: object) -> str:
        """The app category a merchant_ref belongs to, or ''."""
        if not isinstance(name, str):
            return ""
        return self.merchant_category.get(_key(name), "")

    # -- the tree ---------------------------------------------------------
    def ancestors(self, slug: str) -> frozenset:
        out: set = set()
        stack = list(self.parents.get(slug, ()))
        while stack:
            s = stack.pop()
            if s in out:
                continue
            out.add(s)
            stack.extend(self.parents.get(s, ()))
        return frozenset(out)

    def descendants(self, slug: str) -> frozenset:
        out: set = set()
        stack = list(self.children.get(slug, ()))
        while stack:
            s = stack.pop()
            if s in out:
                continue
            out.add(s)
            stack.extend(self.children.get(s, ()))
        return frozenset(out)

    def family(self, *slugs: str) -> frozenset:
        """The slugs, everything above them and everything below them.

        This is the set the exclusion guardrail asks about. Excluding 'travel'
        stops a card earning on 'hotels' (a descendant), and excluding 'hotels'
        makes a 'travel' bonus (an ancestor) partly dead — both directions matter,
        so both are here.
        """
        out: set = set()
        for s in slugs:
            if not s:
                continue
            out.add(s)
            out |= self.ancestors(s)
            out |= self.descendants(s)
        return frozenset(out)


def _key(name: object) -> str:
    """A merchant or category name reduced to comparable letters and digits."""
    if not isinstance(name, str):
        return ""
    return _NON_ALNUM.sub("", name.lower())


def _read(path) -> object:
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build_taxonomy(merchants: object, app_categories: object) -> Taxonomy:
    """Assemble a Taxonomy from the two documents' parsed contents.

    Both arguments are tolerated as None or as the wrong shape: a missing file
    must degrade into "resolves nothing", never into a traceback in the middle of
    a weekly run, and never into a silently smaller family for the guardrail.
    """
    slugs: set = set()
    parents: dict = {}
    children: dict = {}
    merchant_category: dict = {}
    aliases: dict = {}
    mcc_category: dict = {}
    merchants_by_mcc: dict = {}
    merchants_by_category: dict = {}

    def edge(child: object, parent: object) -> None:
        if not isinstance(child, str) or not child:
            return
        slugs.add(child)
        if isinstance(parent, str) and parent:
            slugs.add(parent)
            parents.setdefault(child, set()).add(parent)
            children.setdefault(parent, set()).add(child)

    def alias_words(text: object, slug: str) -> None:
        for word in _NON_ALNUM.sub(" ", str(text or "").lower()).split():
            alias(word, slug)

    def alias(word: object, slug: str) -> None:
        w = _NON_ALNUM.sub("_", str(word).lower()).strip("_")
        if not w or w in _STOPWORDS or w in _DISPLAY_WORD_BLOCKLIST:
            return
        # First writer wins, so a slug always beats a display word, and a display
        # word always beats a merchant name. Ambiguity resolves the same way on
        # every run rather than by dict ordering.
        aliases.setdefault(w, slug)

    # --- seed/merchants.json: slug ids, slug parents ---------------------
    if isinstance(merchants, dict):
        for row in merchants.get("categories") or []:
            if isinstance(row, dict):
                edge(row.get("id"), row.get("parent_id"))
        for row in merchants.get("merchants") or []:
            if not isinstance(row, dict):
                continue
            cat = row.get("category_id")
            mid = row.get("merchant_name")
            mcc = row.get("mcc_primary")
            if isinstance(cat, str) and cat:
                slugs.add(cat)
                for name_key in ("merchant_name", "display_name"):
                    k = _key(row.get(name_key))
                    if k:
                        merchant_category.setdefault(k, cat)
                if isinstance(mid, str) and mid:
                    merchants_by_category.setdefault(cat, set()).add(mid)
            if isinstance(mcc, str) and mcc:
                if isinstance(mid, str) and mid:
                    merchants_by_mcc.setdefault(mcc, set()).add(mid)
                if isinstance(cat, str) and cat:
                    mcc_category.setdefault(mcc, set()).add(cat)

    # --- the app's categories.json: integer ids, integer parents ---------
    if isinstance(app_categories, dict):
        rows = app_categories.get("categories")
        rows = rows if isinstance(rows, list) else []
        by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
        for r in rows:
            if not isinstance(r, dict):
                continue
            edge(r.get("category_name"),
                 by_id.get(r.get("parent_id"), {}).get("category_name"))

    # --- the alias index -------------------------------------------------
    # Order matters and is the priority order: the slug itself, then the hand
    # table, then display words, then merchant names.
    for slug in sorted(slugs):
        alias(slug, slug)
    for word, slug in sorted(_PROSE_ALIASES.items()):
        if slug in slugs:
            alias(word, slug)
    if isinstance(app_categories, dict):
        rows = app_categories.get("categories")
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and r.get("category_name") in slugs:
                alias_words(r.get("display_name"), r["category_name"])
    if isinstance(merchants, dict):
        for row in merchants.get("categories") or []:
            if isinstance(row, dict) and row.get("id") in slugs:
                alias_words(row.get("display_name"), row["id"])
        for row in merchants.get("merchants") or []:
            if not isinstance(row, dict):
                continue
            cat = row.get("category_id")
            if isinstance(cat, str) and cat in slugs:
                for name_key in ("merchant_name", "display_name"):
                    alias(row.get(name_key), cat)

    return Taxonomy(
        slugs=frozenset(slugs),
        parents={k: frozenset(v) for k, v in parents.items()},
        children={k: frozenset(v) for k, v in children.items()},
        merchant_category=merchant_category,
        aliases=aliases,
        mcc_category={k: frozenset(v) for k, v in mcc_category.items()},
        merchants_by_mcc={k: frozenset(v) for k, v in merchants_by_mcc.items()},
        merchants_by_category={k: frozenset(v)
                               for k, v in merchants_by_category.items()},
    )


_DEFAULT: list = []


def default_taxonomy() -> Taxonomy:
    """The taxonomy built from this checkout's own files, built once per process."""
    if not _DEFAULT:
        _DEFAULT.append(build_taxonomy(_read(C.MERCHANTS_JSON), _read(APP_CATEGORIES)))
    return _DEFAULT[0]


def reset_default() -> None:
    """Drop the cache. For tests that point the loader at fixture files."""
    _DEFAULT.clear()
