This repository has had **no continuous integration of any kind**. Two consequences this fixes:

- `tools/test_pipeline.py` ships **29 self-tests** that only ever ran when somebody remembered to run them by hand
- the **live production data currently fails its own validator** with 3 errors, and nothing was watching

The tooling is the work from the long-open **PR #5**, minus the committed `__pycache__/*.pyc` — and `.gitignore` now covers those, which is how they got in.

| command | |
|---|---|
| `kredme.py status` | dev vs prod versions and restore points |
| `kredme.py validate --target dev\|prod\|working` | check an environment is safe |
| `kredme.py promote` | dev → prod, local only; you still push |
| `kredme.py undo` | restore prod from the previous snapshot |

## CI

Runs the self-tests, then `validate --target working` so a PR is judged on its own proposed content rather than on whatever is sitting on dev or main.

Plus a **strict manifest integrity check**. `validate --target working` deliberately runs with `strict_checksums=False`, because a mismatch is normal mid-edit ([`kredme.py:308`](https://github.com/kredme-data/kredme-data/blob/data/tooling-and-ci/tools/kredme.py#L308)). In CI the tree is *not* mid-edit — it is exactly what would be committed — and a stale checksum is precisely what makes the app reject a sync with **"Sync failed"**. That must not be able to reach a branch.

This repo is **public**, so these minutes are free — unlike the app repo, where the same job is billed.

## Expected: merging this turns `main` RED

That is correct, and it is the point. Production data has three real errors:

| where | problem |
|---|---|
| `merchants.json` | `csd_canteen` → category `departmental_store`, which does not exist (the list defines `department_store`) |
| `news_001` | ships `expires_at`; the app reads `expiry_date` |
| `news_001` | ships `url`; the app reads `source_url` |

Both news mismatches are confirmed against the app itself — `lib/services/news_feed_service.dart:58` and `:64` — not just asserted by the validator.

The fixes land on `dev` and reach `main` by promotion, which is the entire point of having two lanes.
