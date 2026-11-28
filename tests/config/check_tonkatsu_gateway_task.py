"""Check distinct broker, record and inference TLS paths in the Gateway task."""
import json
from pathlib import Path
import re
import sys


def validate(template, account):
    resources = [row for row in template["Resources"].values() if row["Type"] != "AWS::CDK::Metadata"]
    task, = resources
    assert task["Type"] == "AWS::ECS::TaskDefinition"
    task = task["Properties"]
    assert task["RequiresCompatibilities"] == ["FARGATE"] and task["NetworkMode"] == "awsvpc"
    assert task["TaskRoleArn"] == {"Ref": "GatewayTaskRoleArn"}
    assert task["ExecutionRoleArn"] == {"Ref": "GatewayExecutionRoleArn"}
    listeners = {
        "gateway": (8445, "tonkatsu-gateway", {
            "SERVER_CERT": "server_cert_pem", "SERVER_KEY": "server_key_pem",
            "BROKER_CA": "broker_ca_pem", "BROKER_CERT": "broker_cert_pem",
            "OLLAMA_SERVER_CERT": "inference_server_cert_pem", "OLLAMA_CLIENT_CERT": "inference_client_cert_pem",
            "OLLAMA_CLIENT_KEY": "inference_client_key_pem",
        }),
        "records": (8447, "tonkatsu-business-gateway", {
            "BUSINESS_GATEWAY_SERVER_CERT": "records_server_cert_pem",
            "BUSINESS_GATEWAY_SERVER_KEY": "records_server_key_pem",
            "BUSINESS_GATEWAY_CLIENT_CA": "records_client_ca_pem",
            "BUSINESS_GATEWAY_CLIENT_CERT": "records_client_cert_pem",
        }),
    }
    assert len(task["ContainerDefinitions"]) == len(listeners)
    for container in task["ContainerDefinitions"]:
        name = container["Name"]
        port, service, fields = listeners.pop(name)
        assert container["Image"] == {"Ref": "GatewayImageUri"}
        assert container["Essential"] and container["ReadonlyRootFilesystem"] and not container["Privileged"]
        assert container["User"] == "10001:10001"
        assert container["LinuxParameters"] == {"Capabilities": {"Drop": ["ALL"]}}
        assert container["MountPoints"] == [{
            "ContainerPath": "/run/foodtech-tls", "SourceVolume": name + "-tls", "ReadOnly": False,
        }]
        assert container["PortMappings"] == [{"ContainerPort": port, "Protocol": "tcp"}]
        assert container["LogConfiguration"]["Options"]["awslogs-stream-prefix"] == name
        env = {row["Name"]: row["Value"] for row in container["Environment"]}
        assert env["FOODTECH_SERVICE"] == service
        assert env["FOODTECH_TONKATSU_GATEWAY_TASK_ROLE_ID"] == {"Ref": "GatewayTaskRoleId"}
        assert env["FOODTECH_TONKATSU_SOURCE_READ_ROLE_ID"] == {"Ref": "SourceReadRoleId"}
        assert env["FOODTECH_RECIPE_TIER1_MODEL_ID"] == "llama3.1:8b"
        assert not any(key.endswith("_PEM") or key == "AWS_EXECUTION_ENV" for key in env)
        if name == "gateway":
            assert env["FOODTECH_TONKATSU_OLLAMA_PRIVATE_ORIGIN"] == {"Ref": "InferencePrivateOrigin"}
        else:
            assert "FOODTECH_TONKATSU_OLLAMA_PRIVATE_ORIGIN" not in env
        values = {item["Name"]: item["ValueFrom"]["Fn::Join"] for item in container["Secrets"]}
        assert values == {
            f"FOODTECH_TONKATSU_{suffix}_PEM": ["", [
                {"Ref": "GatewayTlsBundleArn"}, f":{key}::", {"Ref": "GatewayTlsBundleVersion"},
            ]] for suffix, key in fields.items()
        }
    assert not listeners
    params = template["Parameters"]
    cases = {
        "InferencePrivateOrigin": ("https://ollama.foodtech.internal:8448", "http://127.0.0.1:11434"),
        "GatewayImageUri": (f"{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-gateway-dev@sha256:" + "a" * 64,
                            f"{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-gateway-dev:latest"),
        "GatewayTlsBundleVersion": ("12345678-abcd-abcd-abcd-123456789abc", "AWSCURRENT"),
    }
    for name, (valid, invalid) in cases.items():
        parameter = params[name]
        assert "Default" not in parameter
        assert re.fullmatch(parameter["AllowedPattern"], valid) and not re.fullmatch(parameter["AllowedPattern"], invalid)
    assert "SeparateRoleIds" in template["Rules"]


if __name__ == "__main__":
    validate(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
    print("Tonkatsu Gateway task contract passed")
