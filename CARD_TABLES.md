# The ten cards, one table each

What we changed, what the bank actually says, and what a user sees move. Every issuer value below
is the verbatim sentence stored on the row that holds the number, read from the issuer's own site
or PDF on **20 August 2026**. Rows are ordered biggest user-facing correction first.

**LEAD** marks a value that comes from an aggregator (a card-review site), not from the bank. A
LEAD is a starting point for a check, never a fact. **Rendered percentages are what the app shows
today**, replayed through the app's own recommendation engine against all 273 merchants.

---

## 1. IndianOil HDFC Bank Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| the three "5%" bonus rates (IndianOil pumps, groceries, bill-pay) | 5% treated as 5% cash | *"2.2 5% Fuel Points on fuel transactions at IOCL petrol pumps & outlets"* + *"Please note that 1 Fuel Point = 1 Reward Point"* + *"The redemption against the statement balance will be at the rate of 1 Fuel Point = ₹0.2"* | **Corrected.** "5%" is 5% in Fuel Points worth 20 paise each, i.e. **1.0% in rupees** | 21 merchants drop **5.00% → 1.00%**. The card stops being recommended as a 5% card when it pays 1% |
| base rate at non-IndianOil pumps | base tier paid 0.13% at every pump | *"Exclusion for 1 FP for every INR 150 – Fuel, Rent, Government related transactions, EMI, Wallet and Jewellery."* | **Corrected.** A `fuel`-scoped rule at rate 0; IndianOil's own merchant rule still outranks it | BPCL, HPCL, Shell, Jio-BP, Nayara: **0.13% → no rewards**. IndianOil unchanged at 1.00% |
| grocery / bill-pay eligibility | matched our category slug | *"Grocery 5411, 5499, 5921, 5462 \| Utility & Telecom 4812, 4814, 4900"* | **Corrected** by MCC, via `conditions_json` | Licious, FreshToHome (MCC 5422) and FreeCharge (MCC 6012): **1.00% → 0.13%** — they were never eligible |
| `base_reward_rate` | 0.0 — the whole card rendered 0.00% | *"1 Fuel Point for every ₹150 spent (including UPI transactions) … Min Transaction of INR 150"* | **Corrected**, and now carries its quote in a card-level `_provenance` block | 214 merchants **0.00% → 0.13%**. The card stops ranking last on every purchase |
| IndianOil cap | the sentence *"250 FP/month first 6 months, 150 FP/month thereafter"* stored as text, so the app dropped the cap entirely | *"Max 250 Fuel Points per month in first 6 months, Max 150 Fuel Points post 6 months from card issuance per month*"* | **Partly.** 150 stored — the conservative tier — at `confidence: medium` with `source_conflict`. The schema cannot hold a cap that steps down by card age | A cardholder past six months is now capped correctly at ₹3,000/month. **A first-six-months cardholder is cut off ₹2,000 early.** Written on the row |
| grocery rate confidence | `high` | The same T&C pays 5% on grocery in §2.3 **and** lists *"4. Supermarkets"* among *"2.1 Reward Points will not be accrued for the following transactions"* | **Flagged, not decided.** `confidence: medium`, `source_conflict: true`, and the conflict written out in words | No rate change. The card's grocery rate is marked as contested |
| exclusions (wallet, rent, government, EMI) | four rows with no source at all | *"2.1 Reward Points will not be accrued … 1. Wallet 2. Rent 3. Government related transactions … 6. EMI (all type) … 11. Insurance"* | **Corrected.** All four now cite it | No rate change; three of the four were already enforced |
| `other: emi transactions` | inert | — | **Cannot be fixed in data.** The app matches an exclusion only on MCC or category; an EMI conversion is neither | Nothing. Kept as evidence, with a note saying why |
| telecom bill-pay | 0.13% | telecom MCCs 4812/4814 **are** on the issuer's eligible bill-pay list | **Known under-statement, not fixed.** The rule holds one category and a second rule would have to reuse the frozen name | 7 telecom billers stay at **0.13%** when the issuer pays 1.00% |
| the frozen rule names "**5%** Fuel Points on grocery / utility bill payments" | unchanged | — | **Blocked.** Renaming resets every holder's saved cap progress | The detail screen prints "5%" above a computed 1.00%. Decision (a)/(b) in the PR |
| redemption values (IXRP 96 paise, catalogue 20 paise, statement cash 20 paise) | 3 rows | statement cash **is** issuer-quoted; the other two rows cite CardInsider | **LEAD** on 3 of 3 rows | Nothing yet |

