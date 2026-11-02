"""Opt-in live check of collection -> FIFO -> standardization -> cooking process.

Only the existing foodtech development tables and queue are used. This script
creates a unique synthetic batch and removes its records in finally.
"""

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
import httpx
from boto3.dynamodb.conditions import Key

from foodtech_business_api import cooking
from foodtech_business_api.auth_store import Session
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_collection_adapter.aws_flow import DynamoOrderedIngestor
from foodtech_contracts import dedup_key, stream_binding_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker


ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
DATA_TABLE = "foodtech-data-flow-dev"
COOKING_TABLE = "foodtech-cooking-jobs-dev"
ORDERED_QUEUE = "foodtech-ordered-dev.fifo"
QUARANTINE_QUEUE = "foodtech-standardizationdlq-dev.fifo"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/tonkatsu-process-high-v1.json"


async def post(client: httpx.AsyncClient, path: str, body: dict, expected: int = 200) -> dict:
    response = await client.post(path, json=body, headers={"X-CSRF-Token": "validation-csrf"})
    if response.status_code != expected:
        raise AssertionError(f"{path}: expected {expected}, got {response.status_code}: {response.text}")
    return response.json()


def remove_partition(table, pk: str) -> None:
    page = table.query(KeyConditionExpression=Key("pk").eq(pk), ConsistentRead=True)
    with table.batch_writer() as writer:
        while True:
            for item in page.get("Items", []):
                writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            if "LastEvaluatedKey" not in page:
                break
            page = table.query(KeyConditionExpression=Key("pk").eq(pk), ConsistentRead=True,
                               ExclusiveStartKey=page["LastEvaluatedKey"])


def remove_test_messages(sqs, queue_url: str, stream_pk: str) -> None:
    for _ in range(4):
        page = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10,
                                   WaitTimeSeconds=1, VisibilityTimeout=5)
        messages = page.get("Messages", [])
        if not messages:
            return
        for message in messages:
            if json.loads(message["Body"]).get("pk") != stream_pk:
                sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"],
                                              VisibilityTimeout=0)
                raise RuntimeError("unexpected message in shared development queue")
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


