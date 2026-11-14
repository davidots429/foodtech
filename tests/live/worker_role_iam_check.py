"""Read-only identity-policy check after the development Worker role is deployed."""

import json
import os

import boto3

ACCOUNT = "573693340100"
REGION = "ap-southeast-2"
STACK = "FoodtechMinimalDataFlow-dev"
OUTPUT = "StandardizationWorkerTaskRoleArn"


def decisions(iam, role_arn: str, actions: list[str], resources: list[str]) -> dict[tuple[str, str], str]:
    found = {}
    marker = None
    while True:
        args = {"PolicySourceArn": role_arn, "ActionNames": actions,
                "ResourceArns": resources, "MaxItems": 100}
        if marker:
            args["Marker"] = marker
        page = iam.simulate_principal_policy(**args)
        for item in page["EvaluationResults"]:
            found[(item["EvalActionName"], item["EvalResourceName"])] = item["EvalDecision"]
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
    data = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-data-flow-dev"
    cooking = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-cooking-jobs-dev"
    table_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:ConditionCheckItem",
                     "dynamodb:Query"]
    table_results = decisions(iam, role_arn, table_actions, [data, cooking])
    expected_tables = {(action, table): "allowed" for table in (data, cooking)
                       for action in table_actions if action != "dynamodb:Query" or table == cooking}
    expected_tables[("dynamodb:Query", data)] = "implicitDeny"
    ordered = f"arn:aws:sqs:{REGION}:{ACCOUNT}:foodtech-ordered-dev.fifo"
    quarantine = f"arn:aws:sqs:{REGION}:{ACCOUNT}:foodtech-standardizationdlq-dev.fifo"
    sqs_actions = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:SendMessage"]
    queue_results = decisions(iam, role_arn, sqs_actions, [ordered, quarantine])
    expected_queues = {
        ("sqs:ReceiveMessage", ordered): "allowed", ("sqs:DeleteMessage", ordered): "allowed",
        ("sqs:SendMessage", ordered): "implicitDeny",
        ("sqs:ReceiveMessage", quarantine): "implicitDeny",
        ("sqs:DeleteMessage", quarantine): "implicitDeny",
        ("sqs:SendMessage", quarantine): "allowed",
    }
    mismatches = [(action, resource, expected, (table_results | queue_results).get((action, resource)))
                  for (action, resource), expected in (expected_tables | expected_queues).items()
                  if (table_results | queue_results).get((action, resource)) != expected]
    if mismatches:
        raise AssertionError(f"Worker role policy mismatch: {mismatches}")
    print(json.dumps({"account": ACCOUNT, "region": REGION, "role_arn": role_arn,
                      "allowed": sum(value == "allowed" for value in
                                     (expected_tables | expected_queues).values()),
                      "denied": sum(value == "implicitDeny" for value in
                                    (expected_tables | expected_queues).values())}))


if __name__ == "__main__":
    main()
