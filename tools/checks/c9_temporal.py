"""L9 — temporal & lifecycle.

"Was this true in May, and is it still true today?"

Every other layer asks whether a row is well-formed, whether its number is
plausible, or whether the engine will ever execute it. This one asks the only
question that gets worse on its own while nobody touches the file:

    HAS TIME MADE THIS WRONG, AND WOULD ANYONE FIND OUT?

Three separate things live under that question.

1.  **Dates that cannot be placed in time.** A date the file states but that is
    not a real calendar day, sits in an impossible year, or claims we fetched an
    issuer document tomorrow. Nobody — human or machine — can act on it.

2.  **Changes that reach nobody.** The news feed is refetched ONLY on a
    whole-number version bump: `int.tryParse(version.split('.').first)` and then
    `if (serverVersion <= _currentVersion) return false;`
    (lib/services/news_feed_service.dart). Edit the feed, bump 4.0.0 -> 4.0.1,
    and every existing user keeps the old feed forever with no error anywhere.
    The seed manifest is the mirror image: plain string inequality
    (`serverVersion == _localVersion`, lib/services/seed_sync_service.dart), so
    ANY change to the version string syncs — but reusing a version string that
    was already published means a user who has it will never receive the new
    bytes.

3.  **Data nobody has looked at since the issuer changed it.** The file carries
    only 19 dated provenance stamps. Where our own news feed says an issuer
    changed something on a stated date, and no card that item names carries any
    evidence we re-checked it on or after that date, the app is still quoting
    the old number.

WHAT THIS LAYER DELIBERATELY DOES NOT REPORT
--------------------------------------------
Overlapping codes help nobody, so this layer stays off ground another layer
already owns:

  * L1 owns the *syntax* of a declared date field (is it ISO-shaped at all).
    L9 owns its *position in time*. The one syntax gap L9 does fill is a string
    that passes the ISO shape test but is not a real day — `2026-02-30` and
    `2026-13-01` both satisfy `base.iso_ok`, and neither exists.
  * L3 owns manifest checksums, sizes and the file list.
  * L4 owns `points_expiry_months` plausibility.
  * L6 owns past/future `effective_date` and `expiry_date` on reward rules and
    the fact that the engine reads neither. L9 adds only the two questions L6
    does not ask: whether the two dates contradict each other, and whether an
    expired rule is one the engine would actually still fire.

NOTHING HERE IS GRANDFATHERED. `.published/HIGHWATER.json` is read when it
exists, but only ever to CREATE findings about version reachability — never to
suppress one. `tools/rate_baseline.json` is not read at all.

Authority for every app-behaviour claim is the shipping trunk `nous/master`:
    lib/services/news_feed_service.dart   (version gate, isExpired, publishedAt)
    lib/services/seed_sync_service.dart   (manifest version + min_app_version)
    lib/core/engine/recommendation_engine.dart / lib/shared/models/credit_card.dart
"""
from __future__ import annotations

import datetime
import json
import re

from .base import Ctx, Finding, ERROR, WARN, INFO, num, trunc, iso_ok, card_base_pct

LAYER = "L9 temporal & lifecycle"

# --------------------------------------------------------------------------- #
# What counts as a date, and what kind of date it is
# --------------------------------------------------------------------------- #

# Keys whose whole purpose is to name a day. A non-empty string here that we
# cannot turn into a real date is a defect, whatever it looks like.
KNOWN_DATE_KEYS = {
    "effective_date", "expiry_date", "expires_at", "expiry_at",
    "source_fetched_on", "fetched_on", "verified_on", "last_verified",
    "published_at", "published_on", "updated_at", "created_at",
    "valid_from", "valid_to", "valid_until", "start_date", "end_date",
    "bonus_until", "last_checked", "last_updated", "date",
}

# A looser net, used only to notice a date-shaped string sitting under a key
# nobody declared as a date. Value must still be a string.
DATEISH_KEY = re.compile(
    r"(?:^|_)(date|expiry|expires|effective|published|updated|created|"
    r"fetched|verified|valid|until|since|checked)(?:$|_)|(_at|_on)$",
    re.I,
)

# Stamps record something that already happened. A stamp in the future is an
# impossibility, not a schedule.
STAMP_KEYS = {
    "source_fetched_on", "fetched_on", "verified_on", "last_verified",
    "published_at", "published_on", "updated_at", "created_at",
    "last_checked", "last_updated",
}

# Dates a human wrote meaning "from / until". A future one is legitimate
# intent; whether anything acts on it is a separate question.
SCHEDULE_KEYS = {
    "effective_date", "expiry_date", "expires_at", "expiry_at",
    "valid_from", "valid_to", "valid_until", "start_date", "end_date",
    "bonus_until",
}

BLOCKS = ("card", "reward_rules", "exclusion_rules", "milestone_rules",
          "fuel_surcharge_rules", "redemption_rules")

# L6 already reports past / future / unreadable on this exact pair. L9 only
# looks at them for the two questions L6 does not ask.
L6_OWNED = {("reward_rules", "effective_date"), ("reward_rules", "expiry_date")}

# The card catalogue predates the company; nothing legitimately carries a date
# outside this band, so anything outside it is a typing accident.
YEAR_FLOOR = 2015
YEAR_CEILING_SLACK = 3          # today.year + 3

# How fresh "fresh" is. 90 days is the number the brief asks for.
FRESH_DAYS = 90
# A feed nobody has added to in this long has stopped being a live feed.
NEWS_STALE_DAYS = 30
# A story stamped this long after the day the change actually happened is
# carrying the scrape date, not the publication date.
NEWS_ID_DRIFT_DAYS = 30
# merchants.json carries its own stamp; this far behind the manifest means
# nobody is maintaining it.
MERCHANT_STAMP_DRIFT_DAYS = 60

# The two key-name defects this feed is known to have shipped before:
# news_001 used 'expires_at' where the app reads 'expiry_date', and 'url'
# where it reads 'source_url'. NewsArticle.fromJson reads the right-hand name.
NEWS_UNREAD_ALIASES = {
    "expires_at": "expiry_date",
    "expiry": "expiry_date",
    "expires": "expiry_date",
    "valid_until": "expiry_date",
    "url": "source_url",
    "link": "source_url",
    "source": None,                 # real key — placeholder, filtered below
    "published": "published_at",
    "date": "published_at",
    "publish_date": "published_at",
    "created_at": "published_at",
    "cards": "affected_cards",
    "affected_card_ids": "affected_cards",
    "issuers": "affected_issuers",
}
NEWS_UNREAD_ALIASES = {k: v for k, v in NEWS_UNREAD_ALIASES.items() if v}

