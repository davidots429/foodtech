import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore, MemoryCookingStore
from foodtech_business_api.main import app


class AggregateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.subject = "customer-1"
        self.store = MemoryCookingStore()
        self.first_id, self.second_id = str(uuid4()), str(uuid4())
        self.now = datetime.now(timezone.utc)

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
        self._job(self.first_id, 4)
        self._job(self.second_id, 3, safety="FAILED")

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    def _job(self, job_id: str, score: int, *, safety="PASSED", completed_at=None):
        pk = cooking._pk(self.subject, job_id)
        sk = "ASSESSMENT#REV#000000000001"
        self.store.items[(pk, "META")] = {
            "job_id": job_id, "version": 3, "safety_version": 1, "status": "COMPLETED",
            "safety_status": safety, "evaluation_blocked": safety != "PASSED",
            "menu": "TONKATSU", "equipment_profile": "synthetic-fryer", "recipe_version": "recipe-v1",
        }
        self.store.items[(pk, "ASSESSMENT#CURRENT")] = {
            "version": 1, "usable": safety == "PASSED", "safety_version": 1,
            "assessment_sk": sk,
        }
        self.store.items[(pk, sk)] = {
            "sk": sk, "status": "COMPLETED", "menu": "TONKATSU",
            "equipment_profile": "synthetic-fryer", "scale_version": "tonkatsu-common-v1",
            "load_band": "PARTIAL", "completed_at": (completed_at or self.now).isoformat(),
            "scores": {name: score for name in cooking.SCORE_ITEMS},
            "score_percent": "80.00" if score == 4 else "60.00",
            "quality_passed": True, "quality_target": score == 4,
        }
        return pk

    def _body(self):
        return {"job_ids": [self.first_id, self.second_id],
                "period_start": (self.now - timedelta(days=1)).isoformat(),
                "period_end": (self.now + timedelta(days=1)).isoformat(),
                "recipe_version": "recipe-v1", "load_band": "PARTIAL"}

    async def test_exclusion_recalculation_and_immutable_history(self):
        body = self._body()
        self.assertEqual((await self.client.post("/api/customer/assessment-aggregates", json=body)).status_code, 403)
        first = await self.client.post("/api/customer/assessment-aggregates", json=body,
                                       headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(first.status_code, 201, first.text)
        original = first.json()
        self.assertEqual(original["sample_count"], 1)
        self.assertEqual(original["excluded_count"], 1)
        self.assertEqual(original["excluded"][0]["reason"], "SAFETY_NOT_PASSED")
        self.assertEqual(original["mean_quality_score_percent"], "80.00")
        self.assertEqual(original["quality_pass_rate_percent"], "100.00")
        self.assertEqual(original["recipe_improvement_status"], "NOT_EVALUATED")
        self.assertEqual(original["representativeness_status"], "NOT_EVALUATED")
        repeated = await self.client.post("/api/customer/assessment-aggregates", json=body,
                                          headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(repeated.json()["result_id"], original["result_id"])
        second_pk = cooking._pk(self.subject, self.second_id)
        self.store.items[(second_pk, "META")].update(version=4, safety_status="PASSED", evaluation_blocked=False)
        self.store.items[(second_pk, "ASSESSMENT#CURRENT")]["usable"] = True
        revised = (await self.client.post("/api/customer/assessment-aggregates", json=body,
                                          headers={"X-CSRF-Token": "test-csrf"})).json()
        self.assertEqual(revised["group_id"], original["group_id"])
        self.assertNotEqual(revised["result_id"], original["result_id"])
        self.assertEqual(revised["sample_count"], 2)
        self.assertEqual(revised["mean_quality_score_percent"], "70.00")
        path = f"/api/customer/assessment-aggregates/{original['group_id']}/results/{original['result_id']}"
        self.assertEqual((await self.client.get(path)).json()["sample_count"], 1)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_empty_denominator_is_not_zero(self):
        first_pk = cooking._pk(self.subject, self.first_id)
        self.store.items[(first_pk, "META")]["safety_status"] = "FAILED"
        result = (await self.client.post("/api/customer/assessment-aggregates", json=self._body(),
                                         headers={"X-CSRF-Token": "test-csrf"})).json()
        self.assertEqual(result["sample_count"], 0)
        self.assertIsNone(result["mean_quality_score_percent"])
        self.assertIsNone(result["quality_pass_rate_percent"])
        self.assertIsNone(result["item_score_means"])

    async def test_period_and_duplicate_inputs_are_rejected(self):
        body = self._body()
        duplicate = await self.client.post("/api/customer/assessment-aggregates",
                                           json={**body, "job_ids": [self.first_id, self.first_id]},
                                           headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(duplicate.status_code, 422)
        old_period = await self.client.post("/api/customer/assessment-aggregates",
                                            json={**body, "period_end": body["period_start"]},
                                            headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(old_period.status_code, 422)

    async def test_outside_period_is_reported_as_excluded(self):
        first_pk = cooking._pk(self.subject, self.first_id)
        self.store.items[(first_pk, "ASSESSMENT#REV#000000000001")]["completed_at"] = \
            (self.now - timedelta(days=30)).isoformat()
        result = (await self.client.post("/api/customer/assessment-aggregates", json=self._body(),
                                         headers={"X-CSRF-Token": "test-csrf"})).json()
        self.assertEqual(result["sample_count"], 0)
        self.assertCountEqual([item["reason"] for item in result["excluded"]],
                              ["OUTSIDE_PERIOD", "SAFETY_NOT_PASSED"])

    async def test_safety_change_during_write_rejects_snapshot(self):
        first_pk = cooking._pk(self.subject, self.first_id)
        original_save = self.store.save_aggregate

        def safety_changes(*args):
            self.store.items[(first_pk, "META")]["safety_status"] = "FAILED"
            return original_save(*args)

        with patch.object(self.store, "save_aggregate", side_effect=safety_changes):
            response = await self.client.post("/api/customer/assessment-aggregates", json=self._body(),
                                              headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(any(key[1].startswith("RESULT#") for key in self.store.items))


class AggregateTransactionContractTests(unittest.TestCase):
    def test_dynamo_write_checks_all_selected_job_snapshots(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-cooking")
        store.client = MagicMock()
        store.serializer = TypeSerializer()
        meta = {"version": 3, "safety_version": 1, "status": "COMPLETED",
                "safety_status": "PASSED", "evaluation_blocked": False}
        pointer = {"version": 1, "usable": True, "safety_version": 1}
        store.save_aggregate("aggregate", [("job-1", meta, pointer), ("job-2", meta, None)],
                             {"result_id": "a" * 64, "sample_count": 1})
        operations = store.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(operations), 5)
        self.assertEqual(sum("ConditionCheck" in operation for operation in operations), 4)
        self.assertEqual(sum("Put" in operation for operation in operations), 1)
        self.assertIn("attribute_not_exists(pk)", operations[3]["ConditionCheck"]["ConditionExpression"])
