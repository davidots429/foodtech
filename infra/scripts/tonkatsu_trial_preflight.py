"""Read and compare the stopped Tonkatsu trial with its reviewed development plan."""

import argparse
import json
from pathlib import Path
import re
import sys
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError

from private_rest_initialize import ACCOUNT, REGION, VerifiedOperator, canonical_hash, verify_operator_environment

NETWORK = "FoodtechTonkatsuPrivateNetwork-dev"
SERVICES = "FoodtechTonkatsuPrivateServices-dev"
TLS_STACKS = {
    "Business": ("FoodtechTonkatsuPeerTls-dev", "BusinessApi"),
    "Broker": ("FoodtechTonkatsuBrokerTls-dev", "Broker"),
    "Gateway": ("FoodtechTonkatsuPeerTls-dev", "Gateway"),
    "Inference": ("FoodtechTonkatsuInferenceTls-dev", "Inference"),
}
WORKLOADS = {
    "Business": ("business-api", {"business-api": ("broker", 8080)}),
    "Broker": ("broker", {"broker": ("broker", 8446)}),
    "Gateway": ("gateway", {"gateway": ("gateway", 8445), "records": ("gateway", 8447)}),
    "Inference": ("inference", {"relay": ("gateway", 8448), "ollama": ("ollama", None)}),
}
TASK_STACKS = {label: f"FoodtechTonkatsu{label}Task-dev" for label in WORKLOADS}
REVIEWED_STACKS = {NETWORK, SERVICES, *TASK_STACKS.values(), *(stack for stack, _ in TLS_STACKS.values())}
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
HASH = re.compile(r"[a-f0-9]{64}")
DOMAIN = "tonkatsu.foodtech.internal"
SCHEME = f"arn:aws:ecs:{REGION}:{ACCOUNT}"
STOPPED_COUNTS = ("desiredCount", "pendingCount", "runningCount")


class ReviewMismatch(ValueError):
    """A safe phase description that omits AWS response values."""


def require(condition, phase):
    if not condition:
        raise ReviewMismatch(phase)


def checked_plan(plan):
    require(type(plan) is dict and set(plan) == {"network_mode", "templates", "image_digests", "tls_version"},
            "reviewed_plan_fields")
    require(plan["network_mode"] in ("isolated", "managed-login"), "reviewed_network_mode")
    for key, names, validity in (("templates", REVIEWED_STACKS, HASH), ("image_digests", {"broker", "gateway", "ollama"}, DIGEST)):
        values = plan[key]
        require(type(values) is dict and set(values) == names and all(
            isinstance(value, str) and validity.fullmatch(value) for value in values.values()), key)
    require(isinstance(plan["tls_version"], str), "reviewed_tls_version")
    version = UUID(plan["tls_version"])
    require(version.version == 4 and str(version) == plan["tls_version"], "reviewed_tls_version")
    return plan


class ReadOnlyOperator(VerifiedOperator):
    def client(self, service):
        endpoints = {"ecs": "ecs", "ec2": "ec2", "ecr": "api.ecr", "servicediscovery": "servicediscovery"}
        if service not in endpoints:
            require(service in {"cloudformation", "secretsmanager"}, "metadata_service")
            return super().client(service)
        if service not in self.clients:
            self.clients[service] = self.session.client(service, config=self.config, verify=True,
                endpoint_url=f"https://{endpoints[service]}.{REGION}.amazonaws.com")
        return self.clients[service]


def metadata_fields(items, key, value):
    fields = {entry[key]: entry[value] for entry in items}
    require(len(fields) == len(items), "duplicate_metadata_fields")
    return fields


