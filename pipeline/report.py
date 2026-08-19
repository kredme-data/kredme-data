#!/usr/bin/env python3
"""
report.py — the weekly numbers a non-technical founder can act on.

Usage:
    python3 -m pipeline.report                      print this week's report
    python3 -m pipeline.report --write              ... and append it to the history
    python3 -m pipeline.report --json               the metrics row only, for a job step
    python3 -m pipeline.report --trend sourced_rules   one metric's sparkline

Re-running the offline validator every week adds nothing: it is deterministic
over bytes that did not change, so it returns the same verdict forever. What a
weekly job owes the founder instead is a short list of integers that MOVE, each
with a direction that is unambiguously good, so "is the catalogue getting better
or worse" is answerable in ten seconds.

The metric that matters most is `sourced_rules`. Every other number here asks
whether the data is internally consistent; that one asks whether it is TRUE.
0 of 1,205 rules were issuer-verified at handover, so it starts near the floor
and every point of it is real progress.

This module only READS card data. It never patches a card, never touches
`rule_name`, and never writes a computed percentage back into the seed — the
percentages below exist to be counted, not stored.

Stdlib only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from typing import Any

from pipeline import config as C
from pipeline import provenance as P
from pipeline import state as S

# ---------------------------------------------------------------------------
# App-behaviour constants
#
# Mirrored from tools/kredme.py (APP_POINT_VALUE_*), which in turn mirrors
# CreditCardData.sanePointValue and the `?? 0.25` fallback in fromOtaJson. They
# are duplicated rather than imported because tools/ is a script directory, not
# an importable package — but if the app changes either number, both copies have
# to move or this report starts measuring a rate no user is shown.
# ---------------------------------------------------------------------------
APP_POINT_VALUE_DEFAULT = 0.25
APP_POINT_VALUE_MAX = 1.5

# state.record_source's vocabulary. Anything else (fetch_failed, no_url,
# not_issuer_domain) means we still have no issuer document for that card.
RESOLVED_STATUSES = frozenset({"ok", "unchanged"})

# ---------------------------------------------------------------------------
# Definition version
#
# Bumped whenever a metric stops meaning what it meant. Two rows measured by
# different rules are not comparable, and a trend table that subtracts them
# anyway prints a fiction — here, a definition tightening would have rendered as
# "34 reward rules LOST the citation they had", which is the single most alarming
# sentence this report can produce and it would have been false.
#
#   1  sourced_rules counted source_url, source_quote OR any non-empty _sources.
#      That included 34 rules whose only provenance is a quote with no document,
#      and seven citing the placeholder string "bank".
#   2  sourced_rules counts only rules naming a document anyone can open
#      (pipeline.provenance.row_document_urls) — the same definition the
#      validator's L8 layer and `refresh --unsourced-only` use. 61 -> 27.
#
# The guard is small on purpose: it suppresses the COMPARISON, never the
# measurement. This run's number is always reported truthfully.
METRIC_DEFINITION_VERSION = 2

# Metrics whose meaning changed at the version above. Only these lose their
# week-over-week comparison when the version moves; a cap count is a cap count.
DEFINITION_SENSITIVE = ("sourced_rules", "sourced_rules_pct")

# Row keys that are bookkeeping, not measurements. They never appear in the
# trend table and are never diffed.
NOT_A_METRIC = frozenset({"run_at", "metric_definition_version"})

# ---------------------------------------------------------------------------
# What each metric means, and which way is good
#
# +1 up is better, -1 down is better, 0 informational — a count that moves for
# ordinary reasons (a card launched, a card was retired) and should never be
# reported as a regression the founder has to chase.
# ---------------------------------------------------------------------------
GOOD_DIRECTION: dict[str, int] = {
    "total_cards": 0,
    "active_cards": 0,
    "total_reward_rules": 0,
    "sourced_rules": +1,
    "sourced_rules_pct": +1,
    "zero_rate_cards": -1,
    "caps_without_unit": -1,
    "rules_over_ceiling": -1,
    "duplicate_rule_keys": -1,
    "non_numeric_caps": -1,
    "sources_resolved": +1,
    "sources_unresolved": -1,
    "sources_stale_days": -1,
}

# Plain words, because the reader is the founder and not the author of the
# pipeline. "sourced_rules +12" tells him nothing; the sentence does.
METRIC_LABELS: dict[str, str] = {
    "total_cards": "cards in the catalogue",
    "active_cards": "cards switched on in the app",
    "total_reward_rules": "reward rules in total",
    "sourced_rules": "reward rules that cite the bank's own document",
    "sourced_rules_pct": "share of reward rules citing the bank (%)",
    "zero_rate_cards": "cards that show a user 0.0% and rank last",
    "caps_without_unit": "capped rules that never say rupees or points",
    "rules_over_ceiling": "rules displaying a rate no Indian card pays",
    "duplicate_rule_keys": "rules that silently shadow another rule on the same card",
    "non_numeric_caps": "caps the app cannot read, so the boosted rate pays forever",
    "sources_resolved": "cards with a fetched issuer document",
    "sources_unresolved": "cards with no issuer document we could fetch",
    "sources_stale_days": "days since the oldest issuer document was re-read",
}

# The report says "1 more card", never "1 more cards". A founder stops trusting a
# summary that reads like it was generated, and this is the cheapest place to buy
# that trust back.
METRIC_LABELS_ONE: dict[str, str] = {
    "total_cards": "card in the catalogue",
    "active_cards": "card switched on in the app",
    "total_reward_rules": "reward rule in total",
    "sourced_rules": "reward rule that cites the bank's own document",
    "zero_rate_cards": "card that shows a user 0.0% and ranks last",
    "caps_without_unit": "capped rule that never says rupees or points",
    "rules_over_ceiling": "rule displaying a rate no Indian card pays",
    "duplicate_rule_keys": "rule that silently shadows another rule on the same card",
    "non_numeric_caps": "cap the app cannot read, so the boosted rate pays forever",
    "sources_resolved": "card with a fetched issuer document",
    "sources_unresolved": "card with no issuer document we could fetch",
    "sources_stale_days": "day since the oldest issuer document was re-read",
}

# Which regression leads the headline when several appear at once. Ordered by
# what a user actually feels: a wrong rate on screen beats a cap that never
# stops beats a card that merely looks unattractive.
HEADLINE_PRIORITY = (
    "rules_over_ceiling",
    "non_numeric_caps",
    "duplicate_rule_keys",
    "zero_rate_cards",
    "caps_without_unit",
    "sources_unresolved",
    "sources_stale_days",
)

# U+2581..U+2588. Renders in a PR body and in an Actions job summary, which are
# the only two places this string is ever read.
_SPARK = "▁▂▃▄▅▆▇█"


# ---------------------------------------------------------------------------
# Small readers. Every one of these is deliberately tolerant: a single malformed
# card must not crash the weekly job, because a crashed job is how a catalogue
# rots unnoticed.
# ---------------------------------------------------------------------------
def _fnum(v: Any) -> float | None:
    """A JSON number, or None. Booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return None if f != f else f  # NaN


