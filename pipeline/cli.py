#!/usr/bin/env python3
"""
The pipeline entrypoint. Three commands, one per CI stage.

Usage:
    python3 pipeline/cli.py refresh [--card-id ID] [--limit N] [--force] [--dry-run]
    python3 pipeline/cli.py advance [--dry-run]
    python3 pipeline/cli.py news-watch [--issuer NAME] [--force] [--dry-run]
    python3 pipeline/cli.py metrics [--write]
    python3 pipeline/cli.py discover [--issuer NAME] [--no-verify] [--write]

Design, in one paragraph:

`refresh` fetches every active card's issuer document, hashes the normalised text,
and submits an extraction batch for ONLY the cards whose bytes moved since last week.
`advance` is idempotent and runs on a short cron: it looks at what is in flight and
either collects the extraction batch and submits the verification batch, or collects
the verification batch, diffs it against seed/cards.json and writes a patch for a
human to merge. `news-watch` polls the pages where issuers publish revisions and
drafts feed items when one changes.

Nothing here ever writes to `main`, and nothing publishes to users without a person
merging a PR. That is not caution for its own sake: of 18 changes a first pass called
"confirmed at the issuer", an adversarial second pass refuted 6.

Exit codes: 0 ok, 1 data error, 2 config/missing dependency.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline import batch as B          # noqa: E402
from pipeline import config as C         # noqa: E402
from pipeline import diff as D           # noqa: E402
from pipeline import fetch as F          # noqa: E402
from pipeline import newsgen as N        # noqa: E402
from pipeline import report as R         # noqa: E402
from pipeline import sources as S        # noqa: E402
from pipeline import state as ST         # noqa: E402


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


# ANSI-free by default: this output is read in GitHub job summaries as often as in a
# terminal, and escape codes render as noise there.
def ok(msg: str) -> None:
    print(f"OK   {msg}")


def warn(msg: str) -> None:
    print(f"WARN {msg}")


def fail(msg: str) -> None:
    print(f"FAIL {msg}")


def head(msg: str) -> None:
    print(f"\n=== {msg} ===")


def _work_dir() -> pathlib.Path:
    C.WORK_DIR.mkdir(parents=True, exist_ok=True)
    return C.WORK_DIR


# ---------------------------------------------------------------------------
# Spend ceiling — the loud half
#
# batch.submit() already refuses above config.MAX_CYCLE_USD by raising, and that
# refusal is what actually protects the card. What it could not do was be SEEN: the
# exception surfaced as one `FAIL batch submission failed: ...` line, indistinguishable
# from an expired API key, in a log whose job summary prints only the first 120 lines —
# and on a 350-card week the fetch warnings alone can push that line past 120.
#
# So the ceiling is checked here too, before submitting, and reports itself through the
# three channels a person actually reads: the log, a red ::error:: annotation at the top
# of the run, and its own block in the job summary.
# ---------------------------------------------------------------------------
def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def _annotate(title: str, message: str) -> None:
    """Raise a red annotation on the run itself. A no-op outside Actions."""
    if not _in_actions():
        return
    # Annotations are one line; newlines terminate the command and lose the rest.
    flat = " ".join(message.split())
    print(f"::error title={title}::{flat}")


def _summary(markdown: str) -> None:
    """Append a block to the GitHub job summary. A no-op outside Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip() + "\n\n")
    except OSError:  # pragma: no cover - a summary we cannot write must not kill the run
        pass


def _budgeted(est: dict) -> float:
    """What this batch is allowed to be assumed to cost.

    est_usd is a single-point fit against ONE observed bill and it missed that bill by
    38% in the expensive direction. Gating on it directly makes "$15" a tripwire on a
    mean, which roughly half of future batches will exceed. Gating on est_usd_ceiling
    instead overshoots by ~1.76x on a real mix and would refuse ordinary weeks. The
    margin factor is the middle, and it is the number every refusal quotes.
    """
    return float(est["est_usd"]) * C.ESTIMATE_SAFETY_FACTOR


def _ceiling_verdict(
    est: dict,
    limit: "float | None",
    *,
    stage: str,
    already_committed: float = 0.0,
    reserve: float = 0.0,
) -> str:
    """Return "" when this cycle is affordable, otherwise a human-readable refusal.

    THE CEILING IS A CYCLE BUDGET, NOT A PER-BATCH ONE. One Monday cycle is two paid
    submissions from two different workflows, and checking each independently against
    the same constant authorised twice what the constant said. So:

      `already_committed`  what earlier stages of THIS cycle have already committed,
                           in budgeted dollars, read back from batch.json.
      `reserve`            what this stage knows is still coming. Stage 1 reserves the
                           verification batch it is about to make inevitable; without
                           that, a batch that only just fits pays for extraction and
                           then finds verification unaffordable, which strands the
                           money in the most expensive way available.

    Returns the message rather than printing it so the caller decides whether this is a
    refusal (a real run) or a forecast (a dry run). The wording says REFUSING, never
    "trimming": the batch is not shortened to fit, because a silently shortened sweep
    reads exactly like a cheap week and the cards it dropped would be invisible.
    """
    if limit is None:
        return ""
    budgeted = _budgeted(est)
    total = already_committed + budgeted + reserve
    if total <= limit:
        return ""

    parts = [f"this {stage} batch ${budgeted:.2f}"]
    if already_committed:
        parts.insert(0, f"already committed this cycle ${already_committed:.2f}")
    if reserve:
        parts.append(f"verification still to come ${reserve:.2f}")

    return (
        f"{stage} would cost an estimated ${est['est_usd']:.2f} for "
        f"{est['requests']} cards — ${budgeted:.2f} with the "
        f"{C.ESTIMATE_SAFETY_FACTOR}x margin the estimator has historically needed, "
        f"and ${est['est_usd_ceiling']:.2f} at the absolute bound. "
        f"The CYCLE totals ${total:.2f} ({'; '.join(parts)}), "
        f"above the ${limit:.2f} cycle spend limit. REFUSING to submit. "
        f"Nothing has been spent. To allow it, raise MAX_CYCLE_USD in "
        f"pipeline/config.py (one line, merged to dev) to at least ${total:.2f} — that "
        f"is the whole cycle, not one batch. A manual run may instead pass "
        f"--max-usd {total:.2f}, but it must be passed to BOTH weekly-refresh and "
        f"pipeline-advance or the extraction is paid for and never verified."
    )


def _report_ceiling_refusal(message: str, *, forecast: bool) -> None:
    """Print the refusal everywhere a person might be looking."""
    verb = "WOULD REFUSE (dry run — nothing was going to be submitted anyway)" \
        if forecast else "REFUSED"
    fail(f"SPEND CEILING {verb}")
    fail(message)
    if forecast:
        _summary(f"## Spend ceiling — a real run would refuse\n\n{message}")
        return
    _annotate("Spend ceiling exceeded — nothing submitted, nothing spent", message)
    _summary(
        "## :octagonal_sign: Spend ceiling exceeded — nothing was submitted\n\n"
        f"{message}\n\n"
        "The ceiling lives in **`pipeline/config.py`**, on the `MAX_CYCLE_USD = ` line. "
        "That single constant is what the Monday cron enforces — both scheduled "
        "workflows check out `dev`, so changing it there is enough and no workflow file "
        "needs editing. It caps the WHOLE cycle: the extraction batch and the "
        "verification batch that follows it two hours later, together."
    )


