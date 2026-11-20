import json
import unittest
from dataclasses import replace
from decimal import Decimal

import httpx

from foodtech_ai_gateway.ollama import OllamaClient
from foodtech_ai_gateway.recommendation_generation import generate_candidate
from foodtech_contracts.recommendation_guard import Evidence
from foodtech_contracts.recommendation_output import JeyukBaseline, TonkatsuBaseline


class RecommendationGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.evidence = Evidence("COMPLETE", "PASSED", True, False)
        self.baseline = TonkatsuBaseline("STANDARD", 300)

    async def run_responses(self, responses, evidence=None, baseline=None):
        calls = []
        async def invoke(messages, schema):
            calls.append((messages, schema))
            return responses[len(calls) - 1]
        result = await generate_candidate(invoke, evidence=evidence or self.evidence,
                                          baseline=baseline or self.baseline, context={"job": "synthetic"})
        return result, calls

    async def test_invalid_output_retries_then_returns_review_candidate(self):
        result, calls = await self.run_responses(["not JSON", '{}',
            '{"temperature_c":170,"duration_seconds":300}'])
        self.assertEqual((result["status"], result["attempts"]), ("NEEDS_REVIEW", 3))
        self.assertEqual(len(calls), 3)
        self.assertFalse(calls[0][1]["additionalProperties"])
        self.assertEqual(calls[0][1]["properties"]["duration_seconds"]["type"], "integer")

    async def test_exhaustion_never_exposes_invalid_output(self):
        result, calls = await self.run_responses(['sensitive invalid response'] * 3)
        self.assertEqual((result["status"], result["attempts"], result["data"]), ("FAILED", 3, {}))
        self.assertNotIn('sensitive invalid response', str(result))
        self.assertEqual(len(calls), 3)

    async def test_rejected_candidate_is_not_retried(self):
        result, calls = await self.run_responses(['{"temperature_c":300,"duration_seconds":300}'])
        self.assertEqual((result["status"], result["attempts"], result["data"]), ("REJECTED", 1, {}))
        self.assertEqual(len(calls), 1)

    async def test_missing_evidence_and_jam_baseline_do_not_call_provider(self):
        result, calls = await self.run_responses([], evidence=replace(self.evidence, severe_data_gap=True))
        self.assertEqual((result["status"], result["attempts"]), ("INDETERMINATE", 0))
        self.assertEqual(calls, [])
        jeyuk = JeyukBaseline("SMALL", Decimal("200"), None, 480, Decimal("1.8"), 60, 60, True)
        result, calls = await self.run_responses([], baseline=jeyuk)
        self.assertEqual((result["status"], result["attempts"]), ("INDETERMINATE", 0))
        self.assertIn("PREVIOUS_JAM_BASELINE_UNAVAILABLE", result["reasons"])
        self.assertEqual(calls, [])

    async def test_ollama_receives_schema_and_nostream(self):
        def handler(request):
            value = json.loads(request.content)
            self.assertEqual(request.url.path, "/api/chat")
            self.assertEqual(value["format"], {"type": "object"})
            self.assertIs(value["stream"], False)
            return httpx.Response(200, json={"done": True, "message": {"role": "assistant", "content": "{}"}})
        client = OllamaClient(transport=httpx.MockTransport(handler))
        response = await client.structured_chat("synthetic-model", [], {"type": "object"})
        self.assertEqual(response.text, "{}")
