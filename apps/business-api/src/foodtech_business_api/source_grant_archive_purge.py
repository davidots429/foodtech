"""Explicit expiry cleanup of one retired account's source-reference archive.

Trusted global maintenance only. Current Registry completion/deadlines and Auth
absence are prerequisites, not manufactured by this component. A completed
purge describes only planned primary archive rows, never backup disposal,
whole-account erasure, or restored-environment promotion.
"""
import json
import re
from uuid import UUID

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .source_grant_contract import SourceGrantConflict
from .source_grant_inventory import HEAD, MAX_GRANTS, PAGE_SIZE, _pack


class SourceGrantArchivePurge:
    def __init__(self, *, inventory, client, clock):
        self.inventory, self.client, self.clock = inventory, client, clock
        self.store = inventory.store
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def _encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    @staticmethod
    def _operation(purge_id, version, *, read_only=False):
        try:
            if not isinstance(purge_id, str) or str(UUID(purge_id)) != purge_id or \
                    type(version) is not int or not 1 <= version < (2**63 if read_only else 2**63 - 1):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('explicit source archive purge operation required') from None

    def _scope(self, subject, request_id, purge_id, version, *, read_only=False):
        self.inventory._scope(subject, request_id)
        self._operation(purge_id, version, read_only=read_only)
        now = self.clock()
        if type(now) is not int or not 0 < now < 2**63:
            raise ValueError('valid source archive purge clock required')
        return now

    def _auth_absent(self, subject):
        return {'ConditionCheck': {'TableName': self.store.auth_table,
            'Key': self._encode({'pk': 'USER#' + subject}), 'ConditionExpression': 'attribute_not_exists(pk)',
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}

    @staticmethod
    def _eligibility():
        return ('request_id = :request AND #status = :completed AND #version = :version '
            'AND attribute_type(reconcile_until, :number) AND attribute_type(expires_at, :number) '
            'AND reconcile_until > :zero AND expires_at > :zero '
            'AND reconcile_until <= :now AND expires_at <= :now '
            'AND (attribute_not_exists(reconciliation_hold_until) OR reconciliation_hold_until <= :now)')

    def _values(self, request_id, purge_id, version, now):
        return {':request': request_id, ':completed': 'COMPLETED', ':version': version,
                ':number': 'N', ':zero': 0, ':now': now, ':purge': purge_id}

    def _transact(self, items):
        try:
            self.client.transact_write_items(TransactItems=items)
        except self.client.exceptions.TransactionCanceledException:
            raise SourceGrantConflict('source archive eligibility or row changed') from None

    def begin(self, *, subject, request_id, purge_id, expected_version):
        now = self._scope(subject, request_id, purge_id, expected_version)
        if expected_version >= 2**63 - 2:
            raise ValueError('source archive purge requires two available versions')
        _, raw, _ = self.inventory._open_record(subject=subject, request_id=request_id, domain=HEAD)
        return self._begin_manifest(subject, request_id, purge_id, expected_version, bytes(raw))

    def _begin_manifest(self, subject, request_id, purge_id, expected_version, raw):
        now = self._scope(subject, request_id, purge_id, expected_version)
        if expected_version >= 2**63 - 2:
            raise ValueError('source archive purge requires two available versions')
        try:
            head = json.loads(raw)
            if not isinstance(head, dict) or set(head) != {'schema', 'count', 'pages', 'reference_sha256'} or \
                    type(head['schema']) is not int or head['schema'] != 1 or \
                    type(head['count']) is not int or not 0 <= head['count'] <= MAX_GRANTS or \
                    type(head['pages']) is not int or head['pages'] != (head['count'] + PAGE_SIZE - 1) // PAGE_SIZE or \
                    not isinstance(head['reference_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', head['reference_sha256']) or \
                    _pack(head) != raw:
                raise ValueError
        except (TypeError, ValueError, KeyError, UnicodeError):
            raise SourceGrantConflict('source archive purge manifest unavailable') from None
        values = self._values(request_id, purge_id, expected_version, now)
        values.update({':next': expected_version + 1, ':pages': head['pages'], ':active': 'IN_PROGRESS'})
        self._transact([self._auth_absent(subject), {'Update': {'TableName': self.store.registry_table,
            'Key': self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}),
            'UpdateExpression': 'SET #version = :next, source_archive_purge_id = :purge, '
                                'source_archive_pages = :pages, source_archive_purge_state = :active',
            'ConditionExpression': self._eligibility() + ' AND attribute_not_exists(source_archive_purge_id)',
            'ExpressionAttributeNames': {'#status': 'status', '#version': 'version'},
            'ExpressionAttributeValues': self._encode(values), 'ReturnValuesOnConditionCheckFailure': 'NONE'}}])
        return {'expected_version': expected_version + 1, 'pages': head['pages'],
                'state': 'IN_PROGRESS', 'deletion_complete': False}

    def _plan(self, request_id, purge_id, version, *, state='IN_PROGRESS'):
        result = self.client.get_item(TableName=self.store.registry_table,
            Key=self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}), ConsistentRead=True,
            ProjectionExpression='pk, sk, request_id, #status, #version, source_archive_purge_id, '
                'source_archive_pages, source_archive_purge_state',
            ExpressionAttributeNames={'#status': 'status', '#version': 'version'})
        try:
            row = {key: self.decoder.deserialize(value) for key, value in result['Item'].items()}
            pages = row['source_archive_pages']
            if row.get('pk') != 'REGISTRY' or row.get('sk') != 'REQ#' + request_id or \
                    row.get('request_id') != request_id or row.get('status') != 'COMPLETED' or \
                    row.get('version') != version or isinstance(row.get('version'), bool) or \
                    row.get('source_archive_purge_id') != purge_id or \
                    row.get('source_archive_purge_state') != state or isinstance(pages, bool) or \
                    pages != int(pages) or not 0 <= pages <= MAX_GRANTS // PAGE_SIZE:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise SourceGrantConflict('owned source archive purge plan unavailable') from None
        return int(pages)

    def recover(self, *, subject, request_id, purge_id, expected_version, state='IN_PROGRESS'):
        """Read an exact owned plan/terminal receipt after an uncertain response.

        Persist operation ID and intended next version before writing. PURGED
        is the historical atomic receipt, not a fresh backup/residual audit.
        """
        self._scope(subject, request_id, purge_id, expected_version, read_only=True)
        if state not in {'IN_PROGRESS', 'PURGED'}:
            raise ValueError('explicit source archive recovery state required')
        pages = self._plan(request_id, purge_id, expected_version, state=state)
        return {'state': state, 'pages': pages, 'expected_version': expected_version,
                'planned_primary_archive_purged': state == 'PURGED', 'current_residual_verified': False,
                'deletion_complete': False}

    def _registry_check(self, request_id, purge_id, version, now, pages):
        values = self._values(request_id, purge_id, version, now)
        values.update({':pages': pages, ':active': 'IN_PROGRESS'})
        return {'ConditionCheck': {'TableName': self.store.registry_table,
            'Key': self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}),
            'ConditionExpression': self._eligibility() + ' AND source_archive_purge_id = :purge '
                'AND source_archive_pages = :pages AND source_archive_purge_state = :active',
            'ExpressionAttributeNames': {'#status': 'status', '#version': 'version'},
            'ExpressionAttributeValues': self._encode(values), 'ReturnValuesOnConditionCheckFailure': 'NONE'}}

    def _key(self, request_id, domain):
        return self._encode({'pk': 'REQ#' + request_id, 'sk': 'DOMAIN#' + domain})

    def _delete(self, request_id, domain):
        return {'Delete': {'TableName': self.store.progress_table, 'Key': self._key(request_id, domain),
            'ConditionExpression': 'attribute_not_exists(pk) OR (#version = :one AND #state = :verified)',
            'ExpressionAttributeNames': {'#version': 'version', '#state': 'state'},
            'ExpressionAttributeValues': self._encode({':one': 1, ':verified': 'VERIFYING'}),
            'ReturnValuesOnConditionCheckFailure': 'NONE'}}

    def delete_page(self, *, subject, request_id, purge_id, expected_version, start=0, limit=20):
        now = self._scope(subject, request_id, purge_id, expected_version)
        if type(start) is not int or not 0 <= start <= 40 or type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid source archive purge page')
        pages = self._plan(request_id, purge_id, expected_version)
        if start > pages:
            raise SourceGrantConflict('source archive purge page outside plan')
        end = min(start + limit, pages)
        for index in range(start, end):
            now = self._scope(subject, request_id, purge_id, expected_version)
            self._transact([self._auth_absent(subject),
                self._registry_check(request_id, purge_id, expected_version, now, pages),
                self._delete(request_id, f'SOURCE_GRANT_PAGE_{index:04d}')])
        return {'next_index': end, 'delete_acknowledged': end - start, 'state': 'IN_PROGRESS', 'deletion_complete': False}

    def finish(self, *, subject, request_id, purge_id, expected_version):
        now = self._scope(subject, request_id, purge_id, expected_version)
        pages = self._plan(request_id, purge_id, expected_version)
        registry = self._registry_check(request_id, purge_id, expected_version, now, pages)['ConditionCheck']
        registry['UpdateExpression'] = 'SET #version = :next, source_archive_purge_state = :purged'
        registry['ExpressionAttributeValues'].update(self._encode({':next': expected_version + 1, ':purged': 'PURGED'}))
        absent = [{'ConditionCheck': {'TableName': self.store.progress_table,
            'Key': self._key(request_id, f'SOURCE_GRANT_PAGE_{index:04d}'),
            'ConditionExpression': 'attribute_not_exists(pk)', 'ReturnValuesOnConditionCheckFailure': 'NONE'}}
            for index in range(pages)]
        # All planned pages must be absent in this same commit. The manifest is
        # removed last, together with the source-domain receipt, never by TTL.
        self._transact([self._auth_absent(subject), *absent, self._delete(request_id, HEAD), {'Update': registry}])
        return {'state': 'PURGED', 'expected_version': expected_version + 1,
                'planned_primary_archive_purged': True, 'deletion_complete': False}

    def _retired_action(self, request_id, purge_id, version, action):
        self._operation(purge_id, version)
        results = []
        def use(subject, head):
            results.append(action(subject, bytes(head)))
        self.inventory.use_retired_scope(request_id=request_id, action=use)
        if len(results) != 1 or not isinstance(results[0], dict):
            raise SourceGrantConflict('retired source archive cleanup unavailable')
        return results[0]

    def begin_retired(self, *, request_id, purge_id, expected_version):
        return self._retired_action(request_id, purge_id, expected_version,
            lambda subject, raw: self._begin_manifest(subject, request_id, purge_id, expected_version, raw))

    def delete_retired_page(self, *, request_id, purge_id, expected_version, start=0, limit=20):
        return self._retired_action(request_id, purge_id, expected_version,
            lambda subject, _: self.delete_page(subject=subject, request_id=request_id,
                purge_id=purge_id, expected_version=expected_version, start=start, limit=limit))

    def finish_retired(self, *, request_id, purge_id, expected_version):
        return self._retired_action(request_id, purge_id, expected_version,
            lambda subject, _: self.finish(subject=subject, request_id=request_id,
                                          purge_id=purge_id, expected_version=expected_version))

    def recover_retired_receipt(self, *, request_id, purge_id, expected_version):
        """Read the owned terminal receipt even after its encrypted head is gone."""
        self._operation(purge_id, expected_version, read_only=True)
        try:
            if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('explicit source archive receipt required') from None
        pages = self._plan(request_id, purge_id, expected_version, state='PURGED')
        return {'state': 'PURGED', 'pages': pages, 'expected_version': expected_version,
                'planned_primary_archive_purged': True, 'current_residual_verified': False,
                'deletion_complete': False}
