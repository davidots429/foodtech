"""Exercise metadata-only inspection, recovery and incomplete writes without using AWS."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, "path", [str(ROOT / "infra/scripts"), *sys.path]):
    import tonkatsu_trial_tls_initialize as initializer


class TonkatsuTlsInitializationTests(unittest.TestCase):
    def setUp(self):
        self.request_id = str(uuid4())
        self.plan = {"request_id": self.request_id, "broker_host": "broker.foodtech.internal",
                     "gateway_host": "gateway.foodtech.internal",
                     "stacks": {initializer.BROKER_STACK: "f" * 64, initializer.PEER_STACK: "a" * 64},
                     "stores": {role: {
                         "arn": f"arn:aws:secretsmanager:{initializer.REGION}:{initializer.ACCOUNT}:secret:foodtech/tonkatsu-{role}-dev/tls-Ab1234",
                         "key": f"arn:aws:kms:{initializer.REGION}:{initializer.ACCOUNT}:key/{uuid4()}",
                     } for role, _, _ in initializer.DESTINATIONS}}
        self.stores = {row.arn: row for row in initializer.reviewed_plan(self.plan)}
        self.candidate_stage = "foodtech-tonkatsu-trial-" + self.request_id
        self.progress = {"attempted_secret_writes": 0, "acknowledged_secret_writes": 0,
                         "observed_secret_versions": 0}
        self.versions = {}
        self.client = Mock()
        self.sdk = Mock()
        self.sdk.client.return_value = self.client
        self.client.get_resource_policy.return_value = {}
        self.client.put_secret_value.side_effect = self.put
        self.client.get_secret_value.side_effect = self.get

    def put(self, *, SecretId, ClientRequestToken, SecretString, VersionStages):
        self.assertEqual(ClientRequestToken, self.request_id)
        self.assertEqual(VersionStages, [self.candidate_stage])
        self.assertNotIn(SecretId, self.versions)
        self.versions[SecretId] = SecretString
        return {"ARN": SecretId, "Name": self.stores[SecretId].name, "VersionId": ClientRequestToken,
                "VersionStages": [*VersionStages, "AWSCURRENT"]}

    def get(self, *, SecretId, VersionId):
        return {"ARN": SecretId, "Name": self.stores[SecretId].name, "VersionId": VersionId,
                "VersionStages": ["AWSCURRENT", self.candidate_stage], "SecretString": self.versions[SecretId]}

    def run_controlled(self, *, apply=False):
        def verify(sm, kms, store, token, progress, **kwargs):
            self.assertEqual(token, self.request_id)
            self.assertEqual(set(kwargs["required_stages"]), {"AWSCURRENT", self.candidate_stage})
            return store.arn in self.versions

        with patch.object(initializer, "_verify_stack"), patch.object(initializer, "verify_store", side_effect=verify):
            return initializer.initialize(self.sdk, self.plan, self.progress, apply=apply)

    def test_inspection_never_reads_or_writes_values(self):
        result = self.run_controlled()
        self.assertEqual(result["matching_versions"], 0)
        self.assertFalse(result["values_read"])
        self.client.get_secret_value.assert_not_called()
        self.client.put_secret_value.assert_not_called()

    def test_complete_set_can_be_verified_again_without_writes(self):
        first = self.run_controlled(apply=True)
        second = self.run_controlled(apply=True)
        self.assertFalse(first["recovered_existing"])
        self.assertTrue(second["recovered_existing"])
        self.assertEqual(first["versions"], second["versions"])
        self.assertEqual(first["leaf_fingerprints_sha256"], second["leaf_fingerprints_sha256"])
        self.assertEqual(self.client.put_secret_value.call_count, 3)
        self.assertEqual(self.client.get_secret_value.call_count, 6)
        self.assertNotIn("PRIVATE KEY", str(second))

    def test_records_link_recovers_with_its_own_pins_and_no_broker_key_change(self):
        self.plan["records_host"] = "records.foodtech.internal"
        first = self.run_controlled(apply=True)
        second = self.run_controlled(apply=True)
        self.assertEqual(first["hosts"]["records_host"], "records.foodtech.internal")
        self.assertEqual(len(set(first["leaf_fingerprints_sha256"])), 6)
        self.assertEqual(first["leaf_fingerprints_sha256"], second["leaf_fingerprints_sha256"])
        self.assertTrue(second["recovered_existing"])
        self.assertEqual(self.client.put_secret_value.call_count, 3)
        business, broker, gateway = [json.loads(value) for value in self.versions.values()]
        self.assertNotIn("records_client_key_pem", broker)
        self.assertNotIn("records_client_key_pem", gateway)
        self.assertNotIn("records_server_key_pem", business)
        self.assertNotEqual(gateway["records_client_ca_pem"], gateway["broker_ca_pem"])
        business_arn = next(iter(self.versions))
        business["records_client_cert_pem"] = business["bff_cert_pem"]
        self.versions[business_arn] = json.dumps(business)
        with self.assertRaisesRegex(ValueError, "TLS records pins differ"):
            self.run_controlled(apply=True)
        self.assertEqual(self.client.put_secret_value.call_count, 3)

    def test_inference_registration_has_separate_gateway_and_host_keys(self):
        self.plan["records_host"] = "records.foodtech.internal"
        self.plan["inference_host"] = "inference.foodtech.internal"
        self.plan["stacks"][initializer.INFERENCE_STACK] = "e" * 64
        self.plan["stores"]["inference"] = {
            "arn": f"arn:aws:secretsmanager:{initializer.REGION}:{initializer.ACCOUNT}:secret:foodtech/tonkatsu-inference-dev/tls-Ab1234",
            "key": f"arn:aws:kms:{initializer.REGION}:{initializer.ACCOUNT}:key/{uuid4()}",
        }
        self.stores = {row.arn: row for row in initializer.reviewed_plan(self.plan)}
        first = self.run_controlled(apply=True)
        second = self.run_controlled(apply=True)
        self.assertEqual(len(set(first["leaf_fingerprints_sha256"])), 8)
        self.assertEqual(first["leaf_fingerprints_sha256"], second["leaf_fingerprints_sha256"])
        self.assertEqual(self.client.put_secret_value.call_count, 4)
        self.assertNotIn("PRIVATE KEY", str(second))
        business, broker, gateway, inference = [json.loads(value) for value in self.versions.values()]
        self.assertTrue(all(not any(key.startswith("inference_") for key in bundle) for bundle in (business, broker)))
        self.assertNotIn("inference_server_key_pem", gateway)
        self.assertNotIn("inference_client_key_pem", inference)
        self.assertNotEqual(inference["inference_client_ca_pem"], gateway["records_client_ca_pem"])
        inference_arn = self.plan["stores"]["inference"]["arn"]
        inference["inference_client_cert_pem"] = gateway["broker_cert_pem"]
        self.versions[inference_arn] = json.dumps(inference)
        with self.assertRaisesRegex(ValueError, "TLS inference pins differ"):
            self.run_controlled(apply=True)
        self.assertEqual(self.client.put_secret_value.call_count, 4)

    def test_partial_registration_cannot_write_or_read_more(self):
        self.versions[next(iter(self.stores))] = "contents do not matter"
        with self.assertRaisesRegex(ValueError, "partial TLS registration"):
            self.run_controlled(apply=True)
        self.client.get_secret_value.assert_not_called()
        self.client.put_secret_value.assert_not_called()

    def test_full_set_with_mismatched_bundles_cannot_issue_receipt(self):
        self.run_controlled(apply=True)
        first, _, last = self.stores
        self.versions[first] = self.versions[last]
        with self.assertRaisesRegex(ValueError, "unexpected TLS bundle fields"):
            self.run_controlled(apply=True)
        self.assertEqual(self.client.put_secret_value.call_count, 3)

    def test_complete_stack_ownership_is_checked_before_secret_calls(self):
        with patch.object(initializer, "_verify_stack", side_effect=ValueError("unreviewed stack")):
            with self.assertRaisesRegex(ValueError, "unreviewed stack"):
                initializer.initialize(self.sdk, self.plan, self.progress, apply=True)
        self.client.describe_secret.assert_not_called()
        self.client.put_secret_value.assert_not_called()

    def test_secret_with_unreviewed_resource_policy_cannot_be_written(self):
        self.client.get_resource_policy.return_value = {"ResourcePolicy": "unreviewed"}
        with self.assertRaisesRegex(ValueError, "unexpected TLS Secret resource policy"):
            self.run_controlled(apply=True)
        self.client.put_secret_value.assert_not_called()

    def test_reviewed_stack_must_match_protected_resource_ownership(self):
        targets = [target for target in self.stores.values() if target.stack == initializer.PEER_STACK]
        template = {"Resources": {}}
        stack = {"StackName": initializer.PEER_STACK, "StackStatus": "CREATE_COMPLETE",
                 "StackId": f"arn:aws:cloudformation:{initializer.REGION}:{initializer.ACCOUNT}:stack/{initializer.PEER_STACK}/{uuid4()}",
                 "EnableTerminationProtection": True,
                 "Tags": [{"Key": "Project", "Value": "foodtech"}, {"Key": "Environment", "Value": "dev"}],
                 "Outputs": [{"OutputKey": target.prefix + suffix, "OutputValue": value}
                             for target in targets for suffix, value in (("TlsBundleArn", target.arn), ("TlsKeyArn", target.key))]}
        self.client.describe_stacks.return_value = {"Stacks": [stack]}
        self.client.get_template.return_value = {"TemplateBody": template}
        rows = [{"LogicalResourceId": target.prefix + suffix, "PhysicalResourceId": physical,
                 "ResourceType": kind, "ResourceStatus": "CREATE_COMPLETE"} for target in targets
                for suffix, physical, kind in (("TlsKey", target.key.rsplit("/", 1)[-1], "AWS::KMS::Key"),
                                               ("TlsBundle", target.arn, "AWS::SecretsManager::Secret"))]
        self.client.describe_stack_resources.return_value = {"StackResources": rows}
        arguments = (self.client, initializer.PEER_STACK, initializer.canonical_hash(template), targets)
        initializer._verify_stack(*arguments)
        rows[0]["PhysicalResourceId"] = str(uuid4())
        with self.assertRaisesRegex(ValueError, "TLS resource ownership"):
            initializer._verify_stack(*arguments)
        stack["EnableTerminationProtection"] = False
        with self.assertRaisesRegex(ValueError, "protected reviewed TLS stack"):
            initializer._verify_stack(*arguments)
