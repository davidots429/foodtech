import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app
from foodtech_contracts import stream_binding_key


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
        self.assertEqual(value["completeness"], "INSUFFICIENT")
        self.assertIn("CORE_SERIES_MISSING_INVALID_OR_INCONSISTENT", value["completeness_reasons"])
        self.assertFalse(value["recommendation_execution_available"])
        self.assertIn("INGREDIENT_INITIAL_STATE", value["unverified_categories"])
        self.assertNotIn("not-for-model", result.text)
        self.assertNotIn("owner", result.text)
        self.subject = "another-customer"
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_jeyuk_temperature_evidence_excludes_unapproved_fields(self):
        meta = self.store.items[(self.pk, "META")]
        meta.update(menu="JEYUK", jeyuk_feed_safety_aligned=True, jeyuk_feed_mass_status="MATCHED",
                    jeyuk_feeds={"MEAT": {"mass_kg": "1", "initial_temperature_c": "4",
                        "temperature_observed_at": "2026-01-01T00:00:00Z", "reason": "private reason"}})
        response = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        self.assertEqual(response.status_code, 200, response.text)
        feed = response.json()["recorded"]["jeyuk_feeds"]["MEAT"]
        self.assertEqual(feed["initial_temperature_c"], "4")
        self.assertNotIn("private reason", response.text)

    async def test_jeyuk_feed_timing_is_precise_and_detects_inconsistent_records(self):
        from copy import deepcopy
        meta = self.store.items[(self.pk, "META")]
        def feed(name, start, end, group=None):
            return {"ingredient": name, "started_at": start, "completed_at": end,
                    "addition_group_id": group, "provenance": "SYNTHETIC", "reason": "private"}
        meta.update(menu="JEYUK", actual_placed_at="2026-01-01T00:00:05Z",
                    actual_lifted_at="2026-01-01T00:02:00Z", jeyuk_addition_group_id="private-group",
                    jeyuk_additions_simultaneous=True, jeyuk_feeds={
                        "MEAT": feed("MEAT", "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z"),
                        "VEGETABLE": feed("VEGETABLE", "2026-01-01T00:00:30.25Z", "2026-01-01T00:00:31Z", "private-group"),
                        "SAUCE": feed("SAUCE", "2026-01-01T00:01:00.25Z", "2026-01-01T00:01:01Z", "private-group")})
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        response = await self.client.get(path)
        timing = response.json()["recorded"]["jeyuk_feed_timing"]
        self.assertEqual(timing, {"status": "RECORDED", "basis": "MEAT_FEED_COMPLETED",
            "vegetable_start_offset_us": 25250000, "sauce_start_offset_us": 55250000,
            "start_separation_us": 30000000, "simultaneous": True})
        self.assertNotIn("private-group", response.text)
        self.assertNotIn("private", response.text)
        meta["jeyuk_feeds"]["SAUCE"]["started_at"] = "2026-01-01T00:01:00.250001Z"
        meta["jeyuk_additions_simultaneous"] = False
        over = (await self.client.get(path)).json()["recorded"]["jeyuk_feed_timing"]
        self.assertEqual((over["start_separation_us"], over["simultaneous"]), (30_000_001, False))
        meta["jeyuk_feeds"]["SAUCE"]["started_at"] = "2026-01-01T00:01:00.25Z"
        meta["jeyuk_additions_simultaneous"] = True
        original = deepcopy(meta)
        for mutate in (lambda m: m["jeyuk_feeds"]["SAUCE"].update(addition_group_id="other"),
                       lambda m: m["jeyuk_feeds"]["SAUCE"].update(started_at="2026-01-01T00:01:00.250001Z"),
                       lambda m: m.update(actual_placed_at="2026-01-01T00:00:06Z"),
                       lambda m: m["jeyuk_feeds"]["MEAT"].update(provenance="VERIFIED"),
                       lambda m: m["jeyuk_feeds"]["VEGETABLE"].update(ingredient="SAUCE")):
            meta.clear(); meta.update(deepcopy(original)); mutate(meta)
            result = await self.client.get(path)
            self.assertEqual(result.json()["recorded"]["jeyuk_feed_timing"]["status"], "INCONSISTENT")
        meta.clear(); meta.update(deepcopy(original)); del meta["jeyuk_feeds"]["SAUCE"]
        self.assertEqual((await self.client.get(path)).json()["recorded"]["jeyuk_feed_timing"]["status"], "NOT_RECORDED")

    async def test_reheat_history_links_rechecks_without_private_content(self):
        meta = self.store.items[(self.pk, "META")]
        for number in (1, 2):
            segment = {"segment_id": f"segment-{number}", "started_at": f"2026-01-01T00:0{number}:00Z",
                "lifted_at": f"2026-01-01T00:0{number}:30Z", "duration_seconds": "30",
                "previous_safety_version": number, "reason": "private reason", "item_ids": ["private-id"]}
            self.store.items[(self.pk, f"EVENT#SAFETY#{number * 2:012d}")] = {
                "sk": f"EVENT#SAFETY#{number * 2:012d}", "trigger": "TONKATSU_REHEATED",
                "segment": segment, "occurred_at": f"2026-01-01T00:0{number}:30Z"}
            self.store.items[(self.pk, f"EVENT#SAFETY#{number * 2 + 1:012d}")] = {
                "sk": f"EVENT#SAFETY#{number * 2 + 1:012d}", "trigger": "CORE_CHECK",
                "reheat_segment_id": segment["segment_id"], "to_status": "FAILED" if number == 1 else "PASSED",
                "occurred_at": f"2026-01-01T00:0{number}:40Z", "probes": ["private-probe"]}
        meta["latest_tonkatsu_reheat"] = segment
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["reheat_history_status"], "RECORDED")
        self.assertEqual([item["rechecks"][0]["status"] for item in data["reheat_history"]], ["FAILED", "PASSED"])
        self.assertIn("REHEAT_HISTORY_NOT_ASSESSED", data["unverified_categories"])
        self.assertNotIn("private reason", response.text)
        self.assertNotIn("private-id", response.text)
        self.assertNotIn("private-probe", response.text)
        del self.store.items[(self.pk, "EVENT#SAFETY#000000000004")]
        self.assertEqual((await self.client.get(path)).json()["reheat_history_status"], "INCONSISTENT")

    async def test_stale_quality_and_derived_result_are_not_presented_as_current(self):
        self.store.items[(self.pk, "META")]["safety_version"] = 3
        self.store.items[(self.pk, "META")]["version"] = 8
        result = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        value = result.json()
        self.assertIsNone(value["recorded"]["quality"])
        self.assertTrue(value["process_stale"])
        self.assertIn("CURRENT_QUALITY_ASSESSMENT", value["unverified_categories"])
        self.assertIn("CURRENT_PROCESS_DERIVATION", value["unverified_categories"])

    async def test_severe_observation_gap_blocks_recommendation_evidence(self):
        derived = self.store.items[(self.pk, "RESTATEMENT#RESULT#test")]
        derived["observation_coverage"].update(internal_gap_detected=True,
            severe_observation_gap_detected=True)
        response = await self.client.get(f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["observation_coverage"]["severe_observation_gap_detected"])
        self.assertIn("SEVERE_OBSERVATION_GAP", response.json()["completeness_reasons"])
        self.assertEqual(response.json()["completeness"], "INSUFFICIENT")

    async def test_observation_summary_is_allowlisted_and_omitted_when_stale(self):
        result = self.store.items[(self.pk, "RESTATEMENT#RESULT#test")]
        result["temperature_sample_summary"] = {"version": "v1", "basis": "POINT_SAMPLES_ONLY",
            "sample_count": 2, "minimum_sample_celsius": "165", "maximum_sample_celsius": "180",
            "private_note": "never-return-this", "last_sampled_recovery": {
                "span_us": 5000000, "onset_bracketed_by_samples": True, "private_note": "also-private"}}
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        response = await self.client.get(path)
        self.assertEqual(response.json()["recorded"]["temperature_sample_summary"]["minimum_sample_celsius"], "165")
        self.assertNotIn("never-return-this", response.text)
        self.assertNotIn("also-private", response.text)
        self.store.items[(self.pk, "META")]["version"] = 8
        self.assertIsNone((await self.client.get(path)).json()["recorded"]["temperature_sample_summary"])

    async def test_preplacement_observation_does_not_claim_recency_or_return_private_fields(self):
        result = self.store.items[(self.pk, "RESTATEMENT#RESULT#test")]
        result["preplacement_temperature_observation"] = {"status": "OBSERVED", "sample_celsius": "168",
            "age_before_placement_us": 5000000, "quality_status": "WARNING", "private_note": "private-value"}
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        response = await self.client.get(path)
        value = response.json()
        self.assertEqual(value["recorded"]["preplacement_temperature_observation"]["sample_celsius"], "168")
        self.assertIn("PREPLACEMENT_RECENCY_NOT_ASSESSED", value["unverified_categories"])
        self.assertIn("PREPLACEMENT_TEMPERATURE_FLAGGED", value["unverified_categories"])
        self.assertNotIn("private-value", response.text)
        self.store.items[(self.pk, "META")]["version"] = 8
        value = (await self.client.get(path)).json()
        self.assertIsNone(value["recorded"]["preplacement_temperature_observation"])
        self.assertIn("PREPLACEMENT_TEMPERATURE_UNAVAILABLE", value["unverified_categories"])

    async def test_collection_receipt_reports_snapshot_match_without_claiming_completeness(self):
        derived = self.store.items[(self.pk, "RESTATEMENT#RESULT#test")]
        derived["collection_evidence"] = {"receipt_sk": "COLLECTION#RECEIPT#test", "raw_snapshot_matches": True}
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        result = (await self.client.get(path)).json()
        self.assertTrue(result["collection_snapshot_matches_receipt"])
        self.assertIn("SOURCE_COMPLETENESS", result["unverified_categories"])
        self.assertFalse(result["recommendation_execution_available"])
        self.store.items[(self.pk, "META")]["version"] = 8
        self.assertFalse((await self.client.get(path)).json()["collection_snapshot_matches_receipt"])

    async def test_revision_freshness_checks_owned_bound_stream_and_detects_races(self):
        binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001", "batch_id": "test"}
        self.store.items[(self.pk, "META")]["source_binding"] = binding
        derived = self.store.items[(self.pk, "RESTATEMENT#RESULT#test")]
        derived.update(source_stream_pk=stream_binding_key(**binding), source_revision=4)
        path = f"/api/customer/cooking-jobs/{self.job_id}/recommendation-evidence"
        with patch.dict(os.environ, {"FOODTECH_DATA_TABLE": "test-source"}), \
             patch("foodtech_business_api.recommendation_evidence.read_source_revision", new_callable=AsyncMock) as read:
            read.side_effect = [4, 4]
            result = await self.client.get(path)
            self.assertEqual(result.json()["source_revision_status"], "MATCHED_AT_READ")
            self.assertEqual(read.await_args.args, ("test-source", stream_binding_key(**binding)))
            read.side_effect = [5, 5]
            self.assertEqual((await self.client.get(path)).json()["source_revision_status"], "CHANGED")
            read.side_effect = [4, 5]
            self.assertEqual((await self.client.get(path)).status_code, 409)
            read.reset_mock(side_effect=True)
            derived["source_stream_pk"] = "STREAM#wrong"
            self.assertEqual((await self.client.get(path)).json()["source_revision_status"], "INCONSISTENT")
            read.assert_not_awaited()
        derived["source_stream_pk"] = stream_binding_key(**binding)
        with patch.dict(os.environ, {"FOODTECH_DATA_TABLE": ""}):
            self.assertEqual((await self.client.get(path)).json()["source_revision_status"], "NOT_CONFIGURED")

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
