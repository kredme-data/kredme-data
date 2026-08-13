#!/usr/bin/env python3
"""
Anthropic Message Batches wrapper for the extraction and verification passes.

Usage:
    python3 pipeline/batch.py submit  --requests .pipeline-work/requests.json [--dry-run]
    python3 pipeline/batch.py poll    [--batch-id msgbatch_...]
    python3 pipeline/batch.py collect --batch-id msgbatch_... --out .pipeline-work/results.json

    from pipeline import batch
    req = batch.build_extract_request(card_id, card_name, url, document_text)

WHY THIS MODULE EXISTS
----------------------
A synchronous 380-card sweep was measured at ~11.6h. GitHub Actions kills a job
at 6h, so the sweep could never finish inside one run. The Batch API decouples
submission from completion: submit returns in seconds, the model works for up to
24h on Anthropic's side, and a separate short job collects. Three short jobs, no
6h problem — and batch pricing is half of standard on top of that.

Stdlib only, with one exception: the `anthropic` SDK, imported lazily inside
_client() so `import pipeline.batch` — and the whole test suite — works on a
machine with nothing pip-installed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import pathlib
import re
import sys
from typing import Any

# Run as a script (`python3 pipeline/batch.py`), Python puts pipeline/ on
# sys.path rather than the repo root, and the package imports below fail. Only
# fires for the script entry point; `import pipeline.batch` sets __package__.
if __package__ in (None, ""):  # pragma: no cover - exercised by TestCLI
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pipeline import config as C
from pipeline import schema as S
from pipeline import state as st

# ---------------------------------------------------------------------------
# custom_id
#
# Results come back in ANY order, so custom_id is the only thing tying a result
# to the card it came from. Two constraints bound it: the id must survive as an
# opaque token (some real card ids contain parentheses, dots and slashes — e.g.
# 'bobcard_(bank_of_baroda)_card_eterna'), and the whole string must fit 64
# characters, while our longest real id is 88.
#
# So the id half is sanitised to [A-Za-z0-9_-] and, whenever sanitising changed
# anything or the result would overflow, a truncating sha256 suffix of the
# ORIGINAL id is appended. The suffix is what keeps the mapping injective:
# without it 'card_(x)' and 'card__x_' would collide and two cards would share
# one result. The hash is of the original id, so a given card always produces
# the same custom_id across runs.
# ---------------------------------------------------------------------------
KIND_EXTRACT = "extract"
KIND_VERIFY = "verify"
KIND_NEWS = "news"
KINDS = (KIND_EXTRACT, KIND_VERIFY, KIND_NEWS)

CUSTOM_ID_MAX_LEN = 64
_SEPARATOR = "::"
_HASH_LEN = 8

# Budgeted against the LONGER prefix so a card sanitises to the same string for
# both kinds. If extract and verify truncated differently, the caller's
# {custom_id -> card_id} map would silently stop matching across the two passes.
ID_MAX_LEN = CUSTOM_ID_MAX_LEN - len(max(KINDS, key=len)) - len(_SEPARATOR)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % ID_MAX_LEN)
_CUSTOM_ID_RE = re.compile(
    r"^(%s)%s([A-Za-z0-9_-]{1,%d})$" % ("|".join(KINDS), _SEPARATOR, ID_MAX_LEN)
)

# ---------------------------------------------------------------------------
# Batch limits
#
# The documented ceilings are 100,000 requests and 256 MB per batch. We stop
# well short of both: a batch rejected at the limit costs a whole weekly cycle,
# and the margin is free at 380 cards.
# ---------------------------------------------------------------------------
MAX_REQUESTS_PER_BATCH = 90_000
MAX_BYTES_PER_BATCH = 200 * 1024 * 1024

# Measured on English prose plus the tabular fee/rate text that fills issuer
# T&C pages: ~3.6 characters per token. Pure prose runs nearer 4.0 and dense
# numeric tables nearer 3.0, so this sits deliberately on the pessimistic side
# of the mix — a cost estimate that under-reports is the one that hurts.
CHARS_PER_TOKEN = 3.6

# claude-opus-5 will not cache a prefix shorter than this, and it fails silently
# — no error, just no cache hits and a bill to match. tests/test_batch.py asserts
# both built prefixes clear it, so shrinking a schema can never quietly disable
# caching for a whole sweep.
MIN_CACHEABLE_PREFIX_TOKENS = 512


def _sanitise_card_id(card_id: str) -> str:
    """Map a raw card id onto the custom_id charset, injectively."""
    if not isinstance(card_id, str) or not card_id.strip():
        raise ValueError("card_id must be a non-empty string")

    safe = _UNSAFE_CHARS.sub("_", card_id)
    if safe != card_id or len(safe) > ID_MAX_LEN:
        digest = hashlib.sha256(card_id.encode("utf-8")).hexdigest()[:_HASH_LEN]
        keep = ID_MAX_LEN - 1 - _HASH_LEN
        safe = safe[:keep].rstrip("_") + "_" + digest

    if not _SAFE_ID_RE.match(safe):
        raise ValueError(f"could not sanitise card_id {card_id!r} to a usable custom_id")
    return safe


def _custom_id(kind: str, card_id: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    cid = f"{kind}{_SEPARATOR}{_sanitise_card_id(card_id)}"
    if len(cid) > CUSTOM_ID_MAX_LEN:
        raise ValueError(f"custom_id {cid!r} exceeds {CUSTOM_ID_MAX_LEN} characters")
    return cid


def parse_custom_id(custom_id: str) -> tuple[str, str]:
    """Split a custom_id back into (kind, sanitised_card_id).

    This inverts the prefix, not the sanitisation — the hash suffix is one-way.
    Callers that need the original card id keep the map they built at submit
    time: {r["custom_id"]: card_id for r, card_id in zip(requests, card_ids)}.
    """
    if not isinstance(custom_id, str):
        raise ValueError(f"custom_id must be a string, got {type(custom_id).__name__}")
    m = _CUSTOM_ID_RE.match(custom_id)
    if not m:
        raise ValueError(f"malformed custom_id: {custom_id!r}")
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# Request construction
#
# These return plain dicts rather than SDK objects so a request can be inspected,
# diffed and unit-tested on a machine without the SDK. That costs nothing at
# submit time: anthropic.types...Request and MessageCreateParamsNonStreaming are
# TypedDicts, so the dicts below are already exactly what the SDK expects.
#
# Prompt caching is a PREFIX match. The system blocks carry the stable half
# (instructions + schema) with cache_control on the last one, and every volatile
# byte — card id, card name, URL, document text — lives in the user message
# after it. Anything card-specific creeping into a system block would invalidate
# the cache on all 380 requests, so nothing card-specific may go there.
# ---------------------------------------------------------------------------
def _schema_block_text(schema: dict[str, Any]) -> str:
    """Render the output schema for the cached prefix.

    sort_keys makes the rendering byte-stable: the same schema must serialise to
    the same bytes on every request in the batch or the prefix stops matching.
    """
    body = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False)
    return (
        "Your reply is a single JSON object conforming to the schema below. "
        "Every required field must be present, and no key outside the schema is "
        "permitted. Report only what the document states.\n\n" + body
    )


def _system_blocks(system_prompt: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": system_prompt},
        {
            "type": "text",
            "text": _schema_block_text(schema),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _require_document(document_text: str) -> str:
    """An empty document is a fetch bug, and paying a model to read it is waste."""
    if not isinstance(document_text, str) or not document_text.strip():
        raise ValueError("document_text must be a non-empty string")
    return document_text


def build_extract_request(
    card_id: str, card_name: str, url: str, document_text: str
) -> dict[str, Any]:
    """Build one pass-1 extraction request."""
    _require_document(document_text)
    if not isinstance(card_name, str) or not card_name.strip():
        raise ValueError("card_name must be a non-empty string")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")

    user_text = (
        f"card_id: {card_id}\n"
        f"card_name: {card_name}\n"
        f"source_url: {url}\n\n"
        "Document text follows.\n\n"
        f"{document_text}"
    )
    return {
        "custom_id": _custom_id(KIND_EXTRACT, card_id),
        "params": {
            "model": C.EXTRACT_MODEL,
            "max_tokens": C.EXTRACT_MAX_TOKENS,
            "system": _system_blocks(S.EXTRACTION_SYSTEM, S.EXTRACTION_SCHEMA),
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            "output_config": {
                "format": {"type": "json_schema", "schema": S.EXTRACTION_SCHEMA},
                "effort": C.EXTRACT_EFFORT,
            },
        },
    }


def build_news_request(issuer: str, url: str, document_text: str) -> dict[str, Any]:
    """Build one 'what changed on this notice page' request.

    Keyed by issuer, not by card: the watched pages are portfolio-wide notices, and
    mapping a change onto specific cards is newsgen.map_cards's job, not the model's.
    """
    _require_document(document_text)
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError("issuer must be a non-empty string")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")

    user_text = (
        f"issuer: {issuer}\n"
        f"source_url: {url}\n\n"
        "This page changed since we last read it. Page text follows.\n\n"
        f"{document_text}"
    )
    return {
        "custom_id": _custom_id(KIND_NEWS, issuer),
        "params": {
            "model": C.EXTRACT_MODEL,
            "max_tokens": C.EXTRACT_MAX_TOKENS,
            "system": _system_blocks(S.NEWS_CHANGE_SYSTEM, S.NEWS_CHANGE_SCHEMA),
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            "output_config": {
                "format": {"type": "json_schema", "schema": S.NEWS_CHANGE_SCHEMA},
                "effort": C.EXTRACT_EFFORT,
            },
        },
    }


def run_sync(requests: list[dict[str, Any]], *, client: Any = None) -> list[dict[str, Any]]:
    """Run requests synchronously and return their parsed bodies, in input order.

    The news path uses this rather than the Batch API on purpose. A batch can take up
    to 24 hours, and the entire value of a devaluation alert is that it is timely —
    waiting a day to tell someone about a change that takes effect next week defeats
    the point. There are at most a dozen watched pages, so the 50% batch discount is
    worth a few rupees and not worth the delay.

    A request that fails returns {} in its slot rather than raising: one unreachable
    issuer must not lose the other eleven.
    """
    if not isinstance(requests, list):
        raise ValueError(f"requests must be a list, got {type(requests).__name__}")
    if not requests:
        return []

    if client is None:
        client = _client()

    out: list[dict[str, Any]] = []
    for request in requests:
        params = _params_of(request)
        try:
            msg = client.messages.create(**params)
            body = _first_text(msg)
            out.append(json.loads(body) if body else {})
        except Exception:  # noqa: BLE001 — one bad page must not sink the run
            out.append({})
    return out


def build_verify_request(
    card_id: str, document_text: str, observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build one pass-2 verification request for a card's pass-1 observations."""
    _require_document(document_text)
    if not isinstance(observations, list):
        raise ValueError("observations must be a list")
    if not all(isinstance(o, dict) for o in observations):
        raise ValueError("every observation must be a dict")

    # sort_keys again: two runs over the same observations must produce the same
    # bytes, otherwise a re-submit looks like a different request and re-pays.
    claimed = json.dumps(observations, indent=2, sort_keys=True, ensure_ascii=False)
    user_text = (
        f"card_id: {card_id}\n\n"
        "Document text follows.\n\n"
        f"{document_text}\n\n"
        "--- END OF DOCUMENT ---\n\n"
        "Claimed observations to audit, in order. `index` in your verdicts refers "
        "to a position in this list.\n\n"
        f"{claimed}"
    )
    return {
        "custom_id": _custom_id(KIND_VERIFY, card_id),
        "params": {
            "model": C.VERIFY_MODEL,
            "max_tokens": C.VERIFY_MAX_TOKENS,
            "system": _system_blocks(S.VERIFICATION_SYSTEM, S.VERIFICATION_SCHEMA),
            "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
            "output_config": {
                "format": {"type": "json_schema", "schema": S.VERIFICATION_SCHEMA},
                "effort": C.VERIFY_EFFORT,
            },
        },
    }


