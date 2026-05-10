# `redemption_rules` schema — proposal v1

This is the proposed shape for the `redemption_rules` array on each card in `seed/cards.json`. It's designed to capture the three patterns that cardinsider's extracted data uses, plus an existing convention from the seed (`rp_value_standard` / `rp_value_travel` / `rp_value_transfer` on the card object).

**Read this top-to-bottom, then tell me to proceed or what to change.**

---

## Card-level additions (on `card.{...}`)

These three already exist in seed and stay; treat as the headline summary:

| Field | Type | Meaning |
|---|---|---|
| `rp_value_standard` | float \| null | ₹ per point at the most common redemption (usually cashback or basic vouchers). Already in seed. |
| `rp_value_travel` | float \| null | ₹ per point when redeemed via the bank's travel portal. Already in seed. |
| `rp_value_transfer` | float \| null | ₹ per point when transferred to airline/hotel partners (rough estimate; real value depends on partner program). Already in seed. |

Three NEW fields on `card.{...}`:

| Field | Type | Meaning |
|---|---|---|
| `points_expiry_months` | int \| null | How many months from earning before points expire. `null` = never expires. e.g. `36` for "valid 3 years". |
| `min_redemption_points` | int \| null | Global minimum points before any redemption is allowed (some cards have this; e.g. SBI cards often need 500 points). |
| `points_clawback_on_default` | bool | Whether unredeemed points are forfeited if you miss a payment / close the card. Important UX detail. |

---

## Per-card array `redemption_rules: [...]`

One entry per **distinct redemption channel**. A channel is "a way the user converts points to value" — cashback, travel portal, vouchers, partner transfers, etc.

### Common fields (every rule)

| Field | Type | Required | Meaning |
|---|---|---|---|
| `rule_name` | string | yes | Short label, e.g. "Statement Cashback", "SmartBuy Travel Portal", "18K Gold Voucher Tier" |
| `channel_type` | enum | yes | One of: `cashback` \| `travel` \| `merchandise` \| `voucher_catalog` \| `partner_transfer` \| `gift_card` \| `other` |
| `point_value_inr` | float \| null | yes | ₹ per point on this channel. Null when unknown or when channel is a catalog (per-voucher value lives in `voucher_options`). |
| `min_points` | int \| null | yes | Minimum points to redeem on this channel. Null = no minimum. |
| `max_points` | int \| null | yes | Maximum points per redemption. Null = no max. |
| `redemption_fee_inr` | float \| null | yes | ₹ fee charged per redemption on this channel. Null = no fee or unknown. |
| `channel_description` | string | yes | Human-readable description, e.g. "Flight/hotel bookings via HDFC SmartBuy" |
| `_sources` | array of `["bank", "cardinsider"]` | yes | Provenance per user policy |
| `_source_quotes` | object | yes | Verbatim quote per source |
| `confidence` | enum | yes | `high` \| `medium` \| `low` |

### Channel-specific extra fields

#### When `channel_type = "voucher_catalog"`
Use this for rules like AmEx's "18K Gold Collection" or "24K Gold Collection" where users redeem a fixed point amount for a catalog of branded vouchers, each with different ₹ value.

| Field | Type | Meaning |
|---|---|---|
| `voucher_options` | array | List of vouchers in this tier |
| `voucher_options[].voucher_brand` | string | e.g. "Taj", "Amazon", "Tanishq" |
| `voucher_options[].voucher_value_inr` | float | ₹ value of the voucher |
| `voucher_options[].points_required` | int | Points needed for this voucher (often the same across vouchers in a tier; included for clarity) |
| `voucher_options[].effective_value_inr` | float | Computed: `voucher_value_inr / points_required` — lets the app sort by best deal |

`point_value_inr` at the rule level should be set to the **highest** `effective_value_inr` across the catalog (= best-case scenario).

#### When `channel_type = "partner_transfer"`
For airline-mile or hotel-point transfers.

| Field | Type | Meaning |
|---|---|---|
| `transfer_partners` | array | List of partner programs |
| `transfer_partners[].partner_name` | string | e.g. "Asia Miles", "Marriott Bonvoy" |
| `transfer_partners[].partner_type` | enum | `airline` \| `hotel` |
| `transfer_partners[].transfer_ratio` | string | e.g. `"1:1"`, `"2:1"`. Default `"1:1"` if unknown. |
| `transfer_partners[].est_partner_value_inr` | float \| null | Estimated ₹ per partner-point. Null when unknown. |

`point_value_inr` at the rule level is the typical transferred-value (e.g. 1 MR → 1 airline mile → ~₹0.40 = `point_value_inr: 0.40`). This already maps to the existing `card.rp_value_transfer`.

---

## Worked example 1 — SBI Cashback Credit Card (simple, single channel)

