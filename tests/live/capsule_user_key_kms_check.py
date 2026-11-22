"""Synthetic user-wrapped key and raw AES keyring compatibility in development.

No real tenant data is used or persisted. The existing credential test CMK is
not a Capsule pool and this does not validate STS, roles, deletion or backups.
"""
import json
import os
import base64
from dataclasses import replace

import boto3
from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.user_entry import open_entry, seal_entry
from foodtech_capsule_crypto.user_key import UserKeyGenerator, UserKeyUnwrapper
from process_flow_dynamodb_check import ACCOUNT, REGION


def b64(value):
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode("ascii").rstrip("=")


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    kms = session.client("kms")
    metadata = kms.describe_key(KeyId="alias/foodtech/ai-credential-dev-2026-11")["KeyMetadata"]
    if metadata["KeyState"] != "Enabled" or metadata["KeySpec"] != "SYMMETRIC_DEFAULT" or \
            metadata["KeyUsage"] != "ENCRYPT_DECRYPT":
        raise SystemExit("Synthetic compatibility key is not enabled for symmetric encryption")
    key_map = {("synthetic-v1", "shard-0"): metadata["Arn"]}
    tenant = f"psn:v1:credential:2026-11:v1:{b64(1)}"
    other = f"psn:v1:credential:2026-11:v1:{b64(3)}"
    purposes, tenant_purposes = frozenset({"BILLING"}), frozenset({"credential"})
    entry = Entry("BILLING", "2026-11", b64(2))
    generator = UserKeyGenerator(key_map, tenant_purposes, kms_client=kms)
    unwrapper = UserKeyUnwrapper(key_map, tenant_purposes, kms_client=kms)
    observed_buffers = []
    def encrypt(key, record):
        observed_buffers.append(key)
        return seal_entry(key=key, record=record, key_map=key_map, tenant=tenant, entry=entry,
                          approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)
    record, encrypted = generator.generate(tenant=tenant, pool_version="synthetic-v1", shard_id="shard-0", use_key=encrypt)
    if not observed_buffers or any(observed_buffers[0]):
        raise AssertionError("Generated mutable key buffer was not cleared")
    def decrypt(key, user_record):
        observed_buffers.append(key)
        return open_entry(key=key, record=user_record, key_map=key_map, tenant=tenant, envelope=encrypted,
                          expected_purpose="BILLING", expected_epoch="2026-11", approved_purposes=purposes,
                          approved_tenant_purposes=tenant_purposes, kms_client=kms)
    if unwrapper.use(tenant=tenant, record=record, use_key=decrypt) != entry or any(observed_buffers[-1]):
        raise AssertionError("User key round trip or buffer clearing failed")
    for test_tenant, test_record in ((other, record), (tenant, replace(record, encrypted_dek=b"bad"))):
        try:
            unwrapper.use(tenant=test_tenant, record=test_record, use_key=decrypt)
        except ValueError:
            pass
        else:
            raise AssertionError("Wrong tenant or modified encrypted user key was accepted")
    print(json.dumps({"wrapped_user_key_round_trip": "verified", "wrong_context_rejected": "verified",
                      "modified_key_rejected": "verified", "mutable_buffers_cleared": "verified", "persisted": False}))


if __name__ == "__main__":
    main()
