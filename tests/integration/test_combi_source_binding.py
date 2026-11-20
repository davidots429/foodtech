"""C# synthetic source attaches to the authenticated customer's combi meal."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/combi-normal-medium-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class CombiSourceBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_csharp_source_attaches_only_to_matching_meal(self):
        async def session(_request, _surface):
            return Session("customer", "test-customer", None, "csrf", 9999999999, 0)

        store = MemoryCookingStore()
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
             patch.object(cooking, "_session", side_effect=session), \
             patch.object(cooking, "get_cooking_store", return_value=store):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                headers = {"X-CSRF-Token": "csrf"}
                response = await client.post("/api/customer/combi-meals", headers=headers, json={
                    "equipment_size": "MEDIUM_FIXED_RACK_10", "rack_count": 1, "pan_count": 10,
                    "dry_rice_kg": "20", "drained_rice_kg": "24", "added_water_kg": "20",
                    "pan_tare_kg": "10", "rack_tare_kg": "0"})
                self.assertEqual(response.status_code, 201, response.text)
                meal = response.json()["meal"]
                fixture = json.loads(FIXTURE.read_text())
                fixture["mealBatchId"] = meal["meal_batch_id"]
                for phase, key in zip(fixture["phases"],
                                      ("rice_cooking_job_id", "mode_transition_id", "mackerel_cooking_job_id")):
                    phase["cookingJobId"] = meal[key]
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "fixture.json"
                    output = Path(directory) / "raw.jsonl"
                    aggregate = Path(directory) / "aggregate.jsonl"
                    source.write_text(json.dumps(fixture))
                    subprocess.run(["dotnet", str(DLL), "simulate-combi", "--fixture", str(source),
                                    "--output", str(output), "--aggregates", str(aggregate)],
                                   check=True, capture_output=True)
                    records = output.read_text().splitlines()
                path = "/api/customer/combi-meals/" + meal["meal_batch_id"]
                imported = await client.post(path + "/source-observations", headers=headers, json={
                    "expected_version": 1, "device_id": fixture["deviceId"], "records": records})
                self.assertEqual(imported.status_code, 201, imported.text)
                self.assertEqual(imported.json()["meal"]["source"]["phase_counts"],
                                 {"RICE": 1800, "TRANSITION": 180, "MACKEREL": 720})
                first = (await client.get(path + "/source-observations")).json()
                self.assertEqual((first["records"][0], first["next_cursor"]), (records[0], 1))
                last = (await client.get(path + "/source-observations?cursor=26")).json()
                self.assertEqual((last["records"][-1], last["next_cursor"]), (records[-1], None))
                started_at = datetime.fromisoformat(json.loads(records[0])["occurredAt"])
                for version, event, offset in ((2, "RICE_STARTED", 0), (3, "RICE_PROGRAM_COMPLETED", 1800),
                                               (4, "RICE_UNLOADED", 1800)):
                    step = await client.post(path + "/events", headers=headers, json={
                        "expected_version": version, "event": event,
                        "observed_at": (started_at + timedelta(seconds=offset)).isoformat()})
                    self.assertEqual(step.status_code, 200, step.text)
                source_pk = f"SCOPE#USER#test-customer#COMBI_MEAL#{meal['meal_batch_id']}#SOURCE"
                chunk = store.items[(source_pk, "RAW#0000")]
                original = chunk["records"][0]
                changed = json.loads(original)
                changed["chamberCelsius"] = 101
                chunk["records"][0] = json.dumps(changed)
                rejected = await client.post(path + "/source-assessment/rice", headers=headers,
                                             json={"expected_version": 5})
                self.assertEqual(rejected.status_code, 422, rejected.text)
                chunk["records"][0] = original
                assessment = await client.post(path + "/source-assessment/rice", headers=headers,
                                               json={"expected_version": 5})
                self.assertEqual(assessment.status_code, 200, assessment.text)
                self.assertEqual(assessment.json()["step"]["monitored_process_assessment"]["status"], "NORMAL")
                self.assertEqual(assessment.json()["step"]["monitored_process_source"]["raw_sha256"],
                                 imported.json()["meal"]["source"]["raw_sha256"])
                transition_started = await client.post(path + "/events", headers=headers, json={
                    "expected_version": 6, "event": "MODE_TRANSITION_STARTED",
                    "observed_at": (started_at + timedelta(seconds=1800)).isoformat()})
                self.assertEqual(transition_started.status_code, 200, transition_started.text)
                # The source contract does not yet include critical alarm or vent capability.
                # Explicit separate synthetic observations are used for the transition gate.
                transition_samples = [{"second": i, "chamber_celsius": "220",
                                       "climate_setpoint_percent": "10", "door_closed": True,
                                       "critical_alarm": False, "humidity_percent": None}
                                      for i in range(1, 181)]
                ready = await client.post(path + "/transition-evidence", headers=headers, json={
                    "expected_version": 7, "humidity_supported": False, "vent_supported": False,
                    "samples": transition_samples})
                self.assertEqual(ready.status_code, 200, ready.text)
                products = [{"product_id": f"fish-{n}", "rack_id": "rack-1", "shelf": n,
                             "pan_id": f"pan-{n}", "mass_kg": "0.13", "thickness_cm": "1.5"}
                            for n in (1, 2, 3)]
                for version, event, offset in ((8, "MACKEREL_STARTED", 1980),
                                               (9, "MACKEREL_PROGRAM_COMPLETED", 2700),
                                               (10, "MACKEREL_UNLOADED", 2700)):
                    step = await client.post(path + "/events", headers=headers, json={
                        "expected_version": version, "event": event,
                        "observed_at": (started_at + timedelta(seconds=offset)).isoformat(),
                        **({"products": products} if event == "MACKEREL_STARTED" else {})})
                    self.assertEqual(step.status_code, 200, step.text)
                fish = await client.post(path + "/source-assessment/mackerel", headers=headers,
                                         json={"expected_version": 11})
                self.assertEqual(fish.status_code, 200, fish.text)
                self.assertEqual(fish.json()["step"]["monitored_process_assessment"]["status"], "NORMAL")
                self.assertEqual(fish.json()["step"]["safety_status"], "NOT_MEASURED")
