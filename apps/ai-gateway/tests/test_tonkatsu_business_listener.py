"""Exercise the isolated BFF route and leaf pin with local TLS."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from combi_tls_fixture import certificates
from foodtech_ai_gateway.serve_tonkatsu_business import business_server_config
from foodtech_business_api.combi_read_delivery import CombiDeliverySettings, _context
from test_combi_pinned_tls import listening

PREFIX = "/internal/ai/"


class TonkatsuBusinessListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = self.enterContext(TemporaryDirectory())
        self.certs = await asyncio.to_thread(certificates, Path(temporary))
        self.execution_id = str(uuid4())
        self.body = {"tenant_id": "synthetic-customer", "job_id": str(uuid4()), "auth_epoch": 2}
        self.creation_body = {**self.body, "request_id": str(uuid4()), "job_version": 3,
                              "prior_job_id": str(uuid4()), "prior_job_version": 4}
        self.read = self.enterContext(patch("foodtech_ai_gateway.tonkatsu_invocation.read_tonkatsu_request",
                                          return_value={"state": "SUCCEEDED"}))
        self.create = self.enterContext(patch("foodtech_ai_gateway.tonkatsu_invocation.create_tonkatsu_request",
                                            return_value=SimpleNamespace(execution_id=self.execution_id)))

    def configuration(self, app, *, server_cert, server_key, broker_ca, broker_certificate, port):
        return business_server_config(server_cert=server_cert, server_key=server_key, client_ca=broker_ca,
                                      client_certificate=self.certs["business"][0], port=port)

    def client(self, url, *, identity="business"):
        settings = CombiDeliverySettings(url, self.certs["server"][0], *self.certs[identity])
        return httpx.AsyncClient(base_url=url, verify=_context(settings), trust_env=False, timeout=5)

    async def test_only_the_four_protected_routes_are_available_to_the_pinned_business_api(self):
        async with listening(None, self.certs, server_config=self.configuration) as (url, _):
            async with self.client(url) as client:
                created = await client.post(PREFIX + "tonkatsu-recipe-requests", json=self.creation_body)
                self.assertEqual((created.status_code, created.json()["execution_id"]), (201, self.execution_id))
                for resource in ("status", "results", "review-context"):
                    response = await client.post(PREFIX + f"tonkatsu-recipe-{resource}/{self.execution_id}", json=self.body)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["cache-control"], "no-store")
                for path in ("executions", f"tonkatsu-recipe-executions/{self.execution_id}/invoke", "tonkatsu-recipe-requests/"):
                    self.assertEqual((await client.post(PREFIX + path, json=self.body)).status_code, 404)
                self.assertEqual((await client.get("/docs")).status_code, 404)
        self.create.assert_called_once()
        self.assertEqual(self.read.call_count, 3)

    async def test_same_ca_client_with_different_leaf_and_direct_asgi_requests_cannot_read(self):
        path = PREFIX + f"tonkatsu-recipe-results/{self.execution_id}"
        async with listening(None, self.certs, server_config=self.configuration) as (url, config):
            for identity in ("other_business", "broker"):
                with self.subTest(identity=identity):
                    async with self.client(url, identity=identity) as client:
                        with self.assertRaises(httpx.TransportError):
                            await client.post(path, json=self.body)
            async with httpx.AsyncClient(base_url="https://127.0.0.1",
                                         transport=httpx.ASGITransport(app=config.app)) as client:
                response = await client.post(path, json=self.body)
                self.assertEqual(response.status_code, 401)
        self.read.assert_not_called()
