"""C# synthetic combi observations feed the approved platform rules."""

import json
import shutil
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from foodtech_contracts.combi_mackerel_process import Sample as FishSample, assess as assess_fish
from foodtech_contracts.combi_rice_process import Sample as RiceSample, assess as assess_rice
from foodtech_contracts.combi_transition import TransitionSample, assess_transition


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/combi-normal-medium-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class CombiMockTests(unittest.TestCase):
    def test_failure_fixtures_produce_approved_platform_findings(self):
        for name in ("rice-low", "transition-timeout", "mackerel-low"):
            with self.subTest(scenario=name), tempfile.TemporaryDirectory() as directory:
                raw_path = Path(directory) / "raw.jsonl"
                aggregate_path = Path(directory) / "aggregate.jsonl"
                subprocess.run(["dotnet", str(DLL), "simulate-combi", "--fixture",
                                str(ROOT / f"fixtures/combi-{name}-v1.json"),
                                "--output", str(raw_path), "--aggregates", str(aggregate_path)],
                               capture_output=True, check=True, text=True)
                raw = [json.loads(line, parse_float=Decimal) for line in raw_path.read_text().splitlines()]
                self.assertTrue(all(item["provenance"] == "SYNTHETIC" for item in raw))
                self.assertEqual([item["sequence"] for item in raw], list(range(1, len(raw) + 1)))
                if name == "rice-low":
                    rows = [item for item in raw if item["phase"] == "RICE"]
                    result = assess_rice([RiceSample(item["phaseSecond"], Decimal(item["chamberCelsius"]),
                        Decimal(item["measuredHumidityPercent"]), item["doorState"]) for item in rows],
                        len(rows), "NORMAL", "MEASURED")
                    self.assertEqual(result["status"], "FAILED")
                    self.assertIn("LOW_TEMPERATURE_PROCESS_FAILURE", result["reason_codes"])
                elif name == "transition-timeout":
                    rows = [item for item in raw if item["phase"] == "TRANSITION"]
                    result = assess_transition([TransitionSample(item["phaseSecond"], Decimal(item["chamberCelsius"]),
                        Decimal(item["climateSetpointPercent"]), item["doorState"] == "CLOSED",
                        item["criticalAlarm"], None) for item in rows], humidity_supported=False, vent_supported=False)
                    self.assertEqual(result["status"], "MODE_TRANSITION_TIMEOUT")
                    self.assertFalse(result["mackerel_load_allowed"])
                else:
                    rows = [item for item in raw if item["phase"] == "MACKEREL"]
                    result = assess_fish([FishSample(item["phaseSecond"], Decimal(item["chamberCelsius"]),
                        item["doorState"]) for item in rows], len(rows), "NORMAL")
                    self.assertEqual(result["status"], "FAILED")
                    self.assertIn("LOW_TEMPERATURE_PROCESS_FAILURE", result["reason_codes"])

    def test_three_phases_and_five_second_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_path = Path(directory) / "raw.jsonl"
            aggregate_path = Path(directory) / "aggregate.jsonl"
            completed = subprocess.run(["dotnet", str(DLL), "simulate-combi", "--fixture", str(FIXTURE),
                                        "--output", str(raw_path), "--aggregates", str(aggregate_path)],
                                       capture_output=True, check=True, text=True)
            raw = [json.loads(line, parse_float=Decimal) for line in raw_path.read_text().splitlines()]
            aggregates = [json.loads(line) for line in aggregate_path.read_text().splitlines()]
        self.assertEqual((len(raw), len(aggregates)), (2700, 540))
        self.assertEqual(json.loads(completed.stdout)["seed"], 2026)
        self.assertEqual([item["sequence"] for item in raw], list(range(1, 2701)))
        self.assertEqual(len({item["eventId"] for item in raw}), len(raw))
        self.assertEqual(len({item["mealBatchId"] for item in raw}), 1)
        self.assertEqual(len({item["cookingJobId"] for item in raw}), 3)
        self.assertTrue(all(item["provenance"] == "SYNTHETIC" for item in raw))
        rice = [item for item in raw if item["phase"] == "RICE"]
        transition = [item for item in raw if item["phase"] == "TRANSITION"]
        fish = [item for item in raw if item["phase"] == "MACKEREL"]
        self.assertEqual((len(rice), len(transition), len(fish)), (1800, 180, 720))
        self.assertTrue(all(item["lastSequence"] - item["firstSequence"] == 4 and
                            item["outsideTargetSeconds"] == 0 for item in aggregates))
        rice_result = assess_rice([RiceSample(item["phaseSecond"], Decimal(item["chamberCelsius"]),
                                             Decimal(item["measuredHumidityPercent"]), item["doorState"]) for item in rice],
                                  1800, "NORMAL", "MEASURED")
        self.assertEqual(rice_result["status"], "NORMAL")
        transition_result = assess_transition([TransitionSample(item["phaseSecond"],
            Decimal(item["chamberCelsius"]), Decimal(item["climateSetpointPercent"]),
            item["doorState"] == "CLOSED", False, None) for item in transition],
            humidity_supported=False, vent_supported=False)
        self.assertEqual(transition_result["ready_at_second"], 180)
        fish_result = assess_fish([FishSample(item["phaseSecond"], Decimal(item["chamberCelsius"]),
                                              item["doorState"]) for item in fish], 720, "NORMAL")
        self.assertEqual(fish_result["status"], "NORMAL")

    def test_invalid_fixture_produces_no_output(self):
        fixture = json.loads(FIXTURE.read_text())
        fixture["phases"][1]["cookingJobId"] = fixture["phases"][0]["cookingJobId"]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.json"
            raw = Path(directory) / "raw.jsonl"
            aggregates = Path(directory) / "aggregate.jsonl"
            source.write_text(json.dumps(fixture))
            result = subprocess.run(["dotnet", str(DLL), "simulate-combi", "--fixture", str(source),
                                     "--output", str(raw), "--aggregates", str(aggregates)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(raw.exists())
            self.assertFalse(aggregates.exists())
