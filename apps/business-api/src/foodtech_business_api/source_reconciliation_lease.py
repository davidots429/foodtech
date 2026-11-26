"""Versioned live Registry hold for the isolated source-grant restore worker.

Trusted global-coordinator injection only; not a public restore API or proof of
completion. It never creates a tombstone, fabricates retention deadlines, marks
whole-account deletion complete, or releases a hold as a successful restore.
"""
import re
from decimal import Decimal
from uuid import UUID

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from .source_grant_contract import SourceGrantConflict
from .subject_identity import valid_subject

MAX_HOLD_SECONDS = 600


class SourceReconciliationLeaseStore:
    def __init__(self, *, registry_table, restored_catalog_arn, client, clock):
        if not isinstance(registry_table, str) or not re.fullmatch(r'foodtech-deletion-registry-[a-z0-9-]{1,32}', registry_table):
            raise ValueError('explicit live Registry table required')
        match = re.fullmatch(r'arn:aws:dynamodb:ap-southeast-2:[0-9]{12}:table/foodtech-source-access-restore-([a-f0-9-]{36})',
                             restored_catalog_arn if isinstance(restored_catalog_arn, str) else '')
        if not match or str(UUID(match[1])) != match[1]:
            raise ValueError('explicit isolated restore table ARN required')
        self.registry_table, self.restored_arn = registry_table, restored_catalog_arn
        self.client, self.clock, self.serializer = client, clock, TypeSerializer()

    def _encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    @staticmethod
    def _identity(request_id, reconciliation_id, expected_version):
        try:
            if not isinstance(request_id, str) or str(UUID(request_id)) != request_id or \
                    not isinstance(reconciliation_id, str) or str(UUID(reconciliation_id)) != reconciliation_id or \
                    type(expected_version) is not int or not 1 <= expected_version < 2**63:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('explicit reconciliation identity and version required') from None

    def _scope(self, request_id, reconciliation_id, expected_version, deadline):
        self._identity(request_id, reconciliation_id, expected_version)
        now = self.clock()
        if expected_version >= 2**63 - 1 or type(now) is not int or not 0 < now < 2**63 or type(deadline) is not int or \
                not now < deadline < 2**63 or deadline - now > MAX_HOLD_SECONDS:
            raise ValueError('explicit bounded reconciliation lease required')
        return now

    def _write(self, *, request_id, expression, condition, names, values):
        try:
            self.client.update_item(TableName=self.registry_table,
                Key=self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}),
                UpdateExpression=expression, ConditionExpression=condition,
                ExpressionAttributeNames=names, ExpressionAttributeValues=self._encode(values),
                ReturnValues='NONE', ReturnValuesOnConditionCheckFailure='NONE')
        except self.client.exceptions.ConditionalCheckFailedException:
            raise SourceGrantConflict('reconciliation Registry state changed') from None

    def acquire(self, *, request_id, reconciliation_id, expected_version, hold_until):
        """Hold an existing completed/failed request still inside its retention window.

        A failed request must first have been explicitly transitioned by the
        global coordinator. An expired/still-active hold cannot be stolen here.
        None retention fields or an absent tombstone cannot satisfy the guard.
        """
        now = self._scope(request_id, reconciliation_id, expected_version, hold_until)
        self._write(request_id=request_id,
            expression='SET #status = :active, #version = :next, reconciliation_id = :restore, '
                       'reconciliation_hold_until = :until, source_grant_restore_table_arn = :table',
            condition='request_id = :request AND #version = :expected AND '
                '(#status = :completed OR #status = :failed) AND attribute_type(reconcile_until, :number) '
                'AND attribute_type(expires_at, :number) AND reconcile_until > :now AND expires_at > :now '
                'AND (attribute_not_exists(reconciliation_hold_until) OR reconciliation_hold_until <= :now)',
            names={'#status': 'status', '#version': 'version'},
            values={':active': 'RECONCILING', ':next': expected_version + 1, ':restore': reconciliation_id,
                ':until': hold_until, ':table': self.restored_arn, ':request': request_id, ':expected': expected_version,
                ':completed': 'COMPLETED', ':failed': 'RECONCILE_FAILED', ':now': now, ':number': 'N'})
        return {'request_id': request_id, 'reconciliation_id': reconciliation_id,
                'expected_version': expected_version + 1, 'hold_until': hold_until}

    def recover(self, *, request_id, reconciliation_id, expected_version):
        """Recover an exact owned active lease after an uncertain write response.

        Persist request/operation IDs and the intended next version before the
        original write. Recovery does not update, extend, or steal a hold.
        """
        now = self.clock()
        if type(now) is not int or not 0 < now < 2**63 - 1:
            raise ValueError('valid reconciliation clock required')
        self._identity(request_id, reconciliation_id, expected_version)
        response = self.client.get_item(TableName=self.registry_table,
            Key=self._encode({'pk': 'REGISTRY', 'sk': 'REQ#' + request_id}), ConsistentRead=True,
            ProjectionExpression='pk, sk, request_id, #status, #version, reconciliation_id, '
                                 'reconciliation_hold_until, source_grant_restore_table_arn',
            ExpressionAttributeNames={'#status': 'status', '#version': 'version'})
        try:
            decoder = TypeDeserializer()
            row = {key: decoder.deserialize(value) for key, value in response['Item'].items()}
            hold = row['reconciliation_hold_until']
            current = self.clock()
            if row.get('pk') != 'REGISTRY' or row.get('sk') != 'REQ#' + request_id or \
                    row.get('request_id') != request_id or row.get('status') != 'RECONCILING' or \
                    type(row.get('version')) is bool or row.get('version') != expected_version or \
                    row.get('reconciliation_id') != reconciliation_id or \
                    row.get('source_grant_restore_table_arn') != self.restored_arn or \
                    isinstance(hold, bool) or not isinstance(hold, (int, Decimal)) or \
                    (isinstance(hold, Decimal) and not hold.is_finite()) or hold != int(hold) or \
                    not 0 < hold < 2**63 or type(current) is not int or not now <= current < hold:
                raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError):
            raise SourceGrantConflict('owned active reconciliation lease unavailable') from None
        return {'request_id': request_id, 'reconciliation_id': reconciliation_id,
                'expected_version': expected_version, 'hold_until': int(hold)}

    def renew(self, *, request_id, reconciliation_id, expected_version, previous_hold_until, hold_until):
        """Extend only the caller's still-active hold; never resurrect an expired lease."""
        now = self._scope(request_id, reconciliation_id, expected_version, hold_until)
        if type(previous_hold_until) is not int or not now < previous_hold_until < hold_until:
            raise ValueError('renewal must extend an active reconciliation hold')
        self._write(request_id=request_id,
            expression='SET #version = :next, reconciliation_hold_until = :until',
            condition='request_id = :request AND #status = :active AND #version = :expected '
                'AND reconciliation_id = :restore AND source_grant_restore_table_arn = :table '
                'AND reconciliation_hold_until = :previous AND reconciliation_hold_until > :now',
            names={'#status': 'status', '#version': 'version'},
            values={':request': request_id, ':active': 'RECONCILING', ':expected': expected_version,
                ':next': expected_version + 1, ':restore': reconciliation_id, ':table': self.restored_arn,
                ':previous': previous_hold_until, ':until': hold_until, ':now': now})
        return {'request_id': request_id, 'reconciliation_id': reconciliation_id,
                'expected_version': expected_version + 1, 'hold_until': hold_until}


