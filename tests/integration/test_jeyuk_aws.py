"""DynamoDB and FIFO contracts for the C# synthetic Jeyuk process stream."""

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer

from foodtech_collection_adapter.jeyuk_aws import DynamoJeyukIngestor
from foodtech_standardization_worker.jeyuk_aws import DynamoJeyukProcessor
from foodtech_business_api.jeyuk_source_evidence import audit_source_records
from foodtech_contracts.jeyuk_process import RULE_VERSION, cooking_time_status


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-process-v1.json"
SAFETY_FIXTURE = ROOT / "fixtures/jeyuk-small-safety-v1.json"
DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


class ConditionalFailed(Exception):
    pass


class TransactionCanceled(Exception):
    pass


class FakeTable:
    name = "test-data"

    def __init__(self):
        self.items = {}
        self.exceptions = SimpleNamespace(ConditionalCheckFailedException=ConditionalFailed,
                                          TransactionCanceledException=TransactionCanceled)
        self.meta = SimpleNamespace(client=self)

    def transact_write_items(self, *, TransactItems):
        put, update = TransactItems[0]["Put"], TransactItems[1]["Update"]
        item = put["Item"]
        key = (item["pk"], item["sk"])
        if key in self.items:
            raise TransactionCanceled()
        revision = update["Key"]
        revision_key = (revision["pk"], revision["sk"])
        previous = self.items.get(revision_key, {}).get("revision", 0)
        self.items[key] = item
        self.items[revision_key] = {**revision, "revision": previous + 1}

    def get_item(self, *, Key, ConsistentRead):
        assert ConsistentRead
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, *, Item, ConditionExpression):
        assert ConditionExpression is not None
        key = (Item["pk"], Item["sk"])
        if key in self.items:
            raise ConditionalFailed()
        self.items[key] = Item


class FakeSqs:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeDdb:
    exceptions = SimpleNamespace(TransactionCanceledException=TransactionCanceled)

    def __init__(self, table):
        self.table = table
        self.decoder = TypeDeserializer()

    def transact_write_items(self, *, TransactItems):
        writes = []
        for operation in TransactItems:
            put = operation["Put"]
            item = {name: self.decoder.deserialize(value) for name, value in put["Item"].items()}
            key = (item["pk"], item["sk"])
            current = self.table.items.get(key)
            if put["ConditionExpression"] == "attribute_not_exists(pk)":
                allowed = current is None
            else:
                prior = self.decoder.deserialize(put["ExpressionAttributeValues"][":prior"])
                allowed = current is not None and current["last_second"] == prior
            if not allowed:
                raise TransactionCanceled()
            writes.append((key, item))
        for key, item in writes:
            self.table.items[key] = item


