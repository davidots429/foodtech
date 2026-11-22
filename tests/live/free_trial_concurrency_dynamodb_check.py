"""Opt-in contention test of one synthetic tenant's monthly trial limit."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import boto3

from foodtech_ai_gateway.registry import DynamoRegistry

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
TABLE = "foodtech-ai-execution-registry-dev"


def main():
    if os.environ.get("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use the foodtech development profile")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise SystemExit("Unexpected AWS account")
    resource = session.resource("dynamodb")
    table = resource.Table(TABLE)
    tenant = f"synthetic-trial-race-{uuid4().hex}"
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    key = {"pk": f"LIMIT#{period}#TENANT#{tenant}", "sk": "META"}

    def ensure(index):
        # Each thread gets its own boto3 resource, which is not thread-safe.
        thread_session = boto3.Session(profile_name="foodtech", region_name=REGION)
        registry = DynamoRegistry(TABLE, period, resource=thread_session.resource("dynamodb"),
                                  client=thread_session.client("dynamodb"), auth_table="foodtech-auth-sessions-dev")
        registry.ensure_trial_tenant_limit(tenant, Decimal("0"), 3 if index % 2 else 7)

    auth = session.resource("dynamodb").Table("foodtech-auth-sessions-dev")
    auth_key = {"pk": f"USER#{tenant}"}
    auth.put_item(Item=auth_key, ConditionExpression="attribute_not_exists(pk)")
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(ensure, range(16)))
        first = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not first or first.get("calls_remaining") not in (3, 7) or \
                first.get("remaining_usd") != 0 or first.get("origin") != "FREE_TRIAL" or first.get("version") != 1:
            raise AssertionError("Concurrent creation did not preserve a single initial limit")
        table.update_item(Key=key, UpdateExpression="SET calls_remaining = :zero, #v = :version",
                          ConditionExpression="attribute_exists(pk)", ExpressionAttributeNames={"#v": "version"},
                          ExpressionAttributeValues={":zero": 0, ":version": 2})
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(ensure, range(16)))
        after = table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not after or after.get("calls_remaining") != 0 or after.get("version") != 2:
            raise AssertionError("Existing exhausted trial limit was refilled or overwritten")
        print("Concurrent trial creation and exhausted-limit preservation verified")
    finally:
        auth.delete_item(Key=auth_key)
        table.delete_item(Key=key)
        if table.get_item(Key=key, ConsistentRead=True).get("Item"):
            raise RuntimeError("Synthetic trial limit cleanup failed")
        print("Synthetic trial limit cleanup verified")


if __name__ == "__main__":
    main()
