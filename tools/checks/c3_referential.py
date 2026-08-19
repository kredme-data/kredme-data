"""L3 — referential integrity.

Every reference in the shipped data must resolve to something that actually
exists in what ships. A dangling reference never raises an exception in the
Dart app: it silently becomes "Other", or the rule simply never fires, so the
user sees a lower reward than the data promises and nothing is logged.

Scope of this layer:
  * reward_rules.category_id   -> the APP's assets/data/categories/categories.json
  * reward_rules.merchant_ref  -> merchants.json slugs (and the app's bundled copy)
  * a rule setting BOTH merchant_ref and category_id (the engine flags this)
  * category_ref set without category_id (text-only, never resolved)
  * exclusion_rules value for exclusion_type == 'category'
  * merchants.json category_id -> the app's categories
  * news feed affected card ids -> card ids
  * card.image_asset           -> a real file in the app checkout
  * manifest.json declared files -> files on disk, checksum and size
  * duplicate card id / image_asset / normalised card_name

Nothing here is grandfathered and nothing is suppressed. The runner owns policy.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path

from .base import (Ctx, Finding, Skipped, ERROR, WARN, INFO, num, trunc, iso_ok,
                   card_base_pct)

LAYER = "L3 referential integrity"

# Tokens that carry no product identity. Used only for duplicate-name detection,
# never for merging issuers — see the AU Bank / Axis Bank trap in MEMORY.
_NAME_FILLER = {"credit", "card", "cards", "bank", "the"}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Characters that break an id where it is USED as a reference — inside a file
# path, a URL, or a Firestore document id. Deliberately narrow: brackets and
# dots are ugly but harmless, and flagging them would bury the one that matters.
_ID_UNSAFE = re.compile(r"[/\\#?\s]")

# Keys that have been used, in this repo or the app's bundle, to hold a
# merchant's slug. This used to widen a base helper that was missing
# `merchant_name` — the one key merchants.json actually ships — which meant
# Ctx.merchant_slugs() returned an EMPTY set for all 273 rows and every caller
# that trusted it (c5's merchant branch) was unreachable code. The gap is closed
# in checks/base.py, so there is now one definition of "the merchants we ship"
# and this list only records the historical spellings for the row-level scan
# below.
_MERCHANT_SLUG_KEYS = ("merchant_name", "slug", "merchant_slug", "merchant_ref", "id")

# Both spellings have been used for the news feed's card list.
_NEWS_CARD_KEYS = ("affected_card_ids", "affected_cards")


# --------------------------------------------------------------------------- #
# small local helpers
# --------------------------------------------------------------------------- #
def _s(v):
    """The value as a non-empty stripped string, else None."""
    return v.strip() if isinstance(v, str) and v.strip() else None


def _rows(v):
    """A list of rows from a list, or from the usual {"<name>": [...]} wrapper."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        for k in ("merchants", "items", "cards", "categories", "news"):
            if isinstance(v.get(k), list):
                return v[k]
    return []


def _norm_card_name(name) -> str:
    """Lowercase, drop punctuation and filler words, keep word order.

    Deliberately NOT a similarity score. Card names in this file differ by a
    single meaningful token far more often than they are genuine duplicates
    ('AU Altura' vs 'AU Altura Plus'), so anything fuzzier produces almost
    all false positives.
    """
    s = _NON_ALNUM.sub(" ", (name or "").lower())
    return " ".join(t for t in s.split() if t not in _NAME_FILLER)


def _merchant_slugs(ctx: Ctx) -> set:
    """The shared helper, plus the legacy spellings this layer alone tolerates.

    The workaround this used to be — re-deriving the whole set because the base
    helper was broken — is gone. ctx.merchant_slugs() is now correct and is the
    single source of truth; this only adds the historical key names.
    """
    out = set(ctx.merchant_slugs() or set())
    for r in _rows(ctx.merchants):
        if not isinstance(r, dict):
            continue
        for k in _MERCHANT_SLUG_KEYS:
            s = _s(r.get(k))
            if s:
                out.add(s)
    return out


def _category_names(rows) -> set:
    out = set()
    for c in rows or []:
        if isinstance(c, dict):
            for k in ("category_name", "id", "slug"):
                s = _s(c.get(k))
                if s:
                    out.add(s)
    return out


def _read_json(path: Path):
    import json
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _app_file(ctx: Ctx, rel: str):
    root = ctx.app_root
    if not root:
        return None
    try:
        p = Path(root) / rel
        return p if p.exists() else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 1. reward_rules -> categories / merchants
