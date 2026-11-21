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


class ActiveReconciliationConflict(Exception):
    pass


class RegistryDeletionConflict(Exception):
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
    items = page.get('Items')
    if not isinstance(items, list) or len(items) > limit:
        raise ValueError('invalid partition inventory page')
    keys = []
    previous = cursor
    for item in items:
        if not isinstance(item, dict) or item.get('pk') != partition or not isinstance(item.get('sk'), str) or \
                not 1 <= len(item['sk']) <= 2048 or (previous is not None and item['sk'] <= previous):
            raise ValueError('invalid partition inventory record')
        keys.append(item['sk'])
        previous = item['sk']
    next_key = page.get('LastEvaluatedKey')
    _checkpoint(next_key)
    if next_key is not None and (next_key['pk'] != partition or not keys or next_key['sk'] != keys[-1]):
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


def ensure_reserved_marker(client, *, auth_table: str, registry_table: str,
                           tenant: str, request_id: str, execution_id: str) -> None:
    """Mark a confirmed legacy reservation while its exact deletion lock is held.

    A concurrent settlement makes the condition fail; the authorized caller
    must re-read it. This does not identify all legacy executions or declare a
    safe deletion point, and it must not be exposed to customers.
    """
    _scope(tenant, 1)
    try:
        if str(UUID(request_id)) != request_id or not isinstance(execution_id, str) or \
                not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', execution_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid active reconciliation scope') from None
    if not auth_table or not registry_table or auth_table == registry_table:
        raise ValueError('separate auth and registry tables required')
    serializer = TypeSerializer()
    def encode(values):
        return {name: serializer.serialize(value) for name, value in values.items()}
    try:
        client.transact_write_items(TransactItems=[
            {'ConditionCheck': {'TableName': auth_table, 'Key': encode({'pk': f'USER#{tenant}'}),
                                'ConditionExpression': 'deletion_lock_request = :request',
                                'ExpressionAttributeValues': encode({':request': request_id})}},
            {'ConditionCheck': {'TableName': registry_table,
                                'Key': encode({'pk': f'EXEC#{execution_id}', 'sk': 'META'}),
                                'ConditionExpression': 'tenant_id = :tenant AND #state = :reserved',
                                'ExpressionAttributeNames': {'#state': 'state'},
                                'ExpressionAttributeValues': encode({':tenant': tenant, ':reserved': 'RESERVED'})}},
            {'Update': {'TableName': registry_table,
                        'Key': encode({'pk': f'ACTIVE#{tenant}', 'sk': f'EXEC#{execution_id}'}),
                        'UpdateExpression': 'SET marker_version = :version',
                        'ExpressionAttributeValues': encode({':version': 1})}},
        ])
    except client.exceptions.TransactionCanceledException as exc:
        if any(reason.get('Code') == 'ConditionalCheckFailed' for reason in exc.response.get('CancellationReasons', [])):
            raise ActiveReconciliationConflict from exc
        raise


def reconcile_reserved_page(table, client, *, auth_table: str, registry_table: str,
                            tenant: str, request_id: str, limit: int,
                            checkpoint: dict | None = None) -> InventoryPage:
    """Reconcile reserved executions in one scan page or return no progress.

    This requires an authenticated maintenance caller and durable deletion
    lock. A full scan is not a snapshot and does not prove deletion readiness.
    """
    _scope(tenant, limit)
    try:
        if str(UUID(request_id)) != request_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid active reconciliation request') from None
    if not auth_table or not registry_table or auth_table == registry_table or getattr(table, 'name', None) != registry_table:
        raise ValueError('invalid active reconciliation tables')
    serializer = TypeSerializer()
    def encode(values):
        return {name: serializer.serialize(value) for name, value in values.items()}
    def check_lock():
        try:
            client.transact_write_items(TransactItems=[{'ConditionCheck': {
                'TableName': auth_table, 'Key': encode({'pk': f'USER#{tenant}'}),
                'ConditionExpression': 'deletion_lock_request = :request',
                'ExpressionAttributeValues': encode({':request': request_id})}}])
        except client.exceptions.TransactionCanceledException as exc:
            if any(reason.get('Code') == 'ConditionalCheckFailed' for reason in exc.response.get('CancellationReasons', [])):
                raise ActiveReconciliationConflict from exc
            raise
    check_lock()
    page = scan_registry_partitions(table, tenant=tenant, limit=limit, checkpoint=checkpoint)
    for partition in page.partitions:
        if not partition.startswith('EXEC#'):
            continue
        item = table.get_item(Key={'pk': partition, 'sk': 'META'}, ConsistentRead=True).get('Item')
        if not item or item.get('pk') != partition or item.get('sk') != 'META' or item.get('tenant_id') != tenant or \
                item.get('state') not in {'PENDING', 'RESERVED', 'SUCCEEDED', 'FAILED'}:
            raise ActiveReconciliationConflict('execution changed or is unavailable')
        if item['state'] == 'RESERVED':
            ensure_reserved_marker(client, auth_table=auth_table, registry_table=registry_table,
                                   tenant=tenant, request_id=request_id, execution_id=partition.removeprefix('EXEC#'))
    check_lock()
    return page