# ---------------------------------------------------------------------------
# refresh — stage 1
# ---------------------------------------------------------------------------
def _report_fetch_failures(failures: "list[tuple[str, str, str]]") -> None:
    """Every failed fetch, grouped by host and reason.

    This used to print `warn(...)` per card behind `if failed <= 10`, so a run
    that lost 38 cards showed 10 lines — all of them the same host, with no hint
    that 28 more existed or that they were two whole issuers. The cap was there
    to stop a bad week flooding the log; grouping achieves that without hiding
    the shape, because the failures that matter are correlated by host, not
    scattered across cards.

    Reads as "19 cards behind www.bobcard.co.in, all one TLS fault" rather than
    ten unrelated-looking card ids.
    """
    if not failures:
        return

    from urllib.parse import urlsplit

    groups: dict[tuple[str, str], list[str]] = {}
    for card_id, url, err in failures:
        host = (urlsplit(url).hostname or "?").lower()
        groups.setdefault((host, _reason_of(err)), []).append(card_id)

    warn(f"{len(failures)} source(s) could not be read, across "
         f"{len({h for h, _ in groups})} host(s):")
    for (host, reason), ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        warn(f"  {len(ids):>3} cards  {host}  — {reason}")
        # Three is enough to go and check one by hand; the host and reason are
        # what actually identify the problem.
        sample = ", ".join(sorted(ids)[:3])
        more = f", +{len(ids) - 3} more" if len(ids) > 3 else ""
        warn(f"          e.g. {sample}{more}")


def _reason_of(err: str) -> str:
    """Collapse an error string to the class of fault, so grouping works."""
    low = (err or "").lower()
    if "certificate_verify_failed" in low:
        return "TLS chain incomplete (server omits its intermediate)"
    if "javascript shell" in low:
        return "JavaScript-rendered page — no static text to read"
    if "no text extracted" in low:
        return "fetched but no text could be extracted"
    if low.startswith("http "):
        return err.strip()
    return (err or "unknown").split(":")[0].strip() or "unknown"


def _refuse_if_batch_in_flight(args: argparse.Namespace) -> int:
    """Refuse to start a second cycle while one is still in flight. 0 = go ahead.

    Nothing used to stop this. cmd_refresh records changed cards as status 'fetched',
    and has_changed() returns True for any status that is not 'done' — so a second run
    sees exactly the same cards as changed and pays for every one of them again. Every
    path reaches it: "Re-run failed jobs" on a weekly-refresh whose only failure was the
    final `git push origin HEAD:dev`; a second workflow_dispatch; a scheduled run
    following a manual one. `concurrency: cancel-in-progress: false` serialises the
    runs, it does not suppress the second one.

    Then `advance` iterates pending_batches(kind='extract') and runs _advance_extract
    per batch, so two stranded extraction batches produce two more paid verification
    submissions: four paid submissions for one week's work.
    """
    pending = ST.pending_batches(ST.load_batch_state())
    if not pending:
        return 0
    if getattr(args, "force_resubmit", False):
        warn(f"{len(pending)} batch(es) already in flight — --force-resubmit given, "
             f"submitting anyway. This WILL be billed twice for any card in both.")
        return 0

    fail(f"{len(pending)} batch(es) already in flight — refusing to submit another. "
         f"The cards below are already paid for; submitting again bills them twice.")
    for b in pending:
        fail(f"  {b.get('batch_id')}  kind={b.get('kind')}  "
             f"{b.get('request_count')} requests  submitted {b.get('submitted_at')}")
    fail("Collect them with: python3 pipeline/cli.py advance")
    fail("If you are certain the in-flight batches are dead and you want to pay again, "
         "re-run with --force-resubmit.")
    _annotate(
        "Refusing to submit — a batch is already in flight",
        f"{len(pending)} batch(es) from an earlier run are still marked submitted. "
        f"Run `pipeline/cli.py advance` to collect them. Nothing was spent.",
    )
    _summary(
        "## :octagonal_sign: Nothing submitted — a batch is already in flight\n\n"
        + "\n".join(
            f"- `{b.get('batch_id')}` ({b.get('kind')}, {b.get('request_count')} requests, "
            f"submitted {b.get('submitted_at')})" for b in pending
        )
        + "\n\nThose requests are already paid for. `pipeline-advance.yml` collects them "
          "on its 2-hourly cron; submitting a second batch would bill the same cards "
          "again."
    )
    return 2