# ---------------------------------------------------------------------------
# Cost estimation — pure, no SDK, no network
# ---------------------------------------------------------------------------
def _est_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _params_of(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError(f"request must be a dict, got {type(request).__name__}")
    params = request.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"request {request.get('custom_id')!r} has no params dict")
    return params


def _prefix_text(params: dict[str, Any]) -> str:
    """The cached half: every system block, concatenated."""
    blocks = params.get("system") or []
    if not isinstance(blocks, list):
        raise ValueError("params['system'] must be a list of blocks")
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def _volatile_text(params: dict[str, Any]) -> str:
    """The uncached half: everything in the message turns."""
    out: list[str] = []
    for msg in params.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            out.append(content)
        elif isinstance(content, list):
            out.extend(b.get("text", "") for b in content if isinstance(b, dict))
    return "".join(out)


def estimate_cost(requests: list[dict[str, Any]], model: str) -> dict[str, Any]:
    """Price a batch before submitting it.

    `est_usd_uncached` is this same batch priced at STANDARD rates — it is the
    batch-discount comparison, not a no-caching comparison, so est_usd is always
    exactly BATCH_DISCOUNT x est_usd_uncached. Both figures already assume the
    shared prefix is written once and read thereafter.

    Two output figures, because they answer different questions:

      `est_output_tokens` / `est_usd_ceiling` bill every request's full max_tokens.
      This is the number the bill cannot exceed.

      `est_typical_output_tokens` / `est_usd` use config.TYPICAL_OUTPUT_TOKENS — the
      observed size of a real schema-constrained response. This is the number it will
      probably cost, and it is what the CLI leads with, because a ceiling that
      overstates by ~10x gets ignored and then the real number surprises someone.
    """
    if not isinstance(requests, list):
        raise ValueError(f"requests must be a list, got {type(requests).__name__}")
    if model not in C.PRICING:
        raise ValueError(f"no published pricing for model {model!r}")

    in_per_mtok, out_per_mtok = C.PRICING[model]

    est_input_tokens = 0
    est_output_tokens = 0
    est_typical_output_tokens = 0
    billed_input_units = 0.0
    seen_prefixes: set[str] = set()

    for request in requests:
        params = _params_of(request)
        max_tokens = params.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError(
                f"request {request.get('custom_id')!r} has no positive max_tokens"
            )

        # Kind comes from the custom_id prefix, which parse_custom_id already
        # guarantees is one of the known kinds; an unknown one falls back to the
        # generic figure rather than raising, because a cost estimate must never
        # be the thing that stops a run.
        cid = str(request.get("custom_id", ""))
        kind = cid.split(_SEPARATOR, 1)[0] if _SEPARATOR in cid else ""
        est_typical_output_tokens += min(
            max_tokens,
            C.TYPICAL_OUTPUT_TOKENS.get(kind, C.DEFAULT_TYPICAL_OUTPUT_TOKENS),
        )

        prefix = _prefix_text(params)
        prefix_tokens = _est_tokens(prefix)
        volatile_tokens = _est_tokens(_volatile_text(params))

        est_input_tokens += prefix_tokens + volatile_tokens
        est_output_tokens += max_tokens

        # Cache is keyed on the prefix bytes, so requests carrying different
        # system blocks (extract vs verify) each pay their own first write.
        key = st.sha256_text(prefix)
        multiplier = (
            C.CACHE_READ_MULTIPLIER if key in seen_prefixes else C.CACHE_WRITE_MULTIPLIER
        )
        seen_prefixes.add(key)
        billed_input_units += prefix_tokens * multiplier + volatile_tokens

    billed_input_usd = billed_input_units / 1_000_000.0 * in_per_mtok
    ceiling_uncached = billed_input_usd + est_output_tokens / 1_000_000.0 * out_per_mtok
    typical_uncached = (
        billed_input_usd + est_typical_output_tokens / 1_000_000.0 * out_per_mtok
    )
    return {
        "requests": len(requests),
        "est_input_tokens": est_input_tokens,
        "est_output_tokens": est_output_tokens,
        "est_typical_output_tokens": est_typical_output_tokens,
        # est_usd is the likely bill; est_usd_ceiling is the bound. Both batched.
        "est_usd": typical_uncached * C.BATCH_DISCOUNT,
        "est_usd_ceiling": ceiling_uncached * C.BATCH_DISCOUNT,
        # Same batch at standard (non-batch) rates, for the discount comparison.
        "est_usd_uncached": typical_uncached,
    }


