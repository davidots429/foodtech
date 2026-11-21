import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from boto3.dynamodb.types import TypeDeserializer
from foodtech_ai_gateway.inventory_checkpoint_cipher import InventoryCheckpointCipher
from foodtech_ai_gateway.inventory_scan_progress import InventoryScanProgressStore
from foodtech_ai_gateway.legacy_tenant_inventory import InventoryPage


class Cipher:
    def __init__(self): self.values = {}
    def seal(self, job, subject, checkpoint, **context):
        token = str(len(self.values)).encode()
        self.values[token] = (job, subject, context.copy(), checkpoint.copy())
        return token
    def open(self, job, subject, ciphertext, **context):
        saved_job, saved_subject, saved_context, checkpoint = self.values[ciphertext]
        if (job, subject, context) != (saved_job, saved_subject, saved_context): raise ValueError('scope mismatch')
        return checkpoint.copy()


class Table:
    name = 'progress'
    def __init__(self): self.item = None
    def get_item(self, **_kwargs): return {'Item': self.item.copy()}


class Client:
    def __init__(self, table):
        self.table, self.decoder, self.fail = table, TypeDeserializer(), False
    def transact_write_items(self, *, TransactItems):
        if self.fail: raise RuntimeError('conflict')
        op = TransactItems[-1]
        if 'Put' in op:
            self.table.item = {key: self.decoder.deserialize(value) for key, value in op['Put']['Item'].items()}
        else:
            values = {key: self.decoder.deserialize(value) for key, value in op['Update']['ExpressionAttributeValues'].items()}
            if self.table.item['version'] != values[':expected'] or self.table.item['phase'] != values[':old']:
                raise RuntimeError('conflict')
            self.table.item.update(phase=values[':next'], version=values[':next_version'], ciphertext=values[':ciphertext'])


class InventoryProgressTests(unittest.TestCase):
    def setUp(self):
        self.table, self.cipher = Table(), Cipher()
        self.client = Client(self.table)
        self.store = InventoryScanProgressStore(self.table, self.client, self.cipher, auth_table='auth')
        self.job, self.request = str(uuid4()), str(uuid4())
        self.arn = 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/registry'
        self.source = SimpleNamespace(name='registry', table_arn=self.arn)
        self.store.start(self.job, 'owner', self.request, self.arn)

    def test_reconciliation_failure_preserves_progress_and_binding(self):
        kwargs = dict(job_id=self.job, subject='owner', request_id=self.request, source_table=self.source, limit=2)
        with patch('foodtech_ai_gateway.inventory_scan_progress.reconcile_reserved_page', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError): self.store.run_page(**kwargs)
        self.assertEqual(self.table.item['version'], 1)
        with patch('foodtech_ai_gateway.inventory_scan_progress.reconcile_reserved_page',
                   side_effect=[InventoryPage((), {'pk': 'private', 'sk': 'META'}, 2), InventoryPage((), None, 0)]):
            result = self.store.run_page(**kwargs)
            self.assertEqual((result.phase, result.version), ('CONTINUE', 2))
            with self.assertRaises(ValueError): self.store.read_progress(self.job, 'other', self.request, self.arn)
            with self.assertRaises(ValueError): self.store.read_progress(self.job, 'owner', str(uuid4()), self.arn)
            result = self.store.run_page(**kwargs)
            self.assertEqual((result.phase, result.version), ('SCAN_COMPLETE', 3))

    def test_progress_write_failure_does_not_advance(self):
        self.client.fail = True
        with patch('foodtech_ai_gateway.inventory_scan_progress.reconcile_reserved_page', return_value=InventoryPage((), None, 0)):
            with self.assertRaises(RuntimeError):
                self.store.run_page(job_id=self.job, subject='owner', request_id=self.request,
                                    source_table=self.source, limit=1)
        self.assertEqual(self.table.item['version'], 1)

    def test_cipher_context_binds_request_and_source(self):
        cipher = object.__new__(InventoryCheckpointCipher); cipher.environment = 'dev'
        original = cipher._context(self.job, self.request, 'INITIAL', 1, self.arn)
        self.assertEqual(original['purpose'], 'AI_INVENTORY_CHECKPOINT')
        self.assertNotEqual(original, cipher._context(self.job, str(uuid4()), 'INITIAL', 1, self.arn))
        self.assertNotEqual(original, cipher._context(self.job, self.request, 'INITIAL', 2, self.arn))


if __name__ == '__main__': unittest.main()
