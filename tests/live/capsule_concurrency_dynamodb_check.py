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

from foodtech_business_api.capsule_inventory import CapsuleInventory, CapsuleInventoryError
from foodtech_business_api.capsule_writer import CapsuleWriter
from process_flow_dynamodb_check import ACCOUNT, DATA_TABLE, REGION, remove_partition

KEY_METADATA = {"pool_version": "synthetic-v1", "shard_id": "shard-0",
                "key_arn": "arn:aws:kms:ap-southeast-2:123456789012:key/synthetic",
                "user_key_id": "d9b699c2-61c7-462e-a65d-15f019a0cf13"}

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
    locator, create_action = writer.create_action()
    client.transact_write_items(TransactItems=[create_action])
    try:
        table.update_item(Key={"pk": locator, "sk": "META"}, UpdateExpression="SET pool_version = :v",
                          ExpressionAttributeValues={":v": "orphaned"})
        try:
            writer.attach_user_key(locator=locator, expected_revision=1, encrypted_dek=b"synthetic-wrapped-key", **KEY_METADATA)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        else:
            raise AssertionError("Partial key metadata was overwritten")
        table.update_item(Key={"pk": locator, "sk": "META"}, UpdateExpression="REMOVE pool_version")
        writer.attach_user_key(locator=locator, expected_revision=1, encrypted_dek=b"synthetic-wrapped-key", **KEY_METADATA)
        table.update_item(Key={"pk": locator, "sk": "META"}, UpdateExpression="REMOVE encrypted_user_dek")
        try:
            writer.put_entry(locator=locator, purpose="BILLING", epoch="2026-09", ciphertext=b"synthetic",
                             expected_revision=2, **KEY_METADATA)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Entry accepted without wrapped user key")
        table.update_item(Key={"pk": locator, "sk": "META"}, UpdateExpression="SET encrypted_user_dek = :key",
                          ExpressionAttributeValues={":key": b"synthetic-wrapped-key"})
        try:
            writer.put_entry(locator=locator, purpose="BILLING", epoch="2026-10", ciphertext=b"synthetic",
                expected_revision=2, **{**KEY_METADATA, "user_key_id": str(uuid.uuid4())})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Mismatched user key was accepted")
        def put(purpose):
            try:
                writer.put_entry(locator=locator, purpose=purpose, epoch="2026-11",
                                 ciphertext=b"synthetic-not-a-real-ciphertext", expected_revision=2, **KEY_METADATA)
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
        action = writer.lock_action(locator=locator, request_id=request_id, expected_revision=3)
        marker = {"pk": marker_pk, "sk": "TOMBSTONE", "provenance": "SYNTHETIC", "request_id": request_id}
        client.transact_write_items(TransactItems=[action, {"Put": {"TableName": DATA_TABLE,
            "Item": {name: serializer.serialize(value) for name, value in marker.items()},
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}}])
        staged = []
        result = inventory.stage(locator, request_id, lambda sk, payload, metadata: staged.append((sk, payload, metadata)))
        if result.revision != 4 or result.entry_count != 1 or len(staged) != 1 or \
                staged[0][1] != b"synthetic-not-a-real-ciphertext" or staged[0][2] != KEY_METADATA:
            raise AssertionError("Locked inventory does not match the successful write")
        try:
            inventory.stage(locator, str(uuid.uuid4()), lambda *_: (_ for _ in ()).throw(
                AssertionError("Wrong request staged an entry")))
        except CapsuleInventoryError:
            pass
        else:
            raise AssertionError("Wrong deletion request read the locked Capsule")
        try:
            writer.put_entry(locator=locator, purpose="BILLING", epoch="2026-12",
                             ciphertext=b"synthetic", expected_revision=4, **KEY_METADATA)
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
    print(json.dumps({"partial_key_rejected": "verified", "missing_wrapped_key_rejected": "verified",
                      "wrong_user_key_rejected": "verified", "competing_writes": "verified", "locked_inventory": "verified",
                      "wrong_request_rejected": "verified", "locked_write_rejected": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
