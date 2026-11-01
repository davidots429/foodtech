import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore, MemoryCookingStore
from foodtech_contracts import stream_binding_key
from foodtech_business_api.main import app


SCORES = {name: 4 for name in cooking.SCORE_ITEMS}
OBSERVATIONS = {name: "합성 시나리오 관찰값" for name in cooking.SCORE_ITEMS}
CSRF = {"X-CSRF-Token": "test-csrf"}


def probe(item_id, temperature="75.0", calibration="OK", missing_second=None):
    return {"item_id": item_id, "calibration_status": calibration,
            "samples": [{"at_second": second, "celsius": temperature}
                        for second in range(61) if second != missing_second]}


class CookingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}, clear=False)
        self.env.start()
        self.store = MemoryCookingStore()
        self.subject = "customer-1"

        async def session(_request, surface):
            if surface != "customer":
                raise AssertionError("customer route required")
            return Session("customer", self.subject, None, "test-csrf", 9999999999, 0)

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

    async def create(self):
        response = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                                          json={"planned_portions": 2, "recipe_version": "recipe-v1"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def transition(self, job, event, reason=None):
        response = await self.client.post(
            f"/api/customer/cooking-jobs/{job['job_id']}/transitions", headers=CSRF,
            json={"event": event, "expected_version": job["version"], "reason": reason},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def completed_with_safety(self):
        job = await self.create()
        for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
            job = await self.transition(job, event)
        response = await self.client.post(
            f"/api/customer/cooking-jobs/{job['job_id']}/safety/core-check", headers=CSRF,
            json={"expected_version": job["version"], "probes": [probe("piece-1"), probe("piece-2", "76.1")]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["safety_status"], "PASSED")
        return response.json()

    def assessment_body(self, job, current=0, kind="INITIAL", reason=None):
        return {"expected_version": job["version"], "expected_current_version": current,
                "kind": kind, "reason": reason, "scores": SCORES.copy(), "observations": OBSERVATIONS.copy(),
                "scale_version": "tonkatsu-common-v1", "planned_portions": 2,
                "actual_portions": 2, "actual_mass_kg": "0.4", "load_percent": "50",
                "load_band": "PARTIAL"}

    async def test_completed_assessment_version_and_safety_block(self):
        job = await self.completed_with_safety()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        first = await self.client.post(path + "/assessments", headers=CSRF,
                                       json=self.assessment_body(job))
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["score_percent"], "80.00")
        self.assertTrue(first.json()["quality_target"])
        current = (await self.client.get(path)).json()
        self.assertEqual(current["current_assessment"]["version"], 1)
        self.assertTrue(current["current_assessment"]["usable"])

        stale = await self.client.post(path + "/assessments", headers=CSRF,
                                       json=self.assessment_body(job))
        self.assertEqual(stale.status_code, 409)
        job = current["job"]
        suspect = await self.client.post(path + "/safety/events", headers=CSRF,
                                         json={"event": "SUSPECTED", "expected_version": job["version"],
                                               "reason": "합성 이취", "manual_stop_confirmed": True})
        self.assertEqual(suspect.status_code, 200, suspect.text)
        current = (await self.client.get(path)).json()
        self.assertFalse(current["current_assessment"]["usable"])
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                                                 json=self.assessment_body(current["job"], 1, "REASSESSMENT", "재검"))).status_code, 409)

        cleared = await self.client.post(path + "/safety/events", headers=CSRF,
                                          json={"event": "CLEARED", "expected_version": current["job"]["version"],
                                                "reason": "합성 조사 결과 이상 없음"})
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse((await self.client.get(path)).json()["current_assessment"]["usable"])
        checked = await self.client.post(path + "/safety/core-check", headers=CSRF,
                                          json={"expected_version": cleared.json()["version"],
                                                "probes": [probe("piece-1"), probe("piece-2")]})
        self.assertEqual(checked.status_code, 200)
        self.assertFalse((await self.client.get(path)).json()["current_assessment"]["usable"])
        second = await self.client.post(path + "/assessments", headers=CSRF,
                                        json=self.assessment_body(checked.json(), 1, "REASSESSMENT", "의심 해제 후 재평가"))
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["previous_version"], 1)
        self.assertEqual([entry["version"] for entry in (await self.client.get(path + "/assessments")).json()], [1, 2])
        self.assertEqual((await self.client.get(path)).json()["current_assessment"]["version"], 2)
        events = (await self.client.get(path + "/events")).json()
        self.assertEqual(len(events), 7)
        self.assertEqual([event["domain"] for event in events].count("SAFETY_STATUS"), 4)

    async def test_safety_failure_and_separate_recook(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                                                 json=self.assessment_body(job))).status_code, 409)
        for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
            job = await self.transition(job, event)
        failed = await self.client.post(path + "/safety/core-check", headers=CSRF,
                                        json={"expected_version": job["version"],
                                              "probes": [probe("piece-1", "74.9"), probe("piece-2")]})
        self.assertEqual(failed.json()["safety_status"], "FAILED")
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                                                 json=self.assessment_body(failed.json()))).status_code, 409)
        recook = await self.client.post(path + "/recook", headers=CSRF,
                                        json={"expected_version": failed.json()["version"],
                                              "reason": "안전 기준 미달"})
        self.assertEqual(recook.status_code, 201, recook.text)
        self.assertEqual(recook.json()["source_job_id"], job["job_id"])
        self.assertNotEqual(recook.json()["job_id"], job["job_id"])
        self.assertEqual((await self.client.get(path)).json()["job"]["safety_status"], "FAILED")

    async def test_owner_and_csrf_boundary(self):
        response = await self.client.post("/api/customer/cooking-jobs",
                                          json={"planned_portions": 2, "recipe_version": "v1"})
        self.assertEqual(response.status_code, 403)
        job = await self.create()
        self.subject = "customer-2"
        self.assertEqual((await self.client.get(f"/api/customer/cooking-jobs/{job['job_id']}")).status_code, 404)

    async def test_stream_binding_is_explicit_unique_and_owner_scoped(self):
        first = await self.create()
        path = f"/api/customer/cooking-jobs/{first['job_id']}/source-binding"
        body = {"expected_version": first["version"], "source_id": "synthetic-rest",
                "device_id": "mockprofile-100-a-001", "batch_id": "batch-a-001"}
        self.assertEqual((await self.client.post(path, json=body)).status_code, 403)
        bound = await self.client.post(path, headers=CSRF, json=body)
        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["source_binding"]["batch_id"], "batch-a-001")
        self.assertEqual((await self.client.post(path, headers=CSRF, json=body)).status_code, 409)

        second = await self.create()
        second_path = f"/api/customer/cooking-jobs/{second['job_id']}/source-binding"
        body["expected_version"] = second["version"]
        self.assertEqual((await self.client.post(second_path, headers=CSRF, json=body)).status_code, 409)
        self.subject = "customer-2"
        self.assertEqual((await self.client.post(path, headers=CSRF, json=body)).status_code, 404)

    async def test_dynamodb_decimal_counters_can_form_event_keys(self):
        job = await self.create()
        key = (cooking._pk(self.subject, job["job_id"]), "META")
        self.store.items[key]["version"] = Decimal(1)
        self.store.items[key]["job_state_version"] = Decimal(1)
        changed = await self.transition(job, "PREHEAT_COMPLETED")
        self.assertEqual(changed["status"], "READY")
        events = (await self.client.get(f"/api/customer/cooking-jobs/{job['job_id']}/events")).json()
        self.assertEqual(events[0]["sk"], "EVENT#JOB#000000000002")

    async def test_quality_scale_and_load_boundary(self):
        job = await self.completed_with_safety()
        path = f"/api/customer/cooking-jobs/{job['job_id']}/assessments"
        body = self.assessment_body(job)
        body["scores"]["taste"] = 2
        body["actual_mass_kg"] = "0.41"
        body["load_percent"] = "51.25"
        self.assertEqual((await self.client.post(path, headers=CSRF, json=body)).status_code, 422)
        body["load_band"] = "REFERENCE"
        response = await self.client.post(path, headers=CSRF, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["quality_passed"])
        self.assertFalse(response.json()["quality_target"])

    async def test_missing_second_blocks_quality_completion(self):
        job = await self.create()
        for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
            job = await self.transition(job, event)
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        checked = await self.client.post(path + "/safety/core-check", headers=CSRF,
                                          json={"expected_version": job["version"],
                                                "probes": [probe("piece-1", missing_second=30), probe("piece-2")]})
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["safety_status"], "INCOMPLETE")
        assessment = await self.client.post(path + "/assessments", headers=CSRF,
                                            json=self.assessment_body(checked.json()))
        self.assertEqual(assessment.status_code, 409)


