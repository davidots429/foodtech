"""Opt-in synthetic legacy AI inventory discovery against development AWS."""
import os
from uuid import uuid4

import boto3
from foodtech_ai_gateway.legacy_tenant_inventory import list_partition_keys, resolve_credential_vault, scan_registry_partitions


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('unexpected AWS account')
    table = session.resource('dynamodb').Table('foodtech-ai-execution-registry-dev')
    vault = session.resource('dynamodb').Table('foodtech-ai-credential-vault-dev')
    tenant, run = 'synthetic-' + uuid4().hex, uuid4().hex
    exec_pk, consent_pk = f'EXEC#{run}', f'CONSENT#{run}'
    credential_pk = f'CREDENTIAL#{run}'
    limit_pk, other_limit = f'LIMIT#2026-11#TENANT#{tenant}', f'LIMIT#2026-11#TENANT#{tenant}-other'
    items = [{'pk': exec_pk, 'sk': 'META', 'tenant_id': tenant},
             {'pk': exec_pk, 'sk': 'RESULT'}, {'pk': exec_pk, 'sk': 'CALL#1'},
             {'pk': consent_pk, 'sk': 'META', 'tenant_id': tenant},
             {'pk': credential_pk, 'sk': 'META', 'tenant_id': tenant, 'credential_id': run,
              'tenant_pseudonym': 'synthetic-' + run, 'vault_ref': f'vault:{run}'},
             {'pk': limit_pk, 'sk': 'META'}, {'pk': other_limit, 'sk': 'META'}]
    created = []
    vault_key = {'pk': credential_pk, 'sk': 'META'}
    vault_created = False
    try:
        for item in items:
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
            created.append({'pk': item['pk'], 'sk': item['sk']})
        vault.put_item(Item={**vault_key, 'credential_id': run, 'tenant_pseudonym': 'synthetic-' + run},
                       ConditionExpression='attribute_not_exists(pk)')
        vault_created = True
        checkpoint, found = None, set()
        for _ in range(100):
            page = scan_registry_partitions(table, tenant=tenant, limit=100, checkpoint=checkpoint)
            found.update(page.partitions)
            checkpoint = page.checkpoint
            if checkpoint is None:
                break
        if checkpoint is not None or found != {exec_pk, consent_pk, limit_pk, credential_pk}:
            raise AssertionError('synthetic tenant partitions were not discovered exactly')
        if resolve_credential_vault(table, vault, tenant=tenant, credential_id=run).key != vault_key:
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
    finally:
        for key in created:
            table.delete_item(Key=key)
        if vault_created:
            vault.delete_item(Key=vault_key)
    print('tenant partition discovery, execution children, isolation and cleanup verified')


if __name__ == '__main__':
    main()
