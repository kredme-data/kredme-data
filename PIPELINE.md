# The weekly pipeline — operator guide

Two automated loops, both landing in `dev`, neither reaching a user without a person
merging a PR.

| Loop | Cadence | What it does |
|---|---|---|
| **Card-data refresh** | Mondays 03:00 UTC | Re-reads every active card's issuer document, extracts what changed, verifies it adversarially, opens a PR with a field-level patch |
| **News watch** | Daily 02:30 UTC | Polls the pages where issuers publish revisions; when one moves, drafts `news/feed.json` items |

---

## Why it is built this way

Four constraints shaped every decision. They are all measured, not assumed.

**1. A synchronous sweep cannot finish.** 380 cards × (fetch + one model call) was
measured at ~11.6 hours. GitHub Actions kills a job at 6. So the pipeline uses the
**Message Batches API**: `refresh` submits and exits in minutes, and a separate short
job collects whenever the batch ends. No job goes near the limit, and a runner that
dies loses nothing because the batch keeps processing on Anthropic's side.

**2. Most cards do not change in a given week.** Every source document's normalised
text is hashed and the hash is committed. Next week we re-fetch, re-hash, and only
cards whose bytes actually moved reach the model. This is the difference between a
₹10,000 week and a ₹300 one. `normalise_text` collapses whitespace before hashing so
a page that merely reflows does not read as changed.

**3. One extraction pass is not enough, and its failure mode is the expensive one.**
On a known-answer control, a single pass got **100% of its numbers right and 78% of
its "I could not find this" claims wrong** — it declared unfindable a clause that
named the card explicitly. A one-pass pipeline therefore ships correct rates
*alongside a false all-clear*. Pass 2 is a separate batch that sees pass 1's output
and must find each quote verbatim in the source or the observation dies; its second
job is completeness — listing what pass 1 missed.

**4. Unattended publishing to users is not safe yet.** Of 18 card changes that a
first pass called "confirmed at the issuer", an adversarial second pass **refuted 6** —
a wrong effective date, an overstated card list, a source document that had been
superseded. So the pipeline automates detection, extraction, verification and patch
generation, and stops at a PR. That is an engineering judgement based on a measured
error rate, not caution for its own sake. Revisit it when the refute rate is near zero.

---

## Running it

```bash
# Everything, without spending anything or submitting anything
python3 pipeline/cli.py refresh --dry-run

# One card end to end
python3 pipeline/cli.py refresh --card-id hdfc_bank_regalia_gold

# Advance whatever is in flight (idempotent — safe to run any time)
python3 pipeline/cli.py advance

# Check the notice pages without calling the model
python3 pipeline/cli.py news-watch --dry-run

# This week's numbers
python3 pipeline/cli.py metrics

# Which issuer page each card is read from (offline, no model, no spend)
python3 pipeline/cli.py discover --issuer hdfc
python3 pipeline/cli.py discover --write        # merge verified matches
```

### `discover` — what it does and why it matters more than it looks

The seed schema has **no URL field**. Without overrides, every card of an issuer
resolves to the same landing page: 373 cards to 35 URLs, 54 SBI cards sharing one.
Asking the model for one card's mechanics out of a page listing forty is the
difference between a refresh that works and one that merely runs.

`discover` harvests each issuer's listing page **and its sitemap** — SBI's listing
page yields no card links at all while its sitemap yields 209 — and matches links to
cards on **exact** name equality. Two keys, both exact: a token set (order-insensitive,
so "Diners Club Black" matches `black-diners-club`) and a concatenation
(boundary-insensitive, so "MoneyBack" matches `money-back` and "Doctor's Regalia"
matches `doctors-regalia`).

It refuses far more than it accepts, on purpose. **A wrong per-card URL is much worse
than no per-card URL**: point Regalia at Regalia Gold's page and the extractor reads
Regalia Gold's rates, the adversarial pass finds every quote exactly where it should
be and *confirms* them, and a real cardholder sees a confidently wrong number. A
missing override only costs precision. So anything ambiguous keeps the landing page,
and every match is fetched and must (a) name the card and (b) say "credit card" —
that second check is what stopped four cards resolving to a debit card, a savings
account and two banking programmes that reuse the same product names.

Hand-written entries in `sources_overrides.json` are never overwritten by a crawl.

### ⚠ The workflow files live on `main`, the code lives on `dev`

GitHub honours `schedule` and `workflow_dispatch` **only from the default branch**,
which here is `main`. So the three workflow YAMLs are committed to `main`, while
everything they run comes from `dev` — each one's first step is
`actions/checkout@v4` with `ref: dev`. **main carries the trigger; dev carries the
work.** Both loops open their PRs with `base: dev`, so no bot ever writes to `main`
and publishing to users stays a deliberate act through `tools/kredme.py`.

