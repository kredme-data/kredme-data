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

## WORKSTREAM 0 — Two data environments (do everything through this)

> Replaces hand-editing `seed/` and `news/` on `main`. Those are now **prod**
> and should only ever be written by `promote`.

### The two lanes

| | Branch | URL | Read by |
|---|---|---|---|
| **DEV** | `dev` | `https://raw.githubusercontent.com/kredme-data/kredme-data/dev` | TestFlight / dev APK builds |
| **PROD** | `main` | `https://kredme-data.github.io/kredme-data` | every store build, always |

GitHub Pages can only serve **one** branch, which is why dev is served from
`raw.githubusercontent.com` (branch-addressable) rather than a Pages path.
Raw caches for ~5 minutes, so a dev change appears on your phone within that.

Dev may be identical to prod, or ahead of it. Data only ever flows one way:

```
edit on dev  ->  validate  ->  test on your phone  ->  promote  ->  push
```

### Commands

```bash
cd ~/Downloads/KredMe/kredme-data

python3 tools/kredme.py status                    # dev vs prod
python3 tools/kredme.py validate --target dev     # before you test
python3 tools/kredme.py validate --target prod    # what users have now
python3 tools/kredme.py promote --dry-run         # what prod would receive
python3 tools/kredme.py promote                   # dev -> prod (LOCAL)
python3 tools/kredme.py undo                      # restore previous prod
```

`promote` must be run from `main` and refuses if `seed/`/`news/` are dirty.
Nothing touches the network — it prints the `git push` for you to run.

### Changing dev data

```bash
git checkout dev
# edit news/feed.json or seed/*.json
python3 tools/kredme.py validate --target dev
git add seed news && git commit -m "news: ..." && git push origin dev
# within ~5 min your dev build sees it. Prod is untouched.
```

### Promoting to real users

```bash
git checkout main
python3 tools/kredme.py promote          # validates dev, snapshots prod, bumps versions
git add seed news && git commit -m "data: promote" && git push origin main
curl -s https://kredme-data.github.io/kredme-data/seed/manifest.json | head -5
```

### What the gate catches

| Check | Why |
|---|---|
| News uses `expiry_date` / `source_url` / `severity` | the app **ignores** `expires_at` / `url` |
| Every `affected_cards` id exists in `cards.json` | a typo means the alert reaches **nobody** |
| `merchant_ref` resolves to a `merchant_name` | else that reward rule never fires |
| Manifest checksums match real bytes | a mismatch is what shows as **"Sync failed"** |
| Empty merchants / categories | silently breaks all merchant matching |
| Card count vs prod (`--allow-shrink` to override) | catches a truncated export |
| Duplicate ids, dangling category, bad JSON shape | |

### Two footguns it handles for you

1. **News MAJOR bump.** The app refetches only when the leading integer
   increases. `promote` does it, and refuses rather than guessing if prod's
   version is unreadable — emitting a *lower* version would stop news forever.
2. **Checksum regeneration** from the bytes actually written.

A high-water mark (`.published/HIGHWATER.json`) means a version is never
re-used after an `undo` — otherwise a correction would reach nobody.

### If a bad promote goes out

```bash
python3 tools/kredme.py undo
git add seed news && git commit -m "data: roll back" && git push origin main
```

`undo` snapshots first, so the rollback is itself reversible.
`python3 tools/kredme.py undo --list` shows every restore point.

### Tests

```bash
python3 tools/test_pipeline.py     # 29 tests, no dependencies
```

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

## Categories: merchants.json must mirror the app

`seed/merchants.json` has a `categories` block. **The app never reads it** — it
resolves a merchant's `category_id` against its own BUNDLED asset,
`assets/data/categories/categories.json` (`lib/core/utils.dart`), and a miss
silently yields category "Other" so the merchant's reward rules never match.

That block therefore exists for one reason: so `validate` can check something
real. It is only useful while it mirrors the app's list exactly.

It had drifted: 9 slugs here did not exist in the app, orphaning 27 of 116
merchants — Netflix, Spotify, Blinkit, Zepto, Myntra, Nykaa and 21 more could
never match a category reward rule.

**If you add or rename a category in the app's bundled asset, mirror it here in
the same change.** Nothing can detect the drift automatically: this repo is
public and the app repo is private, so CI here cannot read that asset.
