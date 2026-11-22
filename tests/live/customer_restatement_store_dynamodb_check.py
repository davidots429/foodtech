"""Opt-in development DynamoDB check of customer restatement request history."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

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
    store = DynamoCookingStore(TABLE, "foodtech-auth-sessions-dev")
    auth = session.resource("dynamodb").Table("foodtech-auth-sessions-dev")
    auth_key = {"pk": f"USER#{subject}"}
    auth_created = False
    request_id = str(uuid.uuid4())
    request = {"sk": f"RESTATEMENT#REQUEST#{request_id}", "request_id": request_id,
               "status": "PENDING", "reason": "CUSTOMER_REFRESH", "job_version_at_detection": 1,
               "source_stream_pk": "SYNTHETIC#test"}
    try:
        store.table.put_item(Item={"pk": pk, "sk": "META", "version": 1})
        auth.put_item(Item={**auth_key, "epoch": 0, "deletion_lock_request": str(uuid.uuid4())},
                      ConditionExpression="attribute_not_exists(pk)")
        auth_created = True
        try:
            store.create_customer_restatement(pk, 1, request, 0)
        except Conflict:
            pass
        else:
            raise AssertionError("locked request was accepted")
        assert store.customer_restatement(pk, request_id) is None
        assert store.history_revision(subject) is None
        auth.update_item(Key=auth_key, UpdateExpression="REMOVE deletion_lock_request")
        try:
            store.create_customer_restatement(pk, 1, request, 1)
        except Conflict:
            pass
        else:
            raise AssertionError("stale session accepted")
        saved = store.create_customer_restatement(pk, 1, request, 0)
        assert saved["status"] == "PENDING" and store.history_revision(subject) == 1
        assert store.create_customer_restatement(pk, 1, request)["request_id"] == request_id
        assert store.history_revision(subject) == 1
        concurrent_id = str(uuid.uuid4())
        concurrent_request = {**request, "request_id": concurrent_id,
                              "sk": f"RESTATEMENT#REQUEST#{concurrent_id}"}
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: store.create_customer_restatement(pk, 1, concurrent_request),
                                        range(2)))
        assert all(item["request_id"] == concurrent_id for item in results)
        assert store.history_revision(subject) == 2
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
        assert store.history_revision(subject) == 2
        print("customer request, concurrent replay, conflict and history revision verified")
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
        if auth_created:
            auth.delete_item(Key=auth_key)
        print("synthetic cleanup complete")


if __name__ == "__main__":
    main()
