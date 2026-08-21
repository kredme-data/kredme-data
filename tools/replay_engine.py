#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Replay the app's recommendation engine over the whole catalogue, before vs after.

WHY THIS EXISTS
    A card-data change is safe or unsafe only in terms of the number a user is
    shown. `validate_cards.py` proves the file is internally consistent; it does
    not compute a single rate. This script does: it is a line-for-line port of

        KredMe-main/lib/core/engine/recommendation_engine.dart   (rank / _evaluate)
        KredMe-main/lib/shared/models/credit_card.dart           (rateForRule / baseReward)

    and it replays every card x every merchant against two copies of
    seed/cards.json so a diff can be stated as "these rates moved, these did not".

WHAT IT DELIBERATELY DOES NOT MODEL
    Spend data is empty — the reading a user gets before they have logged any
    spend this period. Caps and spend thresholds therefore never bind, which is
    the GENEROUS end of the range: a real rate can only be this or lower.
    The `isFuel` surcharge-waiver tie-break is not ported; it never changes a
    percentage, only the order of two cards that already tie.

USAGE
    python3 tools/replay_engine.py OLD_CARDS.json [NEW_CARDS.json]

    With one argument it replays the working tree against that file.
"""

import json, math, datetime

FUEL_MCCS = {'5541', '5542', '5172'}

def num(v):
    if v is None: return None
    if isinstance(v, bool): return None
    if isinstance(v, (int, float)): return float(v)
    try: return float(v)
    except Exception: return None

def sane_pv(v):
    if v is None or (isinstance(v, float) and math.isnan(v)) or v <= 0 or v > 1.5:
        return 0.25
    return v

def infer_user_pref_gate(explicit, rule_name):
    if explicit is not None: return explicit
    n = (rule_name or '').lower()
    negated = any(s in n for s in ('non-prime', 'non prime', 'without prime', 'not a prime'))
    if not negated and any(s in n for s in ('prime member', 'for prime', '(prime)')):
        return {'match_all': [{'field': 'user.is_prime_member', 'op': 'eq', 'value': True}]}
    if 'swiggy one' in n or 'with swiggy' in n:
        return {'match_all': [{'field': 'user.has_swiggy_one', 'op': 'eq', 'value': True}]}
    if 'amazon pay balance' in n or 'amazon pay wallet' in n:
        return {'match_all': [{'field': 'user.has_amazon_pay_balance', 'op': 'eq', 'value': True}]}
    return None

class Rule:
    __slots__ = ('name','type','category_id','channel','merchant_ref','portal_name','conditions',
                 'reward_type','rate','unit','cap_amount','cap_period','cap_kind','min_txn',
                 'thr_min','thr_max','thr_period','priority','point_value')
    def __init__(self, j):
        self.name = j.get('rule_name') or ''
        self.type = j.get('rule_type') or 'base_rate'
        self.category_id = j.get('category_id')          # slug string (app resolves to int id)
        self.channel = j.get('channel')
        self.merchant_ref = j.get('merchant_ref')
        self.portal_name = j.get('portal_name')
        self.conditions = infer_user_pref_gate(j.get('conditions_json'), self.name)
        self.reward_type = j.get('reward_type') or 'points_per_spend'
        self.rate = num(j.get('reward_rate')) or 0.0
        u = num(j.get('reward_unit_spend'));  self.unit = 100.0 if u is None else u
        self.cap_amount = num(j.get('cap_amount'))
        self.cap_period = j.get('cap_period')
        self.cap_kind = j.get('cap_kind') or 'reward'
        self.min_txn = num(j.get('min_txn_amount'))
        self.thr_min = num(j.get('spend_threshold_min'))
        self.thr_max = num(j.get('spend_threshold_max'))
        self.thr_period = j.get('threshold_period')
        self.priority = int(j.get('priority') or 0)
        self.point_value = num(j.get('point_value'))

class Card:
    __slots__ = ('id','name','bank','base_rate','rp_std','forex','annual_fee','has_rupay_upi',
                 'rules','exclusions','is_active')
    def __init__(self, e):
        c = e['card']
        self.id = c['id']; self.name = c['card_name']; self.bank = c.get('issuer','')
        self.base_rate = num(c.get('base_reward_rate')) or 0.0
        rp = num(c.get('rp_value_standard'))
        self.rp_std = 0.25 if rp is None else rp          # OTA loader: `?? 0.25`
        self.forex = num(c.get('forex_markup_pct'));  self.forex = 3.5 if self.forex is None else self.forex
        self.annual_fee = num(c.get('annual_fee')) or 0.0
        self.has_rupay_upi = (c.get('has_rupay_upi') or 0) == 1
        self.is_active = c.get('is_active', 1)
        self.rules = [Rule(r) for r in e.get('reward_rules', [])]
        self.exclusions = e.get('exclusion_rules', [])

    @property
    def base_reward(self):
        return self.base_rate * sane_pv(self.rp_std) * 100

    def rate_for_rule(self, r):
        if r.reward_type == 'cashback_pct':
            return r.rate * 100
        if r.reward_type == 'multiplier':
            pv = sane_pv(r.point_value if r.point_value is not None else self.rp_std)
            return r.rate * self.base_rate * pv * 100
        if r.reward_type == 'points_per_spend':
            if r.unit <= 0: return self.base_reward
            pv = sane_pv(r.point_value if r.point_value is not None else self.rp_std)
            return (r.rate / r.unit) * pv * 100
        return self.base_reward

class Merchant:
    __slots__ = ('id','name','category_name','mcc','is_online','is_international')
    def __init__(self, j):
        self.id = j['merchant_name']; self.name = j['display_name']
        self.category_name = j.get('category_id') or ''
        self.mcc = j.get('mcc_primary')
        self.is_online = (j.get('is_online') or 0) == 1
        self.is_international = (j.get('metadata') or {}).get('is_international') is True

class Engine:
    def __init__(self, cards, parent_of):
        self.parent_of = parent_of
        self.merchant_rules = {}; self.category_rules = {}; self.base_rules = {}
        self.exclusions = {}
        for card in cards:
            if card.exclusions: self.exclusions[card.id] = card.exclusions
            for r in card.rules:
                if r.type in ('portal_bonus', 'milestone'): continue
                if r.type == 'merchant_specific':
                    if r.merchant_ref:
                        self.merchant_rules.setdefault(r.merchant_ref, {}).setdefault(card.id, []).append(r)
                elif r.type == 'conditional':
                    if r.merchant_ref:
                        self.merchant_rules.setdefault(r.merchant_ref, {}).setdefault(card.id, []).append(r)
                    elif r.category_id is not None:
                        self.category_rules.setdefault(r.category_id, {}).setdefault(card.id, []).append(r)
                    else:
                        self.base_rules.setdefault(card.id, []).append(r)
                elif r.type == 'category_bonus':
                    if r.category_id is None:
                        if r.conditions is not None:
                            self.base_rules.setdefault(card.id, []).append(r)
                        # else dropped, as the engine does
                    else:
                        self.category_rules.setdefault(r.category_id, {}).setdefault(card.id, []).append(r)
                elif r.type == 'threshold_tier':
                    if r.category_id is not None:
                        self.category_rules.setdefault(r.category_id, {}).setdefault(card.id, []).append(r)
                    else:
                        self.base_rules.setdefault(card.id, []).append(r)
                elif r.type in ('base_rate', 'channel_specific', 'promotional'):
                    self.base_rules.setdefault(card.id, []).append(r)
        for m in self.merchant_rules.values():
            for l in m.values(): self._sort(l)
        for m in self.category_rules.values():
            for l in m.values(): self._sort(l)
        for l in self.base_rules.values(): self._sort(l)

    @staticmethod
    def _sort(l):
        l.sort(key=lambda r: (-r.priority, -r.rate))

    def _channel_matches(self, r, m, card):
        ch = r.channel
        if ch is None: return True
        if ch == 'online': return m.is_online
        if ch == 'offline': return not m.is_online
        if ch == 'upi': return card.has_rupay_upi
        if ch == 'portal': return False
        if ch == 'app': return m.is_online
        return False

    def _passes_conditions(self, r, m):
        c = r.conditions
        if c is None: return True
        ma = c.get('match_all')
        if ma is None: return True
        now = datetime.datetime.now()
        for cond in ma:
            f, op, v = cond.get('field'), cond.get('op'), cond.get('value')
            if f == 'txn.category':
                if op == 'in' and m.category_name not in v: return False
                if op == 'eq' and m.category_name != v: return False
            elif f == 'txn.merchant':
                if op == 'in' and m.id not in v: return False
                if op == 'eq' and m.id != v: return False
            elif f == 'txn.is_online':
                if op == 'eq' and isinstance(v, bool) and m.is_online != v: return False
            elif f == 'calendar.quarter':
                if not _op(op, (now.month - 1)//3 + 1, v): return False
            elif f == 'calendar.month':
                if not _op(op, now.month, v): return False
            elif f == 'calendar.day_of_week':
                dow = now.isoweekday()
                if op == 'in' and isinstance(v, list):
                    ok = False
                    names = {'monday':1,'tuesday':2,'wednesday':3,'thursday':4,'friday':5,'saturday':6,'sunday':7}
                    for d in v:
                        if isinstance(d, int) and d == dow: ok = True; break
                        if isinstance(d, str):
                            dl = d.lower()
                            if dl == 'weekend' and dow in (6,7): ok = True; break
                            if dl == 'weekday' and dow <= 5: ok = True; break
                            if names.get(dl) == dow: ok = True; break
                    if not ok: return False
                else:
                    if not _op(op, dow, v): return False
            elif f in ('user.is_prime_member','user.has_amazon_pay_balance','user.has_swiggy_one'):
                if op == 'eq' and isinstance(v, bool) and v is not False: return False   # UserPrefs all default false
            elif f == 'user.quarterly_spend':
                if not _op(op, 0.0, float(v)): return False
            elif f == 'user.selected_categories':
                return False
        return True

    @staticmethod
    def _passes_threshold(r):
        if r.thr_min is None and r.thr_max is None: return True
        total = 0.0                                   # no logged spend
        if r.thr_min is not None and total < r.thr_min: return False
        if r.thr_max is not None and total >= r.thr_max: return False
        return True

    @staticmethod
    def _check_cap(r):
        if r.cap_amount is None or r.cap_period is None: return None
        used = 0.0                                    # no logged spend
        rem = r.cap_amount - used
        return 0.0 if rem < 0 else rem

    def _candidate_category_rules(self, cat, card_id):
        out = []; cur = cat
        while cur is not None:
            out.extend(self.category_rules.get(cur, {}).get(card_id, []))
            cur = self.parent_of.get(cur)
        if len(out) > 1: self._sort(out)
        return out

    def _is_excluded(self, excl, m):
        for e in excl:
            t = e.get('exclusion_type')
            if t == 'mcc' and m.mcc is not None and m.mcc == e.get('exclusion_value'): return True
            if t == 'category' and m.category_name == e.get('exclusion_value'): return True
        return False

    def evaluate(self, card, m):
        is_fuel = m.category_name == 'fuel' or (m.mcc is not None and m.mcc in FUEL_MCCS)
        excl = self.exclusions.get(card.id, [])
        if self._is_excluded(excl, m):
            return dict(card=card, pct=0.0, label='excluded', rule_type='excluded',
                        excluded=True, rule_name=None, is_fuel=is_fuel)
        def build(pct, label, rtype, rname):
            p = pct
            if m.is_international:
                p -= card.forex
            return dict(card=card, pct=p, label=label, rule_type=rtype,
                        excluded=False, rule_name=rname, is_fuel=is_fuel)
        for r in self.merchant_rules.get(m.id, {}).get(card.id, []):
            if not self._channel_matches(r, m, card): continue
            if not self._passes_conditions(r, m): continue
            if not self._passes_threshold(r): continue
            rem = self._check_cap(r)
            if rem is not None and rem <= 0: continue
            return build(card.rate_for_rule(r), 'Merchant rate', r.type, r.name)
        if m.category_name:
            for r in self._candidate_category_rules(m.category_name, card.id):
                if not self._channel_matches(r, m, card): continue
                if not self._passes_conditions(r, m): continue
                if not self._passes_threshold(r): continue
                rem = self._check_cap(r)
                if rem is not None and rem <= 0: continue
                return build(card.rate_for_rule(r), 'Category rate', r.type, r.name)
        base = self.base_rules.get(card.id, [])
        if m.is_online:
            for r in base:
                if r.channel != 'online': continue
                if not self._passes_conditions(r, m): continue
                if not self._passes_threshold(r): continue
                rem = self._check_cap(r)
                if rem is not None and rem <= 0: continue
                return build(card.rate_for_rule(r), 'Online rate', r.type, r.name)
        if card.has_rupay_upi:
            for r in base:
                if r.channel != 'upi': continue
                if not self._passes_conditions(r, m): continue
                if not self._passes_threshold(r): continue
                rem = self._check_cap(r)
                if rem is not None and rem <= 0: continue
                return build(card.rate_for_rule(r), 'Via UPI', r.type, r.name)
        for r in base:
            if r.channel is not None: continue
            if not self._passes_conditions(r, m): continue
            if not self._passes_threshold(r): continue
            rem = self._check_cap(r)
            if rem is not None and rem <= 0: continue
            lbl = 'Base rate' if r.type == 'base_rate' else 'Partner rate'
            return build(card.rate_for_rule(r), lbl, r.type, r.name)
        return build(card.base_reward, 'Base rate', 'base_rate', None)

    def rank(self, m, cards):
        res = [self.evaluate(c, m) for c in cards]
        def key(x):
            e = self.exclusions.get(x['card'].id, [])
            return (1 if x['excluded'] else 0, -x['pct'], x['card'].annual_fee, len(e))
        res.sort(key=key)
        return res

def _op(op, actual, value):
    if op == 'eq': return actual == value
    if op == 'neq': return actual != value
    if op == 'gt': return actual > value
    if op == 'gte': return actual >= value
    if op == 'lt': return actual < value
    if op == 'lte': return actual <= value
    if op == 'in': return isinstance(value, list) and actual in value
    return True

def load(cards_path, merchants_path):
    cards = [Card(e) for e in json.load(open(cards_path))]
    md = json.load(open(merchants_path))
    parent_of = {c['id']: c.get('parent_id') for c in md['categories'] if c.get('parent_id')}
    merchants = [Merchant(m) for m in md['merchants']]
    return cards, merchants, parent_of


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def _pairs(path, merchants_path):
    cards, merchants, parent = load(path, merchants_path)
    eng = Engine(cards, parent)
    out = {}
    for m in merchants:
        for c in cards:
            r = eng.evaluate(c, m)
            out[(c.id, m.id)] = (round(r['pct'], 6), r['excluded'])
    return cards, merchants, eng, out


def main(argv):
    import os, collections
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mer = os.path.join(here, 'seed', 'merchants.json')
    old = argv[1]
    new = argv[2] if len(argv) > 2 else os.path.join(here, 'seed', 'cards.json')

    cb, mb, eb, before = _pairs(old, mer)
    ca, ma, ea, after = _pairs(new, mer)
    A = {c.id: c for c in ca}
    B = {c.id: c for c in cb}

    print("cards %d -> %d   merchants %d   card-merchant pairs %d"
          % (len(cb), len(ca), len(ma), len(after)))
    if set(A) != set(B):
        print("!! card id set changed:", sorted(set(A) ^ set(B)))

    ups = collections.defaultdict(list)
    downs = collections.defaultdict(list)
    for k, bv in before.items():
        av = after.get(k)
        if av is None:
            continue
        if av[0] > bv[0] + 1e-9:
            ups[k[0]].append((k[1], bv[0], av[0]))
        elif av[0] < bv[0] - 1e-9:
            downs[k[0]].append((k[1], bv[0], av[0]))

    print()
    print("cards with a rate INCREASE: %d  (%d card-merchant pairs)"
          % (len(ups), sum(len(v) for v in ups.values())))
    print("cards with a rate DECREASE: %d  (%d card-merchant pairs)"
          % (len(downs), sum(len(v) for v in downs.values())))

    print()
    print("%-52s %14s %14s" % ("card", "base % b->a", "best % b->a"))
    for cid in sorted(set(ups) | set(downs)):
        bb = max(before[(cid, m.id)][0] for m in mb)
        aa = max(after[(cid, m.id)][0] for m in ma)
        print("%-52s %6.3f -> %6.3f %6.3f -> %6.3f"
              % (cid[:52], B[cid].base_reward, A[cid].base_reward, bb, aa))

    print()
    print("pick screen (top 3) — who appears that did not, and who is pushed out")
    ent = collections.Counter(); disp = collections.Counter()
    for m in ma:
        t3b = [x['card'].id for x in eb.rank(m, cb)[:3]]
        t3a = [x['card'].id for x in ea.rank(m, ca)[:3]]
        for cid in set(t3a) - set(t3b): ent[cid] += 1
        for cid in set(t3b) - set(t3a): disp[cid] += 1
    for cid, n in ent.most_common():
        print("   IN   %-52s on %3d merchants" % (cid[:52], n))
    for cid, n in disp.most_common():
        print("   OUT  %-52s on %3d merchants" % (cid[:52], n))
    return 0


if __name__ == '__main__':
    import sys as _s
    _s.exit(main(_s.argv))
