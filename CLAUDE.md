# kredme-data — the data real users are served

The OTA backend the shipped KredMe app fetches at runtime: `seed/{manifest,cards,merchants}.json` +
`news/feed.json`, served from `main` via GitHub Pages. Data plus one Python publish gate. No application code.

Remote: `github.com/kredme-data/kredme-data` (**public** — the whole repo is web-reachable, including
`RUNBOOK.md` and `tools/`). Live base: `https://kredme-data.github.io/kredme-data`

---

## ⚠️ Two things that will mislead you immediately

**1. `feat/safe-publish-pipeline` and `v5.2/canonical-rate-fields` are dead — both PRs CLOSED 09-Aug.**
Merging the feat branch would have deleted the CI workflow and re-added committed `.pyc` files. v5.2 read
`MERGEABLE/CLEAN` but replaced `cards.json` wholesale with a May snapshot, which would have reverted every
rate fix — its one salvageable idea (`cashback_inr` point values) was harvested into #12 first.

**2. `kredme.py` reads LOCAL branch refs, never `origin`** (`tools/kredme.py:664-673`). Local `main` and `dev`
are both stale here, so `status` reports "dev and prod data are the same" when they are not, and
`validate --target dev` returns errors against a branch that is clean on origin. **Sync first, every time:**

```bash
git fetch origin dev:dev && git pull --ff-only origin main
```

⚠️ The documented `git fetch origin dev:dev main:main` **fails** from `main` or `dev` — exit 128,
"refusing to fetch into branch refs/heads/main checked out at …", and *nothing* updates. Since `promote`
refuses to run anywhere but `main`, that documented form and the promote workflow are mutually exclusive.
Use the recipe above.

## Branches

