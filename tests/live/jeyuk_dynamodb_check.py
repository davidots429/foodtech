"""Opt-in C# Jeyuk Mock -> DynamoDB -> FIFO -> Worker validation.

Uses existing development resources and removes one unique synthetic fixture.
"""

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
import httpx

from foodtech_collection_adapter.jeyuk_aws import DynamoJeyukIngestor
from foodtech_contracts.jeyuk_receipt import matches_jeyuk_snapshot
from foodtech_business_api.jeyuk_source_evidence import read_collection_completion
from foodtech_collection_adapter import __main__ as collector
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker

from mock_quality_dynamodb_check import empty_queue
from process_flow_dynamodb_check import (ACCOUNT, DATA_TABLE, ORDERED_QUEUE, QUARANTINE_QUEUE,
                                         REGION, remove_partition, remove_test_messages)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures/jeyuk-small-process-v1.json"
MOCK_DLL = ROOT / "apps/mock-control/bin/Release/net10.0/MockControl.dll"


def csharp_samples(fixture_id: str, fixture_path: Path = FIXTURE, logical_start_at: str | None = None) -> list[str]:
    dotnet = shutil.which("dotnet")
    if not dotnet or not MOCK_DLL.is_file():
        raise RuntimeError("Build the .NET 10 Mock in Release mode and add dotnet to PATH.")
    with tempfile.TemporaryDirectory(prefix="foodtech-jeyuk-live-") as directory:
        fixture = json.loads(fixture_path.read_text())
        fixture["fixtureId"] = fixture_id
        if logical_start_at is not None:
            fixture["logicalStartAt"] = logical_start_at
        source, output = (Path(directory) / name for name in ("fixture.json", "samples.jsonl"))
        source.write_text(json.dumps(fixture), encoding="utf-8")
        subprocess.run([dotnet, str(MOCK_DLL), "simulate-jeyuk", "--fixture", str(source),
                        "--output", str(output)], check=True, capture_output=True)
        return output.read_text().splitlines()


