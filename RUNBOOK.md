# kredme-data — Operator Runbook

**Owner:** Niladri (KredMe team). **Not** the app developer's responsibility.
This repo is the **OTA backend** the app reads over the network via GitHub Pages
(`https://kredme-data.github.io/kredme-data`). It has two independent jobs:

1. **News feed** — `news/feed.json` — the in-app "news / alerts" content.
2. **Seed data** — `seed/{manifest,cards,merchants}.json` — the card + merchant catalog.

> **Golden rule:** GitHub Pages serves from the **`main`** branch. If it isn't on
> `main` and pushed, it is **not live** — no matter what branch it's committed to.

---

## Current state (as of 2026-07-18)

| Thing | Live (main) | Local/other branch | Problem |
|---|---|---|---|
| Seed version | **5.1.0** | 5.2.0 on `v5.2/canonical-rate-fields` | 5.2.0 never merged to `main` → not deployed |
| News feed | 1 item, "Welcome to KredMe", 2026-03-29 | — | Never updated; only placeholder content |
| Card rates | CardInsider snapshot (~May 2026) | — | Stale / pre-devaluation; not issuer-vetted |

---

## WORKSTREAM 0 — The safe pipeline (do everything through this)

> **This replaces hand-editing `seed/` and `news/` directly.** Those are now
> "live" and should only ever be written by the `publish` command.

### The model

```
staging/          <- you edit HERE. Never served to users.
  seed/{manifest,cards,merchants}.json
  news/feed.json

seed/ , news/     <- LIVE. Only ever written by `publish`.

.published/       <- automatic snapshots taken before every publish.
                     This is what `undo` restores. Git-ignored.
```

Nothing reaches users until **you** run `git push`. The tool never touches the
network.

### The four commands

```bash
cd ~/Downloads/KredMe/kredme-data

python3 tools/kredme.py status              # what's staged vs live
python3 tools/kredme.py validate            # is staging safe to ship?
python3 tools/kredme.py publish --dry-run   # show exactly what would change
python3 tools/kredme.py publish             # staging -> live (still LOCAL)
python3 tools/kredme.py undo                # restore the previous live state
```

### A normal day

```bash
# 1. edit staging/news/feed.json  (or staging/seed/*.json)
# 2. check it
python3 tools/kredme.py validate
# 3. see what would change
python3 tools/kredme.py publish --dry-run
# 4. promote it locally
python3 tools/kredme.py publish
# 5. THIS is the step that goes live:
git checkout main
git add seed news
git commit -m "data: news alerts for <thing>"
git push origin main
```

### What the gate actually catches

`publish` refuses to run if `validate` fails. It checks, among other things:

| Check | Why it matters |
|---|---|
| News uses `expiry_date` / `source_url` / `severity` | the app **silently ignores** `expires_at` / `url` — this is why the live feed looks empty |
| Every `affected_cards` id exists in `cards.json` | a typo'd card id means the alert reaches **nobody** |
| Manifest checksums match the real bytes (live) | a mismatch is exactly what shows as **"Sync failed"** in the app |
| Duplicate card / merchant / news ids | ambiguous lookups silently drop records |
| Merchant `category_id` resolves to a real category | dangling refs break merchant matching |
| Card count sanity | catches a truncated / half-written catalog |

### Two things it does for you automatically

1. **News version MAJOR bump.** The app only refetches news when the leading
   integer increases (`1.0.0 -> 2.0.0`). `publish` does this for you, so the
   gotcha in Workstream A below can no longer be forgotten.
2. **Checksum regeneration.** The manifest is rebuilt from the bytes actually
   written, so a hand-edited checksum can never cause "Sync failed".

### If a bad publish gets out

```bash
python3 tools/kredme.py undo     # restores the previous live data locally
git add seed news && git commit -m "data: roll back" && git push origin main
```

`undo` snapshots the current state first, so the rollback is itself reversible.
Use `python3 tools/kredme.py undo --list` to see every restore point.

### Tests

```bash
python3 tools/test_pipeline.py     # 19 tests, no dependencies
```

### Known limits (deliberate)

- **The app does not read `staging/`.** Pointing a test build at staging needs
  an app-side base-URL switch (a developer task). The safety guarantee here
  comes from *validate + undo*, not from the app reading staging.
- `.published/` snapshots are **local to this machine**. After a push, the
  git history is the shared source of truth (`git revert`).
- The tool never runs git for you. Pushing stays a deliberate human act.

---

## WORKSTREAM A — Publish real news alerts

This makes the in-app "news alerts" actually work. **No app release needed.**
(App surfacing — the home banner — is a separate app-code task for the developer.)

### A0. One-time: understand the schema the app actually parses
The app's `NewsArticle.fromJson` reads these keys. Your current `feed.json` uses
some **wrong names that the app silently ignores** — fix them:

| Use this key | NOT this | Notes |
|---|---|---|
| `expiry_date` | ~~`expires_at`~~ | ISO-8601 or `null` |
| `source_url` | ~~`url`~~ | the "read more" link |
| `severity` | *(missing)* | `"negative"` (devaluation/urgent), `"warning"`, `"positive"`, `"info"` |
| — | ~~`is_pinned`~~ | app has no pinning; harmless but useless |

