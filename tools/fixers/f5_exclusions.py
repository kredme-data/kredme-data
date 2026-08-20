"""f5_exclusions — make an inert exclusion row real, or prove it cannot be.

THIS MODULE OWNS THE EXCLUSION CLASS OUTRIGHT
----------------------------------------------
The Flutter engine's exclusion switch reads exactly two values and has no
default (recommendation_engine.dart:486-497):

    switch (e.exclusionType) { case 'mcc': ... case 'category': ... }

Anything typed 'other' or 'txn_type' is INERT: the card claims an exclusion it
never enforces, so the app pays rewards the bank does not.

Until this revision that class was handled TWICE. f3_reach carried a substring
phrase table and f5 carried a whole-string one; f3 ran first and f5 stood down
whenever f3 recognised a value; and f3 recognised a strict superset of
everything f5 did. The consequence was not a merge conflict, it was worse — the
reviewed, whole-string, per-phrasing-counted table decided NOTHING, and every
mapping that reached seed/cards.json came from the looser matcher, with no
trace of that in the diff. Both narrowings the table documented ("36 wallet rows
left inert on purpose", "21 rent rows left inert on purpose") never happened.

So the ownership is now singular and explicit:

    exclusion_vocab.py  the wording table, the family walk, and the definition
                        of what a card earns — imported at MODULE SCOPE, so a
                        missing or broken copy takes the run down instead of
                        quietly costing it a gate
    f5_exclusions.py    every edit to an exclusion_rules row, forward and back
    f3_reach.py         no longer touches an exclusion row at all

    f3's GATE 1 POISON, GATE 2 BRAND and GATE 3 MCC moved into
    exclusion_vocab.map_exclusion_value ahead of the table, so nothing was lost
    by the move; only the second, looser category table was dropped.


§1  THE GUARDRAIL, AND WHY IT IS FAMILY-AWARE
----------------------------------------------
The engine runs _isExcluded at STEP 1 (recommendation_engine.dart:308-309),
BEFORE any rule matching, so activating an exclusion does not lower a card's
rate — it removes the card from the pick screen entirely. Under-excluding
leaves things as they are; over-excluding takes money off a real user. Every
gate below fails towards "leave it alone".

Before emitting any edit, compute the set of categories the card actually EARNS
in — exclusion_vocab.card_earns_in, five witnesses, described there — then
expand the EXCLUSION's target into its FAMILY: itself, its ancestors and its
descendants, from the app's own categories.json. If the family intersects what
the card earns, emit nothing and record which earning category blocked it.

'railways' is a child of 'travel'. A card whose only reward rule is
`category_id: travel` and whose exclusion list says 'railways' passes a
name-exact guardrail, because the strings differ. It should not. Two such rows
were written onto live cards before the family walk existed
(kotak_mahindra_bank_royale_signature and rbl_bank_world_safari), and the
validator could not see them either: L6.RULE_EXCLUDED_BY_OWN_CARD compares
category names for equality and never walks the tree.

The witness that took longest to find is the co-brand one. Three PhonePe cards
were switched off at the PhonePe merchant by a `wallet_load` exclusion, because
a co-brand's earning is expressed through the categories its partner's spends
fall into — telecom, utilities, insurance, travel — and never through
merchant_ref='phonepe_wallet'. phonepe_wallet is the ONLY PhonePe row in
merchants.json, so the exclusion removed all three cards at the one place they
are best. Same shape on the ICICI HPCL Coral, an HPCL co-brand dead at HPCL,
BPCL and IndianOil. That is the BPCL Octane failure mode with two different
nouns, and it is now caught by reading the card's own name.


§2  TWO GATES, ON PURPOSE, WITH DIFFERENT WIDTHS
-------------------------------------------------
Writing an exclusion and reverting one are not the same act, so they do not run
the same gate.

    WRITE   card_earns_in(..., wide=True). Includes the blunt line: any card
            shipping fuel_surcharge_rules counts as earning on fuel. 359 of 383
            cards ship that block, so every fuel activation goes to a human. A
            false positive costs one skipped fix.
    REVERT  card_earns_in(..., wide=False). A surcharge WAIVER is a fee waiver,
            not a reward, and reverting 55 live fuel rows on the strength of it
            would put back exclusions the issuers really do apply. A false
            positive here costs the user nothing but costs the file 55 real
            exclusions.

Rows the wide gate would block and the narrow gate keeps are neither reverted
nor hidden: census() lists every one under `kept_but_write_gate_would_block`,
by card and target. Silence was the actual defect.


§3  THE REPAIR HALF HAS TWO TRIGGERS
-------------------------------------
A live 'category' or 'mcc' row carrying `_retyped_from` — so a previous sweep
wrote it — is put back exactly as it was when EITHER:

    (a) it fails the family guardrail; or
    (b) the original wording it records no longer maps to the target it now
        carries. That is derived, not guessed: the issuer's own string is in
        the row. It is what puts back six `gold`/`silver` rows the table no
        longer accepts (see exclusion_vocab §1) and three gift-card rows.

The old value is not invented: `_retyped_from` records it verbatim, which is
why these edits are 'certain' and reversible. Two things that used to go wrong
here are now closed:

    the restored exclusion_type is checked against the schema's own enum before
    it is written. `_retyped_from` is free text — 58 of the 484 stamps in the
    file carry no colon at all — and a stamp reading "reduced rate: fuel" would
    otherwise write `exclusion_type: "reduced rate"` into the shipped file.

    a stamp with no colon is no longer dropped in silence. All 58 of them are
    live `category: fuel` rows, the exact shape the guardrail exists for, and
    census() emitted not one repair.* key about them. The value alone is
    recorded, and 426 of 426 well-formed stamps say the type was 'other', so
    'other' is restored and the assumption is counted.

Be honest about the arithmetic. Every revert adds an L6.EXCLUSION_TYPE_INERT
error. The error count goes UP and the data gets BETTER, because an inert
exclusion costs a user nothing and a wrongly-active one removes their card at
the counter. A fix stage that optimised the number instead of the user would
leave them where they are.


§4  A REVERTED ROW SAYS SO, SO THE NEXT SWEEP DOES NOT UNDO THE DECISION
------------------------------------------------------------------------
The revert keeps `_retyped_from` and adds `_reverted_from`, and the forward
half skips any row carrying `_reverted_from`. Deleting the stamp — which is
what this module used to do — left nothing in the file marking the row as
deliberately inert, so the next sweep re-activated it with no trace of the
earlier decision. The invariant is therefore stated honestly: on a touched row
this module changes exclusion_type, exclusion_value, and the provenance stamps,
and nothing else.


§5  rule_name IS NEVER WRITTEN
-------------------------------
Not read out of, not copied into, not touched. The app keys every user's saved
cap progress on that string, so changing it wipes their progress. Every edit
this module emits is a whole-row replacement of an exclusion_rules row, and
exclusion rows do not carry a rule_name at all. The runner's own forbidden()
guard checks this independently; the tests assert it a third time.
"""
from __future__ import annotations