# --------------------------------------------------------------------------- #
def _check_reward_refs(ctx: Ctx, out: list) -> None:
    app_cats = ctx.app_category_names()
    slugs = _merchant_slugs(ctx)

    if not app_cats:
        out.append(Skipped(
            code="L3.CATEGORY_REFERENCES",
            what="No reward rule's category_id was checked against the categories the "
                 "app actually recognises, and no 'category' exclusion was either.",
            reason="This run has no category vocabulary: there was no app checkout and "
                   "the vendored mirror at tools/app_mirror/categories.json could not be "
                   "read either.",
            impact="read the absence of category findings as evidence that every "
                   "category reference in this file is valid. Not one of them was looked "
                   "at.",
            codes=("L3.RULE_CATEGORY_UNKNOWN", "L3.CATEGORY_TEXT_ONLY",
                   "L3.EXCLUSION_CATEGORY_UNKNOWN", "L3.MERCHANT_CATEGORY_UNKNOWN",
                   "L3.MERCHANT_CATEGORY_UNSET", "L3.BUNDLED_MERCHANT_CATEGORY_BROKEN"),
            restore="Restore tools/app_mirror/categories.json (python3 "
                    "tools/app_mirror/refresh.py --app-root ../KredMe-main), or pass "
                    "--app-root pointing at a KredMe-main checkout.",
        ))
    if not slugs:
        out.append(Finding(
            severity=INFO, code="L3.MERCHANT_LIST_UNAVAILABLE",
            message="No merchant slugs could be read from merchants.json, so no reward "
                    "rule's merchant reference was checked.",
            impact="A rule pointing at a merchant that does not exist never fires, "
                   "and this run cannot see that.",
            fix="Check that seed/merchants.json parses and still carries merchant_name "
                "on every row.",
        ))

    bad_cat = Counter()      # (card_id, value) -> n rules
    bad_cat_names = {}       # (card_id, value) -> a sample rule name
    bad_mer = Counter()
    bad_mer_names = {}
    both = Counter()         # card_id -> n rules
    both_names = defaultdict(list)
    text_only = Counter()    # card_id -> n rules
    text_only_vals = defaultdict(list)

    for cid, _inner, j, r in ctx.rules("reward_rules"):
        try:
            if not isinstance(r, dict):
                continue          # row shape is L1's problem, not this layer's
            key = cid or "(card with no id)"
            cat = _s(r.get("category_id"))
            mer = _s(r.get("merchant_ref"))
            ref = _s(r.get("category_ref"))
            name = _s(r.get("rule_name")) or "(unnamed rule #%d)" % j

            if cat and app_cats and cat not in app_cats:
                bad_cat[(key, cat)] += 1
                bad_cat_names.setdefault((key, cat), name)
            if mer and slugs and mer not in slugs:
                bad_mer[(key, mer)] += 1
                bad_mer_names.setdefault((key, mer), name)
            if cat and mer:
                both[key] += 1
                if len(both_names[key]) < 3:
                    both_names[key].append(name)
            if ref and not cat:
                text_only[key] += 1
                if len(text_only_vals[key]) < 3:
                    text_only_vals[key].append(ref)
        except Exception as exc:                      # never let one row kill the layer
            out.append(Finding(
                severity=WARN, code="L3.ROW_UNREADABLE",
                message="A reward rule could not be inspected for broken references.",
                card_id=cid, block="reward_rules", index=j,
                evidence=trunc("%s: %s" % (type(exc).__name__, exc)),
                impact="This rule's category and merchant links are unverified.",
                fix="Look at this row by hand.",
            ))

    for (cid, val), n in sorted(bad_cat.items()):
        out.append(Finding(
            severity=ERROR, code="L3.RULE_CATEGORY_UNKNOWN",
            message="%d reward rule(s) on this card point at the spending category "
                    "'%s', which the app does not have." % (n, val),
            card_id=cid, block="reward_rules", field="category_id", evidence=trunc(val),
            impact="The app quietly files these rules under 'Other', so the bonus "
                   "never fires and the user is shown a lower reward than we promise.",
            fix="Use one of the category names in the app's "
                "assets/data/categories/categories.json, or add the new category to the "
                "app first (that needs an app release — the category list is bundled, "
                "not fetched). Sample rule: %s" % bad_cat_names.get((cid, val), "?"),
        ))

    for (cid, val), n in sorted(bad_mer.items()):
        out.append(Finding(
            severity=ERROR, code="L3.RULE_MERCHANT_UNKNOWN",
            message="%d reward rule(s) on this card point at the merchant '%s', which "
                    "is not in merchants.json." % (n, val),
            card_id=cid, block="reward_rules", field="merchant_ref", evidence=trunc(val),
            impact="The app can never match a purchase to this merchant, so the rule "
                   "never fires and the card looks worse than it is.",
            fix="Add the merchant to seed/merchants.json, or correct the spelling to an "
                "existing merchant_name. Sample rule: %s" % bad_mer_names.get((cid, val), "?"),
        ))

    for cid, n in sorted(both.items()):
        out.append(Finding(
            severity=ERROR, code="L3.RULE_MERCHANT_AND_CATEGORY",
            message="%d reward rule(s) on this card name both a merchant and a spending "
                    "category. The app treats that as a mistake." % n,
            card_id=cid, block="reward_rules", field="merchant_ref+category_id",
            evidence=trunc("; ".join(both_names.get(cid, []))),
            impact="The merchant wins and the category half is thrown away, so the rule "
                   "fires in fewer places than the data claims.",
            fix="Split it into two rules, or drop whichever of merchant_ref / "
                "category_id is not really part of the offer.",
        ))

    for cid, n in sorted(text_only.items()):
        out.append(Finding(
            severity=INFO, code="L3.CATEGORY_TEXT_ONLY",
            message="%d reward rule(s) on this card describe a category in words but "
                    "carry no machine-readable category, so they can never fire on a "
                    "category." % n,
            card_id=cid, block="reward_rules", field="category_ref",
            evidence=trunc("; ".join(text_only_vals.get(cid, []))),
            impact="The offer is visible on the card's detail page but the "
                   "recommendation screen will never pick this card for that spend.",
            fix="Map the wording to a category_id from the app's categories.json. If no "
                "app category matches, that is a product gap, not a data typo.",
        ))