def _sane_point_value(v: Any) -> float:
    """CreditCardData.sanePointValue — out-of-range point values collapse to 0.25."""
    f = _fnum(v)
    if f is None or f <= 0 or f > APP_POINT_VALUE_MAX:
        return APP_POINT_VALUE_DEFAULT
    return f


def _truthy(v: Any) -> bool:
    """is_active ships as 1/0 today, but has been true/false and "1" before now."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _card_inner(entry: Any) -> dict[str, Any] | None:
    """The card object inside a seed entry, or None when the entry is unusable.

    Mirrors tools/kredme.py: entries are {"card": {...}, "reward_rules": [...]},
    but a bare card dict is accepted so a hand-built fixture works too. An entry
    with no id is dropped from every count rather than counted under "", which
    would let two broken cards collide with each other.
    """
    if not isinstance(entry, dict):
        return None
    inner = entry.get("card")
    if not isinstance(inner, dict):
        inner = entry
    return inner if inner.get("id") else None


def _rules(entry: Any) -> list[dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    rules = entry.get("reward_rules")
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def _card_base_pct(inner: dict[str, Any]) -> float:
    rp = _fnum(inner.get("rp_value_standard"))
    if rp is None:
        rp = APP_POINT_VALUE_DEFAULT
    brr = _fnum(inner.get("base_reward_rate")) or 0.0
    return brr * _sane_point_value(rp) * 100


def _rule_pct(rule: dict[str, Any], inner: dict[str, Any], base_pct: float) -> float:
    """The % the app renders for one rule (credit_card.dart rateForRule).

    This is a MEASUREMENT, not a rewrite. 'N points per Rs X' keeps its block
    size in the data; we divide here only to ask whether what the user sees
    clears the ceiling, and the answer is thrown away after it is counted.
    """
    rtype = rule.get("reward_type")
    rate = _fnum(rule.get("reward_rate")) or 0.0
    rp = _fnum(inner.get("rp_value_standard"))
    if rp is None:
        rp = APP_POINT_VALUE_DEFAULT
    pv_raw = rule.get("point_value")
    pv = _sane_point_value(pv_raw if _fnum(pv_raw) is not None else rp)

    if rtype == "cashback_pct":
        return rate * 100
    if rtype == "multiplier":
        brr = _fnum(inner.get("base_reward_rate")) or 0.0
        return rate * brr * pv * 100
    if rtype == "points_per_spend":
        unit = _fnum(rule.get("reward_unit_spend")) or 0.0
        if unit <= 0:
            return base_pct  # the app's own division-by-zero guard
        return (rate / unit) * pv * 100
    return base_pct


def _has_source(rule: dict[str, Any]) -> bool:
    """True when this rule names a document somebody could go and re-read.

    Delegates to `pipeline.provenance`, which is the one place that decides what
    counts as a citation. The validator's L8 layer and `refresh --unsourced-only`
    read the same function, so the number in this report, the number in a
    validation report and the cards the pipeline queues can never be three
    different answers to one question.

    STEP CHANGE, 2026-08-19. This used to count `source_quote` and any non-empty
    `_sources` as well, and reported 61 of 1,279 rules (4.8%) while the validator
    reported 26 (2.0%) for the same file. The gap was not a rounding difference —
    it was 34 rules whose only "provenance" is a quote with no document behind it,
    plus seven citing the literal placeholder string "bank". Neither can be
    re-read when the issuer devalues next quarter, which is the entire purpose of
    a source, so neither is one.

    The metric therefore drops 61 -> 27 in one run, and the week that happens the
    report will say citations were LOST. They were not; the count was flattering.
    Every earlier row in metrics.jsonl is on the old, looser definition and is not
    comparable across that boundary.
    """
    return bool(P.row_document_urls(rule))


def _has_cap_value(rule: dict[str, Any]) -> bool:
    """A cap the data CLAIMS to have.

    An empty string counts: the field is populated, the app's double.tryParse
    reads null, and the rule then pays its boosted rate forever. That is exactly
    the defect worth counting, so "present but unusable" is a cap, not a blank.
    """
    return "cap_amount" in rule and rule["cap_amount"] is not None


def _cap_is_numeric(value: Any) -> bool:
    """What the app's double.tryParse would accept. Anything else means NO CAP."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return _fnum(value) is not None
    if isinstance(value, str):
        try:
            float(value.strip())
        except ValueError:
            return False
        return True
    return False


