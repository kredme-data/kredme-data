#!/usr/bin/env python3
"""
Tests for pipeline/batch.py — the Message Batches wrapper.

Usage:
    python3 tests/test_batch.py

Stdlib unittest only. Nothing here touches the network or needs the `anthropic`
SDK installed: every API call goes through an injected fake client, which is
also the point — `import pipeline.batch` must work on a bare Python.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline import batch  # noqa: E402
from pipeline import config as C  # noqa: E402
from pipeline import schema as S  # noqa: E402

DOC = "The card earns 4 Reward Points per Rs 150 spent. Annual fee Rs 500."


# ---------------------------------------------------------------------------
# Fakes. Results deliberately arrive as objects, and content blocks as both
# dicts and objects, because the real SDK hands back objects and our fixtures
# should not be the only shape the parser has ever seen.
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class FakeError:
    type: str = ""
    message: str = ""


@dataclasses.dataclass
class FakeMessage:
    content: list[Any]


@dataclasses.dataclass
class FakeInner:
    type: str
    message: Any = None
    error: Any = None


@dataclasses.dataclass
class FakeResult:
    custom_id: Any
    result: Any


@dataclasses.dataclass
class FakeBatch:
    id: Any = "msgbatch_test"
    processing_status: Any = "ended"


@dataclasses.dataclass
class FakeBatches:
    results_list: list[Any] = dataclasses.field(default_factory=list)
    batch: Any = dataclasses.field(default_factory=FakeBatch)
    created: list[Any] = dataclasses.field(default_factory=list)
    retrieved: list[str] = dataclasses.field(default_factory=list)

    def create(self, *, requests: list[Any]) -> Any:
        self.created.append(requests)
        return self.batch

    def retrieve(self, batch_id: str) -> Any:
        self.retrieved.append(batch_id)
        return self.batch

    def results(self, batch_id: str) -> list[Any]:
        return self.results_list


@dataclasses.dataclass
class FakeMessages:
    batches: FakeBatches


@dataclasses.dataclass
class FakeClient:
    messages: FakeMessages


@dataclasses.dataclass
class ExplodingClient:
    """Any attribute touch fails the test — proves dry_run calls nothing."""

    @property
    def messages(self) -> Any:
        raise AssertionError("dry_run must not touch the client")


def fake_client(results: list[Any] | None = None, batch: Any = None) -> FakeClient:
    batches = FakeBatches(results_list=results or [], batch=batch or FakeBatch())
    return FakeClient(messages=FakeMessages(batches=batches))


def succeeded(custom_id: str, payload: Any, *, as_dict: bool = True) -> FakeResult:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    block = {"type": "text", "text": text} if as_dict else FakeTextBlock(text=text)
    return FakeResult(custom_id, FakeInner("succeeded", message=FakeMessage([block])))


@dataclasses.dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


def raw_request(custom_id: str, prefix: str, volatile: str, max_tokens: int) -> dict[str, Any]:
    """A minimal request with exactly known prefix / volatile character counts."""
    return {
        "custom_id": custom_id,
        "params": {
            "model": "claude-opus-5",
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": prefix}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": volatile}]}],
        },
    }


def walk_keys(node: Any) -> set[str]:
    """Every key appearing anywhere in a nested structure."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            found |= walk_keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= walk_keys(item)
    return found


# ---------------------------------------------------------------------------
class TestImportsWithoutSDK(unittest.TestCase):
    def test_module_imports_without_anthropic(self):
        """The lazy-import contract, checked in a FRESH interpreter.

        Asserting on this process's sys.modules cannot test the contract. Where
        the SDK is absent the assertion is vacuous; where it is present — which
        is every CI run, because the workflow pip-installs it before the
        self-tests — any earlier test that so much as probes for the SDK turns
        this red for a reason that has nothing to do with pipeline.batch.

        A subprocess is the only honest form of the question: import
        pipeline.batch and nothing else, then look.
        """
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import pipeline.batch; "
             "print('LEAKED' if 'anthropic' in sys.modules else 'clean')" % str(REPO)],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean", proc.stdout,
                      "pipeline.batch imported the Anthropic SDK at module scope")
        self.assertTrue(callable(batch.build_extract_request))