# ---------------------------------------------------------------------------
# Submit / poll / collect
# ---------------------------------------------------------------------------
def _client() -> Any:
    """Build a real SDK client. Imported here so the module loads without it."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised only off-CI
        raise RuntimeError(
            "the 'anthropic' package is required to talk to the Batch API; "
            "install it with: pip install -r pipeline/requirements.txt"
        ) from exc
    return anthropic.Anthropic()


def _validate_requests(requests: Any) -> list[dict[str, Any]]:
    """Reject anything the Batch API — or collect() — could not handle."""
    if not isinstance(requests, list) or not requests:
        raise ValueError("requests must be a non-empty list")

    seen: set[str] = set()
    for request in requests:
        params = _params_of(request)
        custom_id = request.get("custom_id")
        parse_custom_id(custom_id)
        if custom_id in seen:
            # Duplicates do not merely fail validation upstream: two results
            # under one key would silently overwrite each other in collect().
            raise ValueError(f"duplicate custom_id: {custom_id!r}")
        seen.add(custom_id)
        if not params.get("model"):
            raise ValueError(f"request {custom_id!r} has no model")
    return requests


def _chunk_requests(requests: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split on whichever safety ceiling bites first: count or serialised size."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0

    for request in requests:
        size = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        too_many = len(current) >= MAX_REQUESTS_PER_BATCH
        too_big = current and current_bytes + size > MAX_BYTES_PER_BATCH
        if too_many or too_big:
            chunks.append(current)
            current, current_bytes = [], 0
        current.append(request)
        current_bytes += size

    if current:
        chunks.append(current)
    return chunks


def submit(requests: list[dict[str, Any]], *, client: Any = None, dry_run: bool = False) -> str:
    """Submit one batch and return its id. `dry_run` returns 'dry-run', calls nothing.

    This returns a single id, so it refuses to submit a request list that would
    need more than one batch: submitting two batches while returning one id
    would strand the second batch's results with no handle to collect them. At
    380 cards the ceilings are three orders of magnitude away — they exist so a
    future full-catalogue sweep fails loudly here instead of at the API.
    """
    _validate_requests(requests)

    chunks = _chunk_requests(requests)
    if len(chunks) > 1:
        raise ValueError(
            f"{len(requests)} requests need {len(chunks)} batches "
            f"(ceilings: {MAX_REQUESTS_PER_BATCH} requests / "
            f"{MAX_BYTES_PER_BATCH // (1024 * 1024)} MB); submit them one batch at a time"
        )

    if dry_run:
        return "dry-run"

    if client is None:
        client = _client()
    batch = client.messages.batches.create(requests=chunks[0])
    batch_id = getattr(batch, "id", None)
    if not isinstance(batch_id, str) or not batch_id:
        raise ValueError("the Batch API returned no batch id")
    return batch_id


def poll(batch_id: str, *, client: Any = None) -> str:
    """Return the batch's processing_status ('in_progress' | 'canceling' | 'ended')."""
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")
    if client is None:
        client = _client()
    batch = client.messages.batches.retrieve(batch_id)
    status = getattr(batch, "processing_status", None)
    if not isinstance(status, str) or not status:
        raise ValueError(f"batch {batch_id} returned no processing_status")
    return status


