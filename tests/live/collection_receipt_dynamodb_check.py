"""Opt-in synthetic receipt and raw snapshot validation against development DynamoDB."""
import json
import os
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_collection_adapter.aws_flow import DynamoOrderedIngestor
from foodtech_contracts import stream_key
from foodtech_contracts.collection_evidence import CompletionTracker, matches_snapshot

TABLE = "foodtech-data-flow-dev"


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name="ap-southeast-2")
    if session.client("sts").get_caller_identity()["Account"] != "573693340100":
        raise SystemExit("Unexpected AWS account")
    table = session.resource("dynamodb").Table(TABLE)
    identity = uuid4().hex
    record = {"sourceId": "synthetic-test-" + identity, "deviceId": "mockprofile-100-a-001",
              "batchId": "synthetic-test-" + identity, "sequenceNumber": 1}
    stream = stream_key(record)
    tracker = CompletionTracker(0, "mockprofile-100", record["deviceId"])
    raw = json.dumps(record, separators=(",", ":"))
    tracker.record(raw, record)
    receipt = tracker.finish(next_cursor=1, complete=True, completion_explicit=True)
    raw_key = {"pk": stream, "sk": "RAW#" + identity}
    ingestor = DynamoOrderedIngestor.__new__(DynamoOrderedIngestor)
    ingestor.table = table
    try:
        table.put_item(Item={**raw_key, "raw": raw, "provenance": "SYNTHETIC"})
        ingestor.save_completion_receipt(receipt, "synthetic-check")
        rows = table.query(KeyConditionExpression=Key("pk").eq(stream), ConsistentRead=True)["Items"]
        actual_receipt = next(item for item in rows if item["sk"].startswith("COLLECTION#RECEIPT#"))
        raw_rows = [item for item in rows if item["sk"].startswith("RAW#")]
        assert matches_snapshot(actual_receipt, stream, raw_rows)
        assert not matches_snapshot(actual_receipt, stream, [])
    finally:
        rows = table.query(KeyConditionExpression=Key("pk").eq(stream), ConsistentRead=True)["Items"]
        for item in rows:
            table.delete_item(Key={"pk": stream, "sk": item["sk"]})
        if table.query(KeyConditionExpression=Key("pk").eq(stream), ConsistentRead=True)["Items"]:
            raise RuntimeError("Test records remain")
    print(json.dumps({"receipt_snapshot_match": "PASS", "missing_raw_rejected": True,
                      "cleaned_items": len(rows)}))


if __name__ == "__main__":
    main()
