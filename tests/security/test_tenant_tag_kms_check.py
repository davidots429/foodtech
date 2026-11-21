import importlib.util
import unittest
from pathlib import Path

from botocore.exceptions import ClientError

spec = importlib.util.spec_from_file_location("tenant_tag_probe", Path(__file__).parents[1] / "live" / "tenant_tag_kms_check.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
ROLE = f"arn:aws:iam::{probe.ACCOUNT}:role/foodtech-tenant-tag-poc-test"
KEY = f"arn:aws:kms:{probe.REGION}:{probe.ACCOUNT}:key/12345678-1234-1234-1234-123456789abc"


class Sts:
    def get_caller_identity(self): return {"Account": probe.ACCOUNT}
    def assume_role(self, **kwargs):
        self.tag = kwargs["Tags"][0]["Value"]
        return {"Credentials": {}}


class Kms:
    def __init__(self, sts, negative="deny"): self.sts, self.negative = sts, negative
    def describe_key(self, **_): return {"KeyMetadata": {"Arn": KEY, "KeyState": "Enabled", "KeySpec": "SYMMETRIC_DEFAULT", "KeyUsage": "ENCRYPT_DECRYPT"}}
    def get_paginator(self, name):
        assert name == "list_resource_tags"
        return self
    def paginate(self, **_): return [{"Tags": [{"TagKey": "foodtech-purpose", "TagValue": "tenant-tag-poc"}]}]
    def encrypt(self, *, Plaintext, EncryptionContext, **_): return {"CiphertextBlob": (Plaintext, EncryptionContext)}
    def decrypt(self, *, CiphertextBlob, EncryptionContext, **_):
        plaintext, original_context = CiphertextBlob
        if original_context != EncryptionContext: raise AssertionError("incorrect test context")
        if EncryptionContext["tenant_pseudonym"] != self.sts.tag:
            if self.negative == "allow": return {"Plaintext": plaintext}
            code = "InvalidCiphertextException" if self.negative == "invalid" else "AccessDeniedException"
            raise ClientError({"Error": {"Code": code, "Message": "test"}}, "Decrypt")
        return {"Plaintext": plaintext}


class TestTenantProbe(unittest.TestCase):
    def run_probe(self, mode):
        sts = Sts(); kms = Kms(sts, mode)
        probe.check(sts, kms, lambda _: kms, ROLE, KEY)

    def test_matching_context_allowed_and_other_tenant_denied(self): self.run_probe("deny")
    def test_cross_tenant_allow_is_failure(self):
        with self.assertRaises(AssertionError): self.run_probe("allow")
    def test_invalid_ciphertext_is_not_counted_as_authorization_denial(self):
        with self.assertRaises(ClientError): self.run_probe("invalid")
    def test_rejects_other_accounts_before_using_role(self):
        sts = Sts(); kms = Kms(sts)
        with self.assertRaises(ValueError): probe.check(sts, kms, lambda _: kms, ROLE.replace(probe.ACCOUNT, "000000000000"), KEY)
    def test_rejects_non_test_key_before_encryption(self):
        sts = Sts(); kms = Kms(sts)
        kms.paginate = lambda **_: [{"Tags": []}]
        with self.assertRaises(ValueError): probe.check(sts, kms, lambda _: kms, ROLE, KEY)


if __name__ == "__main__": unittest.main()
