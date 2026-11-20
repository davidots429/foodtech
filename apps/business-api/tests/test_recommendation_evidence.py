import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class RecommendationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryCookingStore()
        self.job_id = str(uuid4())
        self.subject = "owner"
        self.pk = cooking._pk(self.subject, self.job_id)
        self.store.items[(self.pk, "META")] = {"pk": self.pk, "sk": "META", "job_id": self.job_id,
            "owner_subject": self.subject, "version": 7, "safety_version": 2, "status": "COMPLETED",
            "provenance": "SYNTHETIC", "menu": "TONKATSU", "recipe_version": "recipe-1",
            "equipment_profile": "synthetic-fryer", "thickness_profile": "STANDARD",
            "thickness_cm": "1.20", "actual_cooking_seconds": 300, "safety_status": "PASSED",
            "evaluation_blocked": False, "process_assessment_status": "NORMAL",
            "private_note": "not-for-model"}
        self.store.items[(self.pk, "ASSESSMENT#CURRENT")] = {"version": 1, "usable": True,
            "safety_version": 2, "assessment_sk": "ASSESSMENT#REV#000000000001",
            "quality_passed": True, "score_percent": "80"}
        self.store.items[(self.pk, "ASSESSMENT#REV#000000000001")] = {"version": 1,
            "sk": "ASSESSMENT#REV#000000000001", "status": "COMPLETED",
            "safety_version": 2, "quality_passed": True, "score_percent": "80",
            "scale_version": "tonkatsu-common-v1", "observations": {"note": "not-for-model"}}
        result_sk = "RESTATEMENT#RESULT#test"
        self.store.items[(self.pk, "PROCESS#LATEST_RESTATED")] = {"version": 1, "result_sk": result_sk}
        self.store.items[(self.pk, result_sk)] = {"sk": result_sk, "job_version_at_derivation": 7,
            "process_assessment_status": "NORMAL", "process_assessment": {"rule_version": "test"},
            "observation_coverage": {"sample_count": 2, "private_note": "not-for-model"}}
        async def session(_request, surface):
            self.assertEqual(surface, "customer")
            return SimpleNamespace(subject=self.subject)
        self.env = patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"})
        self.session = patch.object(cooking, "_session", side_effect=session)
        self.store_patch = patch.object(cooking, "get_cooking_store", return_value=self.store)
        for item in (self.env, self.session, self.store_patch): item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in (self.store_patch, self.session, self.env): item.stop()

    async def test_only_owned_allowlisted_records_are_returned(self):
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        result = await self.client.get(path)
        self.assertEqual((result.status_code, result.headers["cache-control"]), (200, "no-store"))
        value = result.json()
        self.assertEqual(value["recorded"]["quality"]["score_percent"], "80")
        self.assertEqual(value["process_view"], "LATEST_RESTATED")
        self.assertEqual(value["completeness"], "NOT_ASSESSED")
        self.assertFalse(value["recommendation_execution_available"])
        self.assertIn("INGREDIENT_INITIAL_STATE", value["unverified_categories"])
        self.assertNotIn("not-for-model", result.text)
        self.assertNotIn("owner", result.text)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_stale_quality_and_derived_result_are_not_presented_as_current(self):
        self.store.items[(self.pk, "META")]["safety_version"] = 3
        self.store.items[(self.pk, "META")]["version"] = 8
        result = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        value = result.json()
        self.assertIsNone(value["recorded"]["quality"])
        self.assertTrue(value["process_stale"])
        self.assertIn("CURRENT_QUALITY_ASSESSMENT", value["unverified_categories"])
        self.assertIn("CURRENT_PROCESS_DERIVATION", value["unverified_categories"])

    async def test_concurrent_job_change_rejects_mixed_snapshot(self):
        original = self.store.latest_process_result
        def racing_read(pk):
            result = original(pk)
            self.store.items[(self.pk, "META")]["version"] += 1
            return result
        with patch.object(self.store, "latest_process_result", side_effect=racing_read):
            result = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        self.assertEqual(result.status_code, 409)

    async def test_process_pointer_change_rejects_mixed_snapshot(self):
        original = self.store.latest_process_result
        def racing_read(pk):
            result = original(pk)
            self.store.items[(self.pk, "PROCESS#LATEST_RESTATED")]["version"] += 1
            return result
        with patch.object(self.store, "latest_process_result", side_effect=racing_read):
            result = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        self.assertEqual(result.status_code, 409)

    async def test_blocked_evaluation_and_missing_derivation_are_not_current(self):
        self.store.items[(self.pk, "META")]["evaluation_blocked"] = True
        del self.store.items[(self.pk, "PROCESS#LATEST_RESTATED")]
        result = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        value = result.json()
        self.assertIsNone(value["recorded"]["quality"])
        self.assertIsNone(value["observation_coverage"])
        self.assertEqual(value["process_view"], "AS_PROCESSED")
        self.assertIn("OBSERVATION_COVERAGE", value["unverified_categories"])

    async def test_inconsistent_assessment_pointer_is_not_trusted(self):
        self.store.items[(self.pk, "ASSESSMENT#CURRENT")]["score_percent"] = "100"
        response = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        self.assertIsNone(response.json()["recorded"]["quality"])
        self.assertIn("CURRENT_QUALITY_ASSESSMENT", response.json()["unverified_categories"])
