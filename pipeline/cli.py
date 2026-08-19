#!/usr/bin/env python3
"""
The pipeline entrypoint. Three commands, one per CI stage.

Usage:
    python3 pipeline/cli.py refresh [--card-id ID] [--limit N] [--force] [--dry-run]
    python3 pipeline/cli.py refresh --unsourced-only [--limit N] [--dry-run]
    python3 pipeline/cli.py advance [--dry-run]
    python3 pipeline/cli.py news-watch [--issuer NAME] [--force] [--dry-run]
    python3 pipeline/cli.py metrics [--write]
    python3 pipeline/cli.py evidence [--per-week N] [--json]
    python3 pipeline/cli.py discover [--issuer NAME] [--no-verify] [--write]

Design, in one paragraph:

`refresh` fetches every active card's issuer document, hashes the normalised text,
and submits an extraction batch for ONLY the cards whose bytes moved since last week.
`advance` is idempotent and runs on a short cron: it looks at what is in flight and
either collects the extraction batch and submits the verification batch, or collects
the verification batch, diffs it against seed/cards.json and writes a patch for a
human to merge. `news-watch` polls the pages where issuers publish revisions and
drafts feed items when one changes.

`refresh --unsourced-only` is the one path that is NOT change-driven. The hash gate
makes the weekly run affordable, and it has a blind spot it can never close on its
own: a card whose rates have never been verified, whose issuer page did not move
this week, is skipped — forever. That flag selects the cards whose reward rules
carry no citation, drops the ones already read end to end at their current bytes
so the queue terminates, and opens the gate for exactly what is left. It says
before spending how much of the selection the gate was actually blocking, versus
how much an ordinary refresh would fetch anyway. `evidence` says how many there
are and what finishing costs — both ends of the range.

Nothing here ever writes to `main`, and nothing publishes to users without a person
merging a PR. That is not caution for its own sake: of 18 changes a first pass called
"confirmed at the issuer", an adversarial second pass refuted 6.

Exit codes: 0 ok, 1 data error, 2 config/missing dependency.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
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
from pipeline import provenance as P     # noqa: E402
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


def _gate_decision(
    st: dict,
    card_id: str,
    text_sha256: str,
    *,
    force: bool,
    evidence_gap: bool,
) -> "tuple[bool, str]":
    """Does this card's document reach the model this run, and on what grounds.

    The content-hash gate is the only reason a weekly run costs rupees instead of
    thousands, so exactly three things may open it and each one is named out loud:

      "force"        the operator asked for a full sweep. Applies to every card.
      "changed"      the ordinary weekly reason — the source bytes moved, or we
                     have never finished processing this card at this hash.
      "evidence gap" this card's reward rules carry no citation AND it has not
                     been read to completion at these bytes, so its page not
                     moving proves nothing: an unread page is unread whether or
                     not it was rewritten last night.

    `evidence_gap` is per card and comes from the catch-up selection. It is NOT
    `--force` under another name, and the difference is the whole point: --force
    opens the gate for all 370 active cards and bills a full sweep, while this
    opens it only for the selection and leaves every other card gated exactly as
    before. A card that later acquires evidence — or that is simply read to
    completion — drops out of the selection and falls straight back under the gate
    with no code change. The run reports how many cards this reason actually
    applied to, which today is zero: every selected card had moved or was already
    due, and the flag's contribution is the --limit, not the bypass.
    """
    if force:
        return True, "force"
    if ST.has_changed(st, card_id, text_sha256):
        return True, "changed"
    if evidence_gap:
        return True, "evidence gap"
    return False, "unchanged"


def _preflight_budget(
    cards: int, args: argparse.Namespace, *, dry_run: bool
) -> int:
    """Price a selection BEFORE fetching anything. 0 to proceed, 2 to stop.

    This is the SAME ceiling `batch.submit` enforces — config.MAX_BATCH_USD, moved
    by the same `--max-usd` flag, disabled by the same explicit `--max-usd 0`. It
    is not a second mechanism and must never become one; two spend limits with two
    thresholds is how one of them ends up quietly disabled.

    What it adds is timing. submit() refuses AFTER 174 issuer pages have been
    fetched, which is ten minutes of an operator's evening and a lot of traffic at
    banks that did not ask for it, to learn something arithmetic that was knowable
    in a millisecond.

    EVERY LINE HERE PRINTS BOTH PASSES. The ceiling is compared against the
    extraction forecast because extraction is what this command submits — but
    verification is a second batch against the same ceiling, so a run that clears
    a $25 limit can still bill close to $50 by the time `advance` has finished.
    The advice line used to quote extraction only and call it "a run", which
    under-budgeted a --limit 155 week by 58%.
    """
    extract = P.forecast_usd(cards, both_passes=False)
    both = P.forecast_usd(cards)
    floor = P.forecast_usd(cards, basis="observed")
    ok(f"about ${extract:,.2f} to extract {cards} card(s); "
       f"${both:,.2f} through both passes "
       f"(${floor:,.2f} if 17-Aug's verification yield repeats)")

    max_usd, refusal = _spend_ceiling(args, both)
    if refusal:
        fail(refusal)
        return 2
    if max_usd is None:
        ok(f"no per-batch spend ceiling on this run — you accepted "
           f"${float(args.i_accept_usd):,.2f} explicitly")
        return 0
    if extract <= max_usd:
        return 0

    room = P.cards_within_budget(max_usd)
    cycle_room = P.cards_within_budget(max_usd, both_passes=True)
    if dry_run:
        warn(f"this selection is over the ${max_usd:,.2f} per-batch ceiling — a real "
             f"run would be refused. Continuing: a dry run spends nothing.")
        return 0

    fail(f"{cards} cards forecast at ${extract:,.2f} to extract, above the "
         f"${max_usd:,.2f} PER-BATCH ceiling in config.MAX_BATCH_USD — refusing "
         f"before fetching anything.")
    warn(f"spread it instead:  --unsourced-only --limit {room}   "
         f"(about ${P.forecast_usd(room, both_passes=False):,.2f} to extract, "
         f"${P.forecast_usd(room):,.2f} through both passes)")
    warn(f"the ceiling is per BATCH, and verification is a second batch. For a whole "
         f"cycle inside ${max_usd:,.2f}, use --limit {cycle_room}.")
    warn("or raise the ceiling deliberately with --max-usd N; --max-usd 0 removes it "
         "and then needs --i-accept-usd N.")
    return 2


def _in_flight_guard(args: argparse.Namespace) -> int:
    """Refuse to submit a second extraction batch while one is still in flight.

    Nothing checked this before. `cmd_refresh` loaded batch.json only AFTER
    submitting, so two runs could have two extraction batches open at once — and
    stage 2 overwrote `extractions.json` wholesale on collection, so the second
    batch to end deleted the first one's paid-for results while the first one's
    verification batch was already submitted and billed. Both halves of a ~$50
    cycle, paid for and discarded, with nothing printed anywhere.

    The `concurrency: kredme-pipeline` group used to make this unreachable by
    serialising the workflows. `--unsourced-only` is documented as an operator
    command run locally, off-cron, outside that group, which is exactly what
    makes it reachable.
    """
    bst = ST.load_batch_state()
    pending = ST.pending_batches(bst)
    if not pending or getattr(args, "dry_run", False):
        return 0
    if getattr(args, "allow_concurrent_batch", False):
        warn(f"{len(pending)} batch(es) already in flight and "
             f"--allow-concurrent-batch was passed — proceeding.")
        return 0
    fail(f"{len(pending)} batch(es) already in flight; submitting another would "
         f"overwrite results you have already paid for. Not fetching, not submitting.")
    for b in pending:
        warn(f"  {b.get('kind')} {b.get('batch_id')} — "
             f"{b.get('request_count')} requests, submitted {b.get('submitted_at')}")
    warn("collect them first:  python3 pipeline/cli.py advance")
    warn("or override deliberately with --allow-concurrent-batch.")
    return 2


def _say_push_the_state(batch_id: str) -> None:
    """The step that turns a paid batch into results, and it is a git command.

    `pipeline-advance.yml` checks out branch `dev` and gates on the COMMITTED
    pipeline/state/batch.json. A local run writes that file into the operator's
    working tree and nowhere else, so a batch submitted locally and not pushed is
    collected by nobody: the collector prints "No batch state file." forever, the
    extraction is billed, and Anthropic drops the results after 29 days. The
    rotation counters go with it, so the next run re-selects the same cards and
    pays again — the exact double-payment the round-robin exists to prevent.

    Printed at submit time, loudly, because a doc's last paragraph is not where
    this belongs.
    """
    print("")
    print("!!! NOT DONE YET — this batch is paid for and nobody will collect it")
    print("!!! until pipeline/state is on branch dev, where pipeline-advance.yml")
    print("!!! reads it. Results are dropped after 29 days.")
    print("")
    print("    git add pipeline/state")
    print(f"    git commit -m 'Track extraction batch {batch_id}'")
    print("    git push origin HEAD:dev")
    print("")


def cmd_refresh(args: argparse.Namespace) -> int:
    head("Resolving sources")
    try:
        cards = S.load_cards()
    except (ValueError, OSError) as exc:
        fail(f"could not read seed/cards.json: {exc}")
        return 1

    overrides = S.load_overrides(REPO / "pipeline" / "sources_overrides.json")
    srcs = S.resolve_sources(cards, overrides=overrides)

    # Loaded before selection, not after: the catch-up order reads how often each
    # card has already been tried, so a weekly --limit walks the backlog.
    st = ST.load_state()

    if args.card_id:
        srcs = [s for s in srcs if s.card_id == args.card_id]
        if not srcs:
            fail(f"no active card with id {args.card_id!r}")
            return 1

    # The set of cards allowed past the hash gate on evidence grounds. Empty on an
    # ordinary run, which is what keeps the gate protecting every other card.
    evidence_gap: set[str] = set()
    still_gated = 0

    if args.unsourced_only:
        if args.force:
            fail("--unsourced-only and --force ask for two different runs. "
                 "--unsourced-only reads the cards that cite nothing; --force "
                 "re-reads all %d active cards and bills a full sweep. Pick one."
                 % len(srcs))
            return 2

        head("Selecting cards with no issuer evidence")
        plan = P.plan_catch_up(srcs, cards, st, limit=args.limit or 0)

        ok(f"{plan.sourced} of the {plan.active} card(s) this run considered have a "
           f"reward rule citing a document; {plan.active - plan.sourced} do not")
        if plan.provenance_only:
            ok(f"{plan.provenance_only} of those carry a card-level stamp about an "
               f"annual fee or a forex rate — real evidence, but not about a reward "
               f"rule, so they stay in the queue")
        if plan.exhausted:
            ok(f"{plan.exhausted} card(s) left the queue: already read end to end at "
               f"their current bytes and still citing nothing. Re-reading the same "
               f"page returns the same nothing; they come back when it moves.")
        if plan.unreachable:
            warn(f"{len(plan.unreachable)} of those can never be extracted — no issuer "
                 f"URL resolves for them:")
            for missing in plan.unreachable:
                warn(f"       {missing.card_id:<46} {missing.reason}")
            warn("       fix these by hand in pipeline/sources_overrides.json; the "
                 "pipeline cannot.")
        if not plan.selected:
            # Three very different situations, and calling them all "nothing to
            # do" would hide the two that need a person.
            if plan.unreachable and not plan.exhausted:
                warn("nothing to read: every card here is unsourced AND unreachable. "
                     "This pipeline cannot help them — see the list above.")
            elif plan.exhausted:
                ok("nothing left to read: every unsourced card has already been read "
                   "at its current bytes. The next move is a better DOCUMENT, not "
                   "another batch — try `discover --write`.")
            else:
                ok("every reachable card already cites a document — nothing to catch "
                   "up on")
            return 0

        srcs = plan.selected
        evidence_gap = {s.card_id for s in srcs}
        still_gated = plan.active - len(srcs)

        # WHAT THE MONEY BUYS, before the money is spent. The old line claimed all
        # of these "have never been read", which was true of 31 of 332 — the rest
        # had been fetched and extracted and billed already, and are re-selected
        # by the plain weekly refresh anyway. Meanwhile the 31 it WAS true of had
        # been read to completion at these exact bytes, so re-reading them buys
        # nothing; they now leave the queue instead. An operator deciding whether
        # to spend has to see that split, not a flattering blanket sentence.
        ok(f"reading {len(srcs)} card(s) this run, {plan.deferred} still in the "
           f"backlog for later runs ({plan.never_read} of the backlog has never "
           f"been read)")
        if plan.gate_blocked:
            ok(f"{plan.gate_blocked} of the selection are blocked by the content-hash "
               f"gate today — that is what this flag buys that an ordinary refresh "
               f"cannot")
        if plan.also_due:
            warn(f"{plan.also_due} of the selection would ALSO be fetched by this "
                 f"week's ordinary refresh — running both in one week pays for them "
                 f"twice, in two separate batches. What this flag adds for them is "
                 f"the --limit: an affordable slice instead of one over-budget sweep.")
        if plan.re_reads:
            warn(f"{plan.re_reads} of the selection have been selected before and "
                 f"still cite nothing. A card read twice that yields nothing is a "
                 f"document problem — run `discover --write` before paying again.")
        if plan.on_shared_doc:
            warn(f"{plan.on_shared_doc} of the selection are read from a page shared "
                 f"with other cards. Asking for one card's mechanics out of a listing "
                 f"of forty is the weakest thing this pipeline does; `discover "
                 f"--write` fixes it offline and for free.")
        ok(f"the gate is untouched for the other {still_gated} active card(s)")

        rc = _preflight_budget(len(srcs), args, dry_run=args.dry_run)
        if rc:
            return rc
        rc = _in_flight_guard(args)
        if rc:
            return rc
    elif args.limit:
        srcs = srcs[: args.limit]

    cov = S.coverage_report(srcs)
    ok(f"{cov['resolved']}/{cov['total']} cards have an issuer URL")
    for reason, n in sorted(cov["by_reason"].items(), key=lambda kv: -kv[1]):
        if reason:
            warn(f"{n:>4} unresolved: {reason}")

    head("Fetching")
    resolved = [s for s in srcs if s.url]
    changed: list[tuple[S.Source, str]] = []
    failures: list[tuple[str, str, str]] = []
    unchanged = failed = gate_opened_on_evidence = 0

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

        extract, why = _gate_decision(
            st, s.card_id, res.text_sha256,
            force=args.force, evidence_gap=s.card_id in evidence_gap,
        )
        if not extract:
            unchanged += 1
            # Keep STATUS_DONE — rewriting it to "unchanged" would make has_changed
            # true again next week and undo the whole point of the hash gate.
            ST.record_source(
                st, s.card_id, url=s.url, content_sha256=res.text_sha256,
                fetched_at=_now(), status=ST.STATUS_DONE,
            )
            continue

        if why == "evidence gap":
            gate_opened_on_evidence += 1
        changed.append((s, res.text))
        # "fetched", not "done": the bytes are in hand but nothing has been extracted
        # from them yet. Only stage 3 may mark a card done, or a batch that expires
        # silently retires the card forever.
        ST.record_source(
            st, s.card_id, url=s.url, content_sha256=res.text_sha256,
            fetched_at=_now(), status="fetched",
        )

    ok(f"fetched {len(resolved)}: {len(changed)} changed, {unchanged} unchanged, {failed} failed")
    if unchanged and not args.force:
        ok(f"skipped {unchanged} cards whose source bytes did not move — this is the saving")
    if gate_opened_on_evidence:
        ok(f"{gate_opened_on_evidence} of these had NOT moved and were read anyway — "
           f"they carry no citation, so an unchanged page proves nothing about them")
        ok(f"the gate is unchanged for the other {still_gated} card(s) — nothing "
           f"outside this selection was opened, which is what --force would have done")
    elif evidence_gap:
        ok(f"0 of the selection needed the gate opened — every one of them had moved "
           f"or was already due, so an ordinary refresh would have fetched them too")
    _report_fetch_failures(failures)

    # Every card this run SELECTED counts as an attempt, including the ones whose
    # fetch failed. Counting only successes would park BOBCARD's unfetchable cards
    # at the front of the queue forever and starve every other issuer.
    #
    # ONE timestamp for the whole run, and a SORTED walk. `evidence_gap` is a set,
    # so its iteration order is hash-randomised per process, and `_now()` has
    # one-second resolution — a loop that crossed a second boundary stamped a
    # random subset of the week's cards a second later than the rest. `_attempt_key`
    # tiers on (count, last_attempt_at), so that split one week's selection into two
    # tiers at random and changed which cards later weeks picked. One run, one
    # attempt time.
    if evidence_gap and not args.dry_run:
        attempted_at = _now()
        for card_id in sorted(evidence_gap):
            ST.note_evidence_attempt(st, card_id, attempted_at)

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
    ok(f"{est['requests']} requests, ~{est['est_input_tokens']:,} input tokens")
    ok(f"estimated ${est['est_usd']:.2f} (ceiling ${est['est_usd_ceiling']:.2f}) "
       f"— ${est['est_usd_uncached']:.2f} without the batch discount")

    if args.dry_run:
        ok("dry run — nothing submitted, nothing spent")
        (_work_dir() / "extract_requests.json").write_text(
            json.dumps(reqs, indent=2)[:2_000_000], encoding="utf-8"
        )
        return 0

    ceiling, refusal = _spend_ceiling(args, P.forecast_usd(len(reqs)))
    if refusal:
        fail(refusal)
        return 2

    # Checked here too, not only in the --unsourced-only branch: an ordinary
    # refresh reaches this line as well, and the whole point of the guard is that
    # nothing submits while a paid batch is uncollected.
    rc = _in_flight_guard(args)
    if rc:
        return rc

    try:
        batch_id = B.submit(reqs, max_usd=ceiling)
    except Exception as exc:  # noqa: BLE001 - surface any SDK/transport failure as exit 2
        fail(f"batch submission failed: {exc}")
        return 2

    ok(f"submitted extraction batch {batch_id}")
    bst = ST.load_batch_state()
    ST.add_batch(bst, batch_id=batch_id, kind="extract", submitted_at=_now(), count=len(reqs))
    ST.save_batch_state(bst)
    ST.save_state(st)

    ok("stage 1 complete — pipeline-advance.yml will collect when the batch ends")
    _say_push_the_state(batch_id)
    return 0


# ---------------------------------------------------------------------------
# advance — stages 2 and 3
# ---------------------------------------------------------------------------
def _recollect(bst: dict, batch_id: str) -> int:
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

    Re-collecting costs NOTHING. The batch has ended; retrieving an ended
    batch's results is a read, and Anthropic keeps them for 29 days. The
    expensive half — the model actually running — has already been paid for and
    does not happen again.
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

    ST.mark_batch(bst, batch_id, "submitted")
    ST.save_batch_state(bst)
    ok(f"reopened {match.get('kind')} batch {batch_id} "
       f"({match.get('request_count')} requests) — the next advance will re-collect it")
    ok("this re-reads results already paid for; it does not re-run the model")
    return 0


def cmd_advance(args: argparse.Namespace) -> int:
    bst = ST.load_batch_state()

    if getattr(args, "recollect", ""):
        return _recollect(bst, args.recollect)

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
    """The spend ceiling for this invocation, or None for "no ceiling".

    Absent flag -> config.MAX_BATCH_USD. `--max-usd 0` disables the check, which
    is spelled as an explicit zero rather than a separate --no-limit flag so it
    shows up verbatim in a workflow file and in `gh run view`, where somebody
    reviewing why a big batch went through can see it.

    A NEGATIVE value is now rejected by the parser rather than silently read as
    zero. `--max-usd -1` looked like a tighter limit and removed the limit
    entirely, which is the wrong direction for a typo to fail in.
    """
    raw = getattr(args, "max_usd", None)
    if raw is None:
        return C.MAX_BATCH_USD
    return None if float(raw) == 0 else float(raw)


def _nonneg_usd(raw: str) -> float:
    """argparse type for a dollar amount. Rejects negatives loudly."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number")
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"{value} is negative. A ceiling cannot be below zero; "
            f"--max-usd 0 is how you remove it, and it needs --i-accept-usd too."
        )
    return value