# --------------------------------------------------------------------------- #
# 1b. our vendored copy of the app's categories -> the app's own file
# --------------------------------------------------------------------------- #
def _check_category_mirror(ctx: Ctx, out: list) -> None:
    """Has the vendored category mirror drifted from the app?

    This is the check that pays for the mirror existing. kredme-data is PUBLIC
    and KredMe-main is PRIVATE, so CI here can never see the app — which means
    CI can never notice that the app has added, removed or renamed a spending
    category. Every run that DOES have a checkout is therefore the only chance
    anyone gets to catch it, and the cost of missing it is silent: a renamed
    slug makes the mirror keep answering "yes, that category exists" long after
    the app stopped recognising it, so every category rule pointing at it passes
    validation here and quietly stops firing on the phone.

    Only fires when both copies were readable. It compares id, slug and
    parent_id — the three fields the engine actually uses — so a display_name
    edit does not cry wolf while a renamed slug does.
    """
    drift = ctx.categories_drift
    if drift is None:
        # Only one of the two was readable, so there is nothing to compare. When
        # the mirror is the ONE that is missing, that is a real gap in this
        # repo's own tree rather than a property of the run, so say so.
        if ctx.categories_origin == "app":
            out.append(Finding(
                severity=WARN, code="L3.APP_CATEGORY_MIRROR_MISSING",
                message="This run read the app's categories directly, but the vendored "
                        "copy at tools/app_mirror/categories.json is missing or "
                        "unreadable — so the same run in CI, which has no app checkout, "
                        "will be blind to categories entirely.",
                impact="CI cannot check a single category reference, and a validator "
                       "with no category vocabulary does not merely check less: it "
                       "reported 1,021 errors against a file that has 712.",
                fix="python3 tools/app_mirror/refresh.py --app-root <KredMe-main>",
            ))
        return
    if not drift:
        out.append(Finding(
            severity=INFO, code="L3.APP_CATEGORY_MIRROR_CURRENT",
            message="The vendored category mirror matches the app checkout exactly on "
                    "every id, slug and parent.",
            evidence=trunc("%d categories" % len(ctx.app_category_names())),
            impact="CI, which cannot check the app out, is asking the same question of "
                   "the same vocabulary a developer is.",
            fix="Nothing to do.",
        ))
        return

    # Severity follows the blast radius, not the line count. Drift on a slug no
    # rule in this file mentions costs a user nothing today — the app grew a
    # category we do not use yet. Drift on a slug our data DOES point at is a
    # wrong number on a phone: the rule passes validation here against a
    # vocabulary the app no longer has, and silently stops firing there.
    used = _referenced_categories(ctx)
    hot = [d for d in drift if d.get("slug") in used]
    cold = [d for d in drift if d.get("slug") not in used]

    if hot:
        out.append(Finding(
            severity=ERROR, code="L3.APP_CATEGORY_MIRROR_DRIFT",
            message="The app's category list and our vendored copy disagree on %d "
                    "categor%s this file actually points at. Every CI run is validating "
                    "those rules against the stale one."
                    % (len(hot), "y" if len(hot) == 1 else "ies"),
            block="app_mirror", field="categories",
            evidence=trunc("; ".join(d["text"] for d in hot), 400),
            impact="A slug the app has dropped or renamed still looks valid to CI, so a "
                   "category rule that will never fire on a phone passes the gate here. "
                   "The user is shown a card ranked as if a bonus it no longer earns "
                   "still applies. This is the one defect CI is structurally incapable "
                   "of finding on its own, which is exactly why it must fail loudly on "
                   "a machine that can.",
            fix="python3 tools/app_mirror/refresh.py --app-root <KredMe-main>, then "
                "commit tools/app_mirror/ on its own so the vocabulary change is one "
                "reviewable line of history. Then check every rule pointing at a "
                "renamed slug — the refresh silences the alarm, it does not fix the "
                "rules.",
        ))
    if cold:
        out.append(Finding(
            severity=WARN, code="L3.APP_CATEGORY_MIRROR_STALE",
            message="The vendored category mirror is %d categor%s behind the app, none "
                    "of which this file points at yet."
                    % (len(cold), "y" if len(cold) == 1 else "ies"),
            block="app_mirror", field="categories",
            evidence=trunc("; ".join(d["text"] for d in cold), 400),
            impact="Nothing a user can see today. It becomes a wrong number the moment "
                   "any rule starts using one of these slugs, and CI cannot tell.",
            fix="python3 tools/app_mirror/refresh.py --app-root <KredMe-main>",
        ))


def _referenced_categories(ctx: Ctx) -> set:
    """Every category slug this data file actually points at."""
    used = set()
    for _cid, _inner, _j, r in ctx.rules("reward_rules"):
        if isinstance(r, dict) and isinstance(r.get("category_id"), str):
            used.add(r["category_id"])
    for _cid, _inner, _j, r in ctx.rules("exclusion_rules"):
        if isinstance(r, dict) and _s(r.get("exclusion_type")) == "category" \
                and isinstance(r.get("exclusion_value"), str):
            used.add(r["exclusion_value"])
    for r in _rows(ctx.merchants):
        if isinstance(r, dict) and isinstance(r.get("category_id"), str):
            used.add(r["category_id"])
    return used


# --------------------------------------------------------------------------- #
# 2. reward_rules -> the merchant list bundled inside the APK
# --------------------------------------------------------------------------- #
def _check_bundled_merchants(ctx: Ctx, out: list) -> None:
    """A fresh install uses the merchant list baked into the app until its first
    sync. A merchant that exists here but not there is dead on day one."""
    p = _app_file(ctx, "assets/data/merchants/merchants.json")
    if p is None:
        # This used to `return` with no word at all — "silent by design". It was
        # not a good design: on a run without the app checkout it deleted 18
        # real WARN findings and left nothing to say they had ever existed, so a
        # CI run looked strictly cleaner than a developer's run of the same
        # bytes. Unlike the category vocabulary this one cannot be mirrored into
        # a public repo — it is a private app asset — so the honest move is to
        # declare the gap.
        out.append(Skipped(
            code="L3.BUNDLED_MERCHANT_LIST",
            what="No reward rule's merchant_ref was checked against the merchant list "
                 "baked into the APK, which is the list a brand-new install uses until "
                 "its first sync.",
            reason="No app checkout was available, and this asset lives in the PRIVATE "
                   "app repo, so it cannot be mirrored into this public one.",
            impact="conclude that every merchant a rule names is present on a fresh "
                   "install. That comparison did not happen.",
            codes=("L3.RULE_MERCHANT_NOT_BUNDLED", "L3.BUNDLED_MERCHANTS_UNREADABLE",
                   "L3.BUNDLED_MERCHANT_CATEGORY_BROKEN"),
            restore="Run with --app-root pointing at a KredMe-main checkout.",
        ))
        return
    data = _read_json(p)
    if data is None:
        out.append(Finding(
            severity=INFO, code="L3.BUNDLED_MERCHANTS_UNREADABLE",
            message="The merchant list bundled inside the app could not be read, so we "
                    "could not check what a brand-new install sees before its first sync.",
            evidence=trunc(str(p)),
            fix="Check that the app checkout is complete.",
        ))
        return

    bundled = set()
    for r in _rows(data):
        if isinstance(r, dict):
            for k in _MERCHANT_SLUG_KEYS:
                s = _s(r.get(k))
                if s:
                    bundled.add(s)
    if not bundled:
        return

    missing = Counter()
    vals = defaultdict(set)
    for cid, _inner, _j, r in ctx.rules("reward_rules"):
        try:
            if not isinstance(r, dict):
                continue
            mer = _s(r.get("merchant_ref"))
            if mer and mer not in bundled:
                key = cid or "(card with no id)"
                missing[key] += 1
                vals[key].add(mer)
        except Exception:
            continue

    for cid, n in sorted(missing.items()):
        out.append(Finding(
            severity=WARN, code="L3.RULE_MERCHANT_NOT_BUNDLED",
            message="%d reward rule(s) on this card point at merchant(s) the app does "
                    "not carry in its built-in list: %s."
                    % (n, ", ".join(sorted(vals[cid]))),
            card_id=cid, block="reward_rules", field="merchant_ref",
            evidence=trunc(", ".join(sorted(vals[cid]))),
            impact="A user who has just installed the app, before the first data sync "
                   "finishes, will not get this reward suggested at all.",
            fix="Ask Kartik to refresh assets/data/merchants/merchants.json in the app "
                "from this repo's seed/merchants.json in the next release.",
        ))


