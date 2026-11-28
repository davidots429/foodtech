"""Verify the manual business API task and its separate TLS inputs."""
import json
from pathlib import Path
import re
import sys


def validate(template, account):
    resources = [value for value in template["Resources"].values() if value["Type"] != "AWS::CDK::Metadata"]
    task, = resources
    assert task["Type"] == "AWS::ECS::TaskDefinition"
    task = task["Properties"]
    assert task["RequiresCompatibilities"] == ["FARGATE"] and task["NetworkMode"] == "awsvpc"
    assert task["TaskRoleArn"] == {"Ref": "BusinessTaskRoleArn"}
    assert task["ExecutionRoleArn"] == {"Ref": "BusinessExecutionRoleArn"}
    container, = task["ContainerDefinitions"]
    assert container["Name"] == "business-api" and container["Image"] == {"Ref": "BusinessImageUri"}
    assert container["ReadonlyRootFilesystem"] and not container["Privileged"]
    assert container["User"] == "10001:10001" and container["LinuxParameters"] == {"Capabilities": {"Drop": ["ALL"]}}
    assert container["MountPoints"] == [{
        "ContainerPath": "/run/foodtech-tls", "SourceVolume": "business-tls", "ReadOnly": False,
    }]
    assert container["PortMappings"] == [{"ContainerPort": 8080, "Protocol": "tcp"}]
    log = container["LogConfiguration"]
    assert log == {"LogDriver": "awslogs", "Options": {
        "awslogs-group": "/foodtech/dev/tonkatsu-business-api", "awslogs-region": "ap-southeast-2",
        "awslogs-stream-prefix": "business-api",
    }}
    environment = {entry["Name"]: entry["Value"] for entry in container["Environment"]}
    assert environment["FOODTECH_SERVICE"] == "tonkatsu-business-api"
    assert environment["TMPDIR"] == "/run/foodtech-tls" and environment["FOODTECH_SECURE_COOKIES"] == "true"
    assert environment["FOODTECH_TONKATSU_BROKER_URL"] == {"Ref": "BrokerPrivateOrigin"}
    assert environment["FOODTECH_TONKATSU_GATEWAY_RECORDS_URL"] == {"Ref": "RecordsPrivateOrigin"}
    assert environment["FOODTECH_TONKATSU_GATEWAY_RECORDS_ENABLED"] == "true"
    assert environment["FOODTECH_TONKATSU_CUSTOMER_INVOCATION_ENABLED"] == "true"
    assert environment["FOODTECH_PRINCIPAL_PROVISIONING_ENABLED"] == "true"
    assert not any(name.endswith("_PEM") for name in environment)
    secrets = {
        entry["Name"]: entry["ValueFrom"]["Fn::Join"] for entry in container["Secrets"]
    }
    expected_keys = {
        "BROKER_SERVER_CERT": "broker_server_cert_pem", "BFF_CERT": "bff_cert_pem", "BFF_KEY": "bff_key_pem",
        "GATEWAY_RECORDS_SERVER_CERT": "records_server_cert_pem",
        "GATEWAY_RECORDS_CLIENT_CERT": "records_client_cert_pem",
        "GATEWAY_RECORDS_CLIENT_KEY": "records_client_key_pem",
    }
    assert secrets == {
        f"FOODTECH_TONKATSU_{name}_PEM":
            ["", [{"Ref": "BusinessTlsBundleArn"}, f":{key}::", {"Ref": "BusinessTlsBundleVersion"}]]
        for name, key in expected_keys.items()
    }
    params = template["Parameters"]
    role = f"arn:aws:iam::{account}:role/foodtech-tonkatsu-"
    ecr = f"{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-broker-dev"
    secret = f"arn:aws:secretsmanager:ap-southeast-2:{account}:secret:foodtech/tonkatsu-"
    cases = {
        "BusinessTaskRoleArn": (role + "business-api-dev", role + "broker-dev"),
        "BusinessExecutionRoleArn": (role + "business-api-execution-dev", role + "gateway-execution-dev"),
        "BusinessImageUri": (ecr + "@sha256:" + "a" * 64, ecr + ":latest"),
        "BusinessTlsBundleArn": (secret + "business-api-dev/tls-Ab1234", secret + "broker-dev/tls-Ab1234"),
        "BusinessTlsBundleVersion": ("12345678-abcd-abcd-abcd-123456789abc", "AWSCURRENT"),
        "BrokerPrivateOrigin": ("https://broker.foodtech.internal:8446", "https://broker.example:8446"),
        "RecordsPrivateOrigin": ("https://records.foodtech.internal:8447", "https://gateway.foodtech.internal:8445"),
    }
    for name, (valid, invalid) in cases.items():
        param = params[name]
        assert "Default" not in param
        assert re.fullmatch(param["AllowedPattern"], valid) and not re.fullmatch(param["AllowedPattern"], invalid)


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    print("Tonkatsu business API task contract passed")
