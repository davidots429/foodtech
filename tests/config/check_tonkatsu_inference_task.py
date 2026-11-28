"""Check the inference task and agent execution policy from CDK output."""
import json
from pathlib import Path
import re
import sys


def validate_task(template, account):
    resources = [row for row in template["Resources"].values() if row["Type"] != "AWS::CDK::Metadata"]
    definition, = resources
    assert definition["Type"] == "AWS::ECS::TaskDefinition"
    task = definition["Properties"]
    assert task["RequiresCompatibilities"] == ["FARGATE"] and task["NetworkMode"] == "awsvpc"
    assert task["RuntimePlatform"] == {"OperatingSystemFamily": "LINUX", "CpuArchitecture": "X86_64"}
    assert (task["Cpu"], task["Memory"]) == ("4096", "12288")
    assert "TaskRoleArn" not in task and task["ExecutionRoleArn"] == {"Ref": "InferenceExecutionRoleArn"}
    assert task["Volumes"] == [{"Name": "relay-runtime"}, {"Name": "ollama-runtime"}]
    ollama, relay = task["ContainerDefinitions"]
    assert ollama["Name"] == "ollama" and relay["Name"] == "relay"
    options = (
        (ollama, "OllamaImageUri", "65532:65532", "/run/foodtech-ollama"),
        (relay, "RelayImageUri", "10001:10001", "/run/foodtech-tls"),
    )
    for container, image, user, path in options:
        name = container["Name"]
        assert container["Image"] == {"Ref": image}
        assert container["User"] == user and container["Essential"]
        assert container["ReadonlyRootFilesystem"] and not container["Privileged"]
        assert container["LinuxParameters"] == {"Capabilities": {"Drop": ["ALL"]}}
        assert container["MountPoints"] == [{
            "ContainerPath": path, "SourceVolume": name + "-runtime", "ReadOnly": False,
        }]
        assert container["LogConfiguration"]["Options"]["awslogs-stream-prefix"] == name
        assert not any(item["Name"].endswith("_PEM") or item["Name"].startswith("AWS_")
                       for item in container["Environment"])
    assert "Secrets" not in ollama and "PortMappings" not in ollama
    assert {item["Name"]: item["Value"] for item in ollama["Environment"]} == {
        "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1", "OLLAMA_NOPRUNE": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1",
    }
    assert ollama["HealthCheck"]["Command"] == ["CMD-SHELL", "/bin/ollama show llama3.1:8b >/dev/null"]
    assert relay["PortMappings"] == [{"ContainerPort": 8448, "Protocol": "tcp"}]
    assert relay["DependsOn"] == [{"ContainerName": "ollama", "Condition": "HEALTHY"}]
    assert {item["Name"]: item["ValueFrom"]["Fn::Join"] for item in relay["Secrets"]} == {
        f"FOODTECH_TONKATSU_INFERENCE_{suffix}_PEM": ["", [
            {"Ref": "InferenceTlsBundleArn"}, f":{field}::", {"Ref": "InferenceTlsBundleVersion"},
        ]]
        for suffix, field in {
            "SERVER_CERT": "inference_server_cert_pem", "SERVER_KEY": "inference_server_key_pem",
            "CLIENT_CA": "inference_client_ca_pem", "CLIENT_CERT": "inference_client_cert_pem",
        }.items()
    }
    for name, repository in (("OllamaImageUri", "ollama"), ("RelayImageUri", "gateway")):
        base = f"{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-{repository}-dev"
        parameter = template["Parameters"][name]
        assert "Default" not in parameter
        assert re.fullmatch(parameter["AllowedPattern"], base + "@sha256:" + "a" * 64)
        assert not re.fullmatch(parameter["AllowedPattern"], base + ":latest")


def validate_execution(template, account):
    resources = {row["Type"]: row["Properties"] for row in template["Resources"].values()
                 if row["Type"] != "AWS::CDK::Metadata"}
    assert set(resources) == {"AWS::Logs::LogGroup", "AWS::IAM::Role", "AWS::IAM::Policy"}
    assert resources["AWS::Logs::LogGroup"]["RetentionInDays"] == 7
    trust, = resources["AWS::IAM::Role"]["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert trust["Condition"] == {
        "StringEquals": {"aws:SourceAccount": account},
        "ArnLike": {"aws:SourceArn": f"arn:aws:ecs:ap-southeast-2:{account}:*"},
    }
    policies = resources["AWS::IAM::Policy"]["PolicyDocument"]["Statement"]
    expected = (
        ("ecr:GetAuthorizationToken", "*"),
        (["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], [
            f"arn:aws:ecr:ap-southeast-2:{account}:repository/foodtech/tonkatsu-{repo}-dev"
            for repo in ("gateway", "ollama")
        ]),
        (["logs:CreateLogStream", "logs:PutLogEvents"], [
            f"arn:aws:logs:ap-southeast-2:{account}:log-group:/foodtech/dev/tonkatsu-inference:log-stream:{name}/{name}/*"
            for name in ("relay", "ollama")
        ]),
        ("secretsmanager:GetSecretValue", {"Ref": "InferenceTlsBundleArn"}),
        ("kms:Decrypt", {"Ref": "InferenceTlsKeyArn"}),
    )
    assert [(statement["Action"], statement["Resource"]) for statement in policies] == list(expected)
    assert all(statement["Effect"] == "Allow" and
               statement["Condition"]["StringEquals"]["aws:RequestedRegion"] == "ap-southeast-2"
               for statement in policies)
    assert policies[-2]["Condition"]["Bool"] == {"aws:SecureTransport": "true"}
    assert policies[-1]["Condition"]["StringEquals"] == {
        "aws:RequestedRegion": "ap-southeast-2",
        "kms:ViaService": "secretsmanager.ap-southeast-2.amazonaws.com",
        "kms:EncryptionContext:SecretARN": {"Ref": "InferenceTlsBundleArn"},
    }


if __name__ == "__main__":
    validate_task(json.loads(Path(sys.argv[1]).read_text()), sys.argv[3])
    validate_execution(json.loads(Path(sys.argv[2]).read_text()), sys.argv[3])
    print("Tonkatsu inference task and execution contracts passed")