# --------------------------------------------------------------------------- #
# 3. exclusion_rules value for exclusion_type == 'category'
# --------------------------------------------------------------------------- #
def _check_exclusion_category_refs(ctx: Ctx, out: list) -> None:
    app_cats = ctx.app_category_names()
    if not app_cats:
        return                                    # already reported once above

    bad = Counter()
    for cid, _inner, _j, r in ctx.rules("exclusion_rules"):
        try:
            if not isinstance(r, dict):
                continue
            if _s(r.get("exclusion_type")) != "category":
                continue
            val = _s(r.get("exclusion_value"))
            if val and val not in app_cats:
                bad[(cid or "(card with no id)", val)] += 1
        except Exception:
            continue

    for (cid, val), n in sorted(bad.items()):
        out.append(Finding(
            severity=ERROR, code="L3.EXCLUSION_CATEGORY_UNKNOWN",
            message="%d exclusion(s) on this card exclude the category '%s', which the "
                    "app does not have." % (n, val),
            card_id=cid, block="exclusion_rules", field="exclusion_value",
            evidence=trunc(val),
            impact="The exclusion never matches anything, so we tell the user this card "
                   "earns on spend the issuer actually excludes.",
            fix="Use a category name from the app's categories.json, or re-type this "
                "exclusion as an MCC exclusion.",
        ))


# --------------------------------------------------------------------------- #
# 4. merchants.json -> the app's categories
# --------------------------------------------------------------------------- #
def _check_merchant_categories(ctx: Ctx, out: list) -> None:
    app_cats = ctx.app_category_names()
    if not app_cats:
        return

    rows = _rows(ctx.merchants)
    if not rows:
        return

    unknown = defaultdict(list)     # bad value -> merchant names
    unset = []
    for r in rows:
        try:
            if not isinstance(r, dict):
                continue
            name = _s(r.get("merchant_name")) or _s(r.get("display_name")) or "(unnamed)"
            cat = r.get("category_id")
            s = _s(cat)
            if s is None:
                unset.append(name)
            elif s not in app_cats:
                unknown[s].append(name)
        except Exception:
            continue

    for val, names in sorted(unknown.items()):
        out.append(Finding(
            severity=ERROR, code="L3.MERCHANT_CATEGORY_UNKNOWN",
            message="%d merchant(s) are filed under the category '%s', which the app "
                    "does not have: %s." % (len(names), val, ", ".join(sorted(names)[:8])),
            block="merchants", field="category_id", evidence=trunc(val),
            impact="Every category-based reward rule is skipped for these merchants, so "
                   "spending there is scored on the card's plain base rate only.",
            fix="Re-file the merchant under a category name that exists in the app's "
                "assets/data/categories/categories.json, or add the category to the app "
                "(that needs an app release).",
        ))

    if unset:
        out.append(Finding(
            severity=WARN, code="L3.MERCHANT_CATEGORY_UNSET",
            message="%d merchant(s) in merchants.json have no spending category at all: %s."
                    % (len(unset), ", ".join(sorted(unset)[:10])),
            block="merchants", field="category_id",
            evidence=trunc(", ".join(sorted(unset))),
            impact="Category bonuses are skipped entirely at these merchants, so a card "
                   "with a 5%% dining bonus is scored as if it had none.",
            fix="Give each of these merchants a category_id that exists in the app's "
                "categories.json.",
        ))

    # Same check against the copy shipped inside the app, which a fresh install uses.
    # The skip for this is declared once, by _check_bundled_merchants above, so
    # the same missing asset is not reported as two separate gaps.
    p = _app_file(ctx, "assets/data/merchants/merchants.json")
    if p is None:
        return
    data = _read_json(p)
    if data is None:
        return
    stale = []
    for r in _rows(data):
        if not isinstance(r, dict):
            continue
        name = _s(r.get("merchant_name")) or _s(r.get("display_name")) or "(unnamed)"
        s = _s(r.get("category_id"))
        if s is None or s not in app_cats:
            stale.append(name)
    if stale:
        out.append(Finding(
            severity=WARN, code="L3.BUNDLED_MERCHANT_CATEGORY_BROKEN",
            message="%d of the merchants baked into the app have a spending category the "
                    "app itself does not recognise." % len(stale),
            block="merchants", field="category_id",
            evidence=trunc(", ".join(sorted(stale))),
            impact="On a phone that has just installed the app and not yet synced, "
                   "category bonuses are skipped at roughly a third of all merchants.",
            fix="Ask Kartik to re-bundle assets/data/merchants/merchants.json from this "
                "repo's seed/merchants.json in the next release. Fixing it here alone "
                "does not fix the first-run experience.",
        ))


