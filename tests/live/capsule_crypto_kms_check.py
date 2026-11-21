"""Synthetic Encryption SDK compatibility check using an existing dev KMS key.

This does not establish a Capsule key pool, tenant STS tags or service IAM roles.
No ciphertext is persisted and no real identity data is used.
"""
import base64
import json
import os
from dataclasses import replace

import boto3
from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.reader import CapsuleEntryReader
from foodtech_capsule_crypto.writer import CapsuleEntryWriter
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
    key = kms.describe_key(KeyId="alias/foodtech/ai-credential-dev-2026-11")["KeyMetadata"]
    if key["KeyState"] != "Enabled" or key["KeySpec"] != "SYMMETRIC_DEFAULT" or key["KeyUsage"] != "ENCRYPT_DECRYPT":
        raise SystemExit("Synthetic compatibility key is not an enabled symmetric encryption key")
    key_map = {("synthetic-v1", "shard-0"): key["Arn"]}
    purposes = frozenset({"BILLING"})
    writer = CapsuleEntryWriter(purposes, key_map, kms_client=kms, approved_tenant_purposes=frozenset({"credential"}))
    reader = CapsuleEntryReader(purposes, key_map, kms_client=kms, approved_tenant_purposes=frozenset({"credential"}))
    tenant = f"psn:v1:credential:2026-11:v1:{b64(1)}"
    entry = Entry("BILLING", "2026-11", b64(2))
    def seal():
        return writer.seal(tenant_pseudonym=tenant, entry=entry, pool_version="synthetic-v1", shard_id="shard-0")
    first, second = seal(), seal()
    if first.ciphertext == second.ciphertext or reader.open(tenant_pseudonym=tenant, envelope=first,
            expected_purpose="BILLING", expected_epoch="2026-11") != entry:
        raise AssertionError("Synthetic Capsule encryption round trip failed")
    tampered = replace(first, ciphertext=first.ciphertext[:-1] + bytes([first.ciphertext[-1] ^ 1]))
    cases = ((f"psn:v1:credential:2026-11:v1:{b64(3)}", first, "2026-11"),
             (tenant, first, "2026-12"), (tenant, tampered, "2026-11"))
    for test_tenant, envelope, epoch in cases:
        try:
            reader.open(tenant_pseudonym=test_tenant, envelope=envelope, expected_purpose="BILLING", expected_epoch=epoch)
        except ValueError:
            pass
        else:
            raise AssertionError("Mismatched or modified Capsule entry was accepted")
    print(json.dumps({"sdk_kms_round_trip": "verified", "fresh_ciphertexts": "verified",
                      "context_and_tamper_rejection": "verified", "persisted": False}))


if __name__ == "__main__":
    main()
