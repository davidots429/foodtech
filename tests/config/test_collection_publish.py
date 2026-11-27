import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
with patch.object(sys, 'path', [str(ROOT / 'infra/scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('collection_publish', ROOT / 'infra/scripts/collection_publish.py')
    publish = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish)


class CollectionPublisherTests(unittest.TestCase):
    def setUp(self):
        self.env = {'GITHUB_REPOSITORY': publish.common.REPOSITORY, 'GITHUB_REPOSITORY_ID': publish.common.REPOSITORY_ID,
            'GITHUB_REPOSITORY_OWNER_ID': publish.common.OWNER_ID, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
            'PUBLISH_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-collection-publish-dev',
            'PUBLISH_ACCOUNT_ID': '123456789012'}
        self.repository = {'registryId': '123456789012', 'repositoryName': 'foodtech/collection-dev',
            'repositoryUri': '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/collection-dev',
            'repositoryArn': 'arn:aws:ecr:ap-southeast-2:123456789012:repository/foodtech/collection-dev',
            'imageTagMutability': 'IMMUTABLE', 'encryptionConfiguration': {'encryptionType': 'AES256'},
            'imageScanningConfiguration': {'scanOnPush': True}}

    def test_target_manifest_and_retry_bind_the_collection_destination(self):
        values = publish.target(self.env)
        self.assertEqual(values['repository_name'], 'foodtech/collection-dev')
        self.assertEqual(values['workload'], 'collection')
        digest = 'sha256:' + 'b' * 64
        first = publish.manifest(self.env, digest)
        self.assertEqual(first['image_uri'], values['repository'] + '@' + digest)
        second = publish.manifest({**self.env, 'GITHUB_RUN_ATTEMPT': '2'}, digest)
        self.assertNotEqual(first['image_tag'], second['image_tag'])
        for invalid in ('latest', digest + '\n', 'sha256:bad'):
            with self.assertRaises(ValueError): publish.manifest(self.env, invalid)

    def test_foreign_role_branch_account_or_injected_tag_is_rejected(self):
        for key, value in [('PUBLISH_ROLE_ARN', 'arn:aws:iam::123456789012:role/foodtech-maintenance-publish-dev'),
                ('PUBLISH_ACCOUNT_ID', '000000000000'), ('GITHUB_REF', 'refs/heads/feature'),
                ('GITHUB_SHA', 'a' * 40 + '\ncommand'), ('GITHUB_RUN_ID', '42;command')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish.target({**self.env, key: value})

    def test_claims_require_the_collection_workflow_and_discard_private_fields(self):
        claims = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'sts.amazonaws.com',
            'repository': publish.common.REPOSITORY, 'repository_id': publish.common.REPOSITORY_ID,
            'repository_owner_id': publish.common.OWNER_ID, 'ref': 'refs/heads/main', 'sha': 'a' * 40,
            'event_name': 'workflow_dispatch', 'workflow_ref': publish.WORKFLOW,
            'sub': 'repo:davidots429/foodtech:ref:refs/heads/main', 'nbf': 50, 'exp': 150, 'secret': 'private-value'}
        def token(c):return 'header.' + base64.urlsafe_b64encode(json.dumps(c).encode()).decode().rstrip('=') + '.signature'
        result = publish.common.inspect_claims(token(claims), self.env, 100, expected_workflow=publish.WORKFLOW)
        self.assertNotIn('private-value', json.dumps(result))
        with self.assertRaises(ValueError): publish.common.inspect_claims(token(claims), self.env, 100)
        with self.assertRaises(ValueError):
            publish.common.inspect_claims(token({**claims, 'workflow_ref': publish.common.WORKFLOW}), self.env, 100,
                expected_workflow=publish.WORKFLOW)

    def test_repository_verification_rejects_all_destination_setting_changes(self):
        self.assertEqual(publish.verify_repository(self.env, {'repositories': [self.repository]}), {'repository_verified': True})
        for key in self.repository:
            row = copy.deepcopy(self.repository)
            row[key] = {} if isinstance(row[key], dict) else 'unreviewed'
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish.verify_repository(self.env, {'repositories': [row]})
        for rows in ([], [self.repository, self.repository]):
            with self.assertRaises(ValueError): publish.verify_repository(self.env, {'repositories': rows})

    def test_cli_errors_do_not_reveal_token_values(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['collection_publish.py', 'inspect']), \
                patch.object(publish.common, 'fetch_token', side_effect=ValueError('secret-token')), redirect_stdout(output):
            self.assertEqual(publish.main(), 1)
        self.assertNotIn('secret-token', output.getvalue())

    def test_workflow_guards_and_build_precede_credentials_and_login(self):
        workflow = yaml.load((ROOT / '.github/workflows/publish-collection-image.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['default'], 'inspect')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        job, = workflow['jobs'].values()
        self.assertEqual(job['permissions'], {'contents': 'read', 'id-token': 'write'})
        steps = job['steps']
        build = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('docker/build-push-action@'))
        guards = next(i for i, s in enumerate(steps) if s.get('name', '').startswith('Verify installed protocols'))
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
        for value in ('rest mqtt opcua-json opcua-binary', '--network none', '--read-only', '--cap-drop ALL',
                '--security-opt no-new-privileges', '1.5.7+dfsg-1+deb13u1'):
            self.assertIn(value, steps[guards]['run'])
        self.assertNotIn('secrets.', json.dumps(workflow))
        checkout = next(s for s in steps if s.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with'], {'ref': '${{ github.sha }}', 'persist-credentials': 'false'})


if __name__ == '__main__':
    unittest.main()
