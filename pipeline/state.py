#!/usr/bin/env python3
"""
Pipeline state: what we fetched last week, and what we hashed it to.

This module is the reason the weekly run is affordable. Every source URL's bytes
are hashed and committed; next week we re-fetch, re-hash, and only cards whose
source actually changed reach the model. On a typical week that is a handful of
cards, not 380.

Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import pathlib
from typing import Any

from pipeline import config as C

SCHEMA_VERSION = 1


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Source state
# ---------------------------------------------------------------------------
def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "sources": {}}


def load_state(path: pathlib.Path | None = None) -> dict[str, Any]:
    """Read the committed source state, tolerating a first run or a corrupt file.

    A corrupt or unreadable state file is treated as empty rather than fatal.
    That is deliberate: the cost of a needless full re-extract is money, but the
    cost of a crashed weekly job is that nobody notices the catalogue rotting.
    """
    path = path or C.SOURCE_STATE
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return _empty_state()
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return _empty_state()
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


def save_state(state: dict[str, Any], path: pathlib.Path | None = None) -> None:
    path = path or C.SOURCE_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys so the committed file is byte-stable and diffs stay readable.
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def get_source(state: dict[str, Any], card_id: str) -> dict[str, Any] | None:
    entry = state.get("sources", {}).get(card_id)
    return entry if isinstance(entry, dict) else None


def record_source(
    state: dict[str, Any],
    card_id: str,
    *,
    url: str,
    content_sha256: str | None,
    fetched_at: str,
    status: str,
    note: str = "",
    done_reason: str = "",
    text_chars: int = 0,
) -> None:
    """Record one card's fetch outcome.

    `status` is one of: ok | unchanged | fetch_failed | no_url | not_issuer_domain |
    done | card_gone | unresolved_source

    `done_reason` is carried, not derived. This function REPLACES the whole entry, so a
    caller re-recording an already-finished card has to hand the old reason back in or
    it is silently erased. Prefer `touch_source()` for that path — it updates the fetch
    bookkeeping in place and cannot erase anything.

    `text_chars` is how many characters of text the fetch actually yielded. It is
    recorded because "we read the bank's document and it said nothing" is only a
    finding when a document was in fact read. 19 BOBCARD cards were retired for good
    on 188 characters of navigation menu; a length recorded at fetch time is what lets
    a later stage refuse to call that evidence.
    """
    entry: dict[str, Any] = {
        "url": url,
        "content_sha256": content_sha256,
        "fetched_at": fetched_at,
        "status": status,
        "note": note,
    }
    # Only written when there is one, so 300-odd unfinished rows stay free of a
    # `"done_reason": ""` line and the committed diff stays readable.
    if done_reason:
        entry["done_reason"] = done_reason
    if text_chars:
        entry["text_chars"] = int(text_chars)
    state.setdefault("sources", {})[card_id] = entry


def touch_source(
    state: dict[str, Any],
    card_id: str,
    *,
    fetched_at: str,
    content_sha256: str | None = None,
) -> bool:
    """Update the fetch bookkeeping on an existing entry, erasing nothing else.

    The weekly "bytes did not move" path used to call record_source, which REPLACES the
    whole entry — so every key the entry had earned (why it finished, how long its
    document was, whether its evidence was its own page) had to be hand-carried back in
    or it was silently erased. Every new key would be one more thing to remember. This
    updates the two fields a re-fetch actually learns and leaves the rest alone.

    Deliberately does NOT touch `done_at`: the age of a finding is measured from when
    the finding was made, not from the last time we re-hashed the page it came from.
    """
    entry = get_source(state, card_id)
    if entry is None:
        return False
    entry["fetched_at"] = fetched_at
    if content_sha256:
        entry["content_sha256"] = content_sha256
    return True


# Only a card that made it all the way through the pipeline may suppress a re-extract.
# Any other status means we fetched the bytes but never turned them into a verdict, and
# the work still needs doing.
STATUS_DONE = "done"

# WHY a card finished, recorded alongside the status.
#
# The distinction this encodes is the one that was costing money. "We read the bank's
# document and nothing came of it" and "we have never processed this card" were both
# stored as status 'fetched', and they are completely different facts: the first is a
# finished cycle whose answer happens to be 'no change', the second is unpaid work. The
# first must not be re-billed; the second must.
#
# All four of these mean FINISHED. None of them means the card is retired for good —
# has_changed() still re-reads any card whose source bytes move, whatever the reason
# says. The reason is evidence for a human, never a suppression rule.
DONE_VERIFIED = "verified"                  # at least one observation survived the adversary
DONE_ALL_REFUTED = "all_refuted"            # we proposed things; the adversary killed every one
DONE_NO_OBSERVATIONS = "no_observations"    # we read the document; the extractor found nothing to report

DONE_REASONS = frozenset({
    DONE_VERIFIED,
    DONE_ALL_REFUTED,
    DONE_NO_OBSERVATIONS,
})

# ---------------------------------------------------------------------------
# Two statuses that are NOT `done`, and must never become it.
#
# STATUS_CARD_GONE — the card left seed/cards.json between fetch and verdict. This
# used to be a done reason, which meant the card was retired at that hash FOREVER: if
# it came back to the catalogue with its page unchanged it was never re-extracted,
# because the pipeline believed it had finished. No bank change was needed for the
# data to be wrong. A card absent from the catalogue is never fetched anyway, so
# recording it as its own status costs nothing and keeps the way back.
#
# STATUS_UNRESOLVED_SOURCE — we never found this card's own page. Whatever we read
# was somebody else's document (an issuer's card-LISTING page, shared with 20 other
# cards) or was too short to be a document at all. "Nothing found" in that document is
# a source-resolution failure, not a finding about this card, and it must not suppress
# a re-read. These cards are the source-resolution backlog; they belong in
# pipeline/sources_overrides.json or in `cli.py discover`, not in a retirement list.
#
# Neither is in DONE_REASONS, so has_changed() keeps returning True for both.
# ---------------------------------------------------------------------------
STATUS_CARD_GONE = "card_gone"
STATUS_UNRESOLVED_SOURCE = "unresolved_source"

# `all_refuted` and `no_observations` together are the answer to "which banks publish
# nothing we can use?" — a question that was previously invisible because both outcomes
# were indistinguishable from a card that had never been looked at.
DONE_REASONS_NOTHING_KEPT = frozenset({DONE_ALL_REFUTED, DONE_NO_OBSERVATIONS})


def _age_days(stamp: object, now: "_dt.datetime | None" = None) -> float:
    """Days since an ISO-8601 stamp. An unreadable stamp reads as infinitely old.

    Infinitely old is the safe direction: it forces a re-read. A stamp we cannot parse
    must never be the reason a card is skipped.
    """
    if not isinstance(stamp, str) or not stamp.strip():
        return float("inf")
    try:
        when = _dt.datetime.fromisoformat(stamp.strip().replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    return (now - when).total_seconds() / 86400.0


def has_changed(
    state: dict[str, Any],
    card_id: str,
    new_sha: str,
    *,
    now: "_dt.datetime | None" = None,
) -> bool:
    """True when this card still needs extracting.

    Three conditions have to hold before we skip a card: the source bytes are the ones
    we have seen before, we actually finished processing them, AND the bytes we hashed
    were this card's own document. Comparing on the hash alone was a silent, compounding
    leak — the hash was written at fetch time, so a card whose batch later expired or
    errored still matched next week and was never retried. A card we have never seen
    counts as changed, which is what makes the first run a full sweep and every later
    run incremental.

    THE THIRD CONDITION. 108 of the 139 cards retired by the state repair were pinned to
    a generic issuer card-LISTING page — 21 ICICI cards behind one URL, 19 BOBCARD, 18
    SBI. For those, `done` meant "this issuer's card-list page has not moved", so a bank
    repricing one card could not trigger a re-read of that card: the bytes we watch have
    nothing to do with it. A card whose evidence is not its own page therefore gets a
    MAXIMUM AGE instead of an indefinite skip — it is re-read after
    config.SHARED_SOURCE_MAX_AGE_DAYS whatever the hash says. That bounds the cost (one
    re-read a month, not one a week) without ever letting a card go unread forever.
    """
    prev = get_source(state, card_id)
    if prev is None:
        return True
    old = prev.get("content_sha256")
    if not old or old != new_sha:
        return True
    if prev.get("status") != STATUS_DONE:
        return True
    if prev.get("source_is_card_specific") is False:
        age = _age_days(prev.get("done_at") or prev.get("fetched_at"), now)
        return age >= C.SHARED_SOURCE_MAX_AGE_DAYS
    return False


def mark_done(
    state: dict[str, Any],
    card_id: str,
    reason: str = DONE_VERIFIED,
    *,
    card_specific: bool = True,
    done_at: str = "",
) -> bool:
    """Record that this card completed the pipeline at its current hash, and why.

    Called once the card's observations have been JUDGED — including when the judgement
    was "nothing survived", which is a completed cycle, not a failure to retry. It must
    NOT be called for a card the adversary never saw; that card has not been judged, and
    the money to judge it has not been spent yet.

    `reason` must be one of DONE_REASONS. An unknown reason raises rather than being
    stored: a typo'd reason would still set status='done' and silently retire the card,
    and a wrong retirement is invisible until somebody notices a card's data is stale.

    `card_specific` says whether the bytes behind this verdict were this card's own
    document. False is recorded on the entry, and has_changed() then refuses to let the
    hash suppress a re-read for longer than SHARED_SOURCE_MAX_AGE_DAYS. It is written
    only when False so the committed diff stays readable.
    """
    if reason not in DONE_REASONS:
        raise ValueError(
            f"unknown done reason {reason!r}; expected one of {sorted(DONE_REASONS)}"
        )
    entry = get_source(state, card_id)
    if entry is None:
        return False
    entry["status"] = STATUS_DONE
    entry["done_reason"] = reason
    if done_at:
        entry["done_at"] = done_at
    if card_specific:
        entry.pop("source_is_card_specific", None)
    else:
        entry["source_is_card_specific"] = False
    return True


def mark_card_gone(state: dict[str, Any], card_id: str) -> bool:
    """The card left seed/cards.json. Record it WITHOUT retiring it.

    Clears the hash as well as the status. A card that comes back to the catalogue must
    be extracted again even if the issuer's page never moved, and leaving the old hash
    in place would be a second way to suppress that.
    """
    entry = get_source(state, card_id)
    if entry is None:
        return False
    entry["status"] = STATUS_CARD_GONE
    entry["content_sha256"] = None
    entry.pop("done_reason", None)
    entry.pop("done_at", None)
    return True


def mark_unresolved_source(state: dict[str, Any], card_id: str, note: str = "") -> bool:
    """We never found this card's own readable document. Not a finding, not done."""
    entry = get_source(state, card_id)
    if entry is None:
        return False
    entry["status"] = STATUS_UNRESOLVED_SOURCE
    entry.pop("done_reason", None)
    entry.pop("done_at", None)
    if note:
        entry["note"] = note
    return True


