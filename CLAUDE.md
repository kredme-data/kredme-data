# kredme-data — the data real users are served

The OTA backend the shipped KredMe app fetches at runtime: `seed/{manifest,cards,merchants}.json` +
`news/feed.json`, served from `main` via GitHub Pages. Data, the Python tooling that validates and publishes
it, and the automated refresh pipeline. No application code.

Remote: `github.com/kredme-data/kredme-data` (**public** — the whole repo is web-reachable, including
`RUNBOOK.md` and `tools/`). Live base: `https://kredme-data.github.io/kredme-data`

---

## ⚠️ Two things that will mislead you immediately

**1. `feat/safe-publish-pipeline` and `v5.2/canonical-rate-fields` are dead — both PRs CLOSED 09-Aug.**
Merging the feat branch would have deleted the CI workflow and re-added committed `.pyc` files. v5.2 read
`MERGEABLE/CLEAN` but replaced `cards.json` wholesale with a May snapshot, which would have reverted every
rate fix — its one salvageable idea (`cashback_inr` point values) was harvested into #12 first.

**2. `kredme.py` reads LOCAL branch refs, never `origin`** (`data_dirs`, `tools/kredme.py:1575-1584`).
Local `main` and `dev` are both stale here, so `status` reports "dev and prod data are the same" when they
are not, and
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

