# Delta Patches

This directory contains delta update files for OTA sync.

## Format

Each delta file is named `delta_<from_version>_to_<to_version>.json` and contains:

```json
{
  "from_version": "1.0.0",
  "to_version": "1.1.0",
  "updated_at": "2026-04-01T00:00:00Z",
  "operations": [
    {
      "op": "upsert",
      "table": "cards",
      "data": { ... }
    },
    {
      "op": "upsert",
      "table": "reward_rules",
      "data": { ... }
    },
    {
      "op": "delete",
      "table": "reward_rules",
      "id": 12345
    }
  ]
}
```

## Operations

- `upsert`: Insert or update a record (matched by `id`)
- `delete`: Remove a record by `id`
