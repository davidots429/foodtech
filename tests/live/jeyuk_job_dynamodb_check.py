"""Opt-in C# Jeyuk samples -> FIFO Worker -> bound customer job validation."""

import asyncio
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import patch
from pathlib import Path

import boto3
import httpx

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_collection_adapter.jeyuk_aws import DynamoJeyukIngestor
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker

from jeyuk_dynamodb_check import csharp_samples
from mock_quality_dynamodb_check import empty_queue
from process_flow_dynamodb_check import (ACCOUNT, COOKING_TABLE, DATA_TABLE, ORDERED_QUEUE,
                                         QUARANTINE_QUEUE, REGION, post, remove_partition,
                                         remove_test_messages)


SAFETY_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/jeyuk-small-safety-v1.json"


async def run_check(*, safety: bool = False) -> int:
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
    run_id = uuid.uuid4().hex[:12]
    fixture_id = f"jeyuk-{'safety' if safety else 'job'}-{run_id}"
    stream_pk = f"JEYUK#{fixture_id}"
    subject = f"synthetic-jeyuk-{run_id}"
    data_table = session.resource("dynamodb").Table(DATA_TABLE)
    cooking_table = session.resource("dynamodb").Table(COOKING_TABLE)
    cooking_store = DynamoCookingStore(COOKING_TABLE)
    job_pk = None
    listing_key = None

    async def fake_session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false",
                                     "FOODTECH_COOKING_TABLE": COOKING_TABLE}), \
             patch.object(cooking, "_session", side_effect=fake_session), \
             patch.object(cooking, "get_cooking_store", return_value=cooking_store):
            worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)
            ingestor = DynamoJeyukIngestor(DATA_TABLE, ordered_url)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://local") as client:
                job = await post(client, "/api/customer/cooking-jobs", {
                    "planned_portions": 2, "recipe_version": "synthetic-jeyuk-live-v1",
                    "equipment_profile": "jeyuk-small", "actual_load_kg": "1.8"}, 201)
                job_pk = cooking._pk(subject, job["job_id"])
                listing_key = {"pk": f"SCOPE#USER#{subject}#JOBS",
                               "sk": f"CREATED#{job['created_at']}#{job['job_id']}"}
                path = f"/api/customer/cooking-jobs/{job['job_id']}"
                job = await post(client, path + "/source-binding", {
                    "expected_version": job["version"], "source_id": "synthetic-rest",
                    "device_id": "mockprofile-110-a-001", "batch_id": fixture_id})
                job = await post(client, path + "/transitions", {
                    "expected_version": job["version"], "event": "PREHEAT_COMPLETED"})
                job = await post(client, path + "/transitions", {
                    "expected_version": job["version"], "event": "COOKING_STARTED",
                    "actual_placed_at": datetime.now(timezone.utc).isoformat()})
                raws = csharp_samples(fixture_id, SAFETY_FIXTURE) if safety else csharp_samples(fixture_id)
                for raw in raws:
                    ingestor.ingest(raw)
                processed = []
                references = []
                for _ in range(16):
                    page = sqs.receive_message(QueueUrl=ordered_url, MaxNumberOfMessages=10,
                                               WaitTimeSeconds=1, VisibilityTimeout=180)
                    for message in page.get("Messages", []):
                        reference = json.loads(message["Body"])
                        if reference.get("pk") != stream_pk:
                            sqs.change_message_visibility(QueueUrl=ordered_url,
                                                          ReceiptHandle=message["ReceiptHandle"],
                                                          VisibilityTimeout=0)
                            raise RuntimeError("unexpected message in shared development ordered queue")
                        try:
                            processed.append(worker.process(reference))
                        except Exception:
                            sqs.change_message_visibility(QueueUrl=ordered_url,
                                                          ReceiptHandle=message["ReceiptHandle"],
                                                          VisibilityTimeout=0)
                            raise
                        references.append(reference)
                        sqs.delete_message(QueueUrl=ordered_url, ReceiptHandle=message["ReceiptHandle"])
                    if len(processed) == len(raws):
                        break
                if len(processed) != len(raws):
                    raise AssertionError("not all Jeyuk samples reached the bound job")
                final = (await client.get(path)).json()["job"]
                if safety:
                    if ((processed[59]["auto_safety_status"], processed[59]["mixture_hold_seconds"]) !=
                            ("PENDING", 59) or
                            (processed[60]["auto_safety_status"], processed[60]["mixture_hold_seconds"]) !=
                            ("PASSED", 60) or
                            (final["status"], final["auto_safety_status"], final["evaluation_blocked"]) !=
                            ("RUNNING", "PASSED", True)):
                        raise AssertionError("post-addition sixty-second safety hold was not recorded")
                elif (processed[4]["job_result"] != "WARNING" or
                      processed[29]["job_result"] != "FAILED" or
                      (final["status"], final["process_assessment_status"],
                       final["process_assessment"]["second"]) != ("FAILED", "FAILED", 30)):
                    raise AssertionError("Jeyuk warning or failure did not reach the bound job")
                if worker.process(references[-1])["job_result"] != "REPLAYED":
                    raise AssertionError("replayed assessment changed the bound job")
                print(json.dumps({"processed": len(processed), "scenario": "SAFETY" if safety else "PROCESS",
                                  "job_status": final["status"],
                                  "auto_safety_status": final["auto_safety_status"],
                                  "replay": "REPLAYED"}), flush=True)
                return 0
    finally:
        errors = []
        try:
            remove_test_messages(sqs, ordered_url, stream_pk)
        except Exception as exc:
            errors.append(f"queue cleanup: {exc}")
        try:
            remove_partition(data_table, stream_pk)
            remove_partition(cooking_table, stream_pk)
            if job_pk:
                remove_partition(cooking_table, job_pk)
            if listing_key:
                cooking_table.delete_item(Key=listing_key)
                if cooking_table.get_item(Key=listing_key, ConsistentRead=True).get("Item"):
                    raise AssertionError("synthetic job list entry remains")
        except Exception as exc:
            errors.append(f"table cleanup: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        print("synthetic cleanup complete", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--safety", action="store_true")
    return asyncio.run(run_check(safety=parser.parse_args().safety))


if __name__ == "__main__":
    sys.exit(main())
