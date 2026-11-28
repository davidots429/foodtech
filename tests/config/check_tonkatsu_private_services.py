"""Verify ECS launch bounds and listener readiness from synthesized templates."""

import json
from pathlib import Path
import re
import sys

SERVICES = ("Inference", "Gateway", "Broker", "Business")
FAMILIES = ("inference", "gateway", "broker", "business-api")
PORTS = {
    "Inference": {"relay": 8448},
    "Gateway": {"gateway": 8445, "records": 8447},
    "Broker": {"broker": 8446},
    "Business": {"business-api": 8080},
}


def load(directory, name):
    return json.loads((directory / f"FoodtechTonkatsu{name}-dev.template.json").read_text())


def check_services(directory, account):
    template = load(directory, "PrivateServices")
    resources = {key: row for key, row in template["Resources"].items() if row["Type"] != "AWS::CDK::Metadata"}
    assert set(resources) == {f"{label}Service" for label in SERVICES}
    assert all(row["Type"] == "AWS::ECS::Service" for row in resources.values())
    parameters = template["Parameters"]
    count = parameters["DesiredCountPerService"]
    assert count["Type"] == "Number" and count["Default"] == 0 and count["AllowedValues"] == ["0", "1"]
    previous = None
    for label, family in zip(SERVICES, FAMILIES, strict=True):
        logical_id = f"{label}Service"
        row = resources[logical_id]
        assert row.get("DependsOn", []) == ([previous] if previous else [])
        previous = logical_id
        service = row["Properties"]
        assert service["ServiceName"] == f"foodtech-tonkatsu-{family}-dev"
        assert service["Cluster"] == {"Ref": "ClusterArn"}
        task = f"{label}TaskDefinitionArn"
        assert service["TaskDefinition"] == {"Ref": task}
        stock_task = f"arn:aws:ecs:ap-southeast-2:{account}:task-definition/foodtech-tonkatsu-{family}-dev"
        allowed = parameters[task]["AllowedPattern"]
        assert re.fullmatch(allowed, stock_task + ":1")
        assert not re.fullmatch(allowed, stock_task)
        assert not re.fullmatch(allowed, stock_task.replace(family, "foreign") + ":1")
        assert "Default" not in parameters[task]
        assert service["DesiredCount"] == {"Ref": "DesiredCountPerService"}
        assert service["SchedulingStrategy"] == "REPLICA"
        assert service["LaunchType"] == "FARGATE" and service["PlatformVersion"] == "1.4.0"
        assert service["DeploymentController"] == {"Type": "ECS"}
        assert service["DeploymentConfiguration"] == {
            "MinimumHealthyPercent": 0, "MaximumPercent": 100,
            "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
        }
        assert service["NetworkConfiguration"] == {"AwsvpcConfiguration": {
            "AssignPublicIp": "DISABLED",
            "SecurityGroups": [{"Ref": f"{label}SecurityGroupId"}],
            "Subnets": [{"Ref": "BusinessSubnetId" if label == "Business" else "IsolatedSubnetId"}],
        }}
        assert service["ServiceRegistries"] == [{"RegistryArn": {"Ref": f"{label}DiscoveryArn"}}]
        assert not service["EnableExecuteCommand"]
        assert "LoadBalancers" not in service and "Role" not in service
    rules = template["Rules"]
    for name, suffix in (("SecurityGroups", "SecurityGroupId"), ("DiscoveryRegistries", "DiscoveryArn")):
        pairs = [
            assertion["Assert"]["Fn::Not"][0]["Fn::Equals"]
            for assertion in rules[f"Distinct{name}"]["Assertions"]
        ]
        assert pairs == [[{"Ref": f"{a}{suffix}"}, {"Ref": f"{b}{suffix}"}]
                         for i, a in enumerate(SERVICES) for b in SERVICES[i + 1:]]


def check_health(directory):
    for label, ports in PORTS.items():
        template = load(directory, f"{label}Task")
        definition, = [r["Properties"] for r in template["Resources"].values()
                       if r["Type"] == "AWS::ECS::TaskDefinition"]
        containers = {row["Name"]: row for row in definition["ContainerDefinitions"]}
        for name, port in ports.items():
            container = containers[name]
            assert container["Essential"]
            assert container["PortMappings"] == [{"ContainerPort": port, "Protocol": "tcp"}]
            assert container["HealthCheck"] == {
                "Command": ["CMD", "python", "-c",
                            f'import socket; socket.create_connection(("127.0.0.1", {port}), timeout=3).close()'],
                "Interval": 15, "Timeout": 5, "Retries": 3, "StartPeriod": 45,
            }
        if label == "Inference":
            assert "TaskRoleArn" not in definition
            assert containers["ollama"]["HealthCheck"]["Command"] == [
                "CMD-SHELL", "/bin/ollama show llama3.1:8b >/dev/null",
            ]
            assert containers["relay"]["DependsOn"] == [{"ContainerName": "ollama", "Condition": "HEALTHY"}]


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    check_services(directory, sys.argv[2])
    check_health(directory)
    print("Tonkatsu private service launch and readiness contracts passed")
