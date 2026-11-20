import os
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore, MemoryCookingStore
from foodtech_contracts import stream_binding_key
from foodtech_contracts.tonkatsu_process import cooking_time_status, process_status, thickness_profile
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
                                          json={"planned_portions": 2, "recipe_version": "recipe-v1",
                                                "thickness_cm": "1.20"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_tonkatsu_reheat_requires_fresh_samples_and_preserves_failure(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        start = datetime.now(timezone.utc) - timedelta(seconds=240)
        job = await self.transition(job, "PREHEAT_COMPLETED")
        for event, field, value in (("COOKING_STARTED", "actual_placed_at", start - timedelta(seconds=300)),
                                    ("COOKING_COMPLETED", "actual_lifted_at", start)):
            response = await self.client.post(path + "/transitions", headers=CSRF,
                json={"event": event, "expected_version": job["version"], field: value.isoformat()})
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
        def measured(item, temperature, at):
            return {**probe(item, temperature), "probe_id": "probe-" + item,
                    "removal_sequence": int(item), "measurement_start": at.isoformat(),
                    "measurement_end": (at + timedelta(seconds=60)).isoformat()}
        initial = [measured("1", "74", start), measured("2", "75", start)]
        failed = await self.client.post(path + "/safety/core-check", headers=CSRF,
                                        json={"expected_version": job["version"], "probes": initial})
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["safety_status"], "FAILED")
        reheated_at = start + timedelta(seconds=70)
        lifted = start + timedelta(seconds=120)
        request = {"expected_version": failed.json()["version"], "started_at": reheated_at.isoformat(),
                   "lifted_at": lifted.isoformat(), "reason": "안전 실패 후 추가 가열"}
        invalid = await self.client.post(path + "/safety/tonkatsu-reheat", headers=CSRF,
                                         json={**request, "started_at": start.isoformat()})
        self.assertEqual(invalid.status_code, 422)
        result = await self.client.post(path + "/safety/tonkatsu-reheat", headers=CSRF, json=request)
        self.assertEqual(result.status_code, 201, result.text)
        current = result.json()
        self.assertTrue(current["evaluation_blocked"])
        self.assertEqual(current["safety_status"], "INCOMPLETE")
        self.assertEqual((await self.client.post(path + "/safety/tonkatsu-reheat", headers=CSRF,
                                                 json=request)).status_code, 409)
        bad = await self.client.post(path + "/safety/core-check", headers=CSRF,
            json={"expected_version": current["version"], "probes": [measured("1", "75", start), measured("2", "75", start)]})
        self.assertEqual(bad.status_code, 422)
        missing = [measured("1", "75", lifted), measured("2", "75", lifted)]
        missing[0]["samples"][0]["celsius"] = None
        incomplete = await self.client.post(path + "/safety/core-check", headers=CSRF,
            json={"expected_version": current["version"], "probes": missing})
        self.assertEqual(incomplete.status_code, 200, incomplete.text)
        self.assertEqual(incomplete.json()["safety_status"], "INCOMPLETE")
        self.assertTrue(incomplete.json()["evaluation_blocked"])
        self.assertIsNotNone(incomplete.json()["pending_tonkatsu_reheat"])
        passed = await self.client.post(path + "/safety/core-check", headers=CSRF,
            json={"expected_version": incomplete.json()["version"], "probes": [measured("1", "75", lifted), measured("2", "75", lifted)]})
        self.assertEqual(passed.status_code, 200, passed.text)
        self.assertEqual(passed.json()["safety_status"], "PASSED")
        events = (await self.client.get(path + "/events")).json()
        safety = [event for event in events if event.get("domain") == "SAFETY_STATUS"]
        self.assertEqual([event["trigger"] for event in safety], ["CORE_CHECK", "TONKATSU_REHEATED", "CORE_CHECK", "CORE_CHECK"])
        self.assertEqual(safety[0]["to_status"], "FAILED")
        self.assertEqual(safety[1]["segment"]["segment_id"], safety[3]["reheat_segment_id"])
        evidence = await self.client.get(path + "/recommendation-evidence")
        self.assertEqual(evidence.status_code, 200, evidence.text)
        self.assertEqual(evidence.json()["reheat_history_status"], "RECORDED")
        self.assertEqual([check["status"] for check in evidence.json()["reheat_history"][0]["rechecks"]],
                         ["INCOMPLETE", "PASSED"])

    async def test_running_time_guidance_is_owned_read_only_and_uncached(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        ready = await self.client.post(path + "/transitions", headers=CSRF,
                                       json={"event": "PREHEAT_COMPLETED", "expected_version": job["version"]})
        self.assertEqual(ready.status_code, 200, ready.text)
        placed = datetime.now(timezone.utc) - timedelta(seconds=361)
        running = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_STARTED", "expected_version": ready.json()["version"], "actual_placed_at": placed.isoformat()})
        self.assertEqual(running.status_code, 200, running.text)
        response = await self.client.get(path + "/time-monitor")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["status"], "MAX_TIME_EXCEEDED")
        unchanged = (await self.client.get(path)).json()["job"]
        self.assertEqual((unchanged["status"], unchanged["version"]), ("RUNNING", running.json()["version"]))
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path + "/time-monitor")).status_code, 404)

    async def test_completion_does_not_clear_unknown_legacy_gap_history(self):
        job = await self.create()
        for event in ("PREHEAT_COMPLETED", "COOKING_STARTED"):
            job = await self.transition(job, event)
        pk = cooking._pk(self.subject, job["job_id"])
        meta = self.store.items[(pk, "META")]
        meta.update({"process_assessment": {"data_gap": False, "rule_version": "tonkatsu-temperature-v1"},
                     "process_temperature_status": "NORMAL", "process_assessment_status": "WARNING"})
        completed = await self.transition(job, "COOKING_COMPLETED")
        self.assertEqual(completed["process_time_status"], "NORMAL")
        self.assertEqual(completed["process_assessment_status"], "WARNING")

    async def test_completion_treats_previous_rule_history_as_unknown(self):
        job = await self.create()
        for event in ("PREHEAT_COMPLETED", "COOKING_STARTED"):
            job = await self.transition(job, event)
        meta = self.store.items[(cooking._pk(self.subject, job["job_id"]), "META")]
        meta.update({"process_assessment": {"data_gap": False, "gap_history_verified": True,
                                            "rule_version": "tonkatsu-temperature-v2"},
                     "process_temperature_status": "NORMAL", "process_assessment_status": "NORMAL"})
        completed = await self.transition(job, "COOKING_COMPLETED")
        self.assertEqual(completed["process_assessment_status"], "WARNING")

    async def test_owned_job_list_is_paginated_and_isolated(self):
        jobs = [await self.create() for _ in range(3)]
        first = await self.client.get("/api/customer/cooking-jobs", params={"limit": 2})
        self.assertEqual(first.status_code, 200, first.text)
        page = first.json()
        self.assertEqual([item["job_id"] for item in page["items"]],
                         [jobs[2]["job_id"], jobs[1]["job_id"]])
        self.assertTrue(page["next_cursor"])
        second = (await self.client.get("/api/customer/cooking-jobs",
                                        params={"limit": 2, "cursor": page["next_cursor"]})).json()
        self.assertEqual([item["job_id"] for item in second["items"]], [jobs[0]["job_id"]])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual((await self.client.get("/api/customer/cooking-jobs",
                                                params={"cursor": "bad"})).status_code, 400)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get("/api/customer/cooking-jobs")).json()["items"], [])

    async def test_known_legacy_job_can_be_registered_in_list(self):
        job = await self.create()
        listing_pk = f"SCOPE#USER#{self.subject}#JOBS"
        for key in list(self.store.items):
            if key[0] == listing_pk:
                self.store.items.pop(key)
        self.assertEqual((await self.client.get("/api/customer/cooking-jobs")).json()["items"], [])
        path = f"/api/customer/cooking-jobs/{job['job_id']}/list-entry"
        self.assertEqual((await self.client.post(path)).status_code, 403)
        self.assertEqual((await self.client.post(path, headers=CSRF)).status_code, 200)
        self.assertEqual((await self.client.post(path, headers=CSRF)).status_code, 200)
        self.assertEqual([item["job_id"] for item in
                          (await self.client.get("/api/customer/cooking-jobs")).json()["items"]],
                         [job["job_id"]])
        self.subject = "another-customer"
        self.assertEqual((await self.client.post(path, headers=CSRF)).status_code, 404)

    async def test_job_process_view_keeps_original_and_latest_derivation_separate(self):
        job = await self.create()
        pk = cooking._pk(self.subject, job["job_id"])
        result_sk = "RESTATEMENT#RESULT#synthetic"
        self.store.items[(pk, result_sk)] = {
            "pk": pk, "sk": result_sk, "status": "DERIVED_ONLY",
            "process_assessment_status": "NORMAL", "process_assessment": {"data_gap": False},
            "observation_coverage": {"version": "observed-cooking-span-v1", "sample_count": 2,
                "before_first_us": 1000000, "after_last_us": 0, "internal_gap_detected": False,
                "first_sample_at": "2026-11-14T00:00:01Z", "last_sample_at": "2026-11-14T00:00:06Z"},
            "job_version_at_derivation": job["version"], "created_at": "2026-11-14T00:00:00Z"}
        self.store.items[(pk, "PROCESS#LATEST_RESTATED")] = {
            "pk": pk, "sk": "PROCESS#LATEST_RESTATED", "version": 1, "result_sk": result_sk}
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        latest = await self.client.get(path)
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["process_assessment_view"]["status"], "NORMAL")
        self.assertEqual(latest.json()["process_assessment_view"]["view"], "LATEST_RESTATED")
        self.assertEqual(latest.json()["process_assessment_view"]["observation_coverage"]["before_first_us"], 1000000)
        self.assertEqual(latest.json()["job"]["process_assessment_status"], job["process_assessment_status"])
        original = await self.client.get(path, params={"process_view": "AS_PROCESSED"})
        self.assertEqual(original.json()["process_assessment_view"]["view"], "AS_PROCESSED")
        self.assertIsNone(original.json()["process_assessment_view"]["observation_coverage"])
        self.assertEqual(original.json()["process_assessment_view"]["status"], job["process_assessment_status"])
        self.assertEqual((await self.client.get(path, params={"process_view": "bad"})).status_code, 422)
        self.store.items[(pk, result_sk)].pop("observation_coverage")
        older = await self.client.get(path)
        self.assertIsNone(older.json()["process_assessment_view"]["observation_coverage"])
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def transition(self, job, event, reason=None):
        body = {"event": event, "expected_version": job["version"], "reason": reason}
        if event == "COOKING_STARTED":
            self.placed_at = datetime.now(timezone.utc) - timedelta(seconds=300)
            body["actual_placed_at"] = self.placed_at.isoformat()
        elif event == "COOKING_COMPLETED":
            body["actual_lifted_at"] = (self.placed_at + timedelta(seconds=300)).isoformat()
        response = await self.client.post(
            f"/api/customer/cooking-jobs/{job['job_id']}/transitions", headers=CSRF,
            json=body,
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

    async def test_approved_batch_uses_product_count_and_rated_capacity(self):
        created = await self.client.post("/api/customer/cooking-jobs", headers=CSRF, json={
            "planned_portions": 2, "recipe_version": "recipe-v1", "thickness_cm": "1.20",
            "equipment_profile": "synthetic-fryer-approved-v2"})
        self.assertEqual(created.status_code, 201, created.text)
        job = created.json()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        ready = await self.transition(job, "PREHEAT_COMPLETED")
        blocked = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_STARTED", "expected_version": ready["version"],
            "actual_placed_at": (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()})
        self.assertEqual(blocked.status_code, 409)
        job = ready
        body = {"expected_version": job["version"], "mode": "REFERENCE", "product_weights_g": [160] * 15}
        self.assertEqual((await self.client.post(path + "/tonkatsu-batch", json=body)).status_code, 403)
        bad = await self.client.post(path + "/tonkatsu-batch", headers=CSRF,
                                     json={**body, "product_weights_g": [160] * 16})
        self.assertEqual(bad.status_code, 422)
        recorded = await self.client.post(path + "/tonkatsu-batch", headers=CSRF, json=body)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        job = recorded.json()
        self.assertEqual(job["tonkatsu_batch"]["total_mass_kg"], "2.4")
        self.assertEqual(job["tonkatsu_batch"]["load_percent"], "60.00")
        for event in ("COOKING_STARTED", "COOKING_COMPLETED"):
            job = await self.transition(job, event)
        incomplete = await self.client.post(path + "/safety/core-check", headers=CSRF,
            json={"expected_version": job["version"], "probes": [probe(f"piece-{i}") for i in range(14)]})
        self.assertEqual(incomplete.status_code, 422)
        checked = await self.client.post(path + "/safety/core-check", headers=CSRF,
            json={"expected_version": job["version"], "probes": [probe(f"piece-{i}") for i in range(15)]})
        self.assertEqual(checked.status_code, 200, checked.text)
        assessment = {**self.assessment_body(checked.json()), "actual_mass_kg": "2.4",
                      "load_percent": "60", "load_band": "REFERENCE"}
        scored = await self.client.post(path + "/assessments", headers=CSRF, json=assessment)
        self.assertEqual(scored.status_code, 201, scored.text)
        self.assertEqual(scored.json()["tonkatsu_batch_rule_version"], "tonkatsu-batch-v2")
        self.assertEqual((await self.client.post(path + "/tonkatsu-batch", headers=CSRF,
            json={**body, "expected_version": checked.json()["version"]})).status_code, 409)
        latest = (await self.client.get(path)).json()["job"]
        recooked = await self.client.post(path + "/recook", headers=CSRF,
            json={"expected_version": latest["version"], "reason": "합성 재조리"})
        self.assertEqual(recooked.status_code, 201, recooked.text)
        self.assertEqual(recooked.json()["equipment_profile"], "synthetic-fryer-approved-v2")
        self.assertNotIn("tonkatsu_batch", recooked.json())

    async def test_batch_requires_owner_and_valid_weights(self):
        created = await self.client.post("/api/customer/cooking-jobs", headers=CSRF, json={
            "planned_portions": 2, "recipe_version": "recipe-v1", "thickness_cm": "1.20",
            "equipment_profile": "synthetic-fryer-approved-v2"})
        self.assertEqual(created.status_code, 201, created.text)
        job = created.json()
        path = f"/api/customer/cooking-jobs/{job['job_id']}/tonkatsu-batch"
        for weights in ([166] * 15, [160] * 14, [True] * 15):
            response = await self.client.post(path, headers=CSRF,
                json={"expected_version": job["version"], "mode": "REFERENCE", "product_weights_g": weights})
            self.assertEqual(response.status_code, 422)
        partial = await self.client.post(path, headers=CSRF,
            json={"expected_version": job["version"], "mode": "PARTIAL", "product_weights_g": [160] * 2})
        self.assertEqual(partial.status_code, 200, partial.text)
        self.assertEqual(partial.json()["tonkatsu_batch"]["load_percent"], "8.00")
        self.subject = "customer-2"
        self.assertEqual((await self.client.post(path, headers=CSRF,
            json={"expected_version": 2, "mode": "PARTIAL", "product_weights_g": [160] * 2})).status_code, 404)

    async def test_approved_profile_supports_fifteen_portions_and_legacy_remains_limited(self):
        body = {"planned_portions": 15, "recipe_version": "recipe-v1", "thickness_cm": "1.20"}
        approved = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
            json={**body, "equipment_profile": "synthetic-fryer-approved-v2"})
        self.assertEqual(approved.status_code, 201, approved.text)
        legacy = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
            json={**body, "equipment_profile": "synthetic-fryer"})
        self.assertEqual(legacy.status_code, 422)

    async def test_initial_context_is_versioned_bound_to_batch_and_scoped(self):
        created = await self.client.post("/api/customer/cooking-jobs", headers=CSRF, json={
            "planned_portions": 2, "recipe_version": "recipe-v1", "thickness_cm": "1.20",
            "equipment_profile": "synthetic-fryer-approved-v2"})
        job = created.json()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        batch = {"expected_version": job["version"], "mode": "REFERENCE", "product_weights_g": [160] * 15}
        job = (await self.client.post(path + "/tonkatsu-batch", headers=CSRF, json=batch)).json()
        observed = datetime.now(timezone.utc) - timedelta(seconds=330)
        body = {"expected_version": job["version"], "ingredient_temperature_c": "4.25",
                "temperature_observed_at": observed.isoformat(), "storage_state": "CHILLED",
                "oil_capacity_l": "15.00", "oil_batches_since_change": 0,
                "oil_cooked_mass_kg": "0", "oil_changed_at": (observed - timedelta(hours=1)).isoformat()}
        self.assertEqual((await self.client.post(path + "/tonkatsu-initial-context", json=body)).status_code, 403)
        recorded = await self.client.post(path + "/tonkatsu-initial-context", headers=CSRF, json=body)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        job = recorded.json()
        self.assertEqual(job["tonkatsu_initial_context"]["status"], "DECLARED_UNVERIFIED")
        self.assertEqual(job["tonkatsu_initial_context"]["oil_batches_since_change"], 0)
        evidence = (await self.client.get(path + "/recommendation-evidence")).json()
        self.assertEqual(evidence["recorded"]["tonkatsu_initial_context"]["ingredient_temperature_c"], "4.25")
        self.assertNotIn("INGREDIENT_INITIAL_STATE", evidence["unverified_categories"])
        self.assertNotIn("FRYER_AND_BATCH_BASELINE", evidence["unverified_categories"])
        self.assertIn("INITIAL_CONTEXT_DECLARED_UNVERIFIED", evidence["unverified_categories"])
        self.assertEqual(evidence["completeness"], "INSUFFICIENT")
        frozen = await self.client.post(path + "/tonkatsu-initial-context", headers=CSRF,
            json={**body, "expected_version": job["version"], "storage_state": "FROZEN"})
        self.assertEqual(frozen.status_code, 200, frozen.text)
        job = frozen.json()
        frozen_evidence = (await self.client.get(path + "/recommendation-evidence")).json()
        self.assertIn("STORAGE_OUTSIDE_SCENARIO", frozen_evidence["unverified_categories"])
        events = (await self.client.get(path + "/events")).json()
        initial_events = [event for event in events if event["domain"] == "TONKATSU_INITIAL_CONTEXT"]
        self.assertEqual([event["context"]["storage_state"] for event in initial_events], ["CHILLED", "FROZEN"])
        changed = await self.client.post(path + "/tonkatsu-batch", headers=CSRF,
            json={**batch, "expected_version": job["version"]})
        self.assertEqual(changed.status_code, 200, changed.text)
        evidence = (await self.client.get(path + "/recommendation-evidence")).json()
        self.assertIsNone(evidence["recorded"]["tonkatsu_initial_context"])
        self.assertIn("INITIAL_CONTEXT_STALE", evidence["unverified_categories"])
        self.subject = "customer-2"
        self.assertEqual((await self.client.post(path + "/tonkatsu-initial-context", headers=CSRF,
                                                 json={**body, "expected_version": changed.json()["version"]})).status_code, 404)

    async def test_initial_context_checks_time_and_never_changes_started_job(self):
        created = await self.client.post("/api/customer/cooking-jobs", headers=CSRF, json={
            "planned_portions": 2, "recipe_version": "recipe-v1", "thickness_cm": "1.20",
            "equipment_profile": "synthetic-fryer-approved-v2"})
        job = created.json()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        job = (await self.client.post(path + "/tonkatsu-batch", headers=CSRF, json={"expected_version": job["version"],
            "mode": "PARTIAL", "product_weights_g": [160, 160]})).json()
        endpoint = path + "/tonkatsu-initial-context"
        self.assertEqual((await self.client.post(endpoint, headers=CSRF,
            json={"expected_version": job["version"], "ingredient_temperature_c": "4"})).status_code, 422)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertEqual((await self.client.post(endpoint, headers=CSRF,
            json={"expected_version": job["version"], "oil_changed_at": future})).status_code, 422)
        observed = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        recorded = await self.client.post(endpoint, headers=CSRF,
            json={"expected_version": job["version"], "ingredient_temperature_c": "4", "temperature_observed_at": observed})
        self.assertEqual(recorded.status_code, 200, recorded.text)
        ready = await self.transition(recorded.json(), "PREHEAT_COMPLETED")
        started = await self.client.post(path + "/transitions", headers=CSRF, json={"event": "COOKING_STARTED",
            "expected_version": ready["version"], "actual_placed_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()})
        self.assertEqual(started.status_code, 422)
        started = await self.client.post(path + "/transitions", headers=CSRF, json={"event": "COOKING_STARTED",
            "expected_version": ready["version"], "actual_placed_at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual((await self.client.post(endpoint, headers=CSRF,
            json={"expected_version": started.json()["version"], "storage_state": "CHILLED"})).status_code, 409)

    async def test_core_check_rejects_values_outside_synthetic_input_range(self):
        job = await self.create()
        for event in ("PREHEAT_COMPLETED", "COOKING_STARTED", "COOKING_COMPLETED"):
            job = await self.transition(job, event)
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        for temperature in ("300.1", "-273.16"):
            result = await self.client.post(path + "/safety/core-check", headers=CSRF,
                json={"expected_version": job["version"],
                      "probes": [probe("piece-1", temperature), probe("piece-2")]})
            self.assertEqual(result.status_code, 422)
            current = (await self.client.get(path)).json()["job"]
            self.assertEqual(current["version"], job["version"])
            self.assertTrue(current["evaluation_blocked"])

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
                                               "reason": "합성 이취"})
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

    async def test_assessment_and_safety_refresh_dependent_aggregate(self):
        job = await self.completed_with_safety()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        first_assessment = await self.client.post(path + "/assessments", headers=CSRF,
                                                  json=self.assessment_body(job))
        self.assertEqual(first_assessment.status_code, 201, first_assessment.text)
        period = datetime.now(timezone.utc)
        created = await self.client.post("/api/customer/assessment-aggregates", headers=CSRF, json={
            "job_ids": [job["job_id"]],
            "period_start": (period - timedelta(days=1)).isoformat(),
            "period_end": (period + timedelta(days=1)).isoformat(),
            "recipe_version": "recipe-v1", "load_band": "PARTIAL",
        })
        self.assertEqual(created.status_code, 201, created.text)
        original = created.json()
        current_path = f"/api/customer/assessment-aggregates/{original['group_id']}/current"
        self.assertEqual(original["mean_quality_score_percent"], "80.00")

        job = (await self.client.get(path)).json()["job"]
        revised_body = self.assessment_body(job, current=1, kind="REVISION", reason="합성 재평가")
        revised_body["scores"] = {name: 3 for name in cooking.SCORE_ITEMS}
        revised = await self.client.post(path + "/assessments", headers=CSRF, json=revised_body)
        self.assertEqual(revised.status_code, 201, revised.text)
        updated = (await self.client.get(current_path)).json()
        self.assertNotEqual(updated["result_id"], original["result_id"])
        self.assertEqual(updated["mean_quality_score_percent"], "60.00")

        job = (await self.client.get(path)).json()["job"]
        suspected = await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "SUSPECTED", "expected_version": job["version"], "reason": "합성 안전 의심",
        })
        self.assertEqual(suspected.status_code, 200, suspected.text)
        blocked = (await self.client.get(current_path)).json()
        self.assertEqual(blocked["sample_count"], 0)
        self.assertEqual(blocked["excluded"][0]["reason"], "SAFETY_NOT_PASSED")
        self.assertEqual((await self.client.get(
            f"/api/customer/assessment-aggregates/{original['group_id']}/results/{original['result_id']}"
        )).json()["mean_quality_score_percent"], "80.00")
        retry = await self.client.post(path + "/refresh-aggregates", headers=CSRF)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["refreshed_group_count"], 1)
        self.assertEqual((await self.client.get(current_path)).json()["result_id"], blocked["result_id"])
        self.assertEqual((await self.client.post(path + "/refresh-aggregates")).status_code, 403)
        self.subject = "another-customer"
        self.assertEqual((await self.client.post(path + "/refresh-aggregates", headers=CSRF)).status_code, 404)

    async def test_failed_aggregate_refresh_can_be_retried_after_source_commit(self):
        job = await self.completed_with_safety()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                          json=self.assessment_body(job))).status_code, 201)
        period = datetime.now(timezone.utc)
        created = (await self.client.post("/api/customer/assessment-aggregates", headers=CSRF, json={
            "job_ids": [job["job_id"]],
            "period_start": (period - timedelta(days=1)).isoformat(),
            "period_end": (period + timedelta(days=1)).isoformat(),
            "recipe_version": "recipe-v1", "load_band": "PARTIAL",
        })).json()
        current_path = f"/api/customer/assessment-aggregates/{created['group_id']}/current"
        job = (await self.client.get(path)).json()["job"]
        revised_body = self.assessment_body(job, current=1, kind="REVISION", reason="합성 재평가")
        revised_body["scores"] = {name: 3 for name in cooking.SCORE_ITEMS}
        with patch("foodtech_business_api.assessment_aggregate.refresh_dependents",
                   new=AsyncMock(side_effect=RuntimeError("synthetic storage outage"))):
            failed = await self.client.post(path + "/assessments", headers=CSRF, json=revised_body)
        self.assertEqual(failed.status_code, 503)
        self.assertIn("작업 변경은 저장되었으나", failed.json()["detail"])
        self.assertEqual((await self.client.get(path)).json()["current_assessment"]["version"], 2)
        self.assertEqual((await self.client.get(current_path)).json()["result_id"], created["result_id"])
        self.assertEqual((await self.client.post(path + "/refresh-aggregates", headers=CSRF)).json()
                         ["refreshed_group_count"], 1)
        self.assertNotEqual((await self.client.get(current_path)).json()["result_id"], created["result_id"])

    async def test_assessment_change_refreshes_comparison_and_safety_blocks_new_comparison(self):
        baseline = await self.completed_with_safety()
        baseline_path = f"/api/customer/cooking-jobs/{baseline['job_id']}"
        baseline_body = self.assessment_body(baseline)
        baseline_body["scores"] = {name: 3 for name in cooking.SCORE_ITEMS}
        self.assertEqual((await self.client.post(baseline_path + "/assessments", headers=CSRF,
                          json=baseline_body)).status_code, 201)
        candidate = await self.completed_with_safety()
        candidate_path = f"/api/customer/cooking-jobs/{candidate['job_id']}"
        self.assertEqual((await self.client.post(candidate_path + "/assessments", headers=CSRF,
                          json=self.assessment_body(candidate))).status_code, 201)
        compare_path = candidate_path + "/comparisons"
        created = await self.client.post(compare_path, headers=CSRF,
                                         json={"baseline_job_id": baseline["job_id"]})
        self.assertEqual(created.status_code, 201, created.text)
        original = created.json()
        self.assertEqual(original["score_total_delta"], 7)
        current_path = compare_path + f"/from/{baseline['job_id']}/current"

        baseline = (await self.client.get(baseline_path)).json()["job"]
        revised_body = self.assessment_body(baseline, current=1, kind="REVISION", reason="합성 재평가")
        revised = await self.client.post(baseline_path + "/assessments", headers=CSRF,
                                         json=revised_body)
        self.assertEqual(revised.status_code, 201, revised.text)
        current = (await self.client.get(current_path)).json()
        self.assertNotEqual(current["comparison_id"], original["comparison_id"])
        self.assertEqual(current["score_total_delta"], 0)
        self.assertEqual((await self.client.get(compare_path + "/" + original["comparison_id"]))
                         .json()["score_total_delta"], 7)

        candidate = (await self.client.get(candidate_path)).json()["job"]
        suspected = await self.client.post(candidate_path + "/safety/events", headers=CSRF,
                                           json={"event": "SUSPECTED", "expected_version": candidate["version"],
                                                 "reason": "합성 안전 의심"})
        self.assertEqual(suspected.status_code, 200, suspected.text)
        self.assertEqual((await self.client.get(current_path)).json()["comparison_id"],
                         current["comparison_id"])
        self.assertEqual((await self.client.get(compare_path + "/" + current["comparison_id"] + "/status"))
                         .json()["status"], "STALE")
        retry = await self.client.post(candidate_path + "/refresh-comparisons", headers=CSRF)
        self.assertEqual(retry.json(), {"refreshed_group_count": 0, "blocked_group_count": 1})
        self.assertEqual((await self.client.post(candidate_path + "/refresh-comparisons")).status_code, 403)
        self.subject = "another-customer"
        self.assertEqual((await self.client.post(candidate_path + "/refresh-comparisons",
                                                 headers=CSRF)).status_code, 404)

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
        listed = (await self.client.get("/api/customer/cooking-jobs")).json()["items"]
        self.assertEqual({item["job_id"] for item in listed}, {job["job_id"], recook.json()["job_id"]})
        self.assertEqual((await self.client.get(path)).json()["job"]["safety_status"], "FAILED")

    async def test_cooking_safety_hold_requires_manual_stop_and_explicit_resume(self):
        job = await self.create()
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        suspected = await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "SUSPECTED", "expected_version": job["version"],
            "reason": "합성 이취 발견", "partial_scores": {"ingredient_condition": 4},
            "partial_observations": {"ingredient_condition": "투입 전 육안 확인"},
        })
        self.assertEqual(suspected.status_code, 200, suspected.text)
        held = suspected.json()
        self.assertEqual(held["status"], "SAFETY_HOLD")
        self.assertEqual(held["evaluation_status"], "INTERRUPTED")
        self.assertTrue(held["evaluation_blocked"])
        self.assertIsNone(held["manual_stop_confirmed_at"])
        self.assertIsNotNone(held["safety_hold_requested_at"])
        self.assertEqual((await self.client.get(path)).json()["current_assessment"], None)
        events = (await self.client.get(path + "/events")).json()
        self.assertEqual(events[-1]["partial_scores"], {"ingredient_condition": 4})
        self.assertEqual(events[-1]["partial_observations"], {"ingredient_condition": "투입 전 육안 확인"})
        self.assertFalse(events[-1]["manual_stop_confirmed"])
        self.assertEqual((await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_RESUMED", "expected_version": held["version"], "reason": "재개 시도",
        })).status_code, 409)
        self.assertEqual((await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_COMPLETED", "expected_version": held["version"],
            "actual_lifted_at": datetime.now(timezone.utc).isoformat(),
        })).status_code, 409)
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                                                 json=self.assessment_body(held))).status_code, 409)
        self.assertEqual((await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "CLEARED", "expected_version": held["version"], "reason": "이상 없음",
        })).status_code, 409)
        self.assertEqual((await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "UNDER_REVIEW", "expected_version": held["version"],
            "reason": "수동 중단 시각 누락", "manual_stop_confirmed": True,
        })).status_code, 422)
        reviewed = await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "UNDER_REVIEW", "expected_version": held["version"],
            "reason": "운영자가 현장에서 중단 후 확인", "manual_stop_confirmed": True,
            "manual_stopped_at": datetime.now(timezone.utc).isoformat(),
        })
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertIsNotNone(reviewed.json()["manual_stop_confirmed_at"])
        self.assertIsNotNone(reviewed.json()["manual_stopped_at"])
        cleared = await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "CLEARED", "expected_version": reviewed.json()["version"],
            "reason": "합성 조사 결과 안전 이상 배제",
        })
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(cleared.json()["status"], "SAFETY_HOLD")
        self.assertEqual((await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_RESUMED", "expected_version": cleared.json()["version"],
        })).status_code, 422)
        resumed = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_RESUMED", "expected_version": cleared.json()["version"],
            "reason": "현장 확인 후 재개",
        })
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["status"], "RUNNING")
        self.assertEqual(resumed.json()["evaluation_status"], "INTERRUPTED")

    async def test_safety_failure_terminates_held_job_without_quality_score(self):
        job = await self.create()
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        held = await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "SUSPECTED", "expected_version": job["version"], "reason": "합성 변색",
        })
        failed = await self.client.post(path + "/safety/events", headers=CSRF, json={
            "event": "FAILED", "expected_version": held.json()["version"], "reason": "안전 문제 배제 불가",
        })
        self.assertEqual(failed.status_code, 200, failed.text)
        self.assertEqual(failed.json()["status"], "FAILED")
        self.assertEqual(failed.json()["evaluation_status"], "INTERRUPTED")
        self.assertEqual((await self.client.get(path)).json()["current_assessment"], None)
        self.assertEqual((await self.client.get(path + "/assessments")).json(), [])
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                                                 json=self.assessment_body(failed.json()))).status_code, 409)

    async def test_owner_and_csrf_boundary(self):
        response = await self.client.post("/api/customer/cooking-jobs",
                                          json={"planned_portions": 2, "recipe_version": "v1",
                                                "thickness_cm": "1.20"})
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

    async def test_thickness_profile_and_actual_time_are_recorded(self):
        job = await self.create()
        self.assertEqual(job["thickness_profile"], "STANDARD")
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        self.assertIsNotNone(job["actual_placed_at"])
        job = await self.transition(job, "COOKING_COMPLETED")
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["actual_cooking_seconds"], 300)
        self.assertEqual(job["process_time_status"], "NORMAL")
        self.assertGreaterEqual(job["completion_recording_delay_seconds"], 0)

    async def test_early_lift_fails_job_and_blocks_assessment(self):
        job = await self.create()
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        response = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_COMPLETED", "expected_version": job["version"],
            "actual_lifted_at": (self.placed_at + timedelta(seconds=269)).isoformat(),
        })
        self.assertEqual(response.status_code, 200, response.text)
        failed = response.json()
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["process_time_status"], "EARLY_REMOVAL")
        self.assertEqual(failed["process_assessment_status"], "FAILED")
        self.assertTrue(failed["evaluation_blocked"])
        self.assertEqual((await self.client.post(path + "/safety/core-check", headers=CSRF,
                                                 json={"expected_version": failed["version"],
                                                       "probes": [probe("piece-1"), probe("piece-2")]})).status_code, 409)

    async def test_maximum_time_exceeded_fails_job_and_preserves_evidence(self):
        job = await self.create()
        job = await self.transition(job, "PREHEAT_COMPLETED")
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        placed_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        started = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_STARTED", "expected_version": job["version"],
            "actual_placed_at": placed_at.isoformat(),
        })
        self.assertEqual(started.status_code, 200, started.text)
        job = started.json()
        response = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_COMPLETED", "expected_version": job["version"],
            "actual_lifted_at": (placed_at + timedelta(seconds=361)).isoformat(),
        })
        self.assertEqual(response.status_code, 200, response.text)
        failed = response.json()
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["actual_cooking_seconds"], 361)
        self.assertEqual(failed["process_time_status"], "MAX_TIME_EXCEEDED")
        self.assertTrue(failed["evaluation_blocked"])
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                                                 json=self.assessment_body(failed))).status_code, 409)
        events = (await self.client.get(path + "/events")).json()
        self.assertEqual(events[-1]["process_time_status"], "MAX_TIME_EXCEEDED")

    async def test_concurrent_process_update_rejects_stale_completion(self):
        job = await self.create()
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        pk = cooking._pk(self.subject, job["job_id"])
        original_commit = self.store.commit

        def worker_wins_commit(item_pk, expected_version, meta, assessment=None, pointer=None, event=None):
            if event and event.get("trigger") == "COOKING_COMPLETED":
                worker_meta = {**self.store.items[(pk, "META")],
                               "version": expected_version + 1,
                               "process_temperature_status": "HIGH_WARNING",
                               "process_assessment_status": "WARNING"}
                original_commit(pk, expected_version, worker_meta,
                                event={"sk": "EVENT#PROCESS#concurrent", "domain": "PROCESS_ASSESSMENT",
                                       "trigger": "PROCESS_SAMPLE_ASSESSED", "occurred_at": cooking._now()})
            return original_commit(item_pk, expected_version, meta, assessment, pointer, event)

        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        with patch.object(self.store, "commit", side_effect=worker_wins_commit):
            response = await self.client.post(path + "/transitions", headers=CSRF, json={
                "event": "COOKING_COMPLETED", "expected_version": job["version"],
                "actual_lifted_at": (self.placed_at + timedelta(seconds=300)).isoformat(),
            })
        self.assertEqual(response.status_code, 409, response.text)
        current = (await self.client.get(path)).json()["job"]
        self.assertEqual(current["status"], "RUNNING")
        self.assertEqual(current["process_temperature_status"], "HIGH_WARNING")
        retry = await self.client.post(path + "/transitions", headers=CSRF, json={
            "event": "COOKING_COMPLETED", "expected_version": current["version"],
            "actual_lifted_at": (self.placed_at + timedelta(seconds=300)).isoformat(),
        })
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["process_assessment_status"], "WARNING")
        self.assertEqual(len((await self.client.get(path + "/events")).json()), 4)

    async def test_missing_or_invalid_lift_time_is_rejected(self):
        job = await self.create()
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        path = f"/api/customer/cooking-jobs/{job['job_id']}/transitions"
        for timing in ({}, {"actual_lifted_at": (self.placed_at - timedelta(seconds=1)).isoformat()}):
            response = await self.client.post(path, headers=CSRF, json={
                "event": "COOKING_COMPLETED", "expected_version": job["version"], **timing,
            })
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual((await self.client.get(path.removesuffix("/transitions"))).json()["job"]["status"], "RUNNING")

    async def test_out_of_profile_thickness_is_rejected(self):
        response = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                                          json={"planned_portions": 2, "recipe_version": "v1",
                                                "thickness_cm": "2.01"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("PROFILE_REJECTED", response.json()["detail"])

    async def test_below_range_thickness_remains_warning_after_normal_time(self):
        response = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                                          json={"planned_portions": 2, "recipe_version": "v1",
                                                "thickness_cm": "0.49"})
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        self.assertEqual(job["process_thickness_status"], "BELOW_RANGE")
        job = await self.transition(job, "PREHEAT_COMPLETED")
        job = await self.transition(job, "COOKING_STARTED")
        job = await self.transition(job, "COOKING_COMPLETED")
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["process_time_status"], "NOT_APPLICABLE")
        self.assertEqual(job["process_assessment_status"], "WARNING")


