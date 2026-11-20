"""Opt-in C# Mock HTTPS -> ordered FIFO -> Worker quality/DLQ validation.

Uses only existing foodtech development resources and removes a unique synthetic batch.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_collection_adapter.aws_flow import DynamoOrderedIngestor
from foodtech_contracts import dedup_key, payload_hash, stream_binding_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker

from process_flow_dynamodb_check import (ACCOUNT, DATA_TABLE, ORDERED_QUEUE, QUARANTINE_QUEUE,
                                         REGION, poll_csharp_mock, remove_partition,
                                         remove_test_messages)


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/mockprofile-100-a-001.json"


def empty_queue(sqs, url: str) -> bool:
    attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=[
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesDelayed",
        "ApproximateNumberOfMessagesNotVisible",
    ])["Attributes"]
    return all(int(value) == 0 for value in attributes.values())


def event_ids(table, stream_pk: str) -> set[str]:
    ids = set()
    last_key = None
    while True:
        args = {"KeyConditionExpression": Key("pk").eq(stream_pk) & Key("sk").begins_with("RAW#"),
                "ConsistentRead": True}
        if last_key:
            args["ExclusiveStartKey"] = last_key
        page = table.query(**args)
        ids.update(item["ingestion_event_id"] for item in page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return ids


def remove_quarantine_messages(sqs, url: str, ids: set[str]) -> None:
    for _ in range(4):
        page = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10,
                                   WaitTimeSeconds=1, VisibilityTimeout=5)
        messages = page.get("Messages", [])
        if not messages:
            return
        for message in messages:
            if json.loads(message["Body"]).get("ingestion_event_id") not in ids:
                sqs.change_message_visibility(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"],
                                              VisibilityTimeout=0)
                raise RuntimeError("unexpected message in shared development quarantine queue")
            sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])


def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    sqs = session.client("sqs")
    ordered_url = sqs.get_queue_url(QueueName=ORDERED_QUEUE)["QueueUrl"]
    quarantine_url = sqs.get_queue_url(QueueName=QUARANTINE_QUEUE)["QueueUrl"]
    if not empty_queue(sqs, ordered_url) or not empty_queue(sqs, quarantine_url):
        raise SystemExit("The shared development queues must be empty.")

    run_id = uuid.uuid4().hex[:12]
    fixture = json.loads(FIXTURE.read_text())
    records = fixture["records"]
    batch_id = f"batch-a-quality-{run_id}"
    for record in records:
        record["batchId"] = batch_id
        record["recordId"] = f"rec-a-quality-{run_id}-{record['sequenceNumber']:03d}"
    stream_pk = stream_binding_key("synthetic-rest", "mockprofile-100-a-001", batch_id)
    table = session.resource("dynamodb").Table(DATA_TABLE)
    ingestor = DynamoOrderedIngestor(DATA_TABLE, ordered_url)
    worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, quarantine_url)

    try:
        raws = poll_csharp_mock(fixture, records)
        released = []
        for raw in raws:
            released.extend(ingestor.ingest(raw))
        revision = table.get_item(Key={"pk": stream_pk, "sk": "SOURCE#REVISION"},
                                  ConsistentRead=True)["Item"]["revision"]
        if revision != len(raws):
            raise AssertionError("raw writes and source revision disagree")
        if [item.sequence_number for item in released] != fixture["expected"]["releaseSequence"]:
            raise AssertionError("DynamoDB resequencer did not restore the fixture order")

        processed = []
        for _ in range(6):
            page = sqs.receive_message(QueueUrl=ordered_url, MaxNumberOfMessages=10,
                                       WaitTimeSeconds=1, VisibilityTimeout=30)
            for message in page.get("Messages", []):
                reference = json.loads(message["Body"])
                if reference["pk"] != stream_pk:
                    sqs.change_message_visibility(QueueUrl=ordered_url,
                                                  ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0)
                    raise RuntimeError("unexpected message in shared development ordered queue")
                raw_item = table.get_item(Key={"pk": stream_pk, "sk": reference["raw_sk"]},
                                          ConsistentRead=True)["Item"]
                result = worker.process(reference)
                processed.append((int(raw_item["sequence_number"]), result, reference))
                sqs.delete_message(QueueUrl=ordered_url, ReceiptHandle=message["ReceiptHandle"])
            if len(processed) == len(records):
                break
        if [item[0] for item in processed] != fixture["expected"]["releaseSequence"]:
            raise AssertionError("FIFO worker order differs from the restored sequence")
        if [item[1]["quality_status"] for item in processed] != fixture["expected"]["qualityStatus"]:
            raise AssertionError("Worker quality results differ from the fixture")
        if any(item[1]["dedup_status"] != "UNIQUE" for item in processed):
            raise AssertionError("unique synthetic records were marked duplicate")
        for _, result, reference in processed:
            assessment = table.get_item(Key={"pk": f"EVENT#{result['ingestion_event_id']}",
                                           "sk": "ASSESSMENT#v1"}, ConsistentRead=True)["Item"]
            raw = table.get_item(Key={"pk": stream_pk, "sk": reference["raw_sk"]},
                                 ConsistentRead=True)["Item"]["raw"]
            if assessment["payload_hash"] != payload_hash(json.loads(raw)):
                raise AssertionError("stored canonical payload hash differs from the wire record")
        quarantined = processed[-1][1]
        finding = table.get_item(Key={"pk": f"EVENT#{quarantined['ingestion_event_id']}",
                                      "sk": "FINDING#001"}, ConsistentRead=True)["Item"]
        if finding["code"] != fixture["expected"]["quarantineReason"]:
            raise AssertionError("quarantine finding differs from the fixture")
        dlq_page = sqs.receive_message(QueueUrl=quarantine_url, MaxNumberOfMessages=10,
                                       WaitTimeSeconds=2, VisibilityTimeout=30)
        dlq_messages = dlq_page.get("Messages", [])
        if len(dlq_messages) != 1 or \
                json.loads(dlq_messages[0]["Body"])["ingestion_event_id"] != quarantined["ingestion_event_id"]:
            raise AssertionError("quarantined event was not handed to the standardization DLQ")
        sqs.delete_message(QueueUrl=quarantine_url, ReceiptHandle=dlq_messages[0]["ReceiptHandle"])
        print(json.dumps({"received": len(raws), "released_sequence": [item[0] for item in processed],
                          "quality_status": [item[1]["quality_status"] for item in processed],
                          "quarantine_reason": finding["code"], "dlq_messages": 1}), flush=True)
    finally:
        errors = []
        try:
            remove_test_messages(sqs, ordered_url, stream_pk)
            remove_quarantine_messages(sqs, quarantine_url, event_ids(table, stream_pk))
        except Exception as exc:
            errors.append(f"queue cleanup: {exc}")
        try:
            for record in records:
                digest = hashlib.sha256(dedup_key(record).encode("utf-8")).hexdigest()
                key = {"pk": f"DEDUP#{digest}", "sk": "CANONICAL"}
                table.delete_item(Key=key)
                if table.get_item(Key=key, ConsistentRead=True).get("Item"):
                    raise AssertionError("synthetic deduplication item remains")
            for event_id in event_ids(table, stream_pk):
                remove_partition(table, f"EVENT#{event_id}")
            remove_partition(table, stream_pk)
        except Exception as exc:
            errors.append(f"table cleanup: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        print("synthetic cleanup complete", flush=True)


if __name__ == "__main__":
    main()