def done_reason(state: dict[str, Any], card_id: str) -> str:
    """The recorded reason this card finished, or "" when it has not finished.

    Empty for the rows written before reasons existed, which is honest: we know those
    cards finished, we do not know what they finished with.
    """
    entry = get_source(state, card_id)
    if entry is None or entry.get("status") != STATUS_DONE:
        return ""
    reason = entry.get("done_reason")
    return reason if isinstance(reason, str) else ""


# ---------------------------------------------------------------------------
# Batch state — handles for in-flight Anthropic batches
#
# The weekly job is split across three short GitHub Actions runs (submit / poll /
# apply) precisely so no single job has to sit inside Actions' 6h limit while the
# model works. That only works if the batch id survives between runs, which is
# what this file is.
# ---------------------------------------------------------------------------
def load_batch_state(path: pathlib.Path | None = None) -> dict[str, Any]:
    path = path or C.BATCH_STATE
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "batches": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"schema_version": SCHEMA_VERSION, "batches": []}
    if not isinstance(data, dict) or not isinstance(data.get("batches"), list):
        return {"schema_version": SCHEMA_VERSION, "batches": []}
    return data


def save_batch_state(state: dict[str, Any], path: pathlib.Path | None = None) -> None:
    path = path or C.BATCH_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def add_batch(
    state: dict[str, Any],
    *,
    batch_id: str,
    kind: str,
    submitted_at: str,
    count: int,
    budgeted_usd: float = 0.0,
) -> None:
    """Track a submitted batch. `kind` is 'extract' or 'verify'.

    `budgeted_usd` is what this submission committed, in the same budgeted dollars the
    ceiling is expressed in. It is written here because the two halves of a cycle run in
    two different GitHub Actions jobs, hours apart, and stage 2 cannot check a cycle
    budget it cannot read. Without it the ceiling was checked twice against the same
    constant and authorised twice what it said.
    """
    entry: dict[str, Any] = {
        "batch_id": batch_id,
        "kind": kind,
        "submitted_at": submitted_at,
        "request_count": count,
        "status": "submitted",
    }
    if budgeted_usd:
        entry["budgeted_usd"] = round(float(budgeted_usd), 4)
    state.setdefault("batches", []).append(entry)


