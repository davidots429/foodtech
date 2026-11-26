"""Trusted source-grant mutation, separate from customer job binding.

This is not a customer API or an authorization mechanism for its caller. Only
a separately authorized registrar may receive the client used here. No default
credentials, writer configuration, automatic grants, or public route are added.
Read grants do not establish exclusive ownership for source deletion.
"""
import re
from uuid import UUID

from boto3.dynamodb.types import TypeSerializer

from .source_read_authority import DynamoSourceReadAuthority, _integer
from .subject_identity import valid_subject
from .source_grant_contract import SourceGrantConflict, _grant_mapping


class DynamoSourceReadRegistrar:
    def __init__(self, *, auth_table, catalog_table, client, clock):
        # Reuse the reader's table boundary; do not discover or create resources.
        self.reader = DynamoSourceReadAuthority(auth_table=auth_table, catalog_table=catalog_table,
                                               client=client, clock=clock)
        self.auth_table, self.catalog_table, self.client = auth_table, catalog_table, client
        self.clock, self.serializer = clock, TypeSerializer()

    def _encode(self, row):
        return {key: self.serializer.serialize(value) for key, value in row.items()}

    def _snapshot(self, subject, expected_epoch, source):
        if not valid_subject(subject) or type(expected_epoch) is not int or not 0 <= expected_epoch < 2**63 or \
                not isinstance(source, str) or not re.fullmatch(r'JEYUK#jeyuk-[A-Za-z0-9-]{1,121}', source):
            raise ValueError('explicit account and source scope required')
        response = self.client.get_item(TableName=self.auth_table,
            Key=self._encode({'pk': 'USER#' + subject}), ConsistentRead=True)
        principal = self.reader._decode(response.get('Item'))
        if not isinstance(principal, dict) or principal.get('pk') != 'USER#' + subject or \
                _integer(principal.get('epoch')) != expected_epoch or \
                principal.get('suspended', False) is not False or 'deletion_lock_request' in principal:
            raise SourceGrantConflict('source grant account unavailable')
        return principal, _grant_mapping(principal)

    def _account_update(self, subject, epoch, principal, before, after):
        names = {'#epoch': 'epoch', '#grants': 'source_read_grants', '#suspended': 'suspended'}
        values = {':epoch': epoch, ':no': False, ':after': after}
        condition = 'attribute_exists(pk) AND #epoch = :epoch AND attribute_not_exists(deletion_lock_request) ' \
            'AND (attribute_not_exists(#suspended) OR #suspended = :no)'
        if 'source_read_grants' in principal:
            condition += ' AND #grants = :before'
            values[':before'] = before
        else:
            condition += ' AND attribute_not_exists(#grants)'
        return {'Update': {'TableName': self.auth_table, 'Key': self._encode({'pk': 'USER#' + subject}),
            'UpdateExpression': 'SET #grants = :after', 'ConditionExpression': condition,
            'ExpressionAttributeNames': names, 'ExpressionAttributeValues': self._encode(values),
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}

    def _write(self, items, token):
        try:
            self.client.transact_write_items(TransactItems=items, ClientRequestToken=token)
        except self.client.exceptions.TransactionCanceledException:
            # No unconditional fallback or read/modify/write retry after a race.
            raise SourceGrantConflict('source grant changed') from None

    def grant(self, *, subject, expected_epoch, source_partition, grant_id, expires_at):
        """Add a new explicit grant; never silently replace an existing grant.

        grant_id is the registrar's unique operation ID, never a browser input.
        Expiry is mandatory: this module does not invent a retention policy.
        """
        try:
            if not isinstance(grant_id, str) or str(UUID(grant_id)) != grant_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('canonical grant operation ID required') from None
        now = self.clock()
        if type(now) is not int or not 0 < now < 2**63 or type(expires_at) is not int or not now < expires_at < 2**63:
            raise ValueError('explicit future grant expiry required')
        principal, before = self._snapshot(subject, expected_epoch, source_partition)
        if source_partition in before or grant_id in before.values():
            raise SourceGrantConflict('source grant already exists')
        after = {**before, source_partition: grant_id}
        grant = {'pk': 'GRANT#' + grant_id, 'sk': 'META', 'schema': 1, 'grant_id': grant_id,
            'source_partition': source_partition, 'permission': 'RECIPE_SOURCE_READ', 'state': 'ACTIVE',
            'version': 1, 'expires_at': expires_at}
        final_now = self.clock()
        if type(final_now) is not int or not now <= final_now < expires_at:
            raise SourceGrantConflict('source grant expiry elapsed')
        self._write([self._account_update(subject, expected_epoch, principal, before, after),
            {'Put': {'TableName': self.catalog_table, 'Item': self._encode(grant),
                'ConditionExpression': 'attribute_not_exists(pk)', 'ReturnValuesOnConditionCheckFailure': 'NONE'}}],
            grant_id)
        return grant_id

    def revoke(self, *, subject, expected_epoch, source_partition, expected_grant_id, operation_id):
        """Remove the exact reference and its catalog row in one transaction.

        An already TTL-deleted catalog row is allowed; a different row is not.
        Full account deletion retains references during locked catalog cleanup
        and verifies encrypted recovery inventory before USER retirement. This active-account method cannot replace that lifecycle.
        """
        for value in (expected_grant_id, operation_id):
            try:
                if not isinstance(value, str) or str(UUID(value)) != value:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValueError('canonical grant operation ID required') from None
        principal, before = self._snapshot(subject, expected_epoch, source_partition)
        if before.get(source_partition) != expected_grant_id:
            raise SourceGrantConflict('source grant changed')
        after = {key: value for key, value in before.items() if key != source_partition}
        self._write([self._account_update(subject, expected_epoch, principal, before, after),
            {'Delete': {'TableName': self.catalog_table,
                'Key': self._encode({'pk': 'GRANT#' + expected_grant_id, 'sk': 'META'}),
                'ConditionExpression': 'attribute_not_exists(pk) OR '
                    '(grant_id = :grant AND source_partition = :source AND #permission = :read AND #schema = :schema)',
                'ExpressionAttributeNames': {'#permission': 'permission', '#schema': 'schema'},
                'ExpressionAttributeValues': self._encode({':grant': expected_grant_id, ':source': source_partition,
                    ':read': 'RECIPE_SOURCE_READ', ':schema': 1}), 'ReturnValuesOnConditionCheckFailure': 'NONE'}}],
            operation_id)
