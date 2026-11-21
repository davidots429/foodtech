import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.meal_meta_deletion import MealMetaDeletion
from test_profile_deletion_inventory import Progress


class MealMetaDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.source = Mock(); self.source.name = 'cooking'; self.source.table_arn = 'arn:source'
        self.progress = Progress(); self.progress.client = Mock()
        self.cipher = SimpleNamespace(source_table_arn='arn:source')
        self.inventory = SimpleNamespace(source_table=self.source, progress_store=self.progress, cipher=self.cipher,
            targets=lambda **kw: iter([SimpleNamespace(meal_id='meal')]), _json=json.loads)
        self.residual = SimpleNamespace(DOMAIN='MEAL_RESIDUAL', source_table=self.source, progress_store=self.progress, cipher=self.cipher,
            _checkpoint=Mock(return_value=({'complete': True, 'residual': False}, 1)))
        self.worker = MealMetaDeletion(inventory=self.inventory, residual=self.residual)
        self.scope = {'subject': self.subject, 'request_id': self.request, 'index': 0}
        self.meta = {'pk': f'SCOPE#USER#{self.subject}#COMBI_MEAL#meal', 'sk': 'META',
                     'meal_batch_id': 'meal', 'version': Decimal(3), 'source': {'device_id': 'synthetic'}}

    def test_snapshot_precedes_guarded_delete_and_retry(self):
        self.source.query.side_effect = [{'Items': [self.meta]}, {'Items': []}]
        self.assertEqual(self.worker.remove_one(**self.scope), {'index': 0, 'state': 'VERIFYING'})
        saved = self.progress.items[(self.subject, self.request, 'MEAL_META_000000000000')]
        self.assertIn('synthetic', saved['checkpoint'].decode())
        self.assertEqual(self.worker.remove_one(**self.scope), {'index': 0, 'state': 'VERIFYING'})
        delete = self.progress.client.transact_write_items.call_args.kwargs['TransactItems'][2]['Delete']
        self.assertEqual(delete['ExpressionAttributeValues'][':version'], {'N': '3'})
        self.assertEqual(delete['ExpressionAttributeValues'][':id'], {'S': 'meal'})

    def test_residual_or_another_record_blocks(self):
        self.residual._checkpoint.return_value = ({'complete': False, 'residual': True}, 1)
        with self.assertRaises(ValueError):
            self.worker.remove_one(**self.scope)
        self.residual._checkpoint.return_value = ({'complete': True, 'residual': False}, 1)
        self.source.query.return_value = {'Items': [{'pk': self.meta['pk'], 'sk': 'EVENT#1'}]}
        with self.assertRaises(ValueError):
            self.worker.remove_one(**self.scope)
        self.assertFalse(self.progress.items)
        self.progress.client.transact_write_items.assert_not_called()

    def test_conflict_retains_snapshot_and_missing_snapshot_fails(self):
        self.source.query.return_value = {'Items': [self.meta]}
        self.progress.client.transact_write_items.side_effect = ClientError({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):
            self.worker.remove_one(**self.scope)
        self.assertIn((self.subject, self.request, 'MEAL_META_000000000000'), self.progress.items)


if __name__ == '__main__':
    unittest.main()