from fixers.base import CERTAIN, Edit, trunc
from fixers.exclusion_vocab import (
    CONCEPTS,
    LIVE_EXCLUSION_TYPES,
    SYNONYMS,
    FamilyTreeUnreadable,
    card_earns_in,
    cobrand_index,
    family_index,
    guardrail_conflict,
    map_exclusion_value,
    mcc_owner,
    merchant_index,
    normalise,
)

FAMILY = "exclusions"

HANDLES = [
    # The forward half — retype an inert row onto something the app enforces —
    # and the wording-mismatch half of the repair, which puts a row back into
    # this class on purpose.
    "L6.EXCLUSION_TYPE_INERT",
    # The guardrail half of the repair. The validator's own check for this code
    # compares category NAMES for equality, so the family-level case (excluding
    # a child while earning the parent) produces no finding today. The edits
    # ride under this code because it is the defect they describe; the missing
    # check is noted in census() as a validator gap.
    "L6.RULE_EXCLUDED_BY_OWN_CARD",
]

# The exclusion_type values the schema knows. A restored type that is not one of
# these is refused: never write a schema enum value that came out of a free-text
# provenance field.
KNOWN_EXCLUSION_TYPES = ("mcc", "category", "other", "txn_type", "(none)")

# 426 of the 426 well-formed stamps in seed/cards.json record 'other'. A stamp
# that lost its type prefix is restored to that, and the assumption is counted.
DEFAULT_RESTORED_TYPE = "other"

