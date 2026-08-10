# Reward rule schema — provenance and units

Status: **spec agreed, migration not yet run.** Written 2026-08-11 against `dev` @ seed 5.1.10
(376 cards / 1,205 reward rules). Every number below was measured, not recalled.

Companion to `SCHEMA_redemption.md`, which covers `redemption_rules` only.

---

## 1. Why this exists

**0 of 1,205 reward rules carry any source field.** There is nowhere in the file to record that a
number was checked against the issuer's own document, so a verification sweep run before this lands
produces numbers nobody can audit.

That is the whole problem. Everything else here is secondary.

---

## 2. The thing that makes this cheap

The app **already parses six reward-rule keys the seed has never written**:

| JSON key | parsed at | consumed? |
|---|---|---|
| `cap_kind` | `credit_card.dart:405` | **yes** — `usedAgainstCap` (:321) → `_checkCap` |
| `point_value` | `credit_card.dart:411` | **yes** — `rateForRule` (:601/606/634) |
| `point_currency` | `credit_card.dart:412` | yes, cap-label fallback only |
| `confidence` | `credit_card.dart:413` | no — lands on the model, read nowhere |
| `source_conflict` | `credit_card.dart:414` | no |
| `source_quote` | `credit_card.dart:415` | no |

So provenance does not need a new vocabulary. It needs the seed to start writing names the parser is
already waiting for. Two of the six change user-visible behaviour on the next data publish with **no
app release**.

`confidence` defaults to `'high'` when absent — so today all 1,205 unverified rules are modelled as
high-confidence. Writing `"low"` explicitly removes a silent false claim.

> ⚠️ Unverified: nothing in either repo establishes which `credit_card.dart` is in the *store*
> binary. There are no `v*` tags and `release.yml` has never run. `cap_kind` predates 1 Aug and is
> probably safe; the `point_value` read landed 10–11 Aug. Confirm against the shipped build before
> claiming "no app release needed" for anything.

---

## 3. Fields

### Provenance — on every reward rule

| field | type | required when | populated by |
|---|---|---|---|
| `_sources` | array of `"bank"` \| `"cardinsider"` | always | migration (see §5) |
| `confidence` | `"high"` \| `"medium"` \| `"low"` | always | migration → `"low"` |
| `source_url` | absolute https string | `_sources` contains `"bank"` | sweep |
| `source_quote` | **string** | `_sources` contains `"bank"` | sweep |
| `source_doc_type` | `"product_page"` \| `"mitc"` \| `"tnc"` \| `"fee_schedule"` \| `"aggregator"` | `source_url` present | sweep |
| `source_fetched_on` | `"YYYY-MM-DD"` | `source_url` present | sweep |
| `source_conflict` | boolean | optional | sweep |

Keys are **omitted**, never written as `null`, when absent. `RewardRule.fromJson({})` parses clean.

`_sources` reuses the existing repo convention (`SCHEMA_redemption.md:44`) verbatim rather than
inventing a third one. `source_quote` deliberately **supersedes** the redemption block's
`_source_quotes` object — see the trap in §6.

`"kfs"` is absent from `source_doc_type` on purpose: the KFS carries no reward rates on any issuer
tested. A rate citing one is a provably wrong citation.

### Units — where the file is genuinely ambiguous

| field | type | notes |
|---|---|---|
| `cap_unit` | `"points"` \| `"inr"` | ~230 of 379 caps derivable mechanically; the rest need an issuer document |
| `cap_kind` | `"reward"` \| `"spend"` | app default is `"reward"`; **changes ranking** |

`cap_unit` is *not* redundant with `reward_type`. On the 185 capped rules where the unit is
recoverable from prose, **29 (15.7%) disagree** with what the engine derives — mostly `cashback_pct`
rules whose cap is really points (6E Rewards, NeuCoins, ICICI RP). On those the engine subtracts
points from a rupee ceiling.

`"transactions"` is deliberately **not** a `cap_unit`. The 20 rules with `cap_period: "transaction"`
are expressing a *period*, not a unit.

### Card level

| field | type | notes |
|---|---|---|
| `issuer_key` | slug, closed enum of 20 | 34 issuer strings → 20 keys, mechanically derivable |

`issuer_key` is **added alongside** `card.issuer`, which is never rewritten — `issuer` is the
user-visible bank name (`credit_card.dart:839` hands it straight to `bankName`) and is frozen into
every user's already-logged transactions (`app_database.dart:172`).

---

## 4. Explicitly rejected

| proposed | why not |
|---|---|
| `reward_unit` enum | `reward_type` already *is* that enum — same three values, same semantics, driving the three arithmetics at `credit_card.dart:591/602/607` and mirrored in `kredme.py:390-402`. A second enum is a second place to be wrong. |
| `reward_unit_spend` "make required" | Already holds perfectly: populated on exactly the 284 `points_per_spend` rules, 284/284, zero violations. Encoded as a gate rule instead — 0 data writes. |
| `cap_period` "closed enum" | Field exists on all 1,205 and is already closed in practice. Encoded as a gate rule. The real complaint — that `month` and `cycle` differ — is an **engine** defect (`recommendation_engine.dart` collapses everything except `quarter`/`year` into the calendar month), not a schema gap. |
| `rounding_rule` | `reward_type` already determines it: block flooring is applied by exactly one condition, `rewardType == 'points_per_spend'` at `credit_card.dart:636`. Zero of 1,205 rules state fractional accrual. |
| `eligible_mccs` | No model field, no read site, no engine consumer. MCC data already lives in `merchants.json`. Fillable only by a sweep, and needs app + engine changes before it does anything. |
| `effective_date` / `expiry_date` | **Not a schema change** — both keys already exist on all 1,205, null on all 1,205. Populating them today is net-negative: nothing reads them, so a populated `expiry_date` implies expiry handling that does not exist. App work first. |
| card-level `point_values[]` array | **Dangerous.** `_numOf` returns null for a List, and `credit_card.dart:840` has `?? 0.25` — so replacing the scalar does not throw, it silently makes all 376 cards claim ₹0.25/point. `kredme.py:385` defaults to the same 0.25, so the gate prints green while every rupee figure is invented. Channel-specific values belong on the rule, via `point_value`. |

