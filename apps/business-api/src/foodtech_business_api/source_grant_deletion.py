"""One catalog grant cleanup step after withdrawal's grace and deletion lock.

Trusted deletion-worker client injection only; no route, credentials or IAM are
created here. Account references remain intact until final retirement independently verifies
all catalog grants absent. This role checks but never mutates authorization.
Source data is never deleted.
"""
from uuid import UUID, uuid5

from .account_withdrawal import GRACE_SECONDS
from .source_read_authority import _integer
from .source_read_registrar import DynamoSourceReadRegistrar, SourceGrantConflict, _grant_mapping
from .subject_identity import valid_subject


class DynamoSourceGrantDeletion:
    def __init__(self, *, auth_table, catalog_table, registry_table, client, clock):
        self.registrar = DynamoSourceReadRegistrar(auth_table=auth_table, catalog_table=catalog_table,
                                                  client=client, clock=clock)
        stage = auth_table.removeprefix('foodtech-auth-sessions-')
        if registry_table != 'foodtech-deletion-registry-' + stage:
            raise ValueError('matching-stage deletion registry required')
        self.registry_table, self.client, self.clock = registry_table, client, clock

    def _locked(self, *, subject, request_id, expected_epoch):
        try:
            if not valid_subject(subject) or not isinstance(request_id, str) or str(UUID(request_id)) != request_id or \
                    type(expected_epoch) is not int or not 1 <= expected_epoch < 2**63:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('explicit locked grant deletion scope required') from None
        encode = self.registrar._encode
        account_key = encode({'pk': 'USER#' + subject})
        registry_key = encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id})
        response = self.client.transact_get_items(TransactItems=[
            {'Get': {'TableName': self.registrar.auth_table, 'Key': account_key,
                'ProjectionExpression': 'pk, #epoch, suspended, deletion_lock_request, withdrawal_request_id, '
                    'withdrawal_status, withdrawal_requested_at, withdrawal_deadline, withdrawal_epoch, source_read_grants',
                'ExpressionAttributeNames': {'#epoch': 'epoch'}}},
            {'Get': {'TableName': self.registry_table, 'Key': registry_key,
                'ProjectionExpression': 'pk, sk, request_id, #status',
                'ExpressionAttributeNames': {'#status': 'status'}}},
        ])
        try:
            rows = response['Responses']
            if not isinstance(rows, list) or len(rows) != 2:
                raise ValueError
            account, registry = (self.registrar.reader._decode(row.get('Item')) for row in rows)
            now = self.clock()
            requested, deadline = _integer(account.get('withdrawal_requested_at'), 1), \
                _integer(account.get('withdrawal_deadline'), 1)
            withdrawn_epoch = _integer(account.get('withdrawal_epoch'), 1)
            if account.get('pk') != 'USER#' + subject or account.get('suspended') is not True or \
                    _integer(account.get('epoch'), 1) != expected_epoch or \
                    account.get('deletion_lock_request') != request_id or \
                    account.get('withdrawal_request_id') != request_id or account.get('withdrawal_status') != 'DELETING' or \
                    requested is None or deadline is None or deadline - requested != GRACE_SECONDS or \
                    withdrawn_epoch is None or withdrawn_epoch >= expected_epoch or \
                    type(now) is not int or not deadline <= now < 2**63 or \
                    registry.get('pk') != 'REGISTRY' or registry.get('sk') != 'REQ#' + request_id or \
                    registry.get('request_id') != request_id or registry.get('status') != 'DELETING':
                raise ValueError
            before = _grant_mapping(account)
        except (KeyError, AttributeError, TypeError, ValueError, SourceGrantConflict):
            raise SourceGrantConflict('locked source grant deletion unavailable') from None
        return account_key, registry_key, account, registry, before

    def step(self, *, subject, request_id, expected_epoch):
        snapshot = self._locked(subject=subject, request_id=request_id, expected_epoch=expected_epoch)
        account_key, registry_key, account, registry, before = snapshot
        encode = self.registrar._encode
        entries = sorted(before.items())
        existing = []
        # Every referenced row is read, including sparse/TTL-removed entries.
        # Transaction batches are bounded by DynamoDB's 100-item limit.
        for start in range(0, len(entries), 100):
            batch = entries[start:start + 100]
            response = self.client.transact_get_items(TransactItems=[{'Get': {
                'TableName': self.registrar.catalog_table,
                'Key': encode({'pk': 'GRANT#' + grant, 'sk': 'META'}),
                'ProjectionExpression': 'pk, sk, grant_id, source_partition, #permission, #schema, #version',
                'ExpressionAttributeNames': {'#permission': 'permission', '#schema': 'schema', '#version': 'version'},
            }} for _, grant in batch])
            rows = response.get('Responses')
            if not isinstance(rows, list) or len(rows) != len(batch):
                raise SourceGrantConflict('source catalog snapshot unavailable')
            for (source, grant), row in zip(batch, rows):
                if not isinstance(row, dict):
                    raise SourceGrantConflict('source catalog snapshot unavailable')
                if 'Item' not in row:
                    continue
                item = self.registrar.reader._decode(row['Item'])
                if not item or item.get('pk') != 'GRANT#' + grant or item.get('sk') != 'META' or \
                        item.get('grant_id') != grant or item.get('source_partition') != source or \
                        item.get('permission') != 'RECIPE_SOURCE_READ' or type(item.get('schema')) is bool or \
                        _integer(item.get('schema')) != 1 or _integer(item.get('version'), 1) is None:
                    raise SourceGrantConflict('source catalog binding changed')
                existing.append((source, grant, _integer(item['version'], 1)))
        if not existing:
            if self._locked(subject=subject, request_id=request_id, expected_epoch=expected_epoch) != snapshot:
                raise SourceGrantConflict('source grant account changed')
            return {'state': 'CATALOG_EMPTY_OBSERVED', 'remaining_upper_bound': 0, 'deletion_complete': False}
        source, grant, version = existing[0]
        self.registrar._write([
            {'ConditionCheck': {'TableName': self.registrar.auth_table, 'Key': account_key,
                'ConditionExpression': '#epoch = :epoch AND suspended = :yes AND deletion_lock_request = :request '
                    'AND withdrawal_request_id = :request AND withdrawal_status = :deleting '
                    'AND withdrawal_requested_at = :requested AND withdrawal_deadline = :deadline '
                    'AND withdrawal_epoch = :withdrawn AND #grants = :before',
                'ExpressionAttributeNames': {'#epoch': 'epoch', '#grants': 'source_read_grants'},
                'ExpressionAttributeValues': encode({':epoch': expected_epoch, ':yes': True, ':request': request_id,
                    ':deleting': 'DELETING', ':requested': account['withdrawal_requested_at'],
                    ':deadline': account['withdrawal_deadline'], ':withdrawn': account['withdrawal_epoch'], ':before': before}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'ConditionCheck': {'TableName': self.registry_table, 'Key': registry_key,
                'ConditionExpression': '#status = :deleting AND request_id = :request',
                'ExpressionAttributeNames': {'#status': 'status'},
                'ExpressionAttributeValues': encode({':deleting': 'DELETING', ':request': request_id}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'Delete': {'TableName': self.registrar.catalog_table,
                'Key': encode({'pk': 'GRANT#' + grant, 'sk': 'META'}),
                'ConditionExpression': 'attribute_not_exists(pk) OR (grant_id = :grant AND source_partition = :source '
                    'AND #permission = :read AND #schema = :schema AND #version = :version)',
                'ExpressionAttributeNames': {'#permission': 'permission', '#schema': 'schema', '#version': 'version'},
                'ExpressionAttributeValues': encode({':grant': grant, ':source': source,
                    ':read': 'RECIPE_SOURCE_READ', ':schema': 1, ':version': version}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
        ], str(uuid5(UUID(request_id), 'source-catalog-delete:' + grant + ':' + str(version))))
        # Concurrent deletions can reduce this number; it is an upper bound,
        # never an erasure receipt or permission to discard the reference list.
        return {'state': 'GRANT_REMOVED', 'remaining_upper_bound': len(existing) - 1, 'deletion_complete': False}
