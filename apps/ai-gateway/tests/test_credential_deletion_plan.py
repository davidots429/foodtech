import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_ai_gateway.credential_deletion_plan import CredentialDeletionPlan
from foodtech_ai_gateway.legacy_tenant_inventory import VaultLocation


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.source = SimpleNamespace(name='registry')
        self.vault = SimpleNamespace(name='vault', table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/vault')
        self.progress = Mock(auth_table='auth', progress_table='progress')
        self.progress.load_checkpoint.return_value = None
        self.progress.save_checkpoint.return_value = 1
        self.cipher = SimpleNamespace(source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/registry')
        self.capture = SimpleNamespace(source_table=self.source, progress_store=self.progress, cipher=self.cipher,
            partitions=lambda **kw: iter([SimpleNamespace(partition='CREDENTIAL#key')]))
        self.planner = CredentialDeletionPlan(capture=self.capture, progress_store=self.progress, cipher=self.cipher,
                                               source_table=self.source, vault_table=self.vault)
        self.scope = {'subject': str(uuid4()), 'request_id': str(uuid4()), 'index': 0}

    def test_prepare_persists_secret_target_only_inside_checkpoint(self):
        location = VaultLocation({'pk': 'CREDENTIAL#key', 'sk': 'META'}, 'private-pseudonym')
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault', return_value=location):
            result = self.planner.prepare(**self.scope)
        self.assertEqual(result, {'index': 0, 'state': 'PREPARED', 'version': 1})
        self.assertNotIn('private-pseudonym', repr(result))
        checkpoint = self.progress.save_checkpoint.call_args.kwargs['checkpoint']
        self.progress.load_checkpoint.return_value = {'state': 'IN_PROGRESS', 'version': 1, 'checkpoint': checkpoint}
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault') as resolve:
            self.assertEqual(self.planner.prepare(**self.scope), result)
            resolve.assert_not_called()
        self.vault.table_arn = self.vault.table_arn.replace('/vault', '/other')
        with self.assertRaises(ValueError):
            self.planner.prepare(**self.scope)

    def test_readiness_rechecks_metadata_and_never_authorizes_deletion(self):
        location = VaultLocation({'pk': 'CREDENTIAL#key', 'sk': 'META'}, 'private-pseudonym')
        observer = SimpleNamespace(capture=self.capture, progress_store=self.progress, source_table=self.source,
                                   observe=Mock(return_value={'observation': 'BLOCKED', 'reason': 'ACTIVE_MARKER'}))
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault', return_value=location):
            self.planner.prepare(**self.scope)
            payload = self.progress.save_checkpoint.call_args.kwargs['checkpoint']
            self.progress.load_checkpoint.return_value = {'state': 'IN_PROGRESS', 'version': 1, 'checkpoint': payload}
            result = self.planner.observe_readiness(observer=observer, **self.scope)
        self.assertEqual(result, {'index': 0, 'observation': 'BLOCKED'})
        changed = VaultLocation(location.key, 'changed-pseudonym')
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault', return_value=changed):
            with self.assertRaises(ValueError):
                self.planner.observe_readiness(observer=observer, **self.scope)

    def test_metadata_change_during_execution_observation_is_rejected(self):
        original = VaultLocation({'pk': 'CREDENTIAL#key', 'sk': 'META'}, 'first')
        changed = VaultLocation(original.key, 'changed')
        observer = SimpleNamespace(capture=self.capture, progress_store=self.progress, source_table=self.source,
                                   observe=Mock(return_value={'observation': 'CLEAR_OBSERVED'}))
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault', return_value=original):
            self.planner.prepare(**self.scope)
        payload = self.progress.save_checkpoint.call_args.kwargs['checkpoint']
        self.progress.load_checkpoint.return_value = {'state': 'IN_PROGRESS', 'version': 1, 'checkpoint': payload}
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault', side_effect=[original, changed]):
            with self.assertRaises(ValueError):
                self.planner.observe_readiness(observer=observer, **self.scope)

    def test_invalid_or_changed_metadata_cannot_create_plan(self):
        with patch('foodtech_ai_gateway.credential_deletion_plan.resolve_credential_vault', side_effect=ValueError('mismatch')):
            with self.assertRaises(ValueError):
                self.planner.prepare(**self.scope)
        self.progress.save_checkpoint.assert_not_called()
        self.progress.load_checkpoint.return_value = {'state': 'IN_PROGRESS', 'version': 1, 'checkpoint': json.dumps(
            {'schema': 1, 'partition': 'CREDENTIAL#other', 'vault_arn': self.vault.table_arn,
             'tenant_pseudonym': 'x'}).encode()}
        with self.assertRaises(ValueError):
            self.planner.prepare(**self.scope)


if __name__ == '__main__':
    unittest.main()
