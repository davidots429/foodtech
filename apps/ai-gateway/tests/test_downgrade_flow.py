import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from botocore.session import get_session
from botocore.validate import validate_parameters

from foodtech_ai_gateway.downgrade_flow import DowngradeError, DowngradeFlow, NOTICE_VERSION
from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.policy import Caller, Execution, Feature, GatewayPolicy, Model, Tier, UNCONFIGURED_TIER1
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
        parent.pop("comparison_id")
        execution = self.registry.execution(self.parent)
        self.assertIsNone(execution.job_id)
        self.assertIsNone(execution.job_version)
        self.assertIsNone(execution.comparison_id)

    async def test_comparison_binding_survives_notice_and_rejects_changes(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        job, comparison = str(uuid4()), "a" * 64
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
                binding = {"tenant_id": "customer-a", "job_id": job, "job_version": 7,
                           "comparison_id": comparison}
                created = await client.post("/internal/ai/recipe-requests", json=binding)
                self.assertEqual(created.status_code, 201, created.text)
                parent = created.json()["execution_id"]
                self.assertEqual(self.registry.execution(parent).comparison_id, comparison)
                preview_url = "/internal/ai/recipe-downgrades/preview"
                self.assertEqual((await client.post(preview_url, json={**binding,
                    "parent_execution_id": parent, "comparison_id": "b" * 64})).status_code, 409)
                preview = await client.post(preview_url, json={**binding, "parent_execution_id": parent})
                self.assertEqual(preview.status_code, 201, preview.text)
                quote = preview.json()
                self.assertEqual(quote["comparison_id"], comparison)
                path = f"/internal/ai/recipe-downgrades/{quote['quote_id']}/confirm"
                payload = {**binding, "notice_version": NOTICE_VERSION, "quality_warning_acknowledged": True}
                self.assertEqual((await client.post(path, json={**payload, "comparison_id": None})).status_code, 409)
                confirmed = await client.post(path, json=payload)
                self.assertEqual(confirmed.status_code, 201, confirmed.text)
                self.assertEqual(self.registry.execution(confirmed.json()["execution_id"]).comparison_id, comparison)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

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

    async def test_unconfigured_tier_one_request_and_explicit_downgrade(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        job = str(uuid4())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
                response = await client.post("/internal/ai/recipe-requests", json={
                    "tenant_id": "customer-a", "job_id": job, "job_version": 7})
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()["reason"], "TIER1_MODEL_NOT_CONFIGURED")
                parent_id = response.json()["execution_id"]
                self.assertEqual(self.registry.execution(parent_id).model_id, UNCONFIGURED_TIER1)
                preview_path = "/internal/ai/recipe-downgrades/preview"
                wrong = await client.post(preview_path, json={"tenant_id": "customer-a",
                    "parent_execution_id": parent_id, "job_id": job, "job_version": 8})
                self.assertEqual(wrong.status_code, 409)
                cross_tenant = await client.post(preview_path, json={"tenant_id": "customer-b",
                    "parent_execution_id": parent_id, "job_id": job, "job_version": 7})
                self.assertEqual(cross_tenant.status_code, 409)
                quote = self.flow.preview("customer-a", parent_id)
                self.assertEqual((quote["job_id"], quote["job_version"]), (job, 7))
                child = self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
                self.assertEqual((self.registry.execution(child).job_id, self.registry.execution(child).job_version),
                                 (job, 7))
                self.assertEqual((await client.post("/internal/ai/recipe-requests", json={
                    "tenant_id": "customer-a", "job_id": job, "job_version": True})).status_code, 422)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    async def test_configured_tier_one_request_remains_blocked_until_evidence_is_verified(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        job = str(uuid4())
        body = {"tenant_id": "customer-a", "job_id": job, "job_version": 7}
        try:
            with patch.dict('os.environ', {"FOODTECH_RECIPE_TIER1_MODEL_ID": "tier-one"}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
                    blocked = await client.post("/internal/ai/recipe-requests", json=body)
                    self.assertEqual(blocked.status_code, 201, blocked.text)
                    self.assertEqual(blocked.json()['reason'], 'MODEL_NOT_ACTIVE')
                    self.assertFalse(blocked.json()['execution_available'])
                    self.assertEqual(self.registry.execution(blocked.json()['execution_id']).model_id, 'tier-one')
                    self.resource.table.items[('MODEL#tier-one', 'META')]['state'] = 'ACTIVE'
                    active = await client.post("/internal/ai/recipe-requests", json=body)
                    self.assertEqual(active.status_code, 201, active.text)
                    self.assertEqual(active.json()['reason'], 'RECOMMENDATION_EVIDENCE_UNVERIFIED')
                    self.assertFalse(active.json()['execution_available'])
                    self.assertEqual(self.registry.execution_state(active.json()['execution_id']), 'PENDING')
                    model = AsyncMock()
                    outcome = await app.state.gateway_service.execute_local(
                        Caller('business-api', 'customer-a'), active.json()['execution_id'], 'test', model)
                    self.assertEqual((outcome.state, outcome.error_code), ('DENIED', 'STRUCTURED_RECIPE_REQUIRED'))
                    model.generate.assert_not_called()
            for invalid in ('missing-model', 'llama3.1:8b', UNCONFIGURED_TIER1):
                with patch.dict('os.environ', {"FOODTECH_RECIPE_TIER1_MODEL_ID": invalid}):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway") as client:
                        response = await client.post("/internal/ai/recipe-requests", json=body)
                        self.assertEqual(response.status_code, 503)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    async def test_external_tier_one_requires_transfer_consent_and_never_invokes_provider(self):
        now = datetime.now(timezone.utc)
        self.registry.create_model(Model('external-tier-one', 'openai', Tier.TIER_1, 'ACTIVE', False, True, now, now))
        app.state.mtls_listener = True
        provider = AsyncMock()
        app.state.gateway_service = GatewayService(self.registry, provider=provider)
        try:
            with patch.dict('os.environ', {'FOODTECH_RECIPE_TIER1_MODEL_ID': 'external-tier-one'}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway') as client:
                    response = await client.post('/internal/ai/recipe-requests', json={
                        'tenant_id': 'customer-a', 'job_id': str(uuid4()), 'job_version': 1})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()['reason'], 'EXTERNAL_TRANSFER_CONSENT_REQUIRED')
            self.assertFalse(response.json()['execution_available'])
            self.assertEqual(self.registry.execution_state(response.json()['execution_id']), 'PENDING')
            provider.invoke.assert_not_called()
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    async def test_prior_job_binding_survives_notice_and_rejects_changed_version(self):
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        job, prior = str(uuid4()), str(uuid4())
        binding = {'tenant_id': 'customer-a', 'job_id': job, 'job_version': 7,
                   'prior_job_id': prior, 'prior_job_version': 3}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://gateway') as client:
                created = await client.post('/internal/ai/recipe-requests', json=binding)
                self.assertEqual(created.status_code, 201, created.text)
                parent_id = created.json()['execution_id']
                parent = self.registry.execution(parent_id)
                self.assertEqual((parent.prior_job_id, parent.prior_job_version), (prior, 3))
                preview = '/internal/ai/recipe-downgrades/preview'
                wrong = await client.post(preview, json={**binding, 'parent_execution_id': parent_id, 'prior_job_version': 4})
                self.assertEqual(wrong.status_code, 409)
                result = await client.post(preview, json={**binding, 'parent_execution_id': parent_id})
                self.assertEqual(result.status_code, 201, result.text)
                quote = result.json()
                self.assertEqual((quote['prior_job_id'], quote['prior_job_version']), (prior, 3))
                confirm = f"/internal/ai/recipe-downgrades/{quote['quote_id']}/confirm"
                response = await client.post(confirm, json={**binding, 'notice_version': NOTICE_VERSION,
                                                            'quality_warning_acknowledged': True})
                self.assertEqual(response.status_code, 201, response.text)
                child = self.registry.execution(response.json()['execution_id'])
                self.assertEqual((child.prior_job_id, child.prior_job_version), (prior, 3))
                invalid = await client.post('/internal/ai/recipe-requests', json={**binding, 'prior_job_version': None})
                self.assertEqual(invalid.status_code, 422)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service

    def test_unconfigured_marker_cannot_become_a_model_during_confirmation(self):
        parent = str(uuid4())
        self.registry.create_execution(Execution(parent, "customer-a", Feature.RECIPE_CORRECTION,
            UNCONFIGURED_TIER1, None, "SYNTHETIC", Decimal("0"), job_id=str(uuid4()), job_version=1))
        quote = self.flow.preview("customer-a", parent)
        marker_key = (f"MODEL#{UNCONFIGURED_TIER1}", "META")
        self.client.before_commit = lambda: self.resource.table.items.update({marker_key: {"version": 1}})
        with self.assertRaises(DowngradeError):
            self.flow.confirm("customer-a", quote["quote_id"], NOTICE_VERSION, True)
        self.assertIsNone(self.registry.execution(quote["execution_id"]))

    async def test_customer_to_gateway_notice_and_consent_without_model_call(self):
        from foodtech_business_api import ai, cooking
        from foodtech_business_api.cooking_store import MemoryCookingStore
        from foodtech_business_api.main import app as business_app

        job, prior = str(uuid4()), str(uuid4())
        store = MemoryCookingStore()
        store.items[(cooking._pk("customer-a", job), "META")] = {"job_id": job, "version": 7}
        async def session(request, surface):
            return SimpleNamespace(subject="customer-a", csrf="token")
        evidence = {"job_version": 7, "completeness": "NOT_ASSESSED",
                    "recorded": {"provenance": "SYNTHETIC", "menu": "JEYUK"},
                    "selected_prior_job": {"job_id": prior, "job_version": 3}}
        app.state.mtls_listener = True
        app.state.gateway_service = GatewayService(self.registry)
        try:
            with patch.dict("os.environ", {"FOODTECH_SECURE_COOKIES": "false"}), \
                 patch.object(cooking, "_session", side_effect=session), \
                 patch.object(cooking, "get_cooking_store", return_value=store), \
                 patch("foodtech_business_api.recommendation_evidence.read_evidence", new_callable=AsyncMock,
                       return_value=evidence), \
                 patch.object(ai, "_gateway_settings", return_value=("https://gateway", "", "", "")), \
                 patch.object(ai, "_client", side_effect=lambda *args: httpx.AsyncClient(
                     transport=httpx.ASGITransport(app=app), base_url="https://gateway")):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=business_app),
                                             base_url="http://business") as customer:
                    body = {"job_id": job, "job_version": 7, "prior_job_id": prior, "prior_job_version": 3}
                    headers = {"X-CSRF-Token": "token"}
                    stale = await customer.post("/api/customer/ai/recipe-requests", headers=headers,
                                                json={**body, "prior_job_version": 2})
                    self.assertEqual(stale.status_code, 409)
                    created = await customer.post("/api/customer/ai/recipe-requests", headers=headers, json=body)
                    self.assertEqual(created.status_code, 201, created.text)
                    parent = created.json()["execution_id"]
                    preview = await customer.post(f"/api/customer/ai/recipe-requests/{parent}/local-preview",
                                                  headers=headers, json=body)
                    self.assertEqual(preview.status_code, 201, preview.text)
                    quote = preview.json()
                    path = f"/api/customer/ai/recipe-quotes/{quote['quote_id']}/confirm"
                    consent = {**body, "notice_version": quote["notice_version"],
                               "quality_warning_acknowledged": False}
                    self.assertEqual((await customer.post(path, headers=headers, json=consent)).status_code, 409)
                    consent["quality_warning_acknowledged"] = True
                    confirmed = await customer.post(path, headers=headers, json=consent)
                    self.assertEqual(confirmed.status_code, 201, confirmed.text)
                    self.assertFalse(confirmed.json()["execution_available"])
                    child = self.registry.execution(confirmed.json()["execution_id"])
                    self.assertEqual((child.job_id, child.job_version), (job, 7))
                    self.assertEqual((child.prior_job_id, child.prior_job_version), (prior, 3))
                    self.assertEqual(self.registry.execution_state(child.execution_id), "PENDING")
                    self.assertEqual((await customer.post(path, headers=headers, json=consent)).status_code, 409)
        finally:
            del app.state.mtls_listener
            del app.state.gateway_service
