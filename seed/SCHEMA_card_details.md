# `seed/card_details.md` — prose the card screen renders as-is

`seed/card_details.json` is an object keyed by `card.id` from `cards.json`. Every key must be a
card id and every value must be an object; a string or a list at the root is an ERROR in
`tools/kredme.py validate`, because the app skips such entries silently. Comments therefore live
in this file, not in the JSON.

The app has read this file since 2026-04-25 (`lib/services/card_details_service.dart`), so
**publishing it needs no app release**. A missing file, a missing card and a missing key all render
as an absent section — never as a zero.

## The one rule

**Nothing in this file is parsed into a number.** `benefits.summary` is a `List<String>` rendered
verbatim, with no label, on the card's Benefits tab. It cannot reach the recommendation engine and
cannot change a card's ranking. That is exactly why it is the right home for a rate the engine
must not act on — and exactly why it must never be used to restore a number that was removed from
`cards.json`. If a rate belongs in the ranking, it belongs in `reward_rules`. If it does not, it
belongs here, in words, with the condition attached.

Only write a sentence an issuer document supports, and cite the document in the last string.

## Why the four PhonePe cards are the first entries

They pay their headline rate only on spends routed through the PhonePe app. SBI identifies those
by *"PhonePe's Merchant Identification Number (MID) / Terminal Identification Number (TID)/
Virtual Payment Address (VPA)"*; HDFC's terms say *"Eligible only for transactions initiated on
the PhonePe App"*. A phone cannot see any of that, and no field on a `reward_rules` row can hold
it, so the app deliberately does not rank on the higher rate — it would be wrong on most
purchases. The rate is real and worth knowing, so it is stated here in words instead.

A user reported this on 22 Aug 2026: the app was showing PhonePe SBI SELECT BLACK at 10% on every
merchant in the travel section, including Uber, where the card actually pays 5%. The rules that
did that were removed; these sentences are what replaces them.

## Shape

```json
{
  "<card_id>": {
    "benefits": { "summary": ["A sentence.", "Another sentence.", "Source: … — <url>"] }
  }
}
```

`benefits.summary` is the only key used today. The reader also models `highlights[]` (first tab),
`benefits.lounge`, `benefits.insurance`, `benefits.golf`, `fees.*`, `eligibility.*` and
`redemption.*` — see `card_detail_screen.dart`. Do not put the same sentence in both `highlights`
and `benefits.summary`: they render on different tabs and read as duplication.

## Publishing

Drop the file in `seed/` on `dev`, commit, then `validate` and `promote`. `promote` copies it and
**rebuilds the manifest entry from the file on disk**. Never hand-write a manifest entry: a
declared file that 404s stops card syncing for every user on every cold start, forever.
