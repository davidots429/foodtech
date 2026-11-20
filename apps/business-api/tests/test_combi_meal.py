import os
import unittest
from unittest.mock import patch

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class CombiMealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryCookingStore()
        self.subject = "customer-one"

        async def session(_request, _surface):
            return Session("customer", self.subject, None, "csrf", 9999999999, 0)

        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store)]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.body = {"equipment_size": "MEDIUM_FIXED_RACK_10", "rack_count": 1, "pan_count": 10,
                     "dry_rice_kg": "20", "drained_rice_kg": "24", "added_water_kg": "20",
                     "pan_tare_kg": "10", "rack_tare_kg": "0"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    async def test_creation_stores_linked_records_and_isolates_customers(self):
        response = await self.client.post("/api/customer/combi-meals", json=self.body,
                                          headers={"X-CSRF-Token": "csrf"})
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        meal = result["meal"]
        self.assertEqual(result["rice"]["water_ratio_status"], "NORMAL")
        self.assertEqual(result["rice"]["payload_kg"], "54")
        self.assertEqual(result["rice"]["job_id"], meal["rice_cooking_job_id"])
        self.assertEqual(result["transition"]["transition_id"], meal["mode_transition_id"])
        self.assertEqual(result["mackerel"]["job_id"], meal["mackerel_cooking_job_id"])
        self.assertEqual(result["mackerel"]["safety_status"], "NOT_MEASURED")
        self.assertFalse(result["transition"]["mackerel_load_allowed"])
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        self.assertEqual((await self.client.get(path)).json(), result)
        self.subject = "customer-two"
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_load_rejection_and_water_warning(self):
        headers = {"X-CSRF-Token": "csrf"}
        for values in ({"pan_count": 11}, {"drained_rice_kg": "19"}, {"rack_tare_kg": "2"},
                       {"drained_rice_kg": "60"}):
            result = await self.client.post("/api/customer/combi-meals", headers=headers,
                                            json={**self.body, **values})
            self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(self.store.items, {})
        warning = await self.client.post("/api/customer/combi-meals", headers=headers,
                                         json={**self.body, "added_water_kg": "10"})
        self.assertEqual(warning.status_code, 201, warning.text)
        self.assertEqual(warning.json()["rice"]["water_ratio_status"], "WATER_RATIO_OUT_OF_RANGE")
        denied = await self.client.post("/api/customer/combi-meals", json=self.body)
        self.assertEqual(denied.status_code, 403)