def _spend_ceiling(args: argparse.Namespace, forecast: float) -> "tuple[float | None, str]":
    """(ceiling, refusal). A ceiling of None is only reachable deliberately.

    `--max-usd 0` removes the only automatic protection this pipeline has, and it
    used to do that from a single flag on a single command line with no second
    step anywhere — no prompt, no confirmation, nothing. `refresh
    --unsourced-only --max-usd 0` would have submitted the whole backlog.

    So removing the ceiling now costs a second flag that carries a NUMBER:
    `--i-accept-usd 120`, which must be at least the both-passes forecast this
    run just printed. Typing the amount is the acknowledgement — it cannot be
    pasted from a runbook that was written when the backlog was half the size,
    because the forecast will have moved past it and the run refuses.
    """
    ceiling = _max_usd(args)
    if ceiling is not None:
        return ceiling, ""

    accepted = getattr(args, "i_accept_usd", None)
    if accepted is None:
        return ceiling, (
            f"--max-usd 0 removes the spend ceiling, so it needs you to name the "
            f"amount you are accepting: add --i-accept-usd {forecast:.2f} (or more). "
            f"This run forecasts ${forecast:,.2f} through both passes."
        )
    if float(accepted) + 1e-9 < forecast:
        return ceiling, (
            f"--i-accept-usd {float(accepted):,.2f} is below this run's "
            f"${forecast:,.2f} both-passes forecast. Raise it deliberately or "
            f"drop --max-usd 0."
        )
    return ceiling, ""


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


