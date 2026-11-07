import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx

from foodtech_ai_gateway.external_flow import ExternalFlow, ExternalFlowError, FIXTURE_PROMPT
from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.policy import Caller, Credential, Model, Tier
from foodtech_ai_gateway.provider import ProviderResult
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from test_registry import AtomicClient, MemoryResource


class FixtureProvider:
    def __init__(self):
        self.calls = 0

    async def generate(self, provider, model_id, prompt, secret):
        self.calls += 1
        assert (provider, model_id, prompt, bytes(secret)) == (
            "openai", "gpt-4o-mini", FIXTURE_PROMPT, b"synthetic-key")
        return ProviderResult("synthetic answer", Decimal("0.0002"), 20, 30)


class FixtureVault:
    def read(self, credential):
        assert credential.credential_id == "key-1"
        return object()

    def decrypt(self, _record):
        return bytearray(b"synthetic-key")


class ExternalFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        resource = MemoryResource()
        self.registry = DynamoRegistry("registry-test", "2026-11", resource=resource,
                                       client=AtomicClient(resource.table))
        now = datetime.now(timezone.utc)
        self.registry.create_model(Model(
            "gpt-4o-mini", "openai", Tier.TIER_3, "ACTIVE", False, True, now, now,
            input_cost_ceiling_usd=Decimal("0.000001"),
            output_cost_ceiling_usd=Decimal("0.000001"), price_version="approved-v1"))
        self.registry.create_credential(Credential(
            "key-1", "tenant-a", "openai", "ACTIVE", None, "pseudo-a"),
            vault_ref="vault:key-1")
        self.registry.create_limit("TENANT", "tenant-a", Decimal("1"), calls_remaining=1)
        self.registry.create_limit("PROVIDER", "openai", Decimal("1"))
        self.registry.create_limit("PROJECT", "GLOBAL", Decimal("1"))
        self.provider = FixtureProvider()
        self.service = GatewayService(self.registry, vault=FixtureVault(), provider=self.provider)

    async def test_preview_consent_transaction_reservation_and_settlement(self):
        flow = ExternalFlow(self.registry)
        with self.assertRaises(ExternalFlowError):
            flow.preview("tenant-b", "gpt-4o-mini", "key-1")
        quote = flow.preview("tenant-a", "gpt-4o-mini", "key-1")
        self.assertEqual(quote["data_classification"], "SYNTHETIC")
        self.assertGreater(Decimal(quote["estimated_cost_usd"]), Decimal("0.0002"))
        with self.assertRaises(ExternalFlowError):
            flow.confirm("tenant-a", quote["quote_id"], "wrong-notice")
        execution_id = flow.confirm("tenant-a", quote["quote_id"], quote["notice_version"])
        self.assertEqual(self.registry.quote(quote["quote_id"])["state"], "USED")
        self.assertEqual(self.registry.consent(quote["quote_id"]).execution_id, execution_id)
        with self.assertRaises(ExternalFlowError):
            flow.confirm("tenant-a", quote["quote_id"], quote["notice_version"])
        outcome = await self.service.execute_external(Caller("business-api", "tenant-a"),
                                                      execution_id, FIXTURE_PROMPT)
        self.assertEqual(outcome.state, "SUCCEEDED")
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(self.registry.execution_result(execution_id)["actual_cost_usd"],
                         Decimal("0.0002"))
        self.assertEqual(self.registry.budget("tenant-a", "openai").calls_remaining, 0)
        self.assertEqual(self.registry.budget("tenant-a", "openai").user_remaining_usd,
                         Decimal("0.9998"))

    async def test_gateway_route_is_disabled_by_default_and_uses_fixture(self):
        app.state.mtls_listener = True
        app.state.gateway_service = self.service
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://gateway") as client:
                body = {"tenant_id": "tenant-a", "model_id": "gpt-4o-mini",
                        "credential_id": "key-1"}
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("FOODTECH_EXTERNAL_EXECUTION_ENABLED", None)
                    self.assertEqual((await client.post(
                        "/internal/ai/external/preview", json=body)).status_code, 503)
                with patch.dict(os.environ, {"FOODTECH_EXTERNAL_EXECUTION_ENABLED": "true"}):
                    response = await client.post("/internal/ai/external/preview", json=body)
                    self.assertEqual(response.status_code, 201)
                    quote = response.json()
                    refused = await client.post(
                        f"/internal/ai/executions/{quote['execution_id']}/invoke",
                        json={"tenant_id": "tenant-a", "prompt": "untrusted user text"})
                    self.assertEqual(refused.status_code, 403)
                    self.assertEqual(self.provider.calls, 0)
                    confirmed = await client.post(
                        f"/internal/ai/external/quotes/{quote['quote_id']}/confirm",
                        json={"tenant_id": "tenant-a", "notice_version": quote["notice_version"]})
                    self.assertEqual(confirmed.status_code, 200)
                    self.assertEqual(confirmed.json()["state"], "SUCCEEDED")
                    status = await client.get(
                        f"/internal/ai/executions/{quote['execution_id']}/status",
                        params={"tenant_id": "tenant-a"})
                    self.assertEqual(status.json()["actual_cost_usd"], "0.0002")
                    other = await client.get(
                        f"/internal/ai/executions/{quote['execution_id']}/status",
                        params={"tenant_id": "tenant-b"})
                    self.assertEqual(other.status_code, 404)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    async def test_quote_change_before_atomic_confirmation_leaves_no_consent_or_execution(self):
        flow = ExternalFlow(self.registry)
        quote = flow.preview("tenant-a", "gpt-4o-mini", "key-1")
        def race():
            item = self.registry.resource.table.items[(f"QUOTE#{quote['quote_id']}", "META")]
            item["state"] = "USED"
            item["version"] += 1
        self.registry.client.before_commit = race
        with self.assertRaises(ExternalFlowError):
            flow.confirm("tenant-a", quote["quote_id"], quote["notice_version"])
        self.assertIsNone(self.registry.execution(quote["execution_id"]))
        self.assertIsNone(self.registry.consent(quote["quote_id"]))
        self.assertEqual(self.provider.calls, 0)

    async def test_price_version_change_invalidates_quote(self):
        flow = ExternalFlow(self.registry)
        quote = flow.preview("tenant-a", "gpt-4o-mini", "key-1")
        item = self.registry.resource.table.items[("MODEL#gpt-4o-mini", "META")]
        item["price_version"] = "approved-v2"
        item["version"] += 1
        with self.assertRaises(ExternalFlowError) as caught:
            flow.confirm("tenant-a", quote["quote_id"], quote["notice_version"])
        self.assertEqual(caught.exception.code, "QUOTE_CONFIGURATION_CHANGED")
        self.assertEqual(self.registry.quote(quote["quote_id"])["state"], "OPEN")


if __name__ == "__main__":
    unittest.main()
