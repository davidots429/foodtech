"""Opt-in STS/KMS context isolation probe using an already-provisioned test role.

This checks KMS behavior only; it does not establish who may choose a session tag.
The caller must have Encrypt permission and permission to assume the test role.
No keys, roles or other AWS resources are created or modified by this script.
"""
import argparse
import os
import uuid

import boto3
from botocore.exceptions import ClientError

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"


def check(sts, kms, scoped_kms_factory, role_arn: str, key_arn: str) -> None:
    if sts.get_caller_identity()["Account"] != ACCOUNT:
        raise ValueError("Unexpected AWS account")
    if not role_arn.startswith(f"arn:aws:iam::{ACCOUNT}:role/") or not key_arn.startswith(
            f"arn:aws:kms:{REGION}:{ACCOUNT}:key/"):
        raise ValueError("Test role and key must belong to the development account and region")
    marker = uuid.uuid4().hex
    tenant_a, tenant_b = f"synthetic-a-{marker}", f"synthetic-b-{marker}"
    context_a = {"tenant_pseudonym": tenant_a, "purpose": "TENANT_TAG_TEST"}
    context_b = {"tenant_pseudonym": tenant_b, "purpose": "TENANT_TAG_TEST"}
    # Both ciphertexts are encrypted with their actual, matching contexts. The
    # negative request therefore cannot pass merely because KMS detected an
    # incorrect encryption context before evaluating tenant access.
    ciphertext_a = kms.encrypt(KeyId=key_arn, Plaintext=b"synthetic-a", EncryptionContext=context_a)["CiphertextBlob"]
    ciphertext_b = kms.encrypt(KeyId=key_arn, Plaintext=b"synthetic-b", EncryptionContext=context_b)["CiphertextBlob"]
    credentials = sts.assume_role(RoleArn=role_arn, RoleSessionName=f"foodtech-poc-{marker}",
                                  DurationSeconds=900, Tags=[{"Key": "tenant_pseudonym", "Value": tenant_a}])["Credentials"]
    scoped = scoped_kms_factory(credentials)
    if scoped.decrypt(KeyId=key_arn, CiphertextBlob=ciphertext_a, EncryptionContext=context_a)["Plaintext"] != b"synthetic-a":
        raise AssertionError("Matching tenant decrypt returned unexpected plaintext")
    try:
        scoped.decrypt(KeyId=key_arn, CiphertextBlob=ciphertext_b, EncryptionContext=context_b)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "AccessDeniedException":
            raise
    else:
        raise AssertionError("Cross-tenant decrypt was unexpectedly allowed")
    print("KMS matching tenant allowed and other tenant denied; tag issuance and deployed callers remain unverified")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-role-arn", required=True)
    parser.add_argument("--test-key-arn", required=True)
    args = parser.parse_args()
    if os.environ.get("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    def scoped(credentials):
        return boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"], region_name=REGION).client("kms")
    check(session.client("sts"), session.client("kms"), scoped, args.test_role_arn, args.test_key_arn)


if __name__ == "__main__":
    main()
