"""Opt-in C# Jeyuk Mock -> DynamoDB -> FIFO -> Worker validation.

Uses existing development resources and removes one unique synthetic fixture.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import boto3

from foodtech_collection_adapter.jeyuk_aws import DynamoJeyukIngestor
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker

from mock_quality_dynamodb_check import empty_queue
from process_flow_dynamodb_check import (ACCOUNT, DATA_TABLE, ORDERED_QUEUE, QUARANTINE_QUEUE,
                                         REGION, remove_partition, remove_test_messages)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-process-v1.json"
MOCK_DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


def csharp_samples(fixture_id: str, fixture_path: Path = FIXTURE) -> list[str]:
    dotnet = shutil.which("dotnet")
    if not dotnet or not MOCK_DLL.is_file():
        raise RuntimeError("Build the .NET 10 Mock in Release mode and add dotnet to PATH.")
    with tempfile.TemporaryDirectory(prefix="foodtech-jeyuk-live-") as directory:
        fixture = json.loads(fixture_path.read_text())
        fixture["fixtureId"] = fixture_id
        source, output = (Path(directory) / name for name in ("fixture.json", "samples.jsonl"))
        source.write_text(json.dumps(fixture), encoding="utf-8")
        subprocess.run([dotnet, str(MOCK_DLL), "simulate-jeyuk", "--fixture", str(source),
                        "--output", str(output)], check=True, capture_output=True)
        return output.read_text().splitlines()


def main() -> int:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    sqs = session.client("sqs")
    ordered_url = sqs.get_queue_url(QueueName=ORDERED_QUEUE)["QueueUrl"]
    quarantine_url = sqs.get_queue_url(QueueName=QUARANTINE_QUEUE)["QueueUrl"]
    if not empty_queue(sqs, ordered_url) or not empty_queue(sqs, quarantine_url):
        raise SystemExit("Shared development queues must be empty.")
    fixture_id = f"jeyuk-live-{uuid.uuid4().hex[:12]}"
    stream_pk = f"JEYUK#{fixture_id}"
    table = session.resource("dynamodb").Table(DATA_TABLE)
    ingestor = DynamoJeyukIngestor(DATA_TABLE, ordered_url)
    worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)
    processed = []
    try:
        raws = csharp_samples(fixture_id)
        for raw in raws:
            ingestor.ingest(raw)
        for _ in range(8):
            page = sqs.receive_message(QueueUrl=ordered_url, MaxNumberOfMessages=10,
                                       WaitTimeSeconds=1, VisibilityTimeout=30)
            for message in page.get("Messages", []):
                reference = json.loads(message["Body"])
                if reference.get("pk") != stream_pk:
                    sqs.change_message_visibility(QueueUrl=ordered_url,
                                                  ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
                    raise RuntimeError("unexpected message in shared development ordered queue")
                try:
                    processed.append(worker.process(reference))
                except Exception:
                    sqs.change_message_visibility(QueueUrl=ordered_url,
                                                  ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
                    raise
                sqs.delete_message(QueueUrl=ordered_url, ReceiptHandle=message["ReceiptHandle"])
            if len(processed) == len(raws):
                break
        if len(processed) != 30 or [item["second"] for item in processed] != list(range(1, 31)):
            raise AssertionError("Jeyuk FIFO samples were not processed in order")
        if processed[4]["process_status"] != "WARNING" or \
                "AGITATOR_JAM_WARNING" not in processed[4]["reason_codes"]:
            raise AssertionError("five-second jam warning was not stored")
        if processed[29]["process_status"] != "FAILED" or \
                "AGITATOR_FAILURE" not in processed[29]["reason_codes"]:
            raise AssertionError("thirty-second agitator failure was not stored")
        state = table.get_item(Key={"pk": stream_pk, "sk": "STATE"}, ConsistentRead=True)["Item"]
        if int(state["last_second"]) != 30:
            raise AssertionError("Jeyuk monitor pointer did not reach second 30")
        if not worker.process({"profile_id": "mockprofile-110", "pk": stream_pk,
                               "raw_sk": "RAW#000030", "fixture_id": fixture_id,
                               "second": 30})["replayed"]:
            raise AssertionError("FIFO replay did not reuse the stored assessment")
        print(json.dumps({"processed": len(processed), "warning_at": 5, "failure_at": 30,
                          "replay": "REPLAYED", "state_second": int(state["last_second"])}), flush=True)
        return 0
    finally:
        errors = []
        try:
            remove_test_messages(sqs, ordered_url, stream_pk)
        except Exception as exc:
            errors.append(f"queue cleanup: {exc}")
        try:
            remove_partition(table, stream_pk)
        except Exception as exc:
            errors.append(f"table cleanup: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    sys.exit(main())