**Trap:** there are two copies of each workflow in this repo. Editing `dev`'s copy
changes nothing about when the job runs, because `main`'s copy is the one GitHub
reads. Change a cron, a permission, an input or a concurrency group in **both**
places or the change silently does not take effect. If a scheduled run seems to
have vanished, diff `main:.github/workflows/` against `dev:.github/workflows/`
before looking anywhere else.

From CI: **Actions → Weekly card-data refresh → Run workflow**. Use `dry_run` first.
`force` re-extracts all 380 cards and costs real money — it exists for a schema change,
not for routine use.

## Tests

```bash
python3 tests/run_all.py
```

Stdlib `unittest`, no pip installs, no network. `run_all.py` additionally asserts that
every pipeline module imports on a bare Python — the Anthropic SDK is imported lazily
inside the functions that call it, and CI fails if someone moves that to module scope.

---

## What each stage writes

| Stage | Writes | Never writes |
|---|---|---|
| `refresh` | `pipeline/state/sources.json`, `pipeline/state/batch.json` | anything in `seed/` or `news/` |
| `advance` (2) | `pipeline/state/batch.json` | anything in `seed/` or `news/` |
| `advance` (3) | `seed/cards.json`, `seed/manifest.json`, a PR to `dev` | `main` |
| `news-watch` | `news/feed.json`, a PR to `dev` | `main` |

Promoting `dev → main` stays a separate, deliberate act: `tools/kredme.py promote`.

---

## The guards, and what each one is for

Every one of these exists because the corresponding mistake has already been made here.

| Guard | Prevents |
|---|---|
| **Issuer-domain allowlist** (`config.is_issuer_domain`) | An aggregator's number re-entering the catalogue. Matches on dot-delimited suffix segments, so `hdfcbank.com.evil.tld` is rejected. |
| **Weasel-phrase rejection** (`contains_weasel`) | Sourcing a rate from "earn **up to** 10%". An early pass raised a rule from 10% to 33% off exactly such a sentence. |
| **Upward revisions always blocked** | The same failure, generalised: corrections derived from issuer prose have only ever been valid *downward*. An increase always needs a person. |
| **40% hard ceiling** | An absolute points count landing in a multiplier field — that is what produced the 24% fuel rate. 40 and not 30 because HDFC SmartBuy 10X genuinely reaches ~33%, and a gate that blocks a real product gets switched off. |
| **`rule_name` is never rewritten** | Orphaning every user's cap progress. The app keys spend history on the raw string (`${cardId}\|${ruleName}`), so renaming a rule silently resets caps mid-cycle. |
| **Field-level patches only** | The previous scraper's fatal flaw: `shutil.copyfile()` over the card on any drift, discarding curated benefits, sources and changelog. |
| **Points × unit never collapsed to a percentage** | Destroying the block size — a later PR had to restore it on 105 rules. |
| **Verdict required before apply** | An unverified observation shipping. No verdict is treated as refuted. |
| **Manifest regenerated from bytes** | "Sync failed" in the app, which is what a stale checksum looks like to a user. |

---

## The news version trap

`news_feed_service.dart:126-127` parses **only the leading integer** of the feed
version and refetches only on a **strict increase**:

```dart
final serverVersion = rawVer is int ? rawVer : int.tryParse(rawVer.toString().split('.').first) ?? 0;
if (serverVersion <= _currentVersion) return;
```

Consequences the pipeline encodes in `newsgen.next_version`:

- `2.0.0 → 2.1.0` is **invisible**, permanently, to anyone who already fetched major 2.
- `"v3.0.0"` parses to **0** and the feed then never loads for **anyone**. `tools/kredme.py`
  tolerates the `v` prefix; the app cannot read it. `next_version` raises rather than guess.
- Targeting works **only** through `affected_cards` with exact `card.id` strings.
  `affected_issuers` is parsed by the app and read by nothing.

## What publishing news actually achieves today

Be clear-eyed about this: merging a news PR and promoting it puts the item in the app's
feed, where it is reachable via **Settings → Live Data → News & Alerts** and nowhere
else. There is no push, no badge, no home banner. The app subscribes to zero FCM topics
and discards its FCM token, so no server-side script can address a user.

For a change that matters — a devaluation with a date — the alert that actually lands is
a **Firebase Console → Messaging** campaign to the app audience, which needs no app
release. Add custom data key `cardId` and the tap deep-links to `/card/<id>`; that path
already works. Wiring a real in-app surface (badge, banner, or a topic subscription) is
an app change and is blocked behind the targetSdk 36 bump.

---

## Cost

**Re-measured 2026-08-18 against the first real bill.** The figures below replace an
estimate taken from a 40-card dry run, which understated a full sweep by roughly 78%.

