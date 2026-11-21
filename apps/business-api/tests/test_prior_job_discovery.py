import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class PriorDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject = "owner"
        self.store = MemoryCookingStore()
        async def session(_request, surface):
            assert surface == "customer"
            return Session("customer", self.subject, None, "csrf", 9999999999, 0)
        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store)]
        for item in self.patches: item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.first = self.add_job("2026-01-01T00:00:00Z", "COMPLETED")
        self.failed = self.add_job("2026-01-02T00:00:00Z", "FAILED")
        self.current = self.add_job("2026-01-03T00:00:00Z", "RUNNING")
        self.path = f"/api/customer/cooking-jobs/{self.current}/prior-job-candidate"

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches): item.stop()

    def add_job(self, started, status, *, subject=None):
        owner = subject or self.subject; job_id = str(uuid4()); pk = cooking._pk(owner, job_id)
        item = {"job_id": job_id, "owner_subject": owner, "version": 1, "status": status,
                "menu": "JEYUK", "provenance": "SYNTHETIC", "equipment_profile": "jeyuk-small",
                "recipe_version": "synthetic-v1", "planned_portions": 1, "created_at": started,
                "actual_placed_at": started, "safety_status": "FAILED" if status == "FAILED" else "PASSED",
                "source_binding": {"source_id": "synthetic-rest", "device_id": "mockprofile-110-a-001",
                                   "batch_id": "jeyuk-" + uuid4().hex}}
        self.store.create(pk, item)
        return job_id

    def meta(self, job_id):
        return self.store.items[(cooking._pk(self.subject, job_id), "META")]

    async def test_latest_failed_is_candidate_not_proof_and_other_owner_is_excluded(self):
        self.add_job("2026-01-02T12:00:00Z", "COMPLETED", subject="other")
        unrelated = self.add_job("2026-01-02T06:00:00Z", "COMPLETED")
        self.meta(unrelated).update(menu="TONKATSU", equipment_profile="synthetic-fryer", source_binding=None)
        response = await self.client.get(self.path)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual((value["status"], value["candidate"]["job_id"], value["candidate"]["safety_status"]),
                         ("CANDIDATE", self.failed, "FAILED"))
        self.assertFalse(value["immediately_previous_verified"])
        self.assertEqual(value["history_revision_status"], "UNCHANGED_DURING_READ")
        self.subject = "other"
        self.assertEqual((await self.client.get(self.path)).status_code, 404)

    async def test_unknown_binding_ambiguous_time_and_active_job_are_not_silently_skipped(self):
        self.meta(self.failed)["source_binding"] = None
        self.assertEqual((await self.client.get(self.path)).json()["status"], "HISTORY_INCOMPLETE")
        self.meta(self.failed)["source_binding"] = self.meta(self.first)["source_binding"]
        self.meta(self.failed).update(status="READY", actual_placed_at="invalid")
        self.assertEqual((await self.client.get(self.path)).json()["status"], "HISTORY_INCOMPLETE")
        self.meta(self.failed).update(status="FAILED", actual_placed_at="2026-01-02T00:00:00Z")
        self.meta(self.failed)["actual_placed_at"] = self.meta(self.first)["actual_placed_at"]
        self.assertEqual((await self.client.get(self.path)).json()["status"], "AMBIGUOUS")
        self.meta(self.failed)["actual_placed_at"] = "2026-01-02T00:00:00Z"
        self.meta(self.failed)["status"] = "RUNNING"
        self.assertEqual((await self.client.get(self.path)).json()["status"], "UNAVAILABLE")

    async def test_large_history_or_unlisted_current_is_not_claimed_complete(self):
        with patch("foodtech_business_api.prior_job_discovery.MAX_LISTED_JOBS", 2):
            self.assertEqual((await self.client.get(self.path)).json()["status"], "HISTORY_INCOMPLETE")
        self.meta(self.current)["source_binding"] = None
        self.assertEqual((await self.client.get(self.path)).json()["status"], "NOT_BOUND")

    async def test_paginated_history_and_concurrent_new_job_are_handled(self):
        for _ in range(51):
            self.add_job("2025-01-01T00:00:00Z", "COMPLETED")
        value = (await self.client.get(self.path)).json()
        self.assertEqual((value["status"], value["candidate"]["job_id"]), ("CANDIDATE", self.failed))
        original = self.store.list_jobs
        calls = 0
        def changing(subject, limit, cursor):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.add_job("2026-01-02T12:00:00Z", "FAILED")
            return original(subject, limit, cursor)
        with patch.object(self.store, "list_jobs", side_effect=changing):
            self.assertEqual((await self.client.get(self.path)).status_code, 409)

    async def test_legacy_missing_revision_does_not_claim_full_history(self):
        with patch.object(self.store, "history_revision", return_value=None):
            result = (await self.client.get(self.path)).json()
        self.assertEqual(result["history_revision_status"], "UNTRACKED")
        self.assertFalse(result["immediately_previous_verified"])

    async def test_revision_change_without_visible_list_change_is_rejected(self):
        original = self.store.history_revision; calls = 0
        def changed(subject):
            nonlocal calls
            calls += 1
            value = original(subject)
            return value if calls == 1 else value + 1
        with patch.object(self.store, "history_revision", side_effect=changed):
            self.assertEqual((await self.client.get(self.path)).status_code, 409)

    async def test_changed_history_during_read_is_rejected(self):
        original = self.store.read_meta; counts = {}
        def changing(pk):
            counts[pk] = counts.get(pk, 0) + 1
            if pk == cooking._pk(self.subject, self.failed) and counts[pk] == 2:
                self.store.items[(pk, "META")]["version"] += 1
            return original(pk)
        with patch.object(self.store, "read_meta", side_effect=changing):
            self.assertEqual((await self.client.get(self.path)).status_code, 409)
