"""Validate guarded deletion transaction races using isolated synthetic dev items.

Existing development tables stand in for four separate security domains. This
does not verify production IAM, a deployed Vault, encryption or a deletion saga.
"""
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from foodtech_business_api.capsule_writer import CapsuleWriter
from foodtech_business_api.guarded_deletion import GuardedDeletion, VaultSnapshot
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
    subject, vault_id = str(uuid.uuid4()), str(uuid.uuid4())
    stale_id, stale_capsule_id, first_id, second_id = (str(uuid.uuid4()) for _ in range(4))
    ids = (stale_id, stale_capsule_id, first_id, second_id)
    auth_key = {"pk": f"USER#{subject}"}
    vault_key = {"pk": vault_id, "sk": "META"}
    writer = CapsuleWriter(CAPSULE, frozenset({"BILLING"}), client=client)
    locator, create_action = writer.create_action()
    if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item") or \
            vault.query(KeyConditionExpression=Key("pk").eq(vault_id), ConsistentRead=True).get("Items") or \
            capsule.query(KeyConditionExpression=Key("pk").eq(locator), ConsistentRead=True).get("Items") or \
            any(registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{value}"},
                                  ConsistentRead=True).get("Item") for value in ids):
        raise SystemExit("Synthetic identifiers already exist")
    auth_created = vault_created = capsule_created = False
    try:
        auth.put_item(Item={**auth_key, "epoch": 0, "provenance": "SYNTHETIC"},
                      ConditionExpression=Attr("pk").not_exists())
        auth_created = True
        vault.put_item(Item={**vault_key, "state": "ACTIVE", "revision": 2,
                             "capsule_locator": locator, "provenance": "SYNTHETIC"},
                       ConditionExpression=Attr("pk").not_exists())
        vault_created = True
        client.transact_write_items(TransactItems=[create_action])
        capsule_created = True
        store = GuardedDeletion(auth_table=AUTH, registry_table=REGISTRY, vault_table=VAULT,
                                capsule_table=CAPSULE, approved_purposes=frozenset({"BILLING"}), client=client)
        def call(request_id, vault_revision, capsule_revision=1):
            store.lock_and_register(subject=subject, request_id=request_id,
                snapshot=VaultSnapshot(subject, vault_id, vault_revision, locator, capsule_revision),
                encrypted_user_id=b"synthetic-ciphertext-id", encrypted_capsule_locator=b"synthetic-ciphertext-locator",
                requested_at="2026-11-21T00:00:00Z", policy_version="synthetic-v1")
        try:
            call(stale_id, 1)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Stale Vault revision was accepted")
        try:
            call(stale_capsule_id, 2, 2)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
        else:
            raise AssertionError("Stale Capsule revision was accepted")
        after_stale_vault = vault.get_item(Key=vault_key, ConsistentRead=True)["Item"]
        if auth.get_item(Key=auth_key, ConsistentRead=True)["Item"].get("suspended") or \
                after_stale_vault.get("state") != "ACTIVE" or int(after_stale_vault["revision"]) != 2 or \
                capsule.get_item(Key={"pk": locator, "sk": "META"}, ConsistentRead=True)["Item"]["state"] != "ACTIVE" or \
                any(registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{request_id}"},
                                      ConsistentRead=True).get("Item") for request_id in (stale_id, stale_capsule_id)):
            raise AssertionError("Stale transaction left a partial mutation")
        def compete(request_id):
            try:
                call(request_id, 2)
                return request_id, "CREATED"
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
                return request_id, "CONFLICT"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, (first_id, second_id)))
        if sorted(result for _, result in results) != ["CONFLICT", "CREATED"]:
            raise AssertionError("Competing registrations did not produce exactly one winner")
        winner = next(request_id for request_id, result in results if result == "CREATED")
        call(winner, 2)  # Same client token and exact transaction, within DynamoDB's retry window.
        principal = auth.get_item(Key=auth_key, ConsistentRead=True)["Item"]
        vault_item = vault.get_item(Key=vault_key, ConsistentRead=True)["Item"]
        capsule_item = capsule.get_item(Key={"pk": locator, "sk": "META"}, ConsistentRead=True)["Item"]
        tombstone = registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{winner}"}, ConsistentRead=True).get("Item")
        if not tombstone or principal.get("deletion_lock_request") != winner or int(principal["epoch"]) != 1 or \
                any(item.get("deletion_request_id") != winner or item.get("state") != "DELETION_LOCKED"
                    for item in (vault_item, capsule_item)):
            raise AssertionError("Lock and tombstone state differ")
        loser = second_id if winner == first_id else first_id
        if registry.get_item(Key={"pk": "REGISTRY", "sk": f"REQ#{loser}"}, ConsistentRead=True).get("Item"):
            raise AssertionError("Losing request created a tombstone")
    finally:
        for request_id in ids:
            key = {"pk": "REGISTRY", "sk": f"REQ#{request_id}"}
            item = registry.get_item(Key=key, ConsistentRead=True).get("Item")
            if item:
                if item.get("request_id") != request_id or item.get("policy_version") != "synthetic-v1":
                    raise RuntimeError("Refusing to delete an unexpected registry item")
                registry.delete_item(Key=key, ConditionExpression=Attr("request_id").eq(request_id) &
                                     Attr("policy_version").eq("synthetic-v1"))
                if registry.get_item(Key=key, ConsistentRead=True).get("Item"):
                    raise RuntimeError("Synthetic registry cleanup failed")
        if capsule_created:
            remove_partition(capsule, locator)
        if vault_created:
            remove_partition(vault, vault_id)
        if auth_created:
            auth.delete_item(Key=auth_key, ConditionExpression=Attr("provenance").eq("SYNTHETIC"))
            if auth.get_item(Key=auth_key, ConsistentRead=True).get("Item"):
                raise RuntimeError("Synthetic auth cleanup failed")
    print(json.dumps({"stale_vault_and_capsule_atomicity": "verified", "competing_registration": "verified",
                      "identical_retry": "verified", "cleanup": "verified"}))


if __name__ == "__main__":
    main()
