import copy
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from foodtech_business_api.source_grant_contract import SourceGrantConflict
from foodtech_business_api.source_grant_inventory import SourceGrantInventory, HEAD, _pack
from foodtech_business_api.source_grant_archive_purge import SourceGrantArchivePurge
from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.deletion_progress import DeletionProgressStore
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateRawAesKeyringInput, AesWrappingAlg


class ArchivePurgeTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request, self.operation = str(uuid4()), str(uuid4()), str(uuid4())
        self.client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.serializer = TypeSerializer()
        self.store = SimpleNamespace(auth_table='foodtech-auth-sessions-dev',
            registry_table='foodtech-deletion-registry-dev', progress_table='foodtech-deletion-progress-dev')
        self.grants = {'JEYUK#jeyuk-' + str(i): str(uuid4()) for i in range(101)}
        self.head = SourceGrantInventory._records(self.grants)[-1][1]
        def open_record(**scope):
            self.assertEqual(scope, {'subject': self.subject, 'request_id': self.request, 'domain': HEAD})
            return {}, self.head, b'opaque-encrypted-head'
        self.inventory = SimpleNamespace(store=self.store, _scope=SourceGrantInventory._scope,
                                         _open_record=open_record)
        self.purge = SourceGrantArchivePurge(inventory=self.inventory, client=self.client, clock=lambda: 1000)
        self.scope = dict(subject=self.subject, request_id=self.request, purge_id=self.operation, expected_version=4)
        self.writes = []
        self.client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
                                        lambda params, **_: self.writes.append(copy.deepcopy(params)))

    def plan(self, stub, *, changed=None):
        row = {'pk': 'REGISTRY', 'sk': 'REQ#' + self.request, 'request_id': self.request,
            'status': 'COMPLETED', 'version': 4, 'source_archive_purge_id': self.operation,
            'source_archive_pages': 3, 'source_archive_purge_state': 'IN_PROGRESS', **(changed or {})}
        stub.add_response('get_item', {'Item': {k: self.serializer.serialize(v) for k, v in row.items()}})

    def test_begin_atomically_requires_absent_account_expired_complete_registry_and_no_hold(self):
        with Stubber(self.client) as stub:
            stub.add_response('transact_write_items', {})
            result = self.purge.begin(**self.scope)
        self.assertEqual(result, {'state': 'IN_PROGRESS', 'pages': 3, 'expected_version': 5, 'deletion_complete': False})
        auth, registry = self.writes[0]['TransactItems']
        self.assertEqual(auth['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')
        expression = registry['Update']['ConditionExpression']
        for text in ['#status = :completed', '#version = :version', 'attribute_type(reconcile_until, :number)',
                     'attribute_type(expires_at, :number)', 'reconcile_until <= :now', 'expires_at <= :now',
                     'reconciliation_hold_until <= :now', 'attribute_not_exists(source_archive_purge_id)']:
            self.assertIn(text, expression)
        self.assertNotIn('reconcile_until', registry['Update']['UpdateExpression'])
        self.assertNotIn('expires_at', registry['Update']['UpdateExpression'])

    def test_eligibility_race_blocks_begin_without_unconditional_fallback(self):
        with Stubber(self.client) as stub:
            stub.add_client_error('transact_write_items', service_error_code='TransactionCanceledException')
            with self.assertRaises(SourceGrantConflict):
                self.purge.begin(**self.scope)
            stub.assert_no_pending_responses()
        self.assertEqual(len(self.writes), 1)

    def test_bounded_page_deletes_keep_manifest_until_atomic_finish(self):
        with Stubber(self.client) as stub:
            self.plan(stub)
            stub.add_response('transact_write_items', {})
            stub.add_response('transact_write_items', {})
            result = self.purge.delete_page(**self.scope, start=1, limit=2)
            stub.assert_no_pending_responses()
        self.assertEqual(result['next_index'], 3)
        self.assertFalse(result['deletion_complete'])
        for index, write in enumerate(self.writes, 1):
            auth, registry, delete = write['TransactItems']
            self.assertEqual(delete['Delete']['TableName'], self.store.progress_table)
            self.assertEqual(delete['Delete']['Key']['sk'], {'S': f'DOMAIN#SOURCE_GRANT_PAGE_{index:04d}'})
            self.assertIn('source_archive_purge_id = :purge', registry['ConditionCheck']['ConditionExpression'])
            self.assertIn('expires_at <= :now', registry['ConditionCheck']['ConditionExpression'])
            self.assertTrue(all(next(iter(op.values()))['ReturnValuesOnConditionCheckFailure'] == 'NONE'
                                for op in write['TransactItems']))

    def test_finish_checks_every_page_absent_and_removes_head_with_source_receipt(self):
        with Stubber(self.client) as stub:
            self.plan(stub)
            stub.add_response('transact_write_items', {})
            result = self.purge.finish(**self.scope)
        self.assertEqual(result, {'state': 'PURGED', 'expected_version': 5,
            'planned_primary_archive_purged': True, 'deletion_complete': False})
        operations = self.writes[0]['TransactItems']
        self.assertEqual(len(operations), 6)
        for index, op in enumerate(operations[1:4]):
            self.assertEqual(op['ConditionCheck']['ConditionExpression'], 'attribute_not_exists(pk)')
            self.assertEqual(op['ConditionCheck']['Key']['sk'], {'S': f'DOMAIN#SOURCE_GRANT_PAGE_{index:04d}'})
        self.assertEqual(operations[-2]['Delete']['Key']['sk'], {'S': 'DOMAIN#' + HEAD})
        self.assertEqual(operations[-1]['Update']['ExpressionAttributeValues'][':purged'], {'S': 'PURGED'})
        self.assertNotIn('#status =', operations[-1]['Update']['UpdateExpression'])

    def test_residual_page_or_new_hold_cancels_atomic_finish(self):
        with Stubber(self.client) as stub:
            self.plan(stub)
            stub.add_client_error('transact_write_items', service_error_code='TransactionCanceledException')
            with self.assertRaises(SourceGrantConflict):
                self.purge.finish(**self.scope)
            stub.assert_no_pending_responses()

    def test_wrong_plan_or_bad_manifest_never_deletes(self):
        with Stubber(self.client) as stub:
            self.plan(stub, changed={'source_archive_purge_id': str(uuid4())})
            with self.assertRaises(SourceGrantConflict):
                self.purge.delete_page(**self.scope)
        self.head = _pack({**json.loads(self.head), 'pages': 40})
        with Stubber(self.client):
            with self.assertRaises(SourceGrantConflict):
                self.purge.begin(**self.scope)
        self.assertEqual(self.writes, [])

    def test_exact_receipt_recovery_is_read_only_and_not_fresh_residual_proof(self):
        with Stubber(self.client) as stub:
            self.plan(stub, changed={'source_archive_purge_state': 'PURGED'})
            result = self.purge.recover(**self.scope, state='PURGED')
        self.assertTrue(result['planned_primary_archive_purged'])
        self.assertFalse(result['current_residual_verified'])
        self.assertFalse(result['deletion_complete'])
        self.assertEqual(self.writes, [])


    def test_retired_subject_stays_inside_encrypted_head_and_cleanup_callback(self):
        providers = AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
        keyring = providers.create_raw_aes_keyring(input=CreateRawAesKeyringInput(
            key_namespace='synthetic-test', key_name='expired-source-archive', wrapping_key=b'x' * 32,
            wrapping_alg=AesWrappingAlg.ALG_AES256_GCM_IV12_TAG16))
        cipher = DeletionCheckpointCipher('arn:aws:kms:ap-southeast-2:123456789012:key/abcd', 'dev',
            source_table_arn='arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-dev',
            kms_client=object(), providers=SimpleNamespace(create_aws_kms_keyring=lambda **_: keyring))
        ciphertext = cipher.seal(subject=self.subject, request_id=self.request, domain=HEAD,
                                version=1, state='VERIFYING', checkpoint=self.head)
        self.assertNotIn(self.subject.encode(), ciphertext)
        store = DeletionProgressStore(auth_table=self.store.auth_table, registry_table=self.store.registry_table,
                                     progress_table=self.store.progress_table, client=self.client)
        deletion = SimpleNamespace(registrar=SimpleNamespace(auth_table=store.auth_table,
            catalog_table='foodtech-source-access-dev'), registry_table=store.registry_table)
        inventory = SourceGrantInventory(deletion=deletion, progress_store=store, cipher=cipher)
        purge = SourceGrantArchivePurge(inventory=inventory, client=self.client, clock=lambda: 1000)
        def head(stub):
            item = {'pk': 'REQ#' + self.request, 'sk': 'DOMAIN#' + HEAD, 'version': 1,
                    'state': 'VERIFYING', 'encrypted_checkpoint': ciphertext}
            stub.add_response('get_item', {'Item': {k: self.serializer.serialize(v) for k, v in item.items()}})
        scope = {key: value for key, value in self.scope.items() if key != 'subject'}
        with Stubber(self.client) as stub:
            head(stub)
            stub.add_response('transact_write_items', {})
            started = purge.begin_retired(**scope)
            owned = {**scope, 'expected_version': started['expected_version']}
            head(stub)
            self.plan(stub, changed={'version': owned['expected_version']})
            for _ in range(3):
                stub.add_response('transact_write_items', {})
            progress = purge.delete_retired_page(**owned)
            head(stub)
            self.plan(stub, changed={'version': owned['expected_version']})
            stub.add_response('transact_write_items', {})
            finished = purge.finish_retired(**owned)
            self.plan(stub, changed={'version': finished['expected_version'], 'source_archive_purge_state': 'PURGED'})
            receipt = purge.recover_retired_receipt(**{**scope, 'expected_version': finished['expected_version']})
            stub.assert_no_pending_responses()
        self.assertEqual(progress['delete_acknowledged'], 3)
        self.assertTrue(finished['planned_primary_archive_purged'])
        self.assertFalse(receipt['current_residual_verified'])
        self.assertNotIn(self.subject, json.dumps([started, progress, finished, receipt]))
        for transaction in self.writes:
            auth = transaction['TransactItems'][0]['ConditionCheck']
            self.assertEqual(auth['Key'], {'pk': {'S': 'USER#' + self.subject}})
            self.assertEqual(auth['ConditionExpression'], 'attribute_not_exists(pk)')
        held = []
        self.assertIsNone(cipher.use_source_inventory_head(request_id=self.request, ciphertext=ciphertext,
            action=lambda subject, checkpoint: held.append(checkpoint) or {'must_not_export': subject}))
        self.assertEqual(held[0], bytearray(len(self.head)))
        callback = Mock()
        with self.assertRaises(ValueError) as error:
            cipher.use_source_inventory_head(request_id=str(uuid4()), ciphertext=ciphertext, action=callback)
        callback.assert_not_called()
        self.assertNotIn(self.subject, str(error.exception))

    def test_maximum_plan_checks_40_pages_inside_one_finish_transaction(self):
        with Stubber(self.client) as stub:
            self.plan(stub, changed={'source_archive_pages': 40})
            stub.add_response('transact_write_items', {})
            result = self.purge.finish(**self.scope)
        self.assertEqual(len(self.writes[0]['TransactItems']), 43)
        self.assertTrue(result['planned_primary_archive_purged'])
        self.assertFalse(result['deletion_complete'])


if __name__ == '__main__':
    unittest.main()
