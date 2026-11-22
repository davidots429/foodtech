import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from foodtech_business_api.guarded_deletion_recovery import GuardedDeletionRecovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request, self.vault, self.internal = (str(uuid4()) for _ in range(4))
        self.client = Mock()
        self.client.transact_get_items.return_value = {'Responses': [
            {'Item': {'pk': {'S': f'USER#{self.subject}'}, 'vault_id': {'S': self.vault},
                      'suspended': {'BOOL': True}, 'deletion_lock_request': {'S': self.request}}},
            {'Item': {'pk': {'S': 'REGISTRY'}, 'sk': {'S': f'REQ#{self.request}'},
                      'request_id': {'S': self.request}, 'status': {'S': 'DELETING'},
                      'encrypted_user_id': {'B': b'encrypted-user'}, 'encrypted_capsule_locator': {'B': b'encrypted-locator'}}},
        ]}
        self.store = SimpleNamespace(auth_table='auth', registry_table='registry',
                                     recover_locked_request=Mock(return_value={'status': 'DELETING'}))
        self.cipher = Mock()
        self.cipher.use.side_effect = lambda **kw: kw['callback'](bytearray(self.internal.encode()), bytearray(b'A'*43))
        self.service = GuardedDeletionRecovery(store=self.store, recovery_cipher=self.cipher, client=self.client)

    def test_reads_server_records_and_confirms_decrypted_identity(self):
        result = self.service.recover(authenticated_subject=self.subject, request_id=self.request)
        self.assertEqual(result, {'status': 'DELETING'})
        self.assertNotIn(self.internal, repr(result))
        args = self.store.recover_locked_request.call_args.kwargs
        self.assertEqual(args['vault_id'], self.vault)
        self.assertEqual(args['internal_user_id'], self.internal)
        self.assertEqual(args['capsule_locator'], 'A' * 43)
        self.assertEqual(len(self.client.transact_get_items.call_args.kwargs['TransactItems']), 2)

    def test_action_runs_only_after_locked_confirmation_and_result_does_not_escape(self):
        action = Mock(return_value={'private': self.internal})
        result = self.service.use_locked(authenticated_subject=self.subject, request_id=self.request, action=action)
        self.assertEqual(result, {'status': 'DELETING'})
        action.assert_called_once()
        self.store.recover_locked_request.side_effect = ValueError('lock changed')
        action.reset_mock()
        with self.assertRaises(ValueError):
            self.service.use_locked(authenticated_subject=self.subject, request_id=self.request, action=action)
        action.assert_not_called()

    def test_wrong_lock_or_missing_ciphertext_never_decrypts(self):
        rows = self.client.transact_get_items.return_value['Responses']
        rows[0]['Item']['deletion_lock_request'] = {'S': str(uuid4())}
        with self.assertRaises(ValueError):
            self.service.recover(authenticated_subject=self.subject, request_id=self.request)
        self.cipher.use.assert_not_called()
        rows[0]['Item']['deletion_lock_request'] = {'S': self.request}
        del rows[1]['Item']['encrypted_user_id']
        with self.assertRaises(ValueError):
            self.service.recover(authenticated_subject=self.subject, request_id=self.request)
        self.cipher.use.assert_not_called()


if __name__ == '__main__':
    unittest.main()