def cmd_refresh(args: argparse.Namespace) -> int:
    # Before the network, before anything: is last cycle still running?
    rc = _refuse_if_batch_in_flight(args)
    if rc and not args.dry_run:
        return rc

    head("Resolving sources")
    try:
        cards = S.load_cards()
    except (ValueError, OSError) as exc:
        fail(f"could not read seed/cards.json: {exc}")
        return 1

    overrides = S.load_overrides(REPO / "pipeline" / "sources_overrides.json")
    srcs = S.resolve_sources(cards, overrides=overrides)

    if args.card_id:
        srcs = [s for s in srcs if s.card_id == args.card_id]
        if not srcs:
            fail(f"no active card with id {args.card_id!r}")
            return 1
    if args.limit:
        srcs = srcs[: args.limit]

    cov = S.coverage_report(srcs)
    ok(f"{cov['resolved']}/{cov['total']} cards have an issuer URL")
    for reason, n in sorted(cov["by_reason"].items(), key=lambda kv: -kv[1]):
        if reason:
            warn(f"{n:>4} unresolved: {reason}")

    head("Fetching")
    st = ST.load_state()
    resolved = [s for s in srcs if s.url]
    changed: list[tuple[S.Source, str]] = []
    failures: list[tuple[str, str, str]] = []
    unchanged = failed = 0

    # Fetch first, concurrently across issuers; then walk the cards in seed
    # order to record state. Two reasons for that split, both load-bearing:
    #
    #   - `st` is a plain dict mutated by ST.record_source. Writing to it from
    #     worker threads would be a data race for no gain — the network is the
    #     slow part, not the bookkeeping.
    #   - The per-card output stays in seed order, so a diff of two runs is
    #     readable and the "first 10 warnings" cap shows the same cards it
    #     always did rather than whichever host happened to finish first.
    #
    # fetch_many deduplicates, so the 373 cards sharing 196 URLs cost 196
    # fetches, not 373.
    def _tick(done: int, total: int) -> None:
        if done % 25 == 0:
            print(f"     ... {done}/{total} sources")

    fetched = F.fetch_many([s.url for s in resolved], on_progress=_tick)

    for i, s in enumerate(resolved, 1):
        res = fetched.get(s.url) or F.Fetched(url=s.url, error="not fetched")
        if not res.ok or not res.text:
            failed += 1
            ST.record_source(
                st, s.card_id, url=s.url, content_sha256=None,
                fetched_at=_now(), status="fetch_failed", note=res.error or "empty text",
            )
            failures.append((s.card_id, s.url, res.error or "no text extracted"))
            continue

        if not args.force and not ST.has_changed(st, s.card_id, res.text_sha256):
            unchanged += 1
            # touch_source, not record_source. record_source REPLACES the entry, so
            # every key the row had earned — why it finished, how long its document
            # was, whether that document was this card's own page — had to be
            # hand-carried back in or it was silently erased, and each new key was one
            # more thing to remember. This updates the two fields a re-fetch actually
            # learns and leaves the rest alone.
            ST.touch_source(st, s.card_id, fetched_at=_now(),
                            content_sha256=res.text_sha256)
            continue

        changed.append((s, res.text))
        # "fetched", not "done": the bytes are in hand but nothing has been extracted
        # from them yet. Only stage 3 may mark a card done, or a batch that expires
        # silently retires the card forever.
        #
        # text_chars is recorded now because it can only be measured now, and stage 3
        # needs it: "the extractor found nothing in this document" is a finding about a
        # card only when a document was actually read. 188 characters of navigation
        # menu is not one.
        ST.record_source(
            st, s.card_id, url=s.url, content_sha256=res.text_sha256,
            fetched_at=_now(), status="fetched", text_chars=len(res.text),
        )

    ok(f"fetched {len(resolved)}: {len(changed)} changed, {unchanged} unchanged, {failed} failed")
    if unchanged and not args.force:
        ok(f"skipped {unchanged} cards whose source bytes did not move — this is the saving")
    _report_fetch_failures(failures)

    if not changed:
        ok("nothing to extract this week")
        if not args.dry_run:
            ST.save_state(st)
        return 0

    head("Building extraction batch")
    reqs = [
        B.build_extract_request(s.card_id, s.card_name, s.url, text)
        for s, text in changed
    ]
    est = B.estimate_cost(reqs, C.EXTRACT_MODEL)
    limit = _max_usd(args)
    # Verification is not optional and not free. Submitting this batch commits us to it,
    # so the money it will cost is reserved HERE, where refusing is still free. Without
    # the reservation a batch that only just fits pays for extraction and then finds
    # verification unaffordable two hours later — the extraction is spent, the answer is
    # never produced, and the recovery advice is a re-collect that re-submits and pays
    # again.
    reserve = _budgeted(est) * C.VERIFY_COST_RATIO
    ok(f"{est['requests']} requests, ~{est['est_input_tokens']:,} input tokens")
    ok(f"estimated ${est['est_usd']:.2f} — ${_budgeted(est):.2f} with the "
       f"{C.ESTIMATE_SAFETY_FACTOR}x margin, ${est['est_usd_ceiling']:.2f} at the bound")
    ok(f"verification reserved at ${reserve:.2f} — cycle "
       f"${_budgeted(est) + reserve:.2f}")
    ok(f"cycle spend limit ${limit:.2f}" if limit is not None else "spend limit DISABLED")

    over = _ceiling_verdict(est, limit, stage="extraction", reserve=reserve)

    if args.dry_run:
        # A dry run is how a person finds out what Monday will do, so it has to answer
        # the money question too — silently reporting an estimate the scheduled run will
        # then refuse is the surprise this whole exercise exists to remove.
        if over:
            _report_ceiling_refusal(over, forecast=True)
        else:
            ok("this is within the spend limit — the scheduled run would submit it")
        ok("dry run — nothing submitted, nothing spent")
        (_work_dir() / "extract_requests.json").write_text(
            json.dumps(reqs, indent=2)[:2_000_000], encoding="utf-8"
        )
        # A forecast that says the real run will refuse must not be green. This used to
        # `return 0` unconditionally, so a workflow_dispatch dry run showed a green tick
        # with `FAIL SPEND CEILING WOULD REFUSE` buried in a log the job summary
        # truncates — the exact read-the-tail failure the rest of this work removes.
        return 2 if over else 0

    if over:
        _report_ceiling_refusal(over, forecast=False)
        # Exit non-zero so the step, the job and the run all go red. A ceiling breach
        # that returned 0 would sit in a green run and be found by the invoice.
        return 2

    # Write the hashes BEFORE submitting. If the process dies between the API call and
    # the state write, the batch exists and is billed while nothing on disk records
    # which cards it covers — paid work with no handle. Recording first can only ever
    # cost an unnecessary re-read, which is the cheap direction.
    ST.save_state(st)

    try:
        batch_id = B.submit(reqs, max_usd=limit)
    except Exception as exc:  # noqa: BLE001 - surface any SDK/transport failure as exit 2
        fail(f"batch submission failed: {exc}")
        return 2

    ok(f"submitted extraction batch {batch_id}")
    bst = ST.load_batch_state()
    ST.add_batch(bst, batch_id=batch_id, kind="extract", submitted_at=_now(),
                 count=len(reqs), budgeted_usd=_budgeted(est))
    ST.save_batch_state(bst)
    ST.save_state(st)

    ok("stage 1 complete — pipeline-advance.yml will collect when the batch ends")
    return 0


# ---------------------------------------------------------------------------
# advance — stages 2 and 3
# ---------------------------------------------------------------------------
def _recollect(bst: dict, batch_id: str, *, force: bool = False) -> int:
    """Put an already-collected batch back in front of the collector.

    Why this exists. A batch is marked `collected` the moment its results are
    read, and `pending_batches()` returns only `submitted` — so if anything
    downstream of collection fails, the batch is finished as far as the pipeline
    is concerned while nothing has actually been produced from it. That is not
    hypothetical: on 2026-08-17 stage 2 collected 371 extractions, discarded 232
    of them because the runner had no pdftotext, and exited. The results sat in
    extractions.json with no code path that would ever read them again, and the
    only route forward was re-running the weekly refresh — paying a second time
    to re-extract output we already held. Recovering it took a hand-edit of
    tool-managed state, which is exactly the kind of thing that should be a
    command.

    THE COLLECT ITSELF COSTS NOTHING — and that is not the same as this command
    costing nothing. Retrieving an ended batch's results is a read, and Anthropic
    keeps them for 29 days, so the model does not run again.

    But reopening an EXTRACTION batch puts it back in front of _advance_extract,
    which after collecting goes straight on to build and SUBMIT a fresh paid
    verification batch. If verification has already been submitted for this
    extraction, that is a second bill for the same week's work, and each half is
    independently under the ceiling so nothing else refuses. So this refuses that
    case outright unless --force is given, and says plainly what the free case is.
    """
    match = next((b for b in bst.get("batches", []) if b.get("batch_id") == batch_id), None)
    if match is None:
        fail(f"no tracked batch {batch_id!r}")
        known = [b.get("batch_id") for b in bst.get("batches", [])]
        if known:
            warn("tracked batches: " + ", ".join(str(k) for k in known))
        return 1
    if match.get("status") == "submitted":
        ok(f"{batch_id} is already pending — nothing to reopen")
        return 0

    kind = match.get("kind")
    downstream = _verify_batch_for(bst, batch_id) if kind == "extract" else None
    if downstream is not None and not force:
        fail(f"{batch_id} is an EXTRACTION batch and verification batch "
             f"{downstream.get('batch_id')} was already built from it.")
        fail("Re-collecting it would re-enter _advance_extract and SUBMIT AND PAY FOR "
             "verification a second time. That is not free. Refusing.")
        fail(f"If the verification batch is what you actually need back, reopen that "
             f"one instead: python3 pipeline/cli.py advance --recollect "
             f"{downstream.get('batch_id')}")
        fail("To reopen the extraction anyway and accept a second verification bill, "
             "add --force.")
        return 2

    ST.mark_batch(bst, batch_id, "submitted")
    ST.save_batch_state(bst)
    ok(f"reopened {kind} batch {batch_id} "
       f"({match.get('request_count')} requests) — the next advance will re-collect it")
    ok("re-reading the results themselves is free; the model does not run again")
    if kind == "extract":
        if downstream is None:
            ok("no verification batch was built from this extraction, so the next "
               "advance will build one — that submission IS billed")
        else:
            warn("--force given: the next advance will submit a SECOND verification "
                 "batch for these cards and it will be billed")
    return 0


