"""Opt-in synthetic legacy AI inventory discovery against development AWS."""
import os
from uuid import uuid4

import boto3
from foodtech_ai_gateway.legacy_tenant_inventory import (ActiveReconciliationConflict, CredentialDeletionConflict,
                                                         RegistryDeletionConflict, delete_credential_pair, delete_registry_key,
                                                         delete_registry_partition_page,
                                                         ensure_reserved_marker,
                                                         list_partition_keys, reconcile_reserved_page, resolve_credential_vault,
                                                         scan_registry_partitions)


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    table = session.resource('dynamodb').Table('foodtech-ai-execution-registry-dev')
    vault = session.resource('dynamodb').Table('foodtech-ai-credential-vault-dev')
    auth = session.resource('dynamodb').Table('foodtech-auth-sessions-dev')
    tenant, run = 'synthetic-' + uuid4().hex, uuid4().hex
    exec_pk, consent_pk = f'EXEC#{run}', f'CONSENT#{run}'
    credential_pk = f'CREDENTIAL#{run}'
    limit_pk, other_limit = f'LIMIT#2026-11#TENANT#{tenant}', f'LIMIT#2026-11#TENANT#{tenant}-other'
    items = [{'pk': exec_pk, 'sk': 'META', 'tenant_id': tenant, 'state': 'RESERVED'},
             {'pk': exec_pk, 'sk': 'RESULT'}, {'pk': exec_pk, 'sk': 'CALL#1'},
             {'pk': consent_pk, 'sk': 'META', 'tenant_id': tenant},
             {'pk': credential_pk, 'sk': 'META', 'tenant_id': tenant, 'credential_id': run,
              'tenant_pseudonym': 'synthetic-' + run, 'vault_ref': f'vault:{run}'},
             {'pk': limit_pk, 'sk': 'META'}, {'pk': other_limit, 'sk': 'META'}]
    created = []
    vault_key = {'pk': credential_pk, 'sk': 'META'}
    vault_created = False
    auth_key, request = {'pk': f'USER#{tenant}'}, str(uuid4())
    marker_key = {'pk': f'ACTIVE#{tenant}', 'sk': f'EXEC#{run}'}
    auth_created = False
    try:
        for item in items:
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
            created.append({'pk': item['pk'], 'sk': item['sk']})
        vault.put_item(Item={**vault_key, 'credential_id': run, 'tenant_pseudonym': 'synthetic-' + run},
                       ConditionExpression='attribute_not_exists(pk)')
        vault_created = True
        auth.put_item(Item={**auth_key, 'deletion_lock_request': request}, ConditionExpression='attribute_not_exists(pk)')
        auth_created = True
        def ensure(request_id):
            ensure_reserved_marker(session.client('dynamodb'), auth_table=auth.name, registry_table=table.name,
                                   tenant=tenant, request_id=request_id, execution_id=run)
        try:
            ensure(str(uuid4()))
        except ActiveReconciliationConflict:
            pass
        else:
            raise AssertionError('incorrect lock restored an active marker')
        ensure(request)
        ensure(request)
        if not table.get_item(Key=marker_key, ConsistentRead=True).get('Item'):
            raise AssertionError('reserved legacy marker missing')
        table.delete_item(Key=marker_key)
        checkpoint, found_reserved = None, False
        for _ in range(100):
            page = reconcile_reserved_page(table, session.client('dynamodb'), auth_table=auth.name,
                                           registry_table=table.name, tenant=tenant, request_id=request,
                                           limit=100, checkpoint=checkpoint)
            found_reserved |= exec_pk in page.partitions
            checkpoint = page.checkpoint
            if checkpoint is None:
                break
        if checkpoint is not None or not found_reserved or not table.get_item(Key=marker_key, ConsistentRead=True).get('Item'):
            raise AssertionError('legacy scan did not reconcile reservation')
        try:
            delete_registry_key(table, session.client('dynamodb'), auth_table=auth.name, tenant=tenant,
                                request_id=request, partition=exec_pk, sk='RESULT')
        except RegistryDeletionConflict:
            pass
        else:
            raise AssertionError('active execution child was deleted')
        table.update_item(Key={'pk': exec_pk, 'sk': 'META'}, UpdateExpression='SET #s = :done',
                          ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':done': 'SUCCEEDED'})
        table.delete_item(Key=marker_key)
        try:
            ensure(request)
        except ActiveReconciliationConflict:
            pass
        else:
            raise AssertionError('settled execution regained an active marker')
        checkpoint, found = None, set()
        for _ in range(100):
            page = scan_registry_partitions(table, tenant=tenant, limit=100, checkpoint=checkpoint)
            found.update(page.partitions)
            checkpoint = page.checkpoint
            if checkpoint is None:
                break
        if checkpoint is not None or found != {exec_pk, consent_pk, limit_pk, credential_pk}:
            raise AssertionError('synthetic tenant partitions were not discovered exactly')
        location = resolve_credential_vault(table, vault, tenant=tenant, credential_id=run)
        if location.key != vault_key:
            raise AssertionError('vault location mismatch')
        cursor, keys = None, []
        while True:
            page = list_partition_keys(table, tenant=tenant, partition=exec_pk, limit=1, cursor=cursor)
            keys.extend(page['keys'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        if set(keys) != {'META', 'RESULT', 'CALL#1'}:
            raise AssertionError('execution child inventory incomplete')
        try:
            list_partition_keys(table, tenant=tenant + '-other', partition=exec_pk, limit=1)
        except ValueError:
            pass
        else:
            raise AssertionError('other tenant read partition keys')
        try:
            delete_registry_key(table, session.client('dynamodb'), auth_table=auth.name, tenant=tenant,
                                request_id=request, partition=exec_pk, sk='META')
        except RegistryDeletionConflict:
            pass
        else:
            raise AssertionError('execution parent deleted before children')
        cursor, removed = None, 0
        while True:
            page = delete_registry_partition_page(table, session.client('dynamodb'), auth_table=auth.name,
                tenant=tenant, request_id=request, partition=exec_pk, limit=1, cursor=cursor)
            removed += page['deleted_children']
            cursor = page['next_cursor']
            if page['partition_removed']:
                break
        if removed != 2:
            raise AssertionError('execution child deletion count mismatch')
        for partition in (consent_pk, limit_pk):
            delete_registry_key(table, session.client('dynamodb'), auth_table=auth.name, tenant=tenant,
                                request_id=request, partition=partition, sk='META')
        if any(table.get_item(Key={'pk': partition, 'sk': 'META'}, ConsistentRead=True).get('Item')
               for partition in (exec_pk, consent_pk, limit_pk)):
            raise AssertionError('registry parent remains after deletion')
        def remove(request_id):
            delete_credential_pair(session.client('dynamodb'), auth_table=auth.name, registry_table=table.name,
                                   vault_table=vault.name, tenant=tenant, request_id=request_id, location=location)
        try:
            remove(str(uuid4()))
        except CredentialDeletionConflict:
            pass
        else:
            raise AssertionError('incorrect deletion lock accepted')
        vault.update_item(Key=vault_key, UpdateExpression='SET tenant_pseudonym = :changed',
                          ExpressionAttributeValues={':changed': 'synthetic-other'})
        try:
            remove(request)
        except CredentialDeletionConflict:
            pass
        else:
            raise AssertionError('changed vault record was deleted')
        if not table.get_item(Key=vault_key, ConsistentRead=True).get('Item'):
            raise AssertionError('failed deletion removed registry metadata')
        vault.update_item(Key=vault_key, UpdateExpression='SET tenant_pseudonym = :original',
                          ExpressionAttributeValues={':original': 'synthetic-' + run})
        remove(request)
        remove(request)
        if table.get_item(Key=vault_key, ConsistentRead=True).get('Item') or \
                vault.get_item(Key=vault_key, ConsistentRead=True).get('Item'):
            raise AssertionError('credential pair remains after deletion')
    finally:
        for key in created:
            table.delete_item(Key=key)
        if vault_created:
            vault.delete_item(Key=vault_key)
        table.delete_item(Key=marker_key)
        if auth_created:
            auth.delete_item(Key=auth_key)
    print('tenant partition discovery, execution children, isolation and cleanup verified')


if __name__ == '__main__':
    main()
