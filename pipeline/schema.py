#!/usr/bin/env python3
"""
JSON Schemas for the model's structured output, plus the prompts.

Everything the model returns is schema-constrained via `output_config.format`, so
there is no free-text parsing anywhere in this pipeline. A malformed extraction
is impossible by construction; a *wrong* one is what verify.py exists for.

The schemas obey the API's structured-output limits: no recursion, no numeric or
string constraints, and `additionalProperties: false` on every object.

Stdlib only.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
# One observation = one number the issuer states about one card, with the exact
# sentence it came from. We deliberately do NOT ask the model to produce a patch
# to cards.json: it reports what the document says, and diff.py decides what that
# means for our data. Keeping those two jobs apart is what stops a hallucinated
# field name reaching the seed.
EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["card_id", "found", "observations", "notes"],
    "properties": {
        "card_id": {"type": "string"},
        "found": {
            "type": "boolean",
            "description": "True only if this document is about this specific card and states at least one mechanic.",
        },
        "notes": {
            "type": "string",
            "description": "What the document is, and anything that would mislead a later reader. Say plainly if it says nothing useful.",
        },
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "value", "unit", "source_quote", "confidence"],
                "properties": {
                    "field": {
                        "type": "string",
                        "enum": [
                            "base_reward_rate",
                            "reward_unit_spend",
                            "point_value_inr",
                            "annual_fee_inr",
                            "joining_fee_inr",
                            "fee_waiver_spend_inr",
                            "forex_markup_pct",
                            "category_rate",
                            "category_cap",
                            "excluded_category",
                            "lounge_domestic_visits",
                            "lounge_spend_gate_inr",
                            "milestone_spend_inr",
                            "points_expiry_months",
                            "fuel_surcharge_waiver_pct",
                            "card_discontinued",
                        ],
                    },
                    "value": {
                        "type": "string",
                        "description": "The number or token exactly as the issuer states it. Never your own arithmetic.",
                    },
                    "unit": {
                        "type": "string",
                        "enum": [
                            "points", "inr", "percent", "multiplier", "visits",
                            "months", "transactions", "boolean", "mcc", "category_slug",
                        ],
                    },
                    "per_spend_inr": {
                        "type": "string",
                        "description": "For points_per_spend, the spend block, e.g. '150'. Empty when not applicable.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Spend category or MCC this applies to. Empty for card-level fields.",
                    },
                    "source_quote": {
                        "type": "string",
                        "description": "Verbatim sentence from the document containing this number. Must be copied, never paraphrased.",
                    },
                    "effective_date": {
                        "type": "string",
                        "description": "ISO date if the document states one, else empty.",
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
            },
        },
    },
}

EXTRACTION_SYSTEM = """You extract credit-card mechanics from an Indian bank's own published document.

Your output is data for an app that tells real people which card to use at the till. A number \
you get wrong becomes a wrong recommendation, so being silent is always better than being \
approximately right.

Rules, in order of importance:

1. QUOTE, NEVER PARAPHRASE. Every observation carries `source_quote`: the verbatim sentence from \
the document. If you cannot copy a sentence that states the number, do not report the number.

2. NEVER TAKE A NUMBER FROM AN "UP TO" SENTENCE. "Earn up to 10% back" states a ceiling a \
marketer chose, not a mechanic. The same applies to "as much as", "as high as", "save up to". \
Skip these entirely — do not report them at a lower confidence, skip them.

3. REPORT THE ISSUER'S OWN ARITHMETIC, NOT YOURS. If the card earns "4 Reward Points per Rs 150", \
report value="4", unit="points", per_spend_inr="150". Do NOT convert that to a percentage. \
Converting requires a point value the document probably does not state, and a wrong point value \
silently corrupts the rate.

4. DISTINGUISH "THE DOCUMENT SAYS X" FROM "THE DOCUMENT DOES NOT SAY". If the page is a marketing \
landing page with no mechanics, set found=false and say so in notes. Roughly a quarter of Indian \
issuers publish no reward rate anywhere; "not published" is a correct and permanent answer.

5. IF THE DOCUMENT IS ABOUT A DIFFERENT CARD, set found=false. Issuer sites reuse templates and \
a page reached from one card's URL often describes the whole family.

