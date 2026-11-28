"""Exercise the restricted inventory-to-crypto contract without AWS resources."""
import base64
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.capsule_inventory import CapsuleInventory, CapsuleInventoryError, RestoredCapsuleInventory
from foodtech_business_api.dynamodb_restore_verification import DynamoRestoreVerificationError
from foodtech_business_api.recovery_registry_inventory import RecoveryRegistryLocators, VersionedTombstone
from foodtech_capsule_crypto.contract import Entry, Envelope
from foodtech_capsule_crypto.deletion_plan import EncryptedTarget, prepare_targets, prepare_from_inventory, prepare_from_resolved_inventory
from foodtech_capsule_crypto.restored_plan import RestoredCapsulePlanWorker
from foodtech_capsule_crypto.user_entry import UserEntryEnvelope, open_entry, seal_entry
from foodtech_capsule_crypto.user_key import UserKeyGenerator, UserKeyRecord, UserKeyUnwrapper
from foodtech_contracts.recovery_reconciliation import Tombstone


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
                     'deletion_request_id': request_id, 'encrypted_user_dek': record.encrypted_dek,
                     'scope_purpose': 'credential', 'scope_epoch': '2026-11', 'scope_key_version': 'v1', **common})
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
        connected = prepare_from_inventory(inventory=CapsuleInventory('capsule', purposes, client=Client(meta, row)),
            locator=locator, request_id=request_id, tenant=tenant, key_map=key_map,
            approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)
        self.assertEqual(connected.entries, (plain,))
        internal_id = bytearray(b'12c5f2d4-c6d2-4165-adf7-e7fed537af06')
        class Resolver:
            def derive(self, *, internal_id, scope):
                self.scope = scope
                return tenant
        resolver = Resolver()
        resolved = prepare_from_resolved_inventory(
            inventory=CapsuleInventory('capsule', purposes, client=Client(meta, row)), locator=locator,
            request_id=request_id, internal_id=internal_id, tenant_resolver=resolver, key_map=key_map,
            approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)
        self.assertEqual(resolved.entries, (plain,))
        self.assertEqual((resolver.scope.purpose, resolver.scope.epoch, resolver.scope.key_version),
                         ('credential', '2026-11', 'v1'))
        class WrongResolver:
            def derive(self, *, internal_id, scope):
                return f'psn:v1:credential:2026-11:v1:{digest(3)}'
        with self.assertRaises(ValueError):
            prepare_from_resolved_inventory(inventory=CapsuleInventory('capsule', purposes, client=Client(meta, row)),
                locator=locator, request_id=request_id, internal_id=internal_id, tenant_resolver=WrongResolver(),
                key_map=key_map, approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)
        with self.assertRaises(ValueError):
            prepare_from_resolved_inventory(inventory=CapsuleInventory('capsule', purposes, client=Client(meta, row)),
                locator=locator, request_id=request_id, internal_id=bytearray(b'bad'), tenant_resolver=resolver,
                key_map=key_map, approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)

        class UnstableClient(Client):
            def __init__(self, meta, entry):
                super().__init__(meta, entry)
                self.reads = 0
            def get_item(self, **kwargs):
                self.reads += 1
                changed = dict(self.meta)
                if self.reads > 1:
                    changed['revision'] = serializer.serialize(2)
                return {'Item': changed}
        with self.assertRaises(CapsuleInventoryError):
            prepare_from_inventory(inventory=CapsuleInventory('capsule', purposes, client=UnstableClient(meta, row)),
                locator=locator, request_id=request_id, tenant=tenant, key_map=key_map,
                approved_purposes=purposes, approved_tenant_purposes=tenant_purposes, kms_client=kms)


