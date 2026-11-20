"""C# synthetic source attaches to the authenticated customer's combi meal."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
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
