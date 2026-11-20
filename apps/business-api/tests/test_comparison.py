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
from foodtech_business_api.comparison import refresh_dependents
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore
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
            "safety_version": 1, "evaluation_blocked": False, "menu": "TONKATSU", "provenance": "SYNTHETIC",
            "equipment_profile": equipment, "recipe_version": recipe,
        }
        self.store.items[(pk, "ASSESSMENT#CURRENT")] = {
            "version": 1, "usable": True, "safety_version": 1, "assessment_sk": assessment_sk,
            "quality_passed": True, "score_percent": "60.00" if score == 3 else "80.00",
        }
        self.store.items[(pk, assessment_sk)] = {
            "sk": assessment_sk, "version": 1, "status": "COMPLETED",
            "menu": "TONKATSU",
            "scores": {name: score for name in cooking.SCORE_ITEMS},
            "score_percent": "60.00" if score == 3 else "80.00",
            "quality_passed": True, "quality_target": score == 4,
            "safety_version": 1,
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
        self.assertEqual(len([key for key in self.store.items
                              if key[1] == f"COMPARISON#{result['comparison_id']}"]), 1)
        current_path = self._path() + f"/from/{self.baseline_id}/current"
        self.assertEqual((await self.client.get(current_path)).json()["comparison_id"], result["comparison_id"])
        for job_id in (self.baseline_id, self.candidate_id):
            self.assertEqual(len(self.store.comparison_dependencies(cooking._pk(self.subject, job_id))), 1)
        second = await self.client.post(self._path(), json=body, headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(second.json()["comparison_id"], result["comparison_id"])
        self.assertEqual(len([key for key in self.store.items
                              if key[1] == f"COMPARISON#{result['comparison_id']}"]), 1)
        stored = await self.client.get(self._path() + "/" + result["comparison_id"])
        self.assertEqual(stored.json()["score_total_delta"], 7)
        self.assertEqual((await self.client.get(self._path() + "/" + "x" * 64)).status_code, 400)

    async def test_selected_previous_comparison_is_owned_current_and_not_claimed_immediate(self):
        baseline_pk = cooking._pk(self.subject, self.baseline_id)
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        self.store.items[(baseline_pk, "META")]["actual_lifted_at"] = "2026-11-14T00:00:00Z"
        self.store.items[(candidate_pk, "META")]["actual_lifted_at"] = "2026-11-14T01:00:00Z"
        comparison = (await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                      headers={"X-CSRF-Token": "test-csrf"})).json()
        path = f"/api/customer/cooking-jobs/{self.candidate_id}/recommendation-evidence"
        self.assertEqual((await self.client.get(path, params={"comparison_id": "invalid"})).status_code, 400)
        self.assertEqual((await self.client.get(path, params={"comparison_id": "0" * 64})).status_code, 404)
        response = await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})
        self.assertEqual(response.status_code, 200, response.text)
        selected = response.json()["selected_comparison"]
        self.assertEqual((selected["baseline_job_id"], selected["baseline_score_percent"]), (self.baseline_id, "60.00"))
        self.assertFalse(selected["immediately_previous_verified"])
        self.assertIn("COMPARABLE_PREVIOUS_JOB", response.json()["unverified_categories"])
        baseline_assessment = self.store.items[(baseline_pk, "ASSESSMENT#REV#000000000001")]
        baseline_assessment["score_percent"] = "99.00"
        self.assertEqual((await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})).status_code, 409)
        baseline_assessment["score_percent"] = "60.00"
        self.store.items[(baseline_pk, "META")]["version"] += 1
        self.assertEqual((await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})).status_code, 409)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})).status_code, 404)

    async def test_selected_comparison_rejects_later_or_undated_baseline(self):
        comparison = (await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                      headers={"X-CSRF-Token": "test-csrf"})).json()
        baseline = self.store.items[(cooking._pk(self.subject, self.baseline_id), "META")]
        candidate = self.store.items[(cooking._pk(self.subject, self.candidate_id), "META")]
        candidate["actual_lifted_at"] = "2026-11-14T00:00:00Z"
        path = f"/api/customer/cooking-jobs/{self.candidate_id}/recommendation-evidence"
        self.assertEqual((await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})).status_code, 409)
        baseline["actual_lifted_at"] = "2026-11-14T01:00:00Z"
        self.assertEqual((await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})).status_code, 409)

    async def test_selected_comparison_rejects_baseline_change_during_read(self):
        baseline_pk = cooking._pk(self.subject, self.baseline_id)
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        self.store.items[(baseline_pk, "META")]["actual_lifted_at"] = "2026-11-14T00:00:00Z"
        self.store.items[(candidate_pk, "META")]["actual_lifted_at"] = "2026-11-14T01:00:00Z"
        comparison = (await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                      headers={"X-CSRF-Token": "test-csrf"})).json()
        original = self.store.current_comparison
        def race(pk, baseline_id):
            result = original(pk, baseline_id)
            self.store.items[(baseline_pk, "META")]["version"] += 1
            return result
        path = f"/api/customer/cooking-jobs/{self.candidate_id}/recommendation-evidence"
        with patch.object(self.store, "current_comparison", side_effect=race):
            response = await self.client.get(path, params={"comparison_id": comparison["comparison_id"]})
        self.assertEqual(response.status_code, 409)

    async def test_current_comparison_list_is_scoped_and_paginated(self):
        other_baseline_id = str(uuid4())
        self._job(other_baseline_id, 3, "recipe-v1")
        for baseline_id in (self.baseline_id, other_baseline_id):
            result = await self.client.post(self._path(), json={"baseline_job_id": baseline_id},
                                            headers={"X-CSRF-Token": "test-csrf"})
            self.assertEqual(result.status_code, 201, result.text)
        first = await self.client.get(self._path(), params={"limit": 1})
        self.assertEqual(first.status_code, 200, first.text)
        first_page = first.json()
        self.assertEqual(len(first_page["items"]), 1)
        self.assertTrue(first_page["next_cursor"])
        second = await self.client.get(self._path(), params={"limit": 1,
                                                             "cursor": first_page["next_cursor"]})
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(second.json()["items"]), 1)
        self.assertIsNone(second.json()["next_cursor"])
        self.assertEqual({item["baseline_job_id"] for item in first_page["items"] + second.json()["items"]},
                         {self.baseline_id, other_baseline_id})
        self.assertEqual((await self.client.get(self._path(), params={"cursor": "bad"})).status_code, 400)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(self._path())).status_code, 404)

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

    async def test_refresh_backfills_legacy_comparison_index(self):
        original = (await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                    headers={"X-CSRF-Token": "test-csrf"})).json()
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        dependency_sk = f"COMPARISON#DEP#{self.candidate_id}#{self.baseline_id}"
        for job_id in (self.baseline_id, self.candidate_id):
            self.store.items.pop((cooking._pk(self.subject, job_id), dependency_sk))
        self.store.items.pop((candidate_pk, f"COMPARISON#CURRENT#{self.baseline_id}"))
        current_path = self._path() + f"/from/{self.baseline_id}/current"
        self.assertEqual((await self.client.get(current_path)).status_code, 404)
        repaired = await self.client.post(self._path() + "/" + original["comparison_id"] + "/refresh",
                                          headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(repaired.status_code, 201, repaired.text)
        self.assertEqual(repaired.json()["comparison_id"], original["comparison_id"])
        self.assertEqual((await self.client.get(current_path)).json()["comparison_id"],
                         original["comparison_id"])
        for job_id in (self.baseline_id, self.candidate_id):
            self.assertEqual(len(self.store.comparison_dependencies(cooking._pk(self.subject, job_id))), 1)

    async def test_incompatible_equipment_is_not_compared(self):
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        self.store.items[(candidate_pk, "META")]["equipment_profile"] = "other-device"
        result = await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                                        headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(result.status_code, 422)
        self.assertFalse(any(key[1].startswith("COMPARISON#") for key in self.store.items))

    async def test_jeyuk_adjacent_load_bands_are_comparable(self):
        for job_id, band in ((self.baseline_id, "REFERENCE"),
                             (self.candidate_id, "OVER_RECOMMENDED")):
            pk = cooking._pk(self.subject, job_id)
            self.store.items[(pk, "META")].update(menu="JEYUK", equipment_profile="jeyuk-small")
            self.store.items[(pk, "ASSESSMENT#REV#000000000001")].update(
                menu="JEYUK", equipment_profile="jeyuk-small",
                scale_version="jeyuk-common-v1", load_band=band,
                actual_mass_kg="1.8" if band == "REFERENCE" else "2.5",
                load_percent="100" if band == "REFERENCE" else "138.89")
        result = await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                                        headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(result.status_code, 201, result.text)
        self.assertEqual(result.json()["load_band_relation"], "ADJACENT")
        self.assertEqual(result.json()["improvement_status"], "OBSERVED")

        baseline_pk = cooking._pk(self.subject, self.baseline_id)
        self.store.items[(baseline_pk, "ASSESSMENT#REV#000000000001")]["load_band"] = "PARTIAL"
        rejected = await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                                          headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(rejected.status_code, 422)

    async def test_jeyuk_comparison_requires_matching_scale_and_current_safety(self):
        for job_id in (self.baseline_id, self.candidate_id):
            pk = cooking._pk(self.subject, job_id)
            self.store.items[(pk, "META")].update(menu="JEYUK", equipment_profile="jeyuk-small")
            self.store.items[(pk, "ASSESSMENT#REV#000000000001")].update(
                menu="JEYUK", equipment_profile="jeyuk-small", scale_version="jeyuk-common-v1")
        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        self.store.items[(candidate_pk, "ASSESSMENT#REV#000000000001")]["scale_version"] = "jeyuk-common-v2"
        body = {"baseline_job_id": self.baseline_id}
        self.assertEqual((await self.client.post(self._path(), json=body, headers={
            "X-CSRF-Token": "test-csrf"})).status_code, 422)
        self.store.items[(candidate_pk, "ASSESSMENT#REV#000000000001")]["scale_version"] = "jeyuk-common-v1"
        self.store.items[(candidate_pk, "ASSESSMENT#CURRENT")]["safety_version"] = 0
        self.assertEqual((await self.client.post(self._path(), json=body, headers={
            "X-CSRF-Token": "test-csrf"})).status_code, 409)

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
        self.assertEqual((await self.client.get(self._path() + f"/from/{self.baseline_id}/current"))
                         .json()["comparison_id"], new["comparison_id"])
        listed = (await self.client.get(self._path())).json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["comparison_id"], new["comparison_id"])
        historical = (await self.client.get(self._path() + "/" + old["comparison_id"])).json()
        self.assertEqual(historical["score_total_delta"], 7)
        history_path = self._path() + "/history"
        first_page = (await self.client.get(history_path, params={"limit": 1})).json()
        self.assertEqual(len(first_page["items"]), 1)
        self.assertTrue(first_page["next_cursor"])
        second_page = (await self.client.get(history_path, params={
            "limit": 1, "cursor": first_page["next_cursor"]})).json()
        self.assertEqual(len(second_page["items"]), 1)
        self.assertIsNone(second_page["next_cursor"])
        self.assertEqual({item["comparison_id"] for item in first_page["items"] + second_page["items"]},
                         {old["comparison_id"], new["comparison_id"]})
        self.assertEqual((await self.client.get(history_path, params={"cursor": "invalid"})).status_code, 400)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(history_path)).status_code, 404)

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

    async def test_status_and_refresh_keep_historical_comparison(self):
        first = await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                                       headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(first.status_code, 201, first.text)
        original = first.json()
        path = self._path() + "/" + original["comparison_id"]
        self.assertEqual((await self.client.get(path + "/status")).json()["status"], "CURRENT")
        self.assertEqual((await self.client.post(path + "/refresh")).status_code, 403)
        same = await self.client.post(path + "/refresh", headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(same.status_code, 201, same.text)
        self.assertEqual(same.json()["comparison_id"], original["comparison_id"])

        candidate_pk = cooking._pk(self.subject, self.candidate_id)
        meta = self.store.items[(candidate_pk, "META")]
        pointer = self.store.items[(candidate_pk, "ASSESSMENT#CURRENT")]
        revision = self.store.items[(candidate_pk, pointer["assessment_sk"])].copy()
        revision["sk"] = "ASSESSMENT#REV#000000000002"
        revision["version"] = 2
        revision["scores"] = {name: 3 for name in cooking.SCORE_ITEMS}
        revision["score_percent"] = "60.00"
        self.store.items[(candidate_pk, revision["sk"])] = revision
        meta["version"] += 1
        pointer["version"] = 2
        pointer["assessment_sk"] = revision["sk"]
        stale = (await self.client.get(path + "/status")).json()
        self.assertEqual(stale["status"], "STALE")
        self.assertEqual(stale["changed_job_ids"], [self.candidate_id])
        updated = await self.client.post(path + "/refresh", headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(updated.status_code, 201, updated.text)
        newer = updated.json()
        self.assertNotEqual(newer["comparison_id"], original["comparison_id"])
        self.assertEqual(newer["score_total_delta"], 0)
        self.assertEqual((await self.client.get(path)).json()["score_total_delta"], 7)
        self.assertEqual((await self.client.get(self._path() + "/" + newer["comparison_id"] + "/status"))
                         .json()["status"], "CURRENT")
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path + "/status")).status_code, 404)
        self.assertEqual((await self.client.post(path + "/refresh",
                         headers={"X-CSRF-Token": "test-csrf"})).status_code, 404)

    async def test_safety_invalidation_marks_stale_and_blocks_refresh(self):
        original = (await self.client.post(self._path(), json={"baseline_job_id": self.baseline_id},
                    headers={"X-CSRF-Token": "test-csrf"})).json()
        baseline_pk = cooking._pk(self.subject, self.baseline_id)
        self.store.items[(baseline_pk, "ASSESSMENT#CURRENT")]["usable"] = False
        path = self._path() + "/" + original["comparison_id"]
        stale = (await self.client.get(path + "/status")).json()
        self.assertEqual(stale["status"], "STALE")
        self.assertEqual(stale["changed_job_ids"], [self.baseline_id])
        blocked = await self.client.post(path + "/refresh", headers={"X-CSRF-Token": "test-csrf"})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual((await refresh_dependents(self.store, baseline_pk))["blocked_group_count"], 1)
        self.assertEqual((await self.client.get(path)).json()["score_total_delta"], 7)


