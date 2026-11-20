import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_ai_gateway.downgrade_flow import DowngradeError, DowngradeFlow, NOTICE_VERSION
from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.policy import Caller, Execution, Feature, GatewayPolicy, Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from test_registry import AtomicClient, MemoryResource


class DowngradeFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.resource = MemoryResource()
        self.client = AtomicClient(self.resource.table)
        self.registry = DynamoRegistry("test", "2026-11", resource=self.resource, client=self.client,
                                       local_models={"llama3.1:8b", "tier-one"})
        now = datetime.now(timezone.utc)
        self.registry.create_model(Model("llama3.1:8b", "ollama", Tier.TIER_3, "ACTIVE", True, True, now, now))
        self.registry.create_model(Model("tier-one", "ollama", Tier.TIER_1, "INACTIVE", True, True, now, now))
        self.registry.create_limit("TENANT", "customer-a", Decimal("0"), calls_remaining=3)
        self.registry.create_limit("PROVIDER", "ollama", Decimal("0"))
        self.registry.create_limit("PROJECT", "GLOBAL", Decimal("0"))
        self.parent = str(uuid4())
        self.registry.create_execution(Execution(self.parent, "customer-a", Feature.RECIPE_CORRECTION,
                                                  "tier-one", None, "SYNTHETIC", Decimal("0")))
        self.flow = DowngradeFlow(self.registry)

    def test_confirmation_is_specific_single_use_and_authorizable(self):
        quote = self.flow.preview("customer-a", self.parent)
        self.assertEqual((quote["transmission_location"], quote["estimated_cost_usd"]), ("LOCAL", "0"))
        for tenant, version, ack in (("other", NOTICE_VERSION, True), ("customer-a", "wrong", True),
                                     ("customer-a", NOTICE_VERSION, False)):
            with self.assertRaises(DowngradeError):
                self.flow.confirm(tenant, quote["quote_id"], version, ack)
        execution_id = self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        execution = self.registry.execution(execution_id)
        consent = self.registry.consent(quote["quote_id"])
        self.assertNotEqual(execution_id, self.parent)
        self.assertEqual((execution.parent_execution_id, consent.execution_id), (self.parent, execution_id))
        self.assertTrue(GatewayPolicy(self.registry).authorize(Caller("business-api", "customer-a"), execution_id).allowed)
        shape = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems").input_shape
        validate_parameters({"TransactItems": self.client.calls[-1],
                             "ClientRequestToken": "11111111-1111-1111-1111-111111111111"}, shape)
        with self.assertRaises(DowngradeError):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)

    def test_parent_must_be_owned_tier_one_and_blocked(self):
        with self.assertRaises(DowngradeError): self.flow.preview("customer-b", self.parent)
        item = self.resource.table.items[(f"EXEC#{self.parent}", "META")]
        item["feature"] = "GENERAL_GUIDE"
        with self.assertRaises(DowngradeError): self.flow.preview("customer-a", self.parent)
        item["feature"] = "RECIPE_CORRECTION"
        model = self.resource.table.items[("MODEL#tier-one", "META")]
        model["state"] = "ACTIVE"; model["version"] += 1
        with self.assertRaisesRegex(DowngradeError, "PARENT_NOT_BLOCKED"):
            self.flow.preview("customer-a", self.parent)

    def test_expired_notice_and_non_synthetic_parent_are_rejected(self):
        parent = self.resource.table.items[(f"EXEC#{self.parent}", "META")]
        parent["data_classification"] = "CUSTOMER"
        with self.assertRaises(DowngradeError): self.flow.preview("customer-a", self.parent)
        parent["data_classification"] = "SYNTHETIC"
        quote = self.flow.preview("customer-a", self.parent)
        item = self.resource.table.items[(f"QUOTE#{quote['quote_id']}", "META")]
        item["expires"] = 0
        with self.assertRaises(DowngradeError):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        self.assertIsNone(self.registry.execution(quote["execution_id"]))

    def test_configuration_change_or_limit_exhaustion_blocks_confirmation(self):
        quote = self.flow.preview("customer-a", self.parent)
        target = self.resource.table.items[("MODEL#llama3.1:8b", "META")]
        target["state"] = "INACTIVE"; target["version"] += 1
        with self.assertRaises(DowngradeError):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        self.assertIsNone(self.registry.execution(quote["execution_id"]))
        target["state"] = "ACTIVE"; target["version"] += 1
        key = ("LIMIT#2026-11#TENANT#customer-a", "META")
        self.client.before_commit = lambda: self.resource.table.items[key].update(calls_remaining=0, version=2)
        with self.assertRaises(DowngradeError):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        self.assertIsNone(self.registry.execution(quote["execution_id"]))
        self.assertIsNone(self.registry.consent(quote["quote_id"]))

    def test_parent_change_during_confirmation_cannot_create_consent(self):
        quote = self.flow.preview("customer-a", self.parent)
        parent = self.resource.table.items[(f"EXEC#{self.parent}", "META")]
        self.client.before_commit = lambda: parent.update(version=parent["version"] + 1, state="FAILED")
        with self.assertRaises(DowngradeError):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        self.assertIsNone(self.registry.consent(quote["quote_id"]))
        self.assertIsNone(self.registry.execution(quote["execution_id"]))

    def test_bound_job_is_preserved_and_changed_binding_is_rejected(self):
        parent = self.resource.table.items[(f"EXEC#{self.parent}", "META")]
        parent.update(job_id=str(uuid4()), job_version=7)
        quote = self.flow.preview("customer-a", self.parent)
        self.assertEqual((quote["job_id"], quote["job_version"]), (parent["job_id"], 7))
        parent.update(job_version=8, version=parent["version"] + 1)
        with self.assertRaisesRegex(DowngradeError, "PARENT_CHANGED"):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        self.assertIsNone(self.registry.execution(quote["execution_id"]))
        quote = self.flow.preview("customer-a", self.parent)
        execution_id = self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        child = self.registry.execution(execution_id)
        self.assertEqual((child.job_id, child.job_version), (parent["job_id"], 8))

    def test_legacy_execution_without_job_fields_remains_readable(self):
        parent = self.resource.table.items[(f"EXEC#{self.parent}", "META")]
        parent.pop("job_id")
        parent.pop("job_version")
        execution = self.registry.execution(self.parent)
        self.assertIsNone(execution.job_id)
        self.assertIsNone(execution.job_version)

    async def test_routes_require_mtls_and_explicit_boolean(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
                response = await client.post("/internal/ai/recipe-downgrades/preview",
                    json={"tenant_id": "customer-a", "parent_execution_id": self.parent})
                self.assertEqual(response.status_code, 201, response.text)
                quote = response.json()
                target = f"/internal/ai/recipe-downgrades/{quote['quote_id']}/confirm"
                body = {"tenant_id": "customer-a", "notice_version": NOTICE_VERSION}
                self.assertEqual((await client.post(target, json={**body, "quality_warning_acknowledged": "true"})).status_code, 422)
                self.assertEqual((await client.post(target, json={**body, "quality_warning_acknowledged": False})).status_code, 409)
                confirmed = await client.post(target, json={**body, "quality_warning_acknowledged": True})
                self.assertEqual(confirmed.status_code, 201, confirmed.text)
                self.assertEqual(confirmed.json()["state"], "PENDING")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
                self.assertEqual((await client.post("/internal/ai/recipe-downgrades/preview",
                    json={"tenant_id": "customer-a", "parent_execution_id": self.parent})).status_code, 401)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service