class TonkatsuProcessRuleTests(unittest.TestCase):
    def test_thickness_boundaries(self):
        cases = [("0.49", "BELOW_RANGE", None), ("0.50", "IN_RANGE", "THIN"),
                 ("0.99", "IN_RANGE", "THIN"), ("1.00", "IN_RANGE", "STANDARD"),
                 ("1.49", "IN_RANGE", "STANDARD"), ("1.50", "IN_RANGE", "THICK"),
                 ("2.00", "IN_RANGE", "THICK"), ("2.01", "ABOVE_RANGE", None)]
        for value, status, profile in cases:
            with self.subTest(value=value):
                self.assertEqual(thickness_profile(Decimal(value)), (status, profile))

    def test_all_time_boundaries(self):
        for profile, values in {
            "THIN": (150, 165, 195, 210, 240),
            "STANDARD": (270, 285, 315, 330, 360),
            "THICK": (330, 345, 375, 390, 420),
        }.items():
            early, short, normal, delay, maximum = values
            expected = [(early - 1, "EARLY_REMOVAL"), (early, "SHORT_WARNING"),
                        (short - 1, "SHORT_WARNING"), (short, "NORMAL"),
                        (normal, "NORMAL"), (normal + 1, "DELAY_WARNING"),
                        (delay, "DELAY_WARNING"), (delay + 1, "EXTENDED_COOKING"),
                        (maximum, "EXTENDED_COOKING"), (maximum + 1, "MAX_TIME_EXCEEDED")]
            for second, status in expected:
                with self.subTest(profile=profile, second=second):
                    self.assertEqual(cooking_time_status(profile, second), status)

    def test_combined_severity_preserves_warning_and_failure(self):
        self.assertEqual(process_status("NORMAL", "BELOW_RANGE", "NORMAL"), "WARNING")
        self.assertEqual(process_status("NORMAL", "IN_RANGE", "MAX_TIME_EXCEEDED"), "FAILED")
        self.assertEqual(process_status("NOT_MEASURED", "IN_RANGE", "NORMAL"), "INSUFFICIENT_DATA")
        self.assertEqual(process_status("NORMAL", "IN_RANGE", "NOT_MEASURED"), "INSUFFICIENT_DATA")


