"""Validate synthetic Vault/Capsule creation and guarded snapshot races in dev.

Existing dev tables stand in for separate security domains. No real user IDs,
credentials or plaintext personal data are used. This is not an IAM/KMS test.
"""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from foodtech_business_api.guarded_deletion import GuardedDeletion
from foodtech_business_api.identity_vault import IdentityVault, IdentityVaultConflict
from process_flow_dynamodb_check import ACCOUNT, REGION, remove_partition


AUTH = "foodtech-auth-sessions-dev"
VAULT = "foodtech-cooking-jobs-dev"
CAPSULE = "foodtech-data-flow-dev"
REGISTRY = "foodtech-ai-execution-registry-dev"


def main():
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    client, dynamo = session.client("dynamodb"), session.resource("dynamodb")
    auth, vault, capsule, registry = (dynamo.Table(name) for name in (AUTH, VAULT, CAPSULE, REGISTRY))
    subject = str(uuid.uuid4())
    auth_key = {"pk": f"USER#{subject}"}
    vault_service = IdentityVault(auth_table=AUTH, vault_table=VAULT, capsule_table=CAPSULE,
                                  approved_purposes=frozenset({"BILLING"}), client=client)
    prepared = [vault_service.prepare(authenticated_subject=subject, request_id=str(uuid.uuid4()), expected_epoch=0)
                for _ in range(2)]
    stale_id, delete_id = str(uuid.uuid4()), str(uuid.uuid4())
    if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item") or any(
            vault.query(KeyConditionExpression=Key("pk").eq(value.vault_id), ConsistentRead=True).get("Items") or
            capsule.query(KeyConditionExpression=Key("pk").eq(value.locator), ConsistentRead=True).get("Items")
            for value in prepared) or any(registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{value}"},
                                                             ConsistentRead=True).get("Item")
                                         for value in (stale_id, delete_id)):
        raise SystemExit("Synthetic identifiers already exist")
    auth_created = False
    try:
        try:
            vault_service.commit(authenticated_subject=subject, prepared=prepared[0])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Missing principal was allowed to create identity")
        if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item") or \
                vault.get_item(Key={"pk": prepared[0].vault_id, "sk": "META"}, ConsistentRead=True).get("Item") or \
                capsule.get_item(Key={"pk": prepared[0].locator, "sk": "META"}, ConsistentRead=True).get("Item"):
            raise AssertionError("Missing principal left identity records behind")
        auth.put_item(Item={**auth_key, "epoch": 0, "provenance": "SYNTHETIC"},
                      ConditionExpression=Attr("pk").not_exists())
        auth_created = True
        def provision(value):
            try:
                vault_service.commit(authenticated_subject=subject, prepared=value)
                return value, "CREATED"
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                return value, "CONFLICT"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(provision, prepared))
        if sorted(outcome for _, outcome in outcomes) != ["CONFLICT", "CREATED"]:
            raise AssertionError("Identity creation did not produce exactly one winner")
        winner = next(value for value, outcome in outcomes if outcome == "CREATED")
        loser = next(value for value, outcome in outcomes if outcome == "CONFLICT")
        if vault.get_item(Key={"pk": loser.vault_id, "sk": "META"}, ConsistentRead=True).get("Item") or \
                capsule.get_item(Key={"pk": loser.locator, "sk": "META"}, ConsistentRead=True).get("Item"):
            raise AssertionError("Losing identity creation left an orphan")
        vault_service.commit(authenticated_subject=subject, prepared=winner)
        vault_service.verify_committed(authenticated_subject=subject, prepared=winner)
        try:
            vault_service.verify_committed(authenticated_subject=subject,
                                           prepared=replace(winner, request_id=str(uuid.uuid4())))
        except IdentityVaultConflict:
            pass
        else:
            raise AssertionError("Different request ID was accepted as the committed identity")
        try:
            vault_service.verify_committed(authenticated_subject=subject, prepared=loser)
        except IdentityVaultConflict:
            pass
        else:
            raise AssertionError("Losing request was accepted as the committed identity")
        snapshot, internal_id = vault_service.resolve_deletion(authenticated_subject=subject, expected_epoch=0)
        try:
            if bytes(internal_id) != winner.internal_id.encode("utf-8") or snapshot.capsule_locator != winner.locator:
                raise AssertionError("Resolved identity does not match the atomic creation")
        finally:
            internal_id[:] = b"\x00" * len(internal_id)
        auth.update_item(Key=auth_key, UpdateExpression="ADD #epoch :one",
                         ExpressionAttributeNames={"#epoch": "epoch"}, ExpressionAttributeValues={":one": 1})
        deletion = GuardedDeletion(auth_table=AUTH, registry_table=REGISTRY, vault_table=VAULT,
                                   capsule_table=CAPSULE, approved_purposes=frozenset({"BILLING"}), client=client)
        args = {"subject": subject, "encrypted_user_id": b"synthetic-id-ciphertext",
                "encrypted_capsule_locator": b"synthetic-locator-ciphertext", "requested_at": "2026-11-21T00:00:00Z",
                "policy_version": "synthetic-v1"}
        try:
            deletion.lock_and_register(**args, request_id=stale_id, snapshot=snapshot)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Old authentication epoch was accepted")
        principal_after_stale = auth.get_item(Key=auth_key, ConsistentRead=True)["Item"]
        if principal_after_stale.get("suspended") or int(principal_after_stale["epoch"]) != 1 or \
                registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{stale_id}"}, ConsistentRead=True).get("Item") or \
                vault.get_item(Key={"pk": winner.vault_id, "sk": "META"}, ConsistentRead=True)["Item"]["state"] != "ACTIVE" or \
                capsule.get_item(Key={"pk": winner.locator, "sk": "META"}, ConsistentRead=True)["Item"]["state"] != "ACTIVE":
            raise AssertionError("Stale authentication left a partial deletion lock")
        refreshed, internal_id = vault_service.resolve_deletion(authenticated_subject=subject, expected_epoch=1)
        internal_id[:] = b"\x00" * len(internal_id)
        deletion.lock_and_register(**args, request_id=delete_id, snapshot=refreshed)
        if registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{delete_id}"}, ConsistentRead=True).get("Item") is None:
            raise AssertionError("Guarded deletion registration was not recorded")
    finally:
        for request_id in (stale_id, delete_id):
            key = {"pk": "REGISTRY", "sk": f"REQ#{request_id}"}
            item = registry.get_item(Key=key, ConsistentRead=True).get("Item")
            if item:
                if item.get("request_id") != request_id or item.get("policy_version") != "synthetic-v1":
                    raise RuntimeError("Refusing to delete an unexpected registry record")
                registry.delete_item(Key=key, ConditionExpression=Attr("request_id").eq(request_id) &
                                     Attr("policy_version").eq("synthetic-v1"))
                if registry.get_item(Key=key, ConsistentRead=True).get("Item"):
                    raise RuntimeError("Synthetic registry cleanup failed")
        for value in prepared:
            key = {"pk": value.vault_id, "sk": "META"}
            item = vault.get_item(Key=key, ConsistentRead=True).get("Item")
            if item:
                if item.get("internal_user_id") != value.internal_id or item.get("capsule_locator") != value.locator:
                    raise RuntimeError("Refusing to delete an unexpected Vault record")
                remove_partition(capsule, value.locator)
                remove_partition(vault, value.vault_id)
        if auth_created:
            auth.delete_item(Key=auth_key, ConditionExpression=Attr("provenance").eq("SYNTHETIC"))
            if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item"):
                raise RuntimeError("Synthetic auth cleanup failed")
    print(json.dumps({"missing_principal_rejected": "verified", "atomic_provisioning": "verified", "lost_response_verification": "verified", "stale_auth_rejected": "verified",
                      "guarded_deletion_registration": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
