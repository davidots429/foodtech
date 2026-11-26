import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber
from aws_cryptographic_material_providers.mpl import AwsCryptographicMaterialProviders
from aws_cryptographic_material_providers.mpl.config import MaterialProvidersConfig
from aws_cryptographic_material_providers.mpl.models import CreateRawAesKeyringInput, AesWrappingAlg

from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.deletion_progress import DeletionProgressStore
from foodtech_business_api.source_grant_contract import SourceGrantConflict
from foodtech_business_api.source_grant_inventory import SourceGrantInventory

ARN = 'arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-source-access-dev'


class MemoryProgress:
    auth_table = 'foodtech-auth-sessions-dev'
    registry_table = 'foodtech-deletion-registry-dev'
    progress_table = 'foodtech-deletion-progress-dev'

    def __init__(self):
        self.items = {}

    def load_checkpoint(self, **scope):
        return self.items.get(scope['domain'])

    def save_checkpoint(self, **scope):
        assert scope['expected_version'] == 0 and scope['domain'] not in self.items
        self.items[scope['domain']] = {'version': 1, 'state': scope['state'], 'checkpoint': scope['checkpoint']}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.grants = {'JEYUK#jeyuk-' + str(i): str(uuid4()) for i in range(101)}
        self.store = MemoryProgress()
        self.deletion = SimpleNamespace(registrar=SimpleNamespace(auth_table=self.store.auth_table,
            catalog_table='foodtech-source-access-dev'), registry_table=self.store.registry_table,
            _locked=lambda **_: ('key', 'registry', {}, {}, copy.deepcopy(self.grants)))
        self.cipher = SimpleNamespace(source_table_arn=ARN)
        self.inventory = SourceGrantInventory(deletion=self.deletion, progress_store=self.store, cipher=self.cipher)
        self.scope = dict(subject=self.subject, request_id=self.request, expected_epoch=2)

    def test_bounded_capture_resumes_pages_before_publishing_head(self):
        self.assertEqual(self.inventory.capture(**self.scope, max_writes=2),
            {'state': 'IN_PROGRESS', 'writes': 2, 'deletion_complete': False})
        self.assertNotIn('SOURCE_GRANT_INVENTORY', self.store.items)
        self.assertEqual(self.inventory.capture(**self.scope, max_writes=2),
            {'state': 'CAPTURED', 'writes': 2, 'deletion_complete': False})
        self.assertEqual(self.inventory.capture(**self.scope)['writes'], 0)
        refs = []
        for i in range(3):
            page = json.loads(self.store.items[f'SOURCE_GRANT_PAGE_{i:04d}']['checkpoint'])
            refs.extend(page['references'])
        self.assertEqual(dict(refs), self.grants)
        self.assertEqual(len(self.grants), 101)

    def test_changed_mapping_or_page_cannot_reuse_existing_capture(self):
        self.inventory.capture(**self.scope, max_writes=1)
        self.grants['JEYUK#jeyuk-0'] = str(uuid4())
        with self.assertRaises(SourceGrantConflict):
            self.inventory.capture(**self.scope)
        self.assertNotIn('SOURCE_GRANT_INVENTORY', self.store.items)

    def test_lock_change_never_publishes_inventory(self):
        self.deletion._locked = Mock(side_effect=[('one', {}, {}, {}, self.grants),
                                                 ('changed', {}, {}, {}, self.grants)])
        with self.assertRaises(SourceGrantConflict):
            self.inventory.capture(**self.scope)
        self.assertEqual(self.store.items, {})

    def test_missing_or_changed_encrypted_row_blocks_final_removal_checks(self):
        client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        store = DeletionProgressStore(auth_table=self.store.auth_table, registry_table=self.store.registry_table,
                                     progress_table=self.store.progress_table, client=client)
        records = self.inventory._records(self.grants)
        payloads = {domain.encode(): raw for domain, raw in records}
        cipher = SimpleNamespace(source_table_arn=ARN, open=lambda **kw: payloads[kw['ciphertext']])
        inventory = SourceGrantInventory(deletion=self.deletion, progress_store=store, cipher=cipher)
        serializer = TypeSerializer()
        encoded = lambda row: {key: serializer.serialize(value) for key, value in row.items()}
        with Stubber(client) as stub:
            for domain, _ in records:
                stub.add_response('get_item', {'Item': encoded({'pk': 'REQ#' + self.request,
                    'sk': 'DOMAIN#' + domain, 'version': 1, 'state': 'VERIFYING',
                    'encrypted_checkpoint': domain.encode()})})
            checks = inventory.retirement_conditions(subject=self.subject, request_id=self.request, grants=self.grants)
            self.assertEqual(len(checks), 4)
            self.assertTrue(all(c['ConditionCheck']['ExpressionAttributeValues'][':ciphertext'] in payloads for c in checks))
            self.assertTrue(all(c['ConditionCheck']['ReturnValuesOnConditionCheckFailure'] == 'NONE' for c in checks))
            stub.assert_no_pending_responses()
        with Stubber(client) as stub:
            stub.add_response('get_item', {})
            with self.assertRaises(SourceGrantConflict):
                inventory.retirement_conditions(subject=self.subject, request_id=self.request, grants=self.grants)
        with Stubber(client) as stub:
            domain, _ = records[0]
            stub.add_response('get_item', {'Item': encoded({'pk': 'REQ#' + self.request,
                'sk': 'DOMAIN#' + domain, 'version': 2, 'state': 'VERIFYING', 'encrypted_checkpoint': domain.encode()})})
            with self.assertRaises(SourceGrantConflict):
                inventory.retirement_conditions(subject=self.subject, request_id=self.request, grants=self.grants)

    def test_real_encryption_sdk_checkpoint_is_guarded_ciphertext_and_cross_subject_fails(self):
        # Local raw AES wrapping key replaces KMS transport only in this test.
        # The actual Encryption SDK serialization and commitment policy execute.
        providers = AwsCryptographicMaterialProviders(config=MaterialProvidersConfig())
        keyring = providers.create_raw_aes_keyring(input=CreateRawAesKeyringInput(
            key_namespace='synthetic-test', key_name='source-inventory', wrapping_key=b'k' * 32,
            wrapping_alg=AesWrappingAlg.ALG_AES256_GCM_IV12_TAG16))
        cipher = DeletionCheckpointCipher('arn:aws:kms:ap-southeast-2:123456789012:key/abcd', 'dev',
            source_table_arn=ARN, kms_client=object(),
            providers=SimpleNamespace(create_aws_kms_keyring=lambda **_: keyring))
        domain, raw = self.inventory._records(self.grants)[0]
        client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        store = DeletionProgressStore(auth_table=self.store.auth_table, registry_table=self.store.registry_table,
                                     progress_table=self.store.progress_table, client=client)
        writes = []
        client.meta.events.register('before-parameter-build.dynamodb.TransactWriteItems',
                                    lambda params, **_: writes.append(copy.deepcopy(params)))
        with Stubber(client) as stub:
            stub.add_response('transact_write_items', {}, {'TransactItems': ANY})
            store.save_checkpoint(cipher=cipher, subject=self.subject, request_id=self.request, domain=domain,
                                  expected_version=0, state='VERIFYING', checkpoint=raw)
            stub.assert_no_pending_responses()
        auth, registry, progress = writes[0]['TransactItems']
        self.assertIn('deletion_lock_request', auth['ConditionCheck']['ConditionExpression'])
        self.assertEqual(registry['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        values = progress['Update']['ExpressionAttributeValues']
        ciphertext = values[':checkpoint']['B']
        self.assertNotIn(raw, ciphertext)
        self.assertNotIn(next(iter(self.grants.values())).encode(), ciphertext)
        scope = dict(subject=self.subject, request_id=self.request, domain=domain, version=1, state='VERIFYING')
        self.assertEqual(cipher.open(**scope, ciphertext=ciphertext), raw)
        for change in [{'subject': str(uuid4())}, {'request_id': str(uuid4())}, {'domain': 'ANOTHER'}, {'version': 2}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                cipher.open(**{**scope, **change}, ciphertext=ciphertext)

    def _captured_recovery(self):
        self.inventory.capture(**self.scope, max_writes=10)
        serializer = TypeSerializer()
        payloads = {domain.encode(): row['checkpoint'] for domain, row in self.store.items.items()}
        def get_item(**request):
            # Recovery must not need a live Auth account or catalog row.
            self.assertEqual(request['TableName'], self.store.progress_table)
            self.assertTrue(request['ConsistentRead'])
            domain = request['Key']['sk']['S'].removeprefix('DOMAIN#')
            if domain.encode() not in payloads:
                return {}
            return {'Item': {k: serializer.serialize(v) for k, v in {
                'pk': 'REQ#' + self.request, 'sk': 'DOMAIN#' + domain, 'version': 1,
                'state': 'VERIFYING', 'encrypted_checkpoint': domain.encode()}.items()}}
        store = DeletionProgressStore(auth_table=self.store.auth_table, registry_table=self.store.registry_table,
            progress_table=self.store.progress_table, client=SimpleNamespace(get_item=get_item))
        cipher = SimpleNamespace(source_table_arn=ARN, open=lambda **kw: payloads[kw['ciphertext']])
        return SourceGrantInventory(deletion=self.deletion, progress_store=store, cipher=cipher), payloads

    def test_saved_references_can_be_used_without_live_account_and_are_cleared(self):
        inventory, _ = self._captured_recovery()
        seen, held = [], []
        def use(references):
            seen.append(dict(references))
            held.append(references)
            return 'must not leave callback'
        result = inventory.use_references(subject=self.subject, request_id=self.request, action=use)
        self.assertEqual(seen, [self.grants])
        self.assertEqual(held, [{}])
        self.assertEqual(result, {'references_used': 101, 'deletion_complete': False})

    def test_missing_page_or_changed_manifest_never_invokes_restore_action(self):
        for mode in ['missing', 'digest', 'duplicate']:
            with self.subTest(mode=mode):
                inventory, payloads = self._captured_recovery()
                if mode == 'missing':
                    del payloads[b'SOURCE_GRANT_PAGE_0001']
                elif mode == 'digest':
                    head = json.loads(payloads[b'SOURCE_GRANT_INVENTORY'])
                    head['reference_sha256'] = '0' * 64
                    payloads[b'SOURCE_GRANT_INVENTORY'] = json.dumps(head).encode()
                else:
                    page = json.loads(payloads[b'SOURCE_GRANT_PAGE_0001'])
                    first = json.loads(payloads[b'SOURCE_GRANT_PAGE_0000'])
                    page['references'][0] = first['references'][0]
                    payloads[b'SOURCE_GRANT_PAGE_0001'] = json.dumps(page).encode()
                action = Mock()
                with self.assertRaises(SourceGrantConflict):
                    inventory.use_references(subject=self.subject, request_id=self.request, action=action)
                action.assert_not_called()

    def test_recovery_failure_clears_callback_buffer_and_returns_no_receipt(self):
        inventory, _ = self._captured_recovery()
        held = []
        def failed(references):
            held.append(references)
            raise ValueError('synthetic failure')
        with self.assertRaises(SourceGrantConflict):
            inventory.use_references(subject=self.subject, request_id=self.request, action=failed)
        self.assertEqual(held, [{}])

    def test_nonbinary_ciphertext_is_rejected_before_decode(self):
        inventory, _ = self._captured_recovery()
        serializer = TypeSerializer()
        fake = {'pk': 'REQ#' + self.request, 'sk': 'DOMAIN#SOURCE_GRANT_INVENTORY',
                'version': 1, 'state': 'VERIFYING', 'encrypted_checkpoint': 2 ** 50}
        inventory.store.client.get_item = lambda **_: {'Item': {k: serializer.serialize(v) for k, v in fake.items()}}
        action = Mock()
        with self.assertRaises(SourceGrantConflict):
            inventory.use_references(subject=self.subject, request_id=self.request, action=action)
        action.assert_not_called()

    def test_maximum_supported_scope_fits_final_transaction(self):
        grants = {'JEYUK#jeyuk-' + str(i): str(uuid4()) for i in range(2000)}
        self.assertEqual(len(self.inventory._records(grants)) + 3, 44)
        grants['JEYUK#jeyuk-extra'] = str(uuid4())
        with self.assertRaises(SourceGrantConflict):
            self.inventory._records(grants)


if __name__ == '__main__':
    unittest.main()
