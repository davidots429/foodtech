import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from foodtech_business_api import cooking
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
        self.subject = "customer-two"
        self.assertEqual((await send("RICE_STARTED", 5, start)).status_code, 404)

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
        process_samples = [{"second": i, "chamber_celsius": "220", "door": "CLOSED"}
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

    async def test_low_temperature_failure_and_expired_probe_incomplete(self):
        headers = {"X-CSRF-Token": "csrf"}
        created = (await self.client.post("/api/customer/combi-meals", json=self.body, headers=headers)).json()
        meal = created["meal"]
        path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
        pk = f"SCOPE#USER#{self.subject}#COMBI_MEAL#{meal['meal_batch_id']}"
        unloaded = datetime.now(timezone.utc) - timedelta(seconds=120)
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
        probes[0]["calibration_status"] = "OK"
        probes[1]["readings"][23]["celsius"] = "74.9"
        failed = await self.client.post(path + "/mackerel-safety", headers=headers,
                                        json={**body, "expected_version": 2, "probes": probes})
        self.assertEqual(failed.json()["step"]["safety_status"], "FAILED")
        self.assertEqual(failed.json()["meal"]["status"], "MACKEREL_SAFETY_FAILED")