# ---------------------------------------------------------------------------
class TestCustomId(unittest.TestCase):
    SAFE = re.compile(r"^[A-Za-z0-9_-]+$")

    def test_round_trip_for_id_with_parentheses(self):
        card_id = "bobcard_(bank_of_baroda)_card_eterna"
        req = batch.build_extract_request(card_id, "BoB Eterna", "https://x.test/a", DOC)
        kind, sanitised = batch.parse_custom_id(req["custom_id"])

        self.assertEqual(kind, "extract")
        self.assertEqual(sanitised, batch._sanitise_card_id(card_id))
        self.assertTrue(self.SAFE.match(sanitised), sanitised)
        self.assertNotIn("(", req["custom_id"])
        self.assertLessEqual(len(req["custom_id"]), batch.CUSTOM_ID_MAX_LEN)

    def test_sanitised_half_is_identical_across_kinds(self):
        # extract:: is one byte longer than verify::. If the budget were derived
        # per-kind, the two passes would truncate differently and the caller's
        # custom_id -> card_id map would break between pass 1 and pass 2.
        card_id = "au_bank_(co-branded_with_aditya_birla_finance_limited)_aditya_birla_au_bank_credit_cards"
        e = batch.parse_custom_id(
            batch.build_extract_request(card_id, "X", "https://x.test/a", DOC)["custom_id"]
        )
        v = batch.parse_custom_id(batch.build_verify_request(card_id, DOC, [])["custom_id"])
        self.assertEqual(e[1], v[1])
        self.assertEqual(e[0], "extract")
        self.assertEqual(v[0], "verify")

    def test_already_safe_id_is_left_alone(self):
        self.assertEqual(batch._sanitise_card_id("hdfc_regalia_gold"), "hdfc_regalia_gold")

    def test_length_ceiling_is_enforced_on_the_longest_real_id(self):
        card_id = "au_bank_(co-branded_with_aditya_birla_finance_limited)_aditya_birla_au_bank_credit_cards"
        self.assertEqual(len(card_id), 88)
        cid = batch.build_extract_request(card_id, "X", "https://x.test/a", DOC)["custom_id"]
        self.assertLessEqual(len(cid), batch.CUSTOM_ID_MAX_LEN)
        self.assertTrue(re.match(r"^[A-Za-z0-9_:-]+$", cid))

    def test_boundary_ids_at_and_over_the_budget(self):
        at_budget = "a" * batch.ID_MAX_LEN
        over_budget = "a" * (batch.ID_MAX_LEN + 1)
        self.assertEqual(batch._sanitise_card_id(at_budget), at_budget)
        self.assertEqual(len(batch._sanitise_card_id(over_budget)), batch.ID_MAX_LEN)
        self.assertNotEqual(batch._sanitise_card_id(over_budget), at_budget)

    def test_sanitisation_is_deterministic(self):
        card_id = "fpl_technologies_pvt._ltd._onecard_metal"
        self.assertEqual(batch._sanitise_card_id(card_id), batch._sanitise_card_id(card_id))

    def test_ids_differing_only_in_unsafe_chars_do_not_collide(self):
        # Without the hash suffix both of these sanitise to 'a_b_' and two cards
        # would share one batch result.
        self.assertNotEqual(batch._sanitise_card_id("a(b)"), batch._sanitise_card_id("a_b_"))

    def test_id_of_only_unsafe_characters_still_yields_a_legal_id(self):
        cid = batch._sanitise_card_id("()./")
        self.assertTrue(self.SAFE.match(cid), cid)
        self.assertLessEqual(len(cid), batch.ID_MAX_LEN)

    def test_every_real_card_id_maps_to_a_unique_legal_custom_id(self):
        cards_json = C.CARDS_JSON
        if not cards_json.exists():
            self.skipTest("seed/cards.json not present")
        entries = json.loads(cards_json.read_text(encoding="utf-8"))
        ids = [e["card"]["id"] for e in entries]
        self.assertGreater(len(ids), 300)

        custom_ids = [batch._custom_id("extract", i) for i in ids]
        self.assertEqual(len(set(custom_ids)), len(ids), "custom_id collision across the catalogue")
        for cid in custom_ids:
            self.assertLessEqual(len(cid), batch.CUSTOM_ID_MAX_LEN)
            kind, sanitised = batch.parse_custom_id(cid)
            self.assertEqual(kind, "extract")
            self.assertTrue(self.SAFE.match(sanitised), cid)

    def test_empty_and_blank_card_ids_are_rejected(self):
        for bad in ("", "   ", "\n"):
            with self.assertRaises(ValueError):
                batch._sanitise_card_id(bad)

    def test_non_string_card_id_is_rejected(self):
        with self.assertRaises(ValueError):
            batch._sanitise_card_id(None)
        with self.assertRaises(ValueError):
            batch._sanitise_card_id(12345)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            batch._custom_id("summarise", "hdfc_regalia")

    def test_parse_custom_id_rejects_malformed_ids(self):
        bad = [
            "",
            "extract",
            "extract:hdfc",            # single colon
            "extract::",               # empty id half
            "::hdfc",                  # no kind
            "summarise::hdfc",         # unknown kind
            "extract::hdfc regalia",   # space is outside the charset
            "extract::hdfc(regalia)",  # parentheses are outside the charset
            "extract::" + "a" * (batch.ID_MAX_LEN + 1),
        ]
        for value in bad:
            with self.assertRaises(ValueError, msg=value):
                batch.parse_custom_id(value)

    def test_parse_custom_id_rejects_non_strings(self):
        for value in (None, 42, ["extract::hdfc"]):
            with self.assertRaises(ValueError):
                batch.parse_custom_id(value)