# --------------------------------------------------------------------------- #
# 5. news feed -> card ids
# --------------------------------------------------------------------------- #
def _check_news_refs(ctx: Ctx, out: list) -> None:
    if ctx.news is None:
        out.append(Finding(
            severity=INFO, code="L3.NEWS_UNAVAILABLE",
            message="No news feed was loaded, so the cards named in news alerts were "
                    "not checked.",
            fix="Point the validator at news/feed.json.",
        ))
        return

    items = _rows(ctx.news)
    if not items:
        if not (isinstance(ctx.news, dict) and isinstance(ctx.news.get("items"), list)) \
                and not isinstance(ctx.news, list):
            out.append(Finding(
                severity=ERROR, code="L3.NEWS_FEED_SHAPE_WRONG",
                message="The news feed is not a list of alerts, so no alert can reach a "
                        "card and none of its card links were checked.",
                block="news", evidence=trunc(ctx.news),
                impact="Nobody receives any card news at all.",
                fix="Make news/feed.json either a list of items or an object with an "
                    "'items' list.",
            ))
        return

    known = set()
    for _i, _e, inner, cid in ctx.entries():
        s = _s(cid)
        if s:
            known.add(s)
    if not known:
        return

    for n, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                out.append(Finding(
                    severity=WARN, code="L3.NEWS_ITEM_NOT_OBJECT",
                    message="A news feed entry is not a proper record, so the cards it "
                            "affects could not be checked.",
                    block="news", index=n, evidence=trunc(item),
                    impact="Whatever this entry was meant to say may not reach anyone.",
                    fix="Look at this entry in news/feed.json by hand.",
                ))
                continue
            raw = None
            used_key = None
            for k in _NEWS_CARD_KEYS:
                if k in item:
                    raw = item.get(k)
                    used_key = k
                    break
            if raw is None:
                continue
            if not isinstance(raw, list):
                out.append(Finding(
                    severity=ERROR, code="L3.NEWS_CARDS_NOT_A_LIST",
                    message="News item '%s' lists the cards it affects in the wrong "
                            "shape, so the app cannot attach it to any card."
                            % (_s(item.get("id")) or "#%d" % n),
                    block="news", index=n, field=used_key, evidence=trunc(raw),
                    impact="The alert never shows up on the card it is about.",
                    fix="Make %s a list of card ids." % used_key,
                ))
                continue
            missing = [v for v in raw if not (isinstance(v, str) and v in known)]
            if missing:
                out.append(Finding(
                    severity=ERROR, code="L3.NEWS_CARD_UNKNOWN",
                    message="News item '%s' says it affects %d card(s) that do not exist "
                            "in cards.json: %s."
                            % (_s(item.get("id")) or "#%d" % n, len(missing),
                               ", ".join(trunc(str(v), 40) for v in missing[:6])),
                    block="news", index=n, field=used_key,
                    evidence=trunc(missing),
                    impact="The alert never appears on those cards, so a user holding "
                           "an affected card is not told about a change to it.",
                    fix="Correct the card ids to match the 'id' field in seed/cards.json.",
                ))
        except Exception as exc:
            out.append(Finding(
                severity=WARN, code="L3.NEWS_ITEM_UNREADABLE",
                message="A news feed entry could not be checked for broken card links.",
                block="news", index=n,
                evidence=trunc("%s: %s" % (type(exc).__name__, exc)),
                fix="Look at this entry in news/feed.json by hand.",
            ))


# --------------------------------------------------------------------------- #
# 6. card.image_asset -> a real file in the app checkout
# --------------------------------------------------------------------------- #
def _check_card_images(ctx: Ctx, out: list) -> None:
    root = ctx.app_root
    if not root or not Path(root).exists():
        # This one already knew it had not run — it said so in an INFO note. But
        # an INFO note is finding number 1,300-and-something in a list of 1,424,
        # counted in the same column as observations about the data and read by
        # nobody. Same sentence, promoted to a first-class skip that the
        # scorecard has to count and the verdict has to mention.
        out.append(Skipped(
            code="L3.CARD_IMAGE_FILES",
            what="No card's image_asset was confirmed to point at a file that exists.",
            reason="No app checkout was available, and card artwork is binary asset "
                   "data in the PRIVATE app repo — it cannot be mirrored into this "
                   "public one.",
            impact="assume every card has a picture. A card whose image is missing "
                   "shows an empty tile in the picker, and this run cannot see that.",
            codes=("L3.CARD_IMAGE_MISSING", "L3.CARD_IMAGE_DIR_MISSING"),
            restore="Run with --app-root pointing at a KredMe-main checkout.",
        ))
        return
    root = Path(root)

    missing = []          # (card_id, rel_path)
    unset = []
    dirs_seen = {}        # relative dir -> exists?
    for _i, _e, inner, cid in ctx.entries():
        try:
            rel = _s(inner.get("image_asset"))
            key = _s(cid) or "(card with no id)"
            if rel is None:
                unset.append(key)
                continue
            if rel.startswith("/") or ".." in rel.split("/"):
                out.append(Finding(
                    severity=ERROR, code="L3.CARD_IMAGE_PATH_UNSAFE",
                    message="This card's picture path is not a plain path inside the "
                            "app's assets folder.",
                    card_id=key, block="card", field="image_asset", evidence=trunc(rel),
                    impact="The picture will not load and the card shows an empty tile.",
                    fix="Store the path as assets/cards/<file>.png.",
                ))
                continue
            p = root / rel
            d = str(Path(rel).parent)
            if d not in dirs_seen:
                dirs_seen[d] = (root / d).is_dir()
            if not p.is_file():
                missing.append((key, rel))
        except Exception:
            continue

    if unset:
        out.append(Finding(
            severity=WARN, code="L3.CARD_IMAGE_UNSET",
            message="%d card(s) have no picture file named at all." % len(unset),
            block="card", field="image_asset", evidence=trunc(", ".join(sorted(unset))),
            impact="These cards show an empty tile in the picker.",
            fix="Set image_asset on each of these cards.",
        ))

    if not missing:
        return

    # If not one of the folders the data points at exists, this is one structural
    # fact about the checkout, not 383 separate card defects.
    if dirs_seen and not any(dirs_seen.values()):
        out.append(Finding(
            severity=WARN, code="L3.CARD_IMAGE_DIR_MISSING",
            message="Not one of the folders our card pictures point at exists in the app "
                    "checkout (%s), so all %d card pictures are unresolvable here."
                    % (", ".join(sorted(dirs_seen)), len(missing)),
            block="card", field="image_asset",
            evidence=trunc("; ".join("%s -> %s" % (c, r) for c, r in missing[:5])),
            impact="Cannot be judged from this checkout alone. Note that no code in the "
                   "app reads image_asset today, so these paths may simply be a field "
                   "nothing has ever used rather than 383 broken tiles.",
            fix="Confirm against the branch that actually ships (master) whether card "
                "images are meant to be bundled. If they are not, stop shipping "
                "image_asset; if they are, add the folder to the app and to pubspec.yaml.",
        ))
        return

    for cid, rel in missing:
        out.append(Finding(
            severity=ERROR, code="L3.CARD_IMAGE_MISSING",
            message="This card's picture file is not in the app.",
            card_id=cid, block="card", field="image_asset", evidence=trunc(rel),
            impact="The card shows as an empty tile in the card picker.",
            fix="Add the image to the app at %s, or point image_asset at a file that "
                "is there." % rel,
        ))


