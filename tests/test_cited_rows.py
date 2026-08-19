#!/usr/bin/env python3
"""
The citation invariants, asserted against the real seed/cards.json.

    python3 tests/test_cited_rows.py
    python3 tests/test_cited_rows.py -v

stdlib unittest only, no network, no pip installs, no seed/ writes.

Every rule here is one a 19-Aug provenance pass broke while reporting success.
They are asserted against the shipped file rather than a fixture on purpose: a
fixture proves the writer can behave, and these are about what is actually on
disk about to go to users.

  1. a fetch date is a fact about a fetch, so it comes from the fetch record and
     never from the clock at write time. 29 card-level entries were stamped
     2026-08-19 for bytes sources.json says were read on 2026-08-17.
  2. a source_quote must be findable on the issuer's page. Scraper residue and
     two non-adjacent sentences joined by an ellipsis both make it unfindable.
  3. a row-level source_quote certifies the WHOLE row, so a row that reads
     confidence 'high' may not carry a number its own sentence does not state.
     19 such values shipped, 11 of them in blocks no check inspected.
  4. an exclusion row that hits nothing another row on the same card does not
     already hit buys no protection, and costs the card rank: engine tie-break 3
     (recommendation_engine.dart:288-291) counts exclusion ROWS, not rows that fire.
  5. rp_value_standard drives ranking, so it may only be evidenced from the
     issuer's own redemption terms — never a travel-portal rate, never an
     accrual sentence whose parenthesis happens to give a rupee equivalence.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import diff  # noqa: E402
from pipeline import taxonomy as T  # noqa: E402

CARDS = json.loads((REPO / "seed" / "cards.json").read_text())
MERCHANTS = json.loads((REPO / "seed" / "merchants.json").read_text())
SOURCES = json.loads(
    (REPO / "pipeline" / "state" / "sources.json").read_text()).get("sources", {})
TAX = T.build_taxonomy(MERCHANTS, None)

BY = {c["card"]["id"]: c for c in CARDS}

# The ten cards the 17-Aug paid pass read end to end. Everything below holds for
# all 383 where it can; where an older, unrelated defect would fail a whole-file
# assertion it is named here rather than quietly excluded.
TEN = (
    "hdfc_bank_indianoil_hdfc_bank",
    "axis_bank_indianoil_axis_bank_premium",
    "hdfc_bank_moneyback",
    "axis_bank_atlas",
    "axis_bank_flipkart_axis_bank_super_elite",
    "hdfc_bank_tata_neu_infinity_hdfc_bank",
    "hdfc_bank_tata_neu_plus_hdfc_bank",
    "kotak_mahindra_bank_indianoil_kotak",
    "idfc_first_bank_millennia",
    "idfc_first_bank_hello_cashback",
)

ROW_BLOCKS = ("reward_rules", "fuel_surcharge_rules", "milestone_rules",
              "exclusion_rules", "redemption_rules")

# Numbers a row claims, per block. cap_amount is deliberately included: the cap
# is the number that decides where a user's reward stops.
NUMERIC = {
    "reward_rules": ("reward_rate", "reward_unit_spend", "cap_amount",
                     "min_txn_amount"),
    "fuel_surcharge_rules": ("waiver_pct", "min_txn_amount", "max_txn_amount",
                             "monthly_cap"),
    "milestone_rules": ("spend_target", "bonus_value"),
    "redemption_rules": ("point_value_inr", "min_points"),
    "exclusion_rules": (),
}

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def rows_of(entry, block):
    for i, r in enumerate(entry.get(block) or []):
        if isinstance(r, dict):
            yield i, r


def numbers_in(text):
    out = set()
    for m in _NUM.finditer(str(text or "")):
        try:
            out.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    return out


def stated(value, said):
    """Is this number in that sentence, however the issuer chose to write it?"""
    forms = {value}
    if 0 < value < 1:
        forms.add(value * 100)          # 0.05 written as "5%"
    return any(any(abs(f - x) <= max(1e-9, abs(f) * 0.005) for x in said)
               for f in forms)


# ---------------------------------------------------------------------------
class TestAFetchDateComesFromTheFetch(unittest.TestCase):

    def test_no_citation_claims_to_be_newer_than_the_bytes_it_read(self):
        bad = []
        for cid, entry in BY.items():
            known = (SOURCES.get(cid) or {}).get("fetched_at")
            if not known:
                continue
            fetched = str(known)[:10]
            claims = [p.get("source_fetched_on") for p in entry.get("_provenance", [])]
            for block in ROW_BLOCKS:
                claims += [r.get("source_fetched_on") for _i, r in rows_of(entry, block)]
            for claim in claims:
                if isinstance(claim, str) and claim > fetched:
                    bad.append("%s: cites %s, sources.json says %s"
                               % (cid, claim, fetched))
        self.assertEqual(bad, [], "\n".join(bad))


class TestEveryQuoteIsFindable(unittest.TestCase):

    # Three rows on one card carry an ellipsis that predates this work and is not
    # a splice this pipeline made. Named, not hidden: the assertion below is a
    # ratchet, so a fourth cannot appear without failing.
    KNOWN_OLDER = {("unity_small_finance_bank_roarbank", "reward_rules", 0),
                   ("unity_small_finance_bank_roarbank", "reward_rules", 1),
                   ("unity_small_finance_bank_roarbank", "reward_rules", 2)}

    def _offenders(self):
        out = []
        for cid, entry in BY.items():
            for block in ROW_BLOCKS:
                for i, r in rows_of(entry, block):
                    q = r.get("source_quote")
                    if isinstance(q, str) and q and not diff._is_verbatim(q):
                        out.append((cid, block, i))
            for p in entry.get("_provenance", []):
                q = p.get("source_quote")
                if isinstance(q, str) and q and not diff._is_verbatim(q):
                    out.append((cid, "_provenance", p.get("field")))
        return out

    def test_the_ten_carry_no_residue_and_no_spliced_sentence(self):
        bad = [o for o in self._offenders() if o[0] in TEN]
        self.assertEqual(bad, [], bad)

    def test_the_catalogue_gains_no_new_ones(self):
        bad = {o for o in self._offenders()} - self.KNOWN_OLDER
        self.assertEqual(bad, set(), bad)


class TestAHighConfidenceRowIsTrueOfItsWholeSelf(unittest.TestCase):

    def test_no_number_on_a_high_row_is_absent_from_that_rows_quote(self):
        bad = []
        for cid in TEN:
            entry = BY[cid]
            for block, fields in NUMERIC.items():
                for i, r in rows_of(entry, block):
                    q = r.get("source_quote")
                    if not q or str(r.get("confidence") or "high").lower() != "high":
                        continue
                    said = numbers_in(q)
                    for f in fields:
                        v = r.get(f)
                        if isinstance(v, str):
                            bad.append("%s %s[%d].%s is prose, not a number: %r"
                                       % (cid, block, i, f, v))
                            continue
                        if not isinstance(v, (int, float)) or isinstance(v, bool) or not v:
                            continue
                        if not stated(float(v), said):
                            bad.append("%s %s[%d].%s = %g is not in its own quote"
                                       % (cid, block, i, f, float(v)))
        self.assertEqual(bad, [], "\n".join(bad))


class TestNoExclusionRowIsDeadWeight(unittest.TestCase):

    def test_every_exclusion_row_excludes_something_no_other_row_does(self):
        bad = []
        for cid in TEN:
            rows = list(rows_of(BY[cid], "exclusion_rules"))
            hits = {i: TAX.merchants_hit(r.get("exclusion_type"),
                                         r.get("exclusion_value")) for i, r in rows}
            for i, r in rows:
                mine = hits[i]
                if not mine:
                    continue          # inert type; a different defect, not this one
                others = set().union(*[h for j, h in hits.items() if j != i]) \
                    if len(hits) > 1 else set()
                if mine <= others:
                    bad.append("%s exclusion_rules[%d] %s:%s excludes nothing "
                               "another row does not"
                               % (cid, i, r.get("exclusion_type"),
                                  r.get("exclusion_value")))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_the_two_tata_neu_cards_still_pay_at_steam_playstation_and_xbox(self):
        # The user-visible half of the mcc:5816 defect, stated as the thing a
        # person would notice rather than as a row that should not exist.
        gaming = {m["merchant_name"] for m in MERCHANTS["merchants"]
                  if m.get("mcc_primary") == "5816"}
        self.assertTrue(gaming, "merchants.json no longer tags any merchant 5816")
        for cid in ("hdfc_bank_tata_neu_infinity_hdfc_bank",
                    "hdfc_bank_tata_neu_plus_hdfc_bank"):
            excluded = set()
            for _i, r in rows_of(BY[cid], "exclusion_rules"):
                excluded |= TAX.merchants_hit(r.get("exclusion_type"),
                                              r.get("exclusion_value"))
            self.assertEqual(gaming & excluded, set(),
                             "%s zeroes %s" % (cid, sorted(gaming & excluded)))


class TestPointValueComesFromRedemptionTerms(unittest.TestCase):

    PORTAL = ("smartbuy", "portal", "flight", "hotel", "airmile", "voucher",
              "transfer")
    REDEEMS = ("redeem", "redemption", "cashback", "statement credit")

    # One card outside the ten cites an ACCRUAL sentence for its point value —
    # "a) 5 reward* points per INR 150 spent (1 reward point = INR 1)". It is the
    # same defect as the Axis IndianOil Premium one and it arrived in an earlier
    # commit, so it is named here rather than fixed in a change that must touch
    # only the ten. The assertion is a ratchet: a second one fails this test.
    KNOWN_OLDER = {"sc_ultimate"}

    def _offenders(self):
        out = {}
        for cid, entry in BY.items():
            for p in entry.get("_provenance", []):
                if p.get("path") != "card.rp_value_standard":
                    continue
                q = str(p.get("source_quote") or "").lower()
                if any(w in q for w in self.PORTAL):
                    out[cid] = "cites a conditional channel: %r" % q[:90]
                elif not any(w in q for w in self.REDEEMS):
                    out[cid] = ("cites a sentence that never mentions redeeming: %r"
                                % q[:90])
        return out

    def test_the_ten_cite_only_redemption_terms(self):
        bad = {c: w for c, w in self._offenders().items() if c in TEN}
        self.assertEqual(bad, {}, bad)

    def test_the_catalogue_gains_no_new_ones(self):
        bad = {c: w for c, w in self._offenders().items()
               if c not in self.KNOWN_OLDER}
        self.assertEqual(bad, {}, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
