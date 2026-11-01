import os
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from foodtech_business_api import auth
from foodtech_business_api.auth_store import MemoryStore
from foodtech_business_api.main import app


ENV = {
    "FOODTECH_COGNITO_ISSUER": "https://cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_test",
    "FOODTECH_COGNITO_DOMAIN": "https://foodtech-test.auth.ap-southeast-2.amazoncognito.com",
    "FOODTECH_CUSTOMER_CLIENT_ID": "customer-client",
    "FOODTECH_ADMIN_CLIENT_ID": "admin-client",
    "FOODTECH_CUSTOMER_CALLBACK": "http://127.0.0.1:8000/api/auth/customer/callback",
    "FOODTECH_ADMIN_CALLBACK": "http://127.0.0.1:8000/api/auth/admin/callback",
    "FOODTECH_CUSTOMER_WEB": "http://127.0.0.1:5173",
    "FOODTECH_ADMIN_WEB": "http://127.0.0.1:5174",
    "FOODTECH_SECURE_COOKIES": "false",
}


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000", follow_redirects=False)
        self.store = MemoryStore()
        self.store_patch = patch.object(auth, "get_store", return_value=self.store)
        self.store_patch.start()
        self.cognito_patch = patch.object(auth, "_cognito_access", return_value=True)
        self.cognito_access = self.cognito_patch.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.store_patch.stop()
        self.cognito_patch.stop()
        self.env.stop()

    async def login(self, surface="customer"):
        response = await self.client.get(f"/api/auth/{surface}/login")
        self.assertEqual(response.status_code, 303)
        query = parse_qs(urlparse(response.headers["location"]).query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        if surface == "admin":
            self.assertEqual(query["acr_values"], [auth.ADMIN_ACR])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        return query

    async def callback(self, surface, state, groups=None, acr=None):
        claims = [
            {"sub": "subject-1", "email": "test@example.com"},
            {"sub": "subject-1", "exp": time.time() + 3600,
             "cognito:groups": groups or [],
             "acr": acr if acr is not None else (auth.ADMIN_ACR if groups else "urn:cognito:loa:1")},
        ]
        async def exchange(*args):
            self.assertTrue(args[-1])
            return {"id_token": "id", "access_token": "access"}
        with patch.object(auth, "_exchange", side_effect=exchange), \
             patch.object(auth, "_claims", side_effect=claims):
            return await self.client.get(f"/api/auth/{surface}/callback?code=code&state={state}")

    async def test_customer_session_example_and_csrf_logout(self):
        state = (await self.login())["state"][0]
        self.assertEqual((await self.callback("customer", state)).status_code, 303)
        me = (await self.client.get("/api/customer/me")).json()
        self.assertEqual(me["tier"], "FREE")
        self.assertEqual((await self.client.get("/api/customer/example")).json()["readOnly"], True)
        plan = (await self.client.get("/api/customer/demo-plan")).json()
        self.assertEqual(plan["price"], "00원")
        self.assertFalse(plan["billingEnabled"])
        self.assertEqual((await self.client.get("/api/admin/me")).status_code, 401)
        self.assertEqual((await self.client.post("/api/customer/logout")).status_code, 403)
        self.assertEqual((await self.client.post("/api/customer/logout", headers={"X-CSRF-Token": me["csrf"]})).status_code, 200)
        self.assertEqual((await self.client.get("/api/customer/me")).status_code, 401)

    async def test_admin_requires_group_and_admin_client(self):
        state = (await self.login("admin"))["state"][0]
        self.assertEqual((await self.callback("admin", state)).status_code, 403)
        self.assertEqual((await self.client.get("/api/admin/me")).status_code, 401)
        state = (await self.login("admin"))["state"][0]
        self.assertEqual((await self.callback("admin", state, ["foodtech-admin"], "urn:cognito:loa:1")).status_code, 403)
        state = (await self.login("admin"))["state"][0]
        self.assertEqual((await self.callback("admin", state, ["foodtech-admin"])).status_code, 303)
        self.assertEqual((await self.client.get("/api/admin/me")).json()["surface"], "admin")
        self.assertEqual((await self.client.get("/api/customer/example")).status_code, 401)

    async def test_admin_revoke_takes_effect_on_next_request_and_login(self):
        state = (await self.login("admin"))["state"][0]
        self.assertEqual((await self.callback("admin", state, ["foodtech-admin"])).status_code, 303)
        csrf = (await self.client.get("/api/admin/me")).json()["csrf"]
        route = "/api/admin/users/subject-1/revoke-admin"
        self.assertEqual((await self.client.post(route)).status_code, 403)
        self.assertEqual((await self.client.post(route, headers={"X-CSRF-Token": csrf})).status_code, 200)
        self.assertEqual((await self.client.get("/api/admin/me")).status_code, 401)
        state = (await self.login("admin"))["state"][0]
        self.assertEqual((await self.callback("admin", state, ["foodtech-admin"])).status_code, 403)

    async def test_suspension_takes_effect_on_next_customer_request(self):
        state = (await self.login())["state"][0]
        self.assertEqual((await self.callback("customer", state)).status_code, 303)
        self.store.suspend_user("subject-1")
        self.assertEqual((await self.client.get("/api/customer/me")).status_code, 401)
        state = (await self.login())["state"][0]
        self.assertEqual((await self.callback("customer", state)).status_code, 403)

    async def test_external_cognito_revocation_is_checked_on_each_request(self):
        state = (await self.login("admin"))["state"][0]
        self.assertEqual((await self.callback("admin", state, ["foodtech-admin"])).status_code, 303)
        self.cognito_access.return_value = False
        self.assertEqual((await self.client.get("/api/admin/me")).status_code, 401)

    async def test_state_surface_and_replay_are_rejected(self):
        state = (await self.login())["state"][0]
        self.assertEqual((await self.callback("admin", state)).status_code, 400)
        self.assertEqual((await self.callback("customer", state)).status_code, 400)
        self.assertEqual((await self.client.get("/api/customer/example")).status_code, 401)

    async def test_customer_signup_only(self):
        response = await self.client.get("/api/auth/customer/signup")
        self.assertIn("/signup?", response.headers["location"])
        self.assertEqual((await self.client.get("/api/auth/unknown/login")).status_code, 404)

    async def test_token_signature_and_client_binding(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        settings = auth.Settings.from_env()
        claims = {"sub": "subject-1", "iss": settings.issuer, "exp": time.time() + 300,
                  "token_use": "access", "client_id": settings.customer_client_id}
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})
        with patch.object(auth.jwt.PyJWKClient, "get_signing_key_from_jwt", return_value=type("Key", (), {"key": key.public_key()})()):
            self.assertEqual(auth._claims(token, settings, settings.customer_client_id, "access")["sub"], "subject-1")
            with self.assertRaises(Exception) as context:
                auth._claims(token, settings, settings.admin_client_id, "access")
            self.assertEqual(context.exception.status_code, 401)

    async def test_insecure_cookies_are_local_only(self):
        with patch.dict(os.environ, {"FOODTECH_CUSTOMER_WEB": "https://public.example.com"}):
            with self.assertRaises(Exception) as context:
                auth.Settings.from_env()
            self.assertEqual(context.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