# --------------------------------------------------------------------------- #
# 7. manifest.json -> files on disk
# --------------------------------------------------------------------------- #
def _check_manifest(ctx: Ctx, out: list) -> None:
    man = ctx.manifest
    if not isinstance(man, dict):
        out.append(Finding(
            severity=ERROR, code="L3.MANIFEST_NOT_AN_OBJECT",
            message="manifest.json is not a proper record, so nothing it declares could "
                    "be checked.",
            block="manifest", evidence=trunc(man),
            impact="The app refuses the whole sync when the manifest does not parse, so "
                   "users keep whatever data they already had.",
            fix="Regenerate seed/manifest.json.",
        ))
        return

    files = man.get("files")
    if not isinstance(files, list):
        out.append(Finding(
            severity=ERROR, code="L3.MANIFEST_NO_FILE_LIST",
            message="manifest.json does not list the data files it publishes.",
            block="manifest", field="files", evidence=trunc(files),
            impact="The app has nothing to fetch or verify, so the sync fails.",
            fix="Regenerate seed/manifest.json so it carries a 'files' list.",
        ))
        return

    strict = bool(ctx.config.get("strict_checksums")) if isinstance(ctx.config, dict) else False
    seed_dir = Path(ctx.seed_dir) if ctx.seed_dir else Path(".")
    repo_root = seed_dir.parent

    # On --target working a manifest mismatch is downgraded, because a working
    # tree is mid-edit and `kredme.py promote` regenerates the manifest anyway.
    # That is defensible — but --target working is what a human types just before
    # a publish, and it made the single most sync-breaking condition in the file
    # (the app rejects the sync with "Sync failed" and every user keeps stale
    # card data) print at the same volume as a formatting note, in the only run
    # anybody actually does.
    #
    # The state that actually breaks a publish is narrower and testable: the data
    # has been edited since the manifest was written. When the data file on disk
    # is NEWER than the manifest, this is not "mid-edit", it is "you edited the
    # data and forgot to regenerate" — WARN. Still not ERROR on a working tree,
    # because promote will fix it; loud enough to be seen before it does not.
    stale = False
    try:
        man_mtime = (seed_dir / "manifest.json").stat().st_mtime
        stale = any((seed_dir / f).stat().st_mtime > man_mtime
                    for f in ("cards.json", "merchants.json")
                    if (seed_dir / f).is_file())
    except OSError:
        stale = False
    mismatch_sev = ERROR if strict else (WARN if stale else INFO)

    declared = set()
    for n, f in enumerate(files):
        try:
            if not isinstance(f, dict):
                out.append(Finding(
                    severity=ERROR, code="L3.MANIFEST_ENTRY_NOT_OBJECT",
                    message="An entry in the manifest's file list is not a proper record.",
                    block="manifest", index=n, evidence=trunc(f),
                    impact="The app cannot verify that file, so the sync fails.",
                    fix="Regenerate seed/manifest.json.",
                ))
                continue
            name = _s(f.get("name")) or _s(f.get("path")) or "#%d" % n
            rel = _s(f.get("path")) or _s(f.get("name"))
            if not rel:
                out.append(Finding(
                    severity=ERROR, code="L3.MANIFEST_ENTRY_NO_PATH",
                    message="A manifest entry does not say which file it describes.",
                    block="manifest", index=n, evidence=trunc(f),
                    impact="The app cannot fetch it, so the sync fails.",
                    fix="Give the entry a 'path'.",
                ))
                continue

            candidates = [repo_root / rel, seed_dir / Path(rel).name]
            path = next((c for c in candidates if c.is_file()), None)
            if path is None:
                out.append(Finding(
                    severity=ERROR, code="L3.MANIFEST_FILE_MISSING",
                    message="The manifest publishes '%s' but that file is not here." % rel,
                    block="manifest", index=n, field="path", evidence=trunc(rel),
                    impact="The app asks for a file that does not exist, the sync fails, "
                           "and every user keeps stale card data.",
                    fix="Either add the file or remove the entry from seed/manifest.json.",
                ))
                continue
            declared.add(path.resolve())

            data = path.read_bytes()
            size = f.get("size_bytes")
            want = num(size)
            if want is not None and int(want) != len(data):
                out.append(Finding(
                    severity=mismatch_sev, code="L3.MANIFEST_SIZE_MISMATCH",
                    message="The manifest says '%s' is %d bytes; on disk it is %d."
                            % (name, int(want), len(data)),
                    block="manifest", index=n, field="size_bytes",
                    evidence=trunc("declared %s, actual %d" % (int(want), len(data))),
                    impact="On live data the app rejects the sync with 'Sync failed' and "
                           "every user keeps stale card data.",
                    fix="Regenerate the manifest (tools/kredme.py promote does this) "
                        "before pushing.",
                ))
            elif size is not None and want is None:
                out.append(Finding(
                    severity=mismatch_sev, code="L3.MANIFEST_SIZE_NOT_A_NUMBER",
                    message="The manifest's size for '%s' is not a number." % name,
                    block="manifest", index=n, field="size_bytes", evidence=trunc(size),
                    impact="The app cannot verify the download.",
                    fix="Regenerate the manifest.",
                ))

            checksum = _s(f.get("checksum")) or _s(f.get("sha256"))
            if checksum:
                actual = hashlib.sha256(data).hexdigest()
                if actual.lower() != checksum.lower():
                    out.append(Finding(
                        severity=mismatch_sev, code="L3.MANIFEST_CHECKSUM_MISMATCH",
                        message="The manifest's fingerprint for '%s' does not match the "
                                "file that is actually here." % name,
                        block="manifest", index=n, field="checksum",
                        evidence=trunc("declared %s…, actual %s…"
                                       % (checksum[:12], actual[:12])),
                        impact="On live data the app rejects the sync with 'Sync failed' "
                               "and every user keeps stale card data.",
                        fix="Regenerate the manifest (tools/kredme.py promote does this) "
                            "before pushing.",
                    ))
            else:
                out.append(Finding(
                    severity=mismatch_sev, code="L3.MANIFEST_NO_CHECKSUM",
                    message="The manifest publishes '%s' with no fingerprint." % name,
                    block="manifest", index=n, field="checksum",
                    impact="Nothing can tell a corrupted or truncated download from a "
                           "good one.",
                    fix="Regenerate the manifest so every file carries a checksum.",
                ))
        except Exception as exc:
            out.append(Finding(
                severity=WARN, code="L3.MANIFEST_ENTRY_UNREADABLE",
                message="A manifest entry could not be checked against the files on disk.",
                block="manifest", index=n,
                evidence=trunc("%s: %s" % (type(exc).__name__, exc)),
                fix="Look at seed/manifest.json by hand.",
            ))

    # The other direction: a data file sitting in seed/ that the manifest never
    # publishes will never reach a single user.
    try:
        undeclared = []
        for p in sorted(seed_dir.glob("*.json")):
            if p.name == "manifest.json":
                continue
            if p.resolve() not in declared:
                undeclared.append(p.name)
        if undeclared:
            out.append(Finding(
                severity=INFO, code="L3.MANIFEST_UNDECLARED_FILE",
                message="%d data file(s) sit in seed/ but the manifest never publishes "
                        "them: %s." % (len(undeclared), ", ".join(undeclared)),
                block="manifest", evidence=trunc(", ".join(undeclared)),
                impact="Whatever is in those files never reaches a single user.",
                fix="Add them to seed/manifest.json, or delete them if they are dead.",
            ))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 8. duplicates
