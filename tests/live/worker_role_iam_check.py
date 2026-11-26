"""Read-only identity-policy check after the development Worker role is deployed."""

import json
import os

import boto3
from botocore.config import Config

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
STACK = "FoodtechMinimalDataFlow-dev"
OUTPUT = "StandardizationWorkerTaskRoleArn"


def decisions(iam, simulation: dict, actions: list[str], resources: list[str]) -> dict[tuple[str, str], dict]:
    found = {}
    operation = 'simulate_principal_policy' if 'PolicySourceArn' in simulation else 'simulate_custom_policy'
    pages = iam.get_paginator(operation).paginate(**simulation, ActionNames=actions,
        ResourceArns=resources, PaginationConfig={'PageSize': 100})
    for page in pages:
        for item in page['EvaluationResults']:
            specific = item.get('ResourceSpecificResults') or [
                {'EvalResourceName': item['EvalResourceName'], 'EvalResourceDecision': item['EvalDecision']}]
            for resource in specific:
                found[(item['EvalActionName'], resource['EvalResourceName'])] = {
                    'decision': resource['EvalResourceDecision'],
                    'organization_allowed': item.get('OrganizationsDecisionDetail', {}).get('AllowedByOrganizations'),
                }
    return found


def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 2}, connect_timeout=5,
                    read_timeout=15, ignore_configured_endpoint_urls=True)
    assert session.client("sts", config=config).get_caller_identity()["Account"] == ACCOUNT
    stack = session.client("cloudformation", config=config).describe_stacks(StackName=STACK)["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    if OUTPUT not in outputs:
        raise SystemExit("Worker role is not deployed; merge and explicitly deploy the CDK stack first.")
    role_arn = outputs[OUTPUT]
    iam = session.client("iam", config=config)
    role_name = role_arn.rsplit("/", 1)[1]
    inline = [name for page in iam.get_paginator('list_role_policies').paginate(RoleName=role_name)
              for name in page['PolicyNames']]
    attached = [policy for page in iam.get_paginator('list_attached_role_policies').paginate(RoleName=role_name)
                for policy in page['AttachedPolicies']]
    if len(inline) != 1 or attached:
        raise AssertionError("Worker role policy inventory changed; review all policies before simulation")
    policy = iam.get_role_policy(RoleName=role_name, PolicyName=inline[0])["PolicyDocument"]
    custom = {"PolicyInputList": [json.dumps(policy)]}
    # An SCP can evaluate aws:RequestedRegion. The policy simulator does not
    # populate this request context automatically; omitting it produced an
    # explicit deny for this account that did not represent a regional request.
    principal = {"PolicySourceArn": role_arn, "ContextEntries": [{
        "ContextKeyName": "aws:RequestedRegion", "ContextKeyValues": [REGION],
        "ContextKeyType": "string",
    }]}
    data = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-data-flow-dev"
    cooking = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-cooking-jobs-dev"
    table_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem",
                     "dynamodb:Query"]
    table_results = decisions(iam, custom, table_actions, [data, cooking])
    expected_tables = {(action, table): "allowed" for table in (data, cooking) for action in table_actions}
    auth = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-auth-sessions-dev"
    auth_actions = ['dynamodb:ConditionCheckItem', 'dynamodb:GetItem', 'dynamodb:Query',
                    'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:DeleteItem']
    table_results.update(decisions(iam, custom, auth_actions, [auth]))
    expected_tables.update({(action, auth): 'allowed' if action == 'dynamodb:ConditionCheckItem' else 'implicitDeny'
                            for action in auth_actions})
    ordered = f"arn:aws:sqs:{REGION}:{ACCOUNT}:foodtech-ordered-dev.fifo"
    quarantine = f"arn:aws:sqs:{REGION}:{ACCOUNT}:foodtech-standardizationdlq-dev.fifo"
    sqs_actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage"]
    queue_results = decisions(iam, custom, sqs_actions, [ordered, quarantine])
    expected_queues = {
        ("sqs:ReceiveMessage", ordered): "allowed", ("sqs:DeleteMessage", ordered): "allowed",
        ("sqs:SendMessage", ordered): "implicitDeny",
        ("sqs:ReceiveMessage", quarantine): "implicitDeny",
        ("sqs:DeleteMessage", quarantine): "implicitDeny",
        ("sqs:SendMessage", quarantine): "allowed",
    }
    identity_results = table_results | queue_results
    mismatches = [(action, resource, expected,
                   identity_results.get((action, resource), {}).get("decision"))
                  for (action, resource), expected in (expected_tables | expected_queues).items()
                  if identity_results.get((action, resource), {}).get("decision") != expected]
    if mismatches:
        raise AssertionError(f"Worker identity policy mismatch: {mismatches}")
    effective = decisions(iam, principal, table_actions, [data, cooking]) | \
        decisions(iam, principal, sqs_actions, [ordered, quarantine]) | \
        decisions(iam, principal, auth_actions, [auth])
    blocked = [(action, resource) for (action, resource), expected in
               (expected_tables | expected_queues).items() if expected == "allowed" and
               effective.get((action, resource), {}).get("decision") != "allowed"]
    summary = {"account": ACCOUNT, "region": REGION, "role_arn": role_arn,
                      "allowed": sum(value == "allowed" for value in
                                     (expected_tables | expected_queues).values()),
                      "denied": sum(value == "implicitDeny" for value in
                                    (expected_tables | expected_queues).values()),
               "effective_blocked": len(blocked),
               "organization_blocked": sum(
                   effective.get(key, {}).get("organization_allowed") is False for key in blocked)}
    print(json.dumps(summary))
    if blocked:
        raise SystemExit("Worker role identity policy is scoped correctly, but effective access is blocked; "
                         "inspect Organizations SCPs and verify with an ECS task before claiming runtime access.")


if __name__ == "__main__":
    main()
