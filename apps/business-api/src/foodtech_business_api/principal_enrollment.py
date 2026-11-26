"""Fence account enrollment against concurrent final account retirement.

Read the fence BEFORE fresh Cognito verification, then condition the initial
Put on that exact generation. Retirement advances it atomically with removal
of the old subject. The control item has no user identifiers and no TTL.

Bootstrap is an explicit maintenance operation before enabling enrollment.
Never recreate a lost production control item automatically: old generations
must not be reused. Every principal-removal writer must use the retirement
contract; deployment permissions and rollout must enforce this separately.
"""
import re
from uuid import UUID

from .subject_identity import valid_subject
from .source_grant_contract import SourceGrantConflict, _grant_mapping

from .account_withdrawal import WithdrawalConflict, _integer

FENCE_KEY = 'CONTROL#PRINCIPAL_ENROLLMENT'


class PrincipalEnrollmentConflict(Exception):
    pass


def _subject(subject):
    try:
        if not valid_subject(subject):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid account identity') from None


class DynamoPrincipalEnrollment:
    def __init__(self, *, table, fence_key=FENCE_KEY):
        if not isinstance(fence_key, str) or not fence_key.startswith(FENCE_KEY) or len(fence_key) > 256:
            raise ValueError('invalid enrollment control key')
        self.table, self.fence_key = table, fence_key

    def fence(self):
        item = self.table.get_item(Key={'pk': self.fence_key}, ConsistentRead=True).get('Item')
        try:
            if not item or item.get('state') != 'OPEN' or 'expires' in item:
                raise ValueError
            return _integer(item.get('generation'))
        except (ValueError, TypeError, ArithmeticError, WithdrawalConflict):
            raise PrincipalEnrollmentConflict('enrollment control is unavailable') from None

    def bootstrap(self):
        """Explicit trusted maintenance only, never called from login/startup."""
        try:
            self.table.put_item(Item={'pk': self.fence_key, 'generation': 0, 'state': 'OPEN'},
                                ConditionExpression='attribute_not_exists(pk)')
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            # Never reset the existing generation or reopen a disabled gate.
            pass
        return self.fence()

    def enroll(self, *, subject, expected_generation):
        """Caller must just have rechecked Cognito AFTER reading the fence."""
        _subject(subject)
        expected_generation = _integer(expected_generation)
        record = {'pk': f'USER#{subject}', 'epoch': 0, 'suspended': False}
        try:
            self.table.meta.client.transact_write_items(TransactItems=[
                {'ConditionCheck': {'TableName': self.table.name, 'Key': {'pk': self.fence_key},
                    'ConditionExpression': '#generation = :observed AND #state = :open AND attribute_not_exists(expires)',
                    'ExpressionAttributeNames': {'#generation': 'generation', '#state': 'state'},
                    'ExpressionAttributeValues': {':observed': expected_generation, ':open': 'OPEN'}}},
                {'Put': {'TableName': self.table.name, 'Item': record,
                         'ConditionExpression': 'attribute_not_exists(pk)'}},
            ])
        except self.table.meta.client.exceptions.TransactionCanceledException:
            raise PrincipalEnrollmentConflict('account or enrollment generation changed') from None
        return self.table.get_item(Key={'pk': record['pk']}, ConsistentRead=True).get('Item')