# Keys NewsArticle.fromJson actually reads (news_feed_service.dart:52-67).
NEWS_READ_KEYS = {
    "id", "title", "summary", "category", "severity", "source", "source_url",
    "published_at", "affected_cards", "affected_issuers", "tags",
    "action_text", "expiry_date",
}

# news ids are minted as news_<source>_<YYYY_MM_DD>_<slug>_<hash>, or
# news_<source>_undated_<slug>_<hash>.
NEWS_ID_DATE = re.compile(r"_(\d{4})_(\d{2})_(\d{2})_")

# Engine routing, only as much as is needed to answer "would this still fire".
NEVER_INDEXED_RULE_TYPES = ("portal_bonus", "milestone")
BASE_LANE_CHANNELS = (None, "online", "upi")
BASE_LANE_RULE_TYPES = ("base_rate", "channel_specific", "promotional",
                        "threshold_tier")


# --------------------------------------------------------------------------- #
# Tiny, total helpers. None of these may raise.
# --------------------------------------------------------------------------- #
def _s(v):
    """The value as a non-empty stripped string, or None."""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def _as_date(v):
    """A real calendar day, or None. Accepts 'YYYY-MM-DD' and full timestamps."""
    s = _s(v)
    if s is None or len(s) < 10:
        return None
    try:
        return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10])) \
            if s[4] == "-" and s[7] == "-" else None
    except (ValueError, TypeError, IndexError):
        return None


def _as_moment(v):
    """A date+time, or None. Used where two stamps must be ordered."""
    s = _s(v)
    if s is None:
        return None
    t = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    try:
        d = datetime.datetime.fromisoformat(t)
    except (ValueError, TypeError):
        d = None
    if d is None:
        day = _as_date(s)
        return datetime.datetime(day.year, day.month, day.day) if day else None
    return d.replace(tzinfo=None) if d.tzinfo is not None else d


def _version_parts(v):
    """('5.1.20' -> (5, [5,1,20])) or (None, None). Mirrors the app's parsing:
    the major is int.tryParse of everything before the first dot."""
    s = _s(v)
    if s is None:
        return None, None
    head = s.split(".")[0]
    try:
        major = int(head)
    except (ValueError, TypeError):
        return None, None
    parts = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except (ValueError, TypeError):
            parts.append(-1)
    return major, parts


def _rows(entry, block):
    v = entry.get(block) if isinstance(entry, dict) else None
    return v if isinstance(v, list) else []


def _news_items(news):
    if isinstance(news, dict):
        for key in ("items", "articles"):
            if isinstance(news.get(key), list):
                return news[key]
        return []
    return news if isinstance(news, list) else []


def _today(ctx: Ctx) -> datetime.date:
    """Today, overridable via ctx.config['today'] so a test can pin it.
    Reads ctx; never writes to it."""
    try:
        pinned = _as_date((ctx.config or {}).get("today"))
    except Exception:
        pinned = None
    return pinned or datetime.date.today()


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


# --------------------------------------------------------------------------- #
# run()
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> list[Finding]:
    out: list[Finding] = []
    today = _today(ctx)

    for step in (_sweep_dates, _rule_lifecycle, _check_news, _check_versions,
                 _check_freshness):
        try:
            step(ctx, today, out)
        except Exception as e:                                  # never crash
            out.append(Finding(
                severity=WARN,
                code="L9.CHECK_INCOMPLETE",
                message=(f"The date-and-freshness check '{step.__name__}' stopped "
                         f"early, so part of this section was not examined."),
                evidence=trunc(f"{type(e).__name__}: {e}", 160),
                impact="Some out-of-date data may not be listed below.",
                fix="Send this line to whoever maintains the validator.",
            ))
    return out


