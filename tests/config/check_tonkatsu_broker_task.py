"""Verify the synthesized manual task contract with no AWS resources."""
import json
from pathlib import Path
import re
import sys


def validate(template, account):
    resources = template["Resources"]
    assert {name: resource["Type"] for name, resource in resources.items() if name != "CDKMetadata"} == {
        "BrokerTask": "AWS::ECS::TaskDefinition",
    }
    definition = resources["BrokerTask"]["Properties"]
    assert definition["Family"] == "foodtech-tonkatsu-broker-dev"
    assert definition["RequiresCompatibilities"] == ["FARGATE"]
    assert definition["NetworkMode"] == "awsvpc"
    assert definition["RuntimePlatform"] == {"OperatingSystemFamily": "LINUX", "CpuArchitecture": "X86_64"}
    assert definition["TaskRoleArn"] == {"Ref": "BrokerTaskRoleArn"}
    assert definition["ExecutionRoleArn"] == {"Ref": "BrokerExecutionRoleArn"}
    assert definition["Volumes"] == [{"Name": "broker-tls"}]
    (container,) = definition["ContainerDefinitions"]
    assert container["Image"] == {"Ref": "BrokerImageUri"}
    assert container["User"] == "10001:10001" and container["ReadonlyRootFilesystem"] is True
    assert container["Privileged"] is False and container["LinuxParameters"] == {"Capabilities": {"Drop": ["ALL"]}}
    assert container["PortMappings"] == [{"ContainerPort": 8446, "Protocol": "tcp"}]
    assert container["MountPoints"] == [
        {"SourceVolume": "broker-tls", "ContainerPath": "/run/foodtech-tls", "ReadOnly": False},
    ]
    environment = {entry["Name"]: entry["Value"] for entry in container["Environment"]}
    assert environment == {
        "FOODTECH_SERVICE": "tonkatsu-broker",
        "FOODTECH_STAGE": "dev",
        "FOODTECH_PRINCIPAL_PROVISIONING_ENABLED": "true",
        "FOODTECH_TONKATSU_BROKER_ENABLED": "true",
        "FOODTECH_TONKATSU_BROKER_BIND_HOST": "0.0.0.0",
        "FOODTECH_TONKATSU_BROKER_PORT": "8446",
        "FOODTECH_TONKATSU_BROKER_TASK_ROLE_ARN": {"Ref": "BrokerTaskRoleArn"},
        "FOODTECH_TONKATSU_BROKER_TASK_ROLE_ID": {"Ref": "BrokerTaskRoleId"},
        "FOODTECH_TONKATSU_GATEWAY_URL": {"Ref": "GatewayPrivateOrigin"},
        "FOODTECH_AI_REGISTRY_TABLE": "foodtech-ai-execution-registry-dev",
        "FOODTECH_AUTH_TABLE": "foodtech-auth-sessions-dev",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": "/run/foodtech-tls",
    }
    fields = ("broker_server_cert", "broker_server_key", "bff_ca", "bff_cert",
              "gateway_server_cert", "broker_client_cert", "broker_client_key")
    assert container["Secrets"] == [
        {"Name": f"FOODTECH_TONKATSU_{field.upper()}_PEM", "ValueFrom": {
            "Fn::Join": ["", [{"Ref": "BrokerTlsBundleArn"}, f":{field}_pem::", {"Ref": "BrokerTlsBundleVersion"}]],
        }} for field in fields
    ]
    assert container["LogConfiguration"] == {"LogDriver": "awslogs", "Options": {
        "awslogs-group": "/foodtech/dev/tonkatsu-broker", "awslogs-region": "ap-southeast-2",
        "awslogs-stream-prefix": "broker",
    }}
    cases = {
        "BrokerTaskRoleArn": (f"arn:aws:iam::{account}:role/foodtech-tonkatsu-broker-dev",
                              f"arn:aws:iam::{account}:role/foodtech-recipe-broker-dev"),
        "BrokerExecutionRoleArn": (f"arn:aws:iam::{account}:role/foodtech-tonkatsu-broker-execution-dev",
                                   f"arn:aws:iam::{account}:role/foodtech-tonkatsu-broker-dev"),
        "BrokerImageUri": (
            f"{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-broker-dev@sha256:" + "0" * 64,
            f"{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-broker-dev:latest",
        ),
        "BrokerTaskRoleId": ("AROA" + "0" * 17, "other"),
        "BrokerTlsBundleArn": (f"arn:aws:secretsmanager:ap-southeast-2:{account}:secret:foodtech/tonkatsu-broker-dev/tls-aB1234",
                               f"arn:aws:secretsmanager:ap-southeast-2:{account}:secret:foodtech/gateway-dev/tls-aB1234"),
        "BrokerTlsBundleVersion": ("01234567-0123-4000-8000-000000000000", "AWSCURRENT"),
        "GatewayPrivateOrigin": ("https://tonkatsu-gateway.foodtech.internal:8445", "https://example.org:8445"),
    }
    for name, (allowed, rejected) in cases.items():
        parameter = template["Parameters"][name]
        assert "Default" not in parameter and re.fullmatch(parameter["AllowedPattern"], allowed)
        assert not re.fullmatch(parameter["AllowedPattern"], rejected)