6. AN EFFECTIVE DATE IN THE FUTURE IS STILL WORTH REPORTING — record it in effective_date so the \
pipeline can hold the change until it takes effect."""


# ---------------------------------------------------------------------------
# Verification (pass 2)
# ---------------------------------------------------------------------------
# The handover measured this: a single extraction pass got 100% of its numbers
# right and 78% of its "I could not find this" claims wrong. So pass 2 is not a
# re-run of pass 1 — it is an adversary that must find the quote in the source
# text or the observation dies.
VERIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts", "missed"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["index", "quote_found_verbatim", "supports_value", "refuted", "reason"],
                "properties": {
                    "index": {"type": "integer", "description": "0-based index of the observation being judged."},
                    "quote_found_verbatim": {
                        "type": "boolean",
                        "description": "Does source_quote appear in the document text you were given, word for word?",
                    },
                    "supports_value": {
                        "type": "boolean",
                        "description": "Does that sentence actually state this value, for this card, in this unit?",
                    },
                    "refuted": {"type": "boolean", "description": "True if this observation must not be used."},
                    "reason": {"type": "string"},
                    "corrected_value": {"type": "string", "description": "Empty unless the substance is right but the number is wrong."},
                },
            },
        },
        "missed": {
            "type": "array",
            "description": "Mechanics clearly stated in the document that pass 1 failed to report. This is the point of pass 2.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field", "value", "source_quote"],
                "properties": {
                    "field": {"type": "string"},
                    "value": {"type": "string"},
                    "unit": {"type": "string"},
                    "source_quote": {"type": "string"},
                },
            },
        },
    },
}

VERIFICATION_SYSTEM = """You are auditing another model's extraction from a bank document, before \
the numbers reach a live app.

You have the document text and the claimed observations. Your job has two halves and the second \
is the one people forget.

HALF ONE — refute. For each observation:
  - Search the document for `source_quote`. Does it appear verbatim? Minor whitespace differences \
are fine; a reworded sentence is not, and means refuted=true.
  - Does that sentence state this value, for this card, in this unit? A sentence about a different \
card in the same family, or a different spend category, means refuted=true.
  - Does the sentence contain "up to" / "as much as" / "as high as"? Then it is marketing, not a \
mechanic: refuted=true regardless of how plausible the number looks.
  - Is the value the extractor's own arithmetic rather than the issuer's stated figure \
(e.g. a percentage computed from points x an assumed point value)? refuted=true.
  Default to refuted=true when you are unsure. A missing rate is a visible gap someone fixes; \
a wrong rate is invisible and ships.

HALF TWO — completeness. Re-read the document and list mechanics it clearly states that the \
extraction did not report. On a known-answer control, the first pass got every number it reported \
right and was wrong about 78% of its "I could not find this" claims — it declared unfindable a \
clause that named the card explicitly. Assume the same failure here and go looking."""


# ---------------------------------------------------------------------------
# News change detection
# ---------------------------------------------------------------------------
NEWS_CHANGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["changes"],
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "issuer", "headline", "summary", "severity",
                    "effective_date", "source_quote", "affects_rewards",
                ],
                "properties": {
                    "issuer": {"type": "string"},
                    "headline": {"type": "string", "description": "Under 60 characters, plain English, no jargon."},
                    "summary": {"type": "string", "description": "Two sentences maximum. Lead with what it costs or gains the cardholder."},
                    "card_names": {"type": "array", "items": {"type": "string"}},
                    "severity": {"type": "string", "enum": ["info", "positive", "warning", "negative"]},
                    "effective_date": {"type": "string", "description": "ISO date, or empty if the notice does not state one."},
                    "old_value": {"type": "string"},
                    "new_value": {"type": "string"},
                    "source_quote": {"type": "string", "description": "Verbatim sentence from the notice."},
                    "affects_rewards": {
                        "type": "boolean",
                        "description": "True if this changes what a cardholder earns or pays, as opposed to a process or address change.",
                    },
                },
            },
        }
    },
}

NEWS_CHANGE_SYSTEM = """An Indian bank's notice page changed since we last read it. Identify what \
changed that a cardholder would want to be told about.

Report only changes the notice itself states, with the sentence that states them. Specifically:

  - A revision to fees, interest, forex markup, reward rates, reward caps, category exclusions, \
lounge access, milestones, or benefits. These are what the app is for.
  - NOT: layout changes, marketing refreshes, new card launches, branch addresses, or a reworded \
sentence that means the same thing.

Give the OLD value and the NEW value whenever the notice prints both — banks usually publish these \
as a two-column table, and a change without its old value is not actionable for a reader.

Never take a number from a sentence containing "up to". Write headline and summary for a 25-year-old \
in India who is not a finance person: say what it costs them, not what clause changed."""