# --------------------------------------------------------------------------- #
# 1. Every date-ish value in cards.json, merchants.json, the manifest and news
# --------------------------------------------------------------------------- #
def _sweep_dates(ctx: Ctx, today: datetime.date, out: list) -> None:
    """Judge every stated date by where it sits in time.

    One finding per (card, defect class) — a card with nine broken stamps is
    one line, but two cards are never merged into one line.
    """
    ceiling = today.year + YEAR_CEILING_SLACK
    # bucket[(card_id, code)] = {"block":…, "index":…, "field":…, "hits":[…]}
    bucket: dict = {}

    def note(card_id, code, block, index, field, value, note_text=None):
        b = bucket.setdefault((card_id, code), {"block": block, "index": index,
                                                "field": field, "hits": []})
        b["hits"].append((block, index, field, value, note_text))

    def judge(card_id, block, index, key, value):
        """Classify one candidate date. Total: swallows anything odd."""
        try:
            s = _s(value)
            if s is None:
                return
            known = key in KNOWN_DATE_KEYS
            shaped = iso_ok(s)
            if not known and not shaped:
                return                      # a date-ish NAME holding prose — not ours
            if (block, key) in L6_OWNED and known:
                # L6 owns past/future/unreadable here. L9 keeps only the one
                # thing L6 cannot see: a shape that is not a real day.
                if shaped and _as_date(s) is None:
                    note(card_id, "L9.DATE_NOT_A_REAL_DAY", block, index, key, s)
                return
            day = _as_date(s)
            if day is None:
                note(card_id, "L9.DATE_NOT_A_REAL_DAY" if shaped
                     else "L9.DATE_CANNOT_BE_PLACED_IN_TIME",
                     block, index, key, s)
                return
            if day.year < YEAR_FLOOR or day.year > ceiling:
                note(card_id, "L9.DATE_YEAR_LOOKS_LIKE_A_TYPO", block, index, key, s)
                return
            if day > today:
                if key in STAMP_KEYS:
                    note(card_id, "L9.STAMP_DATED_IN_THE_FUTURE",
                         block, index, key, s)
                elif key in SCHEDULE_KEYS:
                    note(card_id, "L9.SCHEDULED_DATE_NOTHING_ENFORCES",
                         block, index, key, s)
        except Exception:
            return

    def walk(card_id, block, index, obj, depth=0):
        """Descend a row looking for date-ish keys. Depth-capped, total."""
        try:
            if depth > 4:
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    ks = k if isinstance(k, str) else str(k)
                    if isinstance(v, (dict, list)):
                        walk(card_id, block, index, v, depth + 1)
                    elif ks in KNOWN_DATE_KEYS or DATEISH_KEY.search(ks):
                        judge(card_id, block, index, ks, v)
            elif isinstance(obj, list):
                for v in obj[:200]:
                    walk(card_id, block, index, v, depth + 1)
        except Exception:
            return

    # ---- cards.json ------------------------------------------------------ #
    for _, entry, inner, cid in ctx.entries():
        try:
            walk(cid, "card", None, inner)
            for block in BLOCKS:
                if block == "card":
                    continue
                for j, row in enumerate(_rows(entry, block)):
                    if isinstance(row, (dict, list)):
                        walk(cid, block, j, row)
        except Exception:
            continue

    # ---- merchants.json, manifest, news header --------------------------- #
    for label, blob in (("merchants", ctx.merchants), ("manifest", ctx.manifest),
                        ("news", ctx.news if isinstance(ctx.news, dict) else None)):
        if isinstance(blob, dict):
            for k, v in blob.items():
                ks = k if isinstance(k, str) else str(k)
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        k2s = k2 if isinstance(k2, str) else str(k2)
                        if k2s in KNOWN_DATE_KEYS or DATEISH_KEY.search(k2s):
                            judge(None, label, None, k2s, v2)
                elif ks in KNOWN_DATE_KEYS or DATEISH_KEY.search(ks):
                    judge(None, label, None, ks, v)

    # ---- emit ------------------------------------------------------------ #
    texts = {
        "L9.DATE_NOT_A_REAL_DAY": (
            ERROR,
            "is written like a date but is not a day that exists on a calendar",
            "Nothing can work out when this starts, ends or was last checked, so "
            "the data quietly stays live forever.",
            "Correct the day. Watch for month and day swapped round, and for the "
            "31st of a 30-day month."),
        "L9.DATE_CANNOT_BE_PLACED_IN_TIME": (
            WARN,
            "holds something that is not a date at all",
            "We cannot tell how old this information is, so nobody can tell "
            "whether the card's numbers are still current.",
            "Write the date as YYYY-MM-DD, or take the field out entirely."),
        "L9.DATE_YEAR_LOOKS_LIKE_A_TYPO": (
            ERROR,
            "is dated to a year that cannot be right",
            "A mistyped year makes a current record look ancient, or an ancient "
            "one look current — either way the next person trusts the wrong row.",
            "Fix the year by hand against the issuer page it came from."),
        "L9.STAMP_DATED_IN_THE_FUTURE": (
            ERROR,
            "records that we checked the issuer on a day that has not happened yet",
            "Freshness reporting is wrong: this card counts as recently verified "
            "when in fact nobody has checked it.",
            "Correct the date to the day the document was actually read."),
        "L9.SCHEDULED_DATE_NOTHING_ENFORCES": (
            WARN,
            "is set to a future day, but nothing in the app acts on that day",
            "Whatever was meant to switch on or off then will simply not, and no "
            "alarm will go off. The row behaves as if the date were not there.",
            "Put a reminder in the backlog to edit the file by hand on that date."),
    }
    for (cid, code), b in sorted(bucket.items(),
                                 key=lambda kv: (kv[0][1], kv[0][0] or "")):
        sev, what, impact, fix = texts[code]
        hits = b["hits"]
        first = hits[0]
        where = cid or "seed/manifest.json, merchants.json or news/feed.json"
        out.append(Finding(
            severity=sev,
            code=code,
            message=(f"{where}: {len(hits)} {_plural(len(hits), 'date')} "
                     f"{what}."),
            card_id=cid, block=first[0], index=first[1], field=first[2],
            evidence=trunc("; ".join(
                f"{h[0]}" + (f"[{h[1]}]" if h[1] is not None else "") +
                f".{h[2]} = {h[3]!r}" for h in hits[:4]), 220),
            impact=impact,
            fix=fix,
        ))


# --------------------------------------------------------------------------- #
# 2. Rule lifecycle: dates that contradict each other, and expired-but-live
# --------------------------------------------------------------------------- #
def _would_still_fire(row: dict) -> bool:
    """A deliberately conservative replica of the engine's index-time routing
    (recommendation_engine.dart:139-200). True means 'the engine holds this rule
    in a lane it can reach', so an expired one is not merely dead weight — it is
    still being offered to users."""
    try:
        rt = _s(row.get("rule_type")) or "base_rate"
        if rt in NEVER_INDEXED_RULE_TYPES:
            return False
        if rt == "merchant_specific":
            return _s(row.get("merchant_ref")) is not None
        if rt == "category_bonus":
            return (row.get("category_id") is not None
                    or row.get("conditions_json") is not None)
        if rt in BASE_LANE_RULE_TYPES:
            ch = row.get("channel")
            return (_s(ch) if isinstance(ch, str) else None) in BASE_LANE_CHANNELS
        if rt == "conditional":
            return True
        return False                          # unknown type: never indexed
    except Exception:
        return False


