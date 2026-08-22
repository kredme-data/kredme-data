#!/usr/bin/env python3
"""
newsgen.py — turn detected issuer changes into news/feed.json items.

Usage:
    python3 -m pipeline.newsgen --changes .pipeline-work/changes.json
    python3 -m pipeline.newsgen --changes changes.json --out /tmp/feed.json
    python3 -m pipeline.newsgen --changes changes.json --fresh --rewards-only

Input is the `changes` array produced under schema.NEWS_CHANGE_SCHEMA. Output is a
CANDIDATE feed written to scratch — this module never writes news/feed.json and
never touches seed/cards.json (it only reads card ids and card names to target
items). Publishing stays a deliberate act performed by tools/kredme.py.

THE VERSION TRAP
----------------
The shipped app parses only the LEADING INTEGER of the feed version and refetches
only on a strict increase:

    int.tryParse(rawVer.toString().split('.').first) ?? 0     # news_feed_service.dart:126-127

So "2.1.0" after "2.0.0" is invisible forever to anyone who already holds major 2,
and "v3.0.0" parses to 0 — which stops the feed loading for every user, not just
new ones. next_version() therefore refuses to guess: it raises rather than emit a
version the app cannot read.

Stdlib only. Python 3.12+.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

from pipeline import config as C

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
# How long a dated, upcoming change stays on the feed after it takes effect. Once
# the new rate has been live for a quarter it is no longer news, it is just the
# card, and the catalogue itself carries it.
EXPIRY_DAYS = 90

# Keeps a generated id short enough to read in a diff. The hash suffix below is
# what preserves uniqueness after this truncation.
MAX_SLUG_CHARS = 56

_EPOCH = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)

# The app reads this key and then never uses it — there are zero consumers in
# lib/. Emitting it looks like targeting and targets nothing, so newsgen never
# writes it and validate_item flags anyone who does.
DEAD_TARGETING_KEY = "affected_issuers"


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
# Mirrors the app's parse exactly: take everything before the first dot and
# require it to be bare digits. "3abc" is not a typo we can recover from — the
# app reads it as 0 — so it must fail here, loudly, before it reaches a user.
_MAJOR_RE = re.compile(r"[0-9]+")


def next_version(current: str) -> str:
    """The next news version the app will actually refetch on. "2.0.0" -> "3.0.0".

    Raises ValueError on anything the app would read as 0 (a leading 'v', an
    empty string, a non-numeric head). Guessing here is unrecoverable: a lower
    or unparseable version permanently stops news reaching every installed app,
    and no later publish can undo it for a device that already cached the major.
    """
    if not isinstance(current, str):
        raise ValueError(
            f"news version must be a string, got {type(current).__name__}: {current!r}"
        )
    head = current.strip().split(".")[0]
    if not _MAJOR_RE.fullmatch(head):
        raise ValueError(
            f"cannot read a major version from {current!r} — the app parses "
            f"int.tryParse({head!r}) as 0 and would stop loading the feed"
        )
    return f"{int(head) + 1}.0.0"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def _parse_iso(value: object) -> _dt.datetime | None:
    """ISO-8601 string -> tz-aware datetime, or None when it is not a date.

    Naive inputs are read as UTC so a bare "2026-09-01" from an issuer notice can
    be compared with a full timestamp without raising.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed


def _iso_ok(value: object) -> bool:
    """Matches tools/kredme.py's _iso_ok so this module cannot pass its own gate
    and then fail the publish gate."""
    if value is None:
        return True
    return _parse_iso(value) is not None