# ---------------------------------------------------------------------------
class TestRequestShape(unittest.TestCase):
    def setUp(self):
        self.req = batch.build_extract_request(
            "hdfc_regalia_gold", "HDFC Regalia Gold", "https://www.hdfc.bank.in/x", DOC
        )
        self.params = self.req["params"]

    def test_returns_a_plain_dict(self):
        self.assertIsInstance(self.req, dict)
        self.assertEqual(set(self.req), {"custom_id", "params"})

    def test_cache_control_is_on_the_last_system_block_only(self):
        blocks = self.params["system"]
        self.assertGreaterEqual(len(blocks), 2)
        for block in blocks[:-1]:
            self.assertNotIn("cache_control", block)
        self.assertEqual(blocks[-1]["cache_control"], {"type": "ephemeral"})

    def test_no_sampling_parameters_anywhere(self):
        # temperature / top_p / top_k are rejected with a 400 on claude-opus-5.
        keys = walk_keys(self.req)
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, keys)

    def test_no_fallbacks_parameter(self):
        # The Batches API rejects `fallbacks` outright.
        self.assertNotIn("fallbacks", walk_keys(self.req))

    def test_no_assistant_prefill(self):
        roles = [m["role"] for m in self.params["messages"]]
        self.assertEqual(roles, ["user"])

    def test_system_blocks_are_byte_identical_across_cards(self):
        # Caching is a prefix match: one card-specific byte in a system block
        # would invalidate the cache for all 380 requests.
        other = batch.build_extract_request(
            "axis_magnus", "Axis Magnus", "https://www.axis.bank.in/y", "Different doc entirely."
        )
        self.assertEqual(
            json.dumps(self.params["system"], sort_keys=True),
            json.dumps(other["params"]["system"], sort_keys=True),
        )

    def test_card_specific_data_lives_after_the_cached_prefix(self):
        prefix = batch._prefix_text(self.params)
        volatile = batch._volatile_text(self.params)
        for token in ("hdfc_regalia_gold", "HDFC Regalia Gold", "https://www.hdfc.bank.in/x", DOC):
            self.assertNotIn(token, prefix)
            self.assertIn(token, volatile)

    def test_output_config_carries_the_schema_and_effort(self):
        oc = self.params["output_config"]
        self.assertEqual(oc["format"]["type"], "json_schema")
        self.assertIs(oc["format"]["schema"], S.EXTRACTION_SCHEMA)
        self.assertEqual(oc["effort"], C.EXTRACT_EFFORT)

    def test_model_and_max_tokens_come_from_config(self):
        self.assertEqual(self.params["model"], C.EXTRACT_MODEL)
        self.assertEqual(self.params["max_tokens"], C.EXTRACT_MAX_TOKENS)

    def test_cached_prefix_clears_the_minimum_cacheable_length(self):
        # Below 512 tokens claude-opus-5 silently declines to cache — no error,
        # just a full-price bill on every request. Guard both prefixes.
        for req in (
            self.req,
            batch.build_verify_request("hdfc_regalia_gold", DOC, []),
        ):
            tokens = batch._est_tokens(batch._prefix_text(req["params"]))
            self.assertGreater(tokens, batch.MIN_CACHEABLE_PREFIX_TOKENS, req["custom_id"])

    def test_empty_document_is_rejected(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                batch.build_extract_request("hdfc_regalia", "HDFC", "https://x.test/a", bad)
        with self.assertRaises(ValueError):
            batch.build_extract_request("hdfc_regalia", "HDFC", "https://x.test/a", None)

    def test_empty_card_name_and_url_are_rejected(self):
        with self.assertRaises(ValueError):
            batch.build_extract_request("hdfc_regalia", "", "https://x.test/a", DOC)
        with self.assertRaises(ValueError):
            batch.build_extract_request("hdfc_regalia", "HDFC", "", DOC)


# ---------------------------------------------------------------------------
class TestVerifyRequest(unittest.TestCase):
    OBS = [{"field": "annual_fee_inr", "value": "500", "source_quote": "Annual fee Rs 500."}]

    def test_uses_the_verification_prompt_and_schema(self):
        req = batch.build_verify_request("hdfc_regalia", DOC, self.OBS)
        params = req["params"]
        self.assertEqual(batch.parse_custom_id(req["custom_id"])[0], "verify")
        self.assertIn(S.VERIFICATION_SYSTEM, batch._prefix_text(params))
        self.assertIs(params["output_config"]["format"]["schema"], S.VERIFICATION_SCHEMA)
        self.assertEqual(params["output_config"]["effort"], C.VERIFY_EFFORT)
        self.assertEqual(params["model"], C.VERIFY_MODEL)
        self.assertEqual(params["max_tokens"], C.VERIFY_MAX_TOKENS)

    def test_document_and_observations_reach_the_user_turn(self):
        volatile = batch._volatile_text(
            batch.build_verify_request("hdfc_regalia", DOC, self.OBS)["params"]
        )
        self.assertIn(DOC, volatile)
        self.assertIn("annual_fee_inr", volatile)

    def test_observation_serialisation_is_key_order_stable(self):
        # A re-submit must produce byte-identical requests or it re-pays.
        a = batch.build_verify_request("c", DOC, [{"field": "x", "value": "1"}])
        b = batch.build_verify_request("c", DOC, [{"value": "1", "field": "x"}])
        self.assertEqual(a, b)

    def test_empty_observation_list_is_allowed(self):
        # Pass 2 still has work to do with zero claims: the completeness half.
        req = batch.build_verify_request("hdfc_regalia", DOC, [])
        self.assertEqual(batch.parse_custom_id(req["custom_id"])[0], "verify")

    def test_wrong_observation_types_are_rejected(self):
        with self.assertRaises(ValueError):
            batch.build_verify_request("c", DOC, {"field": "x"})
        with self.assertRaises(ValueError):
            batch.build_verify_request("c", DOC, "observations")
        with self.assertRaises(ValueError):
            batch.build_verify_request("c", DOC, [{"field": "x"}, "not a dict"])

    def test_empty_document_is_rejected(self):
        with self.assertRaises(ValueError):
            batch.build_verify_request("c", "", self.OBS)


# ---------------------------------------------------------------------------
class TestEstimateCost(unittest.TestCase):
    # 360 prefix chars / 3.6 == 100 tokens; 180 volatile chars == 50 tokens.
    PREFIX = "P" * 360
    VOLATILE = "V" * 180

    def two_requests(self) -> list[dict[str, Any]]:
        return [
            raw_request("extract::a", self.PREFIX, self.VOLATILE, 1000),
            raw_request("extract::b", self.PREFIX, self.VOLATILE, 1000),
        ]

    def test_hand_computed_case(self):
        est = batch.estimate_cost(self.two_requests(), "claude-opus-5")

        self.assertEqual(est["requests"], 2)
        self.assertEqual(est["est_input_tokens"], 300)     # (100 + 50) x 2
        self.assertEqual(est["est_output_tokens"], 2000)   # ceiling: 1000 x 2

        # 100x1.25 write + 50 volatile, then 100x0.1 read + 50 volatile = 235
        expected_uncached = 235 / 1_000_000 * 5.00 + 2000 / 1_000_000 * 25.00
        self.assertAlmostEqual(est["est_usd_uncached"], expected_uncached, places=12)
        self.assertAlmostEqual(est["est_usd"], expected_uncached * 0.5, places=12)

    def test_batch_price_is_exactly_half_the_standard_price(self):
        est = batch.estimate_cost(self.two_requests(), "claude-opus-5")
        self.assertEqual(est["est_usd"], est["est_usd_uncached"] * C.BATCH_DISCOUNT)
        self.assertEqual(C.BATCH_DISCOUNT, 0.5)

    def test_second_request_pays_the_cache_read_rate(self):
        one = batch.estimate_cost([self.two_requests()[0]], "claude-opus-5")
        two = batch.estimate_cost(self.two_requests(), "claude-opus-5")

        # Marginal input cost of request 2 is 100x0.1 + 50 = 60 token-units,
        # against 100x1.25 + 50 = 175 for request 1.
        marginal_input = (two["est_usd_uncached"] - one["est_usd_uncached"]) - (
            1000 / 1_000_000 * 25.00
        )
        self.assertAlmostEqual(marginal_input, 60 / 1_000_000 * 5.00, places=12)

    def test_a_different_prefix_pays_its_own_cache_write(self):
        mixed = [
            raw_request("extract::a", self.PREFIX, self.VOLATILE, 1000),
            raw_request("verify::a", "Q" * 360, self.VOLATILE, 1000),
        ]
        est = batch.estimate_cost(mixed, "claude-opus-5")
        # Both prefixes are first-seen, so both are writes: 2 x (125 + 50) = 350.
        expected = 350 / 1_000_000 * 5.00 + 2000 / 1_000_000 * 25.00
        self.assertAlmostEqual(est["est_usd_uncached"], expected, places=12)

    def test_token_estimate_uses_the_documented_ratio(self):
        self.assertEqual(batch._est_tokens("x" * 36), 10)
        self.assertEqual(batch._est_tokens(""), 0)
        self.assertEqual(batch._est_tokens("x"), 1)  # ceiling, never zero-rated

    def test_empty_request_list_is_free(self):
        est = batch.estimate_cost([], "claude-opus-5")
        self.assertEqual(est["requests"], 0)
        self.assertEqual(est["est_input_tokens"], 0)
        self.assertEqual(est["est_output_tokens"], 0)
        self.assertEqual(est["est_usd"], 0.0)
        self.assertEqual(est["est_usd_uncached"], 0.0)

    def test_returned_keys_are_exactly_the_contract(self):
        est = batch.estimate_cost(self.two_requests(), "claude-opus-5")
        self.assertEqual(
            set(est),
            {"requests", "est_input_tokens", "est_output_tokens",
             "est_typical_output_tokens", "est_usd", "est_usd_ceiling",
             "est_usd_uncached"},
        )

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            batch.estimate_cost(self.two_requests(), "gpt-4")

    def test_every_priced_model_is_usable(self):
        for model in C.PRICING:
            est = batch.estimate_cost(self.two_requests(), model)
            self.assertGreater(est["est_usd"], 0.0)

    def test_malformed_requests_are_rejected(self):
        with self.assertRaises(ValueError):
            batch.estimate_cost("not a list", "claude-opus-5")
        with self.assertRaises(ValueError):
            batch.estimate_cost(["not a dict"], "claude-opus-5")
        with self.assertRaises(ValueError):
            batch.estimate_cost([{"custom_id": "extract::a"}], "claude-opus-5")
        with self.assertRaises(ValueError):
            batch.estimate_cost(
                [{"custom_id": "extract::a", "params": {"model": "m", "max_tokens": 0}}],
                "claude-opus-5",
            )
        with self.assertRaises(ValueError):
            batch.estimate_cost(
                [{"custom_id": "extract::a", "params": {"model": "m", "max_tokens": "16000"}}],
                "claude-opus-5",
            )

    def test_prices_a_real_built_request(self):
        req = batch.build_extract_request("hdfc_regalia", "HDFC", "https://x.test/a", DOC)
        est = batch.estimate_cost([req], C.EXTRACT_MODEL)
        self.assertEqual(est["requests"], 1)
        self.assertGreater(est["est_input_tokens"], batch.MIN_CACHEABLE_PREFIX_TOKENS)
        self.assertEqual(est["est_output_tokens"], C.EXTRACT_MAX_TOKENS)


# ---------------------------------------------------------------------------
class TestChunking(unittest.TestCase):
    def reqs(self, n: int) -> list[dict[str, Any]]:
        return [raw_request(f"extract::c{i}", "P" * 10, "V" * 10, 100) for i in range(n)]

    def test_splits_exactly_at_the_request_count_boundary(self):
        with mock.patch.object(batch, "MAX_REQUESTS_PER_BATCH", 3):
            self.assertEqual([len(c) for c in batch._chunk_requests(self.reqs(2))], [2])
            self.assertEqual([len(c) for c in batch._chunk_requests(self.reqs(3))], [3])
            self.assertEqual([len(c) for c in batch._chunk_requests(self.reqs(4))], [3, 1])
            self.assertEqual([len(c) for c in batch._chunk_requests(self.reqs(7))], [3, 3, 1])

    def test_splits_on_the_byte_ceiling(self):
        reqs = self.reqs(4)
        size = len(json.dumps(reqs[0], ensure_ascii=False).encode("utf-8"))
        with mock.patch.object(batch, "MAX_BYTES_PER_BATCH", size * 2):
            self.assertEqual([len(c) for c in batch._chunk_requests(reqs)], [2, 2])
        with mock.patch.object(batch, "MAX_BYTES_PER_BATCH", size * 2 - 1):
            self.assertEqual([len(c) for c in batch._chunk_requests(reqs)], [1, 1, 1, 1])

    def test_a_single_oversized_request_is_not_dropped(self):
        reqs = self.reqs(1)
        with mock.patch.object(batch, "MAX_BYTES_PER_BATCH", 1):
            self.assertEqual([len(c) for c in batch._chunk_requests(reqs)], [1])

    def test_empty_input_produces_no_chunks(self):
        self.assertEqual(batch._chunk_requests([]), [])

    def test_ceilings_stay_under_the_documented_api_limits(self):
        self.assertLess(batch.MAX_REQUESTS_PER_BATCH, 100_000)
        self.assertLess(batch.MAX_BYTES_PER_BATCH, 256 * 1024 * 1024)


# ---------------------------------------------------------------------------
class TestSubmit(unittest.TestCase):
    def reqs(self, n: int = 2) -> list[dict[str, Any]]:
        return [raw_request(f"extract::c{i}", "P" * 10, "V" * 10, 100) for i in range(n)]

    def test_dry_run_returns_a_sentinel_and_touches_no_client(self):
        self.assertEqual(
            batch.submit(self.reqs(), client=ExplodingClient(), dry_run=True), "dry-run"
        )

    def test_dry_run_still_validates(self):
        with self.assertRaises(ValueError):
            batch.submit([], client=ExplodingClient(), dry_run=True)

    def test_happy_path_returns_the_batch_id_and_sends_every_request(self):
        client = fake_client(batch=FakeBatch(id="msgbatch_01abc"))
        requests = self.reqs(3)
        self.assertEqual(batch.submit(requests, client=client), "msgbatch_01abc")

        sent = client.messages.batches.created
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], requests)

    def test_empty_and_non_list_inputs_are_rejected(self):
        for bad in ([], None, "requests", {}):
            with self.assertRaises(ValueError):
                batch.submit(bad, client=fake_client())

    def test_duplicate_custom_ids_are_rejected(self):
        dupes = [
            raw_request("extract::same", "P", "V", 100),
            raw_request("extract::same", "P", "V", 100),
        ]
        # Left alone this is silent data loss: collect() keys by custom_id, so
        # the second result would overwrite the first.
        with self.assertRaises(ValueError):
            batch.submit(dupes, client=fake_client())

    def test_malformed_custom_id_is_rejected(self):
        bad = [raw_request("hdfc regalia", "P", "V", 100)]
        with self.assertRaises(ValueError):
            batch.submit(bad, client=fake_client())

    def test_request_without_a_model_is_rejected(self):
        bad = self.reqs(1)
        del bad[0]["params"]["model"]
        with self.assertRaises(ValueError):
            batch.submit(bad, client=fake_client())

    def test_refuses_to_submit_more_than_one_batch(self):
        client = fake_client()
        with mock.patch.object(batch, "MAX_REQUESTS_PER_BATCH", 2):
            with self.assertRaises(ValueError) as ctx:
                batch.submit(self.reqs(5), client=client)
        self.assertIn("3 batches", str(ctx.exception))
        # Nothing may have been submitted, or the extra batches would be orphaned.
        self.assertEqual(client.messages.batches.created, [])

    def test_missing_batch_id_in_the_response_is_an_error(self):
        client = fake_client(batch=FakeBatch(id=None))
        with self.assertRaises(ValueError):
            batch.submit(self.reqs(), client=client)


