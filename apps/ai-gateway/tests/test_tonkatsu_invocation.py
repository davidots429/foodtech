"""Exercise the actual fryer Gateway route over local mutual TLS."""
import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx

from foodtech_ai_gateway.main import app
from foodtech_ai_gateway.policy import Caller
from foodtech_ai_gateway.serve_tonkatsu import BROKER_URI, tonkatsu_server_config
from foodtech_business_api.combi_read_delivery import CombiDeliverySettings, _context

from combi_tls_fixture import certificates
from test_combi_pinned_tls import listening


class TonkatsuInvocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_and_scope_are_bound_before_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            certs = await asyncio.to_thread(certificates, Path(temporary), broker_uri=BROKER_URI)
            identifier = str(uuid4())
            path = f"/internal/ai/tonkatsu-recipe-executions/{identifier}/invoke"
            client = Mock()
            registry = Mock(table_name="foodtech-ai-execution-registry-dev", auth_table="foodtech-auth-sessions-dev")
            role = "arn:aws:iam::123456789012:role/foodtech-tonkatsu-source-read-dev"
            role_id = "AROASYNTHETICREADROLE"
            trusted = Mock(return_value=(role, role_id, client, registry))
            guard = Mock(owner="stored-subject")
            resolver_factory = Mock()
            resolver = resolver_factory.return_value

            async def receive(request, *, resolve_scope, expected_role, expected_role_id):
                self.assertEqual((expected_role, expected_role_id), (role, role_id))
                self.assertIs(resolve_scope(), resolver.resolve_execution.return_value)
                return guard

            execute = AsyncMock(return_value=SimpleNamespace(
                execution_id=identifier, state="DENIED", response_text=None, error_code="EVIDENCE_UNAVAILABLE",
            ))
            module = "foodtech_ai_gateway.tonkatsu_invocation"
            with (
                patch.dict(os.environ, {"FOODTECH_TONKATSU_INVOCATION_ENABLED": "true"}),
                patch(f"{module}.configured_recipe_runtime", trusted),
                patch(f"{module}.DynamoTonkatsuReadScope", resolver_factory),
                patch(f"{module}.receive_internal_tonkatsu_handoff", new=receive),
                patch(f"{module}.GatewayService", return_value=SimpleNamespace(execute_observed_tonkatsu_recipe=execute)),
            ):
                async with listening(app, certs, server_config=tonkatsu_server_config) as (url, config):
                    self.assertIsNone(config.ssl.keylog_filename)
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="https://localhost",
                    ) as direct:
                        response = await direct.post(path, headers={"x-forwarded-client-cert": "synthetic"})
                        self.assertEqual(response.status_code, 401)
                    unpinned = _context(CombiDeliverySettings(url, certs["server"][0], *certs["other_broker"]))
                    async with httpx.AsyncClient(verify=unpinned, trust_env=False, timeout=5) as http:
                        with self.assertRaises(httpx.TransportError):
                            await http.post(url + path)
                    trusted.assert_not_called()

                    pinned = _context(CombiDeliverySettings(url, certs["server"][0], *certs["broker"]))
                    async with httpx.AsyncClient(verify=pinned, trust_env=False, timeout=5) as http:
                        unrelated = await http.post(url + "/internal/ai/executions")
                        self.assertEqual(unrelated.status_code, 404)
                        trusted.assert_not_called()
                        response = await http.post(url + path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.json(), {"execution_id": identifier, "state": "DENIED",
                    "response_text": None, "error_code": "EVIDENCE_UNAVAILABLE"})
                trusted.assert_called_once_with("tonkatsu")
                resolver.resolve_execution.assert_called_once_with(identifier)
                self.assertEqual(execute.await_args.args, (Caller("business-api", "stored-subject"), identifier))
                self.assertIs(execute.await_args.kwargs["tonkatsu_source_guard"], guard)
                guard.close.assert_called_once()
                client.close.assert_called_once()
            for attribute in ("tonkatsu_broker_listener", "tonkatsu_broker_peer_sha256", "mtls_listener"):
                delattr(app.state, attribute)