---

## 2. IndianOil Axis Bank Premium Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| `base_reward_rate` | 0.0, multiplied into every rule — **the card rendered 0.00% at an IndianOil pump** | *"Base points: EDGE MILE on every INR 150 spent: Customer shall earn 1 EDGE Mile (1EDGE Mile = INR 1) on every INR 150 spent"* | **Corrected**, with the quote in `_provenance` | **The single biggest fix in this PR.** IndianOil pump **0.00% → 4.00%**; 17 grocery merchants **0.00% → 1.33%**; 203 others **0.00% → 0.67%** |
| `rp_value_standard` | 1.0 with no source | the same sentence states *"1EDGE Mile = INR 1"* inline, unconditionally | **Corrected** — now sourced rather than assumed | No rate change; the number behind every rate is now checkable |
| `reward_unit_spend` on fuel and grocery | null, so the app fell back to a ₹100 block | *"up to INR 15,000 per statement month"*, *"up to INR 5,000 per statement month"* | **Corrected** to 150 | The 600-mile fuel cap now burns at ₹15,000 (was ₹10,000) — the issuer's own figure |
| grocery cap | 33 | prose: *"up to INR 5,000 per statement month (33 EDGE Miles)"*; table row in the same PDF: *"Grocery / Supermarkets (C) Purchase 5,000 2x 33 NA 33 66"* | **Raised to 66, at `confidence: medium` with `source_conflict`.** 66 is what ₹5,000 pays at 2X per ₹150 — an inference, not a quoted cap | The cap bar now runs to ₹4,950 of grocery instead of ₹2,475. **The frozen rule name still says "max 33 EDGE Miles."** Fourth red-gate item |
| `fee_waiver_spend` | ₹1,00,000 | *"₹30,000"*, confirmed by two issuer documents | **Corrected**, stamped in `_provenance` marked as evidence for that field only | The fee-waiver target shown on the card page falls to ₹30,000 |
| exclusions (utilities, insurance, education, government, wallet, rent, jewellery) | seven rows, one sourced | *"For reward earn: Transactions made on Utility & Telecom, Education, Rent, Wallet load, Government services, Insurance, Gold & Jewellery, Financial Institutions, Cash advances and Repayments."* | **Corrected.** All seven now cite it | No rate change; jewellery was already added (issuer added it 20-Jun-2025) |
| `other: transportation and tolls` | an exclusion with no source | **absent** from the issuer's own exclusion list | **Deleted.** It was inert, so nothing a user sees changes | Nothing |
| `min_txn_amount` ₹150 | added | *"EDGE Miles will not be awarded for any transactions below Rs. 150."* | **Written, but the app reads nothing from this field** | Nothing. The app will still recommend this card for a ₹100 fuel purchase that earns zero |
| redemption values (IOCL fuel ₹1, Travel Edge 20 paise, transfer, XRP ₹1) | 4 rows | all four cite CardInsider | **LEAD** on 4 of 4 rows | Nothing yet |

---

