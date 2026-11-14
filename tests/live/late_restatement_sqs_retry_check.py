"""Opt-in FIFO redelivery check for one synthetic late process event."""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from foodtech_contracts import dedup_key, stream_binding_key
from foodtech_standardization_worker.aws_flow import DynamoStandardizationWorker

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
DATA_TABLE = "foodtech-data-flow-dev"
COOKING_TABLE = "foodtech-cooking-jobs-dev"
ORDERED_QUEUE = "foodtech-ordered-dev.fifo"
DLQ = "foodtech-standardizationdlq-dev.fifo"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures/tonkatsu-process-high-v1.json"


class GuardedSqs:
    def __init__(self, client, event_id: str):
        self.client = client
        self.event_id = event_id
        self.last_receipt = None

    def receive_message(self, **kwargs):
        page = self.client.receive_message(**kwargs)
        for message in page.get("Messages", []):
            if json.loads(message["Body"]).get("ingestion_event_id") != self.event_id:
                self.client.change_message_visibility(QueueUrl=kwargs["QueueUrl"],
                                                      ReceiptHandle=message["ReceiptHandle"],
                                                      VisibilityTimeout=0)
                raise RuntimeError("unexpected message in shared development queue")
            self.last_receipt = message["ReceiptHandle"]
        return page

    def delete_message(self, **kwargs):
        return self.client.delete_message(**kwargs)


def clear_partition(table, pk: str) -> None:
    with table.batch_writer() as writer:
        last = None
        while True:
            args = {"KeyConditionExpression": Key("pk").eq(pk), "ConsistentRead": True}
            if last:
                args["ExclusiveStartKey"] = last
            page = table.query(**args)
            for item in page.get("Items", []):
                writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            last = page.get("LastEvaluatedKey")
            if not last:
                break
    assert table.query(KeyConditionExpression=Key("pk").eq(pk), ConsistentRead=True,
                       Select="COUNT")["Count"] == 0