> **That backlog is cleared. Both branches validate clean and both are green in CI.** (Re-measured 25-Aug.)
> `main` = seed **5.1.38** / news **9.0.0**, **0 errors + 0 warnings**. The last three `validate.yml` runs on
> `main` all succeeded: 32664992255, 32553730626, 32471577455.
> `dev` = seed **5.1.41** / news 9.0.0, **0 errors + 0 warnings**, green.
> The numeric gate (#9), the four verified base rates (#10) and the self-consistency gate plus its 136 rate
> corrections (#12) have all reached `main`; they are no longer dev-only. #10 and #12 merged into `dev` and
> travelled on from there by promote, so do not read their base branch as "still unshipped" - the nine check
> modules under `tools/checks/` are present on `main`, and `main` is at 5.1.38, far past the 5.1.4 they landed on.
> ⚠️ The old warning that `dev` re-categorises 35 merchants is dead - `merchants.json` and `news/feed.json`
> are now byte-identical on the two branches. What `dev` still holds back is a `cards.json` diff plus
> `seed/card_details.json`, which `main` does not carry yet. Read that diff before promoting.

```bash
git show origin/main:seed/manifest.json          # version 5.1.38, news_version 9.0.0
gh run list --repo kredme-data/kredme-data --workflow validate.yml --branch main --limit 5
git diff --stat origin/main origin/dev -- seed news
python3 tools/kredme.py validate --target prod   # "✓ prod passed — 0 errors, 0 warning(s)", exit 0
```

**The three errors this file used to open with are gone from `main`.** Do not go hunting for them.
`departmental_store` is a real entry in the `categories` block of `merchants.json` and `csd_canteen`
resolves to it; `news_001` carries `expiry_date` and `source_url`, the names the app reads, and no item in
the 134-item feed uses `expires_at` or a bare `url`. Read them yourself with
`git show origin/main:seed/merchants.json` and `git show origin/main:news/feed.json`.

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
python3 tests/run_all.py                      # 774 tests, stdlib unittest, no pip, no network
```

**Five things about it that are not obvious:**

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
5. **This contradicts the stdlib-only rule below, deliberately and narrowly.** `pipeline/`
   is the ONLY thing here allowed a third-party import (`anthropic`), and it is imported
   lazily inside the calling functions so `tools/kredme.py` and the entire test suite still
   run on a bare Python. `tests/run_all.py` asserts that and CI fails if someone breaks it.

## Commands

```bash
python3 tools/kredme.py status                  # dev vs prod + restore points (sync refs first!)
python3 tools/kredme.py validate --target prod  # what users get right now - currently exit 0
python3 tools/kredme.py validate --target dev   # currently exit 0
python3 tools/test_pipeline.py                  # 66 self-tests, ~1s, stdlib only
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

**`undo` has restore points now, but nobody has proved a restore.** Both halves of the old warning here were
out of date: promotes have run in the working clone, so `.published/` holds **6 snapshots**, the newest
`20260817-074643__seed-5.1.20__news-3.0.0` (`python3 tools/kredme.py undo --list`); and the "fix main's three
errors first" advice is moot, because prod validates clean. **What is still untested is the restore itself.**
Every snapshot predates the numeric, self-consistency and reachability gates, so a restored 5.1.20 may well
trip checks that did not exist when it was taken, and the tool refuses to print push commands when that
happens: *"The restored data does NOT validate. Do not push it."* There is no `--dry-run` on `undo`, so the
only way to find out is to run it in a throwaway checkout. Do that before you need it in an incident.

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
surface is 4 files, not 6.

**`kredme.py` can now publish the two `seed/` ones** (23-Aug). It splits `SEED_FILES_REQUIRED`
(`cards.json`, `merchants.json` — must exist and must be declared) from `SEED_FILES_OPTIONAL`
(`card_details.json`, `issuer_info.json` — published when present, skipped when absent). **Drop the file in
`seed/` on `dev`, commit, `validate`, `promote`.** That is the whole procedure: promote copies it and rebuilds
the manifest entry from the file on disk. The app already models, fetches and renders both, so **no app
release is involved** — `CardDetailsService` and `IssuerInfoService` are live on `master` today and treat a
missing file as an empty section. `offers/` is still not wired; it needs a path decision first.

⚠️ **Never hand-write a manifest entry.** `seed_sync_service.dart:_applyFullSync` returns false on ANY
non-200 and aborts *before* saving the local version, so an entry naming a file that 404s does not degrade
one tab — it stops card syncing for every user, on every cold start, forever, with no backoff, re-downloading
2.7 MB each time and discarding it. `kredme.py` is safe by construction (every entry is derived from a file
just confirmed on disk) and `validate` errors on any declared-but-missing file. Both are covered by tests in
`tools/test_pipeline.py`. A hand-edited or Firestore-edited manifest has neither protection.

**The card-count shrink guard compares against your local working tree, not live** (`kredme.py:1294-1302`,
baseline from `live_card_count` at `kredme.py:363-377`).
A stale checkout measures the floor against the wrong baseline. Another reason to sync and be on `main`.

**Nothing generates `seed/cards.json`.** `kredme-card-data` (the 394-card scraper) is not wired to this repo;
its Firestore output is never read. Every `cards.json` change is a manual edit or an out-of-band drop.
Never assume regenerating is possible.

**`RUNBOOK.md §B1` is wrong.** It says to merge `v5.2/canonical-rate-fields` as a fast-forward; it no longer
is (`git merge-base --is-ancestor` exits 1), and it predates dev's fix to the same files. Promote dev first,
then treat v5.2 as content to rebase. Read the RUNBOOK for app-schema facts, not procedure — §A1/§B2 describe
the hand-edit workflow that `promote` replaced.

## CI

**Six workflows now, not one** (`git ls-tree -r --name-only origin/main -- .github/workflows`). The gate is
still `validate.yml`, on push and PR to `main` and `dev`: `test_pipeline.py` → `validate --target working` →
an inline strict checksum script (needed because `--target working` runs with `strict_checksums=False`).
The other five are the automation: `weekly-refresh.yml` (Mondays 03:00 UTC, stage 1), `pipeline-advance.yml`
(every 2h at :17, stages 2-3), `news-watch.yml` (daily 02:30 UTC), `data-quality.yml` (validate → fix → PR),
and `news-push.yml` (fires an FCM topic message on a push to `main` that touches `news/feed.json`).
⚠️ `news-push.yml` and `tools/setup_news_push_auth.sh` exist on `main` only, not on `dev`.

**Green on both branches.** The last three `validate.yml` runs on `main` succeeded (32664992255,
32553730626, 32471577455), and so did the last four on `dev`. Verify with
`gh run list --repo kredme-data/kredme-data --workflow validate.yml --branch main --limit 5`.

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

All of the below is measured on `origin/main`, 25-Aug. Sizes:
`git show origin/main:<path> | wc -c`. Counts: `git show origin/main:seed/cards.json` piped into a Python
`len()` over the array and over each entry's `reward_rules`. File tallies: `git ls-tree -r --name-only
origin/main -- <dir> | wc -l`. Test counts: run the suite.

```
tools/kredme.py         2,045 lines — status/validate/promote/undo + the numeric gate
tools/                  32 files, ~29,200 lines of Python — kredme.py is no longer the whole story:
                        validate_cards.py (1,909), fix_cards.py (1,654), checks/c1..c9,
                        fixers/f1..f5, app_mirror/ (the vendored copy of the app's category list)
tools/test_pipeline.py  66 tests
pipeline/               18 files — the weekly refresh and the news watch. See PIPELINE.md.
tests/                  15 files, 774 tests — the pipeline's own suite (`python3 tests/run_all.py`)
seed/manifest.json      2,879 b
seed/cards.json         2,680,453 b — 383 cards / 1,284 reward rules
                        (plus 1,555 exclusion, 884 redemption, 417 milestone, 359 fuel-surcharge rules)
seed/merchants.json     104,333 b — 273 merchants, 25 categories
news/feed.json          130,654 b — 134 items. Oldest is still the 2026-03-29 "Welcome to KredMe"
                        placeholder; newest is 2026-08-22. The feed is now written by the news watch.
delta/                  README only. Zero delta files ever produced; manifest delta_file is null.
```

**89 tracked files on `main`, 94 on `dev`** (`git ls-tree -r --name-only origin/main | wc -l`). **No `.pyc`
is tracked on either branch any more.** The seven files `dev` carries that `main` does not:
`CARD_TABLES.md`, `a.md`, `b.md`, `ten_facts.json`, `seed/card_details.json`,
`seed/SCHEMA_card_details.md`, `seed/SCHEMA_reward_rules.md`.

## Don't touch

- `.published/`, `staging/` — gitignored, tool-managed. Never commit or serve.
- `tools/__pycache__/*.pyc` — no longer tracked on either branch (0 hits from
  `git ls-tree -r --name-only origin/main | grep '\.pyc$'`). Keep it that way; don't re-add.
- `a.md`, `b.md` (dev only) — PR-description drafts committed by mistake. Not documentation.
- `feat/safe-publish-pipeline` — dead branch. Merging it deletes CI and re-adds the `.pyc` files.
- `seed/cards.json` — 2.68 MB machine-produced blob. Never hand-edit without regenerating manifest checksums.
- `test/` — fixtures with deliberate versions like `0.0.1-test`. Don't "fix" them.
- `delta/` — dormant. `manifest.delta_file` must stay `null` unless a delta file is actually generated.

## Related repos

- `../KredMe-main` — the app that reads this. Its `CLAUDE.md` has the reward-unit trap.
- `../kredme-card-data` — 394-card scraper, **not connected to this repo**.
