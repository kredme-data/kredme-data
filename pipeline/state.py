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

# 2 since 2026-08-19: the file gained a top-level `evidence_runs` section beside
# `sources`, so a reader has to be able to tell the two shapes apart. Files
# already on disk keep the version they were written with — load_state does not
# rewrite it — so this only stamps files created from here on.
SCHEMA_VERSION = 2


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

    "Empty" is per SECTION, not per file. The file now carries two independent
    top-level sections, and `evidence_runs` is a record of money already spent —
    which cards the catch-up has paid to read, and when. Emptying the whole file
    because `sources` came back the wrong shape used to cost one needless
    re-extract; it would now also reset the rotation to zero and re-select, and
    re-pay for, the same first N cards. So a structurally valid section is kept
    even when its neighbour is not, and only the broken section is rebuilt.
    """
    path = path or C.SOURCE_STATE
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()

    out = _empty_state()
    if isinstance(data.get("sources"), dict):
        out["sources"] = data["sources"]
    if isinstance(data.get(EVIDENCE_RUNS), dict):
        out[EVIDENCE_RUNS] = data[EVIDENCE_RUNS]
    version = data.get("schema_version")
    out["schema_version"] = version if isinstance(version, int) else SCHEMA_VERSION
    # Anything a future version parks here survives a load/save round trip.
    for key, value in data.items():
        out.setdefault(key, value)
    return out


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
) -> None:
    """Record one card's fetch outcome.

    `status` is one of: ok | unchanged | fetch_failed | no_url | not_issuer_domain
    """
    state.setdefault("sources", {})[card_id] = {
        "url": url,
        "content_sha256": content_sha256,
        "fetched_at": fetched_at,
        "status": status,
        "note": note,
    }


# Only a card that made it all the way through the pipeline may suppress a re-extract.
# Any other status means we fetched the bytes but never turned them into a verdict, and
# the work still needs doing.
STATUS_DONE = "done"


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


def completed_at_current_bytes(state: dict[str, Any], card_id: str) -> bool:
    """True when this card has already been read end-to-end at the bytes on record.

    This is `has_changed` asked BEFORE the fetch, which is when a plan has to be
    priced. It cannot know whether the page has moved since — that needs the new
    hash — so it answers the half that is knowable: did a complete extract ->
    verify -> judge cycle finish against the hash we have stored?

    It is what stops `refresh --unsourced-only` re-billing a card forever. A card
    that was read to completion and still cites nothing did not fail; it was read
    and the document did not yield a citable number. Reading the same bytes again
    with the same prompt returns the same nothing. The card leaves the backlog
    until its bytes move — at which point the ordinary hash gate picks it up on
    its own, with no flag and no special case.
    """
    prev = get_source(state, card_id)
    if prev is None:
        return False
    if not prev.get("content_sha256"):
        return False
    return prev.get("status") == STATUS_DONE


def mark_done(state: dict[str, Any], card_id: str) -> bool:
    """Record that this card completed the pipeline at its current hash.

    Called once the card's observations have been judged — including when the verdict
    was "nothing survived", which is a completed cycle, not a failure to retry.
    """
    entry = get_source(state, card_id)
    if entry is None:
        return False
    entry["status"] = STATUS_DONE
    return True


# ---------------------------------------------------------------------------
# Evidence-gap bookkeeping
#
# `refresh --unsourced-only` reads the cards that carry no citation, oldest-unread
# first, so a weekly `--limit 40` walks the backlog instead of circling the same
# 40 cards. That needs a per-card record of when we last tried.
#
# It lives in its OWN top-level section rather than as a field on the source
# record, for one blunt reason: `record_source` REPLACES the whole per-card dict
# on every run, so any key parked there is wiped the following Monday and the
# rotation silently resets. A separate section cannot be clobbered by it.
#
# `fetched_at` on the source record cannot serve either — refresh stamps it for
# every resolved card each week, changed or not, so it says when we last LOOKED,
# never when we last spent a model call.
# ---------------------------------------------------------------------------
EVIDENCE_RUNS = "evidence_runs"


def evidence_attempts(state: dict[str, Any], card_id: str) -> tuple[int, str]:
    """(how many evidence-gap runs have selected this card, when the last one was).

    A card never selected returns (0, "") — which sorts first, so unread cards are
    always read before anything is read twice.
    """
    section = state.get(EVIDENCE_RUNS)
    if not isinstance(section, dict):
        return 0, ""
    record = section.get(card_id)
    if not isinstance(record, dict):
        return 0, ""
    count = record.get("attempts")
    last = record.get("last_attempt_at")
    return (
        count if isinstance(count, int) and count > 0 else 0,
        last if isinstance(last, str) else "",
    )


def note_evidence_attempt(state: dict[str, Any], card_id: str, at: str) -> None:
    """Record that an evidence-gap run selected this card.

    Called for every card the run SELECTED, including cards whose fetch failed.
    Counting only successful reads would leave BOBCARD's unfetchable cards
    permanently at attempts=0, so they would be picked first every single week and
    no other issuer would ever advance.
    """
    count, _ = evidence_attempts(state, card_id)
    section = state.setdefault(EVIDENCE_RUNS, {})
    if not isinstance(section, dict):
        section = {}
        state[EVIDENCE_RUNS] = section
    section[card_id] = {"attempts": count + 1, "last_attempt_at": at}


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