def _parse_iso(value: Any) -> dt.datetime | None:
    """An ISO timestamp from state, or None. A naive stamp is read as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# The metrics
# ---------------------------------------------------------------------------
def compute_metrics(
    cards: list[dict],
    sources_state: dict,
    *,
    now: dt.datetime | None = None,
) -> dict:
    """Every number the weekly report reasons about. Pure, and flat by design.

    Flat because the output is appended to a JSONL history and rendered as a
    trend table: a nested object cannot be sparklined, diffed or read at a
    glance, so every value here is an int or a float.

    `now` is injectable purely so the staleness metric is testable without a
    clock; callers pass nothing.
    """
    if not isinstance(cards, list):
        raise TypeError(f"cards must be a list of card entries, got {type(cards).__name__}")
    if not isinstance(sources_state, dict):
        raise TypeError(
            "sources_state must be the dict from state.load_state, got "
            f"{type(sources_state).__name__}"
        )

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    total_cards = active_cards = zero_rate_cards = 0
    total_rules = sourced = caps_no_unit = over_ceiling = non_numeric = 0
    seen_keys: set[tuple[str, str]] = set()
    duplicates = 0

    for entry in cards:
        inner = _card_inner(entry)
        if inner is None:
            continue  # unusable entries are dropped from every count, not counted as zero
        total_cards += 1
        if _truthy(inner.get("is_active")):
            active_cards += 1

        # A missing key and an explicit 0 are the same defect to a user: the card
        # renders "0.0%" and sorts last, whatever the reason.
        if not _fnum(inner.get("base_reward_rate")):
            zero_rate_cards += 1

        base_pct = _card_base_pct(inner)
        card_id = str(inner.get("id"))

        for rule in _rules(entry):
            total_rules += 1
            if _has_source(rule):
                sourced += 1

            # The app keys a user's cap progress on '${cardId}|${ruleName}' with
            # the RAW string (app_database.dart:238). So identity here is the raw
            # name too — no strip, no case-fold, and above all no truncation:
            # kredme.py's 80-char key merged 24 genuinely distinct rules into
            # each other, and that discrepancy is the whole reason this counts.
            raw_name = rule.get("rule_name")
            key = (card_id, raw_name if isinstance(raw_name, str) else "")
            if key in seen_keys:
                duplicates += 1
            else:
                seen_keys.add(key)

            if _has_cap_value(rule):
                cap_kind = rule.get("cap_kind")
                if not (isinstance(cap_kind, str) and cap_kind.strip()):
                    caps_no_unit += 1
                if not _cap_is_numeric(rule.get("cap_amount")):
                    non_numeric += 1

            if _rule_pct(rule, inner, base_pct) > C.RATE_CEILING_PCT:
                over_ceiling += 1

    resolved, unresolved, oldest = _source_health(sources_state)
    stale_days = 0 if oldest is None else max(0, (now - oldest).days)

    return {
        "metric_definition_version": METRIC_DEFINITION_VERSION,
        "total_cards": total_cards,
        "active_cards": active_cards,
        "total_reward_rules": total_rules,
        "sourced_rules": sourced,
        "sourced_rules_pct": round(100.0 * sourced / total_rules, 2) if total_rules else 0.0,
        "zero_rate_cards": zero_rate_cards,
        "caps_without_unit": caps_no_unit,
        "rules_over_ceiling": over_ceiling,
        "duplicate_rule_keys": duplicates,
        "non_numeric_caps": non_numeric,
        "sources_resolved": resolved,
        "sources_unresolved": unresolved,
        "sources_stale_days": stale_days,
    }


def _source_health(sources_state: dict) -> tuple[int, int, dt.datetime | None]:
    """(resolved, unresolved, oldest successful fetch) over the committed state.

    A record with a good status but no content hash counts as UNRESOLVED: with
    nothing to compare against next week it cannot tell us whether the issuer
    changed anything, which is the only job that state has.
    """
    entries = sources_state.get("sources")
    if not isinstance(entries, dict):
        return 0, 0, None

    resolved = unresolved = 0
    oldest: dt.datetime | None = None
    for record in entries.values():
        if not isinstance(record, dict):
            unresolved += 1
            continue
        sha = record.get("content_sha256")
        hashed = isinstance(sha, str) and bool(sha.strip())
        if not (record.get("status") in RESOLVED_STATUSES and hashed):
            unresolved += 1
            continue
        resolved += 1
        fetched = _parse_iso(record.get("fetched_at"))
        if fetched is not None and (oldest is None or fetched < oldest):
            oldest = fetched
    return resolved, unresolved, oldest


# ---------------------------------------------------------------------------
# Week over week
# ---------------------------------------------------------------------------
def diff_metrics(prev: dict | None, cur: dict) -> dict:
    """Compare two metric rows.

    Keys come from `cur`: this run defines the metric set, so a metric added
    later appears immediately (as flat, since there is nothing to compare) and a
    metric we stopped computing quietly disappears instead of haunting the table.

    Non-numeric values are skipped entirely — the history row carries a `run_at`
    timestamp and a trend table has no row for a string.
    """
    if not isinstance(cur, dict):
        raise TypeError(f"cur must be a metrics dict, got {type(cur).__name__}")
    if prev is not None and not isinstance(prev, dict):
        raise TypeError(f"prev must be a metrics dict or None, got {type(prev).__name__}")

    # Compared SYMMETRICALLY, and a row written before the key existed is version
    # 1. Two hand-built rows that carry no version therefore still diff normally —
    # only a real history row (no key, so 1) meeting a real compute_metrics row
    # (key, so 2) trips the guard, which is exactly the boundary that matters.
    same_definition = not isinstance(prev, dict) or (
        _fnum(prev.get("metric_definition_version", 1))
        == _fnum(cur.get("metric_definition_version", 1))
    )

    out: dict[str, dict[str, Any]] = {}
    for metric, raw_cur in cur.items():
        if metric in NOT_A_METRIC:
            continue
        cur_val = _fnum(raw_cur)
        if cur_val is None:
            continue
        prev_val = _fnum(prev.get(metric)) if isinstance(prev, dict) else None

        # Measured by a different rule last week, so there is nothing to subtract.
        # Reported as "no comparison", never as a change.
        if prev_val is not None and metric in DEFINITION_SENSITIVE and not same_definition:
            out[metric] = {"prev": None, "cur": raw_cur, "delta": None, "direction": "flat"}
            continue

        if prev_val is None:
            out[metric] = {"prev": None, "cur": raw_cur, "delta": None, "direction": "flat"}
            continue

        delta = round(cur_val - prev_val, 6)
        good = GOOD_DIRECTION.get(metric, 0)
        if delta == 0 or good == 0:
            direction = "flat"
        elif (delta > 0) == (good > 0):
            direction = "better"
        else:
            direction = "worse"
        out[metric] = {
            "prev": prev.get(metric),
            "cur": raw_cur,
            "delta": int(delta) if float(delta).is_integer() else delta,
            "direction": direction,
        }
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _num(v: Any) -> str:
    f = _fnum(v)
    if f is None:
        return "—"
    return f"{f:,.0f}" if f.is_integer() else f"{f:,.2f}"


def _signed(v: Any) -> str:
    f = _fnum(v)
    if f is None:
        return "—"
    body = f"{abs(f):,.0f}" if f.is_integer() else f"{abs(f):,.2f}"
    return f"+{body}" if f > 0 else (f"-{body}" if f < 0 else "0")


def _severity_rank(metric: str) -> int:
    """Where a regression sits in the "who feels this" order. Unknown goes last."""
    if metric in HEADLINE_PRIORITY:
        return HEADLINE_PRIORITY.index(metric)
    return len(HEADLINE_PRIORITY)


def _label(metric: str, count: Any = None) -> str:
    if _fnum(count) is not None and abs(float(count)) == 1:
        return METRIC_LABELS_ONE.get(metric, METRIC_LABELS.get(metric, metric))
    return METRIC_LABELS.get(metric, metric)


def _movement_sentence(metric: str, entry: dict) -> str:
    """One regression or win, said the way a person would say it."""
    delta = _fnum(entry.get("delta")) or 0.0
    word = "more" if delta > 0 else "fewer"
    return (
        f"{_num(abs(delta))} {word} {_label(metric, abs(delta))} "
        f"({_num(entry.get('prev'))} → {_num(entry.get('cur'))})"
    )


def _headline(cur: dict, diffs: dict) -> str:
    """The one line a founder reads. It always answers "is the data more true?".

    Citations lead whenever they moved, in either direction, because that is the
    only metric here that measures truth. Only when they sat still does the
    headline fall back to the worst regression.
    """
    total = _fnum(cur.get("total_reward_rules")) or 0
    sourced = _fnum(cur.get("sourced_rules")) or 0
    pct = _fnum(cur.get("sourced_rules_pct")) or 0.0
    standing = (
        f"{_num(sourced)} of {_num(total)} reward rules ({pct:.1f}%) "
        f"cite the bank's own document"
    )
    short = f"{_num(sourced)} of {_num(total)} ({pct:.1f}%)"

    moved = diffs.get("sourced_rules") or {}
    delta = _fnum(moved.get("delta"))

    if not diffs or all(d.get("delta") is None for d in diffs.values()):
        return f"First run. Baseline: {standing} — everything else is unverified."

    # Past the first-run branch, so the history has comparable rows — but THIS
    # metric has no previous value, which can only mean its definition moved.
    # Saying "34 citations lost" here would be the most alarming and least true
    # sentence this report can print.
    if moved and moved.get("prev") is None:
        return (
            f"Citations are now counted strictly — only a rule naming a document "
            f"anyone can open, the same test the validator and the catch-up run "
            f"use. On that measure, {standing}. Last week's figure used a looser "
            f"rule and is not comparable to it."
        )

    if delta and delta > 0:
        noun = "reward rule now cites" if delta == 1 else "reward rules now cite"
        return f"{_num(delta)} more {noun} the bank's own document — now {short}."
    if delta and delta < 0:
        lost = abs(delta)
        phrase = "reward rule LOST the citation it had" if lost == 1 else \
                 "reward rules LOST the citation they had"
        return f"Careful: {_num(lost)} {phrase} — down to {short}."

    for metric in HEADLINE_PRIORITY:
        entry = diffs.get(metric)
        if entry and entry.get("direction") == "worse":
            return (f"Worse this week: {_movement_sentence(metric, entry)}. "
                    f"Citations unchanged at {short}.")

    wins = [m for m, d in diffs.items() if d.get("direction") == "better"]
    if wins:
        return (f"Better this week: {_movement_sentence(wins[0], diffs[wins[0]])}. "
                f"Citations unchanged at {short}.")
    return f"Nothing moved this week. {standing} — that is the number to move."


def _ordered(metrics: dict) -> list[str]:
    """Declaration order first, then anything new, so the table never reshuffles."""
    known = [m for m in GOOD_DIRECTION if m in metrics]
    extra = sorted(
        m for m in metrics
        if m not in GOOD_DIRECTION and m not in NOT_A_METRIC
        and _fnum(metrics[m]) is not None
    )
    return known + extra


def render_report(cur: dict, prev: dict | None, *, applied: int = 0, blocked: int = 0) -> str:
    """The PR body / job summary. Leads with the sentence, not the table."""
    if not isinstance(cur, dict):
        raise TypeError(f"cur must be a metrics dict, got {type(cur).__name__}")

    diffs = diff_metrics(prev, cur)
    lines: list[str] = ["# KredMe card data — weekly health", ""]
    lines.append(f"**{_headline(cur, diffs)}**")
    lines.append("")

    if applied or blocked:
        lines.append(
            f"This run applied **{applied}** verified change(s) and blocked **{blocked}** "
            f"that failed verification."
        )
    else:
        lines.append(
            "This run changed no card data — no source moved, or nothing survived verification."
        )
    lines.append("")

    regressions = [(m, d) for m, d in diffs.items() if d.get("direction") == "worse"]
    if regressions:
        lines.append("## Needs a human")
        for metric, entry in sorted(regressions, key=lambda kv: _severity_rank(kv[0])):
            lines.append(f"- **{_movement_sentence(metric, entry)}**")
        lines.append("")

    lines.append("## The numbers")
    lines.append("")
    lines.append("| What we track | Last week | This week | Change | Better or worse |")
    lines.append("| --- | ---: | ---: | ---: | --- |")
    for metric in _ordered(cur):
        entry = diffs.get(metric)
        if entry is None:
            continue
        mark = {"better": "better", "worse": "**WORSE**", "flat": ""}[entry["direction"]]
        lines.append(
            f"| {_label(metric)} | {_num(entry['prev'])} | {_num(entry['cur'])} "
            f"| {_signed(entry['delta'])} | {mark} |"
        )
    lines.append("")

    if prev is None:
        lines.append(
            "_No previous run to compare against. Next week's report measures against these._"
        )
    else:
        lines.append(
            "_Only `reward rules that cite the bank's own document` proves the data is TRUE. "
            "Every other number here proves it is merely consistent with itself._"
        )
    return "\n".join(lines) + "\n"


def render_trend(history: list[dict], metric: str, *, width: int = 40) -> str:
    """One metric's last runs as a single line, safe on empty and flat series."""
    values: list[float] = []
    if isinstance(history, list):
        for row in history:
            if not isinstance(row, dict):
                continue
            value = _fnum(row.get(metric))
            if value is not None:
                values.append(value)

    if not values:
        return f"{metric}: no history yet"

    values = values[-max(1, int(width)):]
    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        # One run, or a metric that has not moved. Both are legitimate and
        # neither may divide by zero, so draw a flat mid-height line.
        bar = _SPARK[len(_SPARK) // 2] * len(values)
    else:
        bar = "".join(_SPARK[int((v - low) / span * (len(_SPARK) - 1))] for v in values)

    return (
        f"{metric:<20} {bar}  {_num(values[0])} → {_num(values[-1])} "
        f"over {len(values)} run(s)"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _load_cards(path: pathlib.Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} is not a list of card entries")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline.report",
        description="Weekly card-data health numbers, in words a founder can act on.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical use:  python3 -m pipeline.report --write --applied 3 --blocked 1",
    )
    parser.add_argument("--cards", type=pathlib.Path, default=C.CARDS_JSON)
    parser.add_argument("--sources", type=pathlib.Path, default=C.SOURCE_STATE)
    parser.add_argument("--history", type=pathlib.Path, default=C.METRICS_HISTORY)
    parser.add_argument("--write", action="store_true",
                        help="append this run to the committed metrics history")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="print the metrics row as JSON instead of the report")
    parser.add_argument("--trend", metavar="METRIC",
                        help="print one metric's sparkline from history and exit")
    parser.add_argument("--applied", type=int, default=0,
                        help="verified changes this run applied (for the report body)")
    parser.add_argument("--blocked", type=int, default=0,
                        help="changes this run refused to apply")
    args = parser.parse_args()

    history = S.read_metrics(args.history)

    if args.trend:
        print(render_trend(history, args.trend))
        return 0

    try:
        cards = _load_cards(args.cards)
    except FileNotFoundError:
        print(f"error: no card data at {args.cards}", file=sys.stderr)
        return 1
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
        print(f"error: cannot read {args.cards}: {exc}", file=sys.stderr)
        return 1

    # load_state is deliberately forgiving of a missing or corrupt file, so a
    # first run reports "no sources yet" rather than failing the weekly job.
    metrics = compute_metrics(cards, S.load_state(args.sources))
    prev = history[-1] if history else None

    if args.as_json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        print(render_report(metrics, prev, applied=args.applied, blocked=args.blocked))
        if len(history) >= 2:
            print("## Trend")
            print("```")
            for metric in ("sourced_rules", "zero_rate_cards", "caps_without_unit",
                           "sources_unresolved"):
                print(render_trend(history + [metrics], metric))
            print("```")

    if args.write:
        row = dict(metrics)
        row["run_at"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        S.append_metrics(row, args.history)
        print(f"\nappended to {args.history}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