def _block_field(block: Any, field: str) -> Any:
    """Read a content block written either as an SDK object or a plain dict."""
    if isinstance(block, dict):
        return block.get(field)
    return getattr(block, field, None)


def _first_text(message: Any) -> str | None:
    content = _block_field(message, "content")
    if not isinstance(content, (list, tuple)):
        return None
    for block in content:
        if _block_field(block, "type") == "text":
            text = _block_field(block, "text")
            if isinstance(text, str):
                return text
    return None


def _failure_detail(result: Any) -> str:
    """Best-effort human detail for a non-succeeded result."""
    error = getattr(result, "error", None)
    if error is None:
        return ""
    etype = getattr(error, "type", None) or _block_field(error, "type")
    emsg = getattr(error, "message", None) or _block_field(error, "message")
    return ": ".join(str(p) for p in (etype, emsg) if p)


def collect(batch_id: str, *, client: Any = None) -> dict[str, dict[str, Any]]:
    """Collect a finished batch, keyed by custom_id.

    Results arrive in ANY order — keying by position is the single most common
    way to corrupt a batch pipeline, so nothing here ever indexes results.

    A succeeded result whose body is not valid JSON comes back ok=False with the
    error rather than raising: one unparseable card must not throw away the 379
    good ones we already paid for.
    """
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")
    if client is None:
        client = _client()

    out: dict[str, dict[str, Any]] = {}
    for result in client.messages.batches.results(batch_id):
        custom_id = getattr(result, "custom_id", None)
        if not isinstance(custom_id, str) or not custom_id:
            raise ValueError(f"batch {batch_id} returned a result with no custom_id")
        if custom_id in out:
            raise ValueError(f"batch {batch_id} returned duplicate custom_id {custom_id!r}")

        inner = getattr(result, "result", None)
        rtype = getattr(inner, "type", None)

        if rtype == "succeeded":
            text = _first_text(getattr(inner, "message", None))
            if text is None:
                out[custom_id] = {
                    "ok": False,
                    "data": None,
                    "error": "succeeded but no text block in the message",
                }
                continue
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                out[custom_id] = {
                    "ok": False,
                    "data": None,
                    "error": f"malformed JSON in response: {exc}",
                }
                continue
            out[custom_id] = {"ok": True, "data": data, "error": ""}
        elif rtype in ("errored", "canceled", "expired"):
            detail = _failure_detail(inner)
            out[custom_id] = {
                "ok": False,
                "data": None,
                "error": f"{rtype}: {detail}" if detail else rtype,
            }
        else:
            out[custom_id] = {
                "ok": False,
                "data": None,
                "error": f"unknown result type: {rtype!r}",
            }
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _kind_of(requests: list[dict[str, Any]]) -> str:
    kinds = {parse_custom_id(r["custom_id"])[0] for r in requests}
    if len(kinds) != 1:
        raise ValueError(f"a batch must be all one kind, got {sorted(kinds)}")
    return kinds.pop()


