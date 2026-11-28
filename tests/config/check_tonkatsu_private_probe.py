"""Check that the probe task definitions have only their sender TLS inputs."""

import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("probe", ROOT / "infra/containers/tonkatsu_transport_probe.py")
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

template = json.loads(Path(sys.argv[1]).read_text())
resources = [item for item in template["Resources"].values() if item["Type"] != "AWS::CDK::Metadata"]
assert len(resources) == 3 and all(item["Type"] == "AWS::ECS::TaskDefinition" for item in resources)
remaining = set(probe.LANES)
labels = {"business-api": "Business", "broker": "Broker", "gateway": "Gateway"}
for item in resources:
    task = item["Properties"]
    assert "TaskRoleArn" not in task
    assert task["RequiresCompatibilities"] == ["FARGATE"] and task["NetworkMode"] == "awsvpc"
    assert task["Cpu"] == "256" and task["Memory"] == "512"
    container, = task["ContainerDefinitions"]
    name = container["Name"]
    label = labels[name]
    assert task["ExecutionRoleArn"] == {"Ref": f"{label}ExecutionRoleArn"}
    assert container["Image"] == {"Ref": "GatewayImageUri" if name == "gateway" else "BrokerImageUri"}
    assert "PortMappings" not in container and "HealthCheck" not in container
    assert container["User"] == "10001:10001" and container["ReadonlyRootFilesystem"]
    env = {entry["Name"]: entry["Value"] for entry in container["Environment"]}
    mode = env["FOODTECH_SERVICE"]
    remaining.remove(mode)
    assert mode == f"tonkatsu-{name}"
    assert env["FOODTECH_TONKATSU_TRANSPORT_PROBE"] == "true"
    assert container["LogConfiguration"]["Options"]["awslogs-stream-prefix"] == name
    approved_fields = set()
    for peer, port, origin, *tls_paths in probe.LANES[mode]:
        assert env[probe.PREFIX + origin] == f"https://{peer}.{probe.DOMAIN}:{port}"
        approved_fields.update(probe.PREFIX + key + "_PEM" for key in tls_paths)
    secrets = {entry["Name"]: entry["ValueFrom"]["Fn::Join"] for entry in container["Secrets"]}
    assert set(secrets) == approved_fields
    for separator, (bundle, field, version) in secrets.values():
        assert separator == "" and bundle == {"Ref": f"{label}TlsBundleArn"} and version == {"Ref": "TlsBundleVersion"}
        assert field.endswith("::") and "server_key" not in field
assert not remaining
print("Three role-isolated private probe definitions passed")