class ComparisonTransactionContractTests(unittest.TestCase):
    def test_dynamo_history_query_excludes_pointer_keys_and_exact_end_cursor(self):
        store = object.__new__(DynamoCookingStore)
        store.table = MagicMock()
        candidate_pk = "SCOPE#USER#owner#JOB#candidate"
        result_id = "a" * 64
        item = {"pk": candidate_pk, "sk": f"COMPARISON#{result_id}",
                "comparison_id": result_id, "baseline_job_id": str(uuid4()),
                "compared_at": "2026-11-15T00:00:00Z", "score_total_delta": 2,
                "improvement_status": "OBSERVED"}
        store.table.query.return_value = {"Items": [item]}
        page = store.list_comparison_results(candidate_pk, 1, result_id)
        self.assertEqual(page["items"][0]["comparison_id"], result_id)
        self.assertIsNone(page["next_cursor"])
        args = store.table.query.call_args.kwargs
        self.assertEqual(args["ExclusiveStartKey"], {"pk": candidate_pk, "sk": f"COMPARISON#{result_id}"})
        self.assertEqual(args["Limit"], 2)
        self.assertTrue(args["ConsistentRead"])
        self.assertEqual(args["KeyConditionExpression"].get_expression()["values"][1]
                         .get_expression()["operator"], "begins_with")

    def test_dynamo_history_skips_pointer_page_before_result(self):
        store = object.__new__(DynamoCookingStore)
        store.table = MagicMock()
        candidate_pk = "SCOPE#USER#owner#JOB#candidate"
        result_id = "b" * 64
        pointer_key = {"pk": candidate_pk, "sk": "COMPARISON#CURRENT#baseline"}
        store.table.query.side_effect = [
            {"Items": [pointer_key], "LastEvaluatedKey": pointer_key},
            {"Items": [{"pk": candidate_pk, "sk": f"COMPARISON#{result_id}",
                        "comparison_id": result_id, "baseline_job_id": str(uuid4()),
                        "compared_at": "2026-11-15T00:00:00Z", "score_total_delta": 2,
                        "improvement_status": "OBSERVED"}]},
        ]
        page = store.list_comparison_results(candidate_pk, 1, None)
        self.assertEqual([item["comparison_id"] for item in page["items"]], [result_id])
        self.assertIsNone(page["next_cursor"])
        self.assertEqual(store.table.query.call_args.kwargs["ExclusiveStartKey"], pointer_key)

    def test_dynamo_current_comparison_query_uses_candidate_partition_and_cursor(self):
        store = object.__new__(DynamoCookingStore)
        store.table = MagicMock()
        candidate_pk = "SCOPE#USER#owner#JOB#candidate"
        baseline_id = str(uuid4())
        store.table.query.return_value = {
            "Items": [{"pk": candidate_pk, "sk": f"COMPARISON#CURRENT#{baseline_id}",
                       "comparison_id": "a" * 64, "updated_at": "2026-11-15T00:00:00Z"}],
            "LastEvaluatedKey": {"pk": candidate_pk, "sk": f"COMPARISON#CURRENT#{baseline_id}"},
        }
        page = store.list_current_comparisons(candidate_pk, 1, baseline_id)
        self.assertEqual(page["items"][0]["baseline_job_id"], baseline_id)
        self.assertEqual(page["next_cursor"], baseline_id)
        args = store.table.query.call_args.kwargs
        self.assertEqual(args["ExclusiveStartKey"],
                         {"pk": candidate_pk, "sk": f"COMPARISON#CURRENT#{baseline_id}"})
        self.assertEqual(args["Limit"], 1)
        self.assertTrue(args["ConsistentRead"])

    def test_dynamo_write_checks_both_current_jobs_and_pointers(self):
        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-cooking")
        store.client = MagicMock()
        store.serializer = TypeSerializer()
        meta = {"version": 3, "safety_version": 1}
        pointer = {"version": 1}
        record = {"comparison_id": "a" * 64, "score_total_delta": 2,
                  "baseline_job_id": "baseline-id", "candidate_job_id": "candidate-id",
                  "compared_at": "2026-11-15T00:00:00Z"}
        store.save_comparison("baseline", "candidate", meta, meta, pointer, pointer, record)
        items = store.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(items), 8)
        self.assertEqual(sum("ConditionCheck" in item for item in items), 4)
        self.assertEqual(sum("Put" in item for item in items), 4)
        self.assertIn("usable = :true", items[1]["ConditionCheck"]["ConditionExpression"])
        self.assertIn("evaluation_blocked = :false", items[0]["ConditionCheck"]["ConditionExpression"])

    def test_existing_result_does_not_hide_stale_input(self):
        class Canceled(Exception):
            def __init__(self, codes):
                self.response = {"CancellationReasons": [{"Code": code} for code in codes]}

        store = object.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-cooking")
        store.client = MagicMock()
        store.client.exceptions.TransactionCanceledException = Canceled
        store.serializer = TypeSerializer()
        meta = {"version": 3, "safety_version": 1}
        pointer = {"version": 1}
        record = {"comparison_id": "a" * 64, "baseline_job_id": "baseline-id",
                  "candidate_job_id": "candidate-id", "compared_at": "2026-11-15T00:00:00Z"}
        with patch.object(store, "comparison", return_value=record):
            first_canceled = Canceled(["None"] * 7 + ["ConditionalCheckFailed"])
            store.client.transact_write_items.side_effect = [first_canceled, None]
            self.assertEqual(store.save_comparison("baseline", "candidate", meta, meta,
                                                   pointer, pointer, record), record)
            self.assertEqual(store.client.transact_write_items.call_count, 2)
            repair = store.client.transact_write_items.call_args.kwargs["TransactItems"]
            self.assertEqual(len(repair), 8)
            self.assertIn("if_not_exists", repair[-1]["Update"]["UpdateExpression"])
            store.client.transact_write_items.side_effect = Canceled(
                ["ConditionalCheckFailed"] + ["None"] * 6 + ["ConditionalCheckFailed"])
            with self.assertRaises(Conflict):
                store.save_comparison("baseline", "candidate", meta, meta,
                                      pointer, pointer, record)
