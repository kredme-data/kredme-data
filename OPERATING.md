# Running this without being an engineer

Everything here runs by itself. This is what it does, what it costs, and the four
things worth your attention. It assumes no command line.

For the engineering detail — architecture, guards, what each stage writes —
read [PIPELINE.md](PIPELINE.md) instead. This file is the shorter one.

---

## Where it runs, and why GitHub being free does not make it free

The pipelines run on **GitHub Actions**: GitHub lends you a Linux machine, it wakes on a
schedule, runs the code, and disappears. Nothing runs on your laptop and there is no
server to pay for. Because `kredme-data` is a **public** repo, those machines cost **₹0**.

The bill comes from somewhere else.

> GitHub gives you the desk. The **Claude API bill is the phone calls made from it.**

The pipeline's entire job is to read ~370 issuer documents and ask Claude what each card's
fees and reward rates actually say. Those requests leave GitHub's machine and go to
Anthropic, who charge per token. GitHub hosting the machine has no bearing on that.

| | Who bills it | 17-Aug-2026 |
|---|---|---|
| The machines that run the pipeline | GitHub | **₹0** — public repo |
| Reading 371 documents (extraction) | Anthropic | ~$59.74 |
| Checking that reading (verification) | Anthropic | ~$34.81 |
| | | **$94.55** |

Your other two repos, `KredMe` and `kredme-card-data`, are **private** — those do consume
paid GitHub minutes. Only this one is free.

---

## The five pipelines

Dashboard: **https://github.com/kredme-data/kredme-data/actions**

| Name | What it does | When | Spends? |
|---|---|---|---|
| **Weekly card-data refresh** | Reads every card's issuer page, starts the AI job | Mon 08:30 IST | **Yes** |
| **Pipeline advance** | Collects results, opens a PR with proposed changes | every 2h | **Yes** (2nd pass) |
| **News watch** | Checks issuer notice pages, drafts alerts | daily 08:00 IST | Yes, pennies |
| **News push** | Sends the phone notification | only when you publish | No |
| **Validate data** | Checks the data is not broken | on every change | No |

**Nothing here ever changes what users see on its own.** Every pipeline ends by opening a
pull request for a human. The only thing that reaches real users is a person running
`promote` and pushing.

---

## The four things worth your attention

### 1. A failure email arrives

Open the run from **https://github.com/kredme-data/kredme-data/actions** and read the
last few red lines. Two cautions learned the hard way:

- **The commit shown in the email is the workflow file's, not the code's.** These jobs run
  code from the `dev` branch. Two runs can show an identical commit and behave completely
  differently. Do not conclude "nothing changed" from that line.
- **A green tick is not proof work happened.** A job whose steps were all *skipped* still
  reports success. Check that the step you cared about says `success`, not `skipped`.

### 2. A pull request appears

`bot/card-refresh-*` or `bot/news-*`. This is the pipeline's output and the point of the
whole thing. **Read it before merging.** An adversarial second pass refuted 6 of 18
changes a first pass had called issuer-confirmed, so the machine is wrong often enough
to matter. Merging to `dev` shows nobody; publishing is a separate deliberate step.

### 3. Monday's number

After each Monday run, open the refresh and find this line:

```
OK   fetched 373: 24 changed, 347 unchanged, 2 failed
```

**`changed` is your bill.** ~$0.16 per changed card to extract, ~$0.26 fully processed.
20 changed is about $3. If `changed` sits near 371 week after week, the saving that makes
this affordable is not working — say so, because that is a ~$60 week every week.

### 4. The spend cap

A batch estimated over **$25** is refused outright rather than submitted. That is set in
`pipeline/config.py` as `MAX_BATCH_USD`. It sits above a normal week (~$3–8) and below a
full 371-card sweep (~$60), so a sweep can only happen because somebody decided it should.

---

## Making a change

Two separate things, and the difference matters:

- **`.github/workflows/*.yml`** — 5 small files saying *when* each pipeline runs and what
  to install. Edit these to change a schedule or switch one off.
- **`pipeline/*.py`** — 12 files, the actual behaviour. `fetch.py` downloads pages,
  `batch.py` talks to Claude, `diff.py` decides what changed.

You can edit either in the browser: open the file on GitHub, click the pencil, and it
walks you through opening a PR. No terminal needed.

> ⚠️ **Schedules only take effect from the `main` branch.** GitHub reads the timetable from
> the default branch only. A schedule edited on `dev` does nothing at all, silently. This
> has caught us repeatedly — a workflow change usually needs landing on **both** branches.

`main` is live to real users. `dev` is the safe lane, read by test builds.

---

## Things that are red on purpose

- **Weekly diff scan** (in `kredme-card-data`) — repaired, then taken off its schedule. It
  fed a Firestore collection nothing reads. Its last run stays red forever; that is fine.
- **Deploy to Firestore** (same repo) — has never worked, feeds the same dead end. Left
  alone deliberately.

---

## When something looks wrong

Facts worth having before anyone re-diagnoses from scratch:

- **Collecting an already-finished AI job is free.** Results stay retrievable for 29 days.
  If a run collected results and then died, `advance --recollect <batch_id>` reuses them —
  it does **not** pay again. Re-running the weekly refresh instead pays twice.
- **The cost figures the pipeline prints are estimates**, and were 38% low the first time
  they met a real bill. The `ceiling` figure is the one that cannot be exceeded.
- The real bill is only ever in the Anthropic console: **platform.claude.com**.
