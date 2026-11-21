"""Restricted discovery of legacy subject-keyed AI registry partitions.

Scan checkpoints can contain other tenants' keys. Caller authorization,
checkpoint encryption, write quiescence, vault discovery, and deletion are not
provided. A completed scan is not a snapshot or proof of complete removal.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer


@dataclass(frozen=True)
class InventoryPage:
    partitions: tuple[str, ...] = field(repr=False)
    checkpoint: dict | None = field(repr=False)
    scanned_count: int


@dataclass(frozen=True)
class VaultLocation:
    key: dict = field(repr=False)
    tenant_pseudonym: str = field(repr=False)


class CredentialDeletionConflict(Exception):
    pass


def _scope(tenant: str, limit: int) -> None:
    if not isinstance(tenant, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tenant) or \
            type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid tenant inventory scope')


def _checkpoint(checkpoint: dict | None) -> None:
    if checkpoint is not None and (not isinstance(checkpoint, dict) or set(checkpoint) != {'pk', 'sk'} or
            any(not isinstance(part, str) or not part or len(part) > 2048 for part in checkpoint.values())):
        raise ValueError('invalid inventory checkpoint')


def scan_registry_partitions(table, *, tenant: str, limit: int,
                             checkpoint: dict | None = None) -> InventoryPage:
    _scope(tenant, limit)
    _checkpoint(checkpoint)
    marker = f'#TENANT#{tenant}'
    args = {'FilterExpression': Attr('sk').eq('META') &
            (Attr('tenant_id').eq(tenant) | (Attr('pk').begins_with('LIMIT#') & Attr('pk').contains(marker))),
            'ProjectionExpression': 'pk, sk, tenant_id', 'ConsistentRead': True, 'Limit': limit}
    if checkpoint is not None:
        args['ExclusiveStartKey'] = checkpoint
    page = table.scan(**args)
    items, count = page.get('Items'), page.get('ScannedCount')
    if not isinstance(items, list) or type(count) is not int or not 0 <= count <= limit:
        raise ValueError('invalid inventory page')
    partitions = []
    for item in items:
        pk = item.get('pk')
        if not isinstance(pk, str) or not pk or len(pk) > 2048 or item.get('sk') != 'META':
            raise ValueError('invalid tenant registry record')
        if pk.startswith('LIMIT#'):
            if re.fullmatch(r'LIMIT#[0-9]{4}-(0[1-9]|1[0-2])#TENANT#' + re.escape(tenant), pk):
                partitions.append(pk)
            elif item.get('tenant_id') == tenant:
                raise ValueError('unexpected tenant limit key')
            continue
        if item.get('tenant_id') != tenant or not re.fullmatch(r'(EXEC|CONSENT|QUOTE|CREDENTIAL)#[^#]{1,256}', pk):
            raise ValueError('unexpected tenant registry record')
        partitions.append(pk)
    next_key = page.get('LastEvaluatedKey')
    _checkpoint(next_key)
    return InventoryPage(tuple(partitions), next_key, count)


def list_partition_keys(table, *, tenant: str, partition: str, limit: int,
                        cursor: str | None = None) -> dict:
    """List a discovered partition's keys; recheck the tenant on its META item.

    Child rows need not repeat tenant_id. The caller must block concurrent
    writes and keep partition identifiers within an authenticated worker.
    """
    _scope(tenant, limit)
    if not isinstance(partition, str) or not partition or len(partition) > 2048 or \
            (cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048)):
        raise ValueError('invalid partition inventory scope')
    owner = table.get_item(Key={'pk': partition, 'sk': 'META'}, ConsistentRead=True).get('Item')
    if not owner or owner.get('pk') != partition or owner.get('sk') != 'META':
        raise ValueError('inventory partition unavailable')
    is_limit = bool(re.fullmatch(r'LIMIT#[0-9]{4}-(0[1-9]|1[0-2])#TENANT#' + re.escape(tenant), partition))
    is_owned = bool(re.fullmatch(r'(EXEC|CONSENT|QUOTE|CREDENTIAL)#[^#]{1,256}', partition)) and owner.get('tenant_id') == tenant
    if not (is_limit or is_owned):
        raise ValueError('inventory partition unavailable')
    args = {'KeyConditionExpression': Key('pk').eq(partition), 'ProjectionExpression': 'pk, sk',
            'ConsistentRead': True, 'Limit': limit}
    if cursor is not None:
        args['ExclusiveStartKey'] = {'pk': partition, 'sk': cursor}
    page = table.query(**args)
    keys = []
    for item in page.get('Items', []):
        if item.get('pk') != partition or not isinstance(item.get('sk'), str) or not 1 <= len(item['sk']) <= 2048:
            raise ValueError('invalid partition inventory record')
        keys.append(item['sk'])
    next_key = page.get('LastEvaluatedKey')
    _checkpoint(next_key)
    if next_key is not None and next_key['pk'] != partition:
        raise ValueError('invalid partition inventory checkpoint')
    return {'keys': keys, 'next_cursor': next_key['sk'] if next_key else None}


def resolve_credential_vault(registry, vault, *, tenant: str, credential_id: str) -> VaultLocation:
    """Resolve an existing vault key only after matching tenant and both records.

    This reads metadata only: caller authorization, write quiescence and
    deletion with an atomic lock are mandatory separate responsibilities.
    """
    _scope(tenant, 1)
    if not isinstance(credential_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', credential_id):
        raise ValueError('invalid credential inventory scope')
    key = {'pk': f'CREDENTIAL#{credential_id}', 'sk': 'META'}
    record = registry.get_item(Key=key, ConsistentRead=True).get('Item')
    if not record or any(record.get(name) != value for name, value in key.items()) or \
            record.get('credential_id') != credential_id or record.get('tenant_id') != tenant or \
            record.get('vault_ref') != f'vault:{credential_id}':
        raise ValueError('credential inventory unavailable')
    pseudonym = record.get('tenant_pseudonym')
    if not isinstance(pseudonym, str) or not pseudonym or len(pseudonym) > 256:
        raise ValueError('credential inventory unavailable')
    stored = vault.get_item(Key=key, ProjectionExpression='pk, sk, credential_id, tenant_pseudonym',
                            ConsistentRead=True).get('Item')
    if not stored or any(stored.get(name) != value for name, value in key.items()) or \
            stored.get('credential_id') != credential_id or stored.get('tenant_pseudonym') != pseudonym:
        raise ValueError('credential inventory unavailable')
    return VaultLocation(key, pseudonym)


def delete_credential_pair(client, *, auth_table: str, registry_table: str, vault_table: str,
                           tenant: str, request_id: str, location: VaultLocation) -> None:
    """Delete matched metadata and vault entry under the exact deletion lock.

    The caller must authenticate the request and obtain the location from a
    trusted resolution. Related active executions, backups and all other data
    must be handled separately before deletion can be called complete.
    """
    _scope(tenant, 1)
    tables = (auth_table, registry_table, vault_table)
    if any(not isinstance(table, str) or not table for table in tables) or len(set(tables)) != 3:
        raise ValueError('separate authentication, registry and vault tables required')
    try:
        if str(UUID(request_id)) != request_id or not isinstance(location, VaultLocation):
            raise ValueError
        key = location.key
        if not isinstance(key, dict) or set(key) != {'pk', 'sk'} or key['sk'] != 'META' or \
                not isinstance(key['pk'], str) or not re.fullmatch(r'CREDENTIAL#[A-Za-z0-9_-]{1,128}', key['pk']):
            raise ValueError
        credential_id = key['pk'].removeprefix('CREDENTIAL#')
        pseudonym = location.tenant_pseudonym
        if not isinstance(pseudonym, str) or not pseudonym or len(pseudonym) > 256:
            raise ValueError
    except (ValueError, TypeError, AttributeError, KeyError):
        raise ValueError('invalid credential deletion scope') from None
    serializer = TypeSerializer()
    def encode(values):
        return {name: serializer.serialize(value) for name, value in values.items()}
    try:
        client.transact_write_items(TransactItems=[
            {'ConditionCheck': {'TableName': auth_table, 'Key': encode({'pk': f'USER#{tenant}'}),
                                'ConditionExpression': 'deletion_lock_request = :request',
                                'ExpressionAttributeValues': encode({':request': request_id})}},
            {'Delete': {'TableName': registry_table, 'Key': encode(key),
                        'ConditionExpression': 'attribute_not_exists(pk) OR (tenant_id = :tenant AND credential_id = :id AND tenant_pseudonym = :pseudonym AND vault_ref = :vault)',
                        'ExpressionAttributeValues': encode({':tenant': tenant, ':id': credential_id,
                                                              ':pseudonym': pseudonym, ':vault': f'vault:{credential_id}'})}},
            {'Delete': {'TableName': vault_table, 'Key': encode(key),
                        'ConditionExpression': 'attribute_not_exists(pk) OR (credential_id = :id AND tenant_pseudonym = :pseudonym)',
                        'ExpressionAttributeValues': encode({':id': credential_id, ':pseudonym': pseudonym})}},
        ])
    except client.exceptions.TransactionCanceledException as exc:
        if any(reason.get('Code') == 'ConditionalCheckFailed' for reason in exc.response.get('CancellationReasons', [])):
            raise CredentialDeletionConflict from exc
        raise