## 3. HDFC Bank MoneyBack Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| `base_reward_rate` | 0.0 — the card rendered 0.00% | *"Earn 2 Reward Points on every Rs 150 spent"* | **Corrected**, with the quote in `_provenance` | 64 merchants **0.00% → 0.27%** |
| `rp_value_standard` | 0.2 with no source | *"Reward Points accumulated on HDFC Bank MoneyBack Credit Card can be redeemed against statement balance at the rate 1RP= Upto Rs 0.20"* | **Corrected** — sourced. Note the issuer's own hedge, "Upto" | No rate change |
| online 2X cap | 500 Reward Points | *"customers can earn up to 500 Reward Points per statement cycle for online spends, under the 2X RP feature (i,e, Online spends of up to Rs 37,500 per cycle will earn 2X RP …"* | **Corrected** to `cap_amount: 37500`, `cap_kind: "spend"`. The 500 counts only the **bonus** 2 points per ₹150; the app counts all 4 | The 2X cap bar used to say "exhausted" at **₹18,750** of online spend. It now runs to the issuer's **₹37,500** |
| rent exclusion | missing | *"Rent payments will not earn Reward Points/CashPoints on all the cards."* | **Added**, confidence high | NoBroker, CRED RentPay and 2 more: **0.53% → no rewards** |
| government exclusion | missing | *"Government related transactions will not earn Reward Points/CashPoints on all the cards except … Business Money back, CSC small business moneyback …"* — consumer MoneyBack is **not** on the exempt list | **Added at `confidence: medium`**, because this rests on absence from a list rather than a positive statement | GST, income tax, Parivahan, Passport Seva, BharatKosh: **0.53% → no rewards** |
| `other: gift vouchers`, `other: prepaid cards` | two exclusions with no source | **absent** from every HDFC document read | **Deleted.** Both were inert | Nothing |
| the frozen name "capped at **500 reward points**" | unchanged | — | **Blocked.** Renaming resets cap progress | The detail screen prints "500 reward points" while the field holds ₹37,500 of spend. Two of the PR's 19 errors are this |
| fuel exclusion (MCC 5541) | present | MoneyBack is **absent** from HDFC's no-points-on-fuel list | **Left alone deliberately.** Removing an exclusion raises earn, and a raise needs a positive quote, not an inference | Nothing |
| redemption values (statement cash 20p, SmartBuy 25p, vouchers 25p, air miles 25p) | 4 rows | statement cash and SmartBuy **are** issuer-quoted at 20p and 25p; the air-miles rate is not published by HDFC | **LEAD** on 4 of 4 rows as stored | Nothing yet |

---

## 4. Axis Bank Atlas Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| base rule `point_value` = ₹1 | written on the base rule, taking the card to 2.00% card-wide while the card tile still said 0.50% | *"1 EDGE Mile is equal to INR 1"* — but the sentence sits under the **Travel EDGE portal**, and the same T&C says *"EDGE Miles credited cannot be en-cashed."* | **Deleted.** No sentence supports a mile's value on ordinary spend | 224 merchants **2.00% → 0.50%**. Card tile and base rule now agree |
| `rp_value_standard` | null | the only published value is the travel-portal ₹1 above | **Left null, and escalated.** The 17-Aug channel policy bars a travel-portal rate. **The app then invents ₹0.25 per mile**, which is nobody's number | Every Atlas rate a user sees is computed from an invented 25 paise. This needs a decision, not a data fix |
| travel rule | rendered ~2% as a `multiplier` | *"Earn 5 EDGE Miles for every Rs. 100 spent on Travel EDGE, airline and hotel merchants up to cumulative transactions of Rs. 2,00,000 per month … 1 EDGE Mile is equal to INR 1"* | **Corrected** to 5 miles per ₹100, `point_value` ₹1 kept **only here** because this row's scope is the portal | Renders 5.00% on the card detail screen — and **never** in the pick, because `portal_bonus` rules are skipped by the engine |
| six exclusions' `also_excludes_from_threshold` | 0 | *"spends threshold for Milestone achievement will exclude transactions done on Gold/ Jewellery, Rent, Wallet, Government Institution, Insurance, Fuel, Utilities and Telecom merchants w.e.f. 20th April, 2024"* | **Corrected** to 1 | Users stop being credited with progress toward a 2,500-mile bonus Axis will not pay |
| fuel MCCs | 5541 only | *"Fuel 5541, 5542, 5983"* | **Corrected.** 5542 (unmanned pumps) and 5983 added | No merchant in our catalogue carries 5542 or 5983 yet, so nothing moves today |
| `base_reward_rate` 0.02 | no source | *"2 EDGE Miles per INR 100 spent"* | **Now sourced** in `_provenance` | No rate change |
| redemption rows (Travel Edge portal, partner transfer) | 2 rows, no rupee values | both cite CardInsider | **LEAD** on 2 of 2 rows | Nothing yet |