def _rule_lifecycle(ctx: Ctx, today: datetime.date, out: list) -> None:
    for _, entry, inner, cid in ctx.entries():
        contradictory, live_expired = [], []
        for block in ("reward_rules", "milestone_rules", "redemption_rules"):
            for j, row in enumerate(_rows(entry, block)):
                try:
                    if not isinstance(row, dict):
                        continue
                    eff = _as_date(row.get("effective_date"))
                    exp = _as_date(row.get("expiry_date") or row.get("expires_at"))
                    contradicts = bool(eff and exp and eff > exp)
                    if contradicts:
                        contradictory.append((block, j, row.get("rule_name"),
                                              eff.isoformat(), exp.isoformat()))
                    # A row whose two dates contradict each other is reported
                    # above and not counted again here — one defect, one code.
                    if (block == "reward_rules" and exp and exp < today
                            and not contradicts and _would_still_fire(row)):
                        live_expired.append((j, row.get("rule_name"),
                                             exp.isoformat()))
                except Exception:
                    continue

        if contradictory:
            b, j, nm, eff, exp = contradictory[0]
            out.append(Finding(
                severity=ERROR,
                code="L9.STARTS_AFTER_IT_ENDS",
                message=(f"{cid} has {len(contradictory)} "
                         f"{_plural(len(contradictory), 'rule')} whose start date "
                         f"is later than its end date, so the offer never has a "
                         f"single valid day."),
                card_id=cid, block=b, index=j, field="effective_date",
                evidence=trunc("; ".join(
                    f"{x[0]}[{x[1]}] '{_s(x[2]) or '(unnamed)'}' starts {x[3]}, "
                    f"ends {x[4]}" for x in contradictory[:3]), 220),
                impact=("Whichever date is wrong, one of them is. Nobody can say "
                        "whether this offer is meant to be running today."),
                fix=("Check the issuer page and correct whichever of the two dates "
                     "was mistyped."),
            ))

        if live_expired:
            j, nm, exp = live_expired[0]
            out.append(Finding(
                severity=ERROR,
                code="L9.EXPIRED_RULE_STILL_RANKED",
                message=(f"{cid} has {len(live_expired)} reward "
                         f"{_plural(len(live_expired), 'rule')} whose end date has "
                         f"passed, and the app still uses "
                         f"{'them' if len(live_expired) > 1 else 'it'} to recommend "
                         f"this card."),
                card_id=cid, block="reward_rules", index=j, field="expiry_date",
                evidence=trunc("; ".join(
                    f"[{x[0]}] '{_s(x[1]) or '(unnamed)'}' ended {x[2]}"
                    for x in live_expired[:3]), 220),
                impact=("The user is told they will earn a rate that stopped "
                        "existing. They swipe on our advice and get less."),
                fix=("Delete the rule or replace its numbers with the current "
                     "ones. An end date switches nothing off by itself — no part "
                     "of the app reads it."),
            ))


