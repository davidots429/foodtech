"""Check context/claim boundaries and immutable artifact metadata without live tokens."""
import base64
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import yaml

PATH = Path(__file__).resolve().parents[2] / 'infra/scripts/maintenance_publish.py'
SPEC = importlib.util.spec_from_file_location('maintenance_publish', PATH)
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.env = {'GITHUB_REPOSITORY': publish.REPOSITORY, 'GITHUB_REPOSITORY_ID': publish.REPOSITORY_ID,
            'GITHUB_REPOSITORY_OWNER_ID': publish.OWNER_ID, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40,
            'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
            'PUBLISH_ROLE_ARN': 'arn:aws:iam::123456789012:role/foodtech-maintenance-publish-dev',
            'PUBLISH_ACCOUNT_ID': '123456789012'}
        self.claims = {'iss': 'https://token.actions.githubusercontent.com', 'aud': 'sts.amazonaws.com',
            'repository': publish.REPOSITORY, 'repository_id': publish.REPOSITORY_ID,
            'repository_owner_id': publish.OWNER_ID, 'ref': 'refs/heads/main', 'sha': 'a' * 40,
            'event_name': 'workflow_dispatch', 'workflow_ref': publish.WORKFLOW,
            'sub': 'repo:davidots429/foodtech:ref:refs/heads/main', 'nbf': 50, 'exp': 150}

    def token(self, values=None):
        payload = base64.urlsafe_b64encode(json.dumps(values or self.claims).encode()).decode().rstrip('=')
        return 'synthetic-header.' + payload + '.synthetic-signature'

    def test_exact_legacy_and_immutable_subjects_are_reported(self):
        for subject in (self.claims['sub'], f'repo:davidots429@{publish.OWNER_ID}/foodtech@{publish.REPOSITORY_ID}:ref:refs/heads/main'):
            self.claims['sub'] = subject
            result = publish.inspect_claims(self.token(), self.env, 100)
            self.assertEqual(result['sub'], subject)
            self.assertNotIn('token', result)
            self.assertNotIn('exp', result)

    def test_foreign_branch_event_repository_and_workflow_are_rejected(self):
        for key in ('iss', 'aud', 'repository', 'repository_id', 'repository_owner_id',
                    'ref', 'sha', 'event_name', 'workflow_ref', 'sub'):
            values = copy.deepcopy(self.claims)
            values[key] = 'untrusted'
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish.inspect_claims(self.token(values), self.env, 100)
        for key in ('GITHUB_REF', 'GITHUB_EVENT_NAME', 'GITHUB_REPOSITORY', 'GITHUB_REPOSITORY_ID'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                publish.context({**self.env, key: 'untrusted'})

    def test_expired_future_and_invalid_lifetimes_are_rejected(self):
        for nbf, exp in ((101, 150), (50, 100), (False, 150), (50, '150'), (50, float('inf'))):
            with self.subTest(nbf=nbf, exp=exp), self.assertRaises(ValueError):
                publish.inspect_claims(self.token({**self.claims, 'nbf': nbf, 'exp': exp}), self.env, 100)

    def test_publishing_target_rejects_wrong_account_and_role(self):
        for env in ({**self.env, 'PUBLISH_ACCOUNT_ID': '000000000000'},
                    {**self.env, 'PUBLISH_ROLE_ARN': 'arn:aws:iam::123456789012:role/admin'},
                    {**self.env, 'GITHUB_SHA': 'x\ncommand'},
                    {**self.env, 'GITHUB_RUN_ATTEMPT': '1; command'}):
            with self.assertRaises(ValueError):
                publish.target(env)

    def test_manifest_pins_digest_source_and_unique_attempt(self):
        digest = 'sha256:' + 'b' * 64
        first = publish.manifest(self.env, digest)
        second = publish.manifest({**self.env, 'GITHUB_RUN_ATTEMPT': '2'}, digest)
        self.assertNotEqual(first['image_tag'], second['image_tag'])
        self.assertEqual(first['image_uri'], '123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/maintenance-dev@' + digest)
        self.assertEqual(first['source_sha'], 'a' * 40)
        for value in ('latest', 'sha256:bad', digest + '\n'):
            with self.assertRaises(ValueError):
                publish.manifest(self.env, value)

    def test_workload_selection_cannot_inject_package_path_and_has_separate_tags(self):
        normal = publish.target(self.env)
        selected = publish.target({**self.env, 'IMAGE_WORKLOAD': 'standardization'})
        self.assertEqual(normal['build_target'], 'maintenance')
        self.assertEqual(selected['package'], 'foodtech-standardization-worker')
        self.assertEqual(selected['build_target'], 'standardization')
        self.assertNotEqual(normal['tag'], selected['tag'])
        self.assertEqual(publish.manifest({**self.env, 'IMAGE_WORKLOAD': 'standardization'}, 'sha256:' + 'b' * 64)['workload'], 'standardization')
        for value in ('', '../secret', 'business;command', 'untrusted'):
            with self.assertRaises(ValueError):
                publish.target({**self.env, 'IMAGE_WORKLOAD': value})

    def test_token_error_does_not_print_credentials_or_response(self):
        output = io.StringIO()
        with patch.object(publish, 'fetch_token', side_effect=RuntimeError('secret-token-material')), \
                patch('sys.argv', ['maintenance_publish.py', 'inspect']), redirect_stdout(output):
            self.assertEqual(publish.main(), 1)
        self.assertNotIn('secret-token-material', output.getvalue())

    def test_inspection_discards_all_unrequested_claims(self):
        claims = {**self.claims, 'synthetic_secret': 'must-not-leave-process'}
        result = publish.inspect_claims(self.token(claims), self.env, 100)
        self.assertNotIn('must-not-leave-process', json.dumps(result))

    def test_token_endpoint_redirect_is_refused(self):
        with self.assertRaises(ValueError):
            publish.NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_workflow_is_manual_inspection_first_and_aws_steps_are_publish_only(self):
        workflow = yaml.load((PATH.parents[2] / '.github/workflows/publish-maintenance-image.yml').read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['default'], 'inspect')
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        job, = workflow['jobs'].values()
        self.assertEqual(job['permissions'], {'contents': 'read', 'id-token': 'write'})
        for step in job['steps']:
            if 'aws ' in step.get('run', '') or step.get('uses', '').startswith('aws-actions/'):
                self.assertEqual(step.get('if'), "inputs.operation == 'publish'")
        steps = job['steps']
        build = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('docker/build-push-action@'))
        credentials = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('aws-actions/configure-aws-credentials@'))
        self.assertLess(build, credentials)
        guard = next(i for i, step in enumerate(steps) if step.get('name') == 'Verify disabled read probe before obtaining AWS credentials')
        self.assertLess(build, guard)
        self.assertLess(guard, credentials)
        patch_guard = next(i for i, step in enumerate(steps) if step.get('name') == 'Verify patched maintenance library before obtaining AWS credentials')
        self.assertLess(build, patch_guard)
        self.assertLess(patch_guard, credentials)
        self.assertEqual(steps[patch_guard]['if'], "inputs.operation == 'publish'")
        self.assertIn('1.5.7+dfsg-1+deb13u1', steps[patch_guard]['run'])
        self.assertEqual(steps[guard]['if'], "inputs.operation == 'publish'")
        for isolation in ('--network none', '--read-only', '--cap-drop ALL', '--security-opt no-new-privileges'):
            self.assertIn(isolation, steps[guard]['run'])
        self.assertEqual(steps[build]['with']['push'], 'false')
        checkout = next(step for step in steps if step.get('uses', '').startswith('actions/checkout@'))
        self.assertEqual(checkout['with']['ref'], '${{ github.sha }}')
        self.assertEqual(checkout['with']['persist-credentials'], 'false')
        self.assertNotIn('secrets.', json.dumps(workflow))


if __name__ == '__main__':
    unittest.main()