---

## 5. Flipkart Axis Bank Super Elite Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| `base_reward_rate` | 0.0 — **the card rendered 0.00% at all 273 merchants and ranked near-last on every purchase** | *"Earning outside Flipkart: 2 SuperCoins for every ₹100 spent on all eligible spends (uncapped)"* | **Corrected** to 0.02 SuperCoins per rupee, with the quote in `_provenance` | 241 merchants **0.00% → 0.50%**; Flipkart **0.00% → 3.00%** |
| `rp_value_standard` | **1.0 — an aggregator estimate** | Flipkart, which owns the currency: *"It is agreed and understood that neither SuperCoins, nor Partner Points, shall have any monetary value assigned to them."* | **Deleted.** We will not write a rupee value the program owner disclaims | **Every percentage this card now shows is an issuer-quoted coin count times the app's invented ₹0.25.** Flagged as the loudest open item in the PR |
| the two Flipkart earn rules | typed as `multiplier`, so 12 was being multiplied *by* the base rate | *"Plus customers: 12 SuperCoins for every ₹100 spent on Flipkart capped at 300 coins per transaction"*; *"Non-Plus customers: 6 SuperCoins for every ₹100 spent on Flipkart capped at 150 coins per transaction"* | **Corrected** to absolute counts per ₹100 | Flipkart renders 3.00% (Plus) instead of 0.00%. It would render 12% at ₹1/coin — which is why the coin value matters |
| a base rule | none existed | *"Other spends ^: 2 SuperCoins for every INR 100 spent using the Flipkart Axis Bank Super Elite Credit card"* | **Added.** Its name deliberately carries **no percentage**, because the percentage depends on a coin value nobody publishes and rule names are frozen for ever | 241 merchants get a rate instead of a blank |
| exclusions (fuel, jewellery, gift cards, EMI, cash advance) | five rows, no source | *"Supercoins issuance shall not be eligible for following spends/transactions on the card, Fuel Spends , Purchase of gift cards on Flipkart, EMI transactions , Purchases converted to EMI post facto, Wallet loading transactions ,Purchase of Jewellery items , Cash…"* | **Corrected.** All five now cite it | No change; fuel and jewellery were already enforced |
| `category: wallet_load` | missing | named in the same sentence | **Added** | Paytm Wallet, PhonePe Wallet, Amazon Pay, MobiKwik, Ola Money: **→ no rewards** |
| fuel surcharge band | not written | no issuer figure exists; only a CardInsider band | **LEAD, and not written** | Nothing |
| SuperCoin value 0.75–1.0 | not written | *"Considering supercoin value as 0.75 to 1, the benefit is 12-15%"* — CardInsider | **LEAD, refused** | Nothing |

---

