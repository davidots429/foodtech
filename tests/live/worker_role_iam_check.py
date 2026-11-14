"""Read-only identity-policy check after the development Worker role is deployed."""

import json
import os

import boto3

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
STACK = "FoodtechMinimalDataFlow-dev"
OUTPUT = "StandardizationWorkerTaskRoleArn"


def decisions(iam, simulation: dict, actions: list[str], resources: list[str]) -> dict[tuple[str, str], dict]:
    found = {}
    marker = None
    while True:
        args = {**simulation, "ActionNames": actions, "ResourceArns": resources, "MaxItems": 100}
        if marker:
            args["Marker"] = marker
        page = (iam.simulate_principal_policy(**args) if "PolicySourceArn" in simulation
                else iam.simulate_custom_policy(**args))
        for item in page["EvaluationResults"]:
            specific = item.get("ResourceSpecificResults") or [
                {"EvalResourceName": item["EvalResourceName"],
                 "EvalResourceDecision": item["EvalDecision"]}]
            for resource in specific:
                found[(item["EvalActionName"], resource["EvalResourceName"])] = {
                    "decision": resource["EvalResourceDecision"],
                    "organization_allowed": item.get("OrganizationsDecisionDetail", {}).get(
                        "AllowedByOrganizations"),
                }
        if not page.get("IsTruncated"):
            return found
        marker = page["Marker"]


def main() -> None:
    if os.getenv("AWS_PROFILE") != "foodtech":
        raise SystemExit("Use AWS_PROFILE=foodtech for the development account.")
    session = boto3.Session(profile_name="foodtech", region_name=REGION)
    assert session.client("sts").get_caller_identity()["Account"] == ACCOUNT
    stack = session.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    if OUTPUT not in outputs:
        raise SystemExit("Worker role is not deployed; merge and explicitly deploy the CDK stack first.")
    role_arn = outputs[OUTPUT]
    iam = session.client("iam")
    role_name = role_arn.rsplit("/", 1)[1]
    inline = iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
    if len(inline) != 1 or attached:
        raise AssertionError("Worker role policy inventory changed; review all policies before simulation")
    policy = iam.get_role_policy(RoleName=role_name, PolicyName=inline[0])["PolicyDocument"]
    custom = {"PolicyInputList": [json.dumps(policy)]}
    principal = {"PolicySourceArn": role_arn}
    data = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-data-flow-dev"
    cooking = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-cooking-jobs-dev"
    table_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem",
                     "dynamodb:Query"]
    table_results = decisions(iam, custom, table_actions, [data, cooking])
    expected_tables = {(action, table): "allowed" for table in (data, cooking)
                       for action in table_actions if action != "dynamodb:Query" or table == cooking}
    expected_tables[("dynamodb:Query", data)] = "implicitDeny"
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
        decisions(iam, principal, sqs_actions, [ordered, quarantine])
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
