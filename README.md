# kredme-data

OTA seed data and delta sync backend for KredMe, hosted via GitHub Pages.

**Base URL**: `https://kredme-data.github.io/kredme-data`

## Structure

```
seed/
  manifest.json          ← version info, checksums, file list
  cards.json             ← full card catalog with reward rules
  merchants.json         ← merchant catalog with MCC mappings

delta/
  delta_X_to_Y.json      ← incremental patches between versions

news/
  feed.json              ← in-app news/promo feed
```

## Endpoints

| Path | Description |
|------|-------------|
| `/seed/manifest.json` | Version manifest — app checks this on launch and every 12h |
| `/seed/cards.json` | Full card + reward rules payload |
| `/seed/merchants.json` | Merchant catalog with MCC codes and statement aliases |
| `/delta/delta_<from>_to_<to>.json` | Delta patch between specific versions |
| `/news/feed.json` | News/promo feed items |

## Update Workflow

1. Edit seed JSON files
2. Recompute SHA-256 checksums: `sha256sum seed/cards.json seed/merchants.json`
3. Update `manifest.json` with new checksums, file sizes, and bumped version
4. If delta sync: create `delta/delta_<old>_to_<new>.json` and set `delta_file` in manifest
5. Commit and push — GitHub Pages deploys automatically

## Data Format

See `KredMe_Backend_Architecture_v2.pdf` Section 10.2 for the complete JSON seed format specification.

Each card entry contains: `card` (master catalog fields), `reward_rules` (array), `exclusion_rules` (array), `milestone_rules` (array), `fuel_surcharge_rules` (array).
