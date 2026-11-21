"""Synthetic SDK/KMS -> Capsule DynamoDB -> SDK/KMS round trip in dev.

Uses an existing credential test key only for compatibility; no real identity,
deployment role, tenant tag or dedicated Capsule key is tested.
"""
import json
import os
import uuid

import boto3
from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.capsule_inventory import CapsuleInventory
from foodtech_business_api.capsule_writer import CapsuleWriter
from foodtech_capsule_crypto.contract import Entry, Envelope
from foodtech_capsule_crypto.reader import CapsuleEntryReader
from foodtech_capsule_crypto.writer import CapsuleEntryWriter
from capsule_crypto_kms_check import b64
from process_flow_dynamodb_check import ACCOUNT, DATA_TABLE, REGION, remove_partition


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    kms, client = session.client("kms"), session.client("dynamodb")
    key = kms.describe_key(KeyId="alias/foodtech/ai-credential-dev-2026-11")["KeyMetadata"]
    if key["KeyState"] != "Enabled" or key["KeySpec"] != "SYMMETRIC_DEFAULT" or key["KeyUsage"] != "ENCRYPT_DECRYPT":
        raise SystemExit("Synthetic compatibility key is not enabled for symmetric encryption")
    purposes = frozenset({"BILLING"})
    key_map = {("synthetic-v1", "shard-0"): key["Arn"]}
    crypto = CapsuleEntryWriter(purposes, key_map, kms_client=kms, approved_tenant_purposes=frozenset({"credential"}))
    reader = CapsuleEntryReader(purposes, key_map, kms_client=kms, approved_tenant_purposes=frozenset({"credential"}))
    tenant = f"psn:v1:credential:2026-11:v1:{b64(1)}"
    entry = Entry("BILLING", "2026-11", b64(2))
    envelope = crypto.seal(tenant_pseudonym=tenant, entry=entry, pool_version="synthetic-v1", shard_id="shard-0")
    writer = CapsuleWriter(DATA_TABLE, purposes, client=client)
    inventory = CapsuleInventory(DATA_TABLE, purposes, client=client)
    table = session.resource("dynamodb").Table(DATA_TABLE)
    marker_pk, request_id = f"CAPSULE_TEST#{uuid.uuid4()}", str(uuid.uuid4())
    serializer = TypeSerializer()
    locator, create_action = writer.create_action()
    client.transact_write_items(TransactItems=[create_action])
    try:
        writer.put_entry(locator=locator, purpose=entry.purpose, epoch=entry.epoch, ciphertext=envelope.ciphertext,
                         expected_revision=1, pool_version=envelope.pool_version, shard_id=envelope.shard_id,
                         key_arn=envelope.key_arn)
        marker = {"pk": marker_pk, "sk": "TOMBSTONE", "provenance": "SYNTHETIC", "request_id": request_id}
        client.transact_write_items(TransactItems=[writer.lock_action(locator=locator, request_id=request_id,
            expected_revision=2), {"Put": {"TableName": DATA_TABLE,
            "Item": {name: serializer.serialize(value) for name, value in marker.items()},
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}}])
        staged = []
        result = inventory.stage(locator, request_id, lambda sk, ciphertext, metadata: staged.append((sk, ciphertext, metadata)))
        if result.entry_count != 1 or result.revision != 3 or staged[0][0] != "ENTRY#BILLING#2026-11":
            raise AssertionError("Unexpected Capsule inventory")
        _, ciphertext, metadata = staged[0]
        stored = Envelope(**metadata, ciphertext=ciphertext)
        if stored != envelope or reader.open(tenant_pseudonym=tenant, envelope=stored,
                expected_purpose="BILLING", expected_epoch="2026-11") != entry:
            raise AssertionError("Stored Capsule ciphertext or metadata did not round-trip")
    finally:
        remove_partition(table, locator)
        remove_partition(table, marker_pk)
    print(json.dumps({"kms_dynamodb_round_trip": "verified", "key_metadata": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
