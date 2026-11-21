import unittest
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.deletion_progress import DeletionProgressConflict, DeletionProgressStore


class Client:
    def __init__(self):
        self.calls = []
        self.error = None
        self.responses = None

    def transact_get_items(self, **kwargs):
        self.calls.append(kwargs)
        return {'Responses': self.responses}

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.store = DeletionProgressStore(auth_table='auth', registry_table='registry', progress_table='progress', client=self.client)
        self.args = dict(subject=str(uuid4()), request_id=str(uuid4()), domain='COOKING', expected_version=0,
                         state='IN_PROGRESS', encrypted_checkpoint=b'ciphertext')

    def test_first_and_following_writes_guard_lock_registry_and_version(self):
        self.assertEqual(self.store.save(**self.args), 1)
        ops = self.client.calls[-1]['TransactItems']
        self.assertEqual([next(iter(op)) for op in ops], ['ConditionCheck', 'ConditionCheck', 'Update'])
        self.assertIn('deletion_lock_request', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertEqual(ops[2]['Update']['ConditionExpression'], 'attribute_not_exists(pk)')
        self.assertEqual(self.store.save(**{**self.args, 'expected_version': 1, 'state': 'VERIFYING'}), 2)
        update = self.client.calls[-1]['TransactItems'][2]['Update']
        self.assertEqual(update['ConditionExpression'], '#version = :expected')
        self.assertEqual(update['ExpressionAttributeValues'][':expected'], {'N': '1'})

    def test_save_checkpoint_seals_the_next_version_before_guarded_write(self):
        class Cipher:
            def seal(inner, **scope):
                self.assertEqual(scope['version'], 2)
                self.assertEqual(scope['subject'], self.args['subject'])
                self.assertEqual(scope['checkpoint'], b'private cursor')
                return b'sealed'
        self.assertEqual(self.store.save_checkpoint(cipher=Cipher(), subject=self.args['subject'], request_id=self.args['request_id'],
                domain='COOKING', expected_version=1, state='IN_PROGRESS', checkpoint=b'private cursor'), 2)
        value = self.client.calls[-1]['TransactItems'][2]['Update']['ExpressionAttributeValues'][':checkpoint']
        self.assertEqual(value, {'B': b'sealed'})

    def test_load_requires_locked_consistent_snapshot_and_decrypts_bound_version(self):
        class Cipher:
            def open(inner, **scope):
                self.assertEqual(scope['version'], 3)
                self.assertEqual(scope['state'], 'VERIFYING')
                self.assertEqual(scope['ciphertext'], b'sealed')
                return b'cursor'
        self.client.responses = [
            {'Item': {'deletion_lock_request': {'S': self.args['request_id']}, 'suspended': {'BOOL': True}}},
            {'Item': {'status': {'S': 'DELETING'}}},
            {'Item': {'version': {'N': '3'}, 'state': {'S': 'VERIFYING'}, 'encrypted_checkpoint': {'B': b'sealed'}}},
        ]
        scope = {k: self.args[k] for k in ('subject', 'request_id', 'domain')}
        self.assertEqual(self.store.load_checkpoint(cipher=Cipher(), **scope),
                         {'version': 3, 'state': 'VERIFYING', 'checkpoint': b'cursor'})
        self.assertEqual(len(self.client.calls[-1]['TransactItems']), 3)
        self.client.responses[0]['Item']['deletion_lock_request'] = {'S': str(uuid4())}
        with self.assertRaises(DeletionProgressConflict):
            self.store.load_checkpoint(cipher=Cipher(), **scope)
        self.client.responses[0]['Item']['deletion_lock_request'] = {'S': self.args['request_id']}
        self.client.responses[2] = {}
        self.assertIsNone(self.store.load_checkpoint(cipher=Cipher(), **scope))

    def test_invalid_scope_and_completion_are_rejected_without_writes(self):
        for changes in ({'state': 'COMPLETED'}, {'expected_version': True}, {'domain': 'OTHER#CUSTOMER'},
                        {'encrypted_checkpoint': b''}, {'request_id': 'invalid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save(**{**self.args, **changes})
        self.assertEqual(self.client.calls, [])

    def test_conditional_conflict_is_not_a_success(self):
        self.client.error = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(DeletionProgressConflict):
            self.store.save(**self.args)


if __name__ == '__main__':
    unittest.main()
