#!/usr/bin/env python3
"""
Retire the cards the 2026-08-17 cycle finished but never marked finished.

    python3 tools/repair_pipeline_state.py            # report only, writes nothing
    python3 tools/repair_pipeline_state.py --write    # apply

WHAT WENT WRONG
---------------
pipeline/cli.py stage 3 skipped `mark_done` whenever a card produced no surviving
observation. `has_changed()` needs BOTH "bytes unchanged" AND "status == done" before
it will skip a card, so those cards were re-fetched and re-extracted every Monday,
forever. The bug is fixed in cli.py; this repairs the state it already left behind.

WHAT COUNTS AS EVIDENCE
-----------------------
The only evidence that survives a CI run is what got committed. Three files:

  pipeline/state/batch.json        both 2026-08-17 batches are 'collected', so stage 2
                                   and stage 3 both ran to completion
  pipeline/state/extractions.json  371 extraction results, every one ok=True
  pipeline/state/sources.json      the per-card status the run left behind

A card is retired ONLY when all four of these hold:

  1. its extraction came back ok, and
  2. carried ZERO observations, and
  3. the URL behind it is that card's OWN page — not an issuer card-LISTING page
     shared with other cards, and
  4. `refresh` can actually fetch that card, so something could re-open it later.

THE THREE FILTERS THIS ADDS, AND WHY EACH ONE IS NOT OPTIONAL
-------------------------------------------------------------
The first version of this tool applied only (1) and (2), and retired 139 cards. That
was wrong in three separate ways, each of which loses cards permanently:

NOT THIS CARD'S PAGE (109 of the 139). 21 ICICI cards sat on one
`/personal-banking/cards/credit-card` page, 19 BOBCARD on `/credit-card`, 18 SBI on
`/personal/credit-cards.page`. "Zero observations" from a portfolio listing page is a
source-resolution failure, not the finding "this bank publishes nothing we can use".
Two consequences, both bad. The hash gate becomes all-or-nothing per listing page: one
cosmetic tweak to the ICICI listing re-bills 21 cards in the same second. And — the
one that cannot be traded away — a card whose own T&C genuinely moves is never
re-read, because the bytes we watch belong to a page that has nothing to do with it.

NOT A DOCUMENT AT ALL. 19 live BOBCARD cards were retired on 188 characters:

    'FAQ\\nCareers\\nGet In Touch\\nAbout Us\\nLogin\\nView Cards\\nCompare Cards\\nOffers\\n
     BOBCARD On UPI\\nCredit Card Payment\\nTrack Application\\nApply Now\\n...'

That is the navigation menu. It mentions none of the 19 cards by name. fetch.py's only
content guard, `_looks_like_js_shell`, requires the extracted text to be COMPLETELY
empty AND `<noscript>` present, so 188 characters of nav passes as a healthy read.
config.MIN_SOURCE_CHARS is the floor that catches it. Those 19 cards ship 65 reward
rules to users between them.

UNREACHABLE BY `refresh` (10 cards). 9 are `is_active=0` so resolve_sources skips
them, and sc_priority_visa_infinite's URL fails the issuer allowlist. They were
'fetched', so they would have come back the moment refresh coverage was extended.
Marked 'done' they never will — nothing will ever re-open a card the fetcher cannot
reach. (Those 9 inactive cards still rank in the app: the repo's own validator raises
L6.INACTIVE_CARD_STILL_RANKS as an ERROR on them. That is a separate decision and this
tool does not make it.)

Cards failing filter 3 or the length floor are recorded as `unresolved_source`, which
is NOT a done reason, so `has_changed()` keeps returning True for them and they stay
visible as the source-resolution backlog they actually are. Cards failing filter 4 are
left exactly as they were.

WHAT IS DELIBERATELY LEFT ALONE
-------------------------------
Cards whose extraction DID carry observations but which are still 'fetched'. On the
2026-08-17 data that is 165 cards, and they are two different populations mixed
together:

  * ~156 went to the adversary and had every observation refuted. Genuinely finished.
  * ~9 never reached the adversary at all. The stage-2 log for run 32054381953 says
    "9 cards skipped: source changed since extraction — deferred to the next run".

The verdicts from that run were never committed, so there is no per-card way to tell
which 9. Marking all 165 would retire up to 9 cards nobody ever checked. Paying to
re-read 165 cards once more is the cheap error; silently retiring an unchecked card is
the expensive one, so they stay unfinished and the fixed cli.py will label them
correctly at the end of the next cycle.

SAFETY
------
This only ever sets a status. It never edits a URL, a hash or a fetch time, and it
never touches seed/ or news/. `has_changed()` still re-reads any card whose source
bytes move, whatever its status says — so no card that genuinely needs re-reading can
be lost by running this.

Stdlib only.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline import batch as B      # noqa: E402  (stdlib-only at import; SDK is lazy)
from pipeline import config as C     # noqa: E402
from pipeline import sources as S    # noqa: E402
from pipeline import state as ST     # noqa: E402


def custom_id_map(sources: dict) -> dict[str, str]:
    """{extraction custom_id -> card_id}, rebuilt the way cli.py rebuilds it.

    custom_ids are sanitised and may carry a hash suffix, so they are not recoverable
    from the card id by string surgery. Regenerating them from the committed card list
    is the same trick stage 2 and stage 3 both use, and it is exact.
    """
    return {
        B.build_extract_request(cid, "x", "https://x.invalid", "x" * 200)["custom_id"]: cid
        for cid in sources
        if not cid.startswith("__watch__")
    }


def fetchable_card_ids() -> set[str]:
    """Every card `pipeline/cli.py refresh` can actually fetch, with a URL.

    A card outside this set can never be re-opened by anything, so it must never be
    marked done: `done` is only safe because a later fetch can undo it.
    """
    cards = S.load_cards()
    overrides = S.load_overrides(REPO / "pipeline" / "sources_overrides.json")
    return {s.card_id for s in S.resolve_sources(cards, overrides=overrides) if s.url}


def classify(sources: dict, extractions: dict) -> dict[str, list[str]]:
    """Sort every extraction result into what we can and cannot prove about it."""
    by_custom = custom_id_map(sources)
    reachable = fetchable_card_ids()
    out: dict[str, list[str]] = collections.defaultdict(list)

    for custom_id, result in extractions.items():
        card_id = by_custom.get(custom_id)
        if card_id is None:
            out["unmapped"].append(custom_id)
            continue
        if not result.get("ok"):
            out["extraction_failed"].append(card_id)
            continue

        entry = sources.get(card_id) or {}
        status = entry.get("status")
        n_obs = len((result.get("data") or {}).get("observations") or [])

        if status == ST.STATUS_DONE:
            out["already_done"].append(card_id)
            continue
        if status != "fetched":
            # fetch_failed / ok / anything else: not ours to touch.
            out["other_status"].append(card_id)
            continue
        if n_obs:
            out["leave_unproven"].append(card_id)
            continue

        # Zero observations. Now: was that a finding, or a failure to find the card?
        if card_id not in reachable:
            out["unreachable_by_refresh"].append(card_id)
        elif not S.is_card_specific(sources, card_id):
            out["unresolved_shared_url"].append(card_id)
        else:
            chars = entry.get("text_chars")
            if isinstance(chars, int) and chars < C.MIN_SOURCE_CHARS:
                out["unresolved_too_short"].append(card_id)
            else:
                out["repair_no_observations"].append(card_id)

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true",
                    help="apply the repair; without it nothing is written")
    args = ap.parse_args()

    state = ST.load_state()
    sources = state.get("sources", {})
    if not sources:
        print("FAIL pipeline/state/sources.json has no sources — nothing to repair")
        return 1

    try:
        extractions = json.loads(C.EXTRACTIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"FAIL pipeline/state/extractions.json unreadable ({exc}) — "
              f"without it there is no evidence, so nothing is repaired")
        return 1

    # The batch file is the proof that stage 3 actually ran over these extractions.
    # Without it we would be marking cards done on the strength of a file that could
    # have been written by a run that then died before judging anything.
    batches = ST.load_batch_state().get("batches", [])
    extract_collected = [b for b in batches
                         if b.get("kind") == "extract" and b.get("status") == "collected"]
    if not extract_collected:
        print("FAIL no collected extraction batch in pipeline/state/batch.json — "
              "cannot show that these extractions were ever judged. Nothing repaired.")
        return 1

    groups = classify(sources, extractions)
    repair = sorted(groups["repair_no_observations"])
    shared = sorted(groups["unresolved_shared_url"])
    short = sorted(groups["unresolved_too_short"])

    print("=== Evidence ===")
    print(f"OK   {len(sources)} entries in sources.json "
          f"({sum(1 for k in sources if k.startswith('__watch__'))} are news watch pages)")
    print(f"OK   {len(extractions)} extraction results, "
          f"{sum(1 for v in extractions.values() if v.get('ok'))} ok")
    for b in batches:
        print(f"OK   batch {b.get('batch_id')} kind={b.get('kind')} "
              f"count={b.get('request_count')} status={b.get('status')}")

    counts = collections.Counter(v.get("status") for v in sources.values())
    print("\n=== Status before ===")
    for status, n in counts.most_common():
        print(f"     {n:>4}  {status}")

    print("\n=== Verdict ===")
    print(f"OK   {len(repair)} cards RETIRED: extraction ok, ZERO observations, on that "
          f"card's OWN page, and reachable by refresh")
    print(f"WARN {len(shared)} cards NOT retired: their 'zero observations' came from an "
          f"issuer LISTING page shared with other cards, which is a source-resolution "
          f"gap, not a finding -> {ST.STATUS_UNRESOLVED_SOURCE}")
    if shared:
        top = collections.Counter((sources.get(c) or {}).get("url") for c in shared)
        for url, n in top.most_common(6):
            print(f"     {n:>4}  {url}")
    print(f"WARN {len(short)} cards NOT retired: their source document was under "
          f"{C.MIN_SOURCE_CHARS:,} characters -> {ST.STATUS_UNRESOLVED_SOURCE}")
    print(f"WARN {len(groups['unreachable_by_refresh'])} cards LEFT ALONE: `refresh` "
          f"cannot fetch them at all (inactive, or no allowlisted URL), so nothing "
          f"could ever re-open them if they were marked done")
    for cid in sorted(groups["unreachable_by_refresh"]):
        print(f"          {cid}")
    print(f"WARN {len(groups['leave_unproven'])} cards LEFT UNFINISHED: they had "
          f"observations, and the verdicts were never committed, so there is no "
          f"per-card proof the adversary saw them")
    for label, key in (("already done", "already_done"),
                       ("other status, untouched", "other_status"),
                       ("extraction failed, untouched", "extraction_failed"),
                       ("unmapped custom_id", "unmapped")):
        if groups[key]:
            print(f"     {len(groups[key]):>4}  {label}")

    if not args.write:
        print("\nOK   report only — nothing written. Re-run with --write to apply.")
        return 0

    for card_id in repair:
        ST.mark_done(state, card_id, ST.DONE_NO_OBSERVATIONS, card_specific=True)
    for card_id in shared:
        url = (sources.get(card_id) or {}).get("url", "")
        ST.mark_unresolved_source(
            state, card_id,
            note=f"{url} is an issuer listing page shared with other cards, not this "
                 f"card's own terms — 'nothing found' says nothing about it",
        )
    for card_id in short:
        entry = sources.get(card_id) or {}
        ST.mark_unresolved_source(
            state, card_id,
            note=f"only {entry.get('text_chars')} chars of text at "
                 f"{entry.get('url', '')} — too short to be this card's terms",
        )
    ST.save_state(state)

    after = collections.Counter(v.get("status") for v in ST.load_state()["sources"].values())
    print("\n=== Status after ===")
    for status, n in after.most_common():
        print(f"     {n:>4}  {status}")
    print(f"\nOK   wrote {C.SOURCE_STATE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