def collect_over_https(fixture_id: str, queue_url: str, cursor_key: str) -> int:
    """Run the real collector against a local read-only C# Mock and AWS."""
    with tempfile.TemporaryDirectory(prefix="foodtech-jeyuk-wire-") as directory:
        temp = Path(directory)
        fixture = json.loads(FIXTURE.read_text())
        fixture["fixtureId"] = fixture_id
        source = temp / "fixture.json"
        source.write_text(json.dumps(fixture), encoding="utf-8")
        key, cert, pfx = (temp / name for name in ("key.pem", "cert.pem", "cert.pfx"))
        password, secret = uuid.uuid4().hex, uuid.uuid4().hex
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "pkcs12", "-export", "-out", str(pfx), "-inkey", str(key), "-in", str(cert),
                        "-password", f"pass:{password}"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        base = f"https://localhost:{port}"
        config = {"FOODTECH_MOCK_BASE_URL": base, "FOODTECH_MOCK_CA_FILE": str(cert),
                  "FOODTECH_MOCK_CLIENT_ID": "synthetic-test", "FOODTECH_MOCK_CLIENT_SECRET": secret,
                  "FOODTECH_MOCK_CERT_PASSWORD": password, "FOODTECH_DATA_TABLE": DATA_TABLE,
                  "FOODTECH_ORDERED_QUEUE_URL": queue_url}
        server = subprocess.Popen(["dotnet", str(MOCK_DLL), "serve", "--fixture", str(source),
                                   "--cert", str(pfx), "--port", str(port), "--realtime"],
                                  env={**os.environ, **config}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError("C# Mock exited before HTTPS startup")
                try:
                    if httpx.get(base + "/health", verify=str(cert), timeout=.3).status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(.1)
            else:
                raise TimeoutError("C# Mock HTTPS startup timed out")
            output = io.StringIO()
            with patch.dict(os.environ, config), patch.object(sys, "argv", ["collector", "--profile", "mockprofile-110",
                    "--cursor-key", cursor_key, "--until-complete", "--interval", "0.5", "--max-runtime", "90"]), \
                    contextlib.redirect_stdout(output):
                if collector.main() != 0:
                    raise RuntimeError("Collector failed")
            polls = output.getvalue().splitlines()
            nonempty = [line for line in polls if "received=0 " not in line]
            if len(nonempty) < 2:
                raise AssertionError("Realtime collection did not observe multiple nonempty polls")
            print(f"realtime_nonempty_polls={len(nonempty)}", flush=True)
            return DynamoJeyukIngestor(DATA_TABLE, queue_url).cursor(cursor_key)
        finally:
            server.terminate()
            server.wait(timeout=5)


def main(*, mock_wire: bool = False) -> int:
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
    cursor_key = f"wire-{uuid.uuid4().hex}"
    stream_pk = f"JEYUK#{fixture_id}"
    table = session.resource("dynamodb").Table(DATA_TABLE)
    ingestor = DynamoJeyukIngestor(DATA_TABLE, ordered_url)
    worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)
    processed = []
    try:
        if mock_wire:
            expected_count = collect_over_https(fixture_id, ordered_url, cursor_key)
            if expected_count != 30:
                raise AssertionError("Collector did not save the completed synthetic cursor")
        else:
            raws = csharp_samples(fixture_id)
            for raw in raws:
                ingestor.ingest(raw)
            expected_count = len(raws)
        if mock_wire:
            from boto3.dynamodb.conditions import Key
            rows = table.query(KeyConditionExpression=Key("pk").eq(stream_pk), ConsistentRead=True)["Items"]
            raws = sorted((row for row in rows if row["sk"].startswith("RAW#")), key=lambda row: row["sk"])
            receipts = [row for row in rows if row["sk"].startswith("COLLECTION#RECEIPT#")]
            if len(receipts) != 1 or not matches_jeyuk_snapshot(receipts[0], fixture_id, raws):
                raise AssertionError("Collected raw samples do not match the terminal receipt")
            if read_collection_completion(DATA_TABLE, fixture_id) != {"status": "MATCHED_AT_READ", "raw_count": expected_count}:
                raise AssertionError("Business API could not verify the stored receipt")
            original = raws[0]["raw"]
            ingestor.ingest(original)
            current = table.get_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"}, ConsistentRead=True)["Item"]
            if int(current["revision"]) != expected_count:
                raise AssertionError("Duplicate input incorrectly advanced source revision")
            altered = json.loads(original)
            altered["surfaceCelsius"] = 1
            try:
                ingestor.ingest(json.dumps(altered))
            except ValueError:
                pass
            else:
                raise AssertionError("Changed raw input was not rejected")
            current = table.get_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"}, ConsistentRead=True)["Item"]
            if int(current["revision"]) != expected_count:
                raise AssertionError("Conflicting input incorrectly advanced source revision")
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
            if len(processed) == expected_count:
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
        if mock_wire:
            last = table.get_item(Key={"pk": stream_pk, "sk": "RAW#000030"}, ConsistentRead=True)["Item"]
            later = json.loads(last["raw"])
            later["second"] = 31
            payload = json.dumps(later, separators=(",", ":"))
            def concurrent_ingest(_):
                return DynamoJeyukIngestor(DATA_TABLE, ordered_url).ingest(payload)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(concurrent_ingest, range(4)))
            revision = table.get_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"}, ConsistentRead=True)["Item"]
            if int(revision["revision"]) != 31:
                raise AssertionError("Concurrent duplicate writes advanced source revision incorrectly")
            changed = read_collection_completion(DATA_TABLE, fixture_id)
            if changed != {"status": "NOT_MATCHED", "raw_count": 31}:
                raise AssertionError("Later source addition did not invalidate the receipt match")
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
            if mock_wire:
                remove_partition(table, f"MOCK#mockprofile-110-a-001#{cursor_key}")
        except Exception as exc:
            errors.append(f"table cleanup: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock-wire", action="store_true")
    sys.exit(main(mock_wire=parser.parse_args().mock_wire))