---

## 5. Migration order

Each step must leave `validate --target working` at 0 errors and `test_pipeline.py` green.

0. **Rule-integrity gate** — ✅ *done 2026-08-11.* 5 checks, 0 data writes. See §7.
1. `issuer_key` on 376 cards + `tools/issuer_domains.json`.
2. Issuer-domain allowlist in `kredme.py` — **ship it while the set is empty.** At N=0 there is
   nothing to grandfather. Shipped after the sweep, it would rubber-stamp whatever the sweep produced.
3. `_sources` + `confidence` on 1,205 rules.
4. `cap_kind` on the identified spend-cap rules.
5. `cap_unit` where mechanically derivable.
6. The sweep — fills `source_url` / `source_quote` / `source_doc_type` / `source_fetched_on`.

**Do not run step 3 as a blanket write.** `["cardinsider"]` is factually false on ~229 rules: nine
commits since the CardInsider baseline (`b50211b`) rewrote reward numbers, three of them issuer-sourced
by their own commit message (`92d85d3` AU Altura Plus, `3e06e06` AU Vetta, `0aa738b` IndianOil Kotak).
Derive per rule from git history; rules touched post-baseline get the real source or an omitted key,
not a lie.

**Blocked on an app change:** the sweep must not run before `sanePointValue`'s ceiling is raised.
It collapses anything above 1.5 to 0.25 in **both** the app (`credit_card.dart`) and the gate
(`kredme.py:115-117`), so an issuer-verified ₹2.00/point would be silently discarded and validate
would print green.

---

## 6. Traps specific to this schema

**★ `source_quote` must be a STRING, never an object.** The app does
`json['source_quote'] as String?` — a hard cast. A Map there throws `TypeError`, which propagates out
of `RewardRule.fromJson` and is swallowed by the per-card `catch` at `utils.dart:222`. **The card
vanishes from the catalogue with no error surface.** If a rule ever needs two quotes, add a separate
`_source_quotes` overflow key; never widen this one's type.

**★ Pin `indent=1` in every migration script.** `cards.json` is serialised at indent 1. The repo's
own `write_json` (`kredme.py:171-175`) uses `indent=2` — reaching for it reformats the whole file:
1,718,524 → 1,951,253 bytes, +232,729 of pure whitespace, before adding a single field. A
whitespace-only reformat is a failed step, not a cosmetic detail.

**★ Put the allowlist in `tools/`, not `seed/`.** `SEED_FILES = ("cards.json", "merchants.json")`
(`kredme.py:69`) — promote copies only those two. A `seed/issuer_domains.json` would never reach
`main`, and `validate --target prod` would find it missing. If the check skips on a missing file, the
allowlist is silently off on every prod validation for ever.

**★ Model issuer → **list** of domains.** HDFC uses both `hdfc.bank.in` (26 URLs) and `hdfcbank.com`
(25), both live. A single-domain allowlist rejects half its cards. Co-brands legitimately live on a
third party: Scapia on `scapia.cards`, OneCard on `getonecard.app`. Without an exception concept the
gate generates false positives, they get waived, and waivers become the new grandfathering.

**★ A green allowlist is not evidence a rate is right.** It proves the domain — not the number, not
the page, not that the page still says what it said when scraped.

---

## 7. What the gate now enforces (shipped 2026-08-11)

`validate_rule_integrity` in `kredme.py`. Five checks, zero data writes.

**Ratcheted** — real debt exists, frozen in `tools/rate_baseline.json`, anything new fails:

| check | live count | why it matters |
|---|---|---|
| `non_numeric_caps` | 19 rules / 14 keys | `cap_amount` is a string or object, so `_numOf` returns null. **A null cap is no cap** — the rule pays its accelerated rate for ever. Affects YES Elite (12,000 RP/cycle), IndianOil HDFC, ICICI Parakram, Kotak Essentia, RBL IndianOil Xtra and 4 more. |
| `duplicate_rule_names` | 23 rules / 5 keys | Two rules sharing a name on one card share one cap bucket, and one overwrites the other in the DB. `hdfc_bank_millennia` has 11 rules on 2 names. |
| `cap_without_period` | 3 | `_checkCap` returns null unless both are set, so the cap is never enforced. |

**Unwaivable** — clean today, no baseline key, same reasoning as `over_hard_ceiling`:

- every `points_per_spend` rule carries a `reward_unit_spend` (284/284)
- every `cap_period` is one of `month` / `cycle` / `transaction` / `quarter` / `year` / `day`

> **Do not "fix" `duplicate_rule_names` by renaming a rule.** `rule_name` is the cap-usage key
> (`app_database.dart:238`), so a rename orphans every user's spend history and resets cap progress
> mid-cycle. Fix collisions in the app (shared cap groups), not here.

**Known incoming:** branch `data/kotak-iocl-caps` adds a **6th** collision on
`kotak_mahindra_bank_indianoil_kotak` — one shared 800-point cap split across two category rows
(`dining` + `grocery`) carrying the same name. This gate will flag it as NEW, and that is correct:
the DB keys on `${cardId}|${ruleName}`, so one of the two categories loses its rule entirely.
Note the same name is also what keeps the 800-point cap *shared* — splitting the names would hand
users 800 + 800. Resolve it in the app, then baseline the row with that reasoning recorded.
