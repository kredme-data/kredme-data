# KredMe OTA Sync — Test Skeleton Playbook

This test skeleton contains 4 synthetic cards and 3 merchants designed to exercise
specific engine behaviors. Use version `0.0.1-test` in the manifest to distinguish
from production data.

---

## Test Cards Overview

| ID   | Name                          | Currency       | rpValueStd | Forex | Tests                                    |
|------|-------------------------------|----------------|------------|-------|------------------------------------------|
| 9001 | TEST Cashback Card            | cashback_inr   | 1.0        | 3.5%  | Cap exhaustion waterfall, merchant rules |
| 9002 | TEST RP Card (0.25)           | reward_points  | 0.25       | 3.5%  | **Engine bug visibility**, multiplier    |
| 9003 | TEST Threshold Card           | reward_points  | 0.50       | 2.0%  | Threshold tiers, milestones              |
| 9004 | TEST Conditional Card (0% fx) | cashback_inr   | 1.0        | 0.0%  | conditions_json, forex comparison        |

## Test Merchants

| ID   | Name              | Category        | Tests                                |
|------|-------------------|-----------------|--------------------------------------|
| 9001 | test_merchant_a   | online_shopping | Merchant-specific, conditional, caps |
| 9002 | test_merchant_b   | online_shopping | Multiplier, RP valuation bug         |
| 9003 | test_fuel_station | fuel (MCC 5541) | Exclusion rules, fuel surcharge      |

---

## Test Scenarios & Expected Results

### Scenario 1: Basic Merchant-Specific Ranking
**Input**: ₹1,000 at test_merchant_a, all 4 cards in wallet, user is NOT Prime
**Expected ranking**:
1. Card 9001: 10% × ₹1,000 = ₹100 cashback
2. Card 9004: 3% × ₹1,000 = ₹30 cashback (non-Prime conditional)
3. Card 9002: falls to category bonus or base — 5 RP/₹100 = 50 RP × ₹0.25 = ₹12.50
4. Card 9003: base 1X multiplier = 0.01 × ₹1,000 = 10 RP × ₹0.50 = ₹5.00

### Scenario 2: Conditional Rule — Prime vs Non-Prime
**Input**: ₹1,000 at test_merchant_a, cards 9001 + 9004 in wallet
- **Non-Prime user**: Card 9001 wins (₹100 vs ₹30)
- **Prime user**: Card 9001 still wins (₹100 vs ₹50), but gap narrows

### Scenario 3: Cap Exhaustion Waterfall
**Input**: Card 9001 has already earned ₹500 from test_merchant_a this month (cap exhausted)
**Expected**: Card 9001's merchant-specific rule (priority 90) is skipped.
Waterfall falls to category_bonus at priority 50: 2% × ₹1,000 = ₹20.
Card 9004 now wins with ₹30 (non-Prime) or ₹50 (Prime).

### Scenario 4: Engine Bug Detection (CRITICAL)
**Input**: ₹1,000 at test_merchant_b, card 9002 in wallet
**Bug scenario** (`_computePct` applies rpValueStandard twice for multiplier):
- base_reward = 0.01 (already includes rpValueStandard internally)
- Buggy: reward_pct = base_reward × 10 × 0.25 = 0.025 → ₹25 displayed as ₹6.25
**Correct result**: 
- multiplier 10X of base_reward_rate 0.01 = 0.10 (10% effective)
- Raw points: 0.10 × ₹1,000 = 100 points
- Normalized: 100 × ₹0.25 = ₹25.00

**If the engine shows ₹6.25 instead of ₹25.00, the bug is present.**

### Scenario 5: Threshold Tier Activation
**Input**: ₹5,000 at test_merchant_a, card 9003 in wallet
- **Cumulative spend < ₹50K**: 1X multiplier → 0.01 × ₹5,000 = 50 RP × ₹0.50 = ₹25
- **Cumulative spend > ₹50K**: 5X multiplier → 0.05 × ₹5,000 = 250 RP × ₹0.50 = ₹125

### Scenario 6: Exclusion Rule — Fuel
**Input**: ₹2,000 at test_fuel_station, all cards in wallet
**Expected**: All 4 cards return reward = ₹0 (fuel MCC 5541 excluded on all)
Card 9003 should show fuel surcharge waiver: 1% × ₹2,000 = ₹20 saved

### Scenario 7: International Transaction — Forex Comparison
**Input**: ₹1,000 international purchase at generic merchant, cards 9001 + 9004
- Card 9001: 1% base = ₹10, minus 3.5% forex = ₹35 markup → net = -₹25
- Card 9004: 1% base = ₹10, minus 0% forex = ₹0 markup → net = +₹10
**Card 9004 wins** despite identical reward rate (forex advantage)

---

## Delta Sync Test

File: `delta/delta_0.0.1-test_to_0.0.2-test.json`

After applying delta:
1. Card 9001's merchant_a cap drops from ₹500/mo to ₹300/mo
2. New promo rule: 15% at merchant_a (priority 110, April 2026 only, min ₹500)

**Verify**: 
- Before delta: ₹1,000 at merchant_a → Card 9001 gets 10% = ₹100 (cap ₹500)
- After delta in April: ₹1,000 at merchant_a → Card 9001 gets 15% = ₹150 (promo wins at priority 110)
- After delta in May: promo expired → back to 10% = ₹100 (but cap now ₹300, so ₹5,000 → only ₹300 max before waterfall)
