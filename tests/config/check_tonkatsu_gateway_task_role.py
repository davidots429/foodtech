"""Verify the Gateway task role covers only the local execution registry."""
import json
from pathlib import Path
import sys


def validate(template, account):
    region = "ap-southeast-2"
    resources = {row["Type"]: row["Properties"] for row in template["Resources"].values()
                 if row["Type"] != "AWS::CDK::Metadata"}
    assert set(resources) == {"AWS::IAM::Role", "AWS::IAM::Policy"}
    role = resources["AWS::IAM::Role"]
    assert role["RoleName"] == "foodtech-tonkatsu-gateway-dev"
    assert role.get("ManagedPolicyArns", []) == []
    assert role["AssumeRolePolicyDocument"]["Statement"] == [{
        "Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{region}:{account}:*"}},
    }]
    policy = resources["AWS::IAM::Policy"]
    binding, = policy["Roles"]
    assert template["Resources"][binding["Ref"]]["Properties"] == role
    uid = "????????-????-????-????-????????????"
    model = "MODEL#llama3.1:8b"
    limits = [f"LIMIT#????-??#TENANT#{uid}", "LIMIT#????-??#PROVIDER#ollama", "LIMIT#????-??#PROJECT#GLOBAL"]
    statements = policy["PolicyDocument"]["Statement"]
    expected = {
        "GetItem": ([f"EXEC#{uid}", model, *limits], False),
        "ConditionCheckItem": ([model, *limits], False),
        "PutItem": ([f"EXEC#{uid}", f"ACTIVE#{uid}"], True),
        "UpdateItem": ([f"EXEC#{uid}", *limits], True),
        "DeleteItem": ([f"ACTIVE#{uid}"], True),
    }
    assert len(expected) == len(statements)
    for row in statements:
        service, action = row["Action"].split(":")
        assert service == "dynamodb" and row["Effect"] == "Allow"
        assert row["Resource"] == f"arn:aws:dynamodb:{region}:{account}:table/foodtech-ai-execution-registry-dev"
        keys, transaction = expected.pop(action)
        equals = {"aws:RequestedRegion": region}
        if transaction:
            equals["dynamodb:EnclosingOperation"] = "TransactWriteItems"
        condition = {
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": keys},
            "Null": {"dynamodb:LeadingKeys": "false"},
            "StringEquals": equals,
        }
        if action != "GetItem":
            condition["StringEqualsIfExists"] = {"dynamodb:ReturnValues": "NONE"}
        assert row["Condition"] == condition


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    print("Tonkatsu local Gateway task role contract passed")
