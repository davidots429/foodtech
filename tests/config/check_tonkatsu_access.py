"""Check the synthesized fryer IAM contract before any account-level checks."""
from fnmatch import fnmatchcase
from hashlib import sha256
import json
from pathlib import Path
import sys


def validate(template, account):
    resources = template["Resources"]
    owned = {key: value for key, value in resources.items() if value["Type"] != "AWS::CDK::Metadata"}
    assert sorted(resource["Type"] for resource in owned.values()) == [
        "AWS::IAM::ManagedPolicy", "AWS::IAM::Policy", "AWS::IAM::Policy",
        "AWS::IAM::Policy", "AWS::IAM::Role", "AWS::IAM::Role",
    ]
    for resource in owned.values():
        assert resource["DeletionPolicy"] == resource["UpdateReplacePolicy"] == "Retain"
    properties = {key: resource["Properties"] for key, resource in owned.items()}
    broker_id = next(key for key, value in properties.items() if value.get("RoleName") == "foodtech-tonkatsu-broker-dev")
    read_id = next(key for key, value in properties.items() if value.get("RoleName") == "foodtech-tonkatsu-source-read-dev")
    boundary_id = next(key for key, resource in owned.items() if resource["Type"] == "AWS::IAM::ManagedPolicy")
    regional = {"aws:RequestedRegion": "ap-southeast-2"}
    session = {"sts:RoleSessionName": "tonkatsu-*"}
    gateway_ref = {"Ref": "GatewayTaskRoleArn"}
    business_ref = {"Ref": "BusinessTaskRoleArn"}
    broker = properties[broker_id]
    reader = properties[read_id]
    assert "Policies" not in broker and "ManagedPolicyArns" not in broker
    assert "Policies" not in reader
    assert reader["PermissionsBoundary"] == {"Ref": boundary_id}
    assert reader["ManagedPolicyArns"] == [{"Ref": boundary_id}]
    assert broker["AssumeRolePolicyDocument"]["Statement"] == [{
        "Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Condition": {"StringEquals": {"aws:SourceAccount": account},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:ap-southeast-2:{account}:*"}},
    }]
    assert reader["AssumeRolePolicyDocument"]["Statement"] == [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"AWS": {"Fn::GetAtt": [broker_id, "Arn"]}},
         "Condition": {"StringEquals": regional, "StringLike": session}},
        {"Action": "sts:AssumeRole", "Effect": "Deny", "Principal": {"AWS": [gateway_ref, business_ref]}},
        {"Action": "sts:TagSession", "Effect": "Deny", "Principal": {"AWS": "*"}},
    ]

    job = "SCOPE#USER#*#JOB#????????-????-????-????-????????????"
    roster = "SCOPE#USER#*#JOBS"
    execution = "EXEC#????????-????-????-????-????????????"
    streams = [f"STREAM#14:synthetic-rest|21:mockprofile-100-a-001|{length}:batch-a-*" for length in ("?", "??")]
    controls = ["USER#*", "CONTROL#COOKING_HISTORY_WRITERS"]
    inputs = [("auth-sessions", controls), ("cooking-jobs", [roster, job, *streams]), ("data-flow", streams)]

    def statement(action, table, keys, qualifiers=None, attrs=None):
        condition = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": keys},
                     "Null": {"dynamodb:LeadingKeys": "false"},
                     "StringEquals": {**regional, **(qualifiers or {})}}
        if attrs is not None:
            condition["Null"].update({"dynamodb:Attributes": "false", "dynamodb:EnclosingOperation": "true"})
            condition["ForAllValues:StringEquals"] = {"dynamodb:Attributes": attrs}
        return {"Effect": "Allow", "Action": action,
                "Resource": f"arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-{table}-dev",
                "Condition": condition}

    read_actions = ["dynamodb:GetItem", "dynamodb:Query"]
    assert properties[boundary_id]["PolicyDocument"]["Statement"] == [
        statement(read_actions, "cooking-jobs", [job]), statement(read_actions, "data-flow", streams),
    ]
    batch = "batch-a-example"
    source = "STREAM#14:synthetic-rest|21:mockprofile-100-a-001|"
    assert any(fnmatchcase(f"{source}{len(batch)}:{batch}", pattern) for pattern in streams)
    for key in (f"{source}16:batch-b-example", "EVENT#event", "JEYUK#jeyuk-batch",
                "STREAM#14:synthetic-rest|21:mockprofile-110-a-001|15:batch-a-example"):
        assert not any(fnmatchcase(key, pattern) for pattern in streams)
    history = [statement("dynamodb:GetItem", table, keys, {"dynamodb:EnclosingOperation": "TransactGetItems"})
               for table, keys in inputs]
    history.append(statement("dynamodb:Query", "cooking-jobs", [roster], {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"}))
    checks = [statement("dynamodb:ConditionCheckItem", table, keys, {"dynamodb:ReturnValues": "NONE"})
              for table, keys in [*inputs, ("ai-execution-registry", [execution])]]
    projection = {"dynamodb:Select": "SPECIFIC_ATTRIBUTES"}
    expected = {
        "BrokerPermissions": ([{"Ref": broker_id}], [
            {"Action": "sts:AssumeRole", "Effect": "Allow", "Resource": {"Fn::GetAtt": [read_id, "Arn"]},
             "Condition": {"StringEquals": regional, "StringLike": session}},
            statement("dynamodb:GetItem", "ai-execution-registry", [execution], projection, [
                "pk", "sk", "execution_id", "tenant_id", "feature", "data_classification", "state", "version",
                "job_id", "job_version", "prior_job_id", "prior_job_version", "comparison_id",
                "jeyuk_source_revision", "prior_source_revision", "combi_binding", "tonkatsu_binding",
            ]), *history,
        ]),
        "GatewayPermissions": ([{"Fn::Select": [1, {"Fn::Split": ["/", gateway_ref]}]}], [
            *history, *checks, statement("dynamodb:GetItem", "auth-sessions", ["USER#*"], projection,
                                         ["pk", "epoch", "suspended", "deletion_lock_request"]),
        ]),
        "BusinessConditions": ([{"Fn::Select": [1, {"Fn::Split": ["/", business_ref]}]}], checks),
    }
    baseline = Path(__file__).resolve().parents[2] / "infra/cdk/bin/tonkatsu-access-baseline.json"
    for name, (role_refs, statements) in expected.items():
        assert properties[name]["Roles"] == role_refs
        assert properties[name]["PolicyDocument"]["Statement"] == statements
        assert owned[name]["Metadata"]["GeneratedBaselineSha256"] == sha256(baseline.read_bytes()).hexdigest()
    for parameter in ("GatewayTaskRoleArn", "BusinessTaskRoleArn"):
        assert template["Parameters"][parameter] == {
            "Type": "String", "AllowedPattern": f"^arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_-]{{1,64}}$",
        }
    comparisons = template["Rules"]["DistinctTonkatsuRoles"]["Assertions"][0]["Assert"]["Fn::And"]
    assert [comparison["Fn::Not"][0]["Fn::Equals"] for comparison in comparisons] == [
        [gateway_ref, business_ref],
        *[[ref, f"arn:aws:iam::{account}:role/{name}"]
          for name in (broker["RoleName"], reader["RoleName"]) for ref in (gateway_ref, business_ref)],
    ]


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    print("Tonkatsu IAM definition checks passed")
