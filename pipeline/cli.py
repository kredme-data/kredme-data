#!/usr/bin/env python3
"""
The pipeline entrypoint. Three commands, one per CI stage.

Usage:
    python3 pipeline/cli.py refresh [--card-id ID] [--limit N] [--force] [--dry-run]
    python3 pipeline/cli.py advance [--dry-run]
    python3 pipeline/cli.py news-watch [--issuer NAME] [--force] [--dry-run]
    python3 pipeline/cli.py metrics [--write]

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
# refresh — stage 1
# ---------------------------------------------------------------------------
def cmd_refresh(args: argparse.Namespace) -> int:
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
    unchanged = failed = 0

    for i, s in enumerate(resolved, 1):
        res = F.fetch_source(s.url)
        if not res.ok or not res.text:
            failed += 1
            ST.record_source(
                st, s.card_id, url=s.url, content_sha256=None,
                fetched_at=_now(), status="fetch_failed", note=res.error or "empty text",
            )
            if failed <= 10:
                warn(f"{s.card_id}: {res.error or 'no text extracted'}")
            continue

        if not args.force and not ST.has_changed(st, s.card_id, res.text_sha256):
            unchanged += 1
            ST.record_source(
                st, s.card_id, url=s.url, content_sha256=res.text_sha256,
                fetched_at=_now(), status="unchanged",
            )
            continue

        changed.append((s, res.text))
        ST.record_source(
            st, s.card_id, url=s.url, content_sha256=res.text_sha256,
            fetched_at=_now(), status="ok",
        )
        if i % 25 == 0:
            print(f"     ... {i}/{len(resolved)}")

    ok(f"fetched {len(resolved)}: {len(changed)} changed, {unchanged} unchanged, {failed} failed")
    if unchanged and not args.force:
        ok(f"skipped {unchanged} cards whose source bytes did not move — this is the saving")

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

    try:
        batch_id = B.submit(reqs)
    except Exception as exc:  # noqa: BLE001 - surface any SDK/transport failure as exit 2
        fail(f"batch submission failed: {exc}")
        return 2

    ok(f"submitted extraction batch {batch_id}")
    bst = ST.load_batch_state()
    ST.add_batch(bst, batch_id=batch_id, kind="extract", submitted_at=_now(), count=len(reqs))
    ST.save_batch_state(bst)
    ST.save_state(st)

    # The card text goes to scratch, not to git: verification needs the same bytes the
    # extractor saw, and re-fetching at collect time could silently read a changed page.
    payload = {s.card_id: text for s, text in changed}
    (_work_dir() / "documents.json").write_text(json.dumps(payload), encoding="utf-8")
    ok("stage 1 complete — pipeline-advance.yml will collect when the batch ends")
    return 0


# ---------------------------------------------------------------------------
# advance — stages 2 and 3
# ---------------------------------------------------------------------------
def cmd_advance(args: argparse.Namespace) -> int:
    bst = ST.load_batch_state()

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


def _load_documents() -> dict[str, str]:
    p = C.WORK_DIR / "documents.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


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

    (_work_dir() / "extractions.json").write_text(json.dumps(results), encoding="utf-8")
    ST.mark_batch(bst, bid, "collected")

    docs = _load_documents()
    if not docs:
        warn("scratch documents are gone (a fresh runner) — cannot verify without the "
             "exact bytes the extractor read; re-run refresh")
        ST.save_batch_state(bst)
        return 1

    head("Building verification batch")
    vreqs = []
    for cid, res in good.items():
        _, card_id = B.parse_custom_id(cid)
        data = res.get("data") or {}
        obs = data.get("observations") or []
        if not obs:
            continue
        text = docs.get(card_id) or next(
            (t for k, t in docs.items() if B.build_extract_request(k, "", "", "")["custom_id"] == cid),
            "",
        )
        if not text:
            continue
        vreqs.append(B.build_verify_request(card_id, text, obs))

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

    vid = B.submit(vreqs)
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
    ST.mark_batch(bst, bid, "collected")
    ST.save_batch_state(bst)

    try:
        extractions = json.loads((C.WORK_DIR / "extractions.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail("extractions.json missing — re-run refresh")
        return 1

    head("Applying verdicts")
    cards = S.load_cards()
    by_id = {c["card"]["id"]: c for c in cards if isinstance(c.get("card"), dict)}
    proposals: list[D.Proposal] = []
    survived = killed = 0

    for cid, ex in extractions.items():
        if not ex.get("ok"):
            continue
        kind, card_id = B.parse_custom_id(cid)
        obs = (ex.get("data") or {}).get("observations") or []
        vres = verdicts.get(cid.replace("extract::", "verify::"), {})
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

        entry = by_id.get(card_id)
        if entry is None or not kept:
            continue
        src = ST.get_source(ST.load_state(), card_id) or {}
        proposals.extend(D.observations_to_proposals(entry, kept, src.get("url", "")))

    ok(f"{survived} observations survived verification, {killed} refuted or unverified")
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
            json.dumps(new_cards, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
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
        json.dumps(man, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
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
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="stage 1: fetch sources, submit extraction batch")
    r.add_argument("--card-id", default="")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--force", action="store_true", help="ignore content hashes (full sweep)")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_refresh)

    a = sub.add_parser("advance", help="stages 2-3: collect batches, propose a patch")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_advance)

    n = sub.add_parser("news-watch", help="poll issuer notice pages, draft feed items")
    n.add_argument("--issuer", default="")
    n.add_argument("--force", action="store_true")
    n.add_argument("--dry-run", action="store_true")
    n.set_defaults(fn=cmd_news_watch)

    m = sub.add_parser("metrics", help="print the weekly numbers")
    m.add_argument("--write", action="store_true")
    m.set_defaults(fn=cmd_metrics)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