def cmd_advance(args: argparse.Namespace) -> int:
    bst = ST.load_batch_state()

    if getattr(args, "recollect", ""):
        return _recollect(bst, args.recollect, force=getattr(args, "force", False))

    for pending in ST.pending_batches(bst, kind="extract"):
        rc = _advance_extract(pending, bst, args)
        if rc != 0:
            return rc
    for pending in ST.pending_batches(bst, kind="verify"):
        rc = _advance_verify(pending, bst, args)
        if rc != 0:
            return rc

    if not ST.pending_batches(bst):
        ok("nothing in flight")
    return 0


def _max_usd(args: argparse.Namespace) -> "float | None":
    """The spend ceiling for this invocation.

    Absent flag -> config.MAX_CYCLE_USD. `--max-usd 0` disables the check, which
    is spelled as an explicit zero rather than a separate --no-limit flag so it
    shows up verbatim in a workflow file and in `gh run view`, where somebody
    reviewing why a big batch went through can see it.
    """
    raw = getattr(args, "max_usd", None)
    if raw is None:
        return C.MAX_CYCLE_USD
    return None if float(raw) <= 0 else float(raw)


def _news_max_usd(args: argparse.Namespace) -> "float | None":
    """The spend ceiling for one news-watch run.

    A separate constant from the card cycle on purpose: news-watch fires daily and pays
    standard rates, card refresh fires weekly and pays batch rates. One number cannot
    honestly describe both bills, and config.py used to claim it did.
    """
    raw = getattr(args, "max_usd", None)
    if raw is None:
        return C.MAX_NEWS_USD
    return None if float(raw) <= 0 else float(raw)


def _redocument(
    card_id: str,
    sources_state: dict,
    fetched: "dict[str, F.Fetched] | None" = None,
) -> tuple[str, str]:
    """Re-read a card's source and return (text, why_not) — text is "" when unusable.

    `fetched` is a {url: Fetched} map from F.fetch_many, and passing it is what
    keeps stage 2 inside its job timeout. Fetching here, once per card, is
    strictly serial: 371 cards took over 30 minutes and the job was killed
    mid-loop, discarding 371 already-paid-for extractions and reaching no
    submit at all. Stage 1 hit the identical wall and fixed it with fetch_many;
    this is the same fix, one stage later. Falls back to a direct fetch when no
    map is supplied so a single-card debugging call still works.

    Verification must judge the extractor's quotes against the EXACT bytes the extractor
    read. Stage 1 wrote those bytes to `.pipeline-work/`, which is gitignored scratch on a
    runner that no longer exists by the time stage 2 runs — so this re-fetches instead, and
    uses the committed content hash to prove the bytes are the same ones.

    If the page moved in between, we do not verify against the new bytes and quietly hope:
    the card is skipped this cycle and picked up on the next Monday, when extraction and
    verification will both see the newer page. Skipping is the safe direction — the failure
    it prevents is an observation being marked verified against a document that no longer
    contains it.
    """
    entry = ST.get_source(sources_state, card_id) or {}
    url = entry.get("url") or ""
    expected = entry.get("content_sha256") or ""
    if not url:
        return "", "no recorded source url"
    if not expected:
        return "", "no recorded content hash"

    res = (fetched or {}).get(url) or F.fetch_source(url)
    if not res.ok or not res.text:
        return "", f"re-fetch failed: {res.error or 'no text'}"
    if res.text_sha256 != expected:
        return "", "source changed since extraction — deferred to the next run"
    return res.text, ""


def _verify_batch_for(bst: dict, extract_batch_id: str) -> dict | None:
    """The verification batch already built from this extraction, if there is one.

    Collecting an extraction batch does not just read results — it goes straight on to
    BUILD AND SUBMIT a paid verification batch. So anything that puts an already-
    collected extraction back in front of the collector (`advance --recollect`, a lost
    state push, a re-run of a failed job) submits and pays for verification a second
    time. Batches are recorded in submission order, so the verify batch belonging to an
    extraction is the first verify batch recorded after it.
    """
    batches = bst.get("batches", [])
    for i, b in enumerate(batches):
        if b.get("batch_id") == extract_batch_id:
            for later in batches[i + 1:]:
                if later.get("kind") == "verify":
                    return later
            return None
    return None