Full item shape the app understands:
`id`, `title`, `summary` (required); `category`, `severity`, `source`,
`source_url`, `published_at`, `expiry_date`, `affected_cards` (list of card IDs),
`affected_issuers` (list), `tags` (list), `action_text`.
Wrapper accepts either `items` or `articles`.

- `affected_cards: []` (empty) → shows to **everyone**.
- `affected_cards: ["idfc_first_bank_mayura", ...]` → shows **only to users who hold those cards** (this is the "Affects My Cards" filter).
- Card IDs must match `seed/cards.json` exactly, e.g. `idfc_first_bank_mayura`, `idfc_first_bank_wealth`, `idfc_first_bank_select`. Get them with:
  `python3 -c "import json;[print(c['id']) for c in json.load(open('seed/cards.json')) if 'idfc' in c['id']]"`

### A1. Steps to publish
```bash
cd ~/Downloads/KredMe/kredme-data
git checkout main && git pull origin main
# edit news/feed.json  (see template below)
git add news/feed.json
git commit -m "news: <what you added>"
git push origin main
```

### A2. ⚠️ CRITICAL version gotcha
The app only re-fetches news when the **major integer** of `version` increases
(it parses `"2.0.0".split('.')[0]`). **Minor/patch bumps are ignored.**
→ **Bump the whole number every publish:** `1.0.0 → 2.0.0 → 3.0.0 …`
(Ask the developer to compare the full string / `updated_at` so you don't have to — logged as an app-side item.)

### A3. Verify live + in app
```bash
curl -s https://kredme-data.github.io/kredme-data/news/feed.json | python3 -m json.tool | head -30
```
The app caches and rate-limits news to one fetch / 6 h, so to see it immediately,
clear app data / reinstall on the test device.

### A4. Template — copy, edit, ship
```json
{
  "version": "2.0.0",
  "updated_at": "2026-07-18T00:00:00Z",
  "items": [
    {
      "id": "news_2026_07_idfc_deval",
      "title": "IDFC FIRST cuts reward rates",
      "summary": "IDFC FIRST reduced base earn to 1 RP/₹150 and lowered international multipliers. Check your effective rate before large spends.",
      "category": "devaluation",
      "severity": "negative",
      "source": "IDFC FIRST Bank",
      "source_url": "https://www.idfcfirstbank.com/credit-card",
      "published_at": "2026-07-18T00:00:00Z",
      "expiry_date": null,
      "affected_cards": ["idfc_first_bank_mayura", "idfc_first_bank_wealth", "idfc_first_bank_select"],
      "affected_issuers": ["idfc"],
      "tags": ["devaluation"],
      "action_text": "Review your IDFC cards"
    }
  ]
}
```

---

## WORKSTREAM B — Deploy / refresh seed card data

### B1. Deploy the already-built v5.2.0 (currently stranded)
`v5.2.0` (canonical rate fields, 376 cards) is committed to
`v5.2/canonical-rate-fields` but **never merged to `main`**, which is why the
live endpoint still serves `5.1.0`. Its checksums are already valid.
```bash
cd ~/Downloads/KredMe/kredme-data
git checkout main
git merge v5.2/canonical-rate-fields    # fast-forward; checksums consistent
git push origin main
# verify:
curl -s https://kredme-data.github.io/kredme-data/seed/manifest.json \
 | python3 -c "import sys,json;m=json.load(sys.stdin);print(m['version']);[print(f['name'],f['checksum']) for f in m['files']]"
```
> Note: this deploys the canonical fields + 376 cards, but the **rates are still
> the stale CardInsider snapshot**. Accurate/post-devaluation rates are a
> separate content pass (B2).

### B2. Whenever you edit cards.json / merchants.json (regenerate manifest)
The app **rejects** a sync whose file doesn't match the manifest checksum, so
always regenerate after editing:
```bash
cd ~/Downloads/KredMe/kredme-data/seed
python3 - <<'PY'
import json,hashlib,datetime
m=json.load(open('manifest.json'))
for f in m['files']:
    b=open(f['name'],'rb').read()
    f['checksum']=hashlib.sha256(b).hexdigest(); f['size_bytes']=len(b)
v=m['version'].split('.'); v[-1]=str(int(v[-1])+1); m['version']='.'.join(v)   # bump patch
m['updated_at']=datetime.datetime.utcnow().isoformat()+'Z'
json.dump(m,open('manifest.json','w'),indent=2)
print('new version',m['version'])
PY
cd .. && git add -A && git commit -m "seed: refresh rates" && git push origin main
```
(Seed sync uses **string equality** on `version`, so any change triggers a sync —
no major-bump gotcha here, unlike news.)

### B3. Open question — the scraper bridge
`kredme-card-data` (the 394-card weekly scraper, the intended "source of truth")
is **not connected** to this repo — its Firestore output is never read by the app,
and there's no build step that turns it into `seed/cards.json`. Decide who owns
building that bridge; until then, seed edits here are manual.

---

## Quick reference — endpoints
| Path | Consumed by |
|---|---|
| `/seed/manifest.json` | app on launch + every 12 h (version + checksums) |
| `/seed/cards.json` | full card + reward-rule payload |
| `/seed/merchants.json` | merchant catalog + MCC + statement aliases |
| `/news/feed.json` | in-app news feed |
| `/delta/delta_<from>_to_<to>.json` | optional incremental patch (keep `delta_file: null` for full sync) |
