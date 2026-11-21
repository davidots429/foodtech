import unittest
from unittest.mock import Mock

from foodtech_ai_gateway.legacy_tenant_inventory import list_partition_keys, resolve_credential_vault, scan_registry_partitions


class TenantInventoryTests(unittest.TestCase):
    def test_scan_filters_shared_limit_substrings_and_hides_checkpoint(self):
        table = Mock()
        table.scan.return_value = {'Items': [
            {'pk': 'EXEC#one', 'sk': 'META', 'tenant_id': 'alice'},
            {'pk': 'LIMIT#2026-11#TENANT#alice', 'sk': 'META'},
            {'pk': 'LIMIT#2026-11#TENANT#alice-other', 'sk': 'META'}],
            'ScannedCount': 3, 'LastEvaluatedKey': {'pk': 'other-secret', 'sk': 'META'}}
        page = scan_registry_partitions(table, tenant='alice', limit=10)
        self.assertEqual(page.partitions, ('EXEC#one', 'LIMIT#2026-11#TENANT#alice'))
        self.assertNotIn('other-secret', repr(page))
        self.assertTrue(table.scan.call_args.kwargs['ConsistentRead'])

    def test_scan_rejects_unknown_or_malformed_tenant_records(self):
        table = Mock()
        for pk in ('MODEL#global', 'LIMIT#2026-99#TENANT#alice', 'EXEC#bad#id'):
            table.scan.return_value = {'Items': [{'pk': pk, 'sk': 'META', 'tenant_id': 'alice'}], 'ScannedCount': 1}
            with self.subTest(pk=pk), self.assertRaises(ValueError):
                scan_registry_partitions(table, tenant='alice', limit=1)
        with self.assertRaises(ValueError):
            scan_registry_partitions(table, tenant='alice#other', limit=1)

    def test_child_keys_require_owner_meta_and_are_paginated(self):
        table = Mock()
        table.get_item.return_value = {'Item': {'pk': 'EXEC#run', 'sk': 'META', 'tenant_id': 'alice'}}
        table.query.return_value = {'Items': [{'pk': 'EXEC#run', 'sk': 'CALL#1'}],
                                    'LastEvaluatedKey': {'pk': 'EXEC#run', 'sk': 'CALL#1'}}
        page = list_partition_keys(table, tenant='alice', partition='EXEC#run', limit=1)
        self.assertEqual(page, {'keys': ['CALL#1'], 'next_cursor': 'CALL#1'})
        self.assertTrue(table.get_item.call_args.kwargs['ConsistentRead'])
        self.assertTrue(table.query.call_args.kwargs['ConsistentRead'])
        with self.assertRaises(ValueError):
            list_partition_keys(table, tenant='bob', partition='EXEC#run', limit=1)
        table.query.return_value['LastEvaluatedKey']['pk'] = 'EXEC#other'
        with self.assertRaises(ValueError):
            list_partition_keys(table, tenant='alice', partition='EXEC#run', limit=1)

    def test_tenant_limit_ownership_is_encoded_in_partition_key(self):
        table = Mock()
        table.get_item.return_value = {'Item': {'pk': 'LIMIT#2026-11#TENANT#alice', 'sk': 'META'}}
        table.query.return_value = {'Items': [{'pk': 'LIMIT#2026-11#TENANT#alice', 'sk': 'META'}]}
        self.assertEqual(list_partition_keys(table, tenant='alice', partition='LIMIT#2026-11#TENANT#alice',
                                             limit=10)['keys'], ['META'])
        with self.assertRaises(ValueError):
            list_partition_keys(table, tenant='bob', partition='LIMIT#2026-11#TENANT#alice', limit=10)

    def test_vault_resolution_checks_both_records_and_hides_location(self):
        registry, vault = Mock(), Mock()
        key = {'pk': 'CREDENTIAL#key1', 'sk': 'META'}
        meta = {**key, 'tenant_id': 'alice', 'credential_id': 'key1',
                'tenant_pseudonym': 'private-pseudonym', 'vault_ref': 'vault:key1'}
        registry.get_item.return_value = {'Item': meta}
        vault.get_item.return_value = {'Item': {**key, 'credential_id': 'key1', 'tenant_pseudonym': 'private-pseudonym'}}
        result = resolve_credential_vault(registry, vault, tenant='alice', credential_id='key1')
        self.assertEqual(result.key, key)
        self.assertNotIn('key1', repr(result))
        self.assertNotIn('ciphertext', vault.get_item.call_args.kwargs['ProjectionExpression'])
        with self.assertRaises(ValueError):
            resolve_credential_vault(registry, vault, tenant='bob', credential_id='key1')
        vault.get_item.return_value['Item']['tenant_pseudonym'] = 'other'
        with self.assertRaises(ValueError):
            resolve_credential_vault(registry, vault, tenant='alice', credential_id='key1')
        registry.get_item.return_value['Item']['vault_ref'] = 'vault:other'
        with self.assertRaises(ValueError):
            resolve_credential_vault(registry, vault, tenant='alice', credential_id='key1')


if __name__ == '__main__':
    unittest.main()
