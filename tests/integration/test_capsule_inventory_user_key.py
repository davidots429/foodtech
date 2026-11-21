"""Exercise the restricted inventory-to-crypto contract without AWS resources."""
import base64
import unittest

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.capsule_inventory import CapsuleInventory
from foodtech_capsule_crypto.contract import Entry, Envelope
from foodtech_capsule_crypto.deletion_plan import EncryptedTarget, prepare_targets
from foodtech_capsule_crypto.user_entry import UserEntryEnvelope, open_entry, seal_entry
from foodtech_capsule_crypto.user_key import UserKeyGenerator, UserKeyRecord, UserKeyUnwrapper


def digest(byte):
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip('=')


class KMS:
    def generate_data_key(self, *, KeyId, KeySpec, EncryptionContext):
        self.context = EncryptionContext
        return {'KeyId': KeyId, 'Plaintext': b'x' * 32, 'CiphertextBlob': b'wrapped'}

    def decrypt(self, *, KeyId, CiphertextBlob, EncryptionContext, EncryptionAlgorithm):
        if CiphertextBlob != b'wrapped' or EncryptionContext != self.context:
            raise ValueError('wrong context')
        return {'KeyId': KeyId, 'Plaintext': b'x' * 32}


class Client:
    def __init__(self, meta, entry):
        self.meta, self.entry = meta, entry

    def get_item(self, **kwargs):
        return {'Item': self.meta}

    def get_paginator(self, name):
        return self

    def paginate(self, **kwargs):
        yield {'Items': [self.entry]}


class InventoryCryptoTests(unittest.TestCase):
    def test_stable_inventory_supplies_wrapped_key_for_authenticated_entry(self):
        arn = 'arn:aws:kms:ap-southeast-2:123456789012:key/test-key'
        key_map = {('pool-v1', 'shard-0'): arn}
        tenant = f'psn:v1:credential:2026-11:v1:{digest(1)}'
        purposes, tenant_purposes = frozenset({'BILLING'}), frozenset({'credential'})
        plain = Entry('BILLING', '2026-11', digest(2))
        kms = KMS()
        record, sealed = UserKeyGenerator(key_map, tenant_purposes, kms_client=kms).generate(
            tenant=tenant, pool_version='pool-v1', shard_id='shard-0',
            use_key=lambda key, rec: seal_entry(key=key, record=rec, key_map=key_map, tenant=tenant,
                entry=plain, approved_purposes=purposes, approved_tenant_purposes=tenant_purposes))
        locator = 'A' * 43
        request_id = '12c5f2d4-c6d2-4165-adf7-e7fed537af06'
        serializer = TypeSerializer()
        def item(values):
            return {key: serializer.serialize(value) for key, value in values.items()}
        common = {'user_key_id': record.key_id, 'pool_version': record.pool_version,
                  'shard_id': record.shard_id, 'key_arn': record.key_arn}
        meta = item({'pk': locator, 'sk': 'META', 'revision': 1, 'state': 'DELETION_LOCKED',
                     'deletion_request_id': request_id, 'encrypted_user_dek': record.encrypted_dek, **common})
        row = item({'pk': locator, 'sk': 'ENTRY#BILLING#2026-11',
                    'ciphertext': sealed.entry.ciphertext, **common})
        staged = []
        inventory = CapsuleInventory('capsule', purposes, client=Client(meta, row)).stage(
            locator, request_id, lambda sk, ciphertext, metadata: staged.append((sk, ciphertext, metadata)))
        wrapped = inventory.wrapped_user_key
        recovered_record = UserKeyRecord(wrapped.user_key_id, wrapped.pool_version, wrapped.shard_id,
                                         wrapped.key_arn, wrapped.encrypted_dek)
        ciphertext = staged[0][1]
        envelope = UserEntryEnvelope(wrapped.user_key_id, Envelope(wrapped.pool_version, wrapped.shard_id,
                                                                   wrapped.key_arn, ciphertext))
        result = UserKeyUnwrapper(key_map, tenant_purposes, kms_client=kms).use(
            tenant=tenant, record=recovered_record,
            use_key=lambda key, rec: open_entry(key=key, record=rec, key_map=key_map, tenant=tenant,
                envelope=envelope, expected_purpose='BILLING', expected_epoch='2026-11',
                approved_purposes=purposes, approved_tenant_purposes=tenant_purposes))
        self.assertEqual(result, plain)
        plan = prepare_targets(tenant=tenant, record=recovered_record,
            targets=(EncryptedTarget('BILLING', '2026-11', envelope),), expected_count=inventory.entry_count,
            key_map=key_map, approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)
        self.assertEqual(plan.entries, (plain,))


if __name__ == '__main__':
    unittest.main()