__all__ = [
    "CONCEPTS", "FAMILY", "HANDLES", "KNOWN_EXCLUSION_TYPES",
    "LIVE_EXCLUSION_TYPES", "SYNONYMS", "card_earns_in", "census",
    "family_index", "guardrail_conflict", "map_exclusion_value", "normalise",
    "plan",
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _s(v):
    return v.strip() if isinstance(v, str) else None


def _low(v):
    s = _s(v)
    return s.lower() if s else ""


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


class _Tally(dict):
    """A Counter without importing one, so plan() and census() count alike.
    Reading a key that was never touched gives 0 and does not create it, so the
    census only lists dispositions that actually happened."""
    def __missing__(self, k):
        return 0


def _gates(ctx, tally):
    """(families, merchants, mccs, cobrands) or None when the run must not
    decide anything.

    A tree that will not resolve is a REFUSAL, not a shrug. Degrading to plain
    name equality is exactly the behaviour the family gate exists to replace,
    and it used to happen with no error and no counter if the app's
    categories.json typed `id` as a string.
    """
    try:
        families = family_index(getattr(ctx, "app_categories", None))
    except FamilyTreeUnreadable as e:
        tally["gate.family_tree_unreadable"] += 1
        tally["gate.family_tree_unreadable." + str(e)] += 1
        return None
    return families, merchant_index(ctx), mcc_owner(ctx), cobrand_index(ctx)


def _live_pairs(entry) -> set:
    """The (type, value) pairs this card ALREADY enforces.

    The module used never to look, so it emitted rows that duplicated a live
    exclusion or collapsed onto each other — 8 cards ended up with two or three
    exclusion rows byte-identical except for their provenance stamp.
    """
    return {(_low(r.get("exclusion_type")), _low(r.get("exclusion_value")))
            for r in _rows(entry, "exclusion_rules")
            if isinstance(r, dict)
            and _low(r.get("exclusion_type")) in LIVE_EXCLUSION_TYPES}


# --------------------------------------------------------------------------- #
# A. FORWARD — retype an inert row onto something the app can enforce
# --------------------------------------------------------------------------- #
def _plan_forward(ctx, findings, tally):
    app_cats = ctx.app_category_names()
    if not ctx.have_categories():
        # No app vocabulary on this run. Every target would be unverifiable, so
        # nothing is emitted and the run says so out loud.
        tally["forward.skipped_no_vocabulary"] += 1
        return []

    gates = _gates(ctx, tally)
    if gates is None:
        tally["forward.skipped_family_tree_unreadable"] += 1
        return []
    families, merchants, mccs, cobrands = gates

    wanted = _cards_with(findings, "L6.EXCLUSION_TYPE_INERT")
    edits = []

    for _i, entry, _inner, cid in ctx.entries():
        if cid not in wanted:
            continue
        earns = card_earns_in(entry, merchants, mccs, cobrands, wide=True)
        live = _live_pairs(entry)
        for j, row in enumerate(_rows(entry, "exclusion_rules")):
            if not isinstance(row, dict):
                tally["forward.row_not_an_object"] += 1
                continue
            etype = _low(row.get("exclusion_type"))
            if etype in LIVE_EXCLUSION_TYPES:
                tally["forward.already_live"] += 1
                continue                     # idempotence: nothing to do
            if row.get("_reverted_from"):
                # A previous run took this row OUT of the live set on purpose.
                # Re-activating it here would erase that decision silently.
                tally["forward.left_reverted_on_purpose"] += 1
                continue
            raw = row.get("exclusion_value")
            verdict, target, conf, detail = map_exclusion_value(raw, app_cats)

            if verdict not in LIVE_EXCLUSION_TYPES:
                tally["forward.left_" + verdict] += 1
                if verdict == "concept":
                    tally["cannot_express." + detail] += 1
                elif verdict == "unmapped":
                    tally["unmapped_wording." + normalise(raw)] += 1
                continue

            if verdict == "mcc":
                owner = mccs.get(target)
                blocker = owner if owner and owner in earns else None
                if not blocker:
                    for rec in merchants.values():
                        if target in rec["mccs"] and rec["category"] in earns:
                            blocker = rec["category"]
                            break
                guarded_as = owner or target
            else:
                blocker = guardrail_conflict(target, earns, families)
                guarded_as = target

            tally["forward.would_map"] += 1
            tally["forward.would_map." + str(guarded_as)] += 1

            if blocker:
                tally["forward.guardrail_blocked"] += 1
                tally["forward.guardrail_blocked." + str(guarded_as)] += 1
                tally["guardrail_case."
                      + f"{cid} excl {guarded_as} earns {blocker}"] += 1
                continue

            if (verdict, target) in live:
                # The card already enforces exactly this. A second identical row
                # changes nothing the engine reads and makes the file harder to
                # audit, so the row is left inert instead.
                tally["forward.duplicate_of_live_row"] += 1
                tally["duplicate_case." + f"{cid} already excludes {target}"] += 1
                continue
            live.add((verdict, target))

            new_row = dict(row)                          # a copy; ctx is untouched
            new_row["exclusion_type"] = verdict
            new_row["exclusion_value"] = target
            new_row["_retyped_from"] = f"{etype or '(none)'}:{_s(raw) or ''}"
            if verdict == "mcc":
                reason = ("This exclusion already gives a merchant code, so it "
                          "is written as a merchant-code exclusion, which is "
                          "one of the only two kinds the app can act on.")
            else:
                reason = (
                    f"The issuer's own wording here means "
                    f"{target.replace('_', ' ')}, which is one of the two kinds "
                    f"of exclusion the app can actually act on. Written the way "
                    f"it is today the app ignores it completely, so we promise "
                    f"people rewards on spending the bank pays nothing for.")
            edits.append(Edit(
                card_id=cid, block="exclusion_rules", index=j, field=None,
                old_value=dict(row), new_value=new_row,
                code="L6.EXCLUSION_TYPE_INERT",
                reason=reason,
                evidence=f"exclusion_value = {trunc(_s(raw) or '')}",
                confidence=conf, reversible=True, family=FAMILY,
                notes={"target": target,
                       "matched": detail,
                       "card_earns_in": sorted(earns),
                       "target_family": sorted(families.get(guarded_as)
                                               or {guarded_as})},
            ))
            tally["forward.mapped"] += 1
            tally["forward.mapped." + target] += 1
    return edits


# --------------------------------------------------------------------------- #
# B. REPAIR — put back a row a previous sweep should not have activated
# --------------------------------------------------------------------------- #
def _restore(stamp, tally):
    """(exclusion_type, exclusion_value) to put back, or None to refuse.

    `_retyped_from` is free text, not a controlled format. Both of the ways
    that used to go wrong are refused here rather than written.
    """
    if not isinstance(stamp, str) or not stamp.strip():
        tally["repair.stamp_missing"] += 1
        return None
    if ":" in stamp:
        old_type, old_value = stamp.split(":", 1)
        old_type = old_type.strip().lower()
    else:
        old_type, old_value = DEFAULT_RESTORED_TYPE, stamp
        tally["repair.stamp_type_assumed_other"] += 1
    if old_type not in KNOWN_EXCLUSION_TYPES:
        tally["repair.stamp_type_not_recognised"] += 1
        tally["repair_skipped_case." + f"stamp type {old_type!r}"] += 1
        return None
    return old_type, old_value


def _plan_repair(ctx, findings, tally):
    """See §3. Only rows carrying `_retyped_from` are eligible, because that
    field is the only record of what the row said before, and a revert with no
    recorded original would be an invented value.

    Scanned across every card rather than only cards named by a finding: the
    validator's L6.RULE_EXCLUDED_BY_OWN_CARD check compares category names for
    equality and never walks the tree, so the family-level case this repairs
    produces no finding to key off. That missing check is reported by census().
    """
    app_cats = ctx.app_category_names()
    if not ctx.have_categories():
        tally["repair.skipped_no_vocabulary"] += 1
        return []
    gates = _gates(ctx, tally)
    if gates is None:
        tally["repair.skipped_family_tree_unreadable"] += 1
        return []
    families, merchants, mccs, cobrands = gates
    edits = []

    for _i, entry, _inner, cid in ctx.entries():
        earns = card_earns_in(entry, merchants, mccs, cobrands, wide=False)
        wide = card_earns_in(entry, merchants, mccs, cobrands, wide=True)
        for j, row in enumerate(_rows(entry, "exclusion_rules")):
            if not isinstance(row, dict):
                continue
            etype = _low(row.get("exclusion_type"))
            if etype not in LIVE_EXCLUSION_TYPES:
                continue
            stamp = row.get("_retyped_from")
            if stamp is None:
                continue                    # not written by a sweep; not ours
            if isinstance(stamp, str) and ":" not in stamp:
                # 58 of the 484 stamps in the file lost their type prefix. They
                # used to be dropped here with no key and no census entry, and
                # every one of them is a live `category: fuel` row — the exact
                # shape the guardrail exists for. Counted whether or not this
                # row ends up reverted, so the number is never zero by silence.
                tally["repair.stamp_missing_type_prefix"] += 1
            value = _low(row.get("exclusion_value"))
            target = mccs.get(value, value) if etype == "mcc" else value

            blocker = guardrail_conflict(target, earns, families)
            why = None
            code = "L6.RULE_EXCLUDED_BY_OWN_CARD"
            if blocker:
                why = (
                    f"This card is switched off entirely at "
                    f"{target.replace('_', ' ')} merchants, and it is one of "
                    f"the cards that pays a reward on "
                    f"{blocker.replace('_', ' ')} — the family "
                    f"{target.replace('_', ' ')} belongs to. The app checks "
                    f"exclusions before it looks at any reward rule, so this "
                    f"does not lower the rate, it removes the card from the "
                    f"list. Putting the row back exactly as it was costs a "
                    f"user nothing; leaving it costs them the card.")
            else:
                # Trigger (b): the wording this row records no longer maps to
                # what the row now says. The sweep read the issuer one way and
                # we no longer stand behind that reading.
                orig = stamp.split(":", 1)[1] if ":" in stamp else stamp
                verdict, now, _conf, _d = map_exclusion_value(orig, app_cats)
                if verdict == etype and now == row.get("exclusion_value"):
                    if target in wide and target not in earns:
                        tally["repair.kept_but_write_gate_would_block"] += 1
                        tally["kept_case." + f"{cid} excl {target}"] += 1
                    continue
                code = "L6.EXCLUSION_TYPE_INERT"
                why = (
                    f"A sweep read the issuer's words "
                    f"{trunc(_s(orig) or '', 40)!r} as "
                    f"{str(row.get('exclusion_value')).replace('_', ' ')} and "
                    f"switched the card off on that reading. We no longer stand "
                    f"behind it — the wording names something the app has no "
                    f"way to express — so the row goes back to the words the "
                    f"issuer actually used, where it costs a user nothing.")
                tally["repair.wording_no_longer_maps"] += 1
                tally["repair_wording_case."
                      + f"{cid} {row.get('exclusion_value')} <- {orig}"] += 1

            restored = _restore(stamp, tally)
            if restored is None:
                tally["repair.refused"] += 1
                continue
            old_type, old_value = restored

            new_row = dict(row)
            if old_type == "(none)":
                new_row.pop("exclusion_type", None)
            else:
                new_row["exclusion_type"] = old_type
            new_row["exclusion_value"] = old_value
            # The stamp STAYS, and the revert is recorded next to it. Deleting
            # it left nothing in the file saying this row is deliberately inert,
            # so the next sweep re-activated it with no trace of the decision.
            new_row["_reverted_from"] = f"{etype}:{row.get('exclusion_value')}"

            edits.append(Edit(
                card_id=cid, block="exclusion_rules", index=j, field=None,
                old_value=dict(row), new_value=new_row,
                code=code, reason=why,
                evidence=f"_retyped_from = {trunc(stamp)}",
                confidence=CERTAIN, reversible=True, family=FAMILY,
                notes={"excluded": target, "card_earns_in": sorted(earns),
                       "conflicting_earn": blocker,
                       "target_family": sorted(families.get(target) or {target})},
            ))
            tally["repair.reverted"] += 1
            tally["repair.reverted." + str(target)] += 1
            if blocker:
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
    live and skipped by the forward half, and a reverted row is inert, carries
    `_reverted_from`, and is skipped by both.
    """
    findings = [f for f in (findings or []) if _get(f, "code") in HANDLES]
    tally = _Tally()
    return _plan_forward(ctx, findings, tally) + _plan_repair(ctx, findings, tally)


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
                                                       "duplicate_case.",
                                                       "kept_case.",
                                                       "repair_case.",
                                                       "repair_wording_case.",
                                                       "repair_skipped_case.",
                                                       "gate.family_tree_unreadable.",
                                                       "activated.")))),
        # The app feature request, with a number on it.
        "app_cannot_express": _slice("cannot_express."),
        # Wording nothing recognised — the honest residue, not a failure.
        "unrecognised_wording": _slice("unmapped_wording."),
        # Named, so a human can review each one instead of trusting a count.
        "guardrail_blocked_cases": sorted(_slice("guardrail_case.")),
        "guardrail_repairs": sorted(_slice("repair_case.")),
        "wording_repairs": sorted(_slice("repair_wording_case.")),
        "duplicate_rows_left_inert": sorted(_slice("duplicate_case.")),
        # The asymmetry in §2, named rather than implied: the write gate would
        # refuse these, the revert gate keeps them, and nobody has signed off.
        "kept_but_write_gate_would_block": sorted(_slice("kept_case.")),
        "repair_refusals": sorted(_slice("repair_skipped_case.")),
        # Every row that IS live because of a reading a reviewer should sign
        # off, grouped by the wording that carried it.
        "activated_rows_for_review": _activated_for_review(ctx),
        "validator_gap": (
            "L6.RULE_EXCLUDED_BY_OWN_CARD compares category NAMES for equality "
            "and never walks categories.json, so a card that excludes a child "
            "category while earning its parent produces no finding at all. The "
            "repair half of this module scans every card rather than only the "
            "cards a finding names, because there is no finding to name them."),
    }


# The two readings that are arguments rather than the issuer's own word, and so
# have to be signed off by a human rather than counted. See exclusion_vocab §1.
_REVIEW_READINGS = {
    "wallet_load": "bare wallet wording read as a LOAD",
    "rent": "bare rental / real-estate wording read as house RENT",
}


def _activated_for_review(ctx) -> dict:
    """Every live exclusion row whose original wording was read on one of the
    two elimination arguments, listed by card and wording.

    D3's complaint was not only that the rows existed, it was that a report said
    zero violations remained while 87 rows shipped on wording the table refused.
    The table now accepts them and explains why; this is where the reader checks
    that reasoning against the actual rows instead of taking the count on trust.
    """
    out = {}
    try:
        entries = list(ctx.entries())
    except Exception:                                    # noqa: BLE001
        return out
    app_cats = ctx.app_category_names()
    for _i, entry, _inner, cid in entries:
        for row in _rows(entry, "exclusion_rules"):
            if not isinstance(row, dict):
                continue
            if _low(row.get("exclusion_type")) != "category":
                continue
            value = _low(row.get("exclusion_value"))
            reading = _REVIEW_READINGS.get(value)
            if not reading:
                continue
            stamp = row.get("_retyped_from")
            if not isinstance(stamp, str):
                continue
            orig = stamp.split(":", 1)[1] if ":" in stamp else stamp
            verdict, target, conf, _d = map_exclusion_value(orig, app_cats)
            if verdict != "category" or target != value or conf == CERTAIN:
                continue                # the issuer said the word; nothing to sign
            out.setdefault(f"{value} — {reading}", []).append(
                f"{cid}  <- {_s(orig) or ''!r}")
    return {k: sorted(v) for k, v in sorted(out.items())}


def _by(edits, key):
    out = {}
    for e in edits:
        k = key(e)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
