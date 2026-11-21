import os
import unittest
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from foodtech_business_api import cooking, combi_meal
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app


class CombiMealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryCookingStore()
        self.subject = "customer-one"

        async def session(_request, _surface):
            return Session("customer", self.subject, None, "csrf", 9999999999, 0)

        self.patches = [patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}),
                        patch.object(cooking, "_session", side_effect=session),
                        patch.object(cooking, "get_cooking_store", return_value=self.store)]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.body = {"equipment_size": "MEDIUM_FIXED_RACK_10", "rack_count": 1, "pan_count": 10,
                     "dry_rice_kg": "20", "drained_rice_kg": "24", "added_water_kg": "20",
                     "pan_tare_kg": "10", "rack_tare_kg": "0"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    async def test_rice_preparation_is_versioned_and_precedes_cooking(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)
        meal = created.json()["meal"]
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        started = datetime.now(timezone.utc) - timedelta(minutes=40)
        soaked, drained = started + timedelta(minutes=30), started + timedelta(minutes=35)
        body = {"expected_version": 1, "wash_count": 3, "soaking_water_celsius": "20",
                "washed_rice_kg": "21", "soaked_rice_kg": "25",
                "soaking_started_at": started.isoformat(), "soaking_completed_at": soaked.isoformat(),
                "draining_completed_at": drained.isoformat()}
        self.assertEqual((await self.client.post(path + "/rice-preparation", headers=headers,
                         json={**body, "draining_completed_at": started.isoformat()})).status_code, 422)
        saved = await self.client.post(path + "/rice-preparation", headers=headers, json=body)
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual((saved.json()["step"]["rice_preparation"]["soaking_seconds"],
                          saved.json()["step"]["rice_preparation"]["draining_seconds"]), ("1800", "300"))
        self.assertEqual((await self.client.post(path + "/rice-preparation", headers=headers, json=body)).status_code, 409)
        self.assertEqual((await self.client.post(path + "/rice-preparation", headers=headers,
                         json={**body, "expected_version": 2})).status_code, 422)
        correction = {key: value for key, value in body.items() if key not in {"washed_rice_kg", "soaked_rice_kg"}}
        corrected = await self.client.post(path + "/rice-preparation", headers=headers,
                                           json={**correction, "expected_version": 2, "wash_count": 2, "reason": "횟수 정정"})
        self.assertEqual(corrected.status_code, 201, corrected.text)
        self.assertEqual(corrected.json()["event"]["previous"]["wash_count"], 3)
        self.assertEqual(corrected.json()["step"]["rice_preparation"]["washed_rice_kg"], "21")
        invalid = await self.client.post(path + "/events", headers=headers, json={
            "expected_version": 3, "event": "RICE_STARTED", "observed_at": soaked.isoformat()})
        self.assertEqual(invalid.status_code, 422)
        started_job = await self.client.post(path + "/events", headers=headers, json={
            "expected_version": 3, "event": "RICE_STARTED", "observed_at": drained.isoformat()})
        self.assertEqual(started_job.status_code, 200, started_job.text)
        self.assertEqual((await self.client.post(path + "/rice-preparation", headers=headers,
                         json={**body, "expected_version": 4, "reason": "늦은 수정"})).status_code, 409)
        self.subject = "another-customer"
        self.assertEqual((await self.client.post(path + "/rice-preparation", headers=headers,
                         json={**body, "expected_version": 4})).status_code, 404)

    async def test_creation_stores_linked_records_and_isolates_customers(self):
        response = await self.client.post("/api/customer/combi-meals", json=self.body,
                                          headers={"X-CSRF-Token": "csrf"})
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        meal = result["meal"]
        self.assertEqual(result["rice"]["water_ratio_status"], "NORMAL")
        self.assertEqual(result["rice"]["payload_kg"], "54")
        self.assertEqual(result["rice"]["job_id"], meal["rice_cooking_job_id"])
        self.assertEqual(result["transition"]["transition_id"], meal["mode_transition_id"])
        self.assertEqual(result["mackerel"]["job_id"], meal["mackerel_cooking_job_id"])
        self.assertEqual(result["mackerel"]["safety_status"], "NOT_MEASURED")
        self.assertFalse(result["transition"]["mackerel_load_allowed"])
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        self.assertEqual((await self.client.get(path)).json(), result)
        self.subject = "customer-two"
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_recipe_versions_are_explicit_and_legacy_remains_unset(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = await self.client.post('/api/customer/combi-meals', headers=headers,
            json={**self.body, 'rice_recipe_version': ' rice-v1 ', 'mackerel_recipe_version': 'fish-v2'})
        self.assertEqual(created.status_code, 201, created.text)
        meal = created.json()['meal']
        self.assertEqual((meal['rice_recipe_version'], meal['mackerel_recipe_version']), ('rice-v1', 'fish-v2'))
        loaded = await self.client.get('/api/customer/combi-meals/' + meal['meal_batch_id'])
        self.assertEqual(loaded.json()['meal']['rice_recipe_version'], 'rice-v1')
        old = await self.client.post('/api/customer/combi-meals', headers=headers, json=self.body)
        self.assertNotIn('rice_recipe_version', old.json()['meal'])
        invalid = await self.client.post('/api/customer/combi-meals', headers=headers,
            json={**self.body, 'rice_recipe_version': '   '})
        self.assertEqual(invalid.status_code, 422)

    async def test_quality_sample_evidence_requires_current_customer_and_dimensions(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = await self.client.post('/api/customer/combi-meals', headers=headers,
            json={**self.body, 'mackerel_recipe_version': 'fish-v1'})
        meal = created.json()['meal']
        path = f"/api/customer/combi-meals/{meal['meal_batch_id']}/quality/mackerel/sample-evidence"
        pk = combi_meal._pk(self.subject, meal['meal_batch_id'])
        step_key = f"JOB#MACKEREL#{meal['mackerel_cooking_job_id']}"
        step = self.store.items[(pk, step_key)]
        pointer = {'version': 1, 'safety_version': 1, 'event_sk': 'EVENT#000000000002',
                   'quality_status': 'QUALITY_PASSED', 'score_percent': '80.00', 'quality_target': True, 'usable': True}
        step.update(status='UNLOADED', safety_status='PASSED', safety_version=1,
                    quality_version=1, quality_current=pointer)
        assessment = {'version': 1, 'safety_version': 1, 'menu': 'MACKEREL', 'status': 'COMPLETED',
                      'scale_version': 'mackerel-common-v1', 'scores': {name: 4 for name in cooking.SCORE_ITEMS},
                      'quality_status': 'QUALITY_PASSED', 'quality_passed': True, 'score_percent': '80.00', 'quality_target': True,
                      'quality_note': 'private detail', 'actual_mass_kg': '1.2', 'actual_portions': 8}
        self.store.items[(pk, pointer['event_sk'])] = {'pk': pk, 'sk': pointer['event_sk'],
            'event': 'MACKEREL_QUALITY_ASSESSED', 'assessment': assessment}
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['missing_dimensions'], [])
        self.assertEqual(response.json()['status'], 'SAMPLE_EVIDENCE_READY')
        self.assertEqual(response.json()['load_band'], 'PARTIAL')
        self.assertEqual(response.json()['reference_mass_kg'], '15')
        self.assertEqual(response.json()['recipe_version'], 'fish-v1')
        self.assertNotIn('private detail', response.text)
        self.store.items[(pk, 'META')].pop('mackerel_recipe_version')
        legacy = await self.client.get(path)
        self.assertEqual(legacy.json()['status'], 'DIMENSIONS_INCOMPLETE')
        self.assertEqual(legacy.json()['missing_dimensions'], ['RECIPE_VERSION'])
        step['quality_current']['usable'] = False
        self.assertEqual((await self.client.get(path)).json()['reason'], 'CURRENT_ASSESSMENT_BLOCKED')
        self.subject = 'another-customer'
        self.assertEqual((await self.client.get(path)).status_code, 404)

    async def test_quality_sample_evidence_rejects_changed_snapshot(self):
        from uuid import uuid4
        with patch.object(combi_meal, 'get_meal', new=AsyncMock(side_effect=[
                {'meal': {'version': 1}}, {'meal': {'version': 2}}])):
            result = await self.client.get(f'/api/customer/combi-meals/{uuid4()}/quality/rice/sample-evidence')
        self.assertEqual(result.status_code, 409)

    async def test_meal_quality_aggregate_is_scoped_versioned_and_idempotent(self):
        headers = {'X-CSRF-Token': 'csrf'}
        created = await self.client.post('/api/customer/combi-meals', headers=headers,
            json={**self.body, 'mackerel_recipe_version': 'fish-v1'})
        meta = created.json()['meal']
        pk = combi_meal._pk(self.subject, meta['meal_batch_id'])
        step = self.store.items[(pk, f"JOB#MACKEREL#{meta['mackerel_cooking_job_id']}")]
        pointer = {'version': 1, 'safety_version': 1, 'event_sk': 'EVENT#000000000002',
                   'quality_status': 'QUALITY_PASSED', 'score_percent': '80.00', 'quality_target': True, 'usable': True}
        step.update(status='UNLOADED', safety_status='PASSED', safety_version=1,
                    quality_version=1, quality_current=pointer)
        assessment = {'version': 1, 'safety_version': 1, 'menu': 'MACKEREL', 'status': 'COMPLETED',
                      'scale_version': 'mackerel-common-v1', 'scores': {name: 4 for name in cooking.SCORE_ITEMS},
                      'quality_status': 'QUALITY_PASSED', 'quality_passed': True, 'score_percent': '80.00',
                      'quality_target': True, 'actual_mass_kg': '7.5', 'completed_at': '2026-11-22T01:00:00Z'}
        self.store.items[(pk, pointer['event_sk'])] = {'pk': pk, 'sk': pointer['event_sk'],
            'event': 'MACKEREL_QUALITY_ASSESSED', 'assessment': assessment}
        body = {'meal_ids': [meta['meal_batch_id']], 'menu': 'MACKEREL', 'equipment_size': meta['equipment_size'],
                'recipe_version': 'fish-v1', 'load_band': 'PARTIAL',
                'period_start': '2026-11-22T00:00:00Z', 'period_end': '2026-11-23T00:00:00Z'}
        path = '/api/customer/combi-quality-aggregates'
        response = await self.client.post(path, headers=headers, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        result = response.json()
        self.assertEqual((result['sample_count'], result['mean_quality_score_percent']), (1, '80.00'))
        self.assertEqual(result['snapshot_status'], 'HISTORICAL')
        retry = await self.client.post(path, headers=headers, json=body)
        self.assertEqual(retry.json()['result_id'], result['result_id'])
        excluded = await self.client.post(path, headers=headers, json={**body, 'load_band': 'REFERENCE'})
        self.assertEqual(excluded.status_code, 201, excluded.text)
        self.assertEqual(excluded.json()['sample_count'], 0)
        self.assertEqual(excluded.json()['references'][0]['exclusion_reason'], 'LOAD_BAND_MISMATCH')
        duplicate = await self.client.post(path, headers=headers,
            json={**body, 'meal_ids': [meta['meal_batch_id'], meta['meal_batch_id']]})
        self.assertEqual(duplicate.status_code, 422)
        candidate = await self.client.post(path, headers=headers,
            json={**body, 'period_start': '2026-11-21T00:00:00Z'})
        self.assertEqual(candidate.status_code, 201, candidate.text)
        comparison_body = {'baseline_group_id': result['group_id'], 'baseline_result_id': result['result_id'],
                           'candidate_group_id': candidate.json()['group_id'],
                           'candidate_result_id': candidate.json()['result_id']}
        compared = await self.client.post(path + '/compare', headers=headers, json=comparison_body)
        self.assertEqual(compared.status_code, 200, compared.text)
        self.assertEqual(compared.json()['deltas']['mean_quality_score_percent'], '0.00')
        self.assertEqual(compared.json()['interpretation'], 'DESCRIPTIVE_ONLY')
        bad_comparison = {**comparison_body, 'candidate_group_id': excluded.json()['group_id'],
                          'candidate_result_id': excluded.json()['result_id']}
        self.assertEqual((await self.client.post(path + '/compare', headers=headers, json=bad_comparison)).status_code, 422)
        link = f"{path}/{result['group_id']}/{result['result_id']}"
        self.assertEqual((await self.client.get(link)).status_code, 200)
        unchanged = await self.client.get(link + '/freshness')
        self.assertEqual(unchanged.json()['status'], 'UNCHANGED_AS_OBSERVED')
        save = self.store.save_meal_aggregate
        def change_before_save(*args):
            self.store.items[(pk, 'META')]['version'] += 1
            return save(*args)
        with patch.object(self.store, 'save_meal_aggregate', side_effect=change_before_save):
            self.assertEqual((await self.client.post(path, headers=headers, json=body)).status_code, 409)
        freshness = await self.client.get(link + '/freshness')
        self.assertEqual(freshness.json()['status'], 'CHANGED')
        self.assertEqual(freshness.json()['changed'], [{'meal_id': meta['meal_batch_id'], 'reason': 'VERSION_CHANGED'}])
        changed_compare = await self.client.post(path + '/compare', headers=headers, json=comparison_body)
        self.assertEqual(changed_compare.json()['baseline']['freshness']['status'], 'CHANGED')
        self.assertEqual((await self.client.get(link)).json()['result_id'], result['result_id'])
        del self.store.items[(pk, 'META')]
        missing = await self.client.get(link + '/freshness')
        self.assertEqual(missing.json()['changed'], [{'meal_id': meta['meal_batch_id'], 'reason': 'UNAVAILABLE'}])
        self.subject = 'another-customer'
        self.assertEqual((await self.client.get(link)).status_code, 404)
        self.assertEqual((await self.client.get(link + '/freshness')).status_code, 404)
        self.assertEqual((await self.client.post(path + '/compare', headers=headers, json=comparison_body)).status_code, 404)
        self.assertEqual((await self.client.post(path, headers=headers, json=body)).status_code, 404)

    async def test_linked_source_blocks_unrelated_transition_and_mackerel_samples(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meta = created["meal"]
        meal_id = meta["meal_batch_id"]
        path = "/api/customer/combi-meals/" + meal_id
        pk = combi_meal._pk(self.subject, meal_id)
        stored = self.store.items[(pk, "META")]
        stored.update(status="MODE_TRANSITION", source={"raw_sha256": "a" * 64})
        transition = self.store.items[(pk, f"TRANSITION#{meta['mode_transition_id']}")]
        transition.update(status="IN_PROGRESS", started_at="2026-01-01T00:00:00Z")
        samples = [{"second": 1, "chamber_celsius": "220", "climate_setpoint_percent": "10",
                    "door_closed": True, "critical_alarm": False, "humidity_percent": "10"}]
        response = await self.client.post(path + "/transition-evidence", headers=headers,
            json={"expected_version": 1, "humidity_supported": True, "vent_supported": False, "samples": samples})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("samples", transition)
        self.assertEqual(stored["version"], 1)
        stored["status"] = "MACKEREL_UNLOADED"
        fish = self.store.items[(pk, f"JOB#MACKEREL#{meta['mackerel_cooking_job_id']}")]
        fish.update(status="UNLOADED", program_cooking_duration=2, time_status="NORMAL")
        response = await self.client.post(path + "/mackerel-process", headers=headers,
            json={"expected_version": 1, "samples": [
                {"second": i, "chamber_celsius": "220", "door": "CLOSED", "fan_level": 3, "climate_setpoint_percent": "10"} for i in (1, 2)]})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("monitored_process_assessment", fish)
        self.assertEqual(stored["version"], 1)
        self.subject = "another-customer"
        self.assertEqual((await self.client.post(path + "/mackerel-process", headers=headers,
            json={"expected_version": 1, "samples": [{"second": 1, "chamber_celsius": "220", "door": "CLOSED"}]})).status_code, 404)

    async def test_manual_transition_is_rechecked_against_late_linked_source_before_fish(self):
        headers = {"X-CSRF-Token": "csrf"}
        meta = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()["meal"]
        meal_id = meta["meal_batch_id"]
        path = "/api/customer/combi-meals/" + meal_id
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = []
        for phase, job, count in (("RICE", meta["rice_cooking_job_id"], 2),
                                  ("TRANSITION", meta["mode_transition_id"], 30),
                                  ("MACKEREL", meta["mackerel_cooking_job_id"], 2)):
            for number in range(1, count + 1):
                sequence = len(records) + 1
                at = (start + timedelta(seconds=sequence - 1)).isoformat()
                records.append(json.dumps({"eventId": f"{meal_id}-{sequence:06d}", "mealBatchId": meal_id,
                    "cookingJobId": job, "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001",
                    "eventType": "OBSERVATION", "occurredAt": at, "receivedAt": at, "sequence": sequence,
                    "schemaVersion": "synthetic-combi-v2", "programVersion": "v1", "provenance": "SYNTHETIC",
                    "dataQuality": "VALID", "phase": phase, "phaseSecond": number,
                    "chamberCelsius": 100 if phase == "RICE" else 220,
                    "climateSetpointPercent": 100 if phase == "RICE" else 10,
                    "measuredHumidityPercent": 98 if phase == "RICE" else 10, "humidityValueSource": "MEASURED",
                    "fanLevel": 4 if phase == "RICE" else 3, "doorState": "CLOSED", "criticalAlarm": False,
                    "ventSupported": False, "ventEvent": None}))
        imported = await self.client.post(path + "/source-observations", headers=headers,
            json={"expected_version": 1, "device_id": "combi-1", "records": records})
        self.assertEqual(imported.status_code, 201, imported.text)
        pk = combi_meal._pk(self.subject, meal_id)
        stored = self.store.items[(pk, "META")]
        stored.update(status="MACKEREL_READY", last_observed_at=(start + timedelta(seconds=50)).isoformat())
        rice = self.store.items[(pk, f"JOB#RICE#{meta['rice_cooking_job_id']}")]
        rice.update(status="UNLOADED", active_cooking_started_at=start.isoformat(),
                    program_completed_at=(start + timedelta(seconds=2)).isoformat(), program_cooking_duration=2,
                    time_status="NORMAL", safety_status="PASSED", safety_version=1,
                    monitored_process_assessment={"status": "NORMAL", "reason_codes": [],
                                                  "fan_observation": {"status": "MATCHED"}, "climate_observation": {"status": "MATCHED"}},
                    monitored_process_samples=[{"second": 1}], quality_current={"version": 1, "usable": True})
        visible = (await self.client.get(path)).json()["rice"]
        self.assertEqual(visible["safety_status"], "INCOMPLETE")
        self.assertFalse(visible["quality_current"]["usable"])
        quality = {"expected_version": 2, "expected_current_version": 1, "kind": "REASSESSMENT", "reason": "원천 대조",
                   "scores": {key: 4 for key in cooking.SCORE_ITEMS},
                   "observations": {key: "합성 관찰" for key in cooking.SCORE_ITEMS},
                   "quality_decision": "QUALITY_PASSED", "planned_portions": 3, "actual_portions": 3}
        self.assertEqual((await self.client.post(path + "/quality/rice", headers=headers, json=quality)).status_code, 409)
        rice_result = await self.client.post(path + "/source-assessment/rice", headers=headers,
            json={"expected_version": 2, "reassess_manual_rice": True})
        self.assertEqual(rice_result.status_code, 200, rice_result.text)
        self.assertEqual(rice_result.json()["event"]["previous_samples"], [{"second": 1}])
        self.assertEqual(rice_result.json()["step"]["safety_status"], "INCOMPLETE")
        self.assertFalse(rice_result.json()["step"]["quality_current"]["usable"])
        self.assertNotIn("monitored_process_samples", rice_result.json()["step"])
        stored = self.store.items[(pk, "META")]
        step = self.store.items[(pk, f"TRANSITION#{meta['mode_transition_id']}")]
        step.update(status="DRY_HEAT_READY", mackerel_load_allowed=True,
                    started_at=(start + timedelta(seconds=2)).isoformat(),
                    samples=[{"second": 1, "private": "earlier manual record"}])
        fish = await self.client.post(path + "/events", headers=headers, json={"expected_version": 3,
            "event": "MACKEREL_STARTED", "observed_at": (start + timedelta(seconds=51)).isoformat()})
        self.assertEqual(fish.status_code, 409)
        verified = await self.client.post(path + "/source-assessment/transition", headers=headers,
            json={"expected_version": 3, "reassess_manual_transition": True})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["event"]["event"], "TRANSITION_SOURCE_REASSESSED")
        self.assertEqual(verified.json()["event"]["previous_transition"]["samples"][0]["private"], "earlier manual record")
        self.assertEqual(verified.json()["step"]["source"]["raw_sha256"], imported.json()["meal"]["source"]["raw_sha256"])
        self.assertEqual(verified.json()["meal"]["status"], "MACKEREL_READY")
        self.assertEqual(verified.json()["meal"]["last_observed_at"], (start + timedelta(seconds=50)).isoformat())
        repeat = await self.client.post(path + "/source-assessment/transition", headers=headers,
            json={"expected_version": 4, "reassess_manual_transition": True})
        self.assertEqual(repeat.status_code, 409)
        stored = self.store.items[(pk, "META")]
        stored["status"] = "MACKEREL_SAFETY_PASSED"
        fish = self.store.items[(pk, f"JOB#MACKEREL#{meta['mackerel_cooking_job_id']}")]
        fish.update(status="UNLOADED", active_cooking_started_at=(start + timedelta(seconds=32)).isoformat(),
                    program_completed_at=(start + timedelta(seconds=34)).isoformat(),
                    program_cooking_duration=2, time_status="NORMAL", safety_status="PASSED",
                    monitored_process_assessment={"status": "NORMAL", "reason_codes": []},
                    monitored_process_samples=[{"second": 1}, {"second": 2}],
                    quality_current={"version": 1, "usable": True}, quality_version=1)
        fetched = (await self.client.get(path)).json()
        self.assertFalse(fetched["mackerel"]["quality_current"]["usable"])
        self.assertEqual(fetched["mackerel"]["quality_current"]["blocked_reason"], "MACKEREL_SOURCE_UNVERIFIED")
        self.assertEqual((await self.client.post(path + "/quality/mackerel", headers=headers,
                                                json={**quality, "expected_version": 4})).status_code, 409)
        reassessed = await self.client.post(path + "/source-assessment/mackerel", headers=headers,
            json={"expected_version": 4, "reassess_manual_mackerel": True})
        self.assertEqual(reassessed.status_code, 200, reassessed.text)
        value = reassessed.json()
        self.assertEqual(value["event"]["event"], "MACKEREL_SOURCE_REASSESSED")
        self.assertEqual(value["event"]["previous_samples"], [{"second": 1}, {"second": 2}])
        self.assertEqual(value["step"]["safety_status"], "PASSED")
        self.assertFalse(value["step"]["quality_current"]["usable"])
        self.assertEqual(value["step"]["quality_current"]["blocked_reason"], "MACKEREL_PROCESS_REASSESSED")
        self.assertEqual(value["step"]["monitored_process_source"]["phase"], "MACKEREL")
        fish = self.store.items[(pk, f"JOB#MACKEREL#{meta['mackerel_cooking_job_id']}")]
        fish.pop("monitored_process_assessment")
        fish.pop("monitored_process_source")
        fish["quality_current"] = {"version": 1, "usable": True}
        first_source = await self.client.post(path + "/source-assessment/mackerel", headers=headers,
                                              json={"expected_version": 5})
        self.assertEqual(first_source.status_code, 200, first_source.text)
        self.assertFalse(first_source.json()["step"]["quality_current"]["usable"])
        self.assertEqual(first_source.json()["step"]["quality_current"]["blocked_reason"], "MACKEREL_PROCESS_REASSESSED")
        for phase, key, version in (("rice", f"JOB#RICE#{meta['rice_cooking_job_id']}", 6),
                                    ("mackerel", f"JOB#MACKEREL#{meta['mackerel_cooking_job_id']}", 7)):
            step = self.store.items[(pk, key)]
            step["monitored_process_assessment"].pop("climate_observation")
            legacy = await self.client.post(path + f"/source-assessment/{phase}", headers=headers,
                json={"expected_version": version, "reassess_legacy_climate": True})
            self.assertEqual(legacy.status_code, 200, legacy.text)
            self.assertEqual(legacy.json()["event"]["event"], f"{phase.upper()}_CLIMATE_SOURCE_REASSESSED")
            self.assertNotIn("climate_observation", legacy.json()["event"]["previous_assessment"])
            self.assertEqual(legacy.json()["step"]["monitored_process_assessment"]["climate_observation"]["status"], "MATCHED")
            self.assertFalse(legacy.json()["step"]["quality_current"]["usable"])

    async def test_rice_safety_rejects_legacy_process_without_climate_observation(self):
        headers = {"X-CSRF-Token": "csrf"}
        meta = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()["meal"]
        path = "/api/customer/combi-meals/" + meta["meal_batch_id"]
        pk = combi_meal._pk(self.subject, meta["meal_batch_id"])
        rice = self.store.items[(pk, f"JOB#RICE#{meta['rice_cooking_job_id']}")]
        rice.update(status="UNLOADED", safety_status="PASSED", quality_current={"version": 1, "usable": True},
                    monitored_process_assessment={"status": "NORMAL", "reason_codes": [],
                                                  "fan_observation": {"status": "MATCHED"}})
        visible = (await self.client.get(path)).json()["rice"]
        self.assertEqual(visible["safety_status"], "INCOMPLETE")
        self.assertEqual(visible["quality_current"]["blocked_reason"], "CLIMATE_EVIDENCE_UNVERIFIED")
        self.assertEqual(visible["monitored_process_assessment"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(visible["monitored_process_assessment"]["recorded_status"], "NORMAL")
        self.assertEqual(rice["monitored_process_assessment"]["status"], "NORMAL")
        response = await self.client.post(path + "/rice-safety", headers=headers, json={
            "expected_version": 1, "observation": "NO_ANOMALY", "note": "합성 안전 확인"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["step"]["safety_status"], "INCOMPLETE")
        self.assertFalse(response.json()["event"]["climate_checked"])

    async def test_rice_safety_uses_process_and_explicit_anomaly_confirmation(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meta = created["meal"]
        path = "/api/customer/combi-meals/" + meta["meal_batch_id"]
        pk = combi_meal._pk(self.subject, meta["meal_batch_id"])
        rice = self.store.items[(pk, f"JOB#RICE#{meta['rice_cooking_job_id']}")]
        rice["status"] = "UNLOADED"
        confirmation = {"expected_version": 1, "observation": "NO_ANOMALY", "note": "합성 안전 확인"}
        incomplete = await self.client.post(path + "/rice-safety", headers=headers, json=confirmation)
        self.assertEqual(incomplete.json()["step"]["safety_status"], "INCOMPLETE")
        rice = self.store.items[(pk, rice["sk"])]
        rice["monitored_process_assessment"] = {"status": "NORMAL", "reason_codes": [], "fan_observation": {"status": "MATCHED"}, "climate_observation": {"status": "MATCHED"}}
        passed = await self.client.post(path + "/rice-safety", headers=headers,
                                        json={**confirmation, "expected_version": 2})
        self.assertEqual(passed.json()["step"]["safety_status"], "PASSED")
        quality = {"expected_version": 3, "expected_current_version": 0, "kind": "INITIAL",
                   "scores": {key: 4 for key in cooking.SCORE_ITEMS},
                   "observations": {key: "합성 관찰" for key in cooking.SCORE_ITEMS},
                   "quality_decision": "QUALITY_PASSED", "planned_portions": 3, "actual_portions": 3}
        assessed = await self.client.post(path + "/quality/rice", headers=headers, json=quality)
        self.assertEqual(assessed.status_code, 201, assessed.text)
        self.assertTrue(assessed.json()["step"]["quality_current"]["usable"])
        suspected = await self.client.post(path + "/rice-safety", headers=headers, json={
            "expected_version": 4, "observation": "SUSPECTED", "note": "합성 이상 보고"})
        self.assertEqual(suspected.json()["step"]["safety_status"], "SUSPECTED")
        self.assertFalse(suspected.json()["step"]["quality_current"]["usable"])
        cleared = await self.client.post(path + "/rice-safety", headers=headers,
                                         json={**confirmation, "expected_version": 5, "note": "이상 해소 확인"})
        self.assertEqual(cleared.json()["step"]["safety_status"], "PASSED")
        self.assertFalse(cleared.json()["step"]["quality_current"]["usable"])
        self.assertEqual((await self.client.post(path + "/quality/rice", headers=headers,
            json={**quality, "expected_version": 6})).status_code, 409)
        reassessed = await self.client.post(path + "/quality/rice", headers=headers, json={
            **quality, "expected_version": 6, "expected_current_version": 1,
            "kind": "REASSESSMENT", "reason": "안전 확인 후 재평가"})
        self.assertEqual(reassessed.status_code, 201, reassessed.text)
        self.assertTrue(reassessed.json()["step"]["quality_current"]["usable"])

    async def test_rice_safety_never_accepts_failed_or_unverified_evidence(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meta = created["meal"]
        pk = combi_meal._pk(self.subject, meta["meal_batch_id"])
        step = self.store.items[(pk, f"JOB#RICE#{meta['rice_cooking_job_id']}")]
        step.update(status="UNLOADED", monitored_process_assessment={"status": "FAILED", "reason_codes": []})
        path = "/api/customer/combi-meals/" + meta["meal_batch_id"] + "/rice-safety"
        body = {"expected_version": 1, "observation": "NO_ANOMALY", "note": "확인"}
        response = await self.client.post(path, headers=headers, json=body)
        self.assertEqual(response.json()["step"]["safety_status"], "INCOMPLETE")
        step = self.store.items[(pk, step["sk"])]
        step["monitored_process_assessment"] = {"status": "WARNING", "reason_codes": ["SENSOR_ANOMALY_SUSPECTED"]}
        response = await self.client.post(path, headers=headers, json={**body, "expected_version": 2})
        self.assertEqual(response.json()["step"]["safety_status"], "SUSPECTED")

        step = self.store.items[(pk, step["sk"])]
        step["monitored_process_assessment"] = {"status": "NORMAL", "reason_codes": []}
        self.store.items[(pk, "META")]["source"] = {"raw_sha256": "synthetic"}
        manual = await self.client.post("/api/customer/combi-meals/" + meta["meal_batch_id"] + "/rice-process",
            headers=headers, json={"expected_version": 3, "humidity_source": "MEASURED",
                                   "samples": [{"second": 1, "chamber_celsius": "100", "humidity_percent": "98"}]})
        self.assertEqual(manual.status_code, 409)
        response = await self.client.post(path, headers=headers, json={**body, "expected_version": 3})
        self.assertEqual(response.json()["step"]["safety_status"], "INCOMPLETE")
        step = self.store.items[(pk, step["sk"])]
        step.update(monitored_process_source={"raw_sha256": "synthetic"}, rice_critical_alarm_observed=True)
        response = await self.client.post(path, headers=headers, json={**body, "expected_version": 4})
        self.assertEqual(response.json()["step"]["safety_status"], "SUSPECTED")

    async def test_legacy_rice_normal_without_fan_evidence_does_not_pass_safety(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meta = created["meal"]
        pk = combi_meal._pk(self.subject, meta["meal_batch_id"])
        step = self.store.items[(pk, f"JOB#RICE#{meta['rice_cooking_job_id']}")]
        step.update(status="UNLOADED", monitored_process_assessment={"status": "NORMAL", "reason_codes": []})
        response = await self.client.post(f"/api/customer/combi-meals/{meta['meal_batch_id']}/rice-safety",
            headers=headers, json={"expected_version": 1, "observation": "NO_ANOMALY", "note": "합성 확인"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["step"]["safety_status"], "INCOMPLETE")
        stored = self.store.items[(pk, step["sk"])]
        stored.update(safety_status="PASSED", quality_current={"version": 1, "usable": True})
        snapshot = (await self.client.get(f"/api/customer/combi-meals/{meta['meal_batch_id']}")).json()
        self.assertEqual(snapshot["rice"]["safety_status"], "INCOMPLETE")
        self.assertFalse(snapshot["rice"]["quality_current"]["usable"])
        self.assertEqual(self.store.items[(pk, step["sk"])]["safety_status"], "PASSED")
        quality = {"expected_version": 2, "expected_current_version": 1, "kind": "REASSESSMENT", "reason": "확인",
                   "scores": {key: 4 for key in cooking.SCORE_ITEMS},
                   "observations": {key: "합성 관찰" for key in cooking.SCORE_ITEMS},
                   "quality_decision": "QUALITY_PASSED", "planned_portions": 3, "actual_portions": 3}
        denied = await self.client.post(f"/api/customer/combi-meals/{meta['meal_batch_id']}/quality/rice",
                                        headers=headers, json=quality)
        self.assertEqual(denied.status_code, 409)

    async def test_live_source_isolated_idempotent_and_checked_before_completion(self):
        headers = {"X-CSRF-Token": "csrf"}
        meta = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()["meal"]
        meal_id = meta["meal_batch_id"]
        path = "/api/customer/combi-meals/" + meal_id
        records = []
        for index, (phase, job) in enumerate((("RICE", meta["rice_cooking_job_id"]),
                                               ("TRANSITION", meta["mode_transition_id"]),
                                               ("MACKEREL", meta["mackerel_cooking_job_id"])), 1):
            at = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index-1)).isoformat()
            records.append(json.dumps({"eventId": f"{meal_id}-{index:06d}", "mealBatchId": meal_id,
                "cookingJobId": job, "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001",
                "eventType": "OBSERVATION", "occurredAt": at, "receivedAt": at, "sequence": index,
                "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
                "dataQuality": "VALID", "phase": phase, "phaseSecond": 1, "chamberCelsius": 100,
                "climateSetpointPercent": 100, "measuredHumidityPercent": 98,
                "humidityValueSource": "MEASURED", "fanLevel": 4, "doorState": "CLOSED"}))
        batch = {"device_id": "combi-1", "expected_cursor": 0, "records": records[:1]}
        self.assertEqual((await self.client.get(path + "/source-live")).json()["state"], "NOT_STARTED")
        self.assertEqual((await self.client.post(path + "/source-live", json=batch)).status_code, 403)
        first = await self.client.post(path + "/source-live", json=batch, headers=headers)
        self.assertEqual(first.json(), {"next_cursor": 1, "duplicate": False})
        duplicate = await self.client.post(path + "/source-live", json=batch, headers=headers)
        self.assertEqual(duplicate.json(), {"next_cursor": 1, "duplicate": True})
        events = await self.client.get(path + "/source-live/events")
        self.assertEqual(events.headers["cache-control"], "no-store")
        self.assertEqual([item["code"] for item in events.json()["events"]], ["PHASE_STARTED"])
        self.assertEqual(events.json()["history_from_sequence"], 1)
        snapshot = await self.client.get(path + "/source-live/records")
        self.assertEqual(snapshot.headers["cache-control"], "no-store")
        self.assertEqual(snapshot.json(), {"count": 1, "device_id": "combi-1", "records": records[:1]})
        live_pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal_id}#LIVE"
        chunk = self.store.items[(live_pk, "CHUNK#000000")]
        original = chunk["records"][0]
        chunk["records"][0] = original + " "
        self.assertEqual((await self.client.get(path + "/source-live/records")).status_code, 503)
        chunk["records"][0] = original
        changed = json.loads(records[0]); changed["chamberCelsius"] = 99
        self.assertEqual((await self.client.post(path + "/source-live", json={**batch, "records": [json.dumps(changed)]}, headers=headers)).status_code, 409)
        gap = {**batch, "expected_cursor": 2, "records": records[2:]}
        self.assertEqual((await self.client.post(path + "/source-live", json=gap, headers=headers)).status_code, 409)
        status = (await self.client.get(path + "/source-live")).json()
        self.assertEqual((status["state"], status["count"], status["latest"]["phase"]), ("COLLECTING", 1, "RICE"))
        self.subject = "customer-two"
        self.assertEqual((await self.client.get(path + "/source-live")).status_code, 404)
        self.assertEqual((await self.client.get(path + "/source-live/records")).status_code, 404)
        self.assertEqual((await self.client.get(path + "/source-live/events")).status_code, 404)
        self.subject = "customer-one"
        completion = {"expected_version": 1, "device_id": "combi-1", "records": records}
        self.assertEqual((await self.client.post(path + "/source-observations", json=completion, headers=headers)).status_code, 409)
        rest = {"device_id": "combi-1", "expected_cursor": 1, "records": records[1:]}
        self.assertEqual((await self.client.post(path + "/source-live", json=rest, headers=headers)).status_code, 200)
        monitor = (await self.client.get(path + "/source-live")).json()["mackerel_monitor"]
        self.assertEqual((monitor["last_second"], monitor["complete_from_start"]), (1, True))
        self.assertEqual(monitor["failure_codes"], [])
        history = (await self.client.get(path + "/source-live/events")).json()
        self.assertEqual([item["phase"] for item in history["events"]], ["MACKEREL", "TRANSITION", "RICE"])
        altered = list(records); altered[0] = json.dumps(changed)
        self.assertEqual((await self.client.post(path + "/source-observations", json={**completion, "records": altered}, headers=headers)).status_code, 409)
        response = await self.client.post(path + "/source-observations", json=completion, headers=headers)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual((await self.client.get(path + "/source-live")).json()["state"], "COMPLETED")
        self.assertEqual((await self.client.post(path + "/source-live", json={**batch, "expected_cursor": 3}, headers=headers)).status_code, 409)

    async def test_live_five_second_windows_across_fragments_and_phases(self):
        headers = {"X-CSRF-Token": "csrf"}
        meta = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()["meal"]
        meal_id = meta["meal_batch_id"]
        path = "/api/customer/combi-meals/" + meal_id
        records = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for phase, job, temperatures in (("RICE", meta["rice_cooking_job_id"], [97, 98, 100, 102, 103]),
                                         ("TRANSITION", meta["mode_transition_id"], [214, 215, 220, 225, 226])):
            for phase_second, temperature in enumerate(temperatures, 1):
                seq = len(records) + 1
                at = (start + timedelta(seconds=seq-1)).isoformat()
                records.append(json.dumps({"eventId": f"{meal_id}-{seq:06d}", "mealBatchId": meal_id,
                    "cookingJobId": job, "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001",
                    "eventType": "OBSERVATION", "occurredAt": at, "receivedAt": at, "sequence": seq,
                    "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
                    "dataQuality": "VALID", "phase": phase, "phaseSecond": phase_second,
                    "chamberCelsius": temperature, "climateSetpointPercent": 100, "measuredHumidityPercent": 98,
                    "humidityValueSource": "MEASURED", "fanLevel": 4, "doorState": "CLOSED"}))
        for cursor, end in ((0, 2), (2, 5), (5, 7), (7, 10)):
            result = await self.client.post(path + "/source-live", headers=headers,
                json={"device_id": "combi-1", "expected_cursor": cursor, "records": records[cursor:end]})
            self.assertEqual(result.status_code, 200, result.text)
            live = (await self.client.get(path + "/source-live")).json()
            window = live["latest_window"]
            self.assertEqual(live["rice_monitor"]["last_second"], min(end, 5))
            self.assertTrue(live["rice_monitor"]["complete_from_start"])
            if end == 2:
                self.assertIsNone(window)
            elif end in (5, 7):
                self.assertEqual(window["phase"], "RICE")
                self.assertEqual((window["minimum_celsius"], window["maximum_celsius"],
                                  window["average_celsius"], window["outside_target_seconds"]), ("97", "103", "100", 2))
            else:
                self.assertEqual(window["phase"], "TRANSITION")
                self.assertEqual((window["first_phase_second"], window["last_phase_second"],
                                  window["average_celsius"], window["outside_target_seconds"]), (1, 5, "220", 2))

    async def test_live_event_history_paginates_and_preserves_legacy_boundary(self):
        headers = {"X-CSRF-Token": "csrf"}
        meta = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()["meal"]
        meal_id = meta["meal_batch_id"]; path = "/api/customer/combi-meals/" + meal_id
        records = []
        for seq in range(1, 31):
            at = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seq - 1)).isoformat()
            records.append(json.dumps({"eventId": f"{meal_id}-{seq:06d}", "mealBatchId": meal_id,
                "cookingJobId": meta["rice_cooking_job_id"], "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001",
                "eventType": "OBSERVATION", "occurredAt": at, "receivedAt": at, "sequence": seq,
                "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
                "dataQuality": "VALID", "phase": "RICE", "phaseSecond": seq, "chamberCelsius": 100,
                "climateSetpointPercent": 100, "measuredHumidityPercent": 98, "humidityValueSource": "MEASURED",
                "fanLevel": 4, "doorState": "OPEN" if seq % 2 == 0 else "CLOSED"}))
        for start in range(0, 30, 5):
            result = await self.client.post(path + "/source-live", headers=headers,
                json={"device_id": "combi-1", "expected_cursor": start, "records": records[start:start+5]})
            self.assertEqual(result.status_code, 200, result.text)
        first = (await self.client.get(path + "/source-live/events")).json()
        second = (await self.client.get(path + "/source-live/events?cursor=25")).json()
        self.assertEqual((len(first["events"]), first["next_cursor"], len(second["events"]), second["next_cursor"]),
                         (25, 25, 5, None))
        self.assertEqual([item["sequence"] for item in first["events"] + second["events"]], list(range(30, 0, -1)))
        live_pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal_id}#LIVE"
        self.store.items[(live_pk, "META")]["event_history_from_sequence"] = 6
        self.assertEqual((await self.client.get(path + "/source-live/events")).json()["history_from_sequence"], 6)

    async def test_source_import_binds_and_pages_without_advancing_cooking(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meta = created["meal"]
        meal_id = meta["meal_batch_id"]
        path = "/api/customer/combi-meals/" + meal_id
        phases = [("RICE", meta["rice_cooking_job_id"], 103),
                  ("TRANSITION", meta["mode_transition_id"], 1),
                  ("MACKEREL", meta["mackerel_cooking_job_id"], 1)]
        records = []
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for phase, job_id, count in phases:
            for phase_second in range(1, count + 1):
                seq = len(records) + 1
                at = (start + timedelta(seconds=seq - 1)).isoformat()
                records.append(json.dumps({"eventId": f"{meal_id}-{seq:06d}", "mealBatchId": meal_id,
                    "cookingJobId": job_id, "deviceId": "combi-1", "mockInstanceId": "mockprofile-120-a-001",
                    "eventType": "OBSERVATION", "occurredAt": at, "receivedAt": at, "sequence": seq,
                    "schemaVersion": "synthetic-combi-v1", "programVersion": "v1", "provenance": "SYNTHETIC",
                    "dataQuality": "VALID", "phase": phase, "phaseSecond": phase_second,
                    "chamberCelsius": 100, "climateSetpointPercent": 100, "measuredHumidityPercent": 98,
                    "humidityValueSource": "MEASURED", "fanLevel": 4, "doorState": "CLOSED"}))
        body = {"expected_version": 1, "device_id": "combi-1", "records": records}
        wrong = [*records]
        changed = json.loads(wrong[1]); changed["cookingJobId"] = "foreign"; wrong[1] = json.dumps(changed)
        rejected = await self.client.post(path + "/source-observations", headers=headers,
                                          json={**body, "records": wrong})
        self.assertEqual(rejected.status_code, 422)
        self.assertFalse(any(item_pk.endswith("#SOURCE") for item_pk, _ in self.store.items))
        self.assertEqual((await self.client.post(path + "/source-observations", json=body)).status_code, 403)
        imported = await self.client.post(path + "/source-observations", headers=headers, json=body)
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.json()["meal"]["source"]["phase_counts"],
                         {"RICE": 103, "TRANSITION": 1, "MACKEREL": 1})
        self.assertEqual(imported.json()["meal"]["status"], "PREPARING_RICE")
        first = (await self.client.get(path + "/source-observations")).json()
        second = (await self.client.get(path + "/source-observations?cursor=1")).json()
        self.assertEqual((len(first["records"]), first["next_cursor"], len(second["records"]), second["next_cursor"]),
                         (100, 1, 5, None))
        self.assertEqual(first["records"] + second["records"], records)
        windows = await self.client.get(path + "/source-windows?phase=RICE")
        self.assertEqual(windows.status_code, 200, windows.text)
        self.assertEqual((len(windows.json()["windows"]), windows.json()["next_cursor"]), (21, None))
        self.assertEqual((windows.json()["windows"][0]["average_celsius"],
                          windows.json()["windows"][0]["outside_target_seconds"]), ("100", 0))
        self.assertEqual((windows.json()["windows"][-1]["sample_count"],
                          windows.json()["windows"][-1]["complete_window"]), (3, False))
        page = await self.client.get(path + "/source-windows?phase=RICE&cursor=20")
        self.assertEqual(len(page.json()["windows"]), 1)
        self.assertEqual((await self.client.get(path + "/source-windows?phase=RICE&cursor=21")).status_code, 404)
        source_pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal_id}#SOURCE"
        chunk = self.store.items[(source_pk, "RAW#0000")]
        original = chunk["records"][0]
        altered = json.loads(original); altered["chamberCelsius"] = 101
        chunk["records"][0] = json.dumps(altered)
        self.assertEqual((await self.client.get(path + "/source-windows?phase=RICE")).status_code, 422)
        chunk["records"][0] = original
        self.assertEqual((await self.client.post(path + "/source-observations", headers=headers,
                         json={**body, "expected_version": 2})).status_code, 409)
        for version, event, offset in ((2, "RICE_STARTED", 0), (3, "RICE_PROGRAM_COMPLETED", 102),
                                       (4, "RICE_UNLOADED", 102)):
            step = await self.client.post(path + "/events", headers=headers, json={
                "expected_version": version, "event": event,
                "observed_at": (start + timedelta(seconds=offset)).isoformat()})
            self.assertEqual(step.status_code, 200, step.text)
        mismatch = await self.client.post(path + "/source-assessment/rice", headers=headers,
                                          json={"expected_version": 5})
        self.assertEqual(mismatch.status_code, 422)
        pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal_id}"
        self.store.items[(pk, f"JOB#RICE#{meta['rice_cooking_job_id']}")]["program_cooking_duration"] = 103
        result = await self.client.post(path + "/source-assessment/rice", headers=headers,
                                        json={"expected_version": 5})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["step"]["monitored_process_assessment"]["status"], "FAILED")
        self.assertEqual(result.json()["step"]["monitored_process_source"]["phase"], "RICE")
        transition = await self.client.post(path + "/events", headers=headers, json={
            "expected_version": 6, "event": "MODE_TRANSITION_STARTED",
            "observed_at": (start + timedelta(seconds=103)).isoformat()})
        self.assertEqual(transition.status_code, 200, transition.text)
        legacy = await self.client.post(path + "/source-assessment/transition", headers=headers,
                                         json={"expected_version": 7})
        self.assertEqual(legacy.status_code, 422)
        self.subject = "customer-two"
        self.assertEqual((await self.client.get(path + "/source-observations")).status_code, 404)
        self.assertEqual((await self.client.get(path + "/source-windows?phase=RICE")).status_code, 404)

    async def test_load_rejection_and_water_warning(self):
        headers = {"X-CSRF-Token": "csrf"}
        for values in ({"pan_count": 11}, {"drained_rice_kg": "19"}, {"rack_tare_kg": "2"},
                       {"drained_rice_kg": "60"}):
            result = await self.client.post("/api/customer/combi-meals", headers=headers,
                                            json={**self.body, **values})
            self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(self.store.items, {})
        warning = await self.client.post("/api/customer/combi-meals", headers=headers,
                                         json={**self.body, "added_water_kg": "10"})
        self.assertEqual(warning.status_code, 201, warning.text)
        self.assertEqual(warning.json()["rice"]["water_ratio_status"], "WATER_RATIO_OUT_OF_RANGE")
        denied = await self.client.post("/api/customer/combi-meals", json=self.body)
        self.assertEqual(denied.status_code, 403)

    async def test_rice_sequence_times_and_optimistic_conflict(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        path = "/api/customer/combi-meals/" + created["meal"]["meal_batch_id"]
        start = datetime.now(timezone.utc) - timedelta(seconds=1900)

        async def send(event, version, at):
            return await self.client.post(path + "/events", headers=headers,
                                          json={"event": event, "expected_version": version,
                                                "observed_at": at.isoformat()})

        self.assertEqual((await send("MODE_TRANSITION_STARTED", 1, start)).status_code, 409)
        started = await send("RICE_STARTED", 1, start)
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual((await send("RICE_STARTED", 1, start)).status_code, 409)
        self.assertEqual((await send("RICE_PROGRAM_COMPLETED", 2, start - timedelta(seconds=1))).status_code, 422)
        completed = await send("RICE_PROGRAM_COMPLETED", 2, start + timedelta(seconds=1800))
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual((completed.json()["step"]["time_status"],
                          completed.json()["step"]["program_cooking_duration"]), ("NORMAL", 1800))
        unloaded = await send("RICE_UNLOADED", 3, start + timedelta(seconds=1830))
        self.assertEqual(unloaded.json()["step"]["thermal_exposure_duration"], 1830)
        transition = await send("MODE_TRANSITION_STARTED", 4, start + timedelta(seconds=1840))
        self.assertEqual(transition.status_code, 200, transition.text)
        self.assertFalse(transition.json()["step"]["mackerel_load_allowed"])
        snapshot = (await self.client.get(path)).json()
        self.assertEqual(snapshot["meal"]["status"], "MODE_TRANSITION")
        self.assertEqual(snapshot["rice"]["process_status"], "NOT_MEASURED")
        self.assertEqual(len(snapshot["events"]), 4)
        samples = [{"second": second, "chamber_celsius": "100", "humidity_percent": "98", "door": "CLOSED", "fan_level": 4, "climate_setpoint_percent": "100"}
                   for second in range(1, 1801)]
        process = await self.client.post(path + "/rice-process", headers=headers,
            json={"expected_version": 5, "humidity_source": "MEASURED", "samples": samples})
        self.assertEqual(process.status_code, 200, process.text)
        self.assertEqual(process.json()["step"]["monitored_process_assessment"]["status"], "NORMAL")
        self.assertEqual(process.json()["meal"]["status"], "MODE_TRANSITION")
        self.assertEqual(process.json()["step"]["process_status"], "NOT_MEASURED")
        self.subject = "customer-two"
        self.assertEqual((await send("RICE_STARTED", 6, start)).status_code, 404)

    async def test_mode_evidence_gates_fish_and_preserves_prior_samples(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        path = "/api/customer/combi-meals/" + created["meal"]["meal_batch_id"]
        start = datetime.now(timezone.utc) - timedelta(seconds=2200)
        for version, event, offset in ((1, "RICE_STARTED", 0), (2, "RICE_PROGRAM_COMPLETED", 1800),
                                       (3, "RICE_UNLOADED", 1810), (4, "MODE_TRANSITION_STARTED", 1820)):
            response = await self.client.post(path + "/events", headers=headers,
                json={"expected_version": version, "event": event,
                      "observed_at": (start + timedelta(seconds=offset)).isoformat()})
            self.assertEqual(response.status_code, 200, response.text)
        samples = [{"second": i, "chamber_celsius": "220", "climate_setpoint_percent": "10",
                    "door_closed": True, "critical_alarm": False, "humidity_percent": "10"}
                   for i in range(1, 31)]
        body = {"expected_version": 5, "humidity_supported": True, "vent_supported": False,
                "samples": samples[:15]}
        pending = await self.client.post(path + "/transition-evidence", headers=headers, json=body)
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertFalse(pending.json()["step"]["mackerel_load_allowed"])
        changed = [dict(item) for item in samples]
        changed[0]["chamber_celsius"] = "219"
        rejected = await self.client.post(path + "/transition-evidence", headers=headers,
                                          json={**body, "expected_version": 6, "samples": changed})
        self.assertEqual(rejected.status_code, 409)
        ready = await self.client.post(path + "/transition-evidence", headers=headers,
                                       json={**body, "expected_version": 6, "samples": samples})
        self.assertEqual(ready.status_code, 200, ready.text)
        self.assertEqual(ready.json()["meal"]["status"], "MACKEREL_READY")
        self.assertTrue(ready.json()["step"]["mackerel_load_allowed"])
        self.assertEqual(ready.json()["step"]["ready_at_second"], 30)
        self.assertEqual((await self.client.get(path)).json()["mackerel"]["safety_status"], "NOT_MEASURED")

    async def test_mackerel_workflow_and_safety_sample_gate(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        path = "/api/customer/combi-meals/" + created["meal"]["meal_batch_id"]
        start = datetime.now(timezone.utc) - timedelta(seconds=3100)
        for version, event, offset in ((1, "RICE_STARTED", 0), (2, "RICE_PROGRAM_COMPLETED", 1800),
                                       (3, "RICE_UNLOADED", 1810), (4, "MODE_TRANSITION_STARTED", 1820)):
            response = await self.client.post(path + "/events", headers=headers,
                json={"expected_version": version, "event": event,
                      "observed_at": (start + timedelta(seconds=offset)).isoformat()})
            self.assertEqual(response.status_code, 200, response.text)
        products = [{"product_id": f"fish-{n}", "rack_id": "rack-1", "shelf": n,
                     "pan_id": f"pan-{n}", "mass_kg": "0.13", "thickness_cm": "1.5"}
                    for n in (1, 2, 3)]
        early = await self.client.post(path + "/events", headers=headers,
            json={"expected_version": 5, "event": "MACKEREL_STARTED",
                  "observed_at": (start + timedelta(seconds=1850)).isoformat(), "products": products})
        self.assertEqual(early.status_code, 409)
        samples = [{"second": i, "chamber_celsius": "220", "climate_setpoint_percent": "10",
                    "door_closed": True, "critical_alarm": False, "humidity_percent": "10"} for i in range(1, 31)]
        ready = await self.client.post(path + "/transition-evidence", headers=headers,
            json={"expected_version": 5, "humidity_supported": True, "vent_supported": False, "samples": samples})
        self.assertEqual(ready.status_code, 200, ready.text)
        for version, event, offset in ((6, "MACKEREL_STARTED", 1850),
                                       (7, "MACKEREL_PROGRAM_COMPLETED", 2570),
                                       (8, "MACKEREL_UNLOADED", 2600)):
            response = await self.client.post(path + "/events", headers=headers,
                json={"expected_version": version, "event": event,
                      "observed_at": (start + timedelta(seconds=offset)).isoformat(),
                      **({"products": products} if event == "MACKEREL_STARTED" else {})})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["step"]["thermal_exposure_duration"], 750)
        probes = [{"probe_id": f"probe-{n}", "product_id": f"fish-{n}", "calibration_status": "OK",
                   "readings": [{"second": i, "celsius": "75"} for i in range(60)]} for n in (1, 2, 3)]
        body = {"expected_version": 9, "measured_at": (start + timedelta(seconds=2600)).isoformat(),
                "probes": probes}
        incomplete = await self.client.post(path + "/mackerel-safety", headers=headers,
                                            json={**body, "probes": probes[:2]})
        self.assertEqual(incomplete.json()["step"]["safety_status"], "ASSESSMENT_INCOMPLETE")
        result = await self.client.post(path + "/mackerel-safety", headers=headers,
                                        json={**body, "expected_version": 10})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["step"]["safety_status"], "PASSED")
        self.assertEqual(result.json()["step"]["batch_representative_core_temperature"], "75")
        self.assertEqual(result.json()["step"]["process_status"], "NOT_MEASURED")
        process_samples = [{"second": i, "chamber_celsius": "220", "door": "CLOSED", "fan_level": 3, "climate_setpoint_percent": "10"}
                           for i in range(1, 721)]
        bad_process = await self.client.post(path + "/mackerel-process", headers=headers,
                                             json={"expected_version": 11, "samples": process_samples[:-1]})
        self.assertEqual(bad_process.status_code, 422)
        process = await self.client.post(path + "/mackerel-process", headers=headers,
                                         json={"expected_version": 11, "samples": process_samples})
        self.assertEqual(process.status_code, 200, process.text)
        self.assertEqual(process.json()["step"]["monitored_process_assessment"]["status"], "NORMAL")
        self.assertEqual(process.json()["step"]["safety_status"], "PASSED")
        self.assertEqual(process.json()["step"]["process_status"], "NOT_MEASURED")
        self.assertEqual((await self.client.post(path + "/mackerel-safety", headers=headers,
                         json={**body, "expected_version": 12})).status_code, 409)
        quality = {"expected_version": 12, "expected_current_version": 0, "kind": "INITIAL",
                   "scores": {key: 4 for key in cooking.SCORE_ITEMS},
                   "observations": {key: "합성 관찰 근거" for key in cooking.SCORE_ITEMS},
                   "quality_decision": "QUALITY_PASSED", "planned_portions": 3, "actual_portions": 3}
        blocked = await self.client.post(path + "/quality/rice", headers=headers, json=quality)
        self.assertEqual(blocked.status_code, 409)
        invalid = {**quality, "scores": {**quality["scores"], "texture": 2}}
        self.assertEqual((await self.client.post(path + "/quality/mackerel", headers=headers,
                                                json=invalid)).status_code, 422)
        initial = await self.client.post(path + "/quality/mackerel", headers=headers, json=quality)
        self.assertEqual(initial.status_code, 201, initial.text)
        self.assertEqual((initial.json()["assessment"]["score_percent"],
                          initial.json()["assessment"]["quality_target"]), ("80.00", True))
        self.assertEqual(initial.json()["assessment"]["actual_mass_kg"], "0.39")
        self.assertEqual((await self.client.post(path + "/quality/mackerel", headers=headers,
                         json={**quality, "expected_version": 13})).status_code, 409)
        revised = await self.client.post(path + "/quality/mackerel", headers=headers, json={
            **quality, "expected_version": 13, "expected_current_version": 1, "kind": "REASSESSMENT",
            "reason": "껍질 식감 재확인", "quality_decision": "QUALITY_FAILED",
            "quality_reason_codes": ["SKIN_NOT_CRISP"]})
        self.assertEqual(revised.status_code, 201, revised.text)
        self.assertFalse(revised.json()["assessment"]["quality_passed"])
        snapshot = (await self.client.get(path)).json()
        self.assertEqual(snapshot["mackerel"]["quality_current"]["version"], 2)
        quality_events = [event for event in snapshot["events"] if event["event"] == "MACKEREL_QUALITY_ASSESSED"]
        self.assertEqual([event["assessment"]["version"] for event in quality_events], [1, 2])

    async def test_low_temperature_failure_and_expired_probe_incomplete(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meal = created["meal"]
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal['meal_batch_id']}"
        unloaded = datetime.now(timezone.utc) - timedelta(seconds=300)
        self.store.items[(pk, "META")]["status"] = "MACKEREL_UNLOADED"
        fish = self.store.items[(pk, f"JOB#MACKEREL#{meal['mackerel_cooking_job_id']}")]
        fish.update(food_unloaded_at=unloaded.isoformat(), products=[
            {"product_id": f"fish-{n}", "rack_id": "rack-1", "shelf": n, "pan_id": f"pan-{n}",
             "mass_kg": "0.13", "thickness_cm": "1.5"} for n in (1, 2, 3)])
        probes = [{"probe_id": f"probe-{n}", "product_id": f"fish-{n}", "calibration_status": "OK",
                   "readings": [{"second": i, "celsius": "75"} for i in range(60)]} for n in (1, 2, 3)]
        probes[0]["calibration_status"] = "EXPIRED"
        body = {"expected_version": 1, "measured_at": unloaded.isoformat(), "probes": probes}
        incomplete = await self.client.post(path + "/mackerel-safety", headers=headers, json=body)
        self.assertEqual(incomplete.json()["step"]["safety_status"], "ASSESSMENT_INCOMPLETE")
        self.assertEqual(incomplete.json()["event"]["probe_summaries"][0]["status"], "ASSESSMENT_INCOMPLETE")
        self.assertIsNone(incomplete.json()["event"]["probe_summaries"][0]["longest_75c_hold_seconds"])
        probes[0]["calibration_status"] = "OK"
        probes[1]["readings"][23]["celsius"] = "74.9"
        failed = await self.client.post(path + "/mackerel-safety", headers=headers,
                                        json={**body, "expected_version": 2, "probes": probes})
        self.assertEqual(failed.json()["step"]["safety_status"], "FAILED")
        failed_probe = failed.json()["event"]["probe_summaries"][1]
        self.assertEqual((failed_probe["status"], failed_probe["full_interval_min_celsius"],
                          failed_probe["longest_75c_hold_seconds"]), ("FAILED", "74.9", 36))
        self.assertEqual(failed.json()["meal"]["status"], "MACKEREL_SAFETY_FAILED")
        reheat = {"expected_version": 3, "started_at": (unloaded + timedelta(seconds=61)).isoformat(),
                  "unloaded_at": (unloaded + timedelta(seconds=120)).isoformat(), "reason": "안전 실패 후 추가 가열"}
        invalid = await self.client.post(path + "/mackerel-reheat", headers=headers,
                                         json={**reheat, "started_at": unloaded.isoformat()})
        self.assertEqual(invalid.status_code, 422)
        created_segment = await self.client.post(path + "/mackerel-reheat", headers=headers, json=reheat)
        self.assertEqual(created_segment.status_code, 201, created_segment.text)
        self.assertEqual(created_segment.json()["step"]["safety_status"], "RECHECK_REQUIRED")
        self.assertEqual(created_segment.json()["step"]["safety_probe_summaries"], [])
        self.assertEqual(created_segment.json()["step"]["latest_reheat"]["previous_safety_version"], 2)
        self.assertEqual((await self.client.post(path + "/mackerel-reheat", headers=headers, json=reheat)).status_code, 409)
        probes[1]["readings"][23]["celsius"] = "75"
        previous_time = await self.client.post(path + "/mackerel-safety", headers=headers,
            json={"expected_version": 4, "measured_at": unloaded.isoformat(), "probes": probes})
        self.assertEqual(previous_time.status_code, 422)
        rechecked = await self.client.post(path + "/mackerel-safety", headers=headers,
            json={"expected_version": 4, "measured_at": reheat["unloaded_at"], "probes": probes})
        self.assertEqual(rechecked.status_code, 200, rechecked.text)
        self.assertEqual((rechecked.json()["step"]["safety_status"], rechecked.json()["step"]["safety_version"]),
                         ("PASSED", 3))
        snapshot = (await self.client.get(path)).json()
        events = [event for event in snapshot["events"] if event["event"] in ("MACKEREL_CORE_CHECK", "MACKEREL_REHEATED")]
        self.assertEqual([event["event"] for event in events],
                         ["MACKEREL_CORE_CHECK", "MACKEREL_CORE_CHECK", "MACKEREL_REHEATED", "MACKEREL_CORE_CHECK"])
        self.assertEqual(events[1]["safety_status"], "FAILED")
        self.assertEqual(events[1]["probe_summaries"][1]["full_interval_min_celsius"], "74.9")
        self.assertEqual(events[3]["probe_summaries"][1]["longest_75c_hold_seconds"], 60)
        self.assertEqual(events[1]["safety_version"], events[2]["segment"]["previous_safety_version"])
        self.assertEqual(events[3]["reheat_segment_id"], events[2]["segment"]["segment_id"])

    async def test_sampling_coverage_is_required_for_safe_result(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meal = created["meal"]
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal['meal_batch_id']}"
        unloaded = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.store.items[(pk, "META")]["status"] = "MACKEREL_UNLOADED"
        fish = self.store.items[(pk, f"JOB#MACKEREL#{meal['mackerel_cooking_job_id']}")]
        fish.update(food_unloaded_at=unloaded.isoformat(), products=[
            {"product_id": f"fish-{n}", "rack_id": "rack-1", "shelf": 1, "pan_id": "pan-1",
             "mass_kg": "0.13", "thickness_cm": "1.5"} for n in (1, 2, 3)])
        probes = [{"probe_id": f"probe-{n}", "product_id": f"fish-{n}", "calibration_status": "OK",
                   "readings": [{"second": i, "celsius": "75"} for i in range(60)]} for n in (1, 2, 3)]
        result = await self.client.post(path + "/mackerel-safety", headers=headers,
            json={"expected_version": 1, "measured_at": unloaded.isoformat(), "probes": probes})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["step"]["safety_status"], "ASSESSMENT_INCOMPLETE")
        self.assertIn("SINGLE_SHELF_POSITIONS_INCOMPLETE", result.json()["event"]["selection_issues"])

    async def test_two_shelf_inventory_selects_three_of_four_products(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meal = created["meal"]
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal['meal_batch_id']}"
        self.store.items[(pk, "META")]["status"] = "MACKEREL_READY"
        transition = self.store.items[(pk, f"TRANSITION#{meal['mode_transition_id']}")]
        transition.update(status="DRY_HEAT_READY", mackerel_load_allowed=True)
        products = [{"product_id": f"fish-{n}", "rack_id": "rack-1", "shelf": 1 if n < 3 else 2,
                     "pan_id": "lower" if n < 3 else "upper", "position": "LEFT" if n % 2 else "RIGHT",
                     "mass_kg": "0.13", "thickness_cm": "2.0" if n == 4 else "1.5"} for n in range(1, 5)]
        start = datetime.now(timezone.utc) - timedelta(seconds=1000)
        added = await self.client.post(path + "/events", headers=headers, json={"expected_version": 1,
            "event": "MACKEREL_STARTED", "observed_at": start.isoformat(), "products": products})
        self.assertEqual(added.status_code, 200, added.text)
        for version, event, offset in ((2, "MACKEREL_PROGRAM_COMPLETED", 720), (3, "MACKEREL_UNLOADED", 720)):
            result = await self.client.post(path + "/events", headers=headers, json={"expected_version": version,
                "event": event, "observed_at": (start + timedelta(seconds=offset)).isoformat()})
            self.assertEqual(result.status_code, 200, result.text)
        probes = [{"probe_id": f"probe-{n}", "product_id": f"fish-{n}", "calibration_status": "OK",
                   "readings": [{"second": i, "celsius": "75"} for i in range(60)]} for n in (1, 3, 4)]
        result = await self.client.post(path + "/mackerel-safety", headers=headers, json={"expected_version": 4,
            "measured_at": (start + timedelta(seconds=720)).isoformat(), "probes": probes})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["step"]["safety_status"], "PASSED")
        self.assertEqual(result.json()["event"]["selection_issues"], [])