def observe_active_markers(table, client, *, auth_table: str, tenant: str, request_id: str) -> str:
    """Observe a customer's active marker partition while the same lock holds.

    CLEAR is a point-in-time observation only: legacy reservations may lack a
    marker and older writers may not honor the lock. It is never deletion proof.
    """
    _scope(tenant, 1)
    try:
        if str(UUID(request_id)) != request_id or not isinstance(auth_table, str) or not auth_table or \
                not isinstance(getattr(table, 'name', None), str) or table.name == auth_table:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        raise ValueError('invalid active marker scope') from None
    serializer = TypeSerializer()
    def encode(values):
        return {key: serializer.serialize(value) for key, value in values.items()}
    def check_lock():
        try:
            client.transact_write_items(TransactItems=[{'ConditionCheck': {
                'TableName': auth_table, 'Key': encode({'pk': f'USER#{tenant}'}),
                'ConditionExpression': 'deletion_lock_request = :request',
                'ExpressionAttributeValues': encode({':request': request_id})}}])
        except client.exceptions.TransactionCanceledException as exc:
            if any(reason.get('Code') == 'ConditionalCheckFailed'
                   for reason in (exc.response.get('CancellationReasons') or [])):
                raise ActiveReconciliationConflict from exc
            raise
    check_lock()
    page = table.query(KeyConditionExpression=Key('pk').eq(f'ACTIVE#{tenant}'),
                       ProjectionExpression='pk, sk', ConsistentRead=True, Limit=1)
    items = page.get('Items')
    if not isinstance(items, list) or len(items) > 1:
        raise ValueError('invalid active marker response')
    if items:
        item = items[0]
        if not isinstance(item, dict) or item.get('pk') != f'ACTIVE#{tenant}' or \
                not isinstance(item.get('sk'), str) or not re.fullmatch(r'EXEC#[A-Za-z0-9_-]{1,128}', item['sk']):
            raise ValueError('invalid active marker record')
    elif page.get('LastEvaluatedKey') is not None:
        raise ValueError('invalid active marker response')
    check_lock()
    return 'PRESENT' if items else 'CLEAR'