# --------------------------------------------------------------------------- #
# 3. news/feed.json
# --------------------------------------------------------------------------- #
def _check_news(ctx: Ctx, today: datetime.date, out: list) -> None:
    items = _news_items(ctx.news)
    if not items:
        return                                  # L1/L3 own an unreadable feed

    n_items = len(items)
    alias_hits: dict = {}
    unusable_pub, future_pub, already_expired, bad_expiry = [], [], [], []
    pub_moments: dict = {}
    id_drift = []
    newest = None
    have_expiry = 0

    for i, it in enumerate(items):
        try:
            if not isinstance(it, dict):
                continue

            # -- key-name audit: the historical expires_at / url defect ----- #
            for k in it.keys():
                ks = k if isinstance(k, str) else str(k)
                if ks in NEWS_READ_KEYS:
                    continue
                want = NEWS_UNREAD_ALIASES.get(ks)
                if want:
                    alias_hits.setdefault((ks, want), []).append(i)

            # -- published_at ---------------------------------------------- #
            raw_pub = it.get("published_at")
            m = _as_moment(raw_pub)
            if m is None:
                unusable_pub.append((i, _s(it.get("id")), raw_pub))
            else:
                pub_moments.setdefault(m.isoformat(sep=" "), []).append(i)
                if newest is None or m > newest:
                    newest = m
                if m.date() > today:
                    future_pub.append((i, _s(it.get("id")), _s(raw_pub)))

            # -- expiry_date ------------------------------------------------ #
            raw_exp = it.get("expiry_date")
            if _s(raw_exp) is not None:
                have_expiry += 1
                d = _as_date(raw_exp)
                if d is None:
                    bad_expiry.append((i, _s(it.get("id")), _s(raw_exp)))
                elif d < today:
                    already_expired.append((i, _s(it.get("id")), d.isoformat()))

            # -- does the id's own date agree with published_at? ------------ #
            mid = NEWS_ID_DATE.search(_s(it.get("id")) or "")
            if mid and m is not None:
                try:
                    event = datetime.date(int(mid.group(1)), int(mid.group(2)),
                                          int(mid.group(3)))
                except ValueError:
                    event = None
                if event and (m.date() - event).days > NEWS_ID_DRIFT_DAYS:
                    id_drift.append((i, event.isoformat(), m.date().isoformat(),
                                     (m.date() - event).days))
        except Exception:
            continue

    # ---- the key-name audit result, stated either way -------------------- #
    if alias_hits:
        for (used, want), where in sorted(alias_hits.items()):
            out.append(Finding(
                severity=ERROR,
                code="L9.NEWS_KEY_NAME_APP_DOES_NOT_READ",
                message=(f"{len(where)} news "
                         f"{_plural(len(where), 'story', 'stories')} "
                         f"{_plural(len(where), 'stores', 'store')} this under "
                         f"'{used}', but the app only ever looks for '{want}'."),
                block="news", index=where[0], field=used,
                evidence=trunc(f"used on {_plural(len(where), 'story', 'stories')} "
                               f"at index {', '.join(str(x) for x in where[:8])}", 160),
                impact=("The value is shipped to every handset and read by nobody. "
                        "If it is the expiry date, the story never expires; if it is "
                        "the link, the 'read more' tap goes nowhere."),
                fix=f"Rename the key to '{want}' and bump the feed's whole version number.",
            ))
    else:
        out.append(Finding(
            severity=INFO,
            code="L9.NEWS_KEY_NAMES_MATCH_THE_APP",
            message=(f"Checked all {n_items} news "
                     f"{_plural(n_items, 'story', 'stories')} for the two key-name "
                     f"faults this feed has shipped before ('expires_at' instead of "
                     f"'expiry_date', 'url' instead of 'source_url'). Neither is "
                     f"present today — every story uses the names the app reads."),
            block="news",
            evidence="expiry_date: correct on all stories; source_url: correct on all stories",
            impact="None. This is the clean result.",
            fix="No action. Re-run this check after any hand-edit to the feed.",
        ))

    # ---- published_at ---------------------------------------------------- #
    if unusable_pub:
        out.append(Finding(
            severity=ERROR,
            code="L9.NEWS_PUBLISHED_DATE_UNUSABLE",
            message=(f"{len(unusable_pub)} news "
                     f"{_plural(len(unusable_pub), 'story', 'stories')} "
                     f"{_plural(len(unusable_pub), 'has', 'have')} no readable "
                     f"publication date."),
            block="news", index=unusable_pub[0][0], field="published_at",
            evidence=trunc("; ".join(f"[{i}] {sid or '?'} = {v!r}"
                                     for i, sid, v in unusable_pub[:4]), 220),
            impact=("The app substitutes the moment the phone read the feed, so the "
                    "story permanently claims to be brand new: it sits at the top of "
                    "the news screen and keeps re-lighting the unread badge."),
            fix="Give every story a real published_at, written as YYYY-MM-DDTHH:MM:SSZ.",
        ))
    if future_pub:
        out.append(Finding(
            severity=WARN,
            code="L9.NEWS_PUBLISHED_DATE_IN_THE_FUTURE",
            message=(f"{len(future_pub)} news "
                     f"{_plural(len(future_pub), 'story', 'stories')} "
                     f"{_plural(len(future_pub), 'is', 'are')} dated later than today."),
            block="news", index=future_pub[0][0], field="published_at",
            evidence=trunc("; ".join(f"[{i}] {sid or '?'} = {v}"
                                     for i, sid, v in future_pub[:4]), 220),
            impact=("A future-dated story pins itself to the top of the news screen "
                    "and stays counted as unread until that day arrives."),
            fix="Set the date to the day the issuer actually announced the change.",
        ))

    # ---- expiry ---------------------------------------------------------- #
    if already_expired:
        out.append(Finding(
            severity=WARN,
            code="L9.NEWS_STORY_ALREADY_EXPIRED",
            message=(f"{len(already_expired)} news "
                     f"{_plural(len(already_expired), 'story', 'stories')} "
                     f"expired before today, so {_plural(len(already_expired), 'it is', 'they are')} "
                     f"shipped but never shown."),
            block="news", index=already_expired[0][0], field="expiry_date",
            evidence=trunc("; ".join(f"[{i}] {sid or '?'} expired {d}"
                                     for i, sid, d in already_expired[:4]), 220),
            impact=("The app filters expired stories out before display, so this is "
                    "download weight that reaches no user."),
            fix="Delete the story from the feed, or extend its expiry date.",
        ))
    if bad_expiry:
        out.append(Finding(
            severity=WARN,
            code="L9.NEWS_EXPIRY_UNREADABLE_SO_NEVER_EXPIRES",
            message=(f"{len(bad_expiry)} news "
                     f"{_plural(len(bad_expiry), 'story', 'stories')} "
                     f"{_plural(len(bad_expiry), 'has', 'have')} an end date the app "
                     f"cannot read, so it treats "
                     f"{_plural(len(bad_expiry), 'it', 'them')} as never expiring."),
            block="news", index=bad_expiry[0][0], field="expiry_date",
            evidence=trunc("; ".join(f"[{i}] {sid or '?'} = {v!r}"
                                     for i, sid, v in bad_expiry[:4]), 220),
            impact="An old announcement stays on the news screen indefinitely.",
            fix="Write the end date as YYYY-MM-DD, or set it to null on purpose.",
        ))
    if have_expiry == 0:
        out.append(Finding(
            severity=INFO,
            code="L9.NEWS_NOTHING_EVER_EXPIRES",
            message=(f"None of the {n_items} news "
                     f"{_plural(n_items, 'story', 'stories')} has an end date, so "
                     f"nothing in the feed ever removes itself."),
            block="news", field="expiry_date",
            evidence=f"expiry_date is empty on all {n_items} stories",
            impact=("Time-limited announcements — a festive offer, a fee waiver "
                    "window — will still be on the news screen a year from now."),
            fix=("Set expiry_date on any story that is only true for a while. The "
                 "app already hides a story once that day passes."),
        ))

    # ---- one timestamp shared by many stories ---------------------------- #
    if pub_moments:
        biggest_ts, biggest = max(pub_moments.items(), key=lambda kv: len(kv[1]))
        if len(biggest) >= 3 and len(biggest) >= n_items // 2:
            out.append(Finding(
                severity=WARN,
                code="L9.NEWS_ONE_TIMESTAMP_FOR_MANY_STORIES",
                message=(f"{len(biggest)} of {n_items} news stories carry the exact "
                         f"same publication time, to the second — the moment the feed "
                         f"was generated, not the day each change happened."),
                block="news", index=biggest[0], field="published_at",
                evidence=trunc(f"{biggest_ts} shared by {len(biggest)} stories", 160),
                impact=("The news screen sorts by this time, so the order users see "
                        "is arbitrary, and an announcement from months ago reads as "
                        "today's news."),
                fix=("Stamp each story with the date the issuer announced it. The "
                     "story ids already carry that date."),
            ))

    # ---- the id knows a date the timestamp contradicts ------------------- #
    if id_drift:
        worst = max(id_drift, key=lambda x: x[3])
        out.append(Finding(
            severity=WARN,
            code="L9.NEWS_DATE_CONTRADICTS_ITS_OWN_ID",
            message=(f"{len(id_drift)} news "
                     f"{_plural(len(id_drift), 'story', 'stories')} name a change "
                     f"date in the story id that is well before the publication date "
                     f"we show — up to {worst[3]} days apart."),
            block="news", index=worst[0], field="published_at",
            evidence=trunc("; ".join(f"[{i}] id says {ev}, feed says {pb} "
                                     f"({gap}d)" for i, ev, pb, gap in
                                     sorted(id_drift, key=lambda x: -x[3])[:4]), 220),
            impact=("A user is shown 'yesterday' against a change their issuer made "
                    "in April. It makes stale news look urgent and hides how far "
                    "behind our card data actually is."),
            fix=("Use the date already encoded in the story id as published_at when "
                 "the feed is generated."),
        ))

    # ---- has anyone added to this feed lately? --------------------------- #
    if newest is not None:
        age = (today - newest.date()).days
        if age > NEWS_STALE_DAYS:
            out.append(Finding(
                severity=WARN,
                code="L9.NEWS_FEED_HAS_STOPPED_MOVING",
                message=(f"The newest story in the news feed is {age} days old. "
                         f"Card terms change more often than that."),
                block="news", field="published_at",
                evidence=trunc(f"newest published_at = {newest.date().isoformat()}; "
                               f"{n_items} stories in the feed", 160),
                impact=("Users open the news screen, see nothing new, and stop "
                        "opening it. The alert bell has nothing to ring about."),
                fix=("Run the daily news watch, or check whether it has silently "
                     "stopped producing pull requests."),
            ))