def cycle_committed_usd(state: dict[str, Any], *, since_batch_id: str = "") -> float:
    """Budgeted dollars already committed by the cycle that `since_batch_id` belongs to.

    A cycle is one extraction batch plus the verification batch built from it. Walking
    back from the extraction batch and summing everything recorded at or after it gives
    stage 2 the number stage 1 already spent, which is what makes MAX_CYCLE_USD a cycle
    budget rather than a per-batch tripwire checked twice.
    """
    batches = state.get("batches", [])
    start = 0
    if since_batch_id:
        for i, b in enumerate(batches):
            if b.get("batch_id") == since_batch_id:
                start = i
                break
    total = 0.0
    for b in batches[start:]:
        try:
            total += float(b.get("budgeted_usd") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def mark_batch(state: dict[str, Any], batch_id: str, status: str) -> bool:
    """Set a tracked batch's status. Returns False when the id is unknown."""
    for b in state.get("batches", []):
        if b.get("batch_id") == batch_id:
            b["status"] = status
            return True
    return False


def pending_batches(state: dict[str, Any], kind: str | None = None) -> list[dict[str, Any]]:
    out = [b for b in state.get("batches", []) if b.get("status") == "submitted"]
    if kind:
        out = [b for b in out if b.get("kind") == kind]
    return out


# ---------------------------------------------------------------------------
# Metrics history — the five numbers a founder can read week over week
# ---------------------------------------------------------------------------
def append_metrics(row: dict[str, Any], path: pathlib.Path | None = None) -> None:
    path = path or C.METRICS_HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


def read_metrics(path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    path = path or C.METRICS_HISTORY
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # one bad line must not lose the whole history
    return rows