class SourceGrantRestoreSession:
    """Connect the global coordinator's hold to bounded source reapplication.

    Failures leave the hold intact for authorized recovery. No success release,
    retention update, account completion or restored-environment promotion is
    available from this source-domain session.
    """
    def __init__(self, *, leases, reconciliation):
        if leases.client is not reconciliation.client or leases.registry_table != reconciliation.inventory.store.registry_table or \
                leases.restored_arn != reconciliation.restored_arn:
            raise ValueError('restore lease and source worker boundaries must match')
        self.leases, self.reconciliation = leases, reconciliation

    def start(self, *, subject, request_id, reconciliation_id, expected_version, hold_until):
        if not valid_subject(subject):
            raise ValueError('explicit source restoration subject required')
        lease = self.leases.acquire(request_id=request_id, reconciliation_id=reconciliation_id,
                                    expected_version=expected_version, hold_until=hold_until)
        progress = self.reconciliation.delete_page(subject=subject, request_id=request_id,
            reconciliation_id=reconciliation_id, expected_version=lease['expected_version'])
        return {'lease': lease, 'source_progress': progress, 'restore_promotion_allowed': False}

    def recover(self, *, subject, request_id, reconciliation_id, expected_version, start):
        lease = self.leases.recover(request_id=request_id, reconciliation_id=reconciliation_id,
                                    expected_version=expected_version)
        progress = self.resume(subject=subject, request_id=request_id, reconciliation_id=reconciliation_id,
                               expected_version=lease['expected_version'], start=start)
        return {'lease': lease, 'source_progress': progress, 'restore_promotion_allowed': False}

    def resume(self, *, subject, request_id, reconciliation_id, expected_version, start):
        return self.reconciliation.delete_page(subject=subject, request_id=request_id,
            reconciliation_id=reconciliation_id, expected_version=expected_version, start=start)

    def renew(self, *, request_id, reconciliation_id, expected_version, previous_hold_until, hold_until):
        return self.leases.renew(request_id=request_id, reconciliation_id=reconciliation_id,
            expected_version=expected_version, previous_hold_until=previous_hold_until, hold_until=hold_until)

    def observe(self, *, subject, request_id, reconciliation_id, expected_version):
        return self.reconciliation.observe_empty(subject=subject, request_id=request_id,
            reconciliation_id=reconciliation_id, expected_version=expected_version)
