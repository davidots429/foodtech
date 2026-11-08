import asyncio
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import HTTPException

from foodtech_business_api import auth, tier
from foodtech_business_api.auth_store import Session
from foodtech_business_api.main import app
from foodtech_business_api.tier_store import MemoryTierStore


class TierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        self.store = MemoryTierStore()
        self.subject = str(uuid4())
        self.surface = "customer"

        async def session(_request, expected):
            if expected != self.surface:
                raise HTTPException(401, "로그인이 필요합니다.")
            return Session(self.surface, self.subject if self.surface == "customer" else str(uuid4()),
                           "test@example.com", "test-csrf", 9999999999, 0)

        async def inline_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        self.patches = [patch.object(tier, "_session", side_effect=session),
                        patch.object(auth, "_session", side_effect=session),
                        patch.object(tier, "get_tier_store", return_value=self.store),
                        patch.object(auth, "get_tier_store", return_value=self.store),
                        patch.object(asyncio, "to_thread", side_effect=inline_thread)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.client.aclose()

    async def test_submit_review_and_tier_change(self):
        body = {"business_name": "시험 식당", "business_registration_number": "123-45-67890",
                "notice_accepted": True}
        self.assertEqual((await self.client.post("/api/customer/tier-requests", json=body)).status_code, 403)
        result = await self.client.post("/api/customer/tier-requests", json=body,
                                        headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(result.status_code, 201)
        item = result.json()["application"]
        self.assertEqual(item["business_number_last4"], "7890")
        self.assertNotIn("123-45-67890", str(self.store.items))
        self.assertNotIn("1234567890", str(self.store.items))
        self.assertNotIn("123-45-67890", result.text)
        self.assertIn("실제 과금이 발생하지", result.json()["notice"])
        self.assertEqual(result.json()["price"], "00원")
        self.assertEqual((await self.client.post("/api/customer/tier-requests", json=body,
                                                headers={"X-CSRF-Token": "test-csrf"})).status_code, 409)
        self.assertEqual((await self.client.get("/api/customer/me")).json()["tier"], "FREE")
        self.assertEqual((await self.client.get("/api/admin/tier-requests/pending")).status_code, 401)

        self.surface = "admin"
        pending = await self.client.get("/api/admin/tier-requests/pending")
        self.assertEqual(len(pending.json()["applications"]), 1)
        path = f"/api/admin/tier-requests/{self.subject}/{item['application_id']}/decision"
        self.assertEqual((await self.client.post(path, json={"decision": "APPROVED_FOR_DEMO"})).status_code, 403)
        approved = await self.client.post(path, json={"decision": "APPROVED_FOR_DEMO"},
                                          headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["application"]["tier"], "DEMO_PAID")
        self.assertEqual((await self.client.post(path, json={"decision": "REJECTED"},
                                                headers={"X-CSRF-Token": "test-csrf"})).status_code, 409)
        self.assertEqual((await self.client.get("/api/admin/tier-requests/pending")).json()["applications"], [])
        self.surface = "customer"
        self.assertEqual((await self.client.get("/api/customer/me")).json()["tier"], "DEMO_PAID")
        self.assertEqual((await self.client.get("/api/customer/tier-requests/current")).json()["application"]["status"],
                         "APPROVED_FOR_DEMO")

    async def test_rejection_allows_new_application_without_number_echo(self):
        body = {"business_name": "시험 식당", "business_registration_number": "1234567890",
                "notice_accepted": True}
        self.assertEqual((await self.client.post("/api/customer/tier-requests", json={**body, "notice_accepted": False},
                                                headers={"X-CSRF-Token": "test-csrf"})).status_code, 400)
        invalid = await self.client.post("/api/customer/tier-requests", json={**body, "business_registration_number": "secret-invalid"},
                                         headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn("secret-invalid", invalid.text)
        first = (await self.client.post("/api/customer/tier-requests", json=body,
                                        headers={"X-CSRF-Token": "test-csrf"})).json()["application"]
        self.surface = "admin"
        path = f"/api/admin/tier-requests/{self.subject}/{first['application_id']}/decision"
        self.assertEqual((await self.client.post(path, json={"decision": "REJECTED"},
                                                headers={"X-CSRF-Token": "test-csrf"})).status_code, 200)
        self.surface = "customer"
        self.assertEqual((await self.client.post("/api/customer/tier-requests", json=body,
                                                headers={"X-CSRF-Token": "test-csrf"})).status_code, 201)