def _fmt_iso(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso() -> str:
    return _fmt_iso(_dt.datetime.now(_dt.timezone.utc))


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Words that carry no identity for an event. Two groups, and the second is the
# one that matters: the model re-words the MOVEMENT verb freely between runs, so
# "cap rises to", "cap jumps to" and "cap now" all describe one change.
_EVENT_STOPWORDS = frozenset("""
a an and are as at be by for from get gets in is it its new no not now of on or
the to up was will with you your more less than per card cards credit bank
rise rises rising risen jump jumps jumped cut cuts halve halved drop drops
costs cost costlier change changes changed become becomes goes go made make
makes raise raised limited
""".split())

_CURRENCY = re.compile(r"[₹$]|(?<![a-z])(?:inr|rs)\.?(?![a-z])")
_NUMBERS = re.compile(r"[0-9][0-9,.]*")
_NON_LETTERS = re.compile(r"[^a-z ]+")


def _normalised_subject(title: object) -> str:
    """Reduce a headline to the words that say WHAT changed.

    The feed's ids embed a slug of the headline, and the headline is written by a
    model that re-words it on every run. So one issuer change lands again and
    again under fresh ids: the YES Bank 2026-06-15 fee revision reached 46 copies
    across four days of runs, worded as "Failed auto-debit fee jumps, cap rises to
    INR 5,000", "... fee rises, cap now Rs 5,000" and "... now costs more, cap
    raised to 5,000".

    Currency marks and digits go entirely -- "INR 500", "Rs 500" and "Rs 500" are
    one fee -- as do the movement verbs. What is left is sorted so word order
    cannot fork the key either.
    """
    text = str(title or "").lower()
    text = _CURRENCY.sub(" ", text)
    text = _NUMBERS.sub(" ", text)
    text = _NON_LETTERS.sub(" ", text)
    words = {w for w in text.split() if len(w) > 2 and w not in _EVENT_STOPWORDS}
    return " ".join(sorted(words))


def event_key(item: dict) -> tuple:
    """Identity of the CHANGE an item reports, independent of how it is worded.

    Deliberately does NOT use published_at: that is when we generated the item,
    so it moves every run and would fork the key for one unchanged event -- the
    precise bug this exists to stop.
    """
    if not isinstance(item, dict):
        return ("", "", "", "")
    return (
        str(item.get("source") or item.get("issuer") or ""),
        str(item.get("effective_date") or "")[:10],
        str(item.get("category") or ""),
        _normalised_subject(item.get("title") or item.get("headline")),
    )


def _slug(value: object) -> str:
    return _NON_ALNUM.sub("_", str(value).lower()).strip("_")


def slugify_id(issuer: str, effective_date: str, headline: str) -> str:
    """A stable feed id for one change: 'news_' + [a-z0-9_].

    Determinism is the whole point. The weekly job re-reads the same notice pages
    every week; if the id moved, the same devaluation would land in the feed again
    as a fresh item every single run. Keying on (issuer, effective_date, headline)
    means a re-detected change collapses onto the item already published.

    The short digest is not decoration: the readable part is truncated, and two
    different headlines that share a prefix must not collide onto one id.
    """
    parts = [_slug(issuer), _slug(effective_date) or "undated", _slug(headline)]
    core = "_".join(p for p in parts if p)
    core = _NON_ALNUM.sub("_", core)[:MAX_SLUG_CHARS].strip("_")
    # \x1f separates the fields so ("ab", "c") and ("a", "bc") cannot hash alike.
    raw = "\x1f".join(str(p) for p in (issuer, effective_date, headline))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"news_{core}_{digest}" if core else f"news_{digest}"


# ---------------------------------------------------------------------------
# Card targeting
#
# affected_cards is the ONLY targeting mechanism the app implements. An empty
# list shows the item to everyone, which is noisy but never hides a change from
# the person it affects; a wrong id shows it to the wrong people and hides it
# from the right ones. So every ambiguous case below resolves to "show everyone".
# ---------------------------------------------------------------------------
# Words that carry no identity. Stripping them lets "kotak" match "Kotak Mahindra
# Bank" and "au" match "AU Small Finance Bank" without loosening the match to a
# substring test.
_ISSUER_NOISE = frozenset({
    "bank", "banks", "card", "cards", "limited", "ltd", "pvt", "private",
    "co", "in", "of", "the", "and", "with", "partnership", "small", "finance",
    "technologies", "services", "app",
})
_NAME_NOISE = frozenset({"credit", "card", "cards", "the"})


def _words(text: object) -> list[str]:
    if not isinstance(text, str):
        return []
    return [w for w in _NON_ALNUM.sub(" ", text.lower()).split() if w]


def _issuer_key(name: object) -> frozenset[str]:
    return frozenset(w for w in _words(name) if w not in _ISSUER_NOISE)


def _name_norm(name: object) -> str:
    """'Axis Bank SELECT Credit Card' -> 'axis bank select'."""
    return " ".join(w for w in _words(name) if w not in _NAME_NOISE)


def _card_of(entry: object) -> dict[str, Any] | None:
    """cards.json holds {"card": {...}, "reward_rules": [...]}; tolerate a flat
    card dict too, exactly as tools/kredme.py does."""
    if not isinstance(entry, dict):
        return None
    inner = entry.get("card")
    if isinstance(inner, dict):
        return inner
    return entry if "id" in entry else None


def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for cid in ids:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def map_cards(change: dict, cards: list[dict]) -> list[str]:
    """Resolve a change's issuer + card_names to real card.id strings.

    Returns [] whenever targeting cannot be established safely — the caller must
    read [] as "show this to everyone".

    Three rules, in order:
      - The issuer must resolve. An unknown or blank issuer targets nothing.
      - A named card that matches exactly one card in that issuer's range is
        targeted. A name matching several is AMBIGUOUS and abandons targeting for
        the whole change: picking one of three Vistara cards would hide a
        devaluation from the two holders it actually hits.
      - A named card matching nothing is skipped, not fatal: it simply is not in
        our catalogue, so no user of ours holds it.

    A change that names no cards at all targets that issuer's whole range. That is
    narrower than showing it to all 380 cards' holders and still reaches everyone
    affected. Discontinued cards are deliberately included — someone may still be
    holding one, and a devaluation is exactly what they need to see.
    """
    if not isinstance(change, dict) or not isinstance(cards, list):
        return []

    want = _issuer_key(change.get("issuer"))
    if not want:
        return []

    candidates: list[tuple[str, str, frozenset[str]]] = []
    for entry in cards:
        inner = _card_of(entry)
        if inner is None:
            continue
        cid = inner.get("id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        have = _issuer_key(inner.get("issuer") or inner.get("bank"))
        # Containment in either direction: "idfc" matches "IDFC FIRST Bank", and
        # "IDFC FIRST Bank" matches the entries filed under plain "IDFC Bank".
        # A partial overlap ("First Abu Dhabi" vs "IDFC FIRST") is not a match.
        if not have or not (want <= have or have <= want):
            continue
        norm = _name_norm(inner.get("card_name"))
        candidates.append((cid, norm, frozenset(norm.split())))

    if not candidates:
        return []

    names = change.get("card_names")
    named = [n for n in names if isinstance(n, str) and n.strip()] if isinstance(names, list) else []
    if not named:
        return _dedupe([c[0] for c in candidates])

    out: list[str] = []
    for raw in named:
        query = _name_norm(raw)
        if not query:
            continue
        exact = [cid for cid, norm, _ in candidates if norm == query]
        if exact:
            out.extend(exact)
            continue
        tokens = frozenset(query.split())
        subset = [cid for cid, _, card_tokens in candidates if tokens <= card_tokens]
        if len(subset) == 1:
            out.append(subset[0])
        elif len(subset) > 1:
            return []
    return _dedupe(out)


# ---------------------------------------------------------------------------
# Item construction
# ---------------------------------------------------------------------------
def _text(value: object) -> object:
    """Strip strings; leave anything else untouched so validate_item can report
    what the model actually produced instead of a coerced version of it."""
    return value.strip() if isinstance(value, str) else value


def _category_for(change: dict) -> str:
    stated = change.get("category")
    if isinstance(stated, str) and stated.strip():
        return stated.strip()
    if not change.get("affects_rewards"):
        return "announcement"
    severity = change.get("severity")
    if severity in ("negative", "warning"):
        return "devaluation"
    if severity == "positive":
        return "enhancement"
    return "reward_change"


def _expiry_for(change: dict, published_at: object) -> str | None:
    """EXPIRY_DAYS after the effective date, but only for a change that has not
    taken effect yet.

    An already-effective change has no expiry: it is now simply how the card
    works, and an item that vanishes would leave the user with no explanation for
    a rate they can see in the app.
    """
    effective = _parse_iso(change.get("effective_date"))
    if effective is None:
        return None
    published = _parse_iso(published_at) or _dt.datetime.now(_dt.timezone.utc)
    if effective <= published:
        return None
    return _fmt_iso(effective + _dt.timedelta(days=EXPIRY_DAYS))


def change_to_item(change: dict, *, card_ids: list[str], published_at: str) -> dict:
    """Build one feed item from one detected change.

    Emits only keys inside config.NEWS_VALID_KEYS, and deliberately never emits
    `affected_issuers`: the app parses that key and no widget reads it, so
    populating it produces an item that looks targeted and is not.

    Values the model got wrong (a bogus severity, a blank headline) are passed
    through rather than silently repaired. validate_item is the single gate, and
    rewriting a devaluation's severity to "info" here would ship it as a grey chip
    that nobody notices.
    """
    if not isinstance(change, dict):
        raise TypeError(f"change must be a dict, got {type(change).__name__}")
    if not isinstance(card_ids, list):
        raise TypeError(f"card_ids must be a list, got {type(card_ids).__name__}")

    severity = change.get("severity")
    if severity is None or (isinstance(severity, str) and not severity.strip()):
        severity = "info"

    issuer = change.get("issuer")
    source_url = change.get("source_url")
    # The feed renders source_url as a tappable link inside the app, so it is a
    # publishing surface in its own right. Only an issuer's own page may occupy
    # it — an aggregator link here would ship exactly the sources the catalogue
    # rules ban.
    if not (isinstance(source_url, str) and C.is_issuer_domain(source_url)):
        source_url = None

    item: dict[str, Any] = {
        "id": slugify_id(
            str(issuer or ""),
            str(change.get("effective_date") or ""),
            str(change.get("headline") or ""),
        ),
        "title": _text(change.get("headline")),
        "summary": _text(change.get("summary")),
        "category": _category_for(change),
        "severity": severity,
        "source": (issuer.strip() if isinstance(issuer, str) and issuer.strip() else "KredMe"),
        "source_url": source_url,
        "published_at": published_at,
        "expiry_date": _expiry_for(change, published_at),
        # Copied, never aliased: the caller's list must not change when a later
        # stage edits the item.
        "affected_cards": list(card_ids),
    }

    tags = change.get("tags")
    if isinstance(tags, list) and tags:
        item["tags"] = list(tags)
    action_text = change.get("action_text")
    if isinstance(action_text, str) and action_text.strip():
        item["action_text"] = action_text.strip()
    return item


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_item(item: dict) -> list[str]:
    """Human-readable errors for one feed item. Empty list means valid.

    Deliberately stricter than tools/kredme.py in one place: a missing severity is
    an error here, not a warning. Everything this module emits is machine-written,
    and a devaluation with no severity renders as an informational grey chip.
    """
    if not isinstance(item, dict):
        return [f"item is not an object (got {type(item).__name__})"]

    errors: list[str] = []
    label = item.get("id") if isinstance(item.get("id"), str) else "<no id>"

    for key in sorted(item):
        if key not in C.NEWS_VALID_KEYS:
            errors.append(
                f"{label}: key {key!r} is not read by the app — it would be silently dropped"
            )

    for key in ("id", "title", "summary"):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}: {key!r} must be a non-empty string, got {value!r}")

    severity = item.get("severity")
    if severity not in C.NEWS_SEVERITIES:
        errors.append(
            f"{label}: severity {severity!r} invalid (use one of {sorted(C.NEWS_SEVERITIES)})"
        )

    for key in ("published_at", "expiry_date"):
        if not _iso_ok(item.get(key)):
            errors.append(f"{label}: {key!r} is not ISO-8601 or null: {item.get(key)!r}")

    affected = item.get("affected_cards")
    if affected is not None:
        if not isinstance(affected, list):
            errors.append(
                f"{label}: 'affected_cards' must be a list, got {type(affected).__name__}"
            )
        else:
            for i, cid in enumerate(affected):
                if not isinstance(cid, str) or not cid.strip():
                    errors.append(
                        f"{label}: affected_cards[{i}] must be a card id string, got {cid!r}"
                    )

    # The silent-mis-targeting bug: an author sets affected_issuers, believes the
    # item is scoped to that bank, and ships an item the app shows to everybody.
    if item.get(DEAD_TARGETING_KEY) and not affected:
        errors.append(
            f"{label}: {DEAD_TARGETING_KEY!r} is set but 'affected_cards' is empty — "
            f"the app never reads {DEAD_TARGETING_KEY!r}, so this reaches every user"
        )
    return errors


# ---------------------------------------------------------------------------
# Feed assembly
# ---------------------------------------------------------------------------
def build_feed(items: list[dict], current_version: str, *, updated_at: str) -> dict:
    """Assemble a whole feed at the next major version.

    Expired items are dropped rather than carried: the app filters them at parse
    time anyway, so shipping them is dead weight in every user's cached copy and
    noise in every diff of this file.
    """
    if not isinstance(items, list):
        raise TypeError(f"items must be a list, got {type(items).__name__}")
    version = next_version(current_version)
    now = _parse_iso(updated_at)
    if now is None:
        raise ValueError(f"updated_at must be ISO-8601, got {updated_at!r}")

    kept: list[dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{i}] is not an object (got {type(item).__name__})")
        expiry = _parse_iso(item.get("expiry_date"))
        # Strictly before, so an item expiring at this exact instant still ships.
        # Holding one a moment too long is harmless; dropping one early hides live
        # news from everyone who fetches next.
        if expiry is not None and expiry < now:
            continue
        kept.append(dict(item))

    # Two stable passes: id ascending as the tie-break, then published_at
    # descending. The app sorts too — this is so the committed file diffs cleanly.
    kept.sort(key=lambda it: str(it.get("id") or ""))
    kept.sort(key=lambda it: _parse_iso(it.get("published_at")) or _EPOCH, reverse=True)
    return {"version": version, "updated_at": updated_at, "items": kept}


def merge_feed(existing: dict, new_items: list[dict], *, updated_at: str) -> dict:
    """Fold new items into the live feed, keeping everything still current.

    Dedupe is by id and the NEW item wins, which is what makes a re-detected
    change an update instead of a duplicate — slugify_id being deterministic is
    the other half of that.
    """
    if not isinstance(existing, dict):
        raise TypeError(f"existing feed must be a dict, got {type(existing).__name__}")
    if not isinstance(new_items, list):
        raise TypeError(f"new_items must be a list, got {type(new_items).__name__}")

    old = existing.get("items")
    if old is None:
        old = existing.get("articles")   # the wrapper alias the app also accepts
    if old is None:
        old = []
    if not isinstance(old, list):
        raise ValueError(f"existing feed 'items' must be a list, got {type(old).__name__}")

    # Dedupe on the EVENT, not on the id.
    #
    # Keying on id alone let one issuer change into the feed once per run, because
    # the id embeds a slug of a headline the model rewrites every time. The YES Bank
    # 2026-06-15 fee revision reached 46 copies across four days of runs.
    #
    # Two rules, and they pull in opposite directions on purpose:
    #   - the NEWER item's content wins, so a re-detection can correct wording or
    #     add a card that was missed;
    #   - the FIRST-SEEN id and published_at are kept, so an item a user has already
    #     been notified about cannot notify them again under a new identity.
    #
    # An item with no usable event key falls back to its id, then to its position,
    # so a malformed item is never silently folded into another.
    # PASS 1 — by id, unchanged. A shared id means the same item however it is
    # worded, and the newer copy wins so a correction can land.
    merged: dict[Any, dict] = {}
    for i, item in enumerate(list(old) + list(new_items)):
        if not isinstance(item, dict):
            raise ValueError(f"feed item {i} is not an object (got {type(item).__name__})")
        item_id = item.get("id")
        key = item_id if isinstance(item_id, str) and item_id.strip() else f"\x00no-id-{i}"
        merged[key] = item

    # PASS 2 — by event. Pass 1 cannot see a duplicate whose id moved, and the id
    # embeds a slug of a headline the model rewrites every run, so one issuer
    # change entered the feed once per run: the YES Bank 2026-06-15 fee revision
    # reached 46 copies across four days.
    #
    # Newer content still wins, but the FIRST-SEEN id and published_at are carried
    # over, so an item a user has already been notified about cannot notify them
    # again under a new identity. An item with no usable event key is left alone
    # rather than folded into another.
    by_event: dict[Any, dict] = {}
    for item in merged.values():
        item_id = item.get("id")
        has_id = isinstance(item_id, str) and bool(item_id.strip())
        key = event_key(item)
        # An item with no id, or no usable event key, is left exactly as it is.
        # Without an id there is nothing to say two items are the same PUBLISHED
        # thing rather than two similar ones, and dropping a real change is worse
        # than carrying a duplicate.
        if not has_id or not any(part for part in key):
            by_event[id(item)] = item
            continue
        seen = by_event.get(key)
        if seen is None:
            by_event[key] = item
        else:
            carried = dict(item)
            for sticky in ("id", "published_at"):
                if sticky in seen:
                    carried[sticky] = seen[sticky]
            by_event[key] = carried

    return build_feed(list(by_event.values()), existing.get("version"), updated_at=updated_at)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _changes_from(payload: Any) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("changes")
    if not isinstance(payload, list):
        raise ValueError("changes file must be a list, or an object with a 'changes' list")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Turn detected issuer changes into a candidate news feed.",
    )
    parser.add_argument("--changes", required=True, type=pathlib.Path,
                        help="JSON from the news-change pass: [...] or {'changes': [...]}")
    parser.add_argument("--cards", type=pathlib.Path, default=C.CARDS_JSON,
                        help="card catalogue used to resolve affected_cards")
    parser.add_argument("--feed", type=pathlib.Path, default=C.NEWS_FEED,
                        help="live feed to merge into and read the current version from")
    parser.add_argument("--out", type=pathlib.Path, default=C.CANDIDATES / "feed.json",
                        help="where to write the candidate feed (never the live feed)")
    parser.add_argument("--current-version", default=None,
                        help="override the version to bump from; refuses to guess if unset and unreadable")
    parser.add_argument("--published-at", default=None, help="ISO-8601; defaults to now (UTC)")
    parser.add_argument("--fresh", action="store_true",
                        help="start from an empty item list instead of merging the live feed")
    parser.add_argument("--rewards-only", action="store_true",
                        help="skip changes with affects_rewards=false")
    args = parser.parse_args()

    if not args.changes.exists():
        print(f"error: changes file not found: {args.changes}", file=sys.stderr)
        return 2
    # No catalogue means no targeting, and an untargeted fee alert sent to all 380
    # cards' holders is the noise that makes people mute the feed. Fail instead.
    if not args.cards.exists():
        print(f"error: card catalogue not found: {args.cards}", file=sys.stderr)
        return 2

    try:
        changes = _changes_from(_read_json(args.changes))
        cards = _read_json(args.cards)
    except (json.JSONDecodeError, ValueError, OSError, UnicodeDecodeError) as exc:
        print(f"error: cannot read input: {exc}", file=sys.stderr)
        return 1
    if not isinstance(cards, list):
        print(f"error: {args.cards} must be a JSON list of card entries", file=sys.stderr)
        return 1

    existing: dict[str, Any] = {}
    if args.feed.exists():
        try:
            loaded = _read_json(args.feed)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read feed {args.feed}: {exc}", file=sys.stderr)
            return 1
        if isinstance(loaded, dict):
            existing = loaded

    current = args.current_version or existing.get("version")
    if current is None:
        print(
            "error: no current news version to bump from — pass --current-version. "
            "Guessing would emit a version the app has already seen and news would "
            "never reach a single user again.",
            file=sys.stderr,
        )
        return 2

    published_at = args.published_at or _now_iso()
    kept = existing.get("items")
    if kept is None:
        kept = existing.get("articles")
    base: dict[str, Any] = {"version": current, "items": [] if args.fresh else kept}

    items: list[dict] = []
    skipped = 0
    for change in changes:
        if not isinstance(change, dict):
            print(f"error: change {len(items) + skipped} is not an object", file=sys.stderr)
            return 1
        if args.rewards_only and not change.get("affects_rewards"):
            skipped += 1
            continue
        card_ids = map_cards(change, cards)
        items.append(change_to_item(change, card_ids=card_ids, published_at=published_at))

    try:
        feed = merge_feed(base, items, updated_at=published_at)
    except (TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for item in feed["items"]:
        errors.extend(validate_item(item))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"changes read      {len(changes)}" + (f" ({skipped} skipped)" if skipped else ""))
    print(f"new items         {len(items)}")
    print(f"feed items        {len(feed['items'])}")
    print(f"version           {current} -> {feed['version']}")
    for item in items:
        targets = item.get("affected_cards") or []
        scope = f"{len(targets)} card(s)" if targets else "EVERYONE (untargeted)"
        print(f"  {item['id']}  [{item['severity']}]  {scope}")
    print(f"candidate written {args.out}")

    if errors:
        print(f"\n{len(errors)} validation error(s):", file=sys.stderr)
        for line in errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("validation        clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
