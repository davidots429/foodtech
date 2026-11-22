"""Fence account enrollment against concurrent final account retirement.

Read the fence BEFORE fresh Cognito verification, then condition the initial
Put on that exact generation. Retirement advances it atomically with removal
of the old subject. The control item has no user identifiers and no TTL.

Bootstrap is an explicit maintenance operation before enabling enrollment.
Never recreate a lost production control item automatically: old generations
must not be reused. Every principal-removal writer must use the retirement
contract; deployment permissions and rollout must enforce this separately.
"""
from uuid import UUID

from .account_withdrawal import WithdrawalConflict, _integer

FENCE_KEY = 'CONTROL#PRINCIPAL_ENROLLMENT'


class PrincipalEnrollmentConflict(Exception):
    pass


def _subject(subject):
    try:
        if str(UUID(subject)) != subject:
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
    residuals before publishing COMPLETED. This primitive cannot establish them.
    No Cognito deletion is performed here, and no public endpoint is provided.
    """
    def __init__(self, *, auth_table, registry_table, cognito_client, pool_id, fence_key=FENCE_KEY):
        if not registry_table or registry_table == auth_table.name or not pool_id or cognito_client is None:
            raise ValueError('separate registry and explicit Cognito boundary are required')
        self.enrollment = DynamoPrincipalEnrollment(table=auth_table, fence_key=fence_key)
        self.auth_table, self.registry_table = auth_table, registry_table
        self.cognito, self.pool_id = cognito_client, pool_id

    def retire(self, *, subject, request_id, expected_epoch):
        _subject(subject)
        _subject(request_id)
        expected_epoch = _integer(expected_epoch, 1)
        generation = self.enrollment.fence()
        try:
            self.cognito.admin_get_user(UserPoolId=self.pool_id, Username=subject)
        except self.cognito.exceptions.UserNotFoundException:
            pass
        else:
            raise PrincipalEnrollmentConflict('Cognito identity has not been removed')
        try:
            self.auth_table.meta.client.transact_write_items(TransactItems=[
                {'Update': {'TableName': self.auth_table.name, 'Key': {'pk': self.enrollment.fence_key},
                    'UpdateExpression': 'SET #generation = :next',
                    'ConditionExpression': '#generation = :observed AND #state = :open AND attribute_not_exists(expires)',
                    'ExpressionAttributeNames': {'#generation': 'generation', '#state': 'state'},
                    'ExpressionAttributeValues': {':observed': generation, ':next': generation + 1, ':open': 'OPEN'}}},
                {'Delete': {'TableName': self.auth_table.name, 'Key': {'pk': f'USER#{subject}'},
                    'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes AND #epoch = :epoch',
                    'ExpressionAttributeNames': {'#epoch': 'epoch'},
                    'ExpressionAttributeValues': {':request': request_id, ':yes': True, ':epoch': expected_epoch}}},
                {'ConditionCheck': {'TableName': self.registry_table,
                    'Key': {'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'},
                    'ConditionExpression': '#status = :completed AND request_id = :request',
                    'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': {':completed': 'COMPLETED', ':request': request_id}}},
            ])
        except self.auth_table.meta.client.exceptions.TransactionCanceledException:
            raise PrincipalEnrollmentConflict('account retirement prerequisites changed') from None
