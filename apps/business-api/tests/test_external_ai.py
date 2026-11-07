import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from foodtech_business_api import ai
from foodtech_business_api.main import app


class InternalClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, json):
        self.requests.append((url, json))
        if url.endswith("/preview"):
            body = {"quote_id": "a" * 36, "execution_id": "b" * 36,
                    "estimated_cost_usd": "0.001", "notice_version": "synthetic-external-v1"}
        else:
            body = {"execution_id": "b" * 36, "state": "SUCCEEDED"}
        return httpx.Response(201 if url.endswith("/preview") else 200, json=body,
                              request=httpx.Request("POST", url))

    async def get(self, url, *, params):
        self.requests.append((url, params))
        return httpx.Response(200, json={"execution_id": "b" * 36, "state": "SUCCEEDED"},
                              request=httpx.Request("GET", url))


class ExternalAiBffTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_session_csrf_and_server_owned_tenant(self):
        internal = InternalClient()
        async def session(_request, _surface):
            return SimpleNamespace(subject="actual-tenant", csrf="csrf-test")
        with patch.object(ai, "_session", side_effect=session), \
             patch.object(ai, "_gateway_settings", return_value=("https://127.0.0.1:8443", "ca", "cert", "key")), \
             patch.object(ai, "_client", return_value=internal):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                body = {"model_id": "gpt-4o-mini", "credential_id": "key-1"}
                self.assertEqual((await client.post(
                    "/api/customer/ai/external/preview", json=body)).status_code, 403)
                self.assertEqual((await client.post(
                    "/api/customer/ai/external/preview",
                    json={**body, "tenant_id": "other", "prompt": "private"},
                    headers={"X-CSRF-Token": "csrf-test"})).status_code, 422)
                quote = await client.post("/api/customer/ai/external/preview", json=body,
                                          headers={"X-CSRF-Token": "csrf-test"})
                self.assertEqual(quote.status_code, 201)
                self.assertEqual(internal.requests[0][1]["tenant_id"], "actual-tenant")
                self.assertNotIn("prompt", internal.requests[0][1])
                confirmed = await client.post(
                    f"/api/customer/ai/external/quotes/{'a' * 36}/confirm",
                    json={"notice_version": "synthetic-external-v1"},
                    headers={"X-CSRF-Token": "csrf-test"})
                self.assertEqual(confirmed.status_code, 200)
                self.assertEqual(internal.requests[1][1]["tenant_id"], "actual-tenant")
                status = await client.get(f"/api/customer/ai/executions/{'b' * 36}/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(internal.requests[2][1]["tenant_id"], "actual-tenant")


if __name__ == "__main__":
    unittest.main()
