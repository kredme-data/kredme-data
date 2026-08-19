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

# How much of the catalogue cites a document, and what finishing costs
python3 pipeline/cli.py evidence

# Read a slice of the cards that cite nothing (see "The evidence backlog" below)
python3 pipeline/cli.py refresh --unsourced-only --limit 40 --dry-run

# Which issuer page each card is read from (offline, no model, no spend)
python3 pipeline/cli.py discover --issuer hdfc
python3 pipeline/cli.py discover --write        # merge verified matches
```

### The evidence backlog — `evidence` and `refresh --unsourced-only`

**The weekly refresh is a change detector, and that has a blind spot it can never
close on its own.** Constraint 2 above is what makes a weekly run cost rupees
rather than thousands: a card whose source bytes did not move is skipped. But a
card whose rates have *never been verified*, whose issuer page simply did not
change this week, is skipped for the same reason — and next week, and the week
after.

Measured 2026-08-19 against `seed/cards.json` and the committed pipeline state:

| | |
|---|---:|
| active cards | 370 |
| a **reward rule** cites a document | **9** |
| no reward rule cites anything | **361** |
| ... of those, an annual-fee/forex stamp only (`_provenance`) | 25 |
| ... of those, still to read | **301** |
| ... of those, already read end-to-end at their current bytes | 56 |
| ... of those, no issuer URL at all | 4 |
| distinct issuer pages behind the 301 | 156 |
| reward rules citing a document | 27 of 1,279 (2.1%) |
| reward rules citing **the issuer's own** document | 26 of 1,279 (2.0%) |

**The unit is the reward rule.** An earlier version of this predicate exempted a
card whose only evidence was the card-level `_provenance` block — but that block
records CARD FIELDS (annual fee, forex markup, point value), not reward rules. It
exempted 25 active cards on a stamp about an annual fee while they carried 91
uncited reward rules between them, and the validator graded 22 of those 25 **F**
on the exact metric this pipeline exists to move. Those cards are back in the
queue and counted on their own line.

```bash
python3 pipeline/cli.py evidence                                  # the backlog + its price
python3 pipeline/cli.py refresh --unsourced-only --limit 40 --dry-run
python3 pipeline/cli.py refresh --unsourced-only --limit 40       # ~$6.44 to extract,
                                                                  # ~$12.68 through both
                                                                  # passes; 8 weeks to clear
