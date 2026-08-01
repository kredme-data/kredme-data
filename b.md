Depends on the tooling PR (this branch includes it).

## The three errors, all confirmed against the app source

| where | problem |
|---|---|
| `merchants.json` | `csd_canteen` pointed at category `departmental_store`; the list defines `department_store`. The merchant fell back to **"Other"**, so its category rules never matched. |
| `news_001` | shipped `expires_at`; the app reads `expiry_date` ([`news_feed_service.dart:64`](https://github.com/nousonworktechnologies/KredMe/blob/master/lib/services/news_feed_service.dart#L64)) |
| `news_001` | shipped `url`; the app reads `source_url` (`:58`) |

The two news mismatches are **latent rather than live today** only because both values happen to be `null`. The moment a real item carries an expiry or a link, both are silently dropped.

Warnings cleared too: 5 merchants shared a numeric id (113/115/116/118/121), so the later of each pair was renumbered above the current max. Safe — **all 51 `merchant_ref` values in `cards.json` key on `merchant_name`, never on this integer** (verified, not assumed).

**Deliberately not changed:** `smartbuy_hdfc`, `travel_edge_axis`, `idfc_app_portal` and `payzapp_hdfc` have a null `category_id`. Those are *portals*, not spend categories — the null is correct and the validator is right not to flag them.

## The versions are the part that makes two lanes real

`seed 5.1.0 → 5.1.1`, `news 1.0.0 → 2.0.0`

The app skips the download entirely when `serverVersion == localVersion` ([`seed_sync_service.dart:99`](https://github.com/nousonworktechnologies/KredMe/blob/master/lib/services/seed_sync_service.dart#L99)). With dev and main **both on 5.1.0**, switching data source in Developer Options would fetch nothing and **appear to do nothing** — even though the data differs. News needed a *major* bump specifically, because the app only refetches news on a leading-integer increase.

The manifest is regenerated as well: editing `merchants.json` invalidated its stored checksum and size, and a stale manifest is exactly what produces **"Sync failed"**.

## After this merges

`dev` and `main` differ for the first time — dev valid, main still carrying the three errors. That is a genuine two-lane setup you can test on a phone, and `promote` is what closes the gap.
