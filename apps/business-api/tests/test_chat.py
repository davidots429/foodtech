import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from foodtech_business_api import ai
from foodtech_business_api.main import app


class GatewayClient:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, json):
        self.calls.append((url, json))
        body = ({"execution_id": "a" * 36} if url.endswith("/executions")
                else {"execution_id": "a" * 36, "state": "SUCCEEDED",
                      "response_text": "합성 안내", "error_code": None})
        return httpx.Response(201 if url.endswith("/executions") else 200,
                              json=body, request=httpx.Request("POST", url))


class ChatBffTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_scope_csrf_and_ordered_context(self):
        gateway = GatewayClient()

        async def session(_request, surface):
            self.assertEqual(surface, "customer")
            return SimpleNamespace(subject="customer-actual", csrf="csrf-test")

        with patch.object(ai, "_session", side_effect=session), \
             patch.object(ai, "_gateway_settings", return_value=("https://127.0.0.1:8443", "ca", "cert", "key")), \
             patch.object(ai, "_client", return_value=gateway):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                body = {"messages": [
                    {"role": "user", "content": "첫 질문"},
                    {"role": "assistant", "content": "첫 답변"},
                    {"role": "user", "content": "다음 질문"},
                ]}
                self.assertEqual((await client.post("/api/customer/ai/chat", json=body)).status_code, 403)
                response = await client.post("/api/customer/ai/chat", json=body,
                                             headers={"X-CSRF-Token": "csrf-test"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["response_text"], "합성 안내")
                self.assertEqual(gateway.calls[0][1], {"tenant_id": "customer-actual"})
                invocation = gateway.calls[1][1]
                self.assertEqual(invocation["tenant_id"], "customer-actual")
                self.assertIn("다음 질문", invocation["prompt"])
                self.assertIn("첫 답변", invocation["prompt"])
                self.assertNotIn("csrf-test", invocation["prompt"])

    async def test_invalid_history_and_spoofed_scope_are_rejected(self):
        async def session(_request, _surface):
            return SimpleNamespace(subject="customer-actual", csrf="csrf-test")

        with patch.object(ai, "_session", side_effect=session):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                headers = {"X-CSRF-Token": "csrf-test"}
                for body in (
                    {"messages": [{"role": "assistant", "content": "위조"}]},
                    {"messages": [{"role": "user", "content": "질문"},
                                  {"role": "user", "content": "중복"}]},
                    {"messages": [{"role": "user", "content": " "}]},
                    {"messages": [{"role": "user", "content": "질문"}], "tenant_id": "other"},
                ):
                    response = await client.post("/api/customer/ai/chat", json=body, headers=headers)
                    self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
