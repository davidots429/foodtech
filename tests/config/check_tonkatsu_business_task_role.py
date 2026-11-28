"""Check the customer task role contract without creating roles."""
import json
from pathlib import Path
import sys


def validate(template, account):
    region = "ap-southeast-2"
    resources = {item["Type"]: item["Properties"] for item in template["Resources"].values()
                 if item["Type"] != "AWS::CDK::Metadata"}
    assert set(resources) == {"AWS::IAM::Role", "AWS::IAM::Policy"}
    role = resources["AWS::IAM::Role"]
    assert role["RoleName"] == "foodtech-tonkatsu-business-api-dev"
    assert "ManagedPolicyArns" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [{
        "Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{region}:{account}:*"}},
    }]
    statements = resources["AWS::IAM::Policy"]["PolicyDocument"]["Statement"]
    binding, = resources["AWS::IAM::Policy"]["Roles"]
    assert template["Resources"][binding["Ref"]]["Properties"] == role
    tables = {}
    for statement in statements:
        assert statement["Effect"] == "Allow"
        assert statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == region
        actions = statement["Action"]
        actions = {actions} if isinstance(actions, str) else set(actions)
        if all(action.startswith("dynamodb:") for action in actions):
            resource = statement["Resource"]
            assert resource.startswith(f"arn:aws:dynamodb:{region}:{account}:table/foodtech-")
            condition = statement["Condition"]
            assert condition["Null"]["dynamodb:LeadingKeys"] == "false"
            keys = condition["ForAllValues:StringLike"]["dynamodb:LeadingKeys"]
            keys = (keys,) if isinstance(keys, str) else tuple(keys)
            tables.setdefault(resource.rsplit("/", 1)[1], []).append((actions, keys, condition))
    assert set(tables) == {"foodtech-auth-sessions-dev", "foodtech-cooking-jobs-dev", "foodtech-data-flow-dev"}
    assert tables["foodtech-data-flow-dev"][0][0] == {"dynamodb:GetItem", "dynamodb:Query"}
    assert len(tables["foodtech-data-flow-dev"]) == 1
    auth = tables["foodtech-auth-sessions-dev"]
    enrollment, = [(keys, conditions) for actions, keys, conditions in auth
                   if actions == {"dynamodb:PutItem"}]
    assert len(enrollment[0]) == 1 and enrollment[0][0].startswith("USER#")
    assert set(enrollment[1]["ForAllValues:StringEquals"]["dynamodb:Attributes"]) == {"pk", "epoch", "suspended"}
    assert enrollment[1]["StringEquals"]["dynamodb:EnclosingOperation"] == "TransactWriteItems"
    for actions, keys, condition in auth:
        if keys == ("FLOW#*",) or keys == ("SESSION#*",):
            assert "dynamodb:ConditionCheckItem" not in actions
            continue
        assert not actions.intersection({"dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Scan"})
    cooking = tables["foodtech-cooking-jobs-dev"]
    for actions, keys, condition in cooking:
        if actions != {"dynamodb:GetItem", "dynamodb:Query"}:
            enclosing = condition["StringEquals"].get("dynamodb:EnclosingOperation")
            assert enclosing == (None if actions == {"dynamodb:ConditionCheckItem"} else "TransactWriteItems")
            assert condition["StringEqualsIfExists"] == {"dynamodb:ReturnValues": "NONE"}
        if any(key.startswith("STREAM#") for key in keys):
            assert actions == {"dynamodb:PutItem"}
            assert set(condition["ForAllValues:StringEquals"]["dynamodb:Attributes"]) == {"pk", "sk", "job_pk", "job_id"}
    pool, = [item for item in statements if item["Resource"] == {"Ref": "CustomerUserPoolArn"}]
    assert pool["Action"] == "cognito-idp:AdminGetUser"
    key, = [item for item in statements if item["Resource"] == {"Ref": "AuthRefreshKeyArn"}]
    assert set(key["Action"]) == {"kms:GenerateDataKey", "kms:Decrypt"}
    assert key["Condition"]["StringEquals"] == {
        "aws:RequestedRegion": region,
        "kms:EncryptionContext:purpose": "COGNITO_REFRESH_TOKEN",
        "kms:EncryptionContext:environment": "dev",
        "kms:EncryptionContext:schema_version": "1",
        "kms:EncryptionContext:surface": "customer",
    }
    assert key["Condition"]["StringLike"]["kms:EncryptionContext:session_hash"] == "?" * 64
    assert len(statements) == sum(map(len, tables.values())) + 2


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    print("Tonkatsu business task role contract passed")
