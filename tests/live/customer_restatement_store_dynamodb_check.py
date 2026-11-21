"""Opt-in development DynamoDB check of customer restatement request history."""
import os
import uuid

import boto3
from boto3.dynamodb.conditions import Key

from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-cooking-jobs-dev"


def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech" or os.getenv("FOODTECH_COOKING_TABLE") != TABLE:
        raise SystemExit("Use the foodtech profile and exact development cooking table.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account.")
    subject = "synthetic-request-history-" + uuid.uuid4().hex
    pk = f"SCOPE#USER#{subject}#JOB#test"
    history_pk = f"SCOPE#USER#{subject}#JOBS"
    store = DynamoCookingStore(TABLE)
    request_id = str(uuid.uuid4())
    request = {"sk": f"RESTATEMENT#REQUEST#{request_id}", "request_id": request_id,
               "status": "PENDING", "reason": "CUSTOMER_REFRESH", "job_version_at_detection": 1,
               "source_stream_pk": "SYNTHETIC#test"}
    try:
        store.table.put_item(Item={"pk": pk, "sk": "META", "version": 1})
        saved = store.create_customer_restatement(pk, 1, request)
        assert saved["status"] == "PENDING" and store.history_revision(subject) == 1
        assert store.create_customer_restatement(pk, 1, request)["request_id"] == request_id
        assert store.history_revision(subject) == 1
        try:
            store.create_customer_restatement(pk, 1, {**request, "source_stream_pk": "SYNTHETIC#other"})
        except Conflict:
            pass
        else:
            raise AssertionError("changed idempotent request was accepted")
        stale_id = str(uuid.uuid4())
        try:
            store.create_customer_restatement(pk, 2, {**request, "request_id": stale_id,
                                                       "sk": f"RESTATEMENT#REQUEST#{stale_id}"})
        except Conflict:
            pass
        else:
            raise AssertionError("stale job version was accepted")
        assert store.history_revision(subject) == 1
        print("customer request, replay, conflict and history revision verified")
    finally:
        for partition in (pk, history_pk):
            while True:
                page = store.table.query(KeyConditionExpression=Key("pk").eq(partition), ConsistentRead=True)
                items = page.get("Items", [])
                if not items:
                    break
                with store.table.batch_writer() as writer:
                    for item in items:
                        writer.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
        print("synthetic cleanup complete")


if __name__ == "__main__":
    main()
