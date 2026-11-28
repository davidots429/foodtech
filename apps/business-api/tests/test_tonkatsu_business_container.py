import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from starlette.responses import JSONResponse

from foodtech_business_api.serve_tonkatsu_business_api import CustomerSurface, main


class TonkatsuBusinessContainerTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "FOODTECH_TONKATSU_CUSTOMER_INVOCATION_ENABLED": "true",
            "FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED": "true",
            "FOODTECH_PRINCIPAL_PROVISIONING_ENABLED": "true",
            "FOODTECH_COGNITO_ISSUER": "https://identity.example/pool",
            "FOODTECH_COGNITO_DOMAIN": "https://login.example",
            "FOODTECH_CUSTOMER_CLIENT_ID": "customer-fixture",
            "FOODTECH_ADMIN_CLIENT_ID": "admin-fixture",
            "FOODTECH_CUSTOMER_CALLBACK": "https://customer.example/callback",
            "FOODTECH_ADMIN_CALLBACK": "https://admin.example/callback",
            "FOODTECH_CUSTOMER_WEB": "https://customer.example",
            "FOODTECH_ADMIN_WEB": "https://admin.example",
            "FOODTECH_AUTH_TABLE": "auth-fixture",
            "FOODTECH_COOKING_TABLE": "cooking-fixture",
        }
        self.run_server = self.enterContext(patch("foodtech_business_api.serve_tonkatsu_business_api.uvicorn.run"))

    def test_serves_the_existing_authentication_boundary_on_loopback_by_default(self):
        with patch.dict(os.environ, self.env, clear=True):
            main()
        self.run_server.assert_called_once_with(
            "foodtech_business_api.serve_tonkatsu_business_api:app", host="127.0.0.1", port=8080, proxy_headers=False, access_log=False,
        )

    def test_missing_private_records_mode_or_invalid_port_never_starts_the_api(self):
        for key, value in (("FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED", "false"),
                           ("FOODTECH_TONKATSU_BUSINESS_PORT", "0")):
            with self.subTest(key=key), patch.dict(os.environ, {**self.env, key: value}, clear=True):
                with self.assertRaises(ValueError):
                    main()
        self.run_server.assert_not_called()


class CustomerSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_customer_paths_reach_the_business_application(self):
        async def handler(scope, receive, send):
            response = JSONResponse({"path": scope["path"]})
            await response(scope, receive, send)

        downstream = AsyncMock(side_effect=handler)
        async with httpx.AsyncClient(base_url="http://local", transport=httpx.ASGITransport(
            app=CustomerSurface(downstream)
        )) as client:
            for path in ("/api/auth/customer/login", "/api/auth/customer/callback",
                         "/api/customer/session/refresh", "/api/customer/ai/tonkatsu-recipe-requests"):
                self.assertEqual((await client.get(path)).json(), {"path": path})
            for path in ("/api/auth/admin/login", "/api/auth/admin/password", "/api/admin/me",
                         "/api/auth/customer-derived/login", "/api/customer-admin/me", "/docs", "/openapi.json"):
                with self.subTest(path=path):
                    response = await client.get(path)
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(downstream.await_count, 4)