# --------------------------------------------------------------------------- #
def _check_duplicates(ctx: Ctx, out: list) -> None:
    by_id = defaultdict(list)
    by_img = defaultdict(list)
    by_name = defaultdict(list)

    for i, _e, inner, cid in ctx.entries():
        try:
            key = _s(cid)
            if key:
                by_id[key].append(i)
                m = _ID_UNSAFE.search(key)
                if m:
                    out.append(Finding(
                        severity=ERROR, code="L3.CARD_ID_UNSAFE_CHARACTER",
                        message="This card's id contains %r, which is a separator in a "
                                "file path, a web address and a Firebase record id."
                                % m.group(0),
                        card_id=key, block="card", index=i, field="id",
                        evidence=trunc(key),
                        impact="Anything that builds a path or a link from the id breaks "
                               "— the card's picture path turns into a folder that will "
                               "never exist, and the card cannot be stored under this id "
                               "in Firebase.",
                        fix="Rename the id to letters, digits, underscores and hyphens "
                            "only, then update every reference to it (news alerts, and "
                            "any saved-card records already in the wild).",
                    ))
            else:
                out.append(Finding(
                    severity=ERROR, code="L3.CARD_ID_MISSING",
                    message="A card has no id, so nothing — a news alert, a saved card, "
                            "a reward rule — can refer to it.",
                    block="card", index=i, field="id", evidence=trunc(inner.get("id")),
                    impact="The user cannot reliably keep this card in their wallet.",
                    fix="Give the card a unique id.",
                ))
            img = _s(inner.get("image_asset"))
            if img:
                by_img[img].append(key or "#%d" % i)
            nm = _norm_card_name(inner.get("card_name"))
            if nm:
                by_name[nm].append((key or "#%d" % i, _s(inner.get("card_name")),
                                    _s(inner.get("issuer"))))
        except Exception:
            continue

    for cid, idx in sorted(by_id.items()):
        if len(idx) > 1:
            out.append(Finding(
                severity=ERROR, code="L3.DUPLICATE_CARD_ID",
                message="The card id '%s' is used %d times." % (cid, len(idx)),
                card_id=cid, block="card", field="id",
                evidence=trunc("rows %s" % ", ".join(str(x) for x in idx)),
                impact="Only one of them survives. A user who saved the other card sees "
                       "the wrong card's rewards, and news alerts land on the wrong card.",
                fix="Give each card its own id and re-check anything that referenced it.",
            ))

    for img, cids in sorted(by_img.items()):
        if len(cids) > 1:
            out.append(Finding(
                severity=WARN, code="L3.DUPLICATE_CARD_IMAGE",
                message="%d cards share the same picture file '%s': %s."
                        % (len(cids), img, ", ".join(sorted(cids))),
                block="card", field="image_asset", evidence=trunc(img),
                impact="Two different cards look identical in the picker, so a user can "
                       "pick the wrong one.",
                fix="Give each card its own image, or confirm they really are the same "
                    "plastic.",
            ))

    for nm, rows in sorted(by_name.items()):
        if len(rows) > 1:
            out.append(Finding(
                severity=WARN, code="L3.DUPLICATE_CARD_NAME",
                message="%d cards have effectively the same name once wording is "
                        "ignored: %s." % (len(rows),
                                          "; ".join("%s (%s, %s)" % (r[0], r[1], r[2])
                                                    for r in rows)),
                block="card", field="card_name",
                evidence=trunc(nm),
                impact="The picker shows what looks like the same card twice, and the "
                       "user's spend is split across two entries.",
                fix="Confirm by hand whether these are one product filed twice or two "
                    "genuinely different cards. Never merge on name similarity alone — "
                    "'AU Altura' and 'AU Altura Plus' are different products.",
            ))


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 8. manifest.json stats -> what is actually in the file
# --------------------------------------------------------------------------- #
# THE DEFECT CLASS NO LAYER COVERED: data that is MISSING.
#
# Every finding in all nine layers is derived from a row that is PRESENT, so
# deleting rows made the report shorter and cleaner. Deleting one card produced
# zero findings naming it and took the error count DOWN by one. Deleting 382 of
# 383 cards printed "catalogue 1 cards" with no alarm and exited 1 — the same
# code as the pristine file — so a CI job gating on the exit code saw no change
# at all. It is the one defect class where the tool's own headline is
# arithmetically IMPROVED by the defect.
#
# seed/manifest.json already carries an independently-written declaration of how
# big this file is meant to be, and the string 'total_cards' did not appear in
# the runner or in any of the nine check modules. This reads it.
#
# A SHORTFALL is an ERROR: the manifest was written when the data was complete,
# so rows are gone. A SURPLUS is a WARN: rows were added and nobody regenerated
# the manifest, which is untidy but loses nothing.
_STAT_KEYS = (
    ("total_cards", "card entries"),
    ("total_reward_rules", "reward rules"),
    ("total_exclusion_rules", "exclusion rules"),
    ("total_milestone_rules", "milestone rules"),
    ("total_fuel_surcharge_rules", "fuel surcharge rules"),
    ("total_redemption_rules", "redemption rules"),
    ("total_travel_cards", "travel cards"),
    ("issuers", "distinct issuers"),
    ("total_merchants", "merchants"),
)

