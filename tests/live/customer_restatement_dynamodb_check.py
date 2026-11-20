"""Opt-in synthetic customer restatement through the development FIFO worker."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3
import httpx

from foodtech_business_api import cooking
from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_business_api.main import app
from foodtech_contracts import stream_binding_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker
from late_restatement_dynamodb_check import clean_partition

ACCOUNT = "573693340100"
COOKING_TABLE = "foodtech-cooking-jobs-dev"
DATA_TABLE = "foodtech-data-flow-dev"
QUEUE = "foodtech-ordered-dev.fifo"


async def request(client, subject, job_id, request_id, queue_url, *, status=False):
    async def session(_request, _surface): return SimpleNamespace(subject=subject, csrf="test-csrf")
    config = {"FOODTECH_SECURE_COOKIES": "false", "FOODTECH_DATA_TABLE": DATA_TABLE,
              "FOODTECH_ORDERED_QUEUE_URL": queue_url}
    with patch.dict(os.environ, config), patch.object(cooking, "_session", side_effect=session):
        path = f"/api/customer/cooking-jobs/{job_id}/process-restatements"
        if status:
            return await client.get(path + "/" + request_id)
        return await client.post(path, headers={"X-CSRF-Token": "test-csrf"},
                                 json={"expected_version": 7, "request_id": request_id})


async def api_call(subject, job_id, request_id, queue_url, status=False):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        return await request(client, subject, job_id, request_id, queue_url, status=status)


def main():
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != COOKING_TABLE:
        raise SystemExit("Use the foodtech profile and exact development cooking table")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    sqs = session.client("sqs")
    url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
    stats = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"])["Attributes"]
    if any(int(value) for value in stats.values()):
        raise SystemExit("The shared development FIFO queue must be empty")
    resource = session.resource("dynamodb")
    jobs, data = resource.Table(COOKING_TABLE), resource.Table(DATA_TABLE)
    suffix = uuid4().hex
    subject, job_id, request_id = "synthetic-restatement-" + suffix, str(uuid4()), str(uuid4())
    pk = cooking._pk(subject, job_id)
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001", "batch_id": "test-" + suffix}
    stream = stream_binding_key(**binding)
    event_pks = [f"EVENT#synthetic-{suffix}-{index}" for index in range(2)]
    receipt_handle = None
    try:
        jobs.put_item(Item={"pk": pk, "sk": "META", "job_id": job_id, "version": 7,
            "status": "COMPLETED", "menu": "TONKATSU", "provenance": "SYNTHETIC", "source_binding": binding,
            "actual_placed_at": "2026-11-14T00:00:00Z", "actual_lifted_at": "2026-11-14T00:00:05Z",
            "process_thickness_status": "NORMAL", "process_time_status": "NORMAL"})
        for index, event_pk in enumerate(event_pks):
            event_id = event_pk.removeprefix("EVENT#")
            data.put_item(Item={"pk": stream, "sk": "RAW#" + event_id, "ingestion_event_id": event_id})
            data.put_item(Item={"pk": event_pk, "sk": "ASSESSMENT#v1", "source_pk": stream,
                "ingestion_event_id": event_id, "dedup_status": "UNIQUE", "quality_status": "VALID",
                "normalized": {**binding, "sequence_number": index + 1,
                    "event_time": f"2026-11-14T00:00:0{index * 5}Z", "device_status": "RUNNING",
                    "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}})
        data.put_item(Item={"pk": stream, "sk": "SOURCE#REVISION", "revision": 2})
        response = asyncio.run(api_call(subject, job_id, request_id, url))
        assert response.status_code == 202, response.text
        assert response.json()["status"] == "PENDING"
        replay = asyncio.run(api_call(subject, job_id, request_id, url))
        assert replay.status_code == 202 and replay.json()["status"] == "PENDING"
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=2, WaitTimeSeconds=5,
                                       VisibilityTimeout=90).get("Messages", [])
        assert len(messages) == 1
        receipt_handle = messages[0]["ReceiptHandle"]
        reference = json.loads(messages[0]["Body"])
        assert reference == {"kind": "CUSTOMER_RESTATEMENT", "job_pk": pk, "request_id": request_id}
        with patch.dict(os.environ, {"FOODTECH_COOKING_TABLE": COOKING_TABLE}):
            worker = DynamoStandardizationWorker(DATA_TABLE, url, "unused")
        assert worker.process(reference)["result"] == "DERIVED"
        assert worker.process(reference)["result"] == "ALREADY_COMPLETED"
        sqs.delete_message(QueueUrl=url, ReceiptHandle=receipt_handle)
        receipt_handle = None
        result = asyncio.run(api_call(subject, job_id, request_id, url, status=True))
        assert result.status_code == 200 and result.json()["status"] == "COMPLETED"
        assert DynamoCookingStore(COOKING_TABLE).latest_process_result(pk)["source_revision"] == 2
        stale_id = str(uuid4())
        stale_request = asyncio.run(api_call(subject, job_id, stale_id, url))
        assert stale_request.status_code == 202
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=5,
                                       VisibilityTimeout=90).get("Messages", [])
        assert len(messages) == 1
        receipt_handle = messages[0]["ReceiptHandle"]
        stale_reference = json.loads(messages[0]["Body"])
        assert stale_reference["job_pk"] == pk and stale_reference["request_id"] == stale_id
        jobs.update_item(Key={"pk": pk, "sk": "META"}, UpdateExpression="ADD #v :one",
                         ExpressionAttributeNames={"#v": "version"}, ExpressionAttributeValues={":one": 1})
        assert worker.process(stale_reference)["result"] == "REQUEST_STALE"
        sqs.delete_message(QueueUrl=url, ReceiptHandle=receipt_handle)
        receipt_handle = None
        stale_status = asyncio.run(api_call(subject, job_id, stale_id, url, status=True))
        assert stale_status.json()["status"] == "STALE"
        print(json.dumps({"customer_request": "COMPLETED", "worker_replay": "ALREADY_COMPLETED",
                          "changed_job_request": "STALE", "source_revision": 2, "cleanup_partitions": 4}))
    finally:
        if receipt_handle:
            sqs.delete_message(QueueUrl=url, ReceiptHandle=receipt_handle)
        clean_partition(jobs, pk)
        clean_partition(data, stream)
        for event_pk in event_pks: clean_partition(data, event_pk)


if __name__ == "__main__":
    main()
