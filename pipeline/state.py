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
) -> None:
    """Record one card's fetch outcome.

    `status` is one of: ok | unchanged | fetch_failed | no_url | not_issuer_domain | done

    `done_reason` is carried, not derived. This function REPLACES the whole entry, so a
    caller re-recording an already-finished card (the weekly "bytes did not move" path
    does exactly that) has to hand the old reason back in or it is silently erased and
    we lose the record of why the card finished with nothing.
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
    state.setdefault("sources", {})[card_id] = entry


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
DONE_CARD_GONE = "card_gone"                # the card left seed/cards.json between fetch and verdict

DONE_REASONS = frozenset({
    DONE_VERIFIED,
    DONE_ALL_REFUTED,
    DONE_NO_OBSERVATIONS,
    DONE_CARD_GONE,
})

# `all_refuted` and `no_observations` together are the answer to "which banks publish
# nothing we can use?" — a question that was previously invisible because both outcomes
# were indistinguishable from a card that had never been looked at.
DONE_REASONS_NOTHING_KEPT = frozenset({DONE_ALL_REFUTED, DONE_NO_OBSERVATIONS})


def has_changed(state: dict[str, Any], card_id: str, new_sha: str) -> bool:
    """True when this card still needs extracting.

    Two conditions have to hold before we skip a card: the source bytes are the ones we
    have seen before, AND we actually finished processing them. Comparing on the hash
    alone was a silent, compounding leak — the hash was written at fetch time, so a card
    whose batch later expired or errored still matched next week and was never retried.
    A card we have never seen counts as changed, which is what makes the first run a
    full sweep and every later run incremental.
    """
    prev = get_source(state, card_id)
    if prev is None:
        return True
    old = prev.get("content_sha256")
    if not old or old != new_sha:
        return True
    return prev.get("status") != STATUS_DONE


def mark_done(
    state: dict[str, Any],
    card_id: str,
    reason: str = DONE_VERIFIED,
) -> bool:
    """Record that this card completed the pipeline at its current hash, and why.

    Called once the card's observations have been JUDGED — including when the judgement
    was "nothing survived", which is a completed cycle, not a failure to retry. It must
    NOT be called for a card the adversary never saw; that card has not been judged, and
    the money to judge it has not been spent yet.

    `reason` must be one of DONE_REASONS. An unknown reason raises rather than being
    stored: a typo'd reason would still set status='done' and silently retire the card,
    and a wrong retirement is invisible until somebody notices a card's data is stale.
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
    state: dict[str, Any], *, batch_id: str, kind: str, submitted_at: str, count: int
) -> None:
    """Track a submitted batch. `kind` is 'extract' or 'verify'."""
    state.setdefault("batches", []).append(
        {
            "batch_id": batch_id,
            "kind": kind,
            "submitted_at": submitted_at,
            "request_count": count,
            "status": "submitted",
        }
    )


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