def _advance_extract(pending: dict, bst: dict, args: argparse.Namespace) -> int:
    bid = pending["batch_id"]
    head(f"Extraction batch {bid}")

    # Verification is the paid half that follows collection. If one has already been
    # built from this extraction, collecting it again would submit and pay for a second
    # one — each independently under the ceiling, so nothing else refuses.
    already = _verify_batch_for(bst, bid)
    if already is not None:
        fail(f"{bid} has already produced verification batch "
             f"{already.get('batch_id')} ({already.get('request_count')} requests, "
             f"status {already.get('status')}). Collecting it again would submit and "
             f"PAY FOR verification a second time. Refusing.")
        fail("Marking this extraction collected. If the verification batch is genuinely "
             "lost, reopen THAT one: "
             f"python3 pipeline/cli.py advance --recollect {already.get('batch_id')}")
        ST.mark_batch(bst, bid, "collected")
        ST.save_batch_state(bst)
        return 2

    status = B.poll(bid)
    ok(f"status: {status}")
    if status != "ended":
        ok("still processing — will retry on the next cron")
        return 0

    results = B.collect(bid)
    good = {k: v for k, v in results.items() if v.get("ok")}
    bad = {k: v for k, v in results.items() if not v.get("ok")}
    ok(f"collected {len(good)} extractions, {len(bad)} failed")
    for k, v in list(bad.items())[:10]:
        warn(f"{k}: {v.get('error')}")

    # Reconcile against what we submitted. collect() keys by custom_id and has no view of
    # the request list, so a result that never came back is otherwise indistinguishable
    # from a card that legitimately had nothing to report — and we have already paid for
    # it. Fail rather than advance, so a re-run can retry.
    expected = pending.get("request_count") or 0
    if expected and len(results) != expected:
        fail(f"{bid}: submitted {expected} requests but the batch returned {len(results)} "
             f"— {expected - len(results)} paid-for cards are missing. Not advancing.")
        return 1
    if results and not good:
        fail("every extraction failed — infrastructure, not the data. Not advancing.")
        return 1

    # Tracked, not scratch — stage 3 is a different cron run on a different runner.
    C.EXTRACTIONS.parent.mkdir(parents=True, exist_ok=True)
    C.EXTRACTIONS.write_text(
        json.dumps(results, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    ST.mark_batch(bst, bid, "collected")

    head("Building verification batch")
    sources_state = ST.load_state()
    # Stage 1 keyed its results by custom_id, which is sanitised and may carry a hash
    # suffix, so it is not always recoverable by string surgery. Rebuild the map from the
    # committed source state instead — that is the authoritative card_id list.
    by_custom_id = {
        B.build_extract_request(cid, "x", "https://x.invalid", "x" * 200)["custom_id"]: cid
        for cid in sources_state.get("sources", {})
        if not cid.startswith("__watch__")
    }

    # Re-fetch every source ONCE, concurrently, before building anything.
    #
    # Verification re-reads each card's document to prove the extractor's quotes
    # against the exact bytes it saw. Doing that inside the loop is one serial
    # fetch per card, which on 371 cards ran past this job's 30-minute timeout
    # and was killed part-way — throwing away 371 extractions that had already
    # been paid for and submitting nothing. fetch_many deduplicates and runs one
    # worker per host, so this is the same shape of fix stage 1 already carries,
    # and the same politeness guarantee: never two concurrent requests to one
    # issuer.
    wanted: list[str] = []
    for cid in good:
        card_id = by_custom_id.get(cid)
        entry = ST.get_source(sources_state, card_id) if card_id else None
        url = (entry or {}).get("url")
        if url:
            wanted.append(url)

    def _vtick(done: int, total: int) -> None:
        if done % 25 == 0:
            print(f"     ... re-read {done}/{total} sources")

    refetched = F.fetch_many(wanted, on_progress=_vtick) if wanted else {}

    vreqs = []
    skipped: dict[str, int] = {}
    for cid, res in good.items():
        card_id = by_custom_id.get(cid)
        if card_id is None:
            skipped["unmapped custom_id"] = skipped.get("unmapped custom_id", 0) + 1
            continue
        obs = (res.get("data") or {}).get("observations") or []
        if not obs:
            continue
        text, why = _redocument(card_id, sources_state, refetched)
        if not text:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        vreqs.append(B.build_verify_request(card_id, text, obs))

    for why, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
        warn(f"{n:>4} cards skipped: {why}")

    if not vreqs:
        ok("no observations to verify — nothing proposed this week")
        ST.save_batch_state(bst)
        return 0

    est = B.estimate_cost(vreqs, C.VERIFY_MODEL)
    limit = _max_usd(args)
    # What stage 1 already committed for this cycle, read back off batch.json. The two
    # halves run in different jobs hours apart, so this is the only way the ceiling can
    # mean one cycle rather than one batch.
    committed = ST.cycle_committed_usd(bst, since_batch_id=bid)
    ok(f"{est['requests']} verification requests, estimated ${est['est_usd']:.2f} "
       f"— ${_budgeted(est):.2f} with the {C.ESTIMATE_SAFETY_FACTOR}x margin, "
       f"${est['est_usd_ceiling']:.2f} at the bound")
    ok(f"already committed this cycle ${committed:.2f} — cycle total "
       f"${committed + _budgeted(est):.2f}")
    ok(f"cycle spend limit ${limit:.2f}" if limit is not None else "spend limit DISABLED")

    # Verification is the SECOND half of the week's bill and it runs from a different
    # cron, so a ceiling applied only at stage 1 would let the expensive half through
    # unattended. Same constant, same refusal, same loudness — and now the same budget.
    over = _ceiling_verdict(est, limit, stage="verification", already_committed=committed)

    if args.dry_run:
        if over:
            _report_ceiling_refusal(over, forecast=True)
        ok("dry run — not submitting verification")
        ST.save_batch_state(bst)
        return 0

    if over:
        _report_ceiling_refusal(over, forecast=False)
        # This is the ONE case where --recollect really is free: verification was never
        # submitted, so re-collecting the extraction re-enters this same code path with
        # nothing already paid for downstream. Say that explicitly — the advice is wrong
        # in every other case, and it used to be printed as though it were general.
        recover = (
            f"The {len(good)} extractions are already paid for and are committed as "
            f"collected. Verification was NOT submitted, so nothing downstream of them "
            f"has been billed — and only because of that, re-collecting is free. After "
            f"raising MAX_CYCLE_USD, recover them with: "
            f"python3 pipeline/cli.py advance --recollect {bid}"
        )
        fail(recover)
        _summary(
            "**Recovering the paid-for work.** Verification was never submitted, so "
            "the extractions are the only thing billed and re-collecting them costs "
            f"nothing:\n\n`python3 pipeline/cli.py advance --recollect {bid}`\n\n"
            "Raise `MAX_CYCLE_USD` in `pipeline/config.py` first, or the same refusal "
            "repeats."
        )
        ST.save_batch_state(bst)
        return 2

    vid = B.submit(vreqs, max_usd=limit)
    ST.add_batch(bst, batch_id=vid, kind="verify", submitted_at=_now(),
                 count=len(vreqs), budgeted_usd=_budgeted(est))
    ST.save_batch_state(bst)
    ok(f"submitted verification batch {vid}")
    return 0


def _advance_verify(pending: dict, bst: dict, args: argparse.Namespace) -> int:
    bid = pending["batch_id"]
    head(f"Verification batch {bid}")
    status = B.poll(bid)
    ok(f"status: {status}")
    if status != "ended":
        ok("still processing — will retry on the next cron")
        return 0

    verdicts = B.collect(bid)

    # An infrastructure failure and an adversarial refutation are NOT the same event, and
    # reporting them in the same sentence is how a broken run looks like a clean one. A
    # truncated or unparseable verdict body arrives here as ok=False; if EVERY one failed,
    # the adversary never ran and there is nothing to conclude.
    vbad = {k: v for k, v in verdicts.items() if not v.get("ok")}
    ok(f"collected {len(verdicts) - len(vbad)} verdicts, {len(vbad)} failed")
    for k, v in list(vbad.items())[:10]:
        warn(f"{k}: {v.get('error')}")
    if verdicts and len(vbad) == len(verdicts):
        fail("every verification result failed — that is an infrastructure failure, not "
             "the adversary. Refusing to report it as 'refuted'; the batch stays pending "
             "so a re-run can retry it.")
        return 1

    expected = pending.get("request_count") or 0
    if expected and len(verdicts) != expected:
        fail(f"{bid}: submitted {expected} verification requests but the batch returned "
             f"{len(verdicts)} — {expected - len(verdicts)} paid-for cards are missing. "
             "Not advancing; re-run to retry.")
        return 1

    ST.mark_batch(bst, bid, "collected")
    ST.save_batch_state(bst)

    try:
        extractions = json.loads(C.EXTRACTIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"pipeline/state/extractions.json unreadable ({exc}) — re-run refresh")
        return 1

    head("Applying verdicts")
    cards = S.load_cards()
    # Built through S._inner_card, which is what sources.py uses. This used to be
    # `{c["card"]["id"]: c ...}`, so a seed entry in the OTHER shape sources.py accepts
    # — a flat dict carrying "id" — was invisible here, fell into the card_gone branch,
    # and was recorded as "left the catalogue" while being a live, shipping card. Zero
    # entries are flat today; the point is that the two readers now agree on what a card
    # entry is, so the day one appears it is not silently retired.
    by_id = {}
    for c in cards:
        inner = S._inner_card(c)
        if inner is None:
            continue
        cid = str(inner.get("id") or "").strip()
        if cid:
            by_id[cid] = c
    sources_state = ST.load_state()

    # parse_custom_id returns the SANITISED id, and by_id is keyed on the original. For
    # the 16 live cards whose ids carry parentheses or dots — every BOBCARD, AU co-brand
    # and OneCard/Scapia entry — those differ, so the lookup missed and the card was
    # dropped in silence AFTER we had paid to both extract and verify it. Rebuild the
    # submit-time map instead, exactly as _advance_extract does.
    by_custom_id = {
        B.build_extract_request(c, "x", "https://x.invalid", "x" * 200)["custom_id"]: c
        for c in sources_state.get("sources", {})
        if not c.startswith("__watch__")
    }
    verify_key = {
        B.build_verify_request(c, "x" * 200, [])["custom_id"]: c
        for c in sources_state.get("sources", {})
        if not c.startswith("__watch__")
    }
    by_card_verify = {v: k for k, v in verify_key.items()}

    proposals: list[D.Proposal] = []
    survived = killed = unmapped = 0
    finished: dict[str, int] = {}
    unjudged = gone = thin = unresolved = 0

    for cid, ex in extractions.items():
        if not ex.get("ok"):
            # The extraction request itself failed, so no document was ever turned into
            # observations. Nothing has been judged and the work is still owed: leave the
            # card unfinished so next Monday pays for it, which is the correct bill.
            continue
        card_id = by_custom_id.get(cid)
        if card_id is None:
            unmapped += 1
            warn(f"unmapped custom_id, extraction discarded: {cid}")
            continue
        obs = (ex.get("data") or {}).get("observations") or []
        # Derive the verify custom_id from the card, not by string surgery on the
        # extract id — sanitisation and hash-suffixing mean they are not always related
        # by a simple prefix swap.
        vres = verdicts.get(by_card_verify.get(card_id, ""), {})
        vdata = (vres.get("data") or {}) if vres.get("ok") else {}
        vmap = {v["index"]: v for v in vdata.get("verdicts", []) if isinstance(v.get("index"), int)}

        kept = []
        for i, o in enumerate(obs):
            v = vmap.get(i)
            # No verdict means unverified. Unverified does not ship — that is the
            # whole point of the second pass.
            if v is None or v.get("refuted") or not v.get("quote_found_verbatim") \
                    or not v.get("supports_value"):
                killed += 1
                continue
            kept.append(o)
            survived += 1

        # ------------------------------------------------------------------
        # Did this card FINISH, and if so with what?
        #
        # This used to be one `if entry is None or not kept: continue`, which skipped
        # mark_done and so wrote the same state for two facts that are not the same:
        #
        #   "we read the bank's page, extracted, verified, and nothing survived"
        #   "we have never processed this card"
        #
        # has_changed() needs both unchanged bytes AND status=done to skip a card, so
        # the first fact was stored as the second and the card was re-fetched and
        # re-extracted every Monday, forever. Measured on the 2026-08-17 state: 304 of
        # 373 cards were stuck there, about $13 a week of re-billing for answers we
        # already had.
        #
        # The fix is not "move mark_done past the continue". It is to say which of the
        # four finished outcomes happened, and to keep the ONE genuinely unfinished
        # case — the adversary never saw this card — out of all of them.
        # ------------------------------------------------------------------
        entry = by_id.get(card_id)
        src = ST.get_source(sources_state, card_id) or {}

        if obs and not vmap:
            # Observations exist but no verdict came back for any of them: verification
            # was deferred because the page moved between stage 1 and stage 2, or the
            # verdict body was unusable. The adversary never judged this card, so it is
            # NOT finished. Paying to re-read it is the cheap error; retiring a card
            # nobody checked is the expensive one.
            unjudged += 1
            continue

        if entry is None:
            # Extracted, then the card left seed/cards.json before the verdict landed.
            # NOT done: `done` retires the card at this hash forever, so if it came back
            # to the catalogue with the issuer's page unchanged it would never be
            # extracted again — the pipeline believes it is finished, and no bank change
            # is needed for its data to be wrong. A card absent from the catalogue is
            # never fetched anyway, so its own status costs nothing and keeps the way
            # back open.
            gone += 1
            if kept:
                # Say this out loud. It used to print nothing at all while throwing away
                # observations that had been paid for twice — extracted and verified.
                warn(f"{card_id}: left seed/cards.json after verification; "
                     f"{len(kept)} verified observation(s) discarded unapplied")
            ST.mark_card_gone(sources_state, card_id)
            continue

        card_specific = S.is_card_specific(sources_state.get("sources", {}), card_id)
        chars = src.get("text_chars")

        if not obs:
            # "The extractor found nothing in this document" is a finding about a CARD
            # only when a document about that card was actually read. Two ways it was
            # not, and both were being recorded as findings:
            #
            #   too short   19 BOBCARD cards were retired for good against 188
            #               characters — the entire extractable text of
            #               bobcard.co.in/credit-card is its navigation menu.
            #   not ours    the page belongs to the issuer's whole portfolio, not to
            #               this card.
            #
            # Neither is evidence, so neither may retire a card. They become the
            # source-resolution backlog they always were.
            if isinstance(chars, int) and chars < C.MIN_SOURCE_CHARS:
                thin += 1
                ST.mark_unresolved_source(
                    sources_state, card_id,
                    note=f"only {chars} chars of text at {src.get('url', '')} — "
                         f"too short to be this card's terms",
                )
                continue
            if not card_specific:
                unresolved += 1
                ST.mark_unresolved_source(
                    sources_state, card_id,
                    note=f"{src.get('url', '')} is an issuer listing page, not this "
                         f"card's own terms — 'nothing found' says nothing about it",
                )
                continue
            reason = ST.DONE_NO_OBSERVATIONS
        elif not kept:
            reason = ST.DONE_ALL_REFUTED
        else:
            reason = ST.DONE_VERIFIED
            proposals.extend(D.observations_to_proposals(entry, kept, src.get("url", "")))

        ST.mark_done(sources_state, card_id, reason,
                     card_specific=card_specific, done_at=_now())
        finished[reason] = finished.get(reason, 0) + 1

    ST.save_state(sources_state)
    ok(f"{survived} observations survived verification, {killed} refuted or unverified")
    if finished:
        ok("cards finished this cycle: " + ", ".join(
            f"{n} {r}" for r, n in sorted(finished.items(), key=lambda kv: -kv[1])))
    nothing = sum(n for r, n in finished.items() if r in ST.DONE_REASONS_NOTHING_KEPT)
    if nothing:
        ok(f"{nothing} cards finished with nothing to propose — that is an answer, not a "
           f"failure, and they will not be re-billed until their source bytes move")
    if unjudged:
        warn(f"{unjudged} card(s) had observations the adversary never judged — left "
             f"unfinished on purpose, they will be re-read next run")
    if gone:
        warn(f"{gone} card(s) left seed/cards.json mid-cycle — recorded as "
             f"'{ST.STATUS_CARD_GONE}', NOT retired, so re-adding one brings it back")
    if thin:
        warn(f"{thin} card(s) had a source document under {C.MIN_SOURCE_CHARS:,} "
             f"characters — that is not a document, so 'nothing found' is not a "
             f"finding. Left as '{ST.STATUS_UNRESOLVED_SOURCE}' for a real URL")
    if unresolved:
        warn(f"{unresolved} card(s) found nothing on a page shared with other cards — "
             f"a source-resolution gap, not a result. Left as "
             f"'{ST.STATUS_UNRESOLVED_SOURCE}'; give them a URL in "
             f"pipeline/sources_overrides.json")
    if unmapped:
        warn(f"{unmapped} extraction(s) could not be mapped back to a card — investigate")
    if not proposals:
        ok("no proposals this week")
        return 0

    proposals = [D.gate(p) for p in proposals]
    summary = D.summarise(proposals)
    ok(f"{summary['auto']} auto-applicable, {summary['blocked']} need a human")
    for reason, n in sorted(summary["by_reason"].items(), key=lambda kv: -kv[1]):
        warn(f"{n:>4} blocked: {reason}")

    if args.dry_run:
        print(D.render_markdown(proposals))
        return 0

    new_cards, applied = D.apply_proposals(cards, proposals, only_auto=True)
    if applied:
        C.CARDS_JSON.write_text(
            json.dumps(new_cards, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        _regen_manifest()
        ok(f"applied {len(applied)} changes to seed/cards.json and regenerated the manifest")

    metrics = R.compute_metrics(new_cards, ST.load_state())
    hist = ST.read_metrics()
    body = "\n\n".join([
        D.render_markdown(proposals),
        R.render_report(metrics, hist[-1] if hist else None,
                        applied=len(applied), blocked=summary["blocked"]),
    ])
    (_work_dir() / "pr_body.md").write_text(body, encoding="utf-8")
    ST.append_metrics({"run_at": _now(), **metrics})

    if applied or summary["blocked"]:
        print("PATCH_READY")
    return 0


def _regen_manifest() -> int:
    """Rewrite manifest checksums from the bytes actually on disk.

    A stale checksum is the exact failure that makes the app reject a sync with
    "Sync failed", so this is never optional after touching a seed file.
    """
    import hashlib

    man = json.loads(C.MANIFEST_JSON.read_text(encoding="utf-8"))
    for f in man.get("files", []):
        name = f.get("file") or f.get("name")
        raw = (C.SEED_DIR / name).read_bytes()
        f["checksum"] = hashlib.sha256(raw).hexdigest()
        f["size_bytes"] = len(raw)
    parts = str(man.get("version", "0.0.0")).split(".")
    if len(parts) == 3 and parts[-1].isdigit():
        parts[-1] = str(int(parts[-1]) + 1)
        man["version"] = ".".join(parts)
    man["updated_at"] = _now()
    C.MANIFEST_JSON.write_text(
        json.dumps(man, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return 0


# ---------------------------------------------------------------------------
# news-watch
# ---------------------------------------------------------------------------
def cmd_news_watch(args: argparse.Namespace) -> int:
    head("Polling issuer notice pages")
    st = ST.load_state()
    pages = [(i, u) for i, u in C.WATCH_PAGES if not args.issuer or i == args.issuer]
    if not pages:
        fail(f"no watched page for issuer {args.issuer!r}")
        return 1

    moved: list[tuple[str, str, str]] = []
    for issuer, url in pages:
        res = F.fetch_source(url)
        key = f"__watch__{issuer}"
        if not res.ok or not res.text:
            warn(f"{issuer}: {res.error or 'no text'}")
            ST.record_source(st, key, url=url, content_sha256=None,
                             fetched_at=_now(), status="fetch_failed", note=res.error)
            continue
        if args.force or ST.has_changed(st, key, res.text_sha256):
            moved.append((issuer, url, res.text))
            ok(f"{issuer}: CHANGED")
        else:
            ok(f"{issuer}: unchanged")
        # STATUS_DONE, not "ok". has_changed() ends with `status != STATUS_DONE`, so a
        # watch row written as "ok" reported CHANGED every single day even when the
        # page had not moved by one byte — the hash gate on this path was decorative,
        # and the daily run paid to re-analyse all 11 pages regardless. A watch page is
        # finished the moment it is read: there is no second stage for it.
        ST.record_source(st, key, url=url, content_sha256=res.text_sha256,
                         fetched_at=_now(), status=ST.STATUS_DONE,
                         text_chars=len(res.text))

    ST.save_state(st)
    if not moved:
        ok("no issuer notice page changed today")
        return 0

    if args.dry_run:
        ok(f"dry run — {len(moved)} pages changed, not analysing")
        return 0

    head("Analysing what changed")
    reqs = [B.build_news_request(issuer, url, text) for issuer, url, text in moved] \
        if hasattr(B, "build_news_request") else []
    if not reqs:
        warn("news request builder unavailable — reporting hashes only")
        return 0

    # The news path runs synchronously: it is at most a dozen requests and the whole
    # value of an alert is that it is timely, so waiting up to 24h for a batch would
    # defeat the point.
    #
    # It is still a PAID path, at standard rates with no batch discount, fired daily by
    # cron — and until now it had no ceiling of any kind, while config.py claimed the
    # card ceiling applied to every submission. It has its own now: MAX_NEWS_USD,
    # priced for a day on which every watched page moved.
    news_limit = _news_max_usd(args)
    est = B.estimate_sync_cost(reqs)
    ok(f"{est['requests']} news requests, estimated ${est['est_usd']:.2f} at standard "
       f"rates — ${est['est_usd'] * C.ESTIMATE_SAFETY_FACTOR:.2f} with the "
       f"{C.ESTIMATE_SAFETY_FACTOR}x margin, ${est['est_usd_ceiling']:.2f} at the bound")
    ok(f"news spend limit ${news_limit:.2f}" if news_limit is not None
       else "news spend limit DISABLED")
    over = _ceiling_verdict(est, news_limit, stage="news analysis")
    if over:
        _report_ceiling_refusal(
            over.replace("MAX_CYCLE_USD", "MAX_NEWS_USD")
                .replace("cycle spend limit", "news spend limit"),
            forecast=False,
        )
        return 2

    try:
        changes = B.run_sync(reqs, max_usd=news_limit)
    except ValueError as exc:
        fail(f"news analysis refused: {exc}")
        return 2
    flat = [c for r in changes for c in (r.get("changes") or []) if c.get("affects_rewards")]
    ok(f"{len(flat)} reward-affecting changes detected")
    if not flat:
        return 0

    cards = S.load_cards()
    items = []
    for ch in flat:
        ids = N.map_cards(ch, cards)
        items.append(N.change_to_item(ch, card_ids=ids, published_at=_now()))

    errs = [e for it in items for e in N.validate_item(it)]
    if errs:
        for e in errs[:20]:
            fail(e)
        return 1

    existing = json.loads(C.NEWS_FEED.read_text(encoding="utf-8"))
    feed = N.merge_feed(existing, items, updated_at=_now())
    C.NEWS_FEED.write_text(
        json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ok(f"news/feed.json drafted at version {feed['version']} with {len(feed['items'])} items")

    lines = [
        "## Drafted news alerts",
        "",
        f"Feed version **{existing.get('version')} -> {feed['version']}** "
        "(the app only refetches on a whole-number increase).",
        "",
        "**Read every item before merging.** These are model-drafted from an issuer notice "
        "page that changed. A wrong effective date or an overstated card list reaches every "
        "user who opens the app.",
        "",
    ]
    for it in items:
        who = ", ".join(it.get("affected_cards") or []) or "everyone"
        lines += [f"### {it['title']}", "", it["summary"], "",
                  f"- severity: `{it.get('severity')}`",
                  f"- shows to: {who}",
                  f"- source: {it.get('source_url') or '(none)'}", ""]
    (_work_dir() / "news_pr_body.md").write_text("\n".join(lines), encoding="utf-8")
    print("NEWS_READY")
    return 0


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def cmd_metrics(args: argparse.Namespace) -> int:
    cards = S.load_cards()
    cur = R.compute_metrics(cards, ST.load_state())
    hist = ST.read_metrics()
    print(R.render_report(cur, hist[-1] if hist else None))
    if args.write:
        ST.append_metrics({"run_at": _now(), **cur})
        ok("appended to pipeline/state/metrics.jsonl")
    return 0


# ---------------------------------------------------------------------------
# discover — offline maintenance, not a CI stage
#
# Not wired into a workflow on purpose. It rewrites which document every card is
# read from, which is the single highest-leverage input to the whole pipeline, so
# it runs when a person asks and its output lands in a reviewable diff.
def cmd_discover(args: argparse.Namespace) -> int:
    # Absolute, not relative: cli.py is executed as a script
    # (`python3 pipeline/cli.py`), so it has no parent package and `from . import`
    # raises ImportError at run time — which no test caught, because the tests
    # import the module rather than shelling out to it.
    from pipeline import discover as discovery

    argv: list[str] = []
    if args.issuer:
        argv += ["--issuer", args.issuer]
    if args.json:
        argv += ["--json"]
    if args.no_verify:
        argv += ["--no-verify"]
    if args.write:
        argv += ["--write"]
    return discovery.main(argv)


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The CLI surface, separated from main() so tests can parse real argv.

    That separation is load-bearing rather than tidy: the scheduled Monday run gets
    its flags from a bash string that weekly-refresh.yml assembles out of
    workflow_dispatch inputs, and on a cron every one of those inputs is empty. The
    only honest way to test "what does the unattended run actually do" is to hand
    THIS parser the same empty argv the workflow produces, rather than hand-building
    a Namespace whose defaults the test author chose.
    """
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="stage 1: fetch sources, submit extraction batch")
    r.add_argument("--card-id", default="")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--force", action="store_true", help="ignore content hashes (full sweep)")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-usd", type=float, default=None, help="cap this CYCLE's estimated spend in USD (extraction + the verification it commits us to); 0 disables the cap. Default comes from config.MAX_CYCLE_USD. Pass the SAME value to `advance` or the extraction is paid for and never verified.")
    r.add_argument("--force-resubmit", action="store_true", help="submit even though a batch from an earlier run is still in flight. Every card in both batches is billed twice.")
    r.set_defaults(fn=cmd_refresh)

    a = sub.add_parser("advance", help="stages 2-3: collect batches, propose a patch")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--max-usd", type=float, default=None, help="cap this CYCLE's estimated spend in USD; 0 disables the cap. Default comes from config.MAX_CYCLE_USD. Must match the value `refresh` was given.")
    a.add_argument(
        "--recollect",
        metavar="BATCH_ID",
        default="",
        help="reopen an already-collected batch so the next advance reads it again. "
             "Reading the results is free — they are kept for 29 days and the model does "
             "not re-run — but reopening an EXTRACTION batch makes the next advance "
             "submit and PAY FOR verification again, so that case is refused unless "
             "--force is given. Use when a stage failed AFTER collection.",
    )
    a.add_argument("--force", action="store_true",
                   help="with --recollect, reopen an extraction batch whose verification "
                        "was already submitted, accepting a second verification bill")
    a.set_defaults(fn=cmd_advance)

    n = sub.add_parser("news-watch", help="poll issuer notice pages, draft feed items")
    n.add_argument("--issuer", default="")
    n.add_argument("--force", action="store_true")
    n.add_argument("--dry-run", action="store_true")
    n.add_argument("--max-usd", type=float, default=None,
                   help="cap this news run's estimated spend in USD; 0 disables the cap. "
                        "Default comes from config.MAX_NEWS_USD. This path pays STANDARD "
                        "rates, not batch rates.")
    n.set_defaults(fn=cmd_news_watch)

    m = sub.add_parser("metrics", help="print the weekly numbers")
    m.add_argument("--write", action="store_true")
    m.set_defaults(fn=cmd_metrics)

    d = sub.add_parser("discover", help="find each card's own issuer page")
    d.add_argument("--issuer", default="", help="one issuer slug, e.g. hdfc")
    d.add_argument("--json", action="store_true")
    d.add_argument("--no-verify", action="store_true",
                   help="skip the fetch that confirms a page names its card (fast, unsafe)")
    d.add_argument("--write", action="store_true",
                   help="merge verified matches into pipeline/sources_overrides.json")
    d.set_defaults(fn=cmd_discover)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