# --------------------------------------------------------------------------- #
# 4. Versions: would a change actually reach a phone?
# --------------------------------------------------------------------------- #
def _load_highwater(ctx: Ctx):
    """The highest seed and news versions ever published, if the local
    .published/ directory exists. Used ONLY to raise findings about whether a
    change can reach a user — never to suppress one, and never as a baseline
    that excuses a defect."""
    try:
        base = ctx.seed_dir.parent if ctx.seed_dir is not None else None
        if base is None:
            return None
        p = base / ".published" / "HIGHWATER.json"
        if not p.is_file():
            return None
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _check_versions(ctx: Ctx, today: datetime.date, out: list) -> None:
    man = ctx.manifest if isinstance(ctx.manifest, dict) else {}
    feed = ctx.news if isinstance(ctx.news, dict) else {}
    hw = _load_highwater(ctx) or {}

    seed_v = _s(man.get("version"))
    news_v = _s(feed.get("version")) or _s(man.get("news_version"))
    man_news_v = _s(man.get("news_version"))
    feed_v = _s(feed.get("version"))

    # ---- the feed's version must survive the app's parser ---------------- #
    fmajor, fparts = _version_parts(feed_v)
    if feed_v is not None and fmajor is None:
        out.append(Finding(
            severity=ERROR,
            code="L9.NEWS_VERSION_UNREADABLE_BY_THE_APP",
            message=(f"The news feed's version '{feed_v}' does not start with a "
                     f"whole number, so the app reads it as 0 and decides the feed "
                     f"is older than the copy already on the phone."),
            block="news", field="version", evidence=trunc(feed_v, 60),
            impact="No user ever receives another news story again.",
            fix="Write the version as a plain number-first string, e.g. '5.0.0'.",
        ))

    # ---- manifest and feed must agree on which version the feed is ------- #
    if man_news_v and feed_v and man_news_v != feed_v:
        out.append(Finding(
            severity=WARN,
            code="L9.NEWS_VERSION_DISAGREES_WITH_MANIFEST",
            message=(f"seed/manifest.json says the news feed is version "
                     f"'{man_news_v}', but news/feed.json says '{feed_v}'."),
            block="manifest", field="news_version",
            evidence=f"manifest news_version={man_news_v!r}, feed version={feed_v!r}",
            impact=("The app goes by the feed's own version, so the manifest is "
                    "telling anyone who reads it the wrong thing about what is live."),
            fix="Make the two match, and let the publish tool set both in future.",
        ))

    # ---- version formats -------------------------------------------------- #
    for label, val, field, blk in (("seed data", seed_v, "version", "manifest"),
                                   ("news feed", feed_v, "version", "news")):
        if val is None:
            out.append(Finding(
                severity=ERROR,
                code="L9.VERSION_MISSING",
                message=f"The {label} has no version number at all.",
                block=blk, field=field,
                impact=("The app cannot tell whether it already has this data, so "
                        "it either never updates or re-downloads on every launch."),
                fix="Add a version, e.g. '5.2.0', and bump it on every publish.",
            ))
        elif not re.match(r"^\d+(\.\d+)*$", val):
            out.append(Finding(
                severity=WARN,
                code="L9.VERSION_NOT_PLAIN_NUMBERS",
                message=(f"The {label} version '{val}' is not written as plain "
                         f"dotted numbers."),
                block=blk, field=field, evidence=trunc(val, 60),
                impact=("Nobody can tell at a glance which of two versions is newer, "
                        "and the app's whole-number news check may read it as 0."),
                fix="Use digits and dots only, e.g. '5.2.0'.",
            ))

    # ---- monotonicity, against the highest ever published ---------------- #
    hw_seed = _s(hw.get("seed_version"))
    hw_news = _s(hw.get("news_version"))

    if hw_seed and seed_v:
        _, cur = _version_parts(seed_v)
        _, top = _version_parts(hw_seed)
        if cur and top and cur < top:
            out.append(Finding(
                severity=ERROR,
                code="L9.SEED_VERSION_GOES_BACKWARDS",
                message=(f"The seed data is version '{seed_v}', but '{hw_seed}' has "
                         f"already been published to users."),
                block="manifest", field="version",
                evidence=f"current={seed_v}, highest ever published={hw_seed}",
                impact=("Every user who already has the newer version will never "
                        "receive this data, and the publish tool will refuse to run."),
                fix=f"Set the version above {hw_seed} before publishing.",
            ))
        elif seed_v == hw_seed:
            # Has anything actually been edited at this version? The manifest
            # records a checksum per file; if the bytes on disk no longer match
            # it, an edit has happened and is stranded behind a version that was
            # already published. (L3 owns reporting the mismatch itself; here it
            # is used only to grade how urgent this is.)
            edited = _seed_bytes_moved(ctx, man)
            out.append(Finding(
                severity=ERROR if edited else INFO,
                code="L9.SEED_VERSION_ALREADY_USED",
                message=(
                    (f"The seed data has been edited but still carries version "
                     f"'{seed_v}', which was already published. The edit reaches "
                     f"nobody who already has that version.")
                    if edited else
                    (f"The seed data carries version '{seed_v}', the version already "
                     f"published. Nothing has been edited since, so nothing is wrong "
                     f"yet — but the next edit must bump this number or it will not "
                     f"reach a single existing user.")),
                block="manifest", field="version",
                evidence=(f"current={seed_v}, highest ever published={hw_seed}, "
                          f"files changed since the manifest was written: "
                          f"{'yes' if edited else 'no'}"),
                impact=("The app decides it is already up to date by comparing "
                        "version strings alone. A user on this version never "
                        "downloads the corrected card numbers, however long they "
                        "wait, and no error appears anywhere."),
                fix=(f"Bump the version to something above '{seed_v}' and republish "
                     f"— the publish tool does this for you."),
            ))

    if hw_news and feed_v:
        hmajor, _ = _version_parts(hw_news)
        if fmajor is not None and hmajor is not None:
            if fmajor < hmajor:
                out.append(Finding(
                    severity=ERROR,
                    code="L9.NEWS_VERSION_GOES_BACKWARDS",
                    message=(f"The news feed is version '{feed_v}', below the "
                             f"'{hw_news}' already published."),
                    block="news", field="version",
                    evidence=f"current={feed_v}, highest ever published={hw_news}",
                    impact="No existing user will ever load this feed.",
                    fix=f"Set the whole number above {hmajor}.",
                ))
            elif fmajor == hmajor and feed_v != hw_news:
                out.append(Finding(
                    severity=ERROR,
                    code="L9.NEWS_CHANGE_WILL_REACH_NOBODY",
                    message=(f"The news feed moved from '{hw_news}' to '{feed_v}', "
                             f"but the whole number in front of the first dot did not "
                             f"change. The app only refetches news when that whole "
                             f"number goes up."),
                    block="news", field="version",
                    evidence=f"published={hw_news}, now={feed_v}, "
                             f"whole number stayed {fmajor}",
                    impact=("Every existing user keeps the old stories forever. "
                            "There is no error, no retry and nothing in any log — "
                            "it simply looks like there is no news."),
                    fix=f"Set the version to '{hmajor + 1}.0.0' before publishing.",
                ))
            elif fmajor == hmajor:
                out.append(Finding(
                    severity=INFO,
                    code="L9.NEWS_NEXT_EDIT_NEEDS_A_WHOLE_NUMBER_BUMP",
                    message=(f"The news feed is at '{feed_v}', the same version that "
                             f"is already live. The next time a story is added or "
                             f"changed, the version must go to "
                             f"'{hmajor + 1}.0.0' — a bump to "
                             f"'{_bump(feed_v)}' would reach nobody."),
                    block="news", field="version",
                    evidence=f"live={hw_news}, working copy={feed_v}",
                    impact=("Nothing is wrong right now. This is the trap that has "
                            "silently swallowed news changes before."),
                    fix="Let the publish tool set the version; it handles this rule.",
                ))

    # ---- the manifest stamp should not predate what it describes --------- #
    man_at = _as_moment(man.get("updated_at"))
    newest_content = None
    for it in _news_items(ctx.news):
        if isinstance(it, dict):
            m = _as_moment(it.get("published_at"))
            if m and (newest_content is None or m > newest_content):
                newest_content = m
    if man_at and newest_content and newest_content > man_at + datetime.timedelta(days=1):
        out.append(Finding(
            severity=WARN,
            code="L9.MANIFEST_STAMP_OLDER_THAN_ITS_CONTENT",
            message=(f"seed/manifest.json says it was last updated "
                     f"{man_at.date().isoformat()}, but the news feed contains a "
                     f"story published {newest_content.date().isoformat()}."),
            block="manifest", field="updated_at",
            evidence=f"manifest updated_at={man_at.isoformat(sep=' ')}, "
                     f"newest story={newest_content.isoformat(sep=' ')}",
            impact=("The one date a human checks to see how fresh the published "
                    "data is understates it, so nobody trusts the stamp."),
            fix="Regenerate the manifest with the publish tool rather than by hand.",
        ))

    # ---- merchants.json keeps its own stamp; is anyone maintaining it? --- #
    mer = ctx.merchants if isinstance(ctx.merchants, dict) else {}
    meta = mer.get("_metadata") if isinstance(mer.get("_metadata"), dict) else {}
    mer_at = _as_moment(meta.get("updated_at"))
    if mer_at and man_at:
        gap = (man_at.date() - mer_at.date()).days
        if gap > MERCHANT_STAMP_DRIFT_DAYS:
            out.append(Finding(
                severity=WARN,
                code="L9.MERCHANT_STAMP_NOT_MAINTAINED",
                message=(f"seed/merchants.json says it was last updated "
                         f"{mer_at.date().isoformat()} — {gap} days before the seed "
                         f"data itself."),
                block="merchants", field="_metadata.updated_at",
                evidence=f"merchants updated_at={mer_at.date().isoformat()}, "
                         f"manifest updated_at={man_at.date().isoformat()}",
                impact=("Merchant categories decide which reward rules fire, so a "
                        "stale merchant file quietly changes what every user is "
                        "recommended — and this stamp gives no warning of it."),
                fix=("Update the stamp whenever merchants.json changes, or drop it "
                     "so nobody relies on a date that is not maintained."),
            ))

    # ---- a minimum app version can lock every user out ------------------- #
    min_app = _s(man.get("min_app_version"))
    if min_app:
        mmaj, minp = _version_parts(min_app)
        app_v = _app_version(ctx)
        _, curp = _version_parts(app_v) if app_v else (None, None)
        if mmaj is None or (minp and any(p < 0 for p in minp)):
            out.append(Finding(
                severity=WARN,
                code="L9.MIN_APP_VERSION_NOT_PLAIN_NUMBERS",
                message=(f"min_app_version '{min_app}' is not plain dotted numbers."),
                block="manifest", field="min_app_version", evidence=trunc(min_app, 40),
                impact=("The app cannot compare it, silently treats every build as "
                        "compatible, and the gate does nothing at all."),
                fix="Write it as digits and dots, e.g. '1.1.0'.",
            ))
        elif minp and curp and curp < minp:
            out.append(Finding(
                severity=ERROR,
                code="L9.MIN_APP_VERSION_LOCKS_EVERYONE_OUT",
                message=(f"The data demands app version {min_app} or newer, but the "
                         f"app in this checkout is {app_v}."),
                block="manifest", field="min_app_version",
                evidence=f"min_app_version={min_app}, app pubspec version={app_v}",
                impact=("Every user on the shipped build is refused the update and "
                        "keeps whatever card data they already have, forever."),
                fix=("Lower min_app_version to the version already on the stores, or "
                     "ship the app build first."),
            ))


