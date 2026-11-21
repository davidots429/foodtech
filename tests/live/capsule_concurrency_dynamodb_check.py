"""Exercise synthetic Capsule revision/lock races in the existing dev table.

Uses no real identities or keys. Removes only partitions created by this run.
This does not prove deployed IAM boundaries or ciphertext encryption.
"""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from foodtech_business_api.capsule_inventory import CapsuleInventory
from foodtech_business_api.capsule_writer import CapsuleWriter
from process_flow_dynamodb_check import ACCOUNT, DATA_TABLE, REGION, remove_partition


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    client = session.client("dynamodb")
    table = session.resource("dynamodb").Table(DATA_TABLE)
    writer = CapsuleWriter(DATA_TABLE, frozenset({"BILLING", "USAGE"}), client=client)
    inventory = CapsuleInventory(DATA_TABLE, frozenset({"BILLING", "USAGE"}), client=client)
    marker_pk = f"CAPSULE_TEST#{uuid.uuid4()}"
    serializer = TypeSerializer()
    locator = writer.create()
    try:
        def put(purpose):
            try:
                writer.put_entry(locator=locator, purpose=purpose, epoch="2026-11",
                                 ciphertext=b"synthetic-not-a-real-ciphertext", expected_revision=1)
                return purpose, "CREATED"
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                return purpose, "CONFLICT"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(put, ("BILLING", "USAGE")))
        if sorted(status for _, status in outcomes) != ["CONFLICT", "CREATED"]:
            raise AssertionError("Concurrent writes did not produce one success and one conflict")
        request_id = str(uuid.uuid4())
        action = writer.lock_action(locator=locator, request_id=request_id, expected_revision=2)
        marker = {"pk": marker_pk, "sk": "TOMBSTONE", "provenance": "SYNTHETIC", "request_id": request_id}
        client.transact_write_items(TransactItems=[action, {"Put": {"TableName": DATA_TABLE,
            "Item": {name: serializer.serialize(value) for name, value in marker.items()},
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}}])
        staged = []
        result = inventory.stage(locator, lambda sk, payload: staged.append((sk, payload)))
        if result.revision != 3 or result.entry_count != 1 or len(staged) != 1 or \
                staged[0][1] != b"synthetic-not-a-real-ciphertext":
            raise AssertionError("Locked inventory does not match the successful write")
        try:
            writer.put_entry(locator=locator, purpose="BILLING", epoch="2026-12",
                             ciphertext=b"synthetic", expected_revision=3)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Locked Capsule accepted a new entry")
        if table.get_item(Key={"pk": marker_pk, "sk": "TOMBSTONE"}, ConsistentRead=True).get("Item") is None:
            raise AssertionError("Synthetic lock marker was not committed")
    finally:
        remove_partition(table, locator)
        remove_partition(table, marker_pk)
    print(json.dumps({"competing_writes": "verified", "locked_inventory": "verified",
                      "locked_write_rejected": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
