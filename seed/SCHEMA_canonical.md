# Canonical rate schema (seed v5.2.0+)

Every `reward_rule` carries two canonical fields computed at seed-build time:

| Field | Type | Meaning |
|---|---|---|
| `earned_inr_per_inr_spent` | float \| null | ₹ earned per ₹1 of qualifying spend (e.g. `0.05` means 5% effective). |
| `cap_inr` | float \| object \| null | Cap on this rule's earnings, normalised to ₹. For tiered caps (e.g. HDFC Easy EMI), an object with named tiers. |

The engine reads `earned_inr_per_inr_spent` directly and computes earned ₹ as:

```
earned_inr = min(spent × rule.earned_inr_per_inr_spent, rule.cap_inr − used_so_far)
display_pct = rule.earned_inr_per_inr_spent × 100
```

This **eliminates** the per-`reward_type` branching that was causing bugs (cashback_pct decimal-format inversion, multiplier-vs-absolute ambiguity, point_value fallback inconsistency).

## Conversion table by reward_type

The seed pipeline computes `earned_inr_per_inr_spent` from the original rule fields using this table. `point_value` resolves as `rule.point_value ?? card.rp_value_standard ?? CURRENCY_DEFAULT`.

| `reward_type` | Formula | Notes |
|---|---|---|
| `cashback_pct` | `rate` | rate is fraction (e.g. `0.05` = 5%). |
| `points_per_spend` | `(rate / unit_spend) × point_value` | rate is absolute pts per `unit_spend` ₹. unit defaults to 100 if null. |
| `multiplier` (base > 0) | `base_reward_rate × rate × point_value` | rate is multiplier-of-base ("5X" means 5× base). |
| `multiplier` (base = 0) | `(rate / unit_spend) × point_value` | Co-branded cards (Vistara / IndiGo / SpiceJet etc.) have no general base; seed encoded absolute rate as `multiplier`. Fallback treats it as `points_per_spend`. |
| `cashback_flat` | `null` | Flat one-time rewards (welcome bonuses, milestone vouchers) don't have a per-₹ rate. |

## Cap conversion

| Source | `cap_inr` |
|---|---|
| `cap_kind: 'spend'` | `cap_amount × earned_inr_per_inr_spent` (max-spend → max-cashback equivalent) |
| `cap_kind: 'reward'` and `reward_type: cashback_pct` | `cap_amount` (already in ₹) |
| `cap_kind: 'reward'` and `reward_currency: cashback_inr` | `cap_amount` (1 cashback unit = ₹1) |
| `cap_kind: 'reward'` and points currency | `cap_amount × point_value` |
| `cap_amount` is a dict (tiered caps) | dict preserved with normalised tier values |

## Card-level rp_value_standard defaults

Cards missing `rp_value_standard` had it set from currency-based defaults (101 cards in v5.2.0). Marked with `_rp_value_inferred: true` for audit. Defaults:

| Currency | Default `rp_value_standard` |
|---|---|
| `reward_points` | `0.25` |
| `membership_rewards` | `0.40` |
| `intermiles` | `0.30` |
| `edge_points` | `0.20` |
| `cashpoints` | `1.0` |
| `neucoins` | `1.0` |
| `supercoins` | `1.0` |
| `cashback_inr` | `1.0` |

These are conservative averages for the Indian market. Cards with explicit `rp_value_standard` are unchanged.

## Engine integration

Engines should:
1. Prefer `rule.earned_inr_per_inr_spent` if present.
2. Fall back to per-type computation if null (legacy compat for unknown rule shapes).

Display sites should use `earned_inr_per_inr_spent × 100` for percent display, and `earned_inr_per_inr_spent × txn_amount` for ₹ display. No more ad-hoc `* 100` / `/ 100` scattered across screens.

## Audit artifacts

- `handoff_v4/canonical_high_rate_audit.csv` — every rule with effective ≥10%. Many are correct (premium accelerators) but a few expose seed data bugs (e.g. Axis Bank Cashback `base_reward_rate: 0.75` is clearly wrong — should be `0.01`). Those are not fixed here; they need separate seed-data PRs.

## Why this matters

Before v5.2.0, the engine had three different formulas keyed by `reward_type`, each with implicit unit conventions (rate-format, unit_spend default, point_value fallback path). When the conventions disagreed between seed and engine, every cashback card displayed earnings 100x off (commit 929c97d4 issue). When the seed encoded "5X" with `reward_type: multiplier` but the engine treated it as absolute, SBI Elite dining showed 1.25% instead of 2.5%. Each new card structure has been a fresh chance to break things.

After v5.2.0, the seed pipeline owns all the unit math; the engine reads one fraction. New rule types added later just need their entry in the conversion table — engine doesn't need to change.