def _seed_bytes_moved(ctx: Ctx, man: dict) -> bool:
    """True when a seed file on disk no longer matches the checksum the manifest
    recorded for it — i.e. it has been edited since the manifest was written.
    Read-only, and used only to grade a finding, never to suppress one."""
    try:
        import hashlib
        files = man.get("files")
        if not isinstance(files, list) or ctx.seed_dir is None:
            return False
        root = ctx.seed_dir.parent
        for f in files:
            if not isinstance(f, dict):
                continue
            want = _s(f.get("checksum"))
            rel = _s(f.get("path")) or _s(f.get("name"))
            if not want or not rel:
                continue
            want = want.split(":")[-1]
            p = root / rel
            if not p.is_file():
                p = ctx.seed_dir / rel
                if not p.is_file():
                    continue
            got = hashlib.sha256(p.read_bytes()).hexdigest()
            if got.lower() != want.lower():
                return True
        return False
    except Exception:
        return False


def _bump(v):
    """The patch bump a human would type by hand — quoted in findings only to
    show which bump would NOT work."""
    try:
        parts = (v or "").split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except Exception:
        return "the next patch version"


def _app_version(ctx: Ctx):
    """The Flutter app's own version from pubspec.yaml, or None."""
    try:
        if ctx.app_root is None:
            return None
        p = ctx.app_root / "pubspec.yaml"
        if not p.is_file():
            return None
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[:60]:
            if line.startswith("version:"):
                return _s(line.split(":", 1)[1].split("+")[0])
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# 5. Freshness: what have we actually looked at, and when?
# --------------------------------------------------------------------------- #
def _verification_dates(entry, today) -> list:
    """Every date on this card that is evidence a human or a scrape looked at
    the issuer. Only dated evidence counts — an undated source_url proves the
    row came from somewhere, not that it is still true — and a stamp dated after
    today is not evidence of anything, so it is not allowed to make a card look
    freshly checked."""
    seen = []
    for block in BLOCKS:
        rows = [entry.get("card")] if block == "card" else _rows(entry, block)
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("source_fetched_on", "fetched_on", "verified_on",
                        "last_verified", "last_checked"):
                d = _as_date(row.get(key))
                if d and d <= today:
                    seen.append(d)
    return seen


