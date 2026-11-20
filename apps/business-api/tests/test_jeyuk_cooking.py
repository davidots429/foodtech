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

    async def bind(self, job, batch_id):
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        response = await self.client.post(path + "/source-binding", headers=CSRF,
                                          json={"expected_version": job["version"],
                                                "source_id": "synthetic-rest",
                                                "device_id": "mockprofile-110-a-001",
                                                "batch_id": batch_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def validation_body(version):
        return {"schema_version": "jeyuk-validation-context-v1", "expected_version": version,
                "ingredient_spec_id": "pork-spec-v1", "ingredient_lot_id": "lot-a-001",
                "supplier_id": "supplier-a", "sensor_id": "probe-a",
                "sensor_calibration_record_id": "cal-001",
                "process_condition_profile_id": "jeyuk-process-small",
                "process_condition_version": "v1"}

    async def test_validation_context_is_declared_not_approved_and_audited(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        self.assertEqual(job["validation_context_status"], "NOT_RECORDED")
        body = self.validation_body(job["version"])
        self.assertEqual((await self.client.post(path + "/validation-context", headers=CSRF,
                                                 json=body)).status_code, 409)
        job = await self.bind(job, "jeyuk-context-a")
        body["expected_version"] = job["version"]
        recorded = await self.client.post(path + "/validation-context", headers=CSRF, json=body)
        self.assertEqual(recorded.status_code, 200, recorded.text)
        job = recorded.json()
        self.assertEqual((job["validation_context_status"], job["validation_context_version"]),
                         ("DECLARED_UNVERIFIED", 1))
        self.assertEqual(job["validation_context"]["equipment_device_id"], "mockprofile-110-a-001")
        self.assertNotIn("batch_id", job["validation_context"])
        self.assertNotIn("validated_at", job)
        self.assertEqual((job["safety_status"], job["evaluation_blocked"]), ("NOT_MEASURED", True))
        self.assertEqual((await self.client.post(path + "/validation-context", headers=CSRF,
                                                 json=body)).status_code, 409)
        body["expected_version"] = job["version"]
        body["ingredient_lot_id"] = "lot-a-002"
        changed = await self.client.post(path + "/validation-context", headers=CSRF, json=body)
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertNotEqual(changed.json()["validation_context_digest"], job["validation_context_digest"])
        events = (await self.client.get(path + "/events")).json()
        context_events = [item for item in events if item["domain"] == "VALIDATION_CONTEXT"]
        self.assertEqual(len(context_events), 2)
        self.assertEqual(context_events[1]["previous_digest"], job["validation_context_digest"])
        bad = {**body, "expected_version": changed.json()["version"], "sensor_id": "probe a"}
        self.assertEqual((await self.client.post(path + "/validation-context", headers=CSRF,
                                                 json=bad)).status_code, 422)
        ready = await self.client.post(path + "/transitions", headers=CSRF,
                                       json={"expected_version": changed.json()["version"],
                                             "event": "PREHEAT_COMPLETED"})
        started = await self.client.post(path + "/transitions", headers=CSRF,
                                         json={"expected_version": ready.json()["version"],
                                               "event": "COOKING_STARTED",
                                               "actual_placed_at": datetime.now(timezone.utc).isoformat()})
        self.assertEqual(started.status_code, 200, started.text)
        body["expected_version"] = started.json()["version"]
        self.assertEqual((await self.client.post(path + "/validation-context", headers=CSRF,
                                                 json=body)).status_code, 409)

    async def test_combination_digest_excludes_batch_and_rejects_other_menu(self):
        first = await self.bind(await self.create(), "jeyuk-context-b")
        second = await self.bind(await self.create("1.80"), "jeyuk-context-c")
        digests = []
        for job in (first, second):
            result = await self.client.post(
                f"/api/customer/cooking-jobs/{job['job_id']}/validation-context",
                headers=CSRF, json=self.validation_body(job["version"]))
            self.assertEqual(result.status_code, 200, result.text)
            digests.append(result.json()["validation_context_digest"])
        self.assertEqual(digests[0], digests[1])
        tonkatsu = await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                                          json={"planned_portions": 1, "recipe_version": "v1",
                                                "thickness_cm": "1.2"})
        self.assertEqual(tonkatsu.status_code, 201, tonkatsu.text)
        self.assertEqual((await self.client.post(
            f"/api/customer/cooking-jobs/{tonkatsu.json()['job_id']}/validation-context",
            headers=CSRF, json=self.validation_body(tonkatsu.json()["version"]))).status_code, 422)

    async def test_evidence_report_links_current_process_without_activating_validation(self):
        job = await self.bind(await self.create(), "jeyuk-evidence-a")
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        initial = (await self.client.get(path + "/validation-evidence")).json()
        self.assertEqual(initial["status"], "INCOMPLETE")
        self.assertFalse(initial["activation_allowed"])
        recorded = await self.client.post(path + "/validation-context", headers=CSRF,
                                          json=self.validation_body(job["version"]))
        self.assertEqual(recorded.status_code, 200, recorded.text)
        pk = cooking._pk("jeyuk-customer", job["job_id"])
        meta = self.store.items[(pk, "META")]
        meta.update(status="COMPLETED", auto_safety_status="PASSED", mixture_hold_seconds=60,
                    safety_passed_at_second=61, discharge_check_status="PASSED",
                    actual_cooking_seconds=61, process_time_status="NORMAL",
                    discharge_check_version=1, discharge_check=[{"phase": phase} for phase in
                                                                  ("EARLY", "MIDDLE", "LATE")],
                    safety_status="PASSED", evaluation_blocked=False,
                    process_assessment={"second": 61, "status": "NORMAL",
                                        "source_assessment_pk": "JEYUK#jeyuk-evidence-a",
                                        "source_assessment_sk": "ASSESSMENT#000061",
                                        "rule_version": cooking.JEYUK_RULE_VERSION})
        unconfigured = (await self.client.get(path + "/validation-evidence")).json()
        self.assertEqual(unconfigured["status"], "INCOMPLETE")
        self.assertFalse(unconfigured["source_table_configured"])
        source_checks = {name: True for name in (
            "source_records_contiguous", "source_records_match_binding",
            "source_derived_results_match_raw",
            "source_process_conditions_normal", "source_safety_samples_valid",
            "source_validation_identity_matches",
            "source_process_span_covers_cooking")}
        with patch.dict(os.environ, {"FOODTECH_DATA_TABLE": "test-source"}), \
             patch.object(cooking, "_read_jeyuk_source_records",
                          return_value=([], [])) as source_read, \
             patch("foodtech_business_api.jeyuk_source_evidence.audit_source_records",
                   return_value=source_checks):
            linked = (await self.client.get(path + "/validation-evidence")).json()
            source_read.assert_called_once_with("test-source", "jeyuk-evidence-a", 61)
        self.assertEqual(linked["status"], "LINKED_UNVERIFIED")
        self.assertEqual(linked["process_reference"],
                         {"pk": "JEYUK#jeyuk-evidence-a", "sk": "ASSESSMENT#000061", "second": 61})
        self.assertEqual(linked["context_digest"], recorded.json()["validation_context_digest"])
        self.assertFalse(linked["activation_allowed"])
        meta["process_assessment"]["source_assessment_pk"] = "JEYUK#other-batch"
        mismatch = (await self.client.get(path + "/validation-evidence")).json()
        self.assertEqual(mismatch["status"], "INCOMPLETE")
        self.assertIn("process_reference_linked", mismatch["missing_checks"])
        meta["process_assessment"]["source_assessment_pk"] = "JEYUK#jeyuk-evidence-a"
        meta["safety_status"] = "SUSPECTED"
        revoked = (await self.client.get(path + "/validation-evidence")).json()
        self.assertEqual(revoked["status"], "INCOMPLETE")
        self.assertIn("current_safety_passed", revoked["missing_checks"])

    async def test_validation_evidence_rejects_a_job_changed_during_read(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}/validation-evidence"
        read = self.store.read
        calls = 0
        def changing(pk):
            nonlocal calls
            calls += 1
            meta, pointer = read(pk)
            if calls == 2:
                meta["version"] += 1
            return meta, pointer
        with patch.object(self.store, "read", side_effect=changing):
            result = await self.client.get(path)
        self.assertEqual(result.status_code, 409)

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

    async def test_medium_and_large_jobs_use_their_own_load_and_assessment_baselines(self):
        for size, mass, rated, portions in (("medium", "12", "20.01", 80), ("large", "60", "100.01", 300)):
            with self.subTest(size=size):
                body = {"planned_portions": portions, "recipe_version": "jeyuk-v1",
                        "equipment_profile": f"jeyuk-{size}", "actual_load_kg": mass}
                response = await self.client.post("/api/customer/cooking-jobs", headers=CSRF, json=body)
                self.assertEqual(response.status_code, 201, response.text)
                job = response.json()
                self.assertEqual((job["jeyuk_size_profile"], job["process_load_status"]), (size.upper(), "NORMAL"))
                self.assertEqual((await self.client.post("/api/customer/cooking-jobs", headers=CSRF,
                    json={**body, "actual_load_kg": rated})).status_code, 422)
                job = await self.bind(job, f"jeyuk-{size}-safety-v1")
                pk = cooking._pk("jeyuk-customer", job["job_id"])
                self.store.items[(pk, "META")].update(status="COMPLETED", safety_status="PASSED",
                                                       evaluation_blocked=False)
                assessment = {"expected_version": job["version"], "expected_current_version": 0,
                    "kind": "INITIAL", "scores": {item: 4 for item in cooking.SCORE_ITEMS},
                    "observations": {item: "합성 관찰" for item in cooking.SCORE_ITEMS},
                    "scale_version": "jeyuk-common-v1", "planned_portions": portions,
                    "actual_portions": portions, "actual_mass_kg": mass, "load_percent": "100",
                    "load_band": "REFERENCE", "quality_decision": "QUALITY_PASSED"}
                path = f"/api/customer/cooking-jobs/{job['job_id']}/assessments"
                self.assertEqual((await self.client.post(path, headers=CSRF,
                    json={**assessment, "load_percent": "50"})).status_code, 422)
                result = await self.client.post(path, headers=CSRF, json=assessment)
                self.assertEqual(result.status_code, 201, result.text)
                self.assertEqual((result.json()["load_band"], result.json()["equipment_profile"]),
                                 ("REFERENCE", f"jeyuk-{size}"))

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

    async def test_discharge_probe_requires_auto_hold_and_preserves_revalidation(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        pk = cooking._pk("jeyuk-customer", job["job_id"])
        self.store.items[(pk, "META")].update(status="COMPLETED",
                                                actual_lifted_at="2026-01-01T00:00:00Z")

        def body(version, kind="INITIAL", temperatures=(75, 75, 75), expired=None):
            return {"expected_version": version, "kind": kind, "samples": [
                {"phase": phase, "probe_id": f"probe-{index}",
                 "meat_sample_id": f"meat-{index}", "thickest_or_clumped_part": True,
                 "sampled_at": f"2026-01-01T00:00:0{index}Z",
                 "calibration_status": "EXPIRED" if index == expired else "OK",
                 "celsius": value}
                for index, (phase, value) in enumerate(zip(("EARLY", "MIDDLE", "LATE"), temperatures), 1)]}

        future = body(job["version"])
        future["samples"][-1]["sampled_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        rejected = await self.client.post(path + "/safety/discharge-check", headers=CSRF, json=future)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(self.store.items[(pk, "META")]["version"], job["version"])

        first = await self.client.post(path + "/safety/discharge-check", headers=CSRF,
                                       json=body(job["version"]))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["discharge_check_status"], "PASSED")
        self.assertEqual(first.json()["safety_status"], "INCOMPLETE")
        self.assertTrue(first.json()["evaluation_blocked"])
        self.store.items[(pk, "META")]["auto_safety_status"] = "PASSED"
        unmatched = await self.client.post(path + "/safety/discharge-check", headers=CSRF,
                                           json=body(first.json()["version"], "REVALIDATION"))
        self.assertEqual(unmatched.json()["safety_status"], "INCOMPLETE")
        with patch.object(cooking, "_jeyuk_feed_alignment", return_value=True):
            second = await self.client.post(path + "/safety/discharge-check", headers=CSRF,
                                            json=body(unmatched.json()["version"], "REVALIDATION"))
        self.assertEqual(second.json()["safety_status"], "PASSED")
        self.assertFalse(second.json()["evaluation_blocked"])
        failed = await self.client.post(path + "/safety/discharge-check", headers=CSRF,
                                        json=body(second.json()["version"], "REVALIDATION", (75, 74.9, 75)))
        self.assertEqual(failed.json()["safety_status"], "FAILED")
        self.assertTrue(failed.json()["evaluation_blocked"])
        events = (await self.client.get(path + "/events")).json()
        self.assertEqual(len([event for event in events if event["trigger"] == "JEYUK_DISCHARGE_CHECK"]), 4)

    async def test_discharge_missing_invalid_and_ordered_samples(self):
        job = await self.create()
        path = f"/api/customer/cooking-jobs/{job['job_id']}/safety/discharge-check"
        pk = cooking._pk("jeyuk-customer", job["job_id"])
        self.store.items[(pk, "META")].update(status="COMPLETED", auto_safety_status="PASSED",
                                                actual_lifted_at="2026-01-01T00:00:00Z")
        samples = [{"phase": phase, "probe_id": f"probe-{index}",
                    "meat_sample_id": f"meat-{index}", "thickest_or_clumped_part": True,
                    "sampled_at": f"2026-01-01T00:00:0{index}Z",
                    "calibration_status": "OK", "celsius": 75}
                   for index, phase in enumerate(("EARLY", "MIDDLE", "LATE"), 1)]
        bad = await self.client.post(path, headers=CSRF, json={"expected_version": 1,
                                      "kind": "INITIAL", "samples": list(reversed(samples))})
        self.assertEqual(bad.status_code, 422)
        samples[1]["calibration_status"] = "EXPIRED"
        incomplete = await self.client.post(path, headers=CSRF, json={"expected_version": 1,
                                             "kind": "INITIAL", "samples": samples})
        self.assertEqual(incomplete.json()["discharge_check_status"], "ASSESSMENT_INCOMPLETE")
        self.assertTrue(incomplete.json()["evaluation_blocked"])

    async def test_completed_quality_assessment_keeps_versions_and_safety_gate(self):
        job = await self.create("1.8")
        path = f"/api/customer/cooking-jobs/{job['job_id']}"
        pk = cooking._pk("jeyuk-customer", job["job_id"])
        self.store.items[(pk, "META")].update(status="COMPLETED", actual_lifted_at="2026-01-01T00:00:00Z",
                                                auto_safety_status="PASSED")
        scores = {item: 4 for item in cooking.SCORE_ITEMS}
        observations = {item: f"제육볶음 합성 {item} 관찰" for item in cooking.SCORE_ITEMS}

        def assessment(version, current=0, kind="INITIAL", reason=None, decision="QUALITY_PASSED",
                       reasons=None, mass="1.8", percent="100", band="REFERENCE"):
            return {"expected_version": version, "expected_current_version": current,
                    "kind": kind, "reason": reason, "scores": scores, "observations": observations,
                    "scale_version": "jeyuk-common-v1", "planned_portions": 2, "actual_portions": 2,
                    "actual_mass_kg": mass, "load_percent": percent, "load_band": band,
                    "quality_decision": decision, "quality_reason_codes": reasons or []}

        blocked = await self.client.post(path + "/assessments", headers=CSRF,
                                         json=assessment(job["version"]))
        self.assertEqual(blocked.status_code, 409)
        samples = [{"phase": phase, "probe_id": f"probe-{index}",
                    "meat_sample_id": f"meat-{index}", "thickest_or_clumped_part": True,
                    "sampled_at": f"2026-01-01T00:00:0{index}Z",
                    "calibration_status": "OK", "celsius": 75}
                   for index, phase in enumerate(("EARLY", "MIDDLE", "LATE"), 1)]
        with patch.object(cooking, "_jeyuk_feed_alignment", return_value=True):
            safe = await self.client.post(path + "/safety/discharge-check", headers=CSRF,
                                          json={"expected_version": job["version"], "kind": "INITIAL",
                                                "samples": samples})
        self.assertEqual(safe.json()["safety_status"], "PASSED")
        wrong_scale = assessment(safe.json()["version"])
        wrong_scale["scale_version"] = "tonkatsu-common-v1"
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                         json=wrong_scale)).status_code, 422)
        wrong_pass = assessment(safe.json()["version"])
        wrong_pass["scores"] = {**scores, "texture": 2}
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                         json=wrong_pass)).status_code, 422)
        initial = await self.client.post(path + "/assessments", headers=CSRF,
                                         json=assessment(safe.json()["version"]))
        self.assertEqual(initial.status_code, 201, initial.text)
        self.assertEqual((initial.json()["quality_passed"], initial.json()["load_band"]), (True, "REFERENCE"))
        current = (await self.client.get(path)).json()["current_assessment"]
        self.assertEqual(current["version"], 1)
        fresh = (await self.client.get(path)).json()["job"]
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                         json=assessment(fresh["version"], current=0))).status_code, 409)
        revision = await self.client.post(path + "/assessments", headers=CSRF,
                                          json=assessment(fresh["version"], current=1,
                                                          kind="REASSESSMENT", reason="소스 코팅 재검토",
                                                          decision="QUALITY_FAILED", reasons=["SAUCE_NOT_COATED"]))
        self.assertEqual(revision.status_code, 201, revision.text)
        self.assertFalse(revision.json()["quality_passed"])
        self.assertEqual(len((await self.client.get(path + "/assessments")).json()), 2)
        self.assertEqual((await self.client.get(path)).json()["current_assessment"]["version"], 2)
        fresh = (await self.client.get(path)).json()["job"]
        self.assertEqual((await self.client.post(path + "/assessments", headers=CSRF,
                         json=assessment(fresh["version"], current=2, kind="REVISION", reason="잘못된 코드",
                                         decision="QUALITY_FAILED", reasons=["EQUIPMENT_ERROR"]))).status_code, 422)

    async def test_over_recommended_load_uses_jeyuk_band(self):
        job = await self.create("2.5")
        pk = cooking._pk("jeyuk-customer", job["job_id"])
        self.store.items[(pk, "META")].update(status="COMPLETED", safety_status="PASSED",
                                                evaluation_blocked=False, safety_version=1)
        path = f"/api/customer/cooking-jobs/{job['job_id']}/assessments"
        body = {"expected_version": job["version"], "expected_current_version": 0,
                "kind": "INITIAL", "scores": {item: 4 for item in cooking.SCORE_ITEMS},
                "observations": {item: "합성 관찰" for item in cooking.SCORE_ITEMS},
                "scale_version": "jeyuk-common-v1", "planned_portions": 2, "actual_portions": 2,
                "actual_mass_kg": "2.5", "load_percent": "138.89",
                "load_band": "OVER_RECOMMENDED", "quality_decision": "QUALITY_PASSED"}
        wrong = {**body, "load_band": "REFERENCE"}
        self.assertEqual((await self.client.post(path, headers=CSRF, json=wrong)).status_code, 422)
        accepted = await self.client.post(path, headers=CSRF, json=body)
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(accepted.json()["mass_per_portion_kg"], "1.250")


if __name__ == "__main__":
    unittest.main()