def clear_message(sqs, queue_url: str, event_id: str) -> None:
    for _ in range(5):
        page = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10,
                                   WaitTimeSeconds=1, VisibilityTimeout=1)
        if not page.get("Messages"):
            continue
        for message in page["Messages"]:
            if json.loads(message["Body"]).get("ingestion_event_id") != event_id:
                sqs.change_message_visibility(QueueUrl=queue_url,
                                              ReceiptHandle=message["ReceiptHandle"],
                                              VisibilityTimeout=0)
                raise RuntimeError("unexpected message in shared development queue")
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def main() -> None:
    if (os.getenv("AWS_PROFILE") != "foodtech" or
            os.getenv("FOODTECH_DATA_TABLE") != DATA_TABLE or
            os.getenv("FOODTECH_COOKING_TABLE") != COOKING_TABLE):
        raise SystemExit("Use the foodtech profile and exact development tables.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    assert session.client("sts").get_caller_identity()["Account"] == ACCOUNT
    sqs = session.client("sqs")
    ordered_url = sqs.get_queue_url(QueueName=ORDERED_QUEUE)["QueueUrl"]
    dlq_url = sqs.get_queue_url(QueueName=DLQ)["QueueUrl"]
    attributes = sqs.get_queue_attributes(QueueUrl=ordered_url, AttributeNames=[
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesDelayed",
        "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
    if any(int(value) for value in attributes.values()):
        raise SystemExit("The shared development ordered queue is not empty.")
    data = session.resource("dynamodb").Table(DATA_TABLE)
    cooking = session.resource("dynamodb").Table(COOKING_TABLE)
    run_id = uuid.uuid4().hex[:12]
    event_id = f"synthetic-sqs-late-{run_id}"
    event_pks = [f"EVENT#synthetic-sqs-{run_id}-{name}" for name in ("first", "third")]
    job_pk = f"SCOPE#USER#synthetic-sqs-{run_id}#JOB#test"
    binding = {"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
               "batch_id": f"batch-a-sqs-{run_id}"}
    stream_pk = stream_binding_key(**binding)
    record = json.loads(FIXTURE.read_text())["records"][0]
    record.update({"batchId": binding["batch_id"], "recordId": f"rec-a-sqs-{run_id}-002",
                   "sequenceNumber": 2, "eventTime": "2026-11-14T00:00:05Z"})
    record["measurements"].update({"temperatureCelsius": 170, "elapsedSeconds": 5})
    raw = json.dumps(record, separators=(",", ":"))
    reference = {"pk": stream_pk, "raw_sk": f"RAW#{event_id}",
                 "ingestion_event_id": event_id, "sequence_status": "LATE_AFTER_GAP",
                 "gap_before": []}
    digest = hashlib.sha256(dedup_key(record).encode("utf-8")).hexdigest()
    sent = False
    completed = False
    guarded = None
    try:
        cooking.put_item(Item={"pk": job_pk, "sk": "META", "job_id": "test", "version": 7,
                               "status": "COMPLETED", "source_binding": binding,
                               "process_assessment_status": "WARNING",
                               "process_thickness_status": "NORMAL", "process_time_status": "NORMAL"})
        cooking.put_item(Item={"pk": stream_pk, "sk": "BINDING", "job_pk": job_pk})
        for name, sequence, second in (("first", 1, 0), ("third", 3, 10)):
            source_id = f"synthetic-sqs-{run_id}-{name}"
            data.put_item(Item={"pk": stream_pk, "sk": f"RAW#{source_id}",
                                "ingestion_event_id": source_id})
            data.put_item(Item={"pk": f"EVENT#{source_id}", "sk": "ASSESSMENT#v1",
                                "source_pk": stream_pk,
                                "ingestion_event_id": source_id, "dedup_status": "UNIQUE",
                                "quality_status": "VALID", "finding_codes": [],
                                "normalized": {**binding, "sequence_number": sequence,
                                               "event_time": f"2026-11-14T00:00:{second:02d}Z",
                                               "device_status": "RUNNING",
                                               "CHAMBER_TEMPERATURE": {"value": "170", "unit": "°C"}}})
            if name == "first":
                cooking.put_item(Item={"pk": job_pk, "sk": f"EVENT#PROCESS#{source_id}",
                                       "source_assessment_pk": f"EVENT#{source_id}"})
        data.put_item(Item={"pk": stream_pk, "sk": reference["raw_sk"], "raw": raw,
                            "ingestion_event_id": event_id})
        with patch.dict(os.environ, {"FOODTECH_COOKING_TABLE": COOKING_TABLE}):
            worker = DynamoStandardizationWorker(DATA_TABLE, ordered_url, dlq_url)
        guarded = GuardedSqs(sqs, event_id)
        worker.sqs = guarded
        sqs.send_message(QueueUrl=ordered_url, MessageBody=json.dumps(reference),
                         MessageGroupId=binding["device_id"], MessageDeduplicationId=event_id)
        sent = True
        with patch.object(worker.process_bridge.restatement, "process", return_value="STALE_RETRY"):
            failed_once = False
            for _ in range(5):
                try:
                    result = worker.run_once()
                except RuntimeError as exc:
                    assert "must be retried" in str(exc)
                    failed_once = True
                    break
                assert result == []
            assert failed_once
        assert guarded.last_receipt is not None
        sqs.change_message_visibility(QueueUrl=ordered_url,
                                      ReceiptHandle=guarded.last_receipt, VisibilityTimeout=0)
        request_key = {"pk": job_pk, "sk": f"RESTATEMENT#REQUEST#{event_id}"}
        request = cooking.get_item(Key=request_key, ConsistentRead=True)["Item"]
        assert request["status"] == "PENDING"
        assessment = data.get_item(Key={"pk": f"EVENT#{event_id}", "sk": "ASSESSMENT#v1"},
                                   ConsistentRead=True)["Item"]
        assert assessment["finding_codes"] == ["LATE_AFTER_GAP"]
        retried = None
        for _ in range(10):
            result = worker.run_once()
            if result:
                retried = result[0]
                break
            time.sleep(0.5)
        assert retried is not None and retried["replayed"]
        assert retried["process_result"] == "RESTATEMENT_DERIVED"
        request = cooking.get_item(Key=request_key, ConsistentRead=True)["Item"]
        result = cooking.get_item(Key={"pk": job_pk, "sk": request["result_sk"]},
                                  ConsistentRead=True)["Item"]
        assert request["status"] == "COMPLETED"
        assert result["process_assessment_status"] == "NORMAL"
        assert len(result["included_ingestion_event_ids"]) == 3
        assert result["source_raw_count"] == 3
        assert cooking.get_item(Key={"pk": job_pk, "sk": "META"},
                                ConsistentRead=True)["Item"]["process_assessment_status"] == "WARNING"
        completed = True
        print(json.dumps({"account": ACCOUNT, "first_attempt": "RETRY",
                          "redelivered": retried["replayed"], "result": request["status"],
                          "derived_status": result["process_assessment_status"]}))
    finally:
        if sent:
            if not completed and guarded and guarded.last_receipt:
                try:
                    sqs.change_message_visibility(QueueUrl=ordered_url,
                                                  ReceiptHandle=guarded.last_receipt,
                                                  VisibilityTimeout=0)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "InvalidParameterValue":
                        raise
            clear_message(sqs, ordered_url, event_id)
        clear_partition(cooking, job_pk)
        clear_partition(cooking, stream_pk)
        for pk in [*event_pks, f"EVENT#{event_id}", stream_pk]:
            clear_partition(data, pk)
        data.delete_item(Key={"pk": f"DEDUP#{digest}", "sk": "CANONICAL"})


if __name__ == "__main__":
    main()