class DynamoPrincipalRetirement:
    """Internal final removal after registry completion and Cognito deletion.

    A trusted deletion worker must verify writer quiescence and all storage
    residuals, and retain an encrypted source-reference recovery inventory for
    backup reconciliation, before publishing COMPLETED. This primitive cannot
    establish those global prerequisites or create that recovery inventory.
    No Cognito deletion is performed here, and no public endpoint is provided.
    """
    def __init__(self, *, auth_table, registry_table, cognito_client, pool_id, fence_key=FENCE_KEY,
                 source_catalog_table=None):
        if not registry_table or registry_table == auth_table.name or not pool_id or cognito_client is None:
            raise ValueError('separate registry and explicit Cognito boundary are required')
        self.enrollment = DynamoPrincipalEnrollment(table=auth_table, fence_key=fence_key)
        self.auth_table, self.registry_table = auth_table, registry_table
        self.cognito, self.pool_id = cognito_client, pool_id
        if source_catalog_table is not None:
            match = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth_table.name)
            if not match or registry_table != 'foodtech-deletion-registry-' + match[1] or \
                    source_catalog_table != 'foodtech-source-access-' + match[1]:
                raise ValueError('matching-stage source catalog retirement boundary required')
        self.source_catalog_table = source_catalog_table

    def _source_reference_condition(self, subject, request_id, expected_epoch):
        # This is the privileged final-auth boundary, not the catalog cleanup
        # role. References survive until this independent residual check.
        client = self.auth_table.meta.client
        response = client.transact_get_items(TransactItems=[
            {'Get': {'TableName': self.auth_table.name, 'Key': {'pk': 'USER#' + subject},
                'ProjectionExpression': 'pk, #epoch, suspended, deletion_lock_request, source_read_grants',
                'ExpressionAttributeNames': {'#epoch': 'epoch'}}},
            {'Get': {'TableName': self.registry_table, 'Key': {'pk': 'REGISTRY', 'sk': 'REQ#' + request_id},
                'ProjectionExpression': 'pk, sk, request_id, #status',
                'ExpressionAttributeNames': {'#status': 'status'}}},
        ])
        try:
            rows = response['Responses']
            if not isinstance(rows, list) or len(rows) != 2:
                raise ValueError
            account, registry = (row['Item'] for row in rows)
            if account.get('pk') != 'USER#' + subject or account.get('suspended') is not True or \
                    _integer(account.get('epoch'), 1) != expected_epoch or account.get('deletion_lock_request') != request_id or \
                    registry.get('pk') != 'REGISTRY' or registry.get('sk') != 'REQ#' + request_id or \
                    registry.get('request_id') != request_id or registry.get('status') != 'COMPLETED':
                raise ValueError
            grants = _grant_mapping(account)
        except (KeyError, TypeError, AttributeError, ValueError, WithdrawalConflict, SourceGrantConflict):
            raise PrincipalEnrollmentConflict('retirement source reference snapshot unavailable') from None
        if grants and self.source_catalog_table is None:
            raise PrincipalEnrollmentConflict('independent source catalog verification required')
        references = sorted(grants.values())
        for start in range(0, len(references), 100):
            batch = references[start:start + 100]
            result = client.transact_get_items(TransactItems=[{'Get': {
                'TableName': self.source_catalog_table, 'Key': {'pk': 'GRANT#' + grant, 'sk': 'META'},
                'ProjectionExpression': 'pk, sk'}} for grant in batch])
            found = result.get('Responses')
            if not isinstance(found, list) or len(found) != len(batch) or \
                    any(not isinstance(item, dict) or 'Item' in item for item in found):
                raise PrincipalEnrollmentConflict('source catalog residual or unavailable observation')
        # Account suspension/lock prevents legitimate registrar writes. The
        # deployment's writer/quiescence contract and independent global checks
        # remain required before Registry COMPLETED; reads do not prove those.
        return (('#grants = :observed_grants', {':observed_grants': grants})
                if 'source_read_grants' in account else ('attribute_not_exists(#grants)', {}))

    def retire(self, *, subject, request_id, expected_epoch):
        _subject(subject)
        try:
            if str(UUID(request_id)) != request_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid deletion request identity') from None
        expected_epoch = _integer(expected_epoch, 1)
        generation = self.enrollment.fence()
        try:
            self.cognito.admin_get_user(UserPoolId=self.pool_id, Username=subject)
        except self.cognito.exceptions.UserNotFoundException:
            pass
        else:
            raise PrincipalEnrollmentConflict('Cognito identity has not been removed')
        grant_condition, grant_values = self._source_reference_condition(subject, request_id, expected_epoch)
        try:
            self.auth_table.meta.client.transact_write_items(TransactItems=[
                {'Update': {'TableName': self.auth_table.name, 'Key': {'pk': self.enrollment.fence_key},
                    'UpdateExpression': 'SET #generation = :next',
                    'ConditionExpression': '#generation = :observed AND #state = :open AND attribute_not_exists(expires)',
                    'ExpressionAttributeNames': {'#generation': 'generation', '#state': 'state'},
                    'ExpressionAttributeValues': {':observed': generation, ':next': generation + 1, ':open': 'OPEN'}}},
                {'Delete': {'TableName': self.auth_table.name, 'Key': {'pk': f'USER#{subject}'},
                    'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes AND #epoch = :epoch '
                        'AND ' + grant_condition,
                    'ExpressionAttributeNames': {'#epoch': 'epoch', '#grants': 'source_read_grants'},
                    'ExpressionAttributeValues': {':request': request_id, ':yes': True, ':epoch': expected_epoch, **grant_values}}},
                {'ConditionCheck': {'TableName': self.registry_table,
                    'Key': {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'},
                    'ConditionExpression': '#status = :completed AND request_id = :request',
                    'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': {':completed': 'COMPLETED', ':request': request_id}}},
            ])
        except self.auth_table.meta.client.exceptions.TransactionCanceledException:
            raise PrincipalEnrollmentConflict('account retirement prerequisites changed') from None
