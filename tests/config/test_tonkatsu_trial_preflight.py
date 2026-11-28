import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, "path", [str(ROOT / "infra/scripts"), *sys.path]):
    import tonkatsu_trial_preflight as preflight


class TonkatsuTrialPreflightTests(unittest.TestCase):
    def setUp(self):
        self.version = "b90666a0-6d21-4d56-a463-387e3529cb3a"
        self.plan = {
            "network_mode": "isolated",
            "templates": {name: preflight.canonical_hash({"description": name})
                          for name in preflight.REVIEWED_STACKS},
            "tls_version": self.version,
            "image_digests": {name: "sha256:" + letter * 64
                              for name, letter in zip(("broker", "gateway", "ollama"), "abc", strict=True)},
        }
        self.clients = {name: Mock() for name in
                        ("cloudformation", "ecs", "ec2", "ecr", "servicediscovery", "secretsmanager")}
        self.operator = Mock()
        self.operator.client.side_effect = self.clients.__getitem__
        self.outputs = {stack: {} for stack in preflight.REVIEWED_STACKS}
        self.parameters = {stack: {} for stack in preflight.REVIEWED_STACKS}
        self.network = self.outputs[preflight.NETWORK]
        cluster = f"{preflight.SCHEME}:cluster/foodtech-tonkatsu-private-dev"
        self.network.update(ClusterArn=cluster, VpcId="vpc-12345678", IsolatedSubnetId="subnet-12345678",
                            BusinessSubnetId="subnet-12345678", TesterSecurityGroupId="sg-00000000",
                            ImageAccessSecurityGroupId="sg-00000005", WorkflowAccessSecurityGroupId="sg-00000006")
        self.parameters[preflight.NETWORK].update(S3PrefixListId="pl-00000001", DynamoDbPrefixListId="pl-00000002")
        service_parameters = self.parameters[preflight.SERVICES]
        service_parameters.update(ClusterArn=cluster, DesiredCountPerService="0", BusinessSubnetId="subnet-12345678",
                                  IsolatedSubnetId="subnet-12345678")
        self.services = []
        self.tasks = {}
        self.registries = {}
        self.secrets = {}
        self.groups = []
        self.repo_prefix = f"{preflight.ACCOUNT}.dkr.ecr.{preflight.REGION}.amazonaws.com/"
        for idx, (label, (name, containers)) in enumerate(preflight.WORKLOADS.items(), 1):
            family = f"foodtech-tonkatsu-{name}-dev"
            task = f"{preflight.SCHEME}:task-definition/{family}:1"
            service = f"{preflight.SCHEME}:service/foodtech-tonkatsu-private-dev/{family}"
            registry_id = f"srv-{label.lower()}"
            registry = f"arn:aws:servicediscovery:{preflight.REGION}:{preflight.ACCOUNT}:service/{registry_id}"
            group = f"sg-{idx:08d}"
            bundle = f"arn:aws:secretsmanager:{preflight.REGION}:{preflight.ACCOUNT}:secret:foodtech/tonkatsu-{name}-dev/tls-abc123"
            key = f"arn:aws:kms:{preflight.REGION}:{preflight.ACCOUNT}:key/fake-{idx}"
            tls_stack, prefix = preflight.TLS_STACKS[label]
            self.outputs[tls_stack].update({prefix + "TlsBundleArn": bundle, prefix + "TlsKeyArn": key})
            self.secrets[bundle] = {"ARN": bundle, "KmsKeyId": key, "VersionIdsToStages": {self.version: ["AWSCURRENT"]}}
            self.outputs[preflight.TASK_STACKS[label]][label + "TaskDefinitionArn"] = task
            self.outputs[preflight.SERVICES][label + "ServiceArn"] = service
            self.network.update({label + "Host": f"{name}.{preflight.DOMAIN}", label + "DiscoveryArn": registry,
                                 label + "SecurityGroupId": group})
            service_parameters.update({label + "DiscoveryArn": registry, label + "SecurityGroupId": group,
                                       label + "TaskDefinitionArn": task})
            self.registries[registry_id] = {"Arn": registry, "Name": name, "NamespaceId": "ns-shared",
                                           "DnsConfig": {"DnsRecords": [{"Type": "A", "TTL": 10}]}}
            self.groups.append({"GroupId": group, "VpcId": self.network["VpcId"], "IpPermissions": [],
                                "IpPermissionsEgress": []})
            self.services.append({
                "serviceName": family, "serviceArn": service, "clusterArn": cluster, "status": "ACTIVE",
                "launchType": "FARGATE", "platformVersion": "1.4.0", "taskDefinition": task,
                "desiredCount": 0, "runningCount": 0, "pendingCount": 0,
                "serviceRegistries": [{"registryArn": registry}], "networkConfiguration": {"awsvpcConfiguration": {
                    "assignPublicIp": "DISABLED", "subnets": ["subnet-12345678"], "securityGroups": [group]}},
            })
            self.tasks[task] = {
                "taskDefinitionArn": task, "status": "ACTIVE", "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"],
                "runtimePlatform": {"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
                "executionRoleArn": f"arn:aws:iam::{preflight.ACCOUNT}:role/foodtech-tonkatsu-{name}-execution-dev",
                "containerDefinitions": [self.container(container, repository, port, bundle)
                                         for container, (repository, port) in containers.items()],
            }
            if label != "Inference":
                self.tasks[task]["taskRoleArn"] = f"arn:aws:iam::{preflight.ACCOUNT}:role/{family}"
        for label, group in zip(preflight.WORKLOADS, self.groups, strict=True):
            expected = {"Business": [("Tester", 8080)], "Broker": [("Business", 8446)],
                        "Gateway": [("Broker", 8445), ("Business", 8447)], "Inference": [("Gateway", 8448)]}
            group["IpPermissions"] = [{"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                                       "UserIdGroupPairs": [{"GroupId": self.network[peer + "SecurityGroupId"]}]}
                                      for peer, port in expected[label]]
        outward = {
            "Business": [("Broker", 8446), ("Gateway", 8447)],
            "Broker": [("Gateway", 8445), ("WorkflowAccess", 443)],
            "Gateway": [("Inference", 8448), ("WorkflowAccess", 443)],
            "Inference": [],
        }
        for label, group in zip(preflight.WORKLOADS, self.groups, strict=True):
            group["IpPermissionsEgress"] = [
                {"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                 "UserIdGroupPairs": [{"GroupId": self.network[peer + "SecurityGroupId"]}]}
                for peer, port in [("ImageAccess", 443), *outward[label]]
            ] + [{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                  "PrefixListIds": [{"PrefixListId": "pl-00000001"}] +
                                   ([{"PrefixListId": "pl-00000002"}] if label != "Inference" else [])}]
        self.clients["cloudformation"].describe_stacks.side_effect = self.stack
        self.clients["cloudformation"].get_template.side_effect = lambda **kw: {
            "TemplateBody": {"description": kw["StackName"].split("/")[-2]},
        }
        self.clients["ec2"].describe_subnets.return_value = {"Subnets": [{
            "SubnetId": "subnet-12345678", "MapPublicIpOnLaunch": False, "VpcId": self.network["VpcId"],
            "State": "available",
        }]}
        self.infrastructure_groups = [{"GroupId": self.network[name + "SecurityGroupId"], "VpcId": self.network["VpcId"]}
                                      for name in ("Tester", "ImageAccess", "WorkflowAccess")]
        self.clients["ec2"].describe_security_groups.side_effect = lambda **_: {
            "SecurityGroups": self.groups + self.infrastructure_groups,
        }
        self.clients["servicediscovery"].get_service.side_effect = lambda Id: {"Service": self.registries[Id]}
        self.clients["servicediscovery"].get_namespace.return_value = {
            "Namespace": {"Name": preflight.DOMAIN, "Type": "DNS_PRIVATE"},
        }
        self.clients["ecr"].describe_repositories.side_effect = lambda registryId, repositoryNames: {"repositories": [{
            "repositoryUri": self.repo_prefix + repositoryNames[0], "imageTagMutability": "IMMUTABLE",
            "imageScanningConfiguration": {"scanOnPush": True}, "encryptionConfiguration": {"encryptionType": "AES256"},
        }]}
        self.clients["ecr"].describe_images.side_effect = lambda registryId, repositoryName, imageIds: {
            "imageDetails": [{"imageDigest": imageIds[0]["imageDigest"], "repositoryName": repositoryName,
                              "registryId": registryId}],
        }
        self.clients["secretsmanager"].describe_secret.side_effect = lambda SecretId: self.secrets[SecretId]
        self.clients["ecs"].describe_task_definition.side_effect = lambda taskDefinition: {
            "taskDefinition": self.tasks[taskDefinition],
        }
        self.clients["ecs"].describe_services.return_value = {"services": self.services}

    def stack(self, StackName):
        return {"Stacks": [{
            "StackName": StackName, "StackStatus": "CREATE_COMPLETE",
            "StackId": f"arn:aws:cloudformation:{preflight.REGION}:{preflight.ACCOUNT}:stack/{StackName}/id",
            "Tags": [{"Key": "Project", "Value": "foodtech"}, {"Key": "Environment", "Value": "dev"}],
            "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in self.outputs[StackName].items()],
            "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in self.parameters[StackName].items()],
        }]}

    def container(self, name, repository, port, bundle):
        variables = {
            "business-api": {"FOODTECH_TONKATSU_BROKER_URL": f"https://broker.{preflight.DOMAIN}:8446",
                             "FOODTECH_TONKATSU_GATEWAY_RECORDS_URL": f"https://gateway.{preflight.DOMAIN}:8447"},
            "broker": {"FOODTECH_TONKATSU_GATEWAY_URL": f"https://gateway.{preflight.DOMAIN}:8445"},
            "gateway": {"FOODTECH_TONKATSU_OLLAMA_PRIVATE_ORIGIN": f"https://inference.{preflight.DOMAIN}:8448"},
            "ollama": {"OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_NO_CLOUD": "1"},
        }
        return {"name": name, "image": f"{self.repo_prefix}foodtech/tonkatsu-{repository}-dev@{self.plan['image_digests'][repository]}",
                "readonlyRootFilesystem": True, "essential": True, "healthCheck": {"command": ["CMD", "check"]},
                "portMappings": [{"containerPort": port, "protocol": "tcp"}] if port else [],
                "secrets": [{"name": "TLS", "valueFrom": f"{bundle}:server_cert_pem::{self.version}"}] if port else [],
                "environment": [{"name": key, "value": value} for key, value in variables.get(name, {}).items()]}

    def run_preflight(self):
        return preflight.preflight(self.operator, preflight.checked_plan(self.plan), {})

    def test_reads_reviewed_stopped_trial_without_loading_secret_values(self):
        self.assertEqual(self.run_preflight(), {"status": "verified_stopped_trial", "mode": "isolated",
            "images": 3, "task_revisions": 4, "stacks": 9})
        self.assertEqual(len(self.clients["secretsmanager"].mock_calls), 4)
        self.assertTrue(all(call[0] == "describe_secret" for call in self.clients["secretsmanager"].mock_calls))

    def test_managed_login_egress_is_limited_to_business_subnet_and_role(self):
        self.plan["network_mode"] = "managed-login"
        self.network.update(BusinessSubnetId="subnet-87654321", AuthAccessSecurityGroupId="sg-00000007")
        self.parameters[preflight.SERVICES]["BusinessSubnetId"] = "subnet-87654321"
        self.services[0]["networkConfiguration"]["awsvpcConfiguration"]["subnets"] = ["subnet-87654321"]
        self.clients["ec2"].describe_subnets.return_value["Subnets"].append({
            "SubnetId": "subnet-87654321", "MapPublicIpOnLaunch": False, "VpcId": self.network["VpcId"],
            "State": "available",
        })
        self.infrastructure_groups.append({"GroupId": "sg-00000007", "VpcId": self.network["VpcId"]})
        self.groups[0]["IpPermissionsEgress"].append({"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}], "UserIdGroupPairs": [{"GroupId": "sg-00000007"}]})
        self.assertEqual(self.run_preflight()["mode"], "managed-login")
        self.groups[-1]["IpPermissionsEgress"].append({"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
            "IpRanges": [{"CidrIp": "0.0.0.0/0"}]})
        with self.assertRaisesRegex(preflight.ReviewMismatch, "exact_egress_rules"):
            self.run_preflight()

    def test_rejects_stack_template_drift_before_reading_images_or_ecs(self):
        self.plan["templates"][preflight.NETWORK] = "0" * 64
        with self.assertRaisesRegex(preflight.ReviewMismatch, "stack_template_digest"):
            self.run_preflight()
        self.assertFalse(self.clients["ecs"].mock_calls)
        self.assertFalse(self.clients["ecr"].mock_calls)

    def test_rejects_unguarded_inference_ingress(self):
        self.groups[-1]["IpPermissions"][0]["IpRanges"] = [{"CidrIp": "0.0.0.0/0"}]
        with self.assertRaisesRegex(preflight.ReviewMismatch, "exact_ingress_rules"):
            self.run_preflight()

    def test_rejects_inference_egress_drift(self):
        self.groups[-1]["IpPermissionsEgress"][0]["UserIdGroupPairs"][0]["GroupId"] = "sg-00000007"
        with self.assertRaisesRegex(preflight.ReviewMismatch, "exact_egress_rules"):
            self.run_preflight()

    def test_rejects_task_origin_outside_reviewed_dns(self):
        task = next(iter(self.tasks.values()))
        task["containerDefinitions"][0]["environment"][0]["value"] = "https://unreviewed.invalid"
        with self.assertRaisesRegex(preflight.ReviewMismatch, "task_private_origins"):
            self.run_preflight()

    def test_rejects_live_count_or_wrong_service_binding(self):
        service = self.services[0]
        original = copy.deepcopy(service)
        for field, value, phase in (("runningCount", 1, "service_stopped"),
                                    ("taskDefinition", "other", "service_parameter_binding"),
                                    ("serviceRegistries", [], "private_service_attachment")):
            with self.subTest(field=field), self.assertRaisesRegex(preflight.ReviewMismatch, phase):
                service[field] = value
                try:
                    self.run_preflight()
                finally:
                    service.update(original)

    def test_rejects_replaced_digest_and_unknown_tls_version(self):
        task = next(iter(self.tasks.values()))
        container = task["containerDefinitions"][0]
        original = container["image"]
        container["image"] = original.split("@")[0] + ":latest"
        with self.assertRaisesRegex(preflight.ReviewMismatch, "task_container_image_and_health"):
            self.run_preflight()
        container["image"] = original
        next(iter(self.secrets.values()))["VersionIdsToStages"] = {}
        with self.assertRaisesRegex(preflight.ReviewMismatch, "reviewed_tls_metadata"):
            self.run_preflight()


if __name__ == "__main__":
    unittest.main()
