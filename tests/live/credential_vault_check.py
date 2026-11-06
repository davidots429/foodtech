"""Opt-in live check of the separate registration and Gateway KMS paths.

Only a synthetic dummy key is written to the development Vault table. The
script deletes its item after checking encrypt/decrypt and context rejection.
"""

import asyncio
import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import boto3

from foodtech_ai_gateway.policy import Caller, Consent, Credential, Execution, Feature, Model, Tier
from foodtech_ai_gateway.provider import ProviderResult
from foodtech_ai_gateway.registry import DynamoRegistry
from foodtech_ai_gateway.service import GatewayService
from foodtech_ai_gateway.vault import DynamoCredentialVault, VaultFailure
from foodtech_credential_registration.storage import CredentialWriter


ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
STACK = "FoodtechCredentialVault-dev"
TABLE = "foodtech-ai-credential-vault-dev"
REGISTRY_TABLE = "foodtech-ai-execution-registry-dev"


class SyntheticProvider:
    async def generate(self, provider: str, model_id: str, prompt: str,
                       api_key: bytearray) -> ProviderResult:
        if (provider, model_id, prompt) != ("openai", "gpt-4o-mini", "synthetic prompt") or \
                api_key != b"dummy-provider-key-for-local-validation-only":
            raise AssertionError("Gateway passed unexpected provider, model, prompt or key")
        return ProviderResult("synthetic provider response", Decimal("0.01"), 3, 4)


def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for this development-only check")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    outputs = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    values = {item["OutputKey"]: item["OutputValue"] for item in outputs}
    if values.get("CredentialVaultTableName") != TABLE:
        raise SystemExit("Unexpected Vault table")
    key_arn = values["CredentialStorageKeyArn"]
    table = session.resource("dynamodb").Table(TABLE)
    registry_table = session.resource("dynamodb").Table(REGISTRY_TABLE)
    credential_id = f"synthetic-vault-check-{uuid4().hex}"
    tenant_pseudonym = f"synthetic-pseudo-{uuid4().hex}"
    tenant_id = f"synthetic-tenant-{uuid4().hex}"
    execution_id = str(uuid4())
    consent_id = str(uuid4())
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    registry = DynamoRegistry(REGISTRY_TABLE, period,
                              resource=session.resource("dynamodb"),
                              client=session.client("dynamodb"))
    item_key = {"pk": f"CREDENTIAL#{credential_id}", "sk": "META"}
    registry_keys: list[dict[str, str]] = []

    def created(kind: str, identifier: str) -> None:
        registry_keys.append({"pk": f"{kind}#{identifier}", "sk": "META"})

    secret = bytearray(b"dummy-provider-key-for-local-validation-only")
    writer = CredentialWriter(TABLE, "dev", key_arn,
                              resource=session.resource("dynamodb"),
                              kms_client=session.client("kms"))
    vault = DynamoCredentialVault(TABLE, "dev", {key_arn},
                                  resource=session.resource("dynamodb"),
                                  kms_client=session.client("kms"))
    try:
        writer.store(credential_id, tenant_pseudonym, "openai", secret,
                     pool_version="2026-11", shard_id="0")
        if any(secret):
            raise AssertionError("writer did not clear the input buffer")
        credential = Credential(credential_id, tenant_id, "openai", "ACTIVE",
                                None, tenant_pseudonym, f"vault:{credential_id}")
        record = vault.read(credential)
        plaintext = vault.decrypt(record)
        try:
            if plaintext != b"dummy-provider-key-for-local-validation-only":
                raise AssertionError("decrypted dummy key does not match")
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
        sdk_logger = logging.getLogger("aws_encryption_sdk")
        previous_level = sdk_logger.level
        sdk_logger.setLevel(logging.CRITICAL)
        try:
            try:
                vault.decrypt(replace(record, pool_version="wrong-pool"))
            except VaultFailure:
                context_rejected = True
            else:
                raise AssertionError("incorrect encryption context was accepted")
        finally:
            sdk_logger.setLevel(previous_level)

        now = datetime.now(timezone.utc)
        registry.create_model(Model("gpt-4o-mini", "openai", Tier.TIER_3,
                                    "ACTIVE", False, True, now, now))
        created("MODEL", "gpt-4o-mini")
        registry.create_credential(credential, vault_ref=f"vault:{credential_id}")
        created("CREDENTIAL", credential_id)
        registry.create_consent(Consent(consent_id, execution_id, tenant_id, "gpt-4o-mini",
                                        "EXTERNAL_TRANSFER", "synthetic-notice-v1", now,
                                        transmission_location="EXTERNAL",
                                        estimated_cost_usd=Decimal("0.05")))
        created("CONSENT", consent_id)
        registry.create_execution(Execution(execution_id, tenant_id, Feature.GENERAL_GUIDE,
                                             "gpt-4o-mini", credential_id, "SYNTHETIC",
                                             Decimal("0.05"),
                                             external_transfer_consent_id=consent_id))
        created("EXEC", execution_id)
        registry_keys.append({"pk": f"EXEC#{execution_id}", "sk": "RESULT"})
        for scope, identifier in (("TENANT", tenant_id), ("PROVIDER", "openai"),
                                  ("PROJECT", "GLOBAL")):
            registry.create_limit(scope, identifier, Decimal("1"),
                                  calls_remaining=1 if scope == "TENANT" else None)
            created("LIMIT", f"{period}#{scope}#{identifier}")
        outcome = asyncio.run(GatewayService(registry, vault=vault, provider=SyntheticProvider())
                              .execute_external(Caller("business-api", tenant_id),
                                                execution_id, "synthetic prompt"))
        if outcome.state != "SUCCEEDED" or outcome.response_text != "synthetic provider response" or \
                registry.execution_state(execution_id) != "SUCCEEDED" or \
                registry.execution_result(execution_id)["response_text"] != outcome.response_text or \
                registry.budget(tenant_id, "openai").calls_remaining != 0:
            raise AssertionError("external execution did not persist the expected state")
        print(json.dumps({"table": TABLE, "suite_id": "0x0578",
                          "commitment_policy": "REQUIRE_ENCRYPT_REQUIRE_DECRYPT",
                          "dummy_round_trip": True, "wrong_context_rejected": context_rejected,
                          "registry_state": outcome.state, "provider": "synthetic stub",
                          "remaining_calls": 0}))
    finally:
        for key in reversed(registry_keys):
            registry_table.delete_item(Key=key)
        table.delete_item(Key=item_key)


if __name__ == "__main__":
    main()