class RestoredCapsuleTests(unittest.TestCase):
    def setUp(self):
        self.request = "12c5f2d4-c6d2-4165-adf7-e7fed537af06"
        self.restore = "ffc4a4dd-81fe-4049-8130-0e44f44320a0"
        self.locator = "A" * 43
        self.registry_arn = "arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-deletion-registry-dev"
        self.restore_at = datetime(2026, 11, 28, 10, 0, tzinfo=timezone.utc)
        self.source_capsule_arn = "arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-link-capsule-dev"
        self.capsule_arn = (
            f"arn:aws:dynamodb:ap-southeast-2:123456789012:table/foodtech-link-capsule-restore-{self.restore}"
        )
        arn = "arn:aws:kms:ap-southeast-2:123456789012:key/test-key"
        self.key_map = {("pool-v1", "shard-0"): arn}
        tenant = f"psn:v1:credential:2026-11:v1:{digest(1)}"
        self.purposes = frozenset({"BILLING"})
        self.tenant_purposes = frozenset({"credential"})
        self.entry = Entry("BILLING", "2026-11", digest(2))
        self.kms = KMS()
        record, sealed = UserKeyGenerator(self.key_map, self.tenant_purposes, kms_client=self.kms).generate(
            tenant=tenant, pool_version="pool-v1", shard_id="shard-0",
            use_key=lambda key, rec: seal_entry(
                key=key, record=rec, key_map=self.key_map, tenant=tenant, entry=self.entry,
                approved_purposes=self.purposes, approved_tenant_purposes=self.tenant_purposes,
            ),
        )
        self.resolver = Mock()
        self.resolver.derive.return_value = tenant
        serializer = TypeSerializer()

        def item(values):
            return {key: serializer.serialize(value) for key, value in values.items()}

        common = {"user_key_id": record.key_id, "pool_version": record.pool_version,
                  "shard_id": record.shard_id, "key_arn": record.key_arn}
        self.meta = item({"pk": self.locator, "sk": "META", "revision": 1, "state": "ACTIVE",
                          "encrypted_user_dek": record.encrypted_dek, "scope_purpose": "credential",
                          "scope_epoch": "2026-11", "scope_key_version": "v1", **common})
        self.row = item({"pk": self.locator, "sk": "ENTRY#BILLING#2026-11",
                         "ciphertext": sealed.entry.ciphertext, **common})
        self.expected = VersionedTombstone(Tombstone(self.request, "COMPLETED"), 3)
        self.registry_row = {"pk": "REGISTRY", "sk": f"REQ#{self.request}", "request_id": self.request,
                             "status": "COMPLETED", "version": Decimal(3)}
        self.registry = Mock()
        self.table = self.registry.Table.return_value
        self.sealed = {"Item": {**self.registry_row, "encrypted_user_id": b"sealed-user",
                                "encrypted_capsule_locator": b"sealed-locator"}}
        self.cipher = Mock()
        self.cipher.use.side_effect = lambda **kwargs: kwargs["callback"](
            bytearray(self.request.encode("ascii")), bytearray(self.locator.encode("ascii")))
        self.locators = RecoveryRegistryLocators(
            self.registry, live_registry_arn=self.registry_arn, recovery_cipher=self.cipher,
        )

    def worker(self, client, *, arn=None, actual_time=None):
        capsule_arn = arn or self.capsule_arn
        client.describe_table = Mock(return_value={"Table": {
            "TableArn": capsule_arn, "TableStatus": "ACTIVE", "RestoreSummary": {
                "SourceTableArn": self.source_capsule_arn, "RestoreInProgress": False,
                "RestoreDateTime": actual_time or self.restore_at,
            },
        }})
        inventory = RestoredCapsuleInventory(capsule_arn, self.purposes, recovery_id=self.restore,
            source_table_arn=self.source_capsule_arn, restore_at=self.restore_at, client=client)
        return RestoredCapsulePlanWorker(
            locators=self.locators, inventory=inventory, tenant_resolver=self.resolver, key_map=self.key_map,
            approved_tenant_purposes=self.tenant_purposes, kms_client=self.kms,
        )

    def test_derives_targets_from_active_and_same_request_locked_restored_capsules(self):
        for meta in (self.meta, {**self.meta, "state": {"S": "DELETION_LOCKED"},
                                 "deletion_request_id": {"S": self.request}}):
            with self.subTest(state=meta["state"]):
                self.table.get_item.side_effect = [self.sealed, {"Item": self.registry_row}, {"Item": self.registry_row}]
                client = Mock(wraps=Client(meta, self.row))
                action = Mock()

                self.assertIsNone(self.worker(client).use_targets(self.expected, action))

                self.assertEqual(action.call_args.args[0].entries, (self.entry,))
                action.assert_called_once()
                self.assertEqual(client.get_item.call_count, 2)
                self.assertTrue(all(call.kwargs["TableName"] == self.capsule_arn
                                    and call.kwargs["ConsistentRead"] is True
                                    for call in client.get_item.call_args_list))

    def test_refuses_ambiguous_or_foreign_capsule_state_and_live_table(self):
        for meta in ({**self.meta, "deletion_request_id": {"NULL": True}},
                     {**self.meta, "state": {"S": "DELETION_LOCKED"}, "deletion_request_id": {"S": self.restore}}, {}):
            with self.subTest(meta_state=meta.get("state")):
                self.table.get_item.side_effect = [self.sealed, {"Item": self.registry_row}]
                action = Mock()
                with self.assertRaises(CapsuleInventoryError):
                    self.worker(Client(meta, self.row)).use_targets(self.expected, action)
                action.assert_not_called()
        with self.assertRaises(ValueError):
            self.worker(Client(self.meta, self.row), arn=self.capsule_arn.replace(f"restore-{self.restore}", "dev"))
        with self.assertRaises(ValueError):
            self.worker(Client(self.meta, self.row), arn=self.capsule_arn.replace("123456789012", "123456789013"))
        with self.assertRaises(CapsuleInventoryError):
            CapsuleInventory("live", self.purposes, client=Client(self.meta, self.row)).stage(
                self.locator, self.request, Mock())

    def test_registry_change_during_capsule_read_blocks_target_delivery(self):
        self.table.get_item.side_effect = [
            self.sealed, {"Item": self.registry_row}, {"Item": {**self.registry_row, "version": Decimal(4)}},
        ]
        action = Mock()
        with self.assertRaisesRegex(ValueError, "changed during recovery"):
            self.worker(Client(self.meta, self.row)).use_targets(self.expected, action)
        action.assert_not_called()


    def test_capsule_restored_at_another_time_fails_before_reading_items(self):
        self.table.get_item.side_effect = [self.sealed, {"Item": self.registry_row}]
        client = Mock(wraps=Client(self.meta, self.row))
        action = Mock()
        worker = self.worker(client, actual_time=self.restore_at - timedelta(seconds=1))
        with self.assertRaises(DynamoRestoreVerificationError):
            worker.use_targets(self.expected, action)
        client.get_item.assert_not_called()
        action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