def stack_metadata(client, name, digest):
    (stack,) = client.describe_stacks(StackName=name)["Stacks"]
    require(stack["StackName"] == name and
            stack["StackId"].startswith(f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{name}/") and
            stack["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}, "stack_identity")
    tags = metadata_fields(stack.get("Tags", []), "Key", "Value")
    require((tags.get("Project"), tags.get("Environment")) == ("foodtech", "dev"), "stack_tags")
    body = client.get_template(StackName=stack["StackId"], TemplateStage="Original")["TemplateBody"]
    template = json.loads(body) if isinstance(body, str) else body
    require(canonical_hash(template) == digest, "stack_template_digest")
    return (metadata_fields(stack["Outputs"], "OutputKey", "OutputValue"),
            metadata_fields(stack.get("Parameters", []), "ParameterKey", "ParameterValue"))


def check_network(ec2, discovery, network, parameters, mode):
    require((network["BusinessSubnetId"] != network["IsolatedSubnetId"]) == (mode == "managed-login"),
            "network_mode")
    subnet_ids = {network["BusinessSubnetId"], network["IsolatedSubnetId"]}
    subnets = ec2.describe_subnets(SubnetIds=sorted(subnet_ids))["Subnets"]
    require(len(subnets) == len(subnet_ids) and {row["SubnetId"] for row in subnets} == subnet_ids and all(
        row["VpcId"] == network["VpcId"] and row["State"] == "available" and row["MapPublicIpOnLaunch"] is False
        for row in subnets), "private_subnets")
    roles = {*WORKLOADS, "Tester", "ImageAccess", "WorkflowAccess"}
    if mode == "managed-login":
        roles.add("AuthAccess")
    groups = {network[label + "SecurityGroupId"] for label in roles}
    require(len(groups) == len(roles), "distinct_security_groups")
    actual = ec2.describe_security_groups(GroupIds=sorted(groups))["SecurityGroups"]
    require(len(actual) == len(groups) and all(row["VpcId"] == network["VpcId"] for row in actual)
            and {row["GroupId"] for row in actual} == groups, "network_security_group_ownership")
    ingress = {
        "Business": [("Tester", 8080)], "Broker": [("Business", 8446)],
        "Gateway": [("Broker", 8445), ("Business", 8447)], "Inference": [("Gateway", 8448)],
    }
    for label, edges in ingress.items():
        group = next(group for group in actual if group["GroupId"] == network[label + "SecurityGroupId"])
        incoming = group["IpPermissions"]
        require(all(rule["IpProtocol"] == "tcp" and rule["FromPort"] == rule["ToPort"]
                    and rule.get("UserIdGroupPairs")
                    and not any(rule.get(field) for field in ("IpRanges", "Ipv6Ranges", "PrefixListIds"))
                    for rule in incoming), "exact_ingress_rules")
        require({(peer["GroupId"], rule["FromPort"]) for rule in incoming for peer in rule["UserIdGroupPairs"]} ==
                {(network[source + "SecurityGroupId"], port) for source, port in edges}, "exact_ingress_peers")
        outgoing_peers = {("ImageAccess", 443)}
        outgoing_prefixes = {parameters["S3PrefixListId"]}
        if label != "Inference":
            outgoing_prefixes.add(parameters["DynamoDbPrefixListId"])
        if label in {"Broker", "Gateway"}:
            outgoing_peers.add(("WorkflowAccess", 443))
        for destination, connections in ingress.items():
            outgoing_peers.update((destination, port) for source, port in connections if source == label)
        expected_cidrs = set()
        if label == "Business" and mode == "managed-login":
            outgoing_peers.add(("AuthAccess", 443))
            expected_cidrs.add(("0.0.0.0/0", 443))
        actual_peers, actual_prefixes, actual_cidrs = set(), set(), set()
        for rule in group["IpPermissionsEgress"]:
            if rule["IpProtocol"] == "icmp":
                require([item["CidrIp"] for item in rule.get("IpRanges", [])] == ["255.255.255.255/32"]
                        and (rule["FromPort"], rule["ToPort"]) == (252, 86)
                        and not any(rule.get(key) for key in ("Ipv6Ranges", "UserIdGroupPairs", "PrefixListIds")),
                        "egress_sentinel")
                continue
            require(rule["IpProtocol"] == "tcp" and rule["FromPort"] == rule["ToPort"]
                    and not rule.get("Ipv6Ranges"), "egress_protocol")
            port = rule["FromPort"]
            actual_peers.update((item["GroupId"], port) for item in rule.get("UserIdGroupPairs", []))
            actual_prefixes.update((item["PrefixListId"], port) for item in rule.get("PrefixListIds", []))
            actual_cidrs.update((item["CidrIp"], port) for item in rule.get("IpRanges", []))
        require(actual_peers == {(network[peer + "SecurityGroupId"], port) for peer, port in outgoing_peers}
                and actual_prefixes == {(value, 443) for value in outgoing_prefixes}
                and actual_cidrs == expected_cidrs, "exact_egress_rules")
    namespace_ids = set()
    for label, (name, _) in WORKLOADS.items():
        arn = network[label + "DiscoveryArn"]
        require(arn.startswith(f"arn:aws:servicediscovery:{REGION}:{ACCOUNT}:service/srv-"), "registry_arn")
        row = discovery.get_service(Id=arn.rsplit("/", 1)[1])["Service"]
        require(row["Arn"] == arn and row["Name"] == name and network[label + "Host"] == f"{name}.{DOMAIN}"
                and row["DnsConfig"]["DnsRecords"] == [{"Type": "A", "TTL": 10}], "private_registry")
        declared_namespace = row.get("NamespaceId")
        dns_namespace = row["DnsConfig"].get("NamespaceId")
        require((declared_namespace or dns_namespace) and
                not (declared_namespace and dns_namespace and declared_namespace != dns_namespace), "registry_namespace")
        namespace_ids.add(declared_namespace or dns_namespace)
    require(len(namespace_ids) == 1, "registry_namespace")
    namespace = discovery.get_namespace(Id=namespace_ids.pop())["Namespace"]
    require(namespace["Name"] == DOMAIN and namespace["Type"] == "DNS_PRIVATE", "private_namespace")


def check_services(ecs, network, outputs, parameters, tasks):
    cluster = network["ClusterArn"]
    require(cluster == f"{SCHEME}:cluster/foodtech-tonkatsu-private-dev", "cluster_arn")
    require(parameters["DesiredCountPerService"] == "0", "reviewed_stop_state")
    arns = [outputs[label + "ServiceArn"] for label in WORKLOADS]
    result = ecs.describe_services(cluster=cluster, services=arns)
    services = {row["serviceArn"]: row for row in result["services"]}
    require(not result.get("failures") and len(result["services"]) == len(arns)
            and set(services) == set(arns), "services_reconciled")
    for label, (name, _) in WORKLOADS.items():
        key = label + "ServiceArn"
        expected_name = f"foodtech-tonkatsu-{name}-dev"
        require(outputs[key] == f"{SCHEME}:service/foodtech-tonkatsu-private-dev/{expected_name}", "service_arn")
        row = services[outputs[key]]
        require(row["clusterArn"] == cluster and row["status"] == "ACTIVE" and row["serviceName"] == expected_name
                and row.get("launchType") == "FARGATE" and row.get("platformVersion") == "1.4.0"
                and all(row[count] == 0 for count in STOPPED_COUNTS), "service_stopped")
        subnet = network["BusinessSubnetId" if label == "Business" else "IsolatedSubnetId"]
        group, discovery = (network[label + suffix] for suffix in ("SecurityGroupId", "DiscoveryArn"))
        require(parameters[label + "TaskDefinitionArn"] == row["taskDefinition"] == tasks[label]
                and parameters[label + "SecurityGroupId"] == group
                and parameters[label + "DiscoveryArn"] == discovery, "service_parameter_binding")
        require(row["networkConfiguration"]["awsvpcConfiguration"] == {
            "assignPublicIp": "DISABLED", "subnets": [subnet], "securityGroups": [group],
        } and row["serviceRegistries"] == [{"registryArn": discovery}]
                and not row.get("loadBalancers") and not row.get("enableExecuteCommand"), "private_service_attachment")
    require(parameters["ClusterArn"] == cluster and all(parameters[key] == network[key]
            for key in ("BusinessSubnetId", "IsolatedSubnetId")), "network_parameter_binding")


def check_task(ecs, label, arn, images, bundle, version, network):
    name, expected = WORKLOADS[label]
    family = f"foodtech-tonkatsu-{name}-dev"
    require(re.fullmatch(fr"{SCHEME}:task-definition/{family}:[1-9][0-9]*", arn), "task_definition_arn")
    row = ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
    require(row["taskDefinitionArn"] == arn and row["status"] == "ACTIVE"
            and row["networkMode"] == "awsvpc" and "FARGATE" in row["requiresCompatibilities"]
            and row["runtimePlatform"] == {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
            "task_definition_boundary")
    task_role = f"arn:aws:iam::{ACCOUNT}:role/{family}"
    execution_role = f"arn:aws:iam::{ACCOUNT}:role/foodtech-tonkatsu-{name}-execution-dev"
    require(row["executionRoleArn"] == execution_role
            and row.get("taskRoleArn") == (None if label == "Inference" else task_role), "task_roles")
    containers = {item["name"]: item for item in row["containerDefinitions"]}
    require(len(row["containerDefinitions"]) == len(expected) and set(containers) == set(expected), "task_containers")
    for name, (repository, port) in expected.items():
        container = containers[name]
        require(container["image"] == images[repository] and container["readonlyRootFilesystem"]
                and container["essential"] and not container.get("privileged")
                and container.get("healthCheck", {}).get("command"), "task_container_image_and_health")
        mappings = container.get("portMappings", [])
        require([item["containerPort"] for item in mappings] == ([port] if port else [])
                and all(item["protocol"] == "tcp" for item in mappings), "task_container_ports")
        environment = metadata_fields(container["environment"], "name", "value")
        expected_origins = {
            "business-api": {"FOODTECH_TONKATSU_BROKER_URL": f"https://{network['BrokerHost']}:8446",
                             "FOODTECH_TONKATSU_GATEWAY_RECORDS_URL": f"https://{network['GatewayHost']}:8447"},
            "broker": {"FOODTECH_TONKATSU_GATEWAY_URL": f"https://{network['GatewayHost']}:8445"},
            "gateway": {"FOODTECH_TONKATSU_OLLAMA_PRIVATE_ORIGIN": f"https://{network['InferenceHost']}:8448"},
        }
        require(all(environment.get(key) == value for key, value in expected_origins.get(name, {}).items()),
                "task_private_origins")
        secrets = metadata_fields(container.get("secrets", []), "name", "valueFrom")
        if name == "ollama":
            require(environment["OLLAMA_HOST"] == "127.0.0.1:11434" and environment["OLLAMA_NO_CLOUD"] == "1"
                    and not secrets, "local_model_boundary")
        else:
            require(secrets and all(re.fullmatch(fr"{re.escape(bundle)}:[a-z0-9_]+::{re.escape(version)}", value)
                                    for value in secrets.values()), "task_tls_reference")


def preflight(operator, plan, evidence):
    evidence["phase"] = "stack_metadata"
    cf = operator.client("cloudformation")
    snapshots = {}
    for name, digest in sorted(plan["templates"].items()):
        evidence["last_checked_stack"] = name
        snapshots[name] = stack_metadata(cf, name, digest)
    evidence.pop("last_checked_stack")
    network, network_parameters = snapshots[NETWORK]
    service_outputs, service_parameters = snapshots[SERVICES]
    evidence["phase"] = "network"
    check_network(operator.client("ec2"), operator.client("servicediscovery"),
                  network, network_parameters, plan["network_mode"])
    evidence["phase"] = "images"
    ecr = operator.client("ecr")
    images = {}
    for flavor, digest in plan["image_digests"].items():
        evidence["workload"] = flavor
        repo = f"foodtech/tonkatsu-{flavor}-dev"
        (repository,) = ecr.describe_repositories(registryId=ACCOUNT, repositoryNames=[repo])["repositories"]
        uri = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{repo}"
        require(repository["repositoryUri"] == uri and repository["imageTagMutability"] == "IMMUTABLE"
                and repository["imageScanningConfiguration"]["scanOnPush"]
                and repository["encryptionConfiguration"]["encryptionType"] == "AES256", "reviewed_repository")
        (detail,) = ecr.describe_images(registryId=ACCOUNT, repositoryName=repo, imageIds=[{"imageDigest": digest}])["imageDetails"]
        require(detail["imageDigest"] == digest and detail["repositoryName"] == repo and detail["registryId"] == ACCOUNT,
                "published_image_digest")
        images[flavor] = uri + "@" + digest
    ecs = operator.client("ecs")
    sm = operator.client("secretsmanager")
    tasks = {}
    version = plan["tls_version"]
    for label, (stack, prefix) in TLS_STACKS.items():
        evidence["workload"] = label
        evidence["phase"] = "tls_metadata"
        bundle = snapshots[stack][0][prefix + "TlsBundleArn"]
        name = WORKLOADS[label][0]
        require(re.fullmatch(fr"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:foodtech/tonkatsu-{name}-dev/tls-[A-Za-z0-9]{{6}}", bundle),
                "reviewed_tls_arn")
        secret = sm.describe_secret(SecretId=bundle)
        require(secret["ARN"] == bundle and secret["KmsKeyId"] == snapshots[stack][0][prefix + "TlsKeyArn"]
                and not secret.get("DeletedDate") and secret.get("VersionIdsToStages", {}).get(version),
                "reviewed_tls_metadata")
        evidence["phase"] = "task_metadata"
        tasks[label] = snapshots[TASK_STACKS[label]][0][label + "TaskDefinitionArn"]
        check_task(ecs, label, tasks[label], images, bundle, version, network)
    evidence.pop("workload")
    evidence["phase"] = "service_metadata"
    check_services(ecs, network, service_outputs, service_parameters, tasks)
    return {"status": "verified_stopped_trial", "mode": plan["network_mode"], "images": len(images),
            "task_revisions": len(tasks), "stacks": len(snapshots)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-plan", type=Path, required=True)
    arguments = parser.parse_args(argv)
    evidence = {"phase": "reviewed_plan"}
    try:
        plan = checked_plan(json.loads(arguments.reviewed_plan.read_text()))
        verify_operator_environment()
        evidence["phase"] = "identity"
        result = preflight(ReadOnlyOperator(), plan, evidence)
    except (BotoCoreError, ClientError, OSError, ValueError, TypeError, KeyError) as failure:
        reason = str(failure) if isinstance(failure, ReviewMismatch) else type(failure).__name__
        print(json.dumps({"status": "not_verified", "reason": reason, **evidence}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