## 6. Tata Neu Infinity HDFC Bank Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| UPI rate | 1.5% | *"With effect from 01-Aug-24: - 0.50% back as NeuCoins for transactions done using any UPI ID (Google Pay, PhonePe, Cred, etc.)"*. The extra 1% needs a Tata Neu UPI ID and *"is directly posted in your Tata NeuPass Account"*, not the card | **Corrected to 0.50%** | 233 merchants **1.50% → 0.50%**. This is the card's default lane, so it is the largest single move on this card |
| "Additional 5% NeuCoins … bringing total to 10%" | a rule | **no 10% exists in any HDFC document.** NeuPass is a Tata Digital membership perk on unspecified categories, restricted since 15-Jan-25 to the primary cardholder's number | **Deleted** | The card stops advertising a 10% rate that no bank document supports |
| 11 partner-brand caps | NeuCoin figures derived by multiplying the issuer's rupee row by the rate — numbers that appear nowhere in the quote | *"Electronics \| Croma \| 6,00,000 \| 18,00,000"*, *"Travel \| IHCL Air India Express Air India* \| 8,00,000 \| 30,00,000"*, and so on | **Corrected.** `cap_amount` is now the issuer's rupees of monthly eligible spend with `cap_kind: "spend"` | The cap binds at the same rupee amount as before — this is an evidence fix, not a behaviour fix. Every cap number is now in its own quote |
| shared partner pots | each brand rule holds the whole pot | Taj, Air India and Air India Express draw **one** ₹8,00,000 pot; Tata CLiQ and Westside one ₹3,00,000; Titan and Tanishq one ₹6,00,000; 1mg/Cult/Tata Play one ₹2,00,000 | **Recorded, not fixed.** The app buckets caps by rule name and the names cannot change | A month spanning several brands in one group over-counts the cap up to 3x. Those rows are now `confidence: medium` with the sharing written on them |
| `category: government` | missing | *"… Rental and Government related transactions"* — in the same sentence already cited on this card's other exclusions | **Added** | GST, income tax, Parivahan, Passport Seva, BharatKosh: **1.50% → no rewards** |
| all 14 rules' provenance | 11 quotes, no links, no dates | the Annex-1 rows and the T&C table | **Corrected.** 14 of 14 now carry URL + quote + date + confidence | Nothing directly; the card is now checkable |
| `rp_value_standard` 1.0 | no source | *"NeuCoins can be utilized at the rate of 1 NeuCoin = Rs 1"* | **Now sourced** in `_provenance` | No rate change |
| the frozen name "**1.5%** NeuCoins on UPI spends" | unchanged | — | **Blocked.** Renaming resets cap progress | The detail screen prints "1.5%" above a computed 0.50%. One of the seven |
| `other: cash withdrawals`, `other: emi transactions` | inert | named verbatim in the exclusion sentence | **Evidenced, still inert.** Neither is a merchant category or an MCC | Nothing. Kept as evidence with a note |
| redemption row (Tata Neu app, ₹1) | 1 row | cites CardInsider, though the T&C states the same ₹1 | **LEAD** as stored | Nothing yet |

---

