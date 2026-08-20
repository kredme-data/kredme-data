"""Shared contract for every card-data FIXER module.

Stdlib only. Every module under tools/fixers/ exposes:

    FAMILY  = "reachability"                    # short human label
    HANDLES = ["L6.EXCLUSION_TYPE_INERT", ...]  # exact finding codes it repairs
    def plan(ctx: Ctx, findings: list[dict]) -> list[Edit]

plan() is PURE. It reads ctx (the same Ctx the checks read) and the findings the
validator produced, and it RETURNS proposed edits. It never opens a file for
writing, never mutates ctx, never mutates a row it was handed. The runner is the
only thing that writes, which is what makes every fix dry-runnable and every fix
reviewable as a before/after.

WHY AN EDIT IS A PROPOSAL AND NOT A WRITE
-----------------------------------------
A fixer that wrote in place would be a fixer nobody could review. The two worst
data incidents this repo has had were both "a sweep ran and we found out later":
a blind exclusion sweep that came within one commit of zeroing BPCL Octane's
genuine fuel rewards, and a rename that would have wiped every user's saved cap
progress because the app keys the spend bucket on the rule NAME. An Edit exists
so that both of those are visible as a diff line with a plain-English reason
BEFORE anything is written, and so that a run can be gated by confidence.

DERIVATION IS THE WHOLE RULE
----------------------------
Every new_value must be DERIVED from something already in the file or from a
declared authority (the app's category vocabulary, the card's own redemption
block, the rule's own name). A fixer may never invent a number, a rate, a cap or
a category from outside. If it cannot derive the value, it emits NO edit and the
defect survives. A defect that survives is honest; a guessed value is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from typing import Any

# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
# 'certain'  the edit is FORCED by the data. There is exactly one value the
#            field could hold and still be consistent with what is already in
#            the file — "5,000" -> 5000.0, min 400 > max 250 -> swap.
# 'likely'   a documented heuristic was involved, or a policy call a human owns.
#            Emit it anyway: the runner gates which levels it applies, and a
#            heuristic edit that is never proposed can never be reviewed either.
CERTAIN, LIKELY = "certain", "likely"
_CONFIDENCE = (CERTAIN, LIKELY)


@dataclass
class Edit:
    """One proposed change, anchored to the smallest addressable thing.

    ADDRESSING
    ----------
        card_id   the card's id, as it appears in cards.json
        block     'reward_rules' / 'exclusion_rules' / 'card' / None
        index     row index within that block, or None for the block/entry itself
        field     the key inside that row

    Three shapes, and the runner must handle all three:

      1. FIELD EDIT     field is a str
                        -> set row[field] = new_value
                        (new_value None means: delete that key from the row)

      2. ROW EDIT       field is None, index is an int, block is a row block
                        -> replace the whole row at that index with new_value
                        (new_value None means: delete the row)
                        Use this when two keys must change together or not at
                        all. An exclusion retyped from 'other' to 'category'
                        MUST change exclusion_type and exclusion_value in the
                        same beat: applying half of that pair leaves the file
                        claiming a category called "mobile wallet uploads",
                        which is worse than the defect it was fixing.

      3. ENTRY EDIT     field is None AND block is None
                        -> replace (or, with new_value None, remove) the whole
                        card entry. `index` is the entry's position in
                        cards.json and is only a HINT: card_id is the anchor,
                        because applying an earlier removal shifts every index
                        after it. A runner removing several entries must delete
                        highest-index-first, or look them up by id.

    old_value is always what is there NOW, so the runner can refuse to apply an
    edit whose anchor has moved under it, and so the diff reads without needing
    the original file.

    reason: ONE plain-English sentence a non-technical founder can audit. Not
    "retyped per S1 gate 3" — "The issuer's own wording says wallet loads earn
    nothing, and 'wallet_load' is a category the app can actually enforce."

    evidence: the exact text or field the fix was derived from. If you cannot
    name one, you did not derive the value and you must not emit the edit.

    reversible: True when applying the edit loses nothing that cannot be put
    back from the file itself. A retype that stamps the original into
    `_retyped_from` is reversible. Deleting a card entry is NOT.

    group_id: two or more edits that MUST land together or not at all, when
    they cannot be expressed as one row. Shape 2 above covers the case where the
    keys sit in one row; this covers the case where they do not. A rule that
    keeps its own copy of the card's point value has to move WITH the card's
    value: apply the copy alone and the same point is priced two different ways
    on one card, which is worse than the defect either half was fixing. The
    runner gates and applies a group atomically — held back whole if any member
    is below the confidence gate, refused whole if any member's anchor moved.
    Measured: without it the default gate applied 3 rule-level copies while
    skipping the 3 card-level sources they derive from.

    anchor_fields: {"dotted.path": expected_value} — a STABLE identity for an
    entry-shaped edit, checked instead of comparing the whole entry object. An
    entry edit anchored on the whole entry is refused the moment any other edit
    in the same run touches any field of that card, so 10 of 13 card removals
    were refused on the first pass and applied on the second: the outcome of an
    irreversible operation depended on how many times you ran it. Name the
    fields the decision actually rested on instead.
    """
    card_id: str | None
    block: str | None
    index: int | None
    field: str | None
    old_value: Any
    new_value: Any
    code: str                 # the finding code this edit repairs
    reason: str               # one plain sentence, founder-auditable
    evidence: str             # the exact text/field the value was derived from
    confidence: str = LIKELY  # 'certain' | 'likely'
    reversible: bool = True
    family: str | None = None      # filled in by the runner from FAMILY
    notes: dict = _dc_field(default_factory=dict)   # optional structured extras
    group_id: str | None = None    # all-or-nothing bundle; see the docstring
    anchor_fields: dict | None = None   # stable identity for an ENTRY edit

    def __post_init__(self):
        if self.confidence not in _CONFIDENCE:
            raise ValueError(
                f"confidence must be one of {_CONFIDENCE}, got {self.confidence!r}")
        if not self.reason or not self.reason.strip():
            raise ValueError("every Edit needs a reason a founder can audit")
        if not self.evidence or not str(self.evidence).strip():
            raise ValueError(
                "every Edit needs evidence — the text it was derived from. "
                "No evidence means the value was invented, not derived.")

    # -- what kind of edit is this ---------------------------------------- #
    @property
    def shape(self) -> str:
        if self.field is not None:
            return "field"
        if self.block is None:
            return "entry"
        return "row"

    def anchor(self) -> str:
        """Human-readable address, e.g. hdfc_x.exclusion_rules[3].exclusion_type"""
        bits = [self.card_id or "(no id)"]
        if self.block:
            bits.append(self.block)
        s = ".".join(bits)
        if self.index is not None:
            s += f"[{self.index}]"
        if self.field:
            s += f".{self.field}"
        return s

    def to_dict(self):
        d = {k: v for k, v in self.__dict__.items()
             if v is not None or k in ("old_value", "new_value")}
        d["shape"] = self.shape
        d["anchor"] = self.anchor()
        if not d.get("notes"):
            d.pop("notes", None)
        return d


def trunc(v, n=120) -> str:
    """Same truncation the checks use, so a diff and a finding read alike."""
    import json as _json
    s = v if isinstance(v, str) else _json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"