def _check_freshness(ctx: Ctx, today: datetime.date, out: list) -> None:
    cutoff = today - datetime.timedelta(days=FRESH_DAYS)
    newest_by_card: dict = {}
    total = 0

    for _, entry, inner, cid in ctx.entries():
        total += 1
        try:
            dates = _verification_dates(entry, today)
        except Exception:
            dates = []
        if dates:
            newest_by_card[cid] = max(dates)

    fresh = {c: d for c, d in newest_by_card.items() if d >= cutoff}
    stale = {c: d for c, d in newest_by_card.items() if d < cutoff}
    undated = total - len(newest_by_card)

    # ---- the portfolio-level number the founder asked for ---------------- #
    if total:
        pct = 100.0 * len(fresh) / total
        out.append(Finding(
            severity=WARN if pct < 50 else INFO,
            code="L9.PORTFOLIO_BARELY_VERIFIED",
            message=(f"{len(fresh)} of {total} cards ({pct:.1f}%) carry any evidence "
                     f"that a single field on them was checked against the issuer in "
                     f"the last {FRESH_DAYS} days. {undated} cards carry no date of "
                     f"any kind, so we cannot say when they were last looked at."),
            block="card", field="source_fetched_on",
            evidence=trunc(
                "verified in the window: " +
                (", ".join(f"{c} ({d.isoformat()})"
                           for c, d in sorted(fresh.items())[:8]) or "none"), 300),
            impact=("Card terms move every month. On the cards with no date we are "
                    "quoting numbers of unknown age as current fact, and the app "
                    "shows no age anywhere — a user cannot tell a rate checked last "
                    "week from one copied a year ago."),
            fix=("Stamp source_fetched_on whenever a rule is confirmed against an "
                 "issuer page, and treat the undated cards as unverified rather "
                 "than correct."),
        ))

    if stale:
        oldest = sorted(stale.items(), key=lambda kv: kv[1])
        out.append(Finding(
            severity=WARN,
            code="L9.CARD_NOT_CHECKED_IN_A_LONG_TIME",
            message=(f"{len(stale)} cards were last checked more than {FRESH_DAYS} "
                     f"days ago; the oldest was {oldest[0][1].isoformat()}."),
            card_id=oldest[0][0], block="card", field="source_fetched_on",
            evidence=trunc("; ".join(f"{c} last checked {d.isoformat()}"
                                     for c, d in oldest[:8]), 260),
            impact=("Anything the issuer changed since then is still being shown to "
                    "users as the current rate."),
            fix="Re-run the card refresh for these cards and update the stamp.",
        ))

    # ---- our own news feed says these cards changed. Did we re-check? ---- #
    try:
        _news_vs_freshness(ctx, today, newest_by_card, out)
    except Exception:
        return


def _news_vs_freshness(ctx: Ctx, today, newest_by_card: dict, out: list) -> None:
    """The one cadence signal we actually hold: the news feed states a date on
    which an issuer changed something, and names the cards it hits. If no card
    an item names carries evidence of a check on or after that date, our numbers
    for those cards predate the change the item describes."""
    items = _news_items(ctx.news)
    if not items:
        return
    known = {cid for _, _, _, cid in ctx.entries() if cid}

    for i, it in enumerate(items):
        try:
            if not isinstance(it, dict):
                continue
            m = NEWS_ID_DATE.search(_s(it.get("id")) or "")
            event = None
            if m:
                try:
                    event = datetime.date(int(m.group(1)), int(m.group(2)),
                                          int(m.group(3)))
                except ValueError:
                    event = None
            if event is None:
                event = _as_date(it.get("published_at"))
            if event is None or event > today:
                continue

            cards = [c for c in (it.get("affected_cards") or [])
                     if isinstance(c, str) and c in known]
            if not cards:
                continue
            checked_since = [c for c in cards
                             if newest_by_card.get(c) and newest_by_card[c] >= event]
            if checked_since:
                continue

            sev = (_s(it.get("severity")) or "").lower()
            cat = (_s(it.get("category")) or "").lower()
            worse_for_user = sev in ("negative", "warning") or cat == "devaluation"
            age = (today - event).days

            clause = ("the one card it names carries no record of being checked "
                      "against the issuer since that day"
                      if len(cards) == 1 else
                      f"none of the {len(cards)} cards it names carries any record "
                      f"of being checked against the issuer since that day")
            out.append(Finding(
                severity=WARN if worse_for_user else INFO,
                code="L9.NEWS_SAYS_CHANGED_BUT_NEVER_RECHECKED",
                message=(f"Our own news feed says “"
                         f"{trunc(_s(it.get('title')) or it.get('id'), 70)}” on "
                         f"{event.isoformat()}, {age} days ago — and "
                         f"{clause}."),
                card_id=cards[0] if len(cards) == 1 else None,
                block="news", index=i, field="affected_cards",
                evidence=trunc(f"{cat or 'change'}/{sev or 'unknown'} dated "
                               f"{event.isoformat()}; cards: " + ", ".join(cards[:10])
                               + ("" if len(cards) <= 10 else
                                  f" (+{len(cards) - 10} more)"), 300),
                impact=("We are telling the user about the change on the news screen "
                        "and, on the same cards, still recommending them on the old "
                        "numbers. The two screens contradict each other."
                        if worse_for_user else
                        "The card data may not yet reflect a change we have already "
                        "announced to users."),
                fix=("Re-check these cards against the issuer page, correct the "
                     "affected rules, and stamp source_fetched_on so this stops "
                     "being reported."),
            ))
        except Exception:
            continue