```

Six things about it worth knowing:

1. **The backlog terminates.** A card the pipeline has read end to end at its
   current bytes leaves the queue even though it still cites nothing — the
   document was read and did not yield a citable number, and reading the same
   bytes again returns the same nothing. It re-enters the moment its page moves,
   through the ordinary hash gate, with no flag and no special case. Without that
   subtraction, `--unsourced-only` on a cron is an open-ended recurring bill: 31
   of the 67 cards this pipeline has marked `done` still cite nothing, and every
   one of them would have been re-fetched and re-billed every cycle forever.
2. **The run says what the money buys, before it is spent.** Of a selection, it
   prints how many the content-hash gate is actually blocking (what only this
   flag can reach) versus how many this week's ordinary refresh would have
   fetched anyway. Today that split is **0 blocked / all already due**: 301 cards
   sit at status `fetched` from the 17-Aug cycle, so the plain weekly refresh
   already selects them — and forecasts $48.47, over the $25 per-batch ceiling,
   so it refuses. What this flag adds for those cards is not gate-bypass, it is
   the `--limit`: an affordable slice instead of one over-budget sweep. Running
   it in the same week as the cron pays for the same cards twice.
3. **`--limit N` walks the backlog and the counter counts down.** Cards are
   ordered by how often they have been selected, then how long ago, then card id;
   within a tier, a card with a document of its OWN goes before a card that
   shares a page; within each of those, round-robin across issuers. `deferred`
   counts the live backlog, so it falls week over week — the earlier version
   printed the same constant in week 1 and week 13.
4. **Half the backlog is on a shared page, and that is free to fix.** 161 of the
   301 share a document with another card and 147 resolve to a plain issuer
   landing page — one URL serves 27 RBL cards, 21 ICICI, 20 SBI. Asking the model
   for one card's mechanics out of a listing of forty is the weakest thing this
   pipeline does. `evidence` prints the count and the worst offenders, and the
   ordering reads own-page cards first. **Run `discover --write` before paying:**
   it is offline, free, and it is what turns a landing page into a card page.
5. **Four cards can never be fixed by this pipeline** and are printed by name
   rather than dropped: `city_union_bank_cub_salaryse_level_up`,
   `csb_bank_edge_plus`, `sbm_bank_sbm_zet` (no issuer landing page on the
   allowlist) and `fpl_technologies_pvt._ltd._onecard_metal` (its only URL is
   `www.getonecard.app`, which is not an issuer domain).
6. **The whole backlog at once is refused, not submitted**, *before the first
   fetch* — same ceiling, same `--max-usd` override, just checked early enough to
   save 156 pointless requests to banks.

**The reachable ceiling is not 100%.** 29 reward rules sit on the 13 switched-off
cards and are inside the validator's 1,279 denominator while being permanently
outside anything this pipeline fetches; another 28 are on the four unreachable
active cards. Nobody should read the 2.0% headline as something this pipeline can
take to 100%.

**What counts as evidence** is one function —
`pipeline/provenance.py:card_has_issuer_evidence` — and the validator's L8 layer
imports it rather than keeping its own copy. So does the issuer→domain table, the
aggregator lists and the host matching, moved out of `c8_provenance.py` on
2026-08-19. A rule cites a document when `source_url` or `_sources` names one; a
`source_quote` with no document does **not** count (nothing to re-read at the next
devaluation), and neither does the literal placeholder `"bank"`, which seven rules
in this file carry.

`evidence` prints **both** rule-level counts, labelled: *citing a document we can
open* (27) and *citing the ISSUER'S own document* (26, the validator's headline).
They used to be printed as one number by two commands, which gave the same bytes
two answers on the same day. The single divergent rule is on
`au_small_finance_bank_ixigo_au`, whose four rules cite one `au.bank.in` PDF and
one `ixigo.com` page — **not**, as an earlier note in this file claimed, "two real
`au.bank.in` PDFs and nothing else". A partner domain is a real document and is
not the issuer's, and both facts are now visible.

#### ⚠ Spending: what the flags do

`--max-usd 0` removes the spend ceiling. It now requires `--i-accept-usd N`
alongside it, where N is at least the both-passes forecast the run just printed —
typing the amount is the acknowledgement, and it cannot be pasted from a runbook
written when the backlog was half the size. A negative `--max-usd` is rejected
rather than silently read as "no ceiling".

`MAX_BATCH_USD` is a limit **per batch**, not per cycle. Verification is a second
batch checked against the same ceiling by `advance`, so a run that clears $25 can
still bill close to $50 by the time the cycle ends. Every message that prints the
ceiling says *per batch*, and `evidence` prints both "most cards one BATCH may
hold" (155) and "most cards one CYCLE may hold" (78).

`--unsourced-only` and `--force` are refused together. They ask for two different
runs, and the old warning claimed `--force` would widen a selection that had
already been narrowed — an operator was warned about a $94 sweep that was not
happening.

**Nothing submits while a batch is in flight.** Two extraction batches ending in
the same collection window used to mean the second overwrote the first's results
while the first's verification batch was already billed — ~$50 paid for and
discarded, silently. `refresh` now refuses (override with
`--allow-concurrent-batch`), and stage 2 merges into `extractions.json` instead of
overwriting it.

#### ⚠ A locally-submitted batch is not collected until you push

`pipeline-advance.yml` checks out branch `dev` and gates on the **committed**
`pipeline/state/batch.json`. A local run writes that file into your working tree
and nowhere else. If you do not push it, the collector prints "No batch state
file." forever, the extraction is billed and never collected, and Anthropic drops
the results after 29 days — and the rotation counters go with it, so the next run
re-selects and re-pays for the same cards.

`refresh` now prints this at submit time, loudly. Do it:

```bash
git add pipeline/state
git commit -m 'Track extraction batch <id>'
git push origin HEAD:dev
```

#### ⚠ `metrics` used to report a fourth, looser number

On 2026-08-19 this repo gave three answers to one question about the same bytes:
`metrics` said **61 of 1,279 (4.8%)**, `evidence` said 27 (2.1%), the validator
said 26 (2.0%). The founder-facing command held the most generous definition,
which is the worst place for it. `report._has_source` now delegates to the shared
predicate, so `metrics` reads **27 (2.1%)**.

The metric therefore steps 61 → 27 once. That is a definition change, not a loss,
so `report.METRIC_DEFINITION_VERSION` is stamped into every metrics row and
`diff_metrics` refuses to subtract rows measured different ways — the report says
"last week's figure used a looser rule and is not comparable" instead of
"34 reward rules LOST the citation they had". **Rows in `metrics.jsonl` written
before this change are on the old definition and are not comparable across that
boundary. This changes the weekly PR body even on a run where no flag was used**,
which is why it is a commit of its own in the history.

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

`--unsourced-only` is deliberately **not** wired to a workflow input yet. A
`workflow_dispatch` input is read from `main`'s copy of the YAML, so exposing it means
the identical edit in both places (see the trap above) — and until the first catch-up
run has been read by a person, this should be typed by an operator who is watching the
bill, not offered as a button.

**Run it locally against a checkout of `dev`, and then push `pipeline/state`.**
`weekly-refresh.yml` ends with a "Commit pipeline state" step for a reason: a batch
whose handle is not on `dev` is collected by nobody. A local run has no such step, so
`refresh` prints the three git commands at submit time and you must run them. See
"A locally-submitted batch is not collected until you push" above.

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
| `--unsourced-only --limit 40`, both passes | 40 | **~$12.68** (~₹1,100) |
| Clearing the whole evidence backlog, both passes | 301 | **~$95.45** (~₹8,276), 8 weekly runs |
| `--force` full sweep, extraction only | 371 | **~$59.74** |
| Full sweep, both passes, every card verified | 371 | **~$117.65** |
| Full sweep, both passes, at 17-Aug's verify yield | 371 | **~$94.55** (measured) |

**Each pass is divided by its own request count.** The 17-Aug bill is one bill
with two batches of different sizes: extract 371 requests / $59.74, verify **223**
requests / $34.81. `build_verify_request` is one request per card, so a card that
reaches verification costs $34.81/223 = **$0.156**, not $34.81/371 = $0.094.
Dividing both by 371 understated every both-passes forecast by 24.4%, in the
dangerous direction, on the exact numbers an operator reads to decide whether to
spend. Only 223 of 371 extracted cards produced something worth verifying (60.1%)
— whether that recurs is a property of the documents, so the forecast quotes the
**ceiling** (every card verified) and prints the 17-Aug yield beside it as the
optimistic end.

`refresh` prints both a likely and a ceiling figure before submitting, and `--dry-run`
prints them and submits nothing. **Quote the ceiling, not the likely figure**, to anyone
deciding whether to approve a sweep: `est_usd` assumes a typical response size and was 38%
low the first time it met reality, whereas `est_usd_ceiling` bills every request's full
`max_tokens` and is the number the bill cannot exceed.

**A batch estimated above `config.MAX_BATCH_USD` ($25) is refused, not submitted.** It
raises rather than prompting, because this runs on a cron with nobody to answer. Override
deliberately with `--max-usd N`; `--max-usd 0` disables it **and then requires
`--i-accept-usd N`** naming an amount that covers the run. The ceiling is **per
batch** — verification is a second batch against the same ceiling — so a
"within budget" refresh can bill close to twice it by the time `advance` finishes.

Two figures, and only one of them has authority. `config.USD_PER_CARD_*` is the
measured 17-Aug bill, each pass divided by its own request count; `evidence` and
the `--unsourced-only` pre-flight use it to price a plan **before** any page has
been fetched, because the alternative is fetching 156 issuer pages to find out
what a run costs. `batch.estimate_cost()` is computed from the real document text
and is what `submit()` checks against the ceiling. Never quote the forecast as the
bill.

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

**2. Four cards have no issuer URL at all** — City Union, CSB, SBM (no landing page on
the allowlist) and OneCard (its only URL is `www.getonecard.app`, not an issuer domain).
One card each; low value, and some of these issuers publish no rates anywhere.
`refresh --unsourced-only` names them every run rather than dropping them, because four
cards vanishing quietly makes the backlog look four cards smaller than it is, forever.

**2b. The catalogue is 2.0% issuer-sourced and only this pipeline can move that.** 361
of 370 active cards have no reward rule citing a document. Every repair PR so far has
improved internal consistency, which by construction cannot change this number. See "The
evidence backlog" above; `python3 pipeline/cli.py evidence` is the one command that
reports it — and it caps out well below 100%: 29 rules are on switched-off cards and 28
on the four cards with no reachable URL.

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
