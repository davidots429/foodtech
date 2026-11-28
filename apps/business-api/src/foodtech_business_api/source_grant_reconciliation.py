"""Reapply archived grants to an explicitly isolated restored catalog only.

Trusted restore-worker primitive, not caller authentication or restore promotion.
A global coordinator must establish/renew the live Registry's versioned hold,
freeze restored writers, and verify every store before releasing that hold.
Application clock checks do not provide a DynamoDB server-time predicate.
"""
import json
import re
from decimal import Decimal
from uuid import UUID, uuid5

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .dynamodb_restore_verification import aware_timestamp, verify_restored_table
from .source_grant_contract import SourceGrantConflict, _grant_mapping
from .subject_identity import valid_subject
from .source_grant_inventory import MAX_GRANTS


def validate_restore_page(start, limit):
    if type(start) is not int or not 0 <= start <= 2 * MAX_GRANTS or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('invalid source restoration page')


class SourceGrantReconciliation:
    def __init__(self, *, inventory, client, restored_catalog_arn, clock, restored_auth_arn=None, restore_at=None):
        source_arn = inventory.cipher.source_table_arn
        match = re.fullmatch(r'arn:aws:dynamodb:ap-southeast-2:([0-9]{12}):table/foodtech-source-access-[a-z0-9-]+', source_arn)
        if not match or not isinstance(restored_catalog_arn, str) or not re.fullmatch(
                rf'arn:aws:dynamodb:ap-southeast-2:{match[1]}:table/foodtech-source-access-restore-[a-f0-9-]{{36}}',
                restored_catalog_arn):
            raise ValueError('explicit same-account isolated source restore table required')
        name = restored_catalog_arn.split(':table/', 1)[1]
        run = name.removeprefix('foodtech-source-access-restore-')
        if str(UUID(run)) != run or name in {inventory.catalog_table, inventory.store.auth_table,
                                           inventory.store.registry_table, inventory.store.progress_table}:
            raise ValueError('restore table must differ from live storage')
        expected_auth = (f'arn:aws:dynamodb:ap-southeast-2:{match[1]}:'
                         f'table/foodtech-auth-sessions-restore-{run}')
        if restored_auth_arn is not None and restored_auth_arn != expected_auth:
            raise ValueError('explicit same-run isolated Auth restore table required')
        if restored_auth_arn is not None and not aware_timestamp(restore_at):
            raise ValueError('explicit DynamoDB restore point required')
        if restored_auth_arn is None and restore_at is not None:
            raise ValueError('explicit Auth restore table required with the restore point')
        self.restore_at = restore_at
        self.restored_auth_arn = restored_auth_arn
        self.source_auth_arn = f'arn:aws:dynamodb:ap-southeast-2:{match[1]}:table/{inventory.store.auth_table}'
        self.inventory, self.client, self.clock = inventory, client, clock
        self.restored_arn = self.table = restored_catalog_arn
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def _encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    def _lease(self, request_id, reconciliation_id, expected_version):
        try:
            if str(UUID(request_id)) != request_id or str(UUID(reconciliation_id)) != reconciliation_id or \
                    type(expected_version) is not int or not 1 <= expected_version < 2**63:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('explicit versioned restoration scope required') from None
        result = self.client.get_item(TableName=self.inventory.store.registry_table,
            Key=self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}), ConsistentRead=True,
            ProjectionExpression='pk, sk, request_id, #status, #version, reconciliation_id, '
                                 'reconciliation_hold_until, source_grant_restore_table_arn, source_archive_purge_id',
            ExpressionAttributeNames={'#status': 'status', '#version': 'version'})
        try:
            row = {key: self.decoder.deserialize(value) for key, value in result['Item'].items()}
            hold = row['reconciliation_hold_until']
            now = self.clock()
            if row.get('pk') != 'REGISTRY' or row.get('sk') != 'REQ#' + request_id or \
                    row.get('request_id') != request_id or row.get('status') != 'RECONCILING' or 'source_archive_purge_id' in row or \
                    type(row.get('version')) is bool or row.get('version') != expected_version or \
                    row.get('reconciliation_id') != reconciliation_id or \
                    row.get('source_grant_restore_table_arn') != self.restored_arn or \
                    isinstance(hold, bool) or not isinstance(hold, (int, Decimal)) or \
                    (isinstance(hold, Decimal) and not hold.is_finite()) or hold != int(hold) or not 0 < hold < 2**63 or type(now) is not int or not 0 < now < hold:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise SourceGrantConflict('active source restoration hold unavailable') from None
        return int(hold)

    def _check(self, request_id, reconciliation_id, version, hold):
        now = self.clock()
        if type(now) is not int or not 0 < now < hold:
            raise SourceGrantConflict('source restoration hold elapsed')
        return {'ConditionCheck': {'TableName': self.inventory.store.registry_table,
            'Key': self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}),
            'ConditionExpression': 'request_id = :request AND #status = :active AND #version = :version '
                'AND reconciliation_id = :restore AND reconciliation_hold_until = :hold '
                'AND reconciliation_hold_until > :now AND source_grant_restore_table_arn = :table '
                'AND attribute_not_exists(source_archive_purge_id)',
            'ExpressionAttributeNames': {'#status': 'status', '#version': 'version'},
            'ExpressionAttributeValues': self._encode({':request': request_id, ':active': 'RECONCILING',
                ':version': version, ':restore': reconciliation_id, ':hold': hold, ':now': now, ':table': self.restored_arn}),
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}

    def _verify_restore_pair(self):
        if self.restored_auth_arn is not None:
            verify_restored_table(self.client, source_arn=self.inventory.cipher.source_table_arn,
                restored_arn=self.restored_arn, restore_at=self.restore_at)
            verify_restored_table(self.client, source_arn=self.source_auth_arn,
                restored_arn=self.restored_auth_arn, restore_at=self.restore_at)

    def _restored_auth(self, subject):
        if self.restored_auth_arn is None:
            return None
        if not valid_subject(subject):
            raise ValueError('valid archived subject required')
        row = self.client.get_item(TableName=self.restored_auth_arn,
            Key=self._encode({'pk': 'USER#' + subject}), ConsistentRead=True,
            ProjectionExpression='pk, #grants',
            ExpressionAttributeNames={'#grants': 'source_read_grants'}).get('Item')
        if row is None:
            return None
        try:
            fields = {key: self.decoder.deserialize(value) for key, value in row.items()}
            if fields.get('pk') != 'USER#' + subject:
                raise ValueError
            _grant_mapping(fields)
            return fields
        except (TypeError, ValueError, AttributeError):
            raise SourceGrantConflict('restored source grant account unavailable') from None

    def _auth_check(self, subject, snapshot):
        check = {'TableName': self.restored_auth_arn, 'Key': self._encode({'pk': 'USER#' + subject}),
                 'ReturnValuesOnConditionCheckFailure': 'NONE'}
        if snapshot is None:
            check['ConditionExpression'] = 'attribute_not_exists(pk)'
        else:
            check['ExpressionAttributeNames'] = {'#grants': 'source_read_grants'}
            if 'source_read_grants' in snapshot:
                check['ConditionExpression'] = 'attribute_exists(pk) AND #grants = :grants'
                check['ExpressionAttributeValues'] = self._encode({':grants': snapshot['source_read_grants']})
            else:
                check['ConditionExpression'] = 'attribute_exists(pk) AND attribute_not_exists(#grants)'
        return {'ConditionCheck': check}

    @staticmethod
    def _references(archive, snapshot):
        restored = _grant_mapping(snapshot or {})
        if len(restored) > MAX_GRANTS:
            raise SourceGrantConflict('restored source grant scope exceeds supported size')
        pairs = set(archive.items()) | set(restored.items())
        if len({grant for _, grant in pairs}) != len(pairs):
            raise SourceGrantConflict('restored source grant identifier has conflicting bindings')
        return sorted(pairs)

    def delete_page(self, *, subject, request_id, reconciliation_id, expected_version, start=0, limit=20):
        """Bounded idempotent deletes; canceled/changed holds fail in each transaction."""
        validate_restore_page(start, limit)
        hold = self._lease(request_id, reconciliation_id, expected_version)
        self._verify_restore_pair()
        snapshot = self._restored_auth(subject)
        totals = {}
        def action(references):
            rows = self._references(references, snapshot)
            if start > len(rows):
                raise SourceGrantConflict('source restoration page outside inventory')
            end = min(start + limit, len(rows))
            for source, grant in rows[start:end]:
                check = self._check(request_id, reconciliation_id, expected_version, hold)
                delete = {'Delete': {'TableName': self.table,
                    'Key': self._encode({'pk': 'GRANT#' + grant, 'sk': 'META'}),
                    'ConditionExpression': 'attribute_not_exists(pk) OR (grant_id = :grant AND source_partition = :source '
                                          'AND #permission = :read AND #schema = :schema)',
                    'ExpressionAttributeNames': {'#permission': 'permission', '#schema': 'schema'},
                    'ExpressionAttributeValues': self._encode({':grant': grant, ':source': source,
                                                               ':read': 'RECIPE_SOURCE_READ', ':schema': 1}),
                    'ReturnValuesOnConditionCheckFailure': 'NONE'}}
                operations = [check, delete]
                token_input = (f'source-grant:{request_id}:{expected_version}:{grant}:{hold}:'
                               f'{check["ConditionCheck"]["ExpressionAttributeValues"][":now"]["N"]}')
                if self.restored_auth_arn is not None:
                    auth_check = self._auth_check(subject, snapshot)
                    operations.append(auth_check)
                    token_input += ':' + json.dumps(auth_check, sort_keys=True, separators=(',', ':'))
                try:
                    self.client.transact_write_items(TransactItems=operations,
                        ClientRequestToken=str(uuid5(UUID(reconciliation_id), token_input)))
                except self.client.exceptions.TransactionCanceledException:
                    raise SourceGrantConflict('source restoration lease or grant changed') from None
            if self._lease(request_id, reconciliation_id, expected_version) != hold:
                raise SourceGrantConflict('source restoration hold changed')
            totals.update(delete_acknowledged=end - start, next_index=end,
                          references_exhausted=end == len(rows), deletion_complete=False)
        self.inventory.use_references(subject=subject, request_id=request_id, action=action)
        return totals

    def observe_empty(self, *, subject, request_id, reconciliation_id, expected_version):
        """Observe absence across the configured archive and restore sources."""
        hold = self._lease(request_id, reconciliation_id, expected_version)
        self._verify_restore_pair()
        snapshot = self._restored_auth(subject)
        result = {}
        def action(references):
            grants = [grant for _, grant in self._references(references, snapshot)]
            residual = False
            for offset in range(0, len(grants), 100):
                page = grants[offset:offset + 100]
                response = self.client.transact_get_items(TransactItems=[{'Get': {'TableName': self.table,
                    'Key': self._encode({'pk': 'GRANT#' + grant, 'sk': 'META'}),
                    'ProjectionExpression': 'pk, sk'}} for grant in page])
                rows = response.get('Responses')
                if not isinstance(rows, list) or len(rows) != len(page) or any(not isinstance(row, dict) for row in rows):
                    raise SourceGrantConflict('source restore residual observation unavailable')
                residual = residual or any('Item' in row for row in rows)
            if self._restored_auth(subject) != snapshot:
                raise SourceGrantConflict('restored source grant account changed')
            if self._lease(request_id, reconciliation_id, expected_version) != hold:
                raise SourceGrantConflict('source restoration hold changed')
            result.update(state='RESIDUAL_FOUND' if residual else 'CATALOG_EMPTY_OBSERVED',
                          deletion_complete=False, restore_promotion_allowed=False)
            if self.restored_auth_arn is not None:
                result['restored_auth_checked'] = True
        self.inventory.use_references(subject=subject, request_id=request_id, action=action)
        return result
