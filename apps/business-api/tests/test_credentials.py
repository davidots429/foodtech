import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from foodtech_business_api import credentials
from foodtech_business_api.main import app


class InternalClient:
    def __init__(self):
        self.last_json = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        return httpx.Response(200, json={"key_version": "2026-11", "algorithm": "RSA-OAEP-SHA-256",
                                         "public_key_spki_base64": "public", "expires_at": "future"},
                              request=httpx.Request("GET", "https://127.0.0.1"))

    async def post(self, _url, *, json):
        self.last_json = json
        return httpx.Response(201, json={"credential_id": "generated-id", "state": "ACTIVE"},
                              request=httpx.Request("POST", "https://127.0.0.1"))


class CustomerCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_customer_identity_and_csrf_are_server_owned(self):
        internal = InternalClient()
        async def session(_request, _surface):
            return SimpleNamespace(subject="server-owned-subject", csrf="csrf-test")
        with patch.object(credentials, "_session", side_effect=session), \
             patch.object(credentials, "_settings", return_value=("https://127.0.0.1:8444", "ca", "cert", "key")), \
             patch.object(credentials, "_client", return_value=internal):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                key = await client.get("/api/customer/credentials/transfer-key")
                self.assertEqual(key.status_code, 200)
                self.assertEqual(key.headers["cache-control"], "no-store")
                body = {"provider": "openai", "key_version": "2026-11",
                        "ciphertext_base64": "synthetic-ciphertext"}
                self.assertEqual((await client.post("/api/customer/credentials", json=body)).status_code, 403)
                self.assertEqual((await client.post("/api/customer/credentials", json={**body, "api_key": "leak"},
                                                    headers={"X-CSRF-Token": "csrf-test"})).status_code, 422)
                saved = await client.post("/api/customer/credentials", json=body,
                                          headers={"X-CSRF-Token": "csrf-test"})
                self.assertEqual(saved.status_code, 201)
        self.assertEqual(internal.last_json["tenant_id"], "server-owned-subject")
        self.assertEqual(internal.last_json["ciphertext_base64"], "synthetic-ciphertext")
        self.assertNotIn("api_key", internal.last_json)


if __name__ == "__main__":
    unittest.main()
