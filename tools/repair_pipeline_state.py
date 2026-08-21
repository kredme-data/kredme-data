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

A card is repaired ONLY when its extraction came back ok AND carried ZERO observations.
That pair is per-card proof of a completed cycle: the extractor read the document and
found nothing worth proposing, so there was nothing to verify, so stage 3 had nothing
left to do for that card. Its answer is "this bank publishes nothing we can use", which
is a result — and it is now recorded as one, with done_reason 'no_observations'.

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
This only ever sets status to 'done'. It never edits a URL, a hash or a fetch time, and
it never touches seed/ or news/. `has_changed()` still re-reads any card whose source
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


def classify(sources: dict, extractions: dict) -> dict[str, list[str]]:
    """Sort every extraction result into what we can and cannot prove about it."""
    by_custom = custom_id_map(sources)
    out: dict[str, list[str]] = collections.defaultdict(list)

    for custom_id, result in extractions.items():
        card_id = by_custom.get(custom_id)
        if card_id is None:
            out["unmapped"].append(custom_id)
            continue
        if not result.get("ok"):
            out["extraction_failed"].append(card_id)
            continue

        status = (sources.get(card_id) or {}).get("status")
        n_obs = len((result.get("data") or {}).get("observations") or [])

        if status == ST.STATUS_DONE:
            out["already_done"].append(card_id)
        elif status != "fetched":
            # fetch_failed / ok / anything else: not ours to touch.
            out["other_status"].append(card_id)
        elif n_obs == 0:
            out["repair_no_observations"].append(card_id)
        else:
            out["leave_unproven"].append(card_id)

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

    print(f"=== Evidence ===")
    print(f"OK   {len(sources)} entries in sources.json "
          f"({sum(1 for k in sources if k.startswith('__watch__'))} are news watch pages)")
    print(f"OK   {len(extractions)} extraction results, "
          f"{sum(1 for v in extractions.values() if v.get('ok'))} ok")
    for b in batches:
        print(f"OK   batch {b.get('batch_id')} kind={b.get('kind')} "
              f"count={b.get('request_count')} status={b.get('status')}")

    counts = collections.Counter(v.get("status") for v in sources.values())
    print(f"\n=== Status before ===")
    for status, n in counts.most_common():
        print(f"     {n:>4}  {status}")

    print(f"\n=== Verdict ===")
    print(f"OK   {len(repair)} cards repaired: extraction ok, ZERO observations — "
          f"read, nothing to propose, nothing to verify")
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
        ST.mark_done(state, card_id, ST.DONE_NO_OBSERVATIONS)
    ST.save_state(state)

    after = collections.Counter(v.get("status") for v in ST.load_state()["sources"].values())
    print(f"\n=== Status after ===")
    for status, n in after.most_common():
        print(f"     {n:>4}  {status}")
    print(f"\nOK   wrote {C.SOURCE_STATE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