def _read_requests(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValueError(f"no such requests file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    return _validate_requests(data)


def _cmd_submit(args: argparse.Namespace) -> int:
    requests = _read_requests(pathlib.Path(args.requests))
    kind = _kind_of(requests)
    model = requests[0]["params"]["model"]

    est = estimate_cost(requests, model)
    print(
        f"{est['requests']} {kind} requests · "
        f"~{est['est_input_tokens']:,} in / ~{est['est_output_tokens']:,} out (ceiling) · "
        f"~${est['est_usd']:.2f} batch (${est['est_usd_uncached']:.2f} standard)"
    )

    batch_id = submit(requests, dry_run=args.dry_run)
    if args.dry_run:
        print("dry run — nothing submitted")
        return 0

    batch_state = st.load_batch_state()
    st.add_batch(
        batch_state, batch_id=batch_id, kind=kind, submitted_at=_now(), count=len(requests)
    )
    st.save_batch_state(batch_state)
    print(f"submitted {batch_id} ({kind}, {len(requests)} requests)")
    return 0


def _cmd_poll(args: argparse.Namespace) -> int:
    batch_state = st.load_batch_state()
    if args.batch_id:
        targets = [{"batch_id": args.batch_id, "kind": "?"}]
    else:
        targets = st.pending_batches(batch_state)
    if not targets:
        print("no batches in flight")
        return 0

    changed = False
    for entry in targets:
        batch_id = entry["batch_id"]
        status = poll(batch_id)
        print(f"{batch_id} ({entry.get('kind', '?')}): {status}")
        if status == "ended" and st.mark_batch(batch_state, batch_id, "ended"):
            changed = True
    if changed:
        st.save_batch_state(batch_state)
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    results = collect(args.batch_id)
    ok = sum(1 for r in results.values() if r["ok"])
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"{len(results)} results ({ok} ok, {len(results) - ok} failed) -> {out_path}")

    batch_state = st.load_batch_state()
    if st.mark_batch(batch_state, args.batch_id, "collected"):
        st.save_batch_state(batch_state)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="batch.py",
        description="Anthropic Message Batches wrapper for the weekly card refresh.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Three short jobs: submit -> poll (until ended) -> collect.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit", help="submit a prepared request file as one batch")
    s.add_argument("--requests", required=True, metavar="FILE",
                   help="JSON file holding a list of request dicts")
    s.add_argument("--dry-run", action="store_true",
                   help="estimate and validate only; call nothing")
    s.set_defaults(func=_cmd_submit)

    s = sub.add_parser("poll", help="report processing_status for in-flight batches")
    s.add_argument("--batch-id", help="one batch; default is every batch still submitted")
    s.set_defaults(func=_cmd_poll)

    s = sub.add_parser("collect", help="collect a finished batch, keyed by custom_id")
    s.add_argument("--batch-id", required=True)
    s.add_argument("--out", required=True, metavar="FILE")
    s.set_defaults(func=_cmd_collect)

    args = p.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