async def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    sqs = session.client("sqs")
    ordered_url = sqs.get_queue_url(QueueName=ORDERED_QUEUE)["QueueUrl"]
    quarantine_url = sqs.get_queue_url(QueueName=QUARANTINE_QUEUE)["QueueUrl"]
    attributes = sqs.get_queue_attributes(QueueUrl=ordered_url, AttributeNames=[
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesDelayed",
        "ApproximateNumberOfMessagesNotVisible",
    ])["Attributes"]
    if any(int(value) for value in attributes.values()):
        raise SystemExit("The shared development ordered queue is not empty.")

    run_id = uuid.uuid4().hex[:12]
    batch_id = f"batch-a-live-{run_id}"
    subject = f"synthetic-process-validation-{run_id}"
    records = json.loads(FIXTURE.read_text())["records"]
    for record in records:
        record["batchId"] = batch_id
        record["recordId"] = f"rec-a-live-{run_id}-{record['sequenceNumber']:03d}"
    stream_pk = stream_binding_key("synthetic-rest", "mockprofile-100-a-001", batch_id)
    job_pk = None
    data_table = session.resource("dynamodb").Table(DATA_TABLE)
    cooking_store = DynamoCookingStore(COOKING_TABLE)
    ingestor = DynamoOrderedIngestor(DATA_TABLE, ordered_url)

    async def fake_session(_request, surface):
        assert surface == "customer"
        return Session("customer", subject, None, "validation-csrf", 9999999999, 0)

    try:
        with patch.dict(os.environ, {"FOODTECH_SECURE_COOKIES": "false",
                                     "FOODTECH_COOKING_TABLE": COOKING_TABLE}), \
             patch.object(cooking, "_session", side_effect=fake_session), \
             patch.object(cooking, "get_cooking_store", return_value=cooking_store):
            worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://local") as client:
                job = await post(client, "/api/customer/cooking-jobs",
                                 {"planned_portions": 2, "recipe_version": "synthetic-process-live-v1"}, 201)
                job_pk = cooking._pk(subject, job["job_id"])
                path = f"/api/customer/cooking-jobs/{job['job_id']}"
                job = await post(client, path + "/source-binding", {
                    "expected_version": job["version"], "source_id": "synthetic-rest",
                    "device_id": "mockprofile-100-a-001", "batch_id": batch_id,
                })
                for event in ("PREHEAT_COMPLETED", "COOKING_STARTED"):
                    job = await post(client, path + "/transitions",
                                     {"expected_version": job["version"], "event": event})

                for record in records:
                    released = ingestor.ingest(json.dumps(record, separators=(",", ":")))
                    if len(released) != 1 or released[0].sequence_number != record["sequenceNumber"]:
                        raise AssertionError("ordered collector did not release the next sequence")

                processed = []
                references = []
                for _ in range(6):
                    page = sqs.receive_message(QueueUrl=ordered_url, MaxNumberOfMessages=10,
                                               WaitTimeSeconds=1, VisibilityTimeout=30)
                    for message in page.get("Messages", []):
                        reference = json.loads(message["Body"])
                        if reference["pk"] != stream_pk:
                            sqs.change_message_visibility(QueueUrl=ordered_url,
                                                          ReceiptHandle=message["ReceiptHandle"],
                                                          VisibilityTimeout=0)
                            raise RuntimeError("unexpected message in shared development queue")
                        try:
                            processed.append(worker.process(reference))
                        except Exception:
                            sqs.change_message_visibility(QueueUrl=ordered_url,
                                                          ReceiptHandle=message["ReceiptHandle"],
                                                          VisibilityTimeout=0)
                            raise
                        references.append(reference)
                        sqs.delete_message(QueueUrl=ordered_url, ReceiptHandle=message["ReceiptHandle"])
                    if len(processed) == len(records):
                        break
                if len(processed) != len(records):
                    raise AssertionError(f"expected {len(records)} processed events, got {len(processed)}")
                if [item["process_result"] for item in processed] != ["WARNING"] * 4 + ["FAILED"]:
                    raise AssertionError(f"unexpected process results: {processed}")
                snapshot = (await client.get(path)).json()
                final = snapshot["job"]
                assert final["status"] == "FAILED" and final["evaluation_blocked"]
                assert final["process_temperature_status"] == "HIGH_PROCESS_FAILURE"
                events = (await client.get(path + "/events")).json()
                process_events = [item for item in events if item["domain"] == "PROCESS_ASSESSMENT"]
                assert len(process_events) == 5 and process_events[-1]["trigger"] == "PROCESS_FAILURE_DETECTED"
                assert worker.process(references[-1])["process_result"] == "REPLAYED"
                print(json.dumps({"processed": len(processed), "job_status": final["status"],
                                  "process_status": final["process_assessment_status"],
                                  "temperature_status": final["process_temperature_status"],
                                  "process_events": len(process_events), "replay": "REPLAYED"}), flush=True)
    finally:
        cleanup_errors = []
        try:
            remove_test_messages(sqs, ordered_url, stream_pk)
        except Exception as exc:
            cleanup_errors.append(f"queue cleanup: {exc}")
        try:
            with data_table.batch_writer() as writer:
                for record in records:
                    digest = hashlib.sha256(dedup_key(record).encode("utf-8")).hexdigest()
                    writer.delete_item(Key={"pk": f"DEDUP#{digest}", "sk": "CANONICAL"})
            for item in data_table.query(KeyConditionExpression=Key("pk").eq(stream_pk),
                                         ConsistentRead=True).get("Items", []):
                if item["sk"].startswith("RAW#"):
                    remove_partition(data_table, f"EVENT#{item['ingestion_event_id']}")
            remove_partition(data_table, stream_pk)
            if job_pk:
                remove_partition(cooking_store.table, job_pk)
                cooking_store.table.delete_item(Key={"pk": stream_pk, "sk": "BINDING"})
        except Exception as exc:
            cleanup_errors.append(f"table cleanup: {exc}")
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