# ---------------------------------------------------------------------------
class TestPoll(unittest.TestCase):
    def test_returns_processing_status(self):
        for status in ("in_progress", "canceling", "ended"):
            client = fake_client(batch=FakeBatch(processing_status=status))
            self.assertEqual(batch.poll("msgbatch_01", client=client), status)
            self.assertEqual(client.messages.batches.retrieved, ["msgbatch_01"])

    def test_blank_batch_id_is_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                batch.poll(bad, client=fake_client())

    def test_missing_processing_status_is_an_error(self):
        client = fake_client(batch=FakeBatch(processing_status=None))
        with self.assertRaises(ValueError):
            batch.poll("msgbatch_01", client=client)


# ---------------------------------------------------------------------------
class TestCollect(unittest.TestCase):
    def test_keys_by_custom_id(self):
        client = fake_client([
            succeeded("extract::a", {"card_id": "a", "found": True}),
            succeeded("extract::b", {"card_id": "b", "found": False}),
        ])
        out = batch.collect("msgbatch_01", client=client)
        self.assertEqual(set(out), {"extract::a", "extract::b"})
        self.assertEqual(out["extract::a"]["data"]["card_id"], "a")
        self.assertEqual(out["extract::b"]["data"]["found"], False)

    def test_correct_when_results_arrive_in_reverse_order(self):
        # THE trap. Anything that pairs results with inputs by position silently
        # writes card B's reward rate onto card A.
        payloads = [{"card_id": f"card{i}", "found": True} for i in range(5)]
        forward = [succeeded(f"extract::card{i}", p) for i, p in enumerate(payloads)]

        straight = batch.collect("b1", client=fake_client(list(forward)))
        reversed_ = batch.collect("b2", client=fake_client(list(reversed(forward))))

        self.assertEqual(straight, reversed_)
        for i in range(5):
            self.assertEqual(reversed_[f"extract::card{i}"]["data"]["card_id"], f"card{i}")

    def test_shuffled_order_is_also_correct(self):
        forward = [succeeded(f"extract::card{i}", {"card_id": f"card{i}"}) for i in range(6)]
        shuffled = [forward[3], forward[0], forward[5], forward[1], forward[4], forward[2]]
        out = batch.collect("b1", client=fake_client(shuffled))
        for i in range(6):
            self.assertEqual(out[f"extract::card{i}"]["data"]["card_id"], f"card{i}")

    def test_every_entry_has_the_full_contract_shape(self):
        client = fake_client([
            succeeded("extract::a", {"card_id": "a"}),
            FakeResult("extract::b", FakeInner("expired")),
        ])
        for entry in batch.collect("b1", client=client).values():
            self.assertEqual(set(entry), {"ok", "data", "error"})
            self.assertIsInstance(entry["ok"], bool)
            self.assertIsInstance(entry["error"], str)

    def test_success_carries_no_error_string(self):
        out = batch.collect("b1", client=fake_client([succeeded("extract::a", {"x": 1})]))
        self.assertTrue(out["extract::a"]["ok"])
        self.assertEqual(out["extract::a"]["error"], "")

    def test_errored_canceled_and_expired_are_distinguishable(self):
        client = fake_client([
            FakeResult(
                "extract::e",
                FakeInner("errored", error=FakeError(type="invalid_request", message="bad schema")),
            ),
            FakeResult("extract::c", FakeInner("canceled")),
            FakeResult("extract::x", FakeInner("expired")),
        ])
        out = batch.collect("b1", client=client)

        for key in ("extract::e", "extract::c", "extract::x"):
            self.assertFalse(out[key]["ok"])
            self.assertIsNone(out[key]["data"])

        self.assertIn("errored", out["extract::e"]["error"])
        self.assertIn("invalid_request", out["extract::e"]["error"])
        self.assertIn("bad schema", out["extract::e"]["error"])
        self.assertEqual(out["extract::c"]["error"], "canceled")
        self.assertEqual(out["extract::x"]["error"], "expired")
        self.assertEqual(
            len({out[k]["error"] for k in ("extract::e", "extract::c", "extract::x")}), 3
        )

    def test_malformed_json_in_a_succeeded_result_never_raises(self):
        client = fake_client([
            succeeded("extract::bad", "{not json at all"),
            succeeded("extract::good", {"card_id": "good"}),
        ])
        out = batch.collect("b1", client=client)

        self.assertFalse(out["extract::bad"]["ok"])
        self.assertIsNone(out["extract::bad"]["data"])
        self.assertIn("malformed JSON", out["extract::bad"]["error"])
        # One unparseable card must not throw away the ones we already paid for.
        self.assertTrue(out["extract::good"]["ok"])

    def test_truncated_json_never_raises(self):
        out = batch.collect(
            "b1", client=fake_client([succeeded("extract::a", '{"card_id": "a", "found":')])
        )
        self.assertFalse(out["extract::a"]["ok"])
        self.assertIn("malformed JSON", out["extract::a"]["error"])

    def test_succeeded_with_no_text_block_is_reported_not_raised(self):
        empty = FakeResult("extract::a", FakeInner("succeeded", message=FakeMessage([])))
        out = batch.collect("b1", client=fake_client([empty]))
        self.assertFalse(out["extract::a"]["ok"])
        self.assertIn("no text block", out["extract::a"]["error"])

    def test_object_shaped_content_blocks_are_read(self):
        client = fake_client([succeeded("extract::a", {"card_id": "a"}, as_dict=False)])
        out = batch.collect("b1", client=client)
        self.assertTrue(out["extract::a"]["ok"])
        self.assertEqual(out["extract::a"]["data"], {"card_id": "a"})

    def test_non_text_blocks_are_skipped_before_the_text_block(self):
        message = FakeMessage([
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": '{"card_id": "a"}'},
        ])
        client = fake_client([FakeResult("extract::a", FakeInner("succeeded", message=message))])
        self.assertEqual(batch.collect("b1", client=client)["extract::a"]["data"], {"card_id": "a"})

    def test_unknown_result_type_is_reported(self):
        client = fake_client([FakeResult("extract::a", FakeInner("teleported"))])
        out = batch.collect("b1", client=client)
        self.assertFalse(out["extract::a"]["ok"])
        self.assertIn("unknown result type", out["extract::a"]["error"])

    def test_duplicate_custom_id_in_results_is_an_error(self):
        # Silently keeping the last one would drop a card we paid for.
        client = fake_client([
            succeeded("extract::a", {"card_id": "a"}),
            succeeded("extract::a", {"card_id": "a2"}),
        ])
        with self.assertRaises(ValueError):
            batch.collect("b1", client=client)

    def test_result_without_a_custom_id_is_an_error(self):
        for bad in (None, "", 42):
            client = fake_client([FakeResult(bad, FakeInner("succeeded", message=FakeMessage([])))])
            with self.assertRaises(ValueError):
                batch.collect("b1", client=client)

    def test_blank_batch_id_is_rejected(self):
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                batch.collect(bad, client=fake_client())

    def test_empty_batch_yields_an_empty_map(self):
        self.assertEqual(batch.collect("b1", client=fake_client([])), {})

    def test_round_trips_a_real_extraction_payload(self):
        payload = {
            "card_id": "hdfc_regalia",
            "found": True,
            "notes": "Issuer T&C page.",
            "observations": [
                {
                    "field": "base_reward_rate",
                    "value": "4",
                    "unit": "points",
                    "per_spend_inr": "150",
                    "source_quote": "Earn 4 Reward Points per Rs 150 spent.",
                    "confidence": "high",
                }
            ],
        }
        out = batch.collect("b1", client=fake_client([succeeded("extract::hdfc_regalia", payload)]))
        entry = out["extract::hdfc_regalia"]
        self.assertTrue(entry["ok"])
        # 'N points per Rs X' must survive as points x unit, never a percentage.
        obs = entry["data"]["observations"][0]
        self.assertEqual(obs["value"], "4")
        self.assertEqual(obs["per_spend_inr"], "150")