def delete_registry_key(table, client, *, auth_table: str, deletion_registry_table: str, tenant: str, request_id: str,
                        partition: str, sk: str) -> None:
    """Delete a noncredential registry key under the exact deletion lock.

    The trusted caller must enforce complete discovery and quiesce older
    writers. META deletion is refused while child keys remain. This primitive
    does not establish global deletion completion or authorize its caller.
    """
    _scope(tenant, 1)
    try:
        if str(UUID(request_id)) != request_id or not isinstance(partition, str) or \
                not isinstance(sk, str) or not 1 <= len(sk) <= 2048:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid registry deletion scope') from None
    table_name = getattr(table, 'name', None)
    if not isinstance(auth_table, str) or not auth_table or not isinstance(table_name, str) or \
            not table_name or not isinstance(deletion_registry_table,str) or not deletion_registry_table or \
            len({auth_table,table_name,deletion_registry_table}) != 3:
        raise ValueError('separate auth and registry tables required')
    execution = bool(re.fullmatch(r'EXEC#[A-Za-z0-9_-]{1,128}', partition))
    ordinary = bool(re.fullmatch(r'(CONSENT|QUOTE)#[A-Za-z0-9_-]{1,128}', partition))
    limit = bool(re.fullmatch(r'LIMIT#[0-9]{4}-(0[1-9]|1[0-2])#TENANT#' + re.escape(tenant), partition))
    if not (execution or ordinary or limit):
        raise ValueError('unsupported registry deletion partition')
    if sk == 'META':
        page = table.query(KeyConditionExpression=Key('pk').eq(partition), ProjectionExpression='pk, sk',
                           ConsistentRead=True, Limit=2)
        items = page.get('Items')
        if not isinstance(items, list) or page.get('LastEvaluatedKey') or any(
                item.get('pk') != partition or item.get('sk') != 'META' for item in items) or len(items) > 1:
            raise RegistryDeletionConflict('partition contains unresolved child records')
    serializer = TypeSerializer()
    def encode(values):
        return {name: serializer.serialize(value) for name, value in values.items()}
    operations = [{'ConditionCheck': {'TableName': auth_table, 'Key': encode({'pk': f'USER#{tenant}'}),
        'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
        'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
        {'ConditionCheck': {'TableName': deletion_registry_table,
        'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
        'ConditionExpression': '#status = :active', 'ExpressionAttributeNames': {'#status': 'status'},
        'ExpressionAttributeValues': encode({':active': 'DELETING'})}}]
    if execution:
        operations.append({'ConditionCheck': {'TableName': table_name,
            'Key': encode({'pk': f'ACTIVE#{tenant}', 'sk': partition}),
            'ConditionExpression': 'attribute_not_exists(pk)'}})
    if limit:
        parent_condition, parent_values, names = 'attribute_exists(pk)', {}, {}
    else:
        parent_condition, parent_values, names = 'tenant_id = :tenant', {':tenant': tenant}, {}
        if execution:
            parent_condition += ' AND #state IN (:pending, :success, :failed)'
            parent_values.update({':pending': 'PENDING', ':success': 'SUCCEEDED', ':failed': 'FAILED'})
            names['#state'] = 'state'
    def condition_fields(expression):
        fields = {'ConditionExpression': expression}
        if parent_values: fields['ExpressionAttributeValues'] = encode(parent_values)
        if names: fields['ExpressionAttributeNames'] = names
        return fields
    if sk != 'META':
        operations.append({'ConditionCheck': {'TableName': table_name,
            'Key': encode({'pk': partition, 'sk': 'META'}), **condition_fields(parent_condition)}})
    delete = {'TableName': table_name, 'Key': encode({'pk': partition, 'sk': sk})}
    if sk == 'META':
        delete.update(condition_fields(f'attribute_not_exists(pk) OR ({parent_condition})'))
    operations.append({'Delete': delete})
    try:
        client.transact_write_items(TransactItems=operations)
    except client.exceptions.TransactionCanceledException as exc:
        if any(reason.get('Code') == 'ConditionalCheckFailed' for reason in exc.response.get('CancellationReasons', [])):
            raise RegistryDeletionConflict from exc
        raise


def delete_registry_partition_page(table, client, *, auth_table: str, deletion_registry_table: str, tenant: str, request_id: str,
                                   partition: str, limit: int, cursor: str | None = None) -> dict:
    """Remove a bounded page of child keys, then remove META after a final check.

    Progress is returned only after the whole page succeeds. The caller must
    protect cursors, authenticate the deletion and stop legacy writers. A
    removed partition is not evidence of full customer deletion.
    """
    page = list_partition_keys(table, tenant=tenant, partition=partition, limit=limit, cursor=cursor)
    removed = 0
    for sk in page['keys']:
        if sk != 'META':
            delete_registry_key(table, client, auth_table=auth_table, deletion_registry_table=deletion_registry_table, tenant=tenant, request_id=request_id,
                                partition=partition, sk=sk)
            removed += 1
    if page['next_cursor'] is None:
        delete_registry_key(table, client, auth_table=auth_table, deletion_registry_table=deletion_registry_table, tenant=tenant, request_id=request_id,
                            partition=partition, sk='META')
        return {'deleted_children': removed, 'next_cursor': None, 'partition_removed': True}
    return {'deleted_children': removed, 'next_cursor': page['next_cursor'], 'partition_removed': False}


def delete_credential_pair(client, *, auth_table: str, registry_table: str, vault_table: str, deletion_registry_table: str,
                           tenant: str, request_id: str, location: VaultLocation) -> None:
    """Delete matched metadata and vault entry under the exact deletion lock.

    The caller must authenticate the request and obtain the location from a
    trusted resolution. Related active executions, backups and all other data
    must be handled separately before deletion can be called complete.
    """
    _scope(tenant, 1)
    tables = (auth_table, registry_table, vault_table, deletion_registry_table)
    if any(not isinstance(table, str) or not table for table in tables) or len(set(tables)) != 4:
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
                                'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                                'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
            {'ConditionCheck': {'TableName': deletion_registry_table,
                                'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
                                'ConditionExpression': '#status = :active', 'ExpressionAttributeNames': {'#status': 'status'},
                                'ExpressionAttributeValues': encode({':active': 'DELETING'})}},
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