```jsonc
{
  "card": {
    "id": "sbi_card_cashback_sbi",
    "card_name": "Cashback SBI Credit Card",
    "rp_value_standard": 1.0,           // 1 cashback point = ₹1 (cashback IS in rupees)
    "rp_value_travel": null,
    "rp_value_transfer": null,
    "points_expiry_months": null,        // cashback doesn't expire as points
    "min_redemption_points": null,
    "points_clawback_on_default": false
  },
  "redemption_rules": [
    {
      "rule_name": "Auto Cashback to Statement",
      "channel_type": "cashback",
      "point_value_inr": 1.0,
      "min_points": null,
      "max_points": null,
      "redemption_fee_inr": 0,
      "channel_description": "Cashback automatically credited to statement at end of cycle",
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Cashback automatically credited to statement"},
      "confidence": "high"
    }
  ]
}
```

---

## Worked example 2 — HDFC Doctor's Regalia (multi-channel rates)

```jsonc
{
  "card": {
    "id": "hdfc_bank_doctors_regalia",
    "rp_value_standard": 0.5,            // SmartBuy travel = best rate
    "rp_value_travel": 0.5,
    "rp_value_transfer": null,
    "points_expiry_months": 24,
    "min_redemption_points": null,
    "points_clawback_on_default": true
  },
  "redemption_rules": [
    {
      "rule_name": "SmartBuy Travel Portal",
      "channel_type": "travel",
      "point_value_inr": 0.5,
      "min_points": null, "max_points": null,
      "redemption_fee_inr": 99,
      "channel_description": "Flight and hotel bookings via HDFC SmartBuy portal",
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Flight and hotel bookings via SmartBuy"},
      "confidence": "high"
    },
    {
      "rule_name": "Cash Redemption",
      "channel_type": "cashback",
      "point_value_inr": 0.2,
      "min_points": 500, "max_points": null,
      "redemption_fee_inr": 99,
      "channel_description": "Statement credit / cash redemption",
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Cash redemption at 0.20/point"},
      "confidence": "high"
    },
    {
      "rule_name": "Premium Products & Vouchers",
      "channel_type": "merchandise",
      "point_value_inr": null,             // not stated
      "min_points": null, "max_points": null,
      "redemption_fee_inr": null,
      "channel_description": "Premium products and curated vouchers from HDFC catalog",
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Premium products and vouchers"},
      "confidence": "low"
    }
  ]
}
```

---

## Worked example 3 — AmEx Membership Rewards Credit Card (voucher catalog + transfers)

```jsonc
{
  "card": {
    "id": "amex_membership_rewards",
    "rp_value_standard": 0.37,           // 24K voucher tier best rate
    "rp_value_travel": null,
    "rp_value_transfer": 0.40,
    "points_expiry_months": null,
    "min_redemption_points": null,
    "points_clawback_on_default": true
  },
  "redemption_rules": [
    {
      "rule_name": "Statement Cash (18K tier)",
      "channel_type": "cashback",
      "point_value_inr": 0.33,
      "min_points": 18000, "max_points": null,
      "redemption_fee_inr": 0,
      "channel_description": "Statement credit at 1 MR = ₹0.33 via 18K tier",
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Statement cash (18K collection): 1 MR Point = ₹0.33"},
      "confidence": "high"
    },
    {
      "rule_name": "Statement Cash (24K tier)",
      "channel_type": "cashback",
      "point_value_inr": 0.37,
      "min_points": 24000, "max_points": null,
      "redemption_fee_inr": 0,
      "channel_description": "Statement credit at 1 MR = ₹0.37 via 24K tier",
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Statement cash (24K collection): 1 MR Point = ₹0.37"},
      "confidence": "high"
    },
    {
      "rule_name": "18 Karat Gold Collection (vouchers)",
      "channel_type": "voucher_catalog",
      "point_value_inr": 0.50,             // Taj at ₹9000/18000pts = best deal
      "min_points": 18000, "max_points": null,
      "redemption_fee_inr": 0,
      "channel_description": "Redeem 18,000 MR points for a brand voucher",
      "voucher_options": [
        {"voucher_brand": "Taj",              "voucher_value_inr": 9000, "points_required": 18000, "effective_value_inr": 0.500},
        {"voucher_brand": "Shoppers Stop",    "voucher_value_inr": 7000, "points_required": 18000, "effective_value_inr": 0.389},
        {"voucher_brand": "Tata Cliq",        "voucher_value_inr": 7000, "points_required": 18000, "effective_value_inr": 0.389},
        {"voucher_brand": "Myntra",           "voucher_value_inr": 7000, "points_required": 18000, "effective_value_inr": 0.389},
        {"voucher_brand": "Amazon",           "voucher_value_inr": 6000, "points_required": 18000, "effective_value_inr": 0.333},
        {"voucher_brand": "Flipkart",         "voucher_value_inr": 6000, "points_required": 18000, "effective_value_inr": 0.333},
        {"voucher_brand": "Reliance Digital", "voucher_value_inr": 6000, "points_required": 18000, "effective_value_inr": 0.333}
      ],
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "18 Karat Gold Collection (18,000 points): Taj 9000, Shoppers Stop 7000, ..."},
      "confidence": "high"
    },
    {
      "rule_name": "24 Karat Gold Collection (vouchers)",
      "channel_type": "voucher_catalog",
      "point_value_inr": 0.583,            // Taj at ₹14000/24000pts = best
      "min_points": 24000, "max_points": null,
      "redemption_fee_inr": 0,
      "channel_description": "Redeem 24,000 MR points for a higher-tier brand voucher",
      "voucher_options": [
        {"voucher_brand": "Taj",              "voucher_value_inr": 14000, "points_required": 24000, "effective_value_inr": 0.583},
        {"voucher_brand": "Shoppers Stop",    "voucher_value_inr": 10000, "points_required": 24000, "effective_value_inr": 0.417},
        {"voucher_brand": "Tata Cliq",        "voucher_value_inr": 9000,  "points_required": 24000, "effective_value_inr": 0.375},
        {"voucher_brand": "Tanishq",          "voucher_value_inr": 9000,  "points_required": 24000, "effective_value_inr": 0.375},
        {"voucher_brand": "Amazon",           "voucher_value_inr": 8000,  "points_required": 24000, "effective_value_inr": 0.333},
        {"voucher_brand": "Flipkart",         "voucher_value_inr": 8000,  "points_required": 24000, "effective_value_inr": 0.333},
        {"voucher_brand": "Reliance Digital", "voucher_value_inr": 8000,  "points_required": 24000, "effective_value_inr": 0.333}
      ],
      "_sources": ["cardinsider"],
      "confidence": "high"
    },
    {
      "rule_name": "Partner Mile Transfers",
      "channel_type": "partner_transfer",
      "point_value_inr": 0.40,             // ballpark; real value varies
      "min_points": null, "max_points": null,
      "redemption_fee_inr": 0,
      "channel_description": "Transfer MR Points to airline/hotel loyalty programs",
      "transfer_partners": [
        {"partner_name": "Marriott Bonvoy",      "partner_type": "hotel",   "transfer_ratio": "1:1", "est_partner_value_inr": null},
        {"partner_name": "Singapore KrisFlyer",  "partner_type": "airline", "transfer_ratio": "1:1", "est_partner_value_inr": null}
      ],
      "_sources": ["cardinsider"],
      "_source_quotes": {"cardinsider": "Transfer to partners: Marriott Bonvoy, KrisFlyer"},
      "confidence": "medium"
    }
  ]
}
```

