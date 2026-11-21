"""Competing synthetic reservations against an isolated future-period budget."""
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import boto3

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.registry import DynamoRegistry, RegistryConflict

ACCOUNT, REGION, TABLE = "573693340100", "ap-southeast-2", "foodtech-ai-execution-registry-dev"


def main():
    if os.environ.get("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    resource = session.resource("dynamodb")
    table = resource.Table(TABLE)
    period = f"{secrets.randbelow(4000) + 5000:04d}-{secrets.randbelow(12) + 1:02d}"
    tenant, provider = f"synthetic-reserve-{uuid4().hex}", f"synthetic-{uuid4().hex}"
    ids = [str(uuid4()) for _ in range(8)]
    registry = DynamoRegistry(TABLE, period, resource=resource, client=session.client("dynamodb"))
    keys = []
    barrier = threading.Barrier(len(ids))
    try:
        for scope, identifier, calls in (("TENANT", tenant, 3), ("PROVIDER", provider, None),
                                         ("PROJECT", "GLOBAL", None)):
            registry.create_limit(scope, identifier, Decimal("0"), calls_remaining=calls)
            keys.append({"pk": f"LIMIT#{period}#{scope}#{identifier}", "sk": "META"})
        for execution_id in ids:
            execution = Execution(execution_id, tenant, Feature.GENERAL_GUIDE, "synthetic-model", None,
                                  "SYNTHETIC", Decimal("0"))
            registry.create_execution(execution)
            keys.append({"pk": f"EXEC#{execution_id}", "sk": "META"})

        def attempt(execution_id):
            thread_session = boto3.Session(profile_name="foodtech", region_name=REGION)
            worker = DynamoRegistry(TABLE, period, resource=thread_session.resource("dynamodb"),
                                    client=thread_session.client("dynamodb"))
            worker.begin_request()
            execution = worker.execution(execution_id)
            worker.budget(tenant, provider)
            barrier.wait(timeout=30)
            try:
                worker.reserve(execution, provider)
                return True
            except RegistryConflict:
                return False

        with ThreadPoolExecutor(max_workers=len(ids)) as executor:
            outcomes = list(executor.map(attempt, ids))
        accepted = sum(outcomes)
        remaining = int(table.get_item(Key=keys[0], ConsistentRead=True)["Item"]["calls_remaining"])
        reserved = sum(table.get_item(Key=key, ConsistentRead=True)["Item"]["state"] == "RESERVED" for key in keys[3:])
        if accepted < 1 or accepted > 3 or reserved != accepted or remaining != 3 - accepted:
            raise AssertionError("Concurrent reservations exceeded or disagreed with tenant limit")
        print(f"Concurrent transaction reservations verified: {accepted} accepted, {remaining} remaining")
        for execution_id, succeeded in zip(ids, outcomes):
            if succeeded:
                continue
            registry.begin_request()
            execution = registry.execution(execution_id)
            registry.budget(tenant, provider)
            try:
                registry.reserve(execution, provider)
                accepted += 1
            except RegistryConflict:
                pass
        remaining = int(table.get_item(Key=keys[0], ConsistentRead=True)["Item"]["calls_remaining"])
        reserved = sum(table.get_item(Key=key, ConsistentRead=True)["Item"]["state"] == "RESERVED" for key in keys[3:])
        if accepted != 3 or reserved != 3 or remaining != 0:
            raise AssertionError("Retry did not consume exactly the remaining calls")
        print("Freshly authorized retries consumed only the remaining budget")
        execution_id = next(key["pk"].removeprefix("EXEC#") for key in keys[3:]
                            if table.get_item(Key=key, ConsistentRead=True)["Item"]["state"] == "RESERVED")
        keys.append({"pk": f"EXEC#{execution_id}", "sk": "RESULT"})
        original_client = registry.client
        class ConflictOnce:
            def __getattr__(self, name):
                return getattr(original_client, name)
            def transact_write_items(self, **kwargs):
                table.update_item(Key=keys[1], UpdateExpression="ADD #v :one",
                                  ExpressionAttributeNames={"#v": "version"},
                                  ExpressionAttributeValues={":one": 1})
                return original_client.transact_write_items(**kwargs)
        registry.client = ConflictOnce()
        registry.begin_request()
        try:
            registry.finish(execution_id, succeeded=True, actual_cost_usd=Decimal("0"), response_text="synthetic")
        except RegistryConflict:
            pass
        else:
            raise AssertionError("Expected settlement conflict")
        registry.client = original_client
        registry.begin_request()
        registry.finish(execution_id, succeeded=True, actual_cost_usd=Decimal("0"), response_text="synthetic")
        if table.get_item(Key={"pk": f"EXEC#{execution_id}", "sk": "META"}, ConsistentRead=True)["Item"]["state"] != "SUCCEEDED":
            raise AssertionError("Settlement retry did not succeed")
        print("Settlement after a known conditional conflict verified")
    finally:
        for execution_id in ids:
            table.delete_item(Key={"pk": f"ACTIVE#{tenant}", "sk": f"EXEC#{execution_id}"})
        for key in reversed(keys):
            table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get("Item") for key in keys):
            raise RuntimeError("Synthetic reservation cleanup failed")
        print("Synthetic reservation records removed")


if __name__ == "__main__":
    main()