`main` (authoritative — Pages serves it, every store build reads it) · `dev` (read by dev APKs /
TestFlight via the app's data switcher) · `feat/safe-publish-pipeline` and `v5.2/canonical-rate-fields`
(both dead, PRs closed 09-Aug)

**`dev` IS read by dev builds.** (Corrected 09-Aug — the older claim came from a checkout of the app that
was 35 commits stale.) `KredMe-main` on `nous/master` has `lib/core/config/data_source.dart`:
dev -> `raw.githubusercontent.com/kredme-data/kredme-data/dev`, chosen at runtime via
Settings -> Developer Options -> data source, gated by the compile-time `DEV_MODE` flag so a store build
cannot be pointed at dev by any means. **In dev the app skips Firestore entirely**, so the
Firestore-override trap below applies to prod only.

> **The data users are served fails this repo's own validator, and has since 2026-08-01.**
> `main` = seed 5.1.0 / news 1.0.0, **3 errors + 3 warnings**, CI red.
> `dev` = seed **5.1.4** / news 2.0.0, **0 errors + 0 warnings**, CI green, never promoted.
> dev now also carries the numeric gate (#9), 4 verified base rates (#10), and the self-consistency
> gate + 136 rate corrections + 29 `cashback_inr` point values (#12).
> The fix already exists. Don't re-diagnose those three errors — promote and push.
> ⚠️ `dev` also re-categorises 35 merchants, which changes which reward rules fire. Read that diff first.

The three errors: `csd_canteen` → `departmental_store` (category doesn't exist); `news_001` uses `expires_at`
where the app reads `expiry_date`; `news_001` uses `url` where the app reads `source_url`.

## The weekly pipeline (`pipeline/`)

Two automated loops added 2026-08-13, both landing in `dev` behind a PR: a **Monday
card-data refresh** (fetch issuer docs -> extract -> adversarially verify -> field-level
patch) and a **daily news watch** (poll issuer notice pages -> draft `news/feed.json`).
Operator guide, costs and known gaps: [PIPELINE.md](PIPELINE.md).

```bash
python3 pipeline/cli.py refresh --dry-run     # costs nothing, submits nothing
python3 pipeline/cli.py advance               # idempotent; collects whatever is in flight
python3 pipeline/cli.py news-watch --dry-run
python3 pipeline/cli.py metrics
python3 pipeline/cli.py evidence              # the unsourced backlog + what clearing it costs
python3 pipeline/cli.py refresh --unsourced-only --limit 40 --dry-run
python3 tests/run_all.py                      # 813 tests, stdlib unittest, no pip, no network
```

**Six things about it that are not obvious:**

1. **It uses the Message Batches API, not synchronous calls.** A 380-card sweep was
   measured at ~11.6h and Actions kills a job at 6. `refresh` submits and exits;
   `pipeline-advance.yml` collects on a 2-hourly cron. Three short jobs, no limit problem.
2. **`pipeline/state/sources.json` is tracked on purpose.** It holds each source
   document's text hash and is the only reason the weekly run is affordable - cards whose
   bytes did not move never reach the model. Deleting it forces a full paid sweep — **measured
   17-Aug at $94.55 for both passes (~Rs 8,200), not the ~Rs 4,400 previously written here.**
   ⚠️ The gate only skips a card once it is marked `done`, and `mark_done` runs ONLY in stage 3.
3. **Two model passes, and the second is an adversary.** A single pass was measured getting
   100% of its numbers right and 78% of its "I could not find this" claims wrong. An
   observation with no verdict is treated as refuted and does not ship.
4. **It never publishes to users.** It opens a PR. Of 18 changes a first pass called
   "confirmed at the issuer", a second pass refuted 6. Revisit when that rate is ~0.
5. **It only NOTICED change; now it can also go and get evidence.** The hash gate means a
   card whose rates were never verified, whose page did not move this week, is skipped
   forever — 361 of 370 active cards have no reward rule citing a document, and the
   validator has read 2.0% issuer-sourced through every repair PR because internal
   consistency is not evidence. `refresh --unsourced-only` selects exactly those cards,
   drops the ones already read end-to-end at their current bytes (so the queue
   terminates), and says before spending how much of the selection the hash gate was
   actually blocking versus how much an ordinary refresh would fetch anyway. Composes
   with `--limit`; `pipeline/cli.py evidence` prices the backlog — ~$95 / ~₹8,300 for all
   301, ~$12.68 a week at `--limit 40`. What counts as evidence lives in ONE function
   that L8 imports — see PIPELINE.md, "The evidence backlog".
6. **This contradicts the stdlib-only rule below, deliberately and narrowly.** `pipeline/`
   is the ONLY thing here allowed a third-party import (`anthropic`), and it is imported
   lazily inside the calling functions so `tools/kredme.py` and the entire test suite still
   run on a bare Python. `tests/run_all.py` asserts that and CI fails if someone breaks it.

## Commands

```bash
python3 tools/kredme.py status                  # dev vs prod + restore points (sync refs first!)
python3 tools/kredme.py validate --target prod  # what users get right now — currently exit 1
python3 tools/kredme.py validate --target dev   # currently exit 0
python3 tools/test_pipeline.py                  # 29 self-tests, ~1s, stdlib only
python3 tools/kredme.py promote --dry-run       # shows what prod would receive, writes nothing
python3 tools/kredme.py promote --yes           # dev → prod, LOCAL ONLY. Never pushes.
python3 tools/kredme.py undo --list
```

Stdlib only — no venv, no requirements.txt. CI pins Python 3.12; local is 3.14.3.
Output is ANSI-coloured; pipe through `sed 's/\x1b\[[0-9;]*m//g'` when capturing.

`promote` requires: HEAD on `main`, clean `seed/` and `news/`, synced refs. It **exits 2** (not 1) when dev
validates with warnings but no errors — read that as "warnings blocked me", not a crash. It writes a snapshot
to `.published/`, modifies three files, and prints the add/commit/push commands. **A human must push.**

## Traps

**~~The validator has zero numeric plausibility checks.~~ FIXED — but read what it does and does not prove.**
The gate now replicates the app's display maths and checks: a 10% ceiling / 0.1% floor (ratcheted against
`rate_baseline.json`), a **40% hard ceiling with no baseline key that cannot be grandfathered**, and
**self-consistency** — whether a rule's number agrees with the issuer mechanics stated in its own
`rule_name`. 40% not 30% because HDFC SmartBuy 10X genuinely reaches ~33%.
**It proves the file is internally consistent, NOT that a rate is true.** If a card's stored point value is
wrong the corrected number is still wrong — HDFC Regalia Gold went 20% -> 8.6% under the gate while the
issuer truth is 1.875%. **Still never cite "validate passed" as evidence that card economics are correct.** See [the data audit](../KredMe_Data_Quality_Audit_02Aug2026.md).

**A push to `main` may not reach users at all.** The app tries a **Firestore** manifest *first*
(`seed_sync_service.dart:86`) and falls back to HTTP; a Firestore entry can carry a `url` that overrides the
base URL entirely. The "Pages/main is prod" rule holds only when Firestore is empty. Confirm before claiming
a promote shipped.

**`undo` is unusable today.** Restoring current prod produces data that fails validation, and the tool then
refuses to print the push commands: *"The restored data does NOT validate. Do not push it."* `.published/`
doesn't exist here — no promote has ever run in this clone, so there is only ever one snapshot. Fix main's
three errors before relying on undo.

**News refetches only on a whole-number version bump.** The app parses `"2.0.0".split('.')[0]`, so minor and
patch bumps are invisible. `dev` went 1.0.0 → 2.0.0 for exactly this reason. Seed sync differs — any string
change syncs. `promote` handles both; hand-editing does not.

**`merchants.json`'s `categories` block is not read by the app.** The app resolves `category_id` against its
own bundled `assets/data/categories/categories.json`, and a miss silently becomes "Other" so reward rules
never fire. That block exists only to give `validate` something to check — and because this repo is public
while the app repo is private, CI *cannot* detect drift. **Mirror any app-side category change here by hand.**

**Three files the app is coded to fetch don't exist here** (all HTTP 404): `offers/feed.json`,
`seed/card_details.json`, `seed/issuer_info.json`. The two code paths even disagree on the offers path
(`_patchExtendedKeys` uses `offers/offers.json`, `bank_offers_service` uses `offers/feed.json`). The real OTA
surface is 4 files, not 6. Adding one means editing `seed/manifest.json` **and** teaching `kredme.py` — it
hardcodes `SEED_FILES = ('cards.json','merchants.json')` at line 69.

**The card-count shrink guard compares against your local working tree, not live** (`kredme.py:285-299`).
A stale checkout measures the floor against the wrong baseline. Another reason to sync and be on `main`.

**Nothing generates `seed/cards.json`.** `kredme-card-data` (the 394-card scraper) is not wired to this repo;
its Firestore output is never read. Every `cards.json` change is a manual edit or an out-of-band drop.
Never assume regenerating is possible.

**`RUNBOOK.md §B1` is wrong.** It says to merge `v5.2/canonical-rate-fields` as a fast-forward; it no longer
is (`git merge-base --is-ancestor` exits 1), and it predates dev's fix to the same files. Promote dev first,
then treat v5.2 as content to rebase. Read the RUNBOOK for app-schema facts, not procedure — §A1/§B2 describe
the hand-edit workflow that `promote` replaced.

## CI

One workflow, `validate.yml`, on `main` and `dev`: `test_pipeline.py` → `validate --target working` →
an inline strict checksum script (needed because `--target working` runs with `strict_checksums=False`).

**Currently red on `main`, green on `dev`** — the branch users are served fails; the branch nobody reads passes.
A permanently red default branch teaches everyone to ignore CI; fix it by promoting.

GitHub's built-in `pages-build-deployment` runs on every `main` push (~50s) — that is the actual deploy.
Actions minutes are free here (public repo), unlike the app repo.

## Conventions

Commit subjects are **plain-English sentences**, not conventional commits: *"Fix the three errors in the live
data, and make dev a real lane"*. Only the dead `feat/` branch used `feat:`/`fix:` prefixes — don't copy it.
Everything lands through PRs with merge commits. Branches: `data/<thing>`, `v5.x/<thing>` for seed versions.

Python: stdlib only, one file with section-banner comments, prints OK/WARN/FAIL.

**Data flow is one-way and enforced:** edit on `dev` → validate → test on device → `promote` (from `main`)
→ push. `seed/` and `news/` on `main` should only ever be written by `promote`. If you do hand-edit a seed
file, the manifest checksum **must** be regenerated or the app rejects the sync with "Sync failed".

## Layout

```
tools/kredme.py         ~1,260 lines — the whole pipeline (status/validate/promote/undo) + numeric gate
tools/test_pipeline.py  44 tests
seed/manifest.json      1,205 b
seed/cards.json         1,948,662 b — 376 cards / 1,202 reward rules
seed/merchants.json     53,282 b — 116 merchants, 29 categories
news/feed.json          445 b — ONE placeholder item from 2026-03-29, never updated
delta/                  README only. Zero delta files ever produced; manifest delta_file is null.
```

19 tracked files total, two of which are committed `.pyc`.

## Don't touch

- `.published/`, `staging/` — gitignored, tool-managed. Never commit or serve.
- `tools/__pycache__/*.pyc` — genuinely tracked on the current checkout. `main` deletes them; don't re-add.
- `a.md`, `b.md` (dev only) — PR-description drafts committed by mistake. Not documentation.
- `feat/safe-publish-pipeline` — dead branch. Merging it deletes CI and re-adds the `.pyc` files.
- `seed/cards.json` — 1.86 MB machine-produced blob. Never hand-edit without regenerating manifest checksums.
- `test/` — fixtures with deliberate versions like `0.0.1-test`. Don't "fix" them.
- `delta/` — dormant. `manifest.delta_file` must stay `null` unless a delta file is actually generated.

## Related repos

- `../KredMe-main` — the app that reads this. Its `CLAUDE.md` has the reward-unit trap.
- `../kredme-card-data` — 394-card scraper, **not connected to this repo**.