class DynamoTransactionContractTests(unittest.TestCase):
    def test_source_binding_and_job_version_share_transaction(self):
        calls = []
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-table")
        store.serializer = TypeSerializer()
        store.client = SimpleNamespace(transact_write_items=lambda **kwargs: calls.append(kwargs))
        stream_pk = stream_binding_key("synthetic-rest", "mockprofile-100-a-001", "batch-a-001")
        store.bind_stream("SCOPE#USER#one#JOB#job", 2, {"job_id": "job", "version": 3}, stream_pk)
        writes = calls[0]["TransactItems"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0]["Put"]["ConditionExpression"], "#version = :expected")
        self.assertEqual(writes[1]["Put"]["ConditionExpression"], "attribute_not_exists(pk)")
        self.assertEqual(writes[1]["Put"]["Item"]["pk"], {"S": stream_pk})

    def test_dynamodb_numeric_fields_match_local_api_types(self):
        rows = iter([
            {"Item": {"version": Decimal(3), "planned_portions": Decimal(2)}},
            {"Item": {"version": Decimal(1), "scores": {"taste": Decimal(4)}}},
        ])
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(get_item=lambda **_kwargs: next(rows))
        meta, pointer = store.read("job")
        self.assertEqual(meta["version"], 3)
        self.assertIs(type(meta["version"]), int)
        self.assertEqual(pointer["scores"]["taste"], 4)
        self.assertIs(type(pointer["scores"]["taste"]), int)

    def test_assessment_and_current_pointer_share_conditional_transaction(self):
        calls = []
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-table")
        store.serializer = TypeSerializer()
        store.client = SimpleNamespace(transact_write_items=lambda **kwargs: calls.append(kwargs))
        store.commit("SCOPE#USER#one#JOB#job", 4, {"version": 5},
                     {"sk": "ASSESSMENT#REV#000000000001", "version": 1},
                     {"version": 1, "usable": True})
        self.assertEqual(len(calls), 1)
        writes = calls[0]["TransactItems"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[0]["Put"]["ConditionExpression"], "#version = :expected")
        self.assertEqual(writes[1]["Put"]["ConditionExpression"], "attribute_not_exists(pk)")
        self.assertEqual(writes[2]["Put"]["Item"]["sk"], {"S": "ASSESSMENT#CURRENT"})

    def test_safety_state_and_immutable_event_share_transaction(self):
        calls = []
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-table")
        store.serializer = TypeSerializer()
        store.client = SimpleNamespace(transact_write_items=lambda **kwargs: calls.append(kwargs))
        store.commit("job", 3, {"version": 4, "safety_status": "SUSPECTED"},
                     event={"sk": "EVENT#SAFETY#000000000001", "occurred_at": "2026-11-02T00:00:00Z"})
        writes = calls[0]["TransactItems"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[1]["Put"]["ConditionExpression"], "attribute_not_exists(pk)")


if __name__ == "__main__":
    unittest.main()