_STAT_BLOCK = {
    "total_reward_rules": "reward_rules",
    "total_exclusion_rules": "exclusion_rules",
    "total_milestone_rules": "milestone_rules",
    "total_fuel_surcharge_rules": "fuel_surcharge_rules",
    "total_redemption_rules": "redemption_rules",
}


def _actual_stats(ctx: Ctx) -> dict:
    got = {k: 0 for k, _label in _STAT_KEYS}
    issuers, unusable = set(), {}
    for _i, e, inner, _cid in ctx.entries():
        got["total_cards"] += 1
        iss = _s(inner.get("issuer"))
        if iss:
            issuers.add(iss)
        if inner.get("is_travel"):
            got["total_travel_cards"] += 1
        for key, block in _STAT_BLOCK.items():
            rows = e.get(block)
            if isinstance(rows, list):
                got[key] += len(rows)
            elif rows is not None:
                # Not a list at all — prose typed where an array belongs. The app
                # throws on it and skips the whole card; whoever wrote the
                # manifest counted its characters.
                unusable.setdefault(block, []).append(_s(inner.get("id")) or "?")
    got["issuers"] = len(issuers)
    rows = ctx.merchants.get("merchants") if isinstance(ctx.merchants, dict) else ctx.merchants
    got["total_merchants"] = len(rows) if isinstance(rows, list) else 0
    return got, unusable


def _check_manifest_stats(ctx: Ctx, out: list) -> None:
    man = ctx.manifest
    stats = man.get("stats") if isinstance(man, dict) else None
    if not isinstance(stats, dict) or not any(k in stats for k, _l in _STAT_KEYS):
        out.append(Finding(
            severity=WARN, code="L3.MANIFEST_NO_STATS",
            message="seed/manifest.json declares no row counts, so nothing in this tool "
                    "can tell a deleted card from a card that was never there.",
            block="manifest", field="stats", evidence=trunc(stats),
            impact="Delete a card, a reward rule or an exclusion row and every one of "
                   "the nine layers stays silent — the report just gets shorter. This "
                   "declaration is the only independent record of how big the file "
                   "should be.",
            fix="Add a 'stats' object to seed/manifest.json carrying %s."
                % ", ".join(k for k, _l in _STAT_KEYS),
        ))
        return

    got, unusable = _actual_stats(ctx)
    short, over = [], []
    for key, label in _STAT_KEYS:
        want = num(stats.get(key))
        if want is None or key not in stats:
            continue
        have = got.get(key, 0)
        if have < int(want):
            short.append((key, label, int(want), have))
        elif have > int(want):
            over.append((key, label, int(want), have))

    if short:
        why = ""
        for key, _label, _w, _h in short:
            block = _STAT_BLOCK.get(key)
            if block and unusable.get(block):
                why = (" At least part of the gap is %s: its '%s' is not a list at all, so "
                       "those rows do not exist as rows — the app cannot read them and "
                       "neither can this tool."
                       % (", ".join(sorted(set(unusable[block]))[:3]), block))
                break
        out.append(Finding(
            severity=ERROR, code="L3.MANIFEST_STATS_SHORTFALL",
            message="seed/manifest.json says this file publishes more than it does: "
                    + "; ".join("%d %s declared, %d present" % (w, label, h)
                                for _k, label, w, h in short) + "." + why,
            block="manifest", field="stats",
            evidence=trunc("; ".join("%s: declared %d, actual %d" % (k, w, h)
                                     for k, _l, w, h in short), 320),
            impact="Rows that used to be here are gone. Every other check in this tool "
                   "reads rows that are PRESENT, so a deletion makes the report shorter "
                   "and cleaner instead of louder — this is the only line that notices.",
            fix="Find out whether the rows were deleted or the manifest is stale. If the "
                "data is right, regenerate seed/manifest.json (tools/kredme.py promote "
                "does this). Never regenerate it to silence this without checking first — "
                "that is how a deletion gets published.",
        ))
    if over:
        out.append(Finding(
            severity=WARN, code="L3.MANIFEST_STATS_STALE",
            message="seed/manifest.json under-counts this file: "
                    + "; ".join("%d %s declared, %d present" % (w, label, h)
                                for _k, label, w, h in over) + ".",
            block="manifest", field="stats",
            evidence=trunc("; ".join("%s: declared %d, actual %d" % (k, w, h)
                                     for k, _l, w, h in over), 320),
            impact="Nothing is lost, but the declaration has stopped being a check. Until "
                   "it is regenerated it cannot catch a deletion either.",
            fix="Regenerate seed/manifest.json before publishing.",
        ))
    if not short and not over:
        out.append(Finding(
            severity=INFO, code="L3.MANIFEST_STATS_AGREE",
            message="Every row count seed/manifest.json declares matches what is in the "
                    "file, so nothing has been silently deleted since it was written.",
            block="manifest", field="stats",
            evidence=trunc("; ".join("%s=%d" % (k, got.get(k, 0)) for k, _l in _STAT_KEYS
                                     if k in stats), 320),
            impact="This is the only check here that can see a deletion at all.",
            fix="Nothing. Keep regenerating the manifest whenever the data changes.",
        ))


def run(ctx: Ctx) -> list:
    out: list = []
    for step in (_check_reward_refs,
                 _check_category_mirror,
                 _check_bundled_merchants,
                 _check_exclusion_category_refs,
                 _check_merchant_categories,
                 _check_news_refs,
                 _check_card_images,
                 _check_manifest,
                 _check_manifest_stats,
                 _check_duplicates):
        try:
            step(ctx, out)
        except Exception as exc:
            out.append(Finding(
                severity=WARN, code="L3.CHECK_ABORTED",
                message="Part of the reference check could not finish, so some broken "
                        "links may be unreported.",
                evidence=trunc("%s: %s: %s" % (step.__name__, type(exc).__name__, exc)),
                impact="This run is not a clean bill of health for references.",
                fix="Report this to whoever maintains tools/checks/c3_referential.py.",
            ))
    return out