@unittest.skipUnless(shutil.which("dotnet") and DLL.exists(), "built .NET 10 Mock required")
class JeyukAwsTests(unittest.TestCase):
    def test_full_medium_and_large_streams_persist_every_sample_and_safety_state(self):
        for size, seconds in (("medium", 900), ("large", 1500)):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "samples.jsonl"
                fixture = ROOT / f"fixtures/jeyuk-{size}-full-v1.json"
                bound = Path(directory) / "bound.json"
                start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                subprocess.run(["dotnet", str(DLL), "bind-jeyuk", "--fixture", str(fixture), "--output", str(bound),
                                "--fixture-id", f"jeyuk-{size}-full-v1", "--start-at", start.isoformat()],
                               check=True, capture_output=True)
                subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(bound),
                                "--output", str(output)], check=True, capture_output=True)
                raws = output.read_text().splitlines()
                table, sqs = FakeTable(), FakeSqs()
                ingestor = DynamoJeyukIngestor.__new__(DynamoJeyukIngestor)
                ingestor.table, ingestor.sqs, ingestor.queue_url = table, sqs, "test.fifo"
                worker = DynamoJeyukProcessor(table, FakeDdb(table))
                for raw in raws:
                    result = worker.process(ingestor.ingest(raw))
                self.assertEqual(len(raws), seconds)
                self.assertEqual((result["profile"], result["second"], result["process_status"],
                                  result["auto_safety_status"]), (size.upper(), seconds, "NORMAL", "PASSED"))
                self.assertEqual(len(sqs.messages), seconds)
                pk = f"JEYUK#jeyuk-{size}-full-v1"
                self.assertEqual(table.items[(pk, "STATE")]["last_second"], seconds)
                self.assertEqual(table.items[(pk, "SOURCE#REVISION")]["revision"], seconds)
                source = json.loads(fixture.read_text())
                identity = source["validationIdentity"]
                context = {"actual_load_kg": source["actualLoadKg"],
                    "ingredient_spec_id": identity["ingredientSpecId"], "ingredient_lot_id": identity["ingredientLotId"],
                    "supplier_id": identity["supplierId"], "sensor_id": identity["sensorId"],
                    "sensor_calibration_record_id": identity["sensorCalibrationRecordId"],
                    "process_condition_profile_id": identity["processConditionProfileId"],
                    "process_condition_version": identity["processConditionVersion"]}
                job = {"source_binding": {"batch_id": source["fixtureId"]}, "jeyuk_size_profile": size.upper(),
                    "equipment_profile": f"jeyuk-{size}", "validation_context": context,
                    "process_assessment": {"second": seconds, "source_assessment_sk": f"ASSESSMENT#{seconds:06d}",
                        "status": result["process_status"], "rule_version": RULE_VERSION},
                    "auto_safety_status": result["auto_safety_status"],
                    "mixture_hold_seconds": result["mixture_hold_seconds"],
                    "safety_passed_at_second": result["safety_passed_at_second"],
                    "actual_cooking_seconds": seconds, "process_time_status": cooking_time_status(size.upper(), seconds),
                    "actual_placed_at": start.isoformat(),
                    "actual_lifted_at": (start + timedelta(seconds=seconds)).isoformat(),
                    "process_assessment_status": result["process_status"]}
                assessments = [table.items[(pk, f"ASSESSMENT#{index:06d}")] for index in range(1, seconds + 1)]
                recorded_raw = [table.items[(pk, f"RAW#{index:06d}")] for index in range(1, seconds + 1)]
                checks = audit_source_records(job, assessments, recorded_raw)
                self.assertTrue(all(checks.values()))

    def test_safety_hold_is_persisted_with_process_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "safety.jsonl"
            subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(SAFETY_FIXTURE),
                            "--output", str(output)], check=True, capture_output=True)
            raws = output.read_text().splitlines()
        table = FakeTable()
        sqs = FakeSqs()
        ingestor = DynamoJeyukIngestor.__new__(DynamoJeyukIngestor)
        ingestor.table, ingestor.sqs, ingestor.queue_url = table, sqs, "test.fifo"
        worker = DynamoJeyukProcessor(table, FakeDdb(table))
        results = [worker.process(ingestor.ingest(raw)) for raw in raws]
        self.assertEqual((results[59]["auto_safety_status"], results[59]["mixture_hold_seconds"]),
                         ("PENDING", 59))
        self.assertEqual((results[60]["auto_safety_status"], results[60]["mixture_hold_seconds"],
                          results[60]["safety_passed_at_second"]), ("PASSED", 60, 61))
        state = table.items[("JEYUK#jeyuk-small-safety-v1", "STATE")]
        self.assertEqual(state["safety_state"]["passed_at_second"], 61)

    def test_raw_fifo_state_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "samples.jsonl"
            subprocess.run(["dotnet", str(DLL), "simulate-jeyuk", "--fixture", str(FIXTURE),
                            "--output", str(output)], check=True, capture_output=True)
            raws = output.read_text().splitlines()
        table = FakeTable()
        sqs = FakeSqs()
        ingestor = DynamoJeyukIngestor.__new__(DynamoJeyukIngestor)
        ingestor.table = table
        ingestor.sqs = sqs
        ingestor.queue_url = "test.fifo"
        worker = DynamoJeyukProcessor(table, FakeDdb(table))

        for raw in raws:
            ingestor.ingest(raw)
        self.assertEqual(len(sqs.messages), 30)
        self.assertEqual(len({message["MessageDeduplicationId"] for message in sqs.messages}), 30)
        self.assertEqual({message["MessageGroupId"] for message in sqs.messages},
                         {"jeyuk-small-process-v1"})
        references = [json.loads(message["MessageBody"]) for message in sqs.messages]
        with self.assertRaisesRegex(ValueError, "out of order"):
            worker.process(references[1])
        results = [worker.process(reference) for reference in references]
        self.assertEqual(results[4]["process_status"], "WARNING")
        self.assertIn("AGITATOR_JAM_WARNING", results[4]["reason_codes"])
        self.assertEqual(results[29]["process_status"], "FAILED")
        self.assertIn("AGITATOR_FAILURE", results[29]["reason_codes"])
        self.assertEqual(table.items[("JEYUK#jeyuk-small-process-v1", "STATE")]["last_second"], 30)
        self.assertEqual(len([key for key in table.items if key[1].startswith("ASSESSMENT#")]), 30)
        self.assertTrue(worker.process(references[29])["replayed"])
        with self.assertRaisesRegex(ValueError, "changed"):
            ingestor.ingest(raws[0].replace('"actualRpm":20', '"actualRpm":21'))


if __name__ == "__main__":
    unittest.main()
