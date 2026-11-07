import unittest
from types import SimpleNamespace

import httpx

from foodtech_credential_registration.main import app


class FakeRegistration:
    transfer = SimpleNamespace(public_descriptor=lambda: {
        "key_version": "2026-11", "algorithm": "RSA-OAEP-SHA-256",
        "public_key_spki_base64": "synthetic-public-key", "expires_at": "future"})

    def register(self, tenant_id, provider, key_version, ciphertext):
        assert (tenant_id, provider, key_version, ciphertext) == (
            "subject-1", "openai", "2026-11", "synthetic-ciphertext")
        return "new-credential-id"


class InternalApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_mtls_listener_required_before_public_key_or_registration(self):
        app.state.credential_registrar = FakeRegistration()
        app.state.mtls_listener = False
        body = {"tenant_id": "subject-1", "provider": "openai", "key_version": "2026-11",
                "ciphertext_base64": "synthetic-ciphertext"}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1") as client:
                self.assertEqual((await client.get("/internal/credentials/transfer-key")).status_code, 401)
                self.assertEqual((await client.post("/internal/credentials", json=body)).status_code, 401)
            app.state.mtls_listener = True
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://127.0.0.1") as client:
                self.assertEqual((await client.get("/internal/credentials/transfer-key")).json()["key_version"],
                                 "2026-11")
                result = await client.post("/internal/credentials", json=body)
                self.assertEqual((result.status_code, result.json()["credential_id"]),
                                 (201, "new-credential-id"))
        finally:
            app.state.mtls_listener = False
            del app.state.credential_registrar


if __name__ == "__main__":
    unittest.main()