class DynamoTransactionContractTests(unittest.TestCase):
    def test_job_and_listing_are_created_in_one_transaction(self):
        calls = []
        store = DynamoCookingStore.__new__(DynamoCookingStore)
        store.table = SimpleNamespace(name="test-table")
        store.serializer = TypeSerializer()
        store.client = SimpleNamespace(transact_write_items=lambda **kwargs: calls.append(kwargs))
        meta = {"owner_subject": "one", "job_id": "job", "created_at": "2026-11-15T00:00:00Z",
                "recipe_version": "recipe-v1", "planned_portions": 2, "source_job_id": None}
        job_pk = "SCOPE#USER#one#JOB#job"
        store.create(job_pk, meta)
        writes = calls[0]["TransactItems"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[1]["Put"]["Item"]["pk"]["S"], "SCOPE#USER#one#JOBS")
        self.assertEqual(writes[1]["Put"]["Item"]["sk"]["S"],
                         "CREATED#2026-11-15T00:00:00Z#job")
        store.create_recook(job_pk, 3, "SCOPE#USER#one#JOB#recook",
                            {**meta, "job_id": "recook", "source_job_id": "job"})
        self.assertEqual(len(calls[1]["TransactItems"]), 3)
        self.assertEqual(calls[1]["TransactItems"][2]["Put"]["Item"]["source_job_id"]["S"], "job")

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
