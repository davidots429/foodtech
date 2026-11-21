import os
import unittest
from unittest.mock import patch

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class ConditionProfilesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject = "owner"
        self.store = MemoryCookingStore()
        async def session(_request, surface): return Session(surface, self.subject, None, "csrf", 9999999999, 0)
        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store)]
        for p in self.patches: p.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.path = "/api/customer/cooking-jobs/condition-profiles"
        self.body = {"profile_id": "synthetic-small", "profile_version": "v1", "equipment_profile": "jeyuk-small",
                     "heating_setpoint_celsius": "200", "target_rpm": "30"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches): p.stop()

    async def test_immutable_version_and_customer_boundary(self):
        response = await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json=self.body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["status"], "DECLARED_UNVERIFIED")
        duplicate = await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"},
                                           json={**self.body, "target_rpm": "31"})
        self.assertEqual(duplicate.status_code, 409)
        fetched = await self.client.get(self.path + "/synthetic-small/v1")
        self.assertEqual(fetched.json()["target_rpm"], "30")
        self.subject = "other"
        self.assertEqual((await self.client.get(self.path + "/synthetic-small/v1")).status_code, 404)

    async def test_invalid_profile_and_csrf_are_rejected(self):
        self.assertEqual((await self.client.post(self.path, json=self.body)).status_code, 403)
        for field, value in (("profile_id", "bad/id"), ("target_rpm", "NaN"),
                             ("heating_setpoint_celsius", "301"), ("equipment_profile", "real-device")):
            with self.subTest(field=field):
                result = await self.client.post(self.path, headers={"X-CSRF-Token": "csrf"}, json={**self.body, field: value})
                self.assertEqual(result.status_code, 422)


if __name__ == "__main__": unittest.main()
