"""C# synthetic probes are assessed by the existing customer safety API."""
import json
import copy
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx

from foodtech_business_api import cooking
from foodtech_business_api.cooking_store import MemoryCookingStore
from foodtech_business_api.main import app

ROOT = Path(__file__).resolve().parents[2]
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"
FIXTURE = ROOT / "fixtures/tonkatsu-probes-normal-v1.json"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class TonkatsuProbeMockTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_products_and_faults_reach_platform_assessment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for scenario, expected in (("NORMAL", "PASSED"), ("LOW_CORE", "FAILED"),
                                       ("MISSING_SAMPLE", "INCOMPLETE"), ("EXPIRED_CALIBRATION", "INCOMPLETE")):
                with self.subTest(scenario=scenario):
                    fixture = json.loads(FIXTURE.read_text())
                    fixture.update(scenario=scenario, affectedProduct=2)
                    source, output = root / f"{scenario}.json", root / f"{scenario}-out.json"
                    source.write_text(json.dumps(fixture))
                    command = ["dotnet", str(DLL), "simulate-tonkatsu-probes", "--fixture", str(source),
                               "--output", str(output)]
                    subprocess.run(command, check=True, capture_output=True)
                    generated = json.loads(output.read_text())
                    evidence = {key: generated[key] for key in ("schema_version", "provenance", "generator_version",
                        "fixture_version", "seed", "scenario", "measurement_start_utc", "measurement_end_utc")}
                    self.assertEqual(generated["provenance"], "SYNTHETIC")
                    self.assertEqual(len(generated["probes"]), 4)
                    self.assertEqual(len({p["probe_id"] for p in generated["probe_manifest"]}), 4)
                    self.assertEqual(len(generated["probes"][0]["samples"]), 61)
                    before = output.read_bytes()
                    subprocess.run(command, check=True, capture_output=True)
                    self.assertEqual(output.read_bytes(), before)
                    job_id, subject = str(uuid4()), "synthetic-probe-customer"
                    store = MemoryCookingStore()
                    pk = cooking._pk(subject, job_id)
                    store.items[(pk, "META")] = {"job_id": job_id, "menu": "TONKATSU", "status": "COMPLETED",
                        "version": 7, "safety_version": 0, "safety_status": "NOT_MEASURED",
                        "planned_portions": 4, "equipment_profile": "synthetic-fryer"}
                    async def session(_request, _surface): return SimpleNamespace(subject=subject, csrf="token")
                    with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false"}), \
                         patch.object(cooking, "_session", side_effect=session), \
                         patch.object(cooking, "get_cooking_store", return_value=store):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                            path = f"/api/customer/cooking-jobs/{job_id}/safety/core-check"
                            if scenario == "NORMAL":
                                invalid = copy.deepcopy(generated["probes"])
                                invalid[1]["probe_id"] = invalid[0]["probe_id"]
                                rejected = await client.post(path, headers={"X-CSRF-Token": "token"},
                                    json={"expected_version": 7, "probes": invalid})
                                self.assertEqual(rejected.status_code, 422)
                                invalid = copy.deepcopy(generated["probes"])
                                invalid[0]["measurement_end"] = "2026-11-14T00:00:59Z"
                                rejected = await client.post(path, headers={"X-CSRF-Token": "token"},
                                    json={"expected_version": 7, "probes": invalid})
                                self.assertEqual(rejected.status_code, 422)
                            response = await client.post(path,
                                headers={"X-CSRF-Token": "token"},
                                json={"expected_version": 7, "probes": generated["probes"], "mock_evidence": evidence})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["safety_status"], expected)
                    self.assertEqual(response.json()["core_check"][1]["probe_id"], "synthetic-probe-02")
                    self.assertEqual(response.json()["core_check"][1]["removal_sequence"], 2)
                    self.assertEqual(response.json()["core_check_mock_evidence"]["seed"], 100)
                    summary = response.json()["core_check_summary"][1]
                    self.assertEqual(summary["status"], expected)
                    if scenario == "NORMAL": self.assertEqual(summary["hold_seconds"], 60)
                    if scenario == "LOW_CORE": self.assertEqual(summary["minimum_celsius"], "74")
                    if scenario == "EXPIRED_CALIBRATION": self.assertIsNone(summary["hold_seconds"])
                    self.assertEqual(response.json()["evaluation_blocked"], expected != "PASSED")

    async def test_invalid_fixture_does_not_produce_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = json.loads(FIXTURE.read_text())
            fixture["productCount"] = 19
            source, output = root / "invalid.json", root / "out.json"
            source.write_text(json.dumps(fixture))
            result = subprocess.run(["dotnet", str(DLL), "simulate-tonkatsu-probes", "--fixture", str(source),
                                     "--output", str(output)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
