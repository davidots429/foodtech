import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, 'path', [str(ROOT / 'infra/scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('tonkatsu_broker_publish', ROOT / 'infra/scripts/tonkatsu_broker_publish.py')
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)


class TonkatsuBrokerPublisherTests(unittest.TestCase):
    def setUp(self):
        self.env = {'GITHUB_REPOSITORY': publish.common.REPOSITORY, 'GITHUB_REPOSITORY_ID': publish.common.REPOSITORY_ID,
            'GITHUB_REPOSITORY_OWNER_ID': publish.common.OWNER_ID, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
            'PUBLISH_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-tonkatsu-broker-publish-dev',
            'PUBLISH_ACCOUNT_ID': '123456789012', 'EXPECTED_SOURCE_SHA': 'a' * 40}
        self.repository = {'registryId': '123456789012', 'repositoryName': 'foodtech/tonkatsu-broker-dev',
            'repositoryUri': '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/tonkatsu-broker-dev',
            'repositoryArn': 'arn:aws:ecr:ap-southeast-2:123456789012:repository/foodtech/tonkatsu-broker-dev',
            'imageTagMutability': 'IMMUTABLE', 'encryptionConfiguration': {'encryptionType': 'AES256'},
            'imageScanningConfiguration': {'scanOnPush': True}}

    def test_target_manifest_and_retry_bind_the_broker_destination(self):
        values = publish.target(self.env)
        self.assertEqual(values['repository_name'], 'foodtech/tonkatsu-broker-dev')
        self.assertEqual(values['workload'], 'tonkatsu-broker')
        digest = 'sha256:' + 'b' * 64
        first = publish.manifest(self.env, digest)
        self.assertEqual(first['image_uri'], values['repository'] + '@' + digest)
        second = publish.manifest({**self.env, 'GITHUB_RUN_ATTEMPT': '2'}, digest)
        self.assertNotEqual(first['image_tag'], second['image_tag'])
        for invalid in ('latest', digest + '\n', 'sha256:bad'):
            with self.assertRaises(ValueError): publish.manifest(self.env, invalid)

    def test_foreign_role_branch_account_or_injected_tag_is_rejected(self):
        for key, value in [('PUBLISH_ROLE_ARN', 'arn:aws:iam::123456789012:role/foodtech-maintenance-publish-dev'),
                ('EXPECTED_SOURCE_SHA', 'b' * 40), ('EXPECTED_SOURCE_SHA', ''),
                ('PUBLISH_ACCOUNT_ID', '000000000000'), ('GITHUB_REF', 'refs/heads/feature'),
                ('GITHUB_SHA', 'a' * 40 + '\ncommand'), ('GITHUB_RUN_ID', '42;command')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish.target({**self.env, key: value})

    def test_repository_requires_the_only_reviewed_immutable_destination(self):
        verify = publish.verify_repository
        self.assertEqual(verify(self.env, {"repositories": [self.repository]}), {"repository_verified": True})
        for field, replacement in (("repositoryArn", "another"), ("imageTagMutability", "MUTABLE"),
                                   ("imageScanningConfiguration", {"scanOnPush": False})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify(self.env, {"repositories": [{**self.repository, field: replacement}]})

    def test_gateway_destination_rejects_broker_identity_and_repository(self):
        gateway_env = {**self.env, "PUBLISH_ROLE_ARN": self.env["PUBLISH_ROLE_ARN"].replace("broker", "gateway")}
        gateway_repo = {key: value.replace("tonkatsu-broker", "tonkatsu-gateway") if isinstance(value, str) else value
                        for key, value in self.repository.items()}
        self.assertEqual(publish.manifest(gateway_env, "sha256:" + "f" * 64, service="gateway")["workload"],
                         "tonkatsu-gateway")
        self.assertEqual(publish.verify_repository(gateway_env, {"repositories": [gateway_repo]}, service="gateway"),
                         {"repository_verified": True})
        for environment, repository in ((self.env, gateway_repo), (gateway_env, self.repository)):
            with self.assertRaises(ValueError):
                publish.verify_repository(environment, {"repositories": [repository]}, service="gateway")
        self.assertNotEqual(publish.WORKFLOW, publish.workflow("gateway"))

    def test_gateway_workflow_has_no_broker_role_or_destination(self):
        source = (ROOT / ".github/workflows/publish-tonkatsu-gateway-image.yml").read_text()
        self.assertNotIn("TONKATSU_BROKER", source)
        self.assertNotIn("foodtech/tonkatsu-broker-dev", source)
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow["on"]), {"workflow_dispatch"})
        self.assertEqual(workflow["on"]["workflow_dispatch"]["inputs"]["operation"]["default"], "inspect")
        job, = workflow["jobs"].values()
        build = next(step for step in job["steps"] if step.get("uses", "").startswith("docker/build-push-action"))
        self.assertEqual(build["with"]["build-args"], "FOODTECH_PACKAGE=foodtech-ai-gateway")
        for entrypoint in ("serve_tonkatsu", "serve_tonkatsu_business", "serve_tonkatsu_inference"):
            self.assertIn(f"import foodtech_ai_gateway.{entrypoint};", source)
        for step in job["steps"]:
            if "aws " in step.get("run", "") or step.get("uses", "").startswith(("aws-actions/", "docker/")):
                self.assertEqual(step["if"], "inputs.operation == 'publish'")
            if "run" in step:
                subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
        for mode in ("inspect", "target", "verify-repository", "manifest"):
            self.assertIn(f"tonkatsu_broker_publish.py {mode} --service gateway", source)

    def test_workflow_guards_and_build_precede_credentials_and_login(self):
        workflow = yaml.load((ROOT / '.github/workflows/publish-tonkatsu-broker-image.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['default'], 'inspect')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        job, = workflow['jobs'].values()
        self.assertEqual(job['permissions'], {'contents': 'read', 'id-token': 'write'})
        steps = job['steps']
        build = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('docker/build-push-action@'))
        guards = next(i for i, s in enumerate(steps) if s.get('name', '').startswith('Verify broker entry point'))
        credentials = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('aws-actions/configure-aws-credentials@'))
        verify_repo = next(i for i, s in enumerate(steps) if s.get('name', '').startswith('Verify reviewed repository'))
        push = next(i for i, s in enumerate(steps) if s.get('name', '').startswith('Publish immutable'))
        self.assertLess(build, guards);self.assertLess(guards, credentials)
        self.assertLess(credentials, verify_repo);self.assertLess(verify_repo, push)
        self.assertEqual(steps[build]['with']['push'], 'false')
        self.assertEqual(steps[build]['with']['platforms'], 'linux/amd64')
        for s in steps:
            if 'aws ' in s.get('run', '') or s.get('uses', '').startswith(('aws-actions/', 'docker/')):
                self.assertEqual(s['if'], "inputs.operation == 'publish'")
            if 'run' in s:subprocess.run(['bash', '-n'], input=s['run'], text=True, check=True)
        for value in ("foodtech_business_api.serve_tonkatsu_broker",
                      "foodtech_business_api.serve_tonkatsu_business_api", "/run/foodtech-tls",
                      "--network none", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges"):
            self.assertIn(value, steps[guards]["run"])
        self.assertEqual(steps[build]["with"]["build-args"], "FOODTECH_PACKAGE=foodtech-business-api")
        self.assertEqual(job["env"]["EXPECTED_SOURCE_SHA"], "${{ inputs.expected_sha }}")
        checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with'], {'ref': '${{ github.sha }}', 'persist-credentials': 'false'})


if __name__ == '__main__':
    unittest.main()