Billed for the 17-Aug cycle: **$94.55** — extraction of 371 cards ~$59.74, verification of
223 ~$34.81. That works out at **~$0.16 per card to extract and ~$0.26 fully processed**,
against the ~$0.10 the dry run suggested.

| Scenario | Cards reaching the model | Likely |
|---|---:|---:|
| Typical week (only changed sources) | 20 | **~$3.20** |
| | 50 | ~$8.05 |
| Heavy week (an issuer revises a portfolio) | 100–150 | ~$16–24 |
| `--force` full sweep, extraction only | 371 | **~$59.74** |
| Full sweep, both passes | 371 | **~$94.55** (measured) |

`refresh` prints both a likely and a ceiling figure before submitting, and `--dry-run`
prints them and submits nothing. **Quote the ceiling, not the likely figure**, to anyone
deciding whether to approve a sweep: `est_usd` assumes a typical response size and was 38%
low the first time it met reality, whereas `est_usd_ceiling` bills every request's full
`max_tokens` and is the number the bill cannot exceed.

**A batch estimated above `config.MAX_BATCH_USD` ($25) is refused, not submitted.** It
raises rather than prompting, because this runs on a cron with nobody to answer. Override
deliberately with `--max-usd N`; `--max-usd 0` disables it.

Two things hold the cost down, and one that does not:

- The Batch API halves both input and output. Real.
- The content-hash gate means most cards never reach the model. Real, **but only once a
  card is marked `done`** — and `mark_done` runs solely in stage 3. If stage 3 never
  completes, every card re-extracts at full price the following Monday.
- The shared system prefix is cached at ~10% of input price. **Real but negligible here** —
  the prefix is ~1,327 tokens, so caching the whole batch saves about $1.10. It is not
  what makes this affordable, and a missing cache is never the explanation for a surprise.

Actions minutes are free — this repo is public.

---

## Known gaps (measured, not guessed)

**1. Roughly half the catalogue still shares a page with its siblings.** `discover` gave
181 cards their own document; the rest fall back to `ISSUER_LANDING`, which is correct but
shared. BOBCARD (19 cards) cannot be fetched at all — its server omits the GlobalSign
intermediate certificate, so the chain will not verify and the fix is theirs, not ours.
YES Bank (17) renders its card list in JavaScript and publishes no sitemap, so there is
nothing to harvest. Kotak (24) also has no sitemap. Those three account for most of the
remainder; the rest are cards the issuer no longer lists.

**2. Five cards have no issuer URL at all** — City Union, CSB, OneCard, SBM, Unity. One
card each; low value, and some of these issuers publish no rates anywhere.

**3. A shared landing page is a weak source, not a wrong one.** A card on its issuer's
listing page gets that page plus up to four linked PDFs, which is often enough — issuers
put the numbers in the MITC. It is still worth re-running `discover` after any issuer
redesign. Run `python3 pipeline/cli.py refresh --dry-run` and read the `fetch_failed` list
before trusting a coverage number: 373 cards *resolve to a URL*, which is not the same as
373 cards *whose document we can read*.

**4. The typical-output token figures are estimates.** `config.TYPICAL_OUTPUT_TOKENS` is
the assumed size of a real response. Re-measure it from the first live batch's `usage` and
update; the ceiling figure is the only guaranteed bound until then.

**5. One reviewed HIGH is still open.** Two verified observations targeting the same field
on the same card both pass the gate and are both counted in the "applied" total and listed
under *Applied* in the PR body — but `apply_proposals` writes them in sequence, so only the
last value survives. The write is safe (one field, one final value, provenance stamped) and
the diff shows the truth; what lies is the summary a reviewer reads. Fix is to collapse
per-`(card_id, path)` collisions before rendering, marking all but one
`conflicting_observations`. Until then, read the diff and not the count.

**6. Nothing has run against the live API yet.** Every module is unit-tested with an
injected fake client and the full request shape is asserted, but no real batch has been
submitted. The first live run should be `--limit 5` on cards with confirmed overrides.

---

## When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| `ANTHROPIC_API_KEY is not set` | Secret missing on **this** repo | Settings → Secrets → Actions. A key on `kredme-card-data` has no effect here. |
| Every card reports `fetch_failed` | `poppler-utils` missing, so no PDF yields text | `apt-get install poppler-utils` — CI installs it already |
| `advance` says "scratch documents are gone" | A later stage ran on a fresh runner without stage 1's scratch | Re-run `refresh`; verification must see the exact bytes the extractor read |
| Coverage report shows many `not_issuer_domain` | A card carries an aggregator URL | Add the real issuer URL to `pipeline/sources_overrides.json` |
| PR has 0 auto-applied and many blocked | Working as designed | Read the quotes and merge what is right |
