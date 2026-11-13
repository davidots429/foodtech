import asyncio
import os
import unittest
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore, MemoryCookingStore
from foodtech_business_api.main import app


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject = "customer-1"
        self.store = MemoryCookingStore()
        self.baseline_id, self.candidate_id = str(uuid4()), str(uuid4())

        async def session(_request, surface):
            self.assertEqual(surface, "customer")
            return Session("customer", self.subject, None, "test-csrf", 9999999999, 0)

        async def inline_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store),
                        patch.object(asyncio, "to_thread", side_effect=inline_thread)]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self._job(self.baseline_id, 3, "recipe-v1")
        self._job(self.candidate_id, 4, "recipe-v2")

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    def _job(self, job_id, score, recipe, *, equipment="synthetic-fryer"):
        pk = cooking._pk(self.subject, job_id)
        assessment_sk = "ASSESSMENT#REV#000000000001"
        self.store.items[(pk, "META")] = {
            "job_id": job_id, "version": 3, "status": "COMPLETED", "safety_status": "PASSED",
            "safety_version": 1, "evaluation_blocked": False, "menu": "TONKATSU",
            "equipment_profile": equipment, "recipe_version": recipe,
        }
        self.store.items[(pk, "ASSESSMENT#CURRENT")] = {
            "version": 1, "usable": True, "safety_version": 1, "assessment_sk": assessment_sk,
        }
        self.store.items[(pk, assessment_sk)] = {
            "sk": assessment_sk, "version": 1, "status": "COMPLETED",
            "menu": "TONKATSU",
            "scores": {name: score for name in cooking.SCORE_ITEMS},
            "score_percent": "60.00" if score == 3 else "80.00",
            "quality_passed": True, "quality_target": score == 4,
            "scale_version": "tonkatsu-common-v1", "equipment_profile": equipment,
            "load_band": "PARTIAL", "actual_portions": 2, "actual_mass_kg": "0.40",
            "load_percent": "50.00",
        }
        return pk

    def _path(self):
        return f"/api/customer/cooking-jobs/{self.candidate_id}/comparisons"

    async def test_comparison_is_versioned_and_idempotent(self):
        body = {"baseline_job_id": self.baseline_id}
        self.assertEqual((await self.client.post(self._path(), json=body)).status_code, 403)
        first = await self.client.post(self._path(), json=body, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(first.status_code, 201, first.text)
        result = first.json()
        self.assertEqual(result["score_total_delta"], 7)
        self.assertEqual(result["improvement_status"], "OBSERVED")
        self.assertEqual(result["recipe_improvement_status"], "NOT_EVALUATED")
        self.assertEqual(result["baseline_assessment_version"], 1)
        self.assertEqual(result["candidate_assessment_version"], 1)
        self.assertEqual(len([key for key in self.store.items if key[1].startswith("COMPARISON#")]), 1)
        second = await self.client.post(self._path(), json=body, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(second.json()["comparison_id"], result["comparison_id"])
        self.assertEqual(len([key for key in self.store.items if key[1].startswith("COMPARISON#")]), 1)
        stored = await self.client.get(self._path() + "/" + result["comparison_id"])
        self.assertEqual(stored.json()["score_total_delta"], 7)
        self.assertEqual((await self.client.get(self._path() + "/" + "x" * 64)).status_code, 400)

    async def test_safety_and_tenant_boundaries(self):
        body = {"baseline_job_id": self.baseline_id}
        baseline_pk = cooking._pk(self.subject, self.baseline_id)
        self.store.items[(baseline_pk, "META")]["safety_status"] = "FAILED"
        blocked = await self.client.post(self._path(), json=body, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(blocked.status_code, 409)
        self.store.items[(baseline_pk, "META")]["safety_status"] = "PASSED"
        self.subject = "another-customer"
        self.assertEqual((await self.client.post(self._path(), json=body,
                                                headers={"X-CSRF-Token": "test-csrf"})).status_code, 404)

    async def test_incompatible_equipment_is_not_compared(self):
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        self.store.items[(candidate_pk, "META")]["equipment_profile"] = "other-device"
        result = await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                                        headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(result.status_code, 422)
        self.assertFalse(any(key[1].startswith("COMPARISON#") for key in self.store.items))

    async def test_reassessment_creates_new_record_and_preserves_old_one(self):
        body = {"baseline_job_id": self.baseline_id}
        old = (await self.client.post(self._path(), json=body,
                                      headers={"X-CSRF-Token": "test-csrf"})).json()
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        meta = self.store.items[(candidate_pk, "META")]
        pointer = self.store.items[(candidate_pk, "ASSESSMENT#CURRENT")]
        revision = self.store.items[(candidate_pk, "ASSESSMENT#REV#000000000001")].copy()
        revision["sk"] = "ASSESSMENT#REV#000000000002"
        revision["version"] = 2
        revision["scores"] = {name: 3 for name in cooking.SCORE_ITEMS}
        revision["scores"]["taste"] = 4
        revision["score_percent"] = "62.86"
        self.store.items[(candidate_pk, revision["sk"])] = revision
        meta["version"] += 1
        pointer["version"] = 2
        pointer["assessment_sk"] = revision["sk"]
        new = (await self.client.post(self._path(), json=body,
                                      headers={"X-CSRF-Token": "test-csrf"})).json()
        self.assertNotEqual(new["comparison_id"], old["comparison_id"])
        self.assertEqual(new["score_total_delta"], 1)
        self.assertEqual(new["improvement_status"], "NOT_OBSERVED")
        historical = (await self.client.get(self._path() + "/" + old["comparison_id"])).json()
        self.assertEqual(historical["score_total_delta"], 7)

    async def test_safety_change_between_read_and_write_rejects_comparison(self):
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        original = self.store.save_comparison

        def safety_changes(*args):
            self.store.items[(candidate_pk, "META")]["safety_status"] = "FAILED"
            return original(*args)

        with patch.object(self.store, "save_comparison", side_effect=safety_changes):
            response = await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                                              headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(key[1].startswith("COMPARISON#") for key in self.store.items))


class ComparisonTransactionContractTests(unittest.TestCase):
    def test_dynamo_write_checks_both_current_jobs_and_pointers(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-cooking")
        store.client = MagicMock()
        store.serializer = TypeSerializer()
        meta = {"version": 3, "safety_version": 1}
        pointer = {"version": 1}
        record = {"comparison_id": "a" * 64, "score_total_delta": 2}
        store.save_comparison("baseline", "candidate", meta, meta, pointer, pointer, record)
        items = store.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(items), 5)
        self.assertEqual(sum("ConditionCheck" in item for item in items), 4)
        self.assertEqual(sum("Put" in item for item in items), 1)
        self.assertIn("usable = :true", items[1]["ConditionCheck"]["ConditionExpression"])
        self.assertIn("evaluation_blocked = :false", items[0]["ConditionCheck"]["ConditionExpression"])