---

## Open design decisions — please pick

### D1. Voucher tiers: separate rules or one rule with multiple tiers?
- **Option A** (above example): one rule per tier (e.g. "18K Gold", "24K Gold" are separate `redemption_rules` entries). Easier to render in app as separate "redemption tiers."
- **Option B**: one rule with a `tiers[]` sub-array. Tighter coupling but more nesting.

**My pick: A.** Each tier has different point_value, different min_points, app likely wants to show them as distinct options. Less app-side normalization needed.

### D2. Partner transfers: one rule with array, or one rule per partner?
- **Option A** (above): one `partner_transfer` rule with a `transfer_partners[]` array.
- **Option B**: one rule per partner (so KrisFlyer and Marriott are separate entries).

**My pick: A.** Partners are typically transferred at the same ratio with the same fee; differing only by partner program. Keeps the rule list short.

### D3. `point_value_inr` field naming
- The existing seed uses `rp_value_*` on the card object (rp = reward points).
- I proposed `point_value_inr` on the rule object (more explicit unit).

**My pick: stick with `point_value_inr` for new schema.** The `_inr` suffix prevents confusion (1 MR ≠ 1 cashback rupee ≠ 1 SBI Reward Point).

### D4. Voucher_value cell name
- Cardinsider uses both `value` and `worth` inconsistently.
- I propose normalizing to `voucher_value_inr` (same `_inr` convention).

**My pick: voucher_value_inr.**

### D5. Confidence tagging from cardinsider extractions
- The extracted JSONs don't have per-channel confidence, but they do have wrapper-quality signals (well-formed vs LLM-prose-wrapped, complete vs sparse).
- Proposed: confidence = `high` if cardinsider has explicit `point_value` set; `medium` if structured but missing point_value; `low` if free-text channel name with no numeric data.

**My pick: yes, the auto-tag rule above.**

### D6. What to do for cards where redemption is unclear?
84 of 376 seed cards have extracted JSONs where `redemption.channels` is empty or sparse.
- **Option A**: Leave their `redemption_rules: []` empty for now and flag in a separate `cards_missing_redemption.csv`.
- **Option B**: Synthesize a generic "Statement Credit" rule with `confidence: low` and `point_value_inr` derived from `card.rp_value_standard`.

**My pick: A.** Empty arrays + a CSV gap-list is honest. Synthesizing fake rules just hides the data gap.

---

## What happens after you approve

1. I write `tools/build_redemption.py` that reads each card's extracted JSON, normalizes the channels, and emits the v1 `redemption_rules` array per card.
2. I run it across all 376 seed cards. ~292 get rich rules; ~84 get empty arrays + are listed in a follow-up CSV for you to research from official URLs.
3. I update `seed/cards.json` with the new rules + the 3 new card-level fields.
4. I bump manifest to v5.1.0 and recompute checksum/stats.
5. Open a PR with: the updated cards.json, this SCHEMA.md document committed at `seed/SCHEMA_redemption.md`, and a `cards_missing_redemption.csv` for the 84 gap cards.

**Tell me to proceed (or mark which design decisions you want changed).**
