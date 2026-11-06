import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.ollama import OllamaClient, OllamaFailure, OllamaResult
from foodtech_ai_gateway.policy import Model, Tier
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from foodtech_business_api import ai as business_ai
from foodtech_business_api.main import app as business_app
from test_registry import AtomicClient, MemoryResource


class FakeOllama:
    async def generate(self, model_id, prompt):
        if prompt == "fail":
            raise OllamaFailure("OLLAMA_TIMEOUT")
        return OllamaResult("합성 답변", 4, 3)


class ExecutionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        resource = MemoryResource()
        client = AtomicClient(resource.table)
        registry = DynamoRegistry("test-ai", "2026-11", resource=resource, client=client,
                                  local_models={"llama3.1:8b"})
        now = datetime.now(timezone.utc)
        registry.create_model(Model("llama3.1:8b", "ollama", Tier.TIER_3, "ACTIVE",
                                    True, True, now, now))
        registry.create_limit("TENANT", "customer-1", Decimal("0"), calls_remaining=2)
        registry.create_limit("PROVIDER", "ollama", Decimal("0"))
        registry.create_limit("PROJECT", "GLOBAL", Decimal("0"))
        self.registry = registry
        app.state.gateway_service = GatewayService(registry)
        app.state.ollama_client = FakeOllama()
        app.state.mtls_listener = True

    def tearDown(self):
        for name in ("gateway_service", "ollama_client", "mtls_listener"):
            delattr(app.state, name)

    async def test_gateway_create_invoke_and_persist_response(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://gateway.local") as client:
            created = await client.post("/internal/ai/executions",
                                        json={"tenant_id": "customer-1"})
            self.assertEqual(created.status_code, 201)
            execution_id = created.json()["execution_id"]
            response = await client.post(f"/internal/ai/executions/{execution_id}/invoke",
                                         json={"tenant_id": "customer-1", "prompt": "안내"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["response_text"], "합성 답변")
            self.assertEqual(self.registry.execution_state(execution_id), "SUCCEEDED")
            self.assertEqual(self.registry.execution_result(execution_id)["response_text"],
                             "합성 답변")

    async def test_failure_persisted_and_cannot_invoke_twice(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://gateway.local") as client:
            created = await client.post("/internal/ai/executions",
                                        json={"tenant_id": "customer-1"})
            execution_id = created.json()["execution_id"]
            target = f"/internal/ai/executions/{execution_id}/invoke"
            failed = await client.post(target, json={"tenant_id": "customer-1", "prompt": "fail"})
            self.assertEqual(failed.json()["error_code"], "OLLAMA_TIMEOUT")
            self.assertEqual(self.registry.execution_state(execution_id), "FAILED")
            self.assertEqual(self.registry.execution_result(execution_id)["error_code"],
                             "OLLAMA_TIMEOUT")
            retried = await client.post(target, json={"tenant_id": "customer-1", "prompt": "안내"})
            self.assertEqual(retried.status_code, 403)

    async def test_plain_http_and_wrong_tenant_are_denied(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://gateway.local") as client:
            response = await client.post("/internal/ai/executions",
                                         json={"tenant_id": "customer-1"})
            self.assertEqual(response.status_code, 401)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://gateway.local") as client:
            created = await client.post("/internal/ai/executions",
                                        json={"tenant_id": "customer-1"})
            execution_id = created.json()["execution_id"]
            response = await client.post(f"/internal/ai/executions/{execution_id}/invoke",
                                         json={"tenant_id": "customer-2", "prompt": "안내"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.registry.execution_state(execution_id), "PENDING")

    async def test_ollama_http_contract(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/api/generate")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "llama3.1:8b")
            self.assertEqual(body["options"]["num_ctx"], 4096)
            self.assertEqual(body["options"]["num_predict"], 128)
            self.assertFalse(body["stream"])
            return httpx.Response(200, json={"response": "모델 응답", "done": True,
                                             "prompt_eval_count": 4, "eval_count": 3})

        ollama = OllamaClient(transport=httpx.MockTransport(handler))
        result = await ollama.generate("llama3.1:8b", "시험 입력")
        self.assertEqual(result.text, "모델 응답")

    async def test_business_api_to_gateway_to_local_model_flow(self):
        async def session(request, surface):
            self.assertEqual(surface, "customer")
            return SimpleNamespace(subject="customer-1")

        def gateway_client(ca, cert, key):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://gateway.local")

        with patch.object(business_ai, "_session", session), \
             patch.object(business_ai, "_csrf", lambda request, session: None), \
             patch.object(business_ai, "_gateway_settings",
                          lambda: ("https://gateway.local", "unused-ca", "unused-cert", "unused-key")), \
             patch.object(business_ai, "_client", gateway_client):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=business_app),
                                         base_url="http://business.local") as client:
                response = await client.post("/api/customer/ai/guidance",
                                             json={"prompt": "합성 조리 안내"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response_text"], "합성 답변")
        execution_id = response.json()["execution_id"]
        self.assertEqual(self.registry.execution_result(execution_id)["state"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