# ---------------------------------------------------------------------------
class TestCLI(unittest.TestCase):
    """The entry point, run the way the workflow runs it.

    Only a subprocess catches this class of bug: `python3 pipeline/batch.py`
    puts pipeline/ on sys.path instead of the repo root, so the package imports
    fail at module load — invisible to every in-process test in this file.
    """

    SCRIPT = str(REPO / "pipeline" / "batch.py")

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, self.SCRIPT, *args],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
        )

    def test_help_runs(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for sub in ("submit", "poll", "collect"):
            self.assertIn(sub, proc.stdout)

    def test_dry_run_submit_needs_no_sdk_and_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "requests.json"
            reqs = [
                batch.build_extract_request("hdfc_regalia", "HDFC", "https://x.test/a", DOC),
                batch.build_extract_request("axis_magnus", "Axis", "https://x.test/b", DOC),
            ]
            path.write_text(json.dumps(reqs), encoding="utf-8")

            proc = self.run_cli("submit", "--requests", str(path), "--dry-run")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("2 extract requests", proc.stdout)
            self.assertIn("dry run", proc.stdout)

    def test_data_errors_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = pathlib.Path(tmp) / "bad.json"
            bad.write_text('[{"custom_id": "not a valid id", "params": {}}]', encoding="utf-8")
            self.assertEqual(self.run_cli("submit", "--requests", str(bad), "--dry-run").returncode, 1)

            missing = pathlib.Path(tmp) / "absent.json"
            self.assertEqual(
                self.run_cli("submit", "--requests", str(missing), "--dry-run").returncode, 1
            )

            notjson = pathlib.Path(tmp) / "notjson.json"
            notjson.write_text("{{{", encoding="utf-8")
            self.assertEqual(
                self.run_cli("submit", "--requests", str(notjson), "--dry-run").returncode, 1
            )

    def test_missing_sdk_exits_2(self):
        # poll has to reach the API, so with no `anthropic` installed this is
        # the config/missing-dependency exit code, not a data error.
        #
        # find_spec, NOT `import anthropic`. Importing it to ask whether it is
        # installed LEAVES IT IN sys.modules, and TestImportsWithoutSDK below then
        # fails on a polluted interpreter — which is invisible on a laptop with no
        # SDK installed and fatal in CI, where the workflow pip-installs it before
        # running the suite. That is exactly how it reached the first live run.
        if importlib.util.find_spec("anthropic") is not None:
            self.skipTest("anthropic is installed; the missing-dependency path cannot fire")
        proc = self.run_cli("poll", "--batch-id", "msgbatch_01")
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("anthropic", proc.stderr)


# ---------------------------------------------------------------------------
class TestKindOfBatch(unittest.TestCase):
    def test_single_kind_is_returned(self):
        reqs = [raw_request("extract::a", "P", "V", 100), raw_request("extract::b", "P", "V", 100)]
        self.assertEqual(batch._kind_of(reqs), "extract")

    def test_mixed_kinds_are_rejected(self):
        # extract and verify have different cached prefixes and different
        # collect-time handling; mixing them in one batch state entry loses that.
        reqs = [raw_request("extract::a", "P", "V", 100), raw_request("verify::a", "P", "V", 100)]
        with self.assertRaises(ValueError):
            batch._kind_of(reqs)


# ---------------------------------------------------------------------------
# The news path. Added alongside the card pipeline: the watched notice pages run
# SYNCHRONOUSLY rather than through the Batch API, because a batch can take 24h and
# the whole value of a devaluation alert is that it is timely.
# ---------------------------------------------------------------------------
NEWS_DOC = "Effective 28-08-2026: Dynamic Currency Conversion markup revised to 3.5% from 1.5%. " * 20


class _FakeMessage:
    def __init__(self, text):
        self.content = [{"type": "text", "text": text}]


class _FakeClient:
    """Records the params it was called with so tests can assert on the request."""

    def __init__(self, text='{"changes": []}'):
        self._text = text
        self.calls = []

        outer = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                return _FakeMessage(outer._text)

        self.messages = _Messages()


class _ExplodingClient:
    class messages:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("network down")


class TestNewsRequest(unittest.TestCase):
    def test_custom_id_round_trips(self):
        r = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        self.assertEqual(batch.parse_custom_id(r["custom_id"]), ("news", "axis"))

    def test_news_is_a_recognised_kind(self):
        self.assertIn("news", batch.KINDS)

    def test_adding_news_did_not_shrink_the_id_budget(self):
        # ID_MAX_LEN is derived from the longest kind. 'news' is shorter than
        # 'extract', so adding it must not have changed the budget — if it had,
        # every previously-built custom_id would truncate differently and the
        # {custom_id -> card_id} map would silently stop matching.
        self.assertEqual(
            batch.ID_MAX_LEN,
            batch.CUSTOM_ID_MAX_LEN - len("extract") - len(batch._SEPARATOR),
        )

    def test_uses_the_news_schema_and_prompt(self):
        r = batch.build_news_request("hdfc", "https://www.hdfc.bank.in/x", NEWS_DOC)
        fmt = r["params"]["output_config"]["format"]
        self.assertEqual(fmt["schema"], S.NEWS_CHANGE_SCHEMA)
        self.assertIn(S.NEWS_CHANGE_SYSTEM, r["params"]["system"][0]["text"])

    def test_prefix_is_cached(self):
        r = batch.build_news_request("hdfc", "https://www.hdfc.bank.in/x", NEWS_DOC)
        self.assertIn("cache_control", r["params"]["system"][-1])

    def test_no_sampling_params(self):
        # temperature / top_p / top_k are a 400 on claude-opus-5.
        r = batch.build_news_request("hdfc", "https://www.hdfc.bank.in/x", NEWS_DOC)
        for k in ("temperature", "top_p", "top_k"):
            self.assertNotIn(k, r["params"])

    def test_rejects_empty_issuer_and_url(self):
        for issuer, url in (("", "https://www.hdfc.bank.in/x"), ("hdfc", ""), ("  ", "https://x.io")):
            with self.assertRaises(ValueError):
                batch.build_news_request(issuer, url, NEWS_DOC)

    def test_rejects_empty_document(self):
        with self.assertRaises(ValueError):
            batch.build_news_request("hdfc", "https://www.hdfc.bank.in/x", "")

    def test_two_issuers_share_the_cached_prefix(self):
        a = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        b = batch.build_news_request("hdfc", "https://www.hdfc.bank.in/y", NEWS_DOC)
        self.assertEqual(a["params"]["system"], b["params"]["system"])


class TestRunSync(unittest.TestCase):
    def test_empty_input_calls_nothing(self):
        c = _FakeClient()
        self.assertEqual(batch.run_sync([], client=c), [])
        self.assertEqual(c.calls, [])

    def test_rejects_non_list(self):
        with self.assertRaises(ValueError):
            batch.run_sync({"not": "a list"}, client=_FakeClient())  # type: ignore[arg-type]

    def test_parses_the_body(self):
        c = _FakeClient('{"changes": [{"issuer": "axis"}]}')
        r = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        self.assertEqual(batch.run_sync([r], client=c), [{"changes": [{"issuer": "axis"}]}])

    def test_preserves_input_order(self):
        # Unlike collect(), which must key by custom_id because batch results arrive
        # unordered, run_sync is sequential and positional — assert that holds.
        c = _FakeClient('{"changes": []}')
        reqs = [
            batch.build_news_request(i, f"https://www.{i}.bank.in/x", NEWS_DOC)
            for i in ("axis", "hdfc", "sbi")
        ]
        out = batch.run_sync(reqs, client=c)
        self.assertEqual(len(out), 3)
        sent = [call["messages"][0]["content"][0]["text"] for call in c.calls]
        self.assertTrue(sent[0].startswith("issuer: axis"))
        self.assertTrue(sent[1].startswith("issuer: hdfc"))
        self.assertTrue(sent[2].startswith("issuer: sbi"))

    def test_one_failure_does_not_lose_the_others(self):
        # One unreachable issuer must not sink the other eleven.
        r = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        self.assertEqual(batch.run_sync([r], client=_ExplodingClient()), [{}])

    def test_malformed_json_body_becomes_empty_not_a_raise(self):
        c = _FakeClient("this is not json")
        r = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        self.assertEqual(batch.run_sync([r], client=c), [{}])

    def test_missing_text_block_becomes_empty(self):
        class NoText:
            content = [{"type": "thinking", "thinking": ""}]

        class C2:
            class messages:
                @staticmethod
                def create(**kwargs):
                    return NoText()

        r = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        self.assertEqual(batch.run_sync([r], client=C2()), [{}])


class TestNewsCostEstimate(unittest.TestCase):
    def test_news_kind_uses_its_typical_output(self):
        r = batch.build_news_request("axis", "https://www.axis.bank.in/x", NEWS_DOC)
        est = batch.estimate_cost([r], C.EXTRACT_MODEL)
        self.assertEqual(est["est_typical_output_tokens"], C.TYPICAL_OUTPUT_TOKENS["news"])
        # The ceiling must always bound the likely figure, never the other way round.
        self.assertLess(est["est_usd"], est["est_usd_ceiling"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
