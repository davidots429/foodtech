"""Synthetic KMS-wrapped user key and encrypted Capsule entry in the dev table.

Uses an existing credential test key for compatibility only. This does not test
tenant-scoped IAM, a Capsule CMK pool, real users or deletion/backup guarantees.
"""
import json
import os
import uuid

import boto3
from boto3.dynamodb.types import TypeSerializer
from boto3.dynamodb.conditions import Attr
from foodtech_business_api.capsule_inventory import CapsuleInventory, CapsuleInventoryError
from foodtech_business_api.capsule_writer import CapsuleWriter
from foodtech_capsule_crypto.contract import Entry, Envelope
from foodtech_capsule_crypto.user_entry import UserEntryEnvelope, open_entry, seal_entry
from foodtech_capsule_crypto.user_key import UserKeyGenerator, UserKeyRecord, UserKeyUnwrapper
from capsule_user_key_kms_check import b64
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
    key_map = {("synthetic-v1", "shard-0"): key["Arn"]}
    purposes, tenant_purposes = frozenset({"BILLING"}), frozenset({"credential"})
    generator = UserKeyGenerator(key_map, tenant_purposes, kms_client=kms)
    unwrapper = UserKeyUnwrapper(key_map, tenant_purposes, kms_client=kms)
    tenant = f"psn:v1:credential:2026-11:v1:{b64(1)}"
    entry = Entry("BILLING", "2026-11", b64(2))
    record, encrypted = generator.generate(tenant=tenant, pool_version="synthetic-v1", shard_id="shard-0",
        use_key=lambda user_key, user_record: seal_entry(key=user_key, record=user_record, key_map=key_map,
            tenant=tenant, entry=entry, approved_purposes=purposes, approved_tenant_purposes=tenant_purposes,
            kms_client=kms))
    writer = CapsuleWriter(DATA_TABLE, purposes, client=client)
    inventory = CapsuleInventory(DATA_TABLE, purposes, client=client)
    table = session.resource("dynamodb").Table(DATA_TABLE)
    marker_pk, request_id = f"CAPSULE_TEST#{uuid.uuid4()}", str(uuid.uuid4())
    serializer = TypeSerializer()
    locator, create_action = writer.create_action()
    client.transact_write_items(TransactItems=[create_action])
    try:
        writer.attach_user_key(locator=locator, expected_revision=1, user_key_id=record.key_id,
            encrypted_dek=record.encrypted_dek, pool_version=record.pool_version, shard_id=record.shard_id,
            key_arn=record.key_arn)
        writer.put_entry(locator=locator, purpose=entry.purpose, epoch=entry.epoch, ciphertext=encrypted.entry.ciphertext,
            expected_revision=2, user_key_id=record.key_id, pool_version=record.pool_version,
            shard_id=record.shard_id, key_arn=record.key_arn)
        marker = {"pk": marker_pk, "sk": "TOMBSTONE", "provenance": "SYNTHETIC", "request_id": request_id}
        client.transact_write_items(TransactItems=[writer.lock_action(locator=locator, request_id=request_id,
            expected_revision=3), {"Put": {"TableName": DATA_TABLE,
            "Item": {name: serializer.serialize(value) for name, value in marker.items()},
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}}])
        meta = table.get_item(Key={"pk": locator, "sk": "META"}, ConsistentRead=True)["Item"]
        stored_key = UserKeyRecord(meta["user_key_id"], meta["pool_version"], meta["shard_id"],
                                   meta["key_arn"], bytes(meta["encrypted_user_dek"]))
        staged = []
        result = inventory.stage(locator, request_id, lambda sk, payload, metadata: staged.append((sk, payload, metadata)))
        if result.entry_count != 1 or result.revision != 4 or staged[0][0] != "ENTRY#BILLING#2026-11":
            raise AssertionError("Unexpected Capsule inventory")
        _, ciphertext, metadata = staged[0]
        user_key_id = metadata.pop("user_key_id")
        stored_entry = UserEntryEnvelope(user_key_id, Envelope(**metadata, ciphertext=ciphertext))
        restored = unwrapper.use(tenant=tenant, record=stored_key, use_key=lambda raw, key_record:
            open_entry(key=raw, record=key_record, key_map=key_map, tenant=tenant, envelope=stored_entry,
                expected_purpose="BILLING", expected_epoch="2026-11", approved_purposes=purposes,
                approved_tenant_purposes=tenant_purposes, kms_client=kms))
        if stored_key != record or restored != entry:
            raise AssertionError("Stored user key or Capsule entry did not round-trip")
        # Simulate a partial key removal only; no crypto-shredding claim is made.
        table.update_item(Key={"pk": locator, "sk": "META"}, UpdateExpression="REMOVE encrypted_user_dek",
                          ConditionExpression=Attr("deletion_request_id").eq(request_id))
        try:
            inventory.stage(locator, request_id, lambda *_: (_ for _ in ()).throw(
                AssertionError("Incomplete key metadata staged an entry")))
        except CapsuleInventoryError:
            pass
        else:
            raise AssertionError("Incomplete wrapped key metadata was accepted")
    finally:
        remove_partition(table, locator)
        remove_partition(table, marker_pk)
    print(json.dumps({"wrapped_user_key_storage": "verified", "capsule_entry_round_trip": "verified",
                      "partial_key_removal_rejected": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