## 7. Tata Neu Plus HDFC Bank Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| Tata Neu app rate | 7% | *"Non-EMI Retail Spends on partner Tata Brands \| 2% back as NeuCoins"*, and *"Currently, Bill Payment (Tata Pay), Tanishq, Cult.fit, Air India, Tata Play spends are not eligible for additional 5% NeuCoins via NeuPass Membership"* | **Corrected to 2.00%** | Tata Neu **7.00% → 2.00%** |
| UPI rate | 1% and uncapped | *"0.25% back as NeuCoins for transactions done using any UPI ID"* and *"The total NeuCoins earned on eligible UPI transactions are capped to 500 NeuCoins per month"* | **Corrected to 0.25%** with the 500-coin monthly cap | No merchant moves today — this card is not flagged RuPay-UPI, so the UPI lane never runs (one of the PR's 19 errors) |
| 11 partner-brand caps | derived NeuCoin figures | *"Electronics \| Croma \| 3,00,000 \| 9,00,000"*, *"Stay \| IHCL / Air India Express / Air India* \| 4,00,000 \| 15,00,000"*, *"Grocery \| Big Basket \| 50,000 \| 6,00,000"* … | **Corrected** to rupees of monthly eligible spend, `cap_kind: "spend"` | Same binding point; the numbers are now in their own quotes |
| shared partner pots | each rule holds the whole pot; BigBasket appears twice (direct and via Tata Neu) and each copy holds the pot | one pot per Annex-1 row | **Recorded, not fixed** — same schema limit as Infinity | Over-count of up to 3x in a multi-brand month. Rows marked medium |
| `category: government` | missing | *"… Rental and Government related transactions"* | **Added** | GST, income tax, Parivahan, Passport Seva, BharatKosh: **1.00% → no rewards** |
| `base_reward_rate` 0.01, `rp_value_standard` 1.0 | no source | *"Non-EMI Retail Spends on Non-Tata Brands \| 1% back as NeuCoins"*; *"each NeuCoin may be utilized to avail a Benefit equivalent to Rs 1 on Tata Neu Rewards Program-Eligible Purchases."* | **Now sourced** in `_provenance` | No rate change |
| the frozen names "**7%** … " and "**1%** back as NeuCoins on UPI spends" | unchanged | — | **Blocked** | Detail screen prints 7% above 2.00%, and 1% above 0.25%. Two of the seven |
| `has_rupay_upi` = 0 | left as 0 | *"Any UPI Spends on Rupay Credit Card (Including partner Tata Brands)"* implies a RuPay variant exists | **Deliberately not flipped.** Flipping it makes the engine assume every purchase on this card is paid by UPI, which would drop the card from 1.00% to 0.25% almost everywhere | Nothing. Needs an app-side "how did you pay" input, not a data edit |
| `other: smart emi` | inert | *"Smart EMI / Dial an EMI transaction"* | **Evidenced, still inert** | Nothing |
| redemption row (Tata Neu app, ₹1) | 1 row | cites CardInsider | **LEAD** | Nothing yet |

---

## 8. IndianOil Kotak Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| utilities / insurance / government exclusions | three `category` rows sharing one quote that named none of them and printed no MCC | the MITC MCC table, verbatim: *"Utility 4900, 4814, 4899, 4812"*, *"*Insurance 5960, 6300, 6381, 6399"*, *"*Government 9222, 9223 9311, 9399, 9402, 9405"*, under the heading *"2. MCC excluded from earning Reward Points, Air Miles, Cashback and Milestone spend calculations…"* | **Rewritten as 14 `mcc` rows**, each quoting the table row that names its own category and codes | **FreeCharge (MCC 6012) stops being wrongly zeroed: no rewards → 0.40%.** Airtel, Jio, Vi, BSNL, JioFiber, ACT, Tata Play, Hotstar, Netflix and 16 more (MCC 4814 / 4899, which **are** on the list): **0.40% → no rewards** |
| `also_excludes_from_threshold` on those rows | 0 | the cited heading says *"and Milestone spend calculations"* | **Corrected** to 1 | Users stop being credited with milestone progress the bank will not count |
| fuel and grocery+dining rates | the card's two most important rates carried **no provenance at all**, while the least important rule was marked `high` | *"4% back in Reward Points (24 points/₹150), capped at 1200 Reward Points (₹300) per statement cycle on IndianOil Fuel pump spends."*; *"2% back in Reward Points (12 points/₹150), capped at 800 Reward Points (₹200) per statement cycle on Grocery and Dining spends."* | **Corrected.** 5 of 5 rules now cite the MITC verbatim | No rate change. The card's headline numbers are now checkable |
| department-store cap of 800 | `confidence: high` | the cited sentence caps the whole grocery-and-dining MCC set — *"5812, 5814, 5813, 5411, 5311, 5399, 5422, 5451, 5499 & 5441"*, which **includes 5311, this row's MCC** — at **one shared 800** | **Dropped to `confidence: medium`** with the overstatement written on the row. The app cannot pool a cap across two rule names, and names cannot change | **A user can earn 1,600 reward points against an issuer ceiling of 800.** No rate changes; the ceiling is wrong and now says so |
| fuel exclusion | not added | *"Rewards/ Cashback restriction on fuel & online skill-based gaming purchases under above-mentioned MCC are not applicable for IndianOil Kotak, IndiGo & PVR Credit Cards"* | **Correctly refused.** A blind sweep would have zeroed the card's whole reason to exist | Nothing — and that is the point |
| `rp_value_standard` 0.2 | no source | *"Points can be redeemed at cash value of 20 paise per point"* | **Now sourced.** The catalogue rate of 25 paise is a separate, conditional channel and is **not** used for ranking | No rate change |
| welcome-benefit `bonus_description` | missing (a required field) | *"1000 Reward Points as welcome benefit on a single transaction of INR 500 or above within 30 days of card issuance"* | **Filled in** from the issuer's sentence | The welcome benefit now renders with a description instead of a blank |
| redemption rows (cashback 20p, catalogue 25p) | 2 rows | both cite kotak.bank.in | **Issuer-sourced — the only card of the ten with no LEAD redemption row** | Nothing |

---

## 9. IDFC FIRST Millennia Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| dining and travel | nothing — the base rate applied, so the card's best categories were ranked as its worst | *"10x on Dining, Travel, and International purchases and spends done on your birthday."* at *"1X = 1 Reward Point per Rs 200 \| 1 Reward Point = Rs 0.25"* | **Written.** Two new rules at 10 points per ₹200 = **1.25%** | **56 merchants 0.375% → 1.25%** — every restaurant, food-delivery app, cab, airline, hotel and travel site in the catalogue. Railways keeps its own 1X rate |
| the base rule's quote | *"3x on all eligible spends*"* — cut off at the asterisk | the footnote the asterisk points to: *"*3X Reward Points on all spend categories earn 3X Reward Points; except insurance, utilities, railways, FASTag recharges, dining, travel, international purchases, fuel, EMI transactions & cash withdrawals."* | **Corrected.** The quote now runs through the footnote, and every carved-out category is covered by its own rule or exclusion | The base rate stops being applied where the issuer excludes it |
| `base_reward_rate` | 0.0 — the whole card rendered 0.00% | *"3x on all eligible spends*"* + the unit line above | **Corrected** to 0.015, with the quote in `_provenance` | 187 merchants **0.00% → 0.375%** |
| the four 1X rules | typed as `multiplier`, so they depended on a base rate of zero | *"1x on Utility bill payments, Insurance premium payments, Railway spends and FASTag recharges"* | **Corrected** to 1 point per ₹200 = **0.125%** | 16 utility, insurance and railway merchants **0.00% → 0.125%** |
| the `[fuel]` 1X rule | a fabricated fifth row | the issuer's 1X list has four items, and *"Reward program not applicable on Fuel, Insurance, EMI transactions & Cash withdrawals."* | **Deleted**, and `other: fuel purchases` retyped to `category: fuel` | All 6 fuel pumps: **0.00% → "no reward pts · surcharge waiver applies"** — the card stops being recommended at a pump |
| portal rates 33% / 13% | from a superseded Dec-2025 letter | *"Total Reward Points Earn (A+B) On Bookings via Platform: Up to 25% Reward Points (On Hotel bookings) / Up to 15% Reward Points (On Flight bookings)"* at *"Reward Points can be redeemed at 1 Point = Rs 0.25."* | **Corrected to 6.25% and 3.75%.** Note these are **"Up to" ceilings stored as flat rates** | Shows on the card detail screen. **Never in the pick** — `portal_bonus` rules are skipped by the engine |
| `rp_value_standard` 0.25 | no source | *"1 Reward Point = Rs 0.25"*, and the travel T&C repeats the same 25 paise, so there is no travel uplift to argue about | **Now sourced** in `_provenance` | No rate change |
| the frozen names "**33%**…" and "**13%**…" | unchanged | — | **Blocked** | Detail screen prints 33% above 6.25%. Two of the seven — both on `portal_bonus` rules that never reach the pick |
| insurance | a 1X rule pays 0.125% | the 18-Jun-2026 Product Usage Guide lists insurance under 1X; the same document's redemption line says the reward programme is *"not applicable on … Insurance"* | **Flagged, not decided.** Two issuer sentences in one document | The 1X insurance rule ships. Raised here rather than guessed |
| redemption rows (app, net banking, 25p) | 2 rows | both cite CardInsider, although IDFC states the same 25 paise | **LEAD** on 2 of 2 rows | Nothing yet |

---

## 10. IDFC FIRST Hello Cashback Credit Card

| field | what we shipped | what the issuer says | verdict | what the user sees change |
|---|---|---|---|---|
| fuel | **we paid 1% at every pump** | *"All spends qualify for Cashback except – fuel, ATM Cash withdrawals, EMI, UPI spends done through other UPI apps (GPay, PhonePe, Paytm etc.)"* | **Corrected.** The `[fuel]` earn row deleted and a `category: fuel` exclusion added | All 6 fuel pumps **1.00% → "no reward pts · surcharge waiver applies"**. The separate 1% fuel *surcharge waiver* is a different mechanism and is not shown as if it stacked |
| 13 of 14 rules' provenance | none | the Hello Cashback T&C rate table, row by row: *"Online spends <= ₹10,000 \| 3%"*, *"Incremental Online spends (>₹10,000) \| 5%"*, *"UPI spends on IDFC FIRST Bank Mobile App \| 1%"*, *"Offline spends \| 1%"*, *"Essential spends (…) \| 1%"*, *"Travel bookings made via IDFC FIRST Bank Mobile App \| Bonus 1%"* | **Corrected.** 13 of 14 rules now cite the issuer | No rate change; the card is now checkable |
| the "Base reward rate" rule | 1%, no source, `confidence: low` | **the issuer publishes five earn rows and no catch-all base rate** | **Deliberately left unsourced.** Inventing a citation here is the exact failure this exercise exists to end. **This is why the card is grade C and not grade A** | Nothing. The gap is named instead of papered over |
| `rp_value_standard` 1.0 | no source | *"“Cashback” – shall mean the monetary reward credited to the Credit Card Account where 1 Cashback = ₹1"* | **Now sourced** | No rate change |
| the ₹1,000 online cap | written on both online rules | *"Maximum Cashback that can be earned in a Statement Cycle through spends in Online spends (Category 1) is ₹ 1,000"* — **one** cap across the pair | **Recorded, not fixed.** Two rules, two names, two ₹1,000 pots | A heavy online month can be told it has ₹2,000 of headroom when the bank allows ₹1,000 |
| the ₹1,500 all-category cycle cap | not written | *"Maximum Cashback that can be earned across all Eligible spends in a Statement Cycle is ₹1,500"* | **No field exists for it.** Named as a gap | The app will never tell a user they have hit the card's overall ceiling |
| "1% cashback on in-store card swipes" | `channel: offline` | *"Offline spends \| 1%"* | **Unreachable.** The app tests only `online`, `upi` or no channel on this kind of rule | Nothing — the card's base rate is the same 1%, so no user sees a wrong number. Needs an app change |
| "1% cashback on UPI payments made through the IDFC FIRST Bank App" | `channel: upi` | *"UPI spends on IDFC FIRST Bank Mobile App \| 1%"* | **Unreachable.** The card is not flagged RuPay-UPI. Not flipped, because flipping it makes the engine assume every purchase is paid by UPI | Nothing — same 1% as the base rate |
| "3% cashback on online spends **up to ₹10,000**" | ₹1,000 cashback cap | the ₹10,000 is a spend **tier**, not a ceiling | **Cannot be fixed without a rename.** The validator reads the frozen name as a spend cap | Nothing wrong on screen; one of the PR's 19 errors |
| redemption row (auto-adjust against balance, ₹1) | 1 row | cites CardInsider | **LEAD** | Nothing yet |
