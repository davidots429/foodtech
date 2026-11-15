import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


CSRF = {"X-CSRF-Token": "test-csrf"}


class JeyukCookingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"})
        self.env.start()
        self.store = MemoryCookingStore()

        async def session(_request, surface):
            assert surface == "customer"
            return Session("customer", "jeyuk-customer", None, "test-csrf", 9999999999, 0)

        self.session_patch = patch.object(cooking, "_session", side_effect=session)
        self.store_patch = patch.object(cooking, "get_cooking_store", return_value=self.store)
        self.session_patch.start()
        self.store_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.store_patch.stop()
        self.session_patch.stop()
        self.env.stop()

    async def create(self, load="1.8"):
        response = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                                          json={"planned_portions": 2, "recipe_version": "jeyuk-v1",
                                                "equipment_profile": "jeyuk-small",
                                                "actual_load_kg": load})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_profile_load_and_menu_are_separate_from_fryer(self):
        job = await self.create()
        self.assertEqual((job["menu"], job["jeyuk_size_profile"], job["process_load_status"]),
                         ("JEYUK", "SMALL", "NORMAL"))
        self.assertIsNone(job["thickness_cm"])
        listed = (await self.client.get("/api/customer/cooking-jobs")).json()["items"]
        self.assertEqual((listed[0]["menu"], listed[0]["equipment_profile"]), ("JEYUK", "jeyuk-small"))
        warning = await self.create("2.5")
        self.assertEqual((warning["process_load_status"], warning["process_assessment_status"]),
                         ("OVER_RECOMMENDED_LOAD", "WARNING"))
        for body in (
            {"planned_portions": 2, "recipe_version": "jeyuk-v1", "equipment_profile": "jeyuk-small"},
            {"planned_portions": 2, "recipe_version": "jeyuk-v1", "equipment_profile": "jeyuk-small",
             "actual_load_kg": "3.01"},
            {"planned_portions": 2, "recipe_version": "jeyuk-v1", "equipment_profile": "jeyuk-small",
             "actual_load_kg": "1.8", "thickness_cm": "1.2"},
        ):
            self.assertEqual((await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                                                     json=body)).status_code, 422)

    async def test_binding_and_completion_keep_jeyuk_contract(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        wrong = await self.client.post(path + "/source-binding", headers=CSRF,
                                       json={"expected_version": job["version"],
                                             "source_id": "synthetic-rest",
                                             "device_id": "mockprofile-100-a-001",
                                             "batch_id": "jeyuk-small-process-v1"})
        self.assertEqual(wrong.status_code, 422)
        bound = await self.client.post(path + "/source-binding", headers=CSRF,
                                       json={"expected_version": job["version"],
                                             "source_id": "synthetic-rest",
                                             "device_id": "mockprofile-110-a-001",
                                             "batch_id": "jeyuk-small-process-v1"})
        self.assertEqual(bound.status_code, 200, bound.text)
        job = bound.json()
        self.assertIn(("JEYUK#jeyuk-small-process-v1", "BINDING"), self.store.items)
        ready = await self.client.post(path + "/transitions", headers=CSRF,
                                       json={"expected_version": job["version"],
                                             "event": "PREHEAT_COMPLETED"})
        job = ready.json()
        placed = datetime.now(timezone.utc) - timedelta(seconds=480)
        started = await self.client.post(path + "/transitions", headers=CSRF,
                                         json={"expected_version": job["version"],
                                               "event": "COOKING_STARTED",
                                               "actual_placed_at": placed.isoformat()})
        self.assertEqual(started.status_code, 200, started.text)
        job = started.json()
        completed = await self.client.post(path + "/transitions", headers=CSRF,
                                           json={"expected_version": job["version"],
                                                 "event": "COOKING_COMPLETED",
                                                 "actual_lifted_at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["process_time_status"], "NORMAL")
        self.assertEqual(completed.json()["process_assessment_status"], "INSUFFICIENT_DATA")
        core = await self.client.post(path + "/safety/core-check", headers=CSRF,
                                      json={"expected_version": completed.json()["version"],
                                            "probes": [{"item_id": "piece-1", "calibration_status": "OK",
                                                        "samples": [{"at_second": 0, "celsius": "75"}]}]})
        self.assertEqual(core.status_code, 422)
        self.assertTrue(completed.json()["evaluation_blocked"])


if __name__ == "__main__":
    unittest.main()
