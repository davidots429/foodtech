import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from foodtech_ai_gateway.legacy_tenant_inventory import (RegistryDeletionConflict, VaultLocation, delete_credential_pair,
                                                         delete_registry_key, delete_registry_partition_page, ensure_reserved_marker,
                                                         InventoryPage, list_partition_keys, observe_active_markers, reconcile_reserved_page, resolve_credential_vault,
                                                         scan_registry_partitions)


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

    def test_active_marker_observation_is_bounded_locked_and_fail_closed(self):
        table, client = Mock(), Mock()
        table.name = 'registry'
        request = str(uuid4())
        table.query.return_value = {'Items': [{'pk': 'ACTIVE#alice', 'sk': 'EXEC#run'}]}
        self.assertEqual(observe_active_markers(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice',
                                                request_id=request), 'PRESENT')
        self.assertEqual(client.transact_write_items.call_count, 2)
        for call in client.transact_write_items.call_args_list:
            guards=call.kwargs['TransactItems']
            self.assertIn('suspended = :yes',guards[0]['ConditionCheck']['ConditionExpression'])
            self.assertEqual(guards[1]['ConditionCheck']['ExpressionAttributeValues'][':active'],{'S':'DELETING'})
        self.assertTrue(table.query.call_args.kwargs['ConsistentRead'])
        table.query.return_value = {'Items': []}
        self.assertEqual(observe_active_markers(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice',
                                                request_id=request), 'CLEAR')
        table.query.return_value = {'Items': [{'pk': 'ACTIVE#other', 'sk': 'EXEC#run'}]}
        with self.assertRaises(ValueError):
            observe_active_markers(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice', request_id=request)
        table.query.return_value = {}
        with self.assertRaises(ValueError):
            observe_active_markers(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice', request_id=request)

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

    def test_child_inventory_rejects_malformed_or_nonadvancing_pages(self):
        table = Mock()
        table.get_item.return_value = {'Item': {'pk': 'EXEC#run', 'sk': 'META', 'tenant_id': 'alice'}}
        row = lambda sk: {'pk': 'EXEC#run', 'sk': sk}
        for result in ({}, {'Items': None}, {'Items': [row('A'), row('A')]},
                       {'Items': [row('B'), row('A')]},
                       {'Items': [], 'LastEvaluatedKey': {'pk': 'EXEC#run', 'sk': 'A'}},
                       {'Items': [row('A')], 'LastEvaluatedKey': {'pk': 'EXEC#run', 'sk': 'B'}}):
            table.query.return_value = result
            with self.subTest(result=result), self.assertRaises(ValueError):
                list_partition_keys(table, tenant='alice', partition='EXEC#run', limit=2)
        table.query.return_value = {'Items': [row('A')]}
        with self.assertRaises(ValueError):
            list_partition_keys(table, tenant='alice', partition='EXEC#run', limit=2, cursor='A')

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

    def test_credential_pair_delete_checks_scope_and_lock_in_one_transaction(self):
        client = Mock()
        client.exceptions.TransactionCanceledException = type('Canceled', (Exception,), {})
        request = str(uuid4())
        location = VaultLocation({'pk': 'CREDENTIAL#key1', 'sk': 'META'}, 'pseudo')
        delete_credential_pair(client, auth_table='auth', deletion_registry_table='deletion', registry_table='registry', vault_table='vault',
                               tenant='alice', request_id=request, location=location)
        ops = client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[0]['ConditionCheck']['Key']['pk'], {'S': 'USER#alice'})
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':request'], {'S': request})
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertIn('suspended = :yes', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[2]['Delete']['TableName'], 'registry')
        self.assertEqual(ops[3]['Delete']['TableName'], 'vault')
        self.assertIn('tenant_pseudonym = :pseudonym', ops[2]['Delete']['ConditionExpression'])
        self.assertIn('tenant_pseudonym = :pseudonym', ops[3]['Delete']['ConditionExpression'])
        with self.assertRaises(ValueError):
            delete_credential_pair(client, auth_table='auth', deletion_registry_table='deletion', registry_table='auth', vault_table='vault',
                                   tenant='alice', request_id=request, location=location)

    def test_legacy_marker_requires_same_lock_and_reserved_owner(self):
        client = Mock()
        client.exceptions.TransactionCanceledException = type('Canceled', (Exception,), {})
        request = str(uuid4())
        ensure_reserved_marker(client, auth_table='auth', registry_table='registry', deletion_registry_table='deletion', tenant='alice',
                               request_id=request, execution_id='run')
        ops = client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[0]['ConditionCheck']['ExpressionAttributeValues'][':request'], {'S': request})
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertIn('suspended = :yes', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[2]['ConditionCheck']['ExpressionAttributeValues'][':reserved'], {'S': 'RESERVED'})
        self.assertEqual(ops[2]['ConditionCheck']['ExpressionAttributeValues'][':tenant'], {'S': 'alice'})
        self.assertEqual(ops[3]['Update']['Key']['pk'], {'S': 'ACTIVE#alice'})
        with self.assertRaises(ValueError):
            ensure_reserved_marker(client, auth_table='auth', registry_table='registry', deletion_registry_table='deletion', tenant='alice',
                                   request_id=request, execution_id='bad#id')

    def test_reconcile_page_returns_progress_only_after_all_markers_succeed(self):
        table, client, request = Mock(), Mock(), str(uuid4())
        table.name = 'registry'
        client.exceptions.TransactionCanceledException = type('Canceled', (Exception,), {})
        page = InventoryPage(('EXEC#one', 'CONSENT#one', 'EXEC#two'), {'pk': 'private', 'sk': 'META'}, 3)
        table.get_item.side_effect = lambda **kw: {'Item': {**kw['Key'], 'tenant_id': 'alice', 'state': 'RESERVED'}}
        with patch('foodtech_ai_gateway.legacy_tenant_inventory.scan_registry_partitions', return_value=page), \
             patch('foodtech_ai_gateway.legacy_tenant_inventory.ensure_reserved_marker', side_effect=[None, RuntimeError('changed')]) as ensure:
            with self.assertRaises(RuntimeError):
                reconcile_reserved_page(table, client, auth_table='auth', registry_table='registry', deletion_registry_table='deletion',
                                        tenant='alice', request_id=request, limit=3)
            self.assertEqual(ensure.call_count, 2)
        with patch('foodtech_ai_gateway.legacy_tenant_inventory.scan_registry_partitions', return_value=page), \
             patch('foodtech_ai_gateway.legacy_tenant_inventory.ensure_reserved_marker') as ensure:
            result = reconcile_reserved_page(table, client, auth_table='auth', registry_table='registry', deletion_registry_table='deletion',
                                             tenant='alice', request_id=request, limit=3)
            self.assertEqual(result.checkpoint, page.checkpoint)
            self.assertEqual(ensure.call_count, 2)

    def test_registry_delete_requires_empty_parent_and_blocks_active_execution(self):
        table, client, request = Mock(), Mock(), str(uuid4())
        table.name = 'registry'
        client.exceptions.TransactionCanceledException = type('Canceled', (Exception,), {})
        table.query.return_value = {'Items': [{'pk': 'EXEC#one', 'sk': 'META'}, {'pk': 'EXEC#one', 'sk': 'RESULT'}]}
        with self.assertRaises(RegistryDeletionConflict):
            delete_registry_key(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice', request_id=request,
                                partition='EXEC#one', sk='META')
        client.transact_write_items.assert_not_called()
        delete_registry_key(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice', request_id=request,
                            partition='EXEC#one', sk='RESULT')
        ops = client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'], {'S': 'DELETING'})
        self.assertIn('suspended = :yes', ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[2]['ConditionCheck']['Key']['pk'], {'S': 'ACTIVE#alice'})
        self.assertIn('#state IN', ops[3]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[4]['Delete']['Key']['sk'], {'S': 'RESULT'})
        with self.assertRaises(ValueError):
            delete_registry_key(table, client, auth_table='auth', deletion_registry_table='deletion', tenant='alice', request_id=request,
                                partition='CREDENTIAL#one', sk='META')

    def test_partition_page_deletes_children_before_parent_and_does_not_advance_on_failure(self):
        table, client, request = Mock(), Mock(), str(uuid4())
        kwargs = dict(auth_table='auth', deletion_registry_table='deletion', tenant='alice', request_id=request, partition='EXEC#one', limit=2)
        with patch('foodtech_ai_gateway.legacy_tenant_inventory.list_partition_keys',
                   return_value={'keys': ['CALL#1', 'META'], 'next_cursor': 'META'}), \
             patch('foodtech_ai_gateway.legacy_tenant_inventory.delete_registry_key') as delete:
            page = delete_registry_partition_page(table, client, **kwargs)
            self.assertFalse(page['partition_removed'])
            self.assertEqual(page['next_cursor'], 'META')
            self.assertEqual(delete.call_args.kwargs['sk'], 'CALL#1')
        with patch('foodtech_ai_gateway.legacy_tenant_inventory.list_partition_keys',
                   return_value={'keys': ['RESULT'], 'next_cursor': None}), \
             patch('foodtech_ai_gateway.legacy_tenant_inventory.delete_registry_key',
                   side_effect=[None, RegistryDeletionConflict()]):
            with self.assertRaises(RegistryDeletionConflict):
                delete_registry_partition_page(table, client, cursor='META', **kwargs)
        with patch('foodtech_ai_gateway.legacy_tenant_inventory.list_partition_keys',
                   return_value={'keys': ['RESULT'], 'next_cursor': None}), \
             patch('foodtech_ai_gateway.legacy_tenant_inventory.delete_registry_key') as delete:
            page = delete_registry_partition_page(table, client, cursor='META', **kwargs)
            self.assertTrue(page['partition_removed'])
            self.assertEqual([call.kwargs['sk'] for call in delete.call_args_list], ['RESULT', 'META'])


if __name__ == '__main__':
    unittest.main()
