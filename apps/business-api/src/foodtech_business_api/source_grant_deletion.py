"""One grant-reference cleanup step after withdrawal's grace and deletion lock.

Trusted deletion-worker client injection only; no route, credentials or IAM are
created here. The account retains its durable references until each catalog row
and its reference can be removed atomically. Source data is never deleted.
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

    def step(self, *, subject, request_id, expected_epoch):
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
            {'Get': {'TableName': self.registrar.auth_table, 'Key': account_key}},
            {'Get': {'TableName': self.registry_table, 'Key': registry_key}},
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
        if not before:
            # Absence in one authorized snapshot is not an account erasure receipt.
            return {'state': 'REFERENCES_EMPTY', 'remaining': 0, 'deletion_complete': False}
        source = sorted(before)[0]
        grant = before[source]
        after = {key: value for key, value in before.items() if key != source}
        self.registrar._write([
            {'Update': {'TableName': self.registrar.auth_table, 'Key': account_key,
                'UpdateExpression': 'SET #grants = :after',
                'ConditionExpression': '#epoch = :epoch AND suspended = :yes AND deletion_lock_request = :request '
                    'AND withdrawal_request_id = :request AND withdrawal_status = :deleting '
                    'AND withdrawal_requested_at = :requested AND withdrawal_deadline = :deadline '
                    'AND withdrawal_epoch = :withdrawn AND #grants = :before',
                'ExpressionAttributeNames': {'#epoch': 'epoch', '#grants': 'source_read_grants'},
                'ExpressionAttributeValues': encode({':epoch': expected_epoch, ':yes': True, ':request': request_id,
                    ':deleting': 'DELETING', ':requested': requested, ':deadline': deadline,
                    ':withdrawn': withdrawn_epoch, ':before': before, ':after': after}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'ConditionCheck': {'TableName': self.registry_table, 'Key': registry_key,
                'ConditionExpression': '#status = :deleting AND request_id = :request',
                'ExpressionAttributeNames': {'#status': 'status'},
                'ExpressionAttributeValues': encode({':deleting': 'DELETING', ':request': request_id}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            {'Delete': {'TableName': self.registrar.catalog_table,
                'Key': encode({'pk': 'GRANT#' + grant, 'sk': 'META'}),
                'ConditionExpression': 'attribute_not_exists(pk) OR (grant_id = :grant AND source_partition = :source '
                    'AND #permission = :read AND #schema = :schema)',
                'ExpressionAttributeNames': {'#permission': 'permission', '#schema': 'schema'},
                'ExpressionAttributeValues': encode({':grant': grant, ':source': source,
                    ':read': 'RECIPE_SOURCE_READ', ':schema': 1}),
                'ReturnValuesOnConditionCheckFailure': 'NONE'}},
        ], str(uuid5(UUID(request_id), 'source-grant-delete:' + grant)))
        return {'state': 'GRANT_REFERENCE_REMOVED', 'remaining': len(after), 'deletion_complete': False}
