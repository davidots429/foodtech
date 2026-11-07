"""Synthetic browser RSA -> KMS -> ESDK -> two-table registration round trip."""

import base64
import json
from uuid import uuid4

import boto3
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.vault import DynamoCredentialVault
from foodtech_credential_registration.registration import (
    CredentialRegistrar, TenantPseudonymizer, TransferKey,
)
from foodtech_credential_registration.storage import CredentialWriter


REGION = "ap-southeast-2"
ACCOUNT = "573693340100"
VAULT = "foodtech-ai-credential-vault-dev"
REGISTRY = "foodtech-ai-execution-registry-dev"
STACK = "FoodtechCredentialVault-dev"


def main() -> None:
    identity = boto3.client("sts", region_name=REGION).get_caller_identity()
    if identity["Account"] != ACCOUNT:
        raise RuntimeError("unexpected AWS account")
    stack_response = boto3.client("cloudformation", region_name=REGION).describe_stacks(
        StackName=STACK)
    stack = {item["OutputKey"]: item["OutputValue"]
             for item in stack_response["Stacks"][0]["Outputs"]}
    resource = boto3.resource("dynamodb", region_name=REGION)
    kms = boto3.client("kms", region_name=REGION)
    transfer = TransferKey(stack["CredentialTransferKeyArn"], "2026-11", kms_client=kms)
    descriptor = transfer.public_descriptor()
    public = serialization.load_der_public_key(base64.b64decode(descriptor["public_key_spki_base64"]))
    dummy = b"synthetic-registration-key"
    encrypted = public.encrypt(dummy, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                    algorithm=hashes.SHA256(), label=None))
    registrar = CredentialRegistrar(
        CredentialWriter(VAULT, "dev", stack["CredentialStorageKeyArn"],
                         resource=resource, kms_client=kms),
        REGISTRY, transfer,
        TenantPseudonymizer(stack["CredentialPseudonymKeyArn"], "v1", kms_client=kms),
        dynamodb_client=boto3.client("dynamodb", region_name=REGION))
    credential_id = None
    try:
        tenant_id = f"synthetic-subject-{uuid4()}"
        credential_id = registrar.register(tenant_id, "openai", descriptor["key_version"],
                                           base64.b64encode(encrypted).decode())
        record = DynamoRegistry(REGISTRY, "2026-11", resource=resource).credential(credential_id)
        assert record and record.tenant_id == tenant_id
        vault = DynamoCredentialVault(VAULT, "dev", {stack["CredentialStorageKeyArn"]},
                                      resource=resource, kms_client=kms)
        buffer = vault.decrypt(vault.read(record))
        try:
            assert bytes(buffer) == dummy
        finally:
            buffer[:] = b"\x00" * len(buffer)
        print(json.dumps({"transfer_algorithm": descriptor["algorithm"],
                          "atomic_registration": True, "gateway_decrypt": True,
                          "secret_in_response": False}))
    finally:
        if credential_id:
            key = {"pk": f"CREDENTIAL#{credential_id}", "sk": "META"}
            resource.Table(VAULT).delete_item(Key=key)
            resource.Table(REGISTRY).delete_item(Key=key)


if __name__ == "__main__":
    main()
