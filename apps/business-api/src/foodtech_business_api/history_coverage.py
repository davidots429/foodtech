"""Privileged internal coverage registration, disabled until writer controls exist.

An actual independently verified ENFORCED writer record is a prerequisite, not
something this registrar issues or infers from a passing scan. Ordinary writers
must not alter it or USER.history_coverage; validate effective IAM separately.
No route, CLI, default client, role provisioning or live activation is provided.
"""
import re
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from foodtech_contracts.prior_history_witness import HISTORY_CONTRACT
from .prior_history_authority import _uuid
from .source_read_authority import _integer
from .subject_identity import valid_subject
from .roster_audit import audit_snapshot


class HistoryCoverageUnavailable(Exception):
    pass


class DynamoHistoryCoverageRegistrar:
    def __init__(self, *, auth_table, cooking_table, account_id, client, store, clock):
        stage = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table or '')
        if not stage or cooking_table != 'foodtech-cooking-jobs-' + stage[1] or \
                not isinstance(account_id, str) or not re.fullmatch('[0-9]{12}', account_id) or client is None or \
                store.table.name != cooking_table:
            raise ValueError('explicit same-stage privileged history registrar required')
        self.auth, self.cooking, self.client, self.store, self.clock = auth_table, cooking_table, client, store, clock
        self.cooking_arn = f'arn:aws:dynamodb:ap-southeast-2:{account_id}:table/{cooking_table}'
        self.serializer, self.decoder = TypeSerializer(), TypeDeserializer()

    def _encode(self, row):
        return {key: self.serializer.serialize(value) for key, value in row.items()}

    def _controls(self, owner, epoch):
        response = self.client.transact_get_items(TransactItems=[{'Get': {
            'TableName': self.auth, 'Key': self._encode({'pk': key})}}
            for key in ('USER#' + owner, 'CONTROL#COOKING_HISTORY_WRITERS')])
        rows = response['Responses']
        if len(rows) != 2:
            raise ValueError('missing control records')
        user, writer = ({key: self.decoder.deserialize(value) for key, value in row['Item'].items()} for row in rows)
        now = self.clock()
        if type(now) is not int or not 0 < now < 2**63 or user.get('pk') != 'USER#' + owner or \
                _integer(user.get('epoch')) != epoch or user.get('suspended', False) is not False or \
                'deletion_lock_request' in user or writer.get('pk') != 'CONTROL#COOKING_HISTORY_WRITERS' or \
                writer.get('state') != 'ENFORCED' or _integer(writer.get('version'), 1) is None or writer['version'] > 10**18 or \
                not _uuid(writer.get('generation')) or writer.get('writer_contract') != HISTORY_CONTRACT or \
                writer.get('cooking_table_arn') != self.cooking_arn or \
                not isinstance(writer.get('manifest_sha256'), str) or not re.fullmatch('[a-f0-9]{64}', writer['manifest_sha256']) or \
                _integer(writer.get('verified_at'), 1) is None or _integer(writer.get('valid_until'), 1) is None or \
                not int(writer['verified_at']) <= now < int(writer['valid_until']):
            raise ValueError('verified writer controls unavailable')
        old = user.get('history_coverage')
        if old is not None and (not isinstance(old, dict) or _integer(old.get('version'), 1) is None or old['version'] > 10**18):
            raise ValueError('invalid previous coverage')
        return user, writer

    def register(self, *, owner, auth_epoch, registration_id, max_scanned=10_000):
        """Audit internally, then CAS account coverage + writer + revision.

        A lost transaction response is not success and is never retried here.
        The trusted caller retains a fresh UUID before starting and uses recover
        after uncertainty. Never reuse a prior operation ID for a new audit.
        """
        if not valid_subject(owner) or type(auth_epoch) is not int or not 0 <= auth_epoch < 2**63 or not _uuid(registration_id):
            raise ValueError('explicit trusted account scope required')
        try:
            before = self._controls(owner, auth_epoch)
            previous = before[0].get('history_coverage')
            if previous is not None and (previous.get('registration_id') == registration_id or previous['version'] >= 10**18):
                raise ValueError('use exact receipt recovery, not registration replay')
            observed = audit_snapshot(self.store, owner, max_scanned=max_scanned)
            if observed.owner != owner or self._controls(owner, auth_epoch) != before:
                raise ValueError('controls changed during audit')
            user, writer = before
            old = user.get('history_coverage')
            coverage = {'state': 'ACTIVE', 'version': int(old['version']) + 1 if old else 1,
                'generation': writer['generation'], 'writer_contract': HISTORY_CONTRACT,
                'coverage_scope': 'ALL_RETAINED_JOBS', 'coverage_revision': observed.revision,
                'baseline_roster_sha256': observed.roster_sha256, 'proof_sha256': observed.proof_sha256,
                'writer_manifest_sha256': writer['manifest_sha256'], 'registration_id': registration_id,
                'registration_epoch': auth_epoch, 'record_count': observed.count}
            now = self.clock()
            if type(now) is not int or not int(writer['verified_at']) <= now < int(writer['valid_until']):
                raise ValueError('writer verification expired before registration')
            condition = '#epoch = :epoch AND (attribute_not_exists(suspended) OR suspended = :no) AND '
            condition += 'attribute_not_exists(deletion_lock_request) AND '
            condition += 'attribute_not_exists(history_coverage)' if old is None else 'history_coverage = :old'
            values = {':epoch': auth_epoch, ':no': False, ':coverage': coverage}
            if old is not None:
                values[':old'] = old
            self.client.transact_write_items(ClientRequestToken=str(uuid4()), TransactItems=[
                {'Update': {'TableName': self.auth, 'Key': self._encode({'pk': 'USER#' + owner}),
                    'UpdateExpression': 'SET history_coverage = :coverage', 'ConditionExpression': condition,
                    'ExpressionAttributeNames': {'#epoch': 'epoch'}, 'ExpressionAttributeValues': self._encode(values),
                    'ReturnValuesOnConditionCheckFailure': 'NONE'}},
                {'ConditionCheck': {'TableName': self.auth,
                    'Key': self._encode({'pk': 'CONTROL#COOKING_HISTORY_WRITERS'}),
                    'ConditionExpression': '#state = :enforced AND #version = :version AND generation = :generation AND '
                        'writer_contract = :contract AND cooking_table_arn = :table AND manifest_sha256 = :manifest AND '
                        'verified_at = :verified AND verified_at <= :now AND valid_until = :until AND valid_until > :now',
                    'ExpressionAttributeNames': {'#state': 'state', '#version': 'version'},
                    'ExpressionAttributeValues': self._encode({':enforced': 'ENFORCED', ':version': writer['version'],
                        ':generation': writer['generation'], ':contract': HISTORY_CONTRACT, ':table': self.cooking_arn,
                        ':manifest': writer['manifest_sha256'], ':verified': writer['verified_at'],
                        ':until': writer['valid_until'], ':now': now}), 'ReturnValuesOnConditionCheckFailure': 'NONE'}},
                {'ConditionCheck': {'TableName': self.cooking,
                    'Key': self._encode({'pk': 'SCOPE#USER#' + owner + '#JOBS', 'sk': 'HISTORY#REVISION'}),
                    'ConditionExpression': 'revision = :revision',
                    'ExpressionAttributeValues': self._encode({':revision': observed.revision}),
                    'ReturnValuesOnConditionCheckFailure': 'NONE'}},
            ])
            return {'coverage_version': coverage['version'], 'record_count': observed.count,
                    'immediately_previous_verified': False}
        except (ValueError, TypeError, KeyError, AttributeError, BotoCoreError, ClientError):
            raise HistoryCoverageUnavailable('history coverage registration unavailable') from None

    def recover(self, *, owner, auth_epoch, registration_id):
        """Read the same account/epoch/operation receipt, without writes or audit.

        This is historical registration evidence, never a fresh roster/history
        authorization. Failure to recover does not prove a write did not commit.
        A replaced, locked or revoked record is unavailable, not recreated.
        """
        if not valid_subject(owner) or type(auth_epoch) is not int or not 0 <= auth_epoch < 2**63 or not _uuid(registration_id):
            raise ValueError('explicit trusted registration receipt scope required')
        try:
            user, writer = self._controls(owner, auth_epoch)
            coverage = user.get('history_coverage')
            if not isinstance(coverage, dict) or coverage.get('state') != 'ACTIVE' or \
                    coverage.get('registration_id') != registration_id or \
                    _integer(coverage.get('registration_epoch')) != auth_epoch or \
                    coverage.get('generation') != writer['generation'] or \
                    coverage.get('writer_manifest_sha256') != writer['manifest_sha256'] or \
                    coverage.get('writer_contract') != HISTORY_CONTRACT or coverage.get('coverage_scope') != 'ALL_RETAINED_JOBS' or \
                    _integer(coverage.get('coverage_revision'), 1) is None or coverage['coverage_revision'] > 10**18 or \
                    _integer(coverage.get('record_count')) is None or coverage['record_count'] > 100 or \
                    any(not isinstance(coverage.get(key), str) or not re.fullmatch('[a-f0-9]{64}', coverage[key])
                        for key in ('baseline_roster_sha256', 'proof_sha256')):
                raise ValueError('exact registration receipt unavailable')
            return {'coverage_version': int(coverage['version']), 'record_count': int(coverage['record_count']),
                    'receipt_only': True, 'immediately_previous_verified': False}
        except (ValueError, TypeError, KeyError, AttributeError, BotoCoreError, ClientError):
            raise HistoryCoverageUnavailable('history coverage receipt unavailable') from None