def _advance_extract(pending: dict, bst: dict, args: argparse.Namespace) -> int:
    bid = pending["batch_id"]
    head(f"Extraction batch {bid}")
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
    #
    # MERGED, not overwritten. This used to be a whole-file write, so a second
    # extraction batch collected in the same window deleted the first batch's
    # results — while the first batch's verification batch was already submitted
    # and billed, and would then find nothing in extractions.json to match. Both
    # halves of that cycle were paid for and thrown away, silently. Merging on
    # custom_id keeps every batch's paid output; a card re-extracted later
    # legitimately replaces its own older entry.
    C.EXTRACTIONS.parent.mkdir(parents=True, exist_ok=True)
    merged: dict = {}
    if C.EXTRACTIONS.exists():
        try:
            prior = json.loads(C.EXTRACTIONS.read_text(encoding="utf-8"))
            if isinstance(prior, dict):
                merged.update(prior)
        except (OSError, json.JSONDecodeError):
            warn("extractions.json was unreadable — starting a fresh one")
    kept_from_before = len(set(merged) - set(results))
    merged.update(results)
    if kept_from_before:
        ok(f"kept {kept_from_before} extraction(s) from an earlier batch that stage 3 "
           f"has not consumed yet")
    C.EXTRACTIONS.write_text(
        json.dumps(merged, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
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
    ok(f"{est['requests']} verification requests, estimated ${est['est_usd']:.2f} "
       f"(ceiling ${est['est_usd_ceiling']:.2f})")
    if args.dry_run:
        ok("dry run — not submitting verification")
        ST.save_batch_state(bst)
        return 0

    vceiling, vrefusal = _spend_ceiling(args, est["est_usd"])
    if vrefusal:
        fail(vrefusal)
        return 2
    vid = B.submit(vreqs, max_usd=vceiling)
    ST.add_batch(bst, batch_id=vid, kind="verify", submitted_at=_now(), count=len(vreqs))
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
    by_id = {c["card"]["id"]: c for c in cards if isinstance(c.get("card"), dict)}
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

    for cid, ex in extractions.items():
        if not ex.get("ok"):
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

        # DONE MEANS THE CYCLE FINISHED, not that something survived it.
        #
        # This call used to sit below the `continue` on the next branch, so a card
        # whose observations were all refuted or unverified was never marked done.
        # has_changed() returns True for any status that is not done, so from that
        # week on the ORDINARY, unflagged, scheduled Monday refresh re-extracted
        # that card every single week, forever, with no byte having moved. On the
        # 17-Aug run 232 of 371 extractions were discarded, so most cards land
        # here. state.mark_done's own docstring already said "including when the
        # verdict was 'nothing survived', which is a completed cycle" — the code
        # contradicted its contract, and the contract was right.
        ST.mark_done(sources_state, card_id)

        entry = by_id.get(card_id)
        if entry is None or not kept:
            continue
        src = ST.get_source(sources_state, card_id) or {}
        # The fetch date travels with the evidence. Stamping today onto a
        # sentence read two days ago makes a stale citation look fresh, and
        # freshness is a third of an L8 grade.
        proposals.extend(D.observations_to_proposals(
            entry, kept, src.get("url", ""),
            fetched_on=str(src.get("fetched_at") or "")))

    ST.save_state(sources_state)
    ok(f"{survived} observations survived verification, {killed} refuted or unverified")
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
        ST.record_source(st, key, url=url, content_sha256=res.text_sha256,
                         fetched_at=_now(), status="ok")

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
    changes = B.run_sync(reqs)
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
    ok("for the issuer-by-issuer evidence backlog and what clearing it costs, run: "
       "python3 pipeline/cli.py evidence")
    return 0


# ---------------------------------------------------------------------------
# evidence — how far from "verified" the catalogue is, and what finishing costs
#
# A sibling of `metrics` rather than more rows inside it, for two reasons that are
# about the reader rather than the code.
#
# `metrics` answers "did this week make the data better than last week". Its
# output is a flat row appended to metrics.jsonl and rendered as a trend table
# with sparklines, and report.compute_metrics is documented as flat BY DESIGN —
# a per-issuer breakdown is a nested object, cannot be sparklined or diffed, and
# would push a 10-row table to 30 rows of which 20 never move.
#
# `evidence` answers a different question, asked once and acted on: "how much of
# this can we prove, and what does finishing cost". That is a snapshot for a
# spending decision, not a trend. It also has to stay stable — `metrics` output is
# pasted verbatim into every weekly PR body, so adding a cost forecast there would
# put a dollar figure in front of a reviewer who is deciding something else.
# ---------------------------------------------------------------------------
def cmd_evidence(args: argparse.Namespace) -> int:
    try:
        cards = S.load_cards()
    except (ValueError, OSError) as exc:
        fail(f"could not read seed/cards.json: {exc}")
        return 1

    overrides = S.load_overrides(REPO / "pipeline" / "sources_overrides.json")
    srcs = S.resolve_sources(cards, overrides=overrides)
    rep = P.evidence_report(
        cards, srcs, ST.load_state(), per_week=args.per_week, max_usd=_max_usd(args)
    )

    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(P.render_evidence_report(rep))
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
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="stage 1: fetch sources, submit extraction batch")
    r.add_argument("--card-id", default="")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--force", action="store_true", help="ignore content hashes (full sweep)")
    r.add_argument(
        "--unsourced-only",
        action="store_true",
        help="read only the cards whose REWARD RULES cite no document, least-recently "
             "tried first and own-page-before-shared-page, skipping any card already "
             "read end to end at its current bytes. Opens the content-hash gate for "
             "exactly those card ids; every other card stays gated. Refused together "
             "with --force. Composes with --limit (spread the backlog over weeks), "
             "--card-id and --dry-run. The run prints how much of the selection the "
             "gate was actually blocking before it prints the cost. See `evidence` for "
             "the size of the backlog and what clearing it costs.",
    )
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max-usd", type=_nonneg_usd, default=None, help="cap this batch's estimated spend in USD; 0 disables the cap and then requires --i-accept-usd N. Negative is rejected. The cap is PER BATCH — verification is a second batch against the same cap. Default comes from config.MAX_BATCH_USD.")
    r.add_argument(
        "--i-accept-usd", type=_nonneg_usd, default=None,
        help="the amount you are knowingly accepting when --max-usd 0 removes the "
             "ceiling. Must be at least this run's printed both-passes forecast.",
    )
    r.add_argument(
        "--allow-concurrent-batch", action="store_true",
        help="submit even though a batch is still in flight. Off by default: a "
             "second extraction batch collected in the same window used to delete "
             "the first one's paid-for results.",
    )
    r.set_defaults(fn=cmd_refresh)

    a = sub.add_parser("advance", help="stages 2-3: collect batches, propose a patch")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--max-usd", type=_nonneg_usd, default=None, help="cap this batch's estimated spend in USD; 0 disables the cap and then requires --i-accept-usd N. Negative is rejected. The cap is PER BATCH — verification is a second batch against the same cap. Default comes from config.MAX_BATCH_USD.")
    a.add_argument(
        "--i-accept-usd", type=_nonneg_usd, default=None,
        help="the amount you are knowingly accepting when --max-usd 0 removes the "
             "ceiling. Must be at least this run's printed both-passes forecast.",
    )
    a.add_argument(
        "--recollect",
        metavar="BATCH_ID",
        default="",
        help="reopen an already-collected batch so the next advance reads it again. "
             "Free — the results exist and are kept for 29 days; the model does not re-run. "
             "Use when a stage failed AFTER collection and left paid-for results stranded.",
    )
    a.set_defaults(fn=cmd_advance)

    n = sub.add_parser("news-watch", help="poll issuer notice pages, draft feed items")
    n.add_argument("--issuer", default="")
    n.add_argument("--force", action="store_true")
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(fn=cmd_news_watch)

    m = sub.add_parser("metrics", help="print the weekly numbers")
    m.add_argument("--write", action="store_true")
    m.set_defaults(fn=cmd_metrics)

    e = sub.add_parser(
        "evidence",
        help="how many cards cite a document, by issuer, and what finishing costs",
    )
    e.add_argument("--per-week", type=int, default=40,
                   help="cards per weekly run, for the schedule and per-run cost")
    e.add_argument("--json", action="store_true", help="the numbers only")
    e.add_argument("--max-usd", type=_nonneg_usd, default=None,
                   help="price the per-batch ceiling against this instead of "
                        "config.MAX_BATCH_USD; 0 means no ceiling. `evidence` only "
                        "reports, so no acknowledgement is needed here.")
    e.set_defaults(fn=cmd_evidence)

    d = sub.add_parser("discover", help="find each card's own issuer page")
    d.add_argument("--issuer", default="", help="one issuer slug, e.g. hdfc")
    d.add_argument("--json", action="store_true")
    d.add_argument("--no-verify", action="store_true",
                   help="skip the fetch that confirms a page names its card (fast, unsafe)")
    d.add_argument("--write", action="store_true",
                   help="merge verified matches into pipeline/sources_overrides.json")
    d.set_defaults(fn=cmd_discover)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
