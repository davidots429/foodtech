import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class JeyukFeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryCookingStore()
        self.subject = "feed-owner"
        async def session(_request, surface):
            return Session(surface, self.subject, None, "csrf", 9999999999, 0)
        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store)]
        for item in self.patches: item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.headers = {"X-CSRF-Token": "csrf"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches): item.stop()

    async def test_individual_feeds_group_boundary_and_discharge_history(self):
        created = await self.client.post("/api/customer/cooking-jobs", headers=self.headers, json={
            "planned_portions": 2, "recipe_version": "synthetic-v1", "equipment_profile": "jeyuk-small",
            "actual_load_kg": "1.8"})
        self.assertEqual(created.status_code, 201, created.text)
        job = created.json(); path = f"/api/customer/cooking-jobs/{job['job_id']}"
        placed = datetime.now(timezone.utc) - timedelta(seconds=500)
        async def feed(name, start, end, mass, *, reason=None, version=None):
            return await self.client.post(path + f"/jeyuk-feeds/{name}", headers=self.headers, json={
                "expected_version": job["version"] if version is None else version,
                "started_at": start.isoformat(), "completed_at": end.isoformat(), "mass_kg": mass, "reason": reason})
        recorded = await feed("meat", placed - timedelta(seconds=10), placed, "1.0")
        self.assertEqual(recorded.status_code, 201, recorded.text); job = recorded.json()
        ready = await self.client.post(path + "/transitions", headers=self.headers,
            json={"expected_version": job["version"], "event": "PREHEAT_COMPLETED"})
        job = ready.json()
        mismatch = await self.client.post(path + "/transitions", headers=self.headers, json={
            "expected_version": job["version"], "event": "COOKING_STARTED",
            "actual_placed_at": (placed + timedelta(seconds=1)).isoformat()})
        self.assertEqual(mismatch.status_code, 422)
        started = await self.client.post(path + "/transitions", headers=self.headers, json={
            "expected_version": job["version"], "event": "COOKING_STARTED", "actual_placed_at": placed.isoformat()})
        self.assertEqual(started.status_code, 200, started.text); job = started.json()
        veg = await feed("vegetable", placed + timedelta(seconds=20), placed + timedelta(seconds=25), "0.5")
        self.assertEqual(veg.status_code, 201, veg.text); job = veg.json()
        sauce = await feed("sauce", placed + timedelta(seconds=50), placed + timedelta(seconds=55), "0.3")
        self.assertEqual(sauce.status_code, 201, sauce.text); job = sauce.json()
        self.assertTrue(job["jeyuk_additions_simultaneous"])
        self.assertEqual(job["jeyuk_feed_mass_status"], "MATCHED")
        self.assertEqual(job["jeyuk_feeds"]["SAUCE"]["addition_group_id"],
                         job["jeyuk_feeds"]["VEGETABLE"]["addition_group_id"])
        self.assertEqual((await feed("sauce", placed + timedelta(seconds=51), placed + timedelta(seconds=55), "0.3")).status_code, 422)
        revised = await feed("sauce", placed + timedelta(seconds=51), placed + timedelta(seconds=55), "0.3", reason="시각 정정")
        self.assertEqual(revised.status_code, 201, revised.text); job = revised.json()
        self.assertFalse(job["jeyuk_additions_simultaneous"])
        self.assertEqual((await feed("sauce", placed, placed, "0.3", reason="stale", version=1)).status_code, 409)
        lifted = placed + timedelta(seconds=480)
        completed = await self.client.post(path + "/transitions", headers=self.headers, json={
            "expected_version": job["version"], "event": "COOKING_COMPLETED", "actual_lifted_at": lifted.isoformat()})
        self.assertEqual(completed.status_code, 200, completed.text); job = completed.json()
        discharge = await self.client.post(path + "/jeyuk-discharge", headers=self.headers, json={
            "expected_version": job["version"], "completed_at": (lifted + timedelta(seconds=5)).isoformat()})
        self.assertEqual(discharge.status_code, 201, discharge.text); job = discharge.json()
        self.assertEqual(job["jeyuk_discharge"]["method"], "MANUAL_POUR")
        self.assertEqual(job["jeyuk_discharge"]["duration_seconds"], "5")
        self.assertEqual(job["jeyuk_discharge"]["total_process_duration_seconds"], "495")
        events = (await self.client.get(path + "/events")).json()
        feeds = [event for event in events if event.get("domain") == "JEYUK_FEED"]
        self.assertEqual([event["ingredient"] for event in feeds], ["MEAT", "VEGETABLE", "SAUCE", "SAUCE"])
        self.assertEqual(feeds[-1]["previous"]["started_at"], sauce.json()["jeyuk_feeds"]["SAUCE"]["started_at"])
        self.subject = "other-owner"
        self.assertEqual((await self.client.get(path)).status_code, 404)