def validate_execution(template, account):
    region = "ap-southeast-2"
    log_group = "/foodtech/dev/tonkatsu-broker"
    resources = {key: value for key, value in template["Resources"].items() if key != "CDKMetadata"}
    assert sorted(resource["Type"] for resource in resources.values()) == [
        "AWS::IAM::Policy", "AWS::IAM::Role", "AWS::Logs::LogGroup",
    ]
    role_id, role = next((key, resource["Properties"]) for key, resource in resources.items()
                         if resource["Type"] == "AWS::IAM::Role")
    assert role["RoleName"] == "foodtech-tonkatsu-broker-execution-dev"
    assert "ManagedPolicyArns" not in role and "Policies" not in role
    assert role["AssumeRolePolicyDocument"]["Statement"] == [
        {"Action": "sts:AssumeRole", "Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"},
         "Condition": {"StringEquals": {"aws:SourceAccount": account},
                       "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:{region}:{account}:*"}}},
    ]
    logs = next(resource for resource in resources.values() if resource["Type"] == "AWS::Logs::LogGroup")
    assert logs["DeletionPolicy"] == logs["UpdateReplacePolicy"] == "Retain"
    assert logs["Properties"]["LogGroupName"] == log_group and logs["Properties"]["RetentionInDays"] == 7
    policy = next(resource["Properties"] for resource in resources.values() if resource["Type"] == "AWS::IAM::Policy")
    assert policy["Roles"] == [{"Ref": role_id}]
    regional = {"aws:RequestedRegion": region}
    secret = {"Ref": "BrokerTlsBundleArn"}
    key = {"Ref": "BrokerTlsKeyArn"}

    def allow(action, resource, condition=None):
        return {"Effect": "Allow", "Action": action, "Resource": resource,
                "Condition": condition or {"StringEquals": regional}}

    assert policy["PolicyDocument"]["Statement"] == [
        allow("ecr:GetAuthorizationToken", "*"),
        allow(["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
              f"arn:aws:ecr:{region}:{account}:repository/foodtech/tonkatsu-broker-dev"),
        allow(["logs:CreateLogStream", "logs:PutLogEvents"],
              f"arn:aws:logs:{region}:{account}:log-group:{log_group}:log-stream:broker/broker/*"),
        allow("secretsmanager:GetSecretValue", secret,
              {"StringEquals": regional, "Bool": {"aws:SecureTransport": "true"}}),
        allow("kms:Decrypt", key, {"StringEquals": {**regional,
              "kms:ViaService": f"secretsmanager.{region}.amazonaws.com", "kms:EncryptionContext:SecretARN": secret}}),
    ]
    for name in ("BrokerTlsBundleArn", "BrokerTlsKeyArn"):
        assert "Default" not in template["Parameters"][name]



if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    validate_execution(json.loads(Path(sys.argv[3]).read_text()), sys.argv[2])
    print("Tonkatsu broker task and execution access checks passed")
