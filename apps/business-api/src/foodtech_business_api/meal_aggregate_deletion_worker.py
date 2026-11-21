"""Remove one verified captured aggregate under the locked deletion request.

Caller authentication, writer fencing, group-index cleanup and residual or
backup verification are separate. This is not a deletion receipt.
"""
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from itertools import islice

from .condition_profile_deletion import ProfileDeletionConflict


class MealAggregateDeletionWorker:
    def __init__(self, *, inventory):
        self.inventory = inventory
        self.source_table, self.store = inventory.source_table, inventory.progress_store

    def delete_one(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid meal aggregate target index')
        target = next((item for number, item in enumerate(self.inventory.targets(
            subject=subject, request_id=request_id)) if number == index), None)
        if target is None:
            raise ValueError('meal aggregate target unavailable')
        self._delete_target(subject=subject, request_id=request_id, target=target)
        return {'index': index, 'state': 'VERIFYING'}

    def delete_batch(self, *, subject, request_id, start, count):
        """Verify the complete inventory once, then remove a bounded range."""
        if type(start) is not int or not 0 <= start < 10**12 or \
                type(count) is not int or not 1 <= count <= 20 or start + count >= 10**12:
            raise ValueError('invalid meal aggregate target range')
        selected = list(islice(self.inventory.targets(subject=subject, request_id=request_id), start, start + count))
        if len(selected) != count:
            raise ValueError('meal aggregate target range unavailable')
        for target in selected:
            self._delete_target(subject=subject, request_id=request_id, target=target)
        return {'count': len(selected), 'state': 'VERIFYING'}

    def _delete_target(self, *, subject, request_id, target):
        self.inventory._validate_reference(subject, [target.group_id, target.result_id])
        serializer = TypeSerializer()

        def encode(values):
            return {key: serializer.serialize(value) for key, value in values.items()}

        try:
            self.store.client.transact_write_items(TransactItems=[
                {'ConditionCheck': {'TableName': self.store.auth_table, 'Key': encode({'pk': f'USER#{subject}'}),
                    'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                    'ExpressionAttributeValues': encode({':request': request_id, ':yes': True})}},
                {'ConditionCheck': {'TableName': self.store.registry_table,
                    'Key': encode({'pk': 'REGISTRY', 'sk': f'REQ#{request_id}'}),
                    'ConditionExpression': '#status = :active', 'ExpressionAttributeNames': {'#status': 'status'},
                    'ExpressionAttributeValues': encode({':active': 'DELETING'})}},
                {'Delete': {'TableName': self.source_table.name,
                    'Key': encode({'pk': f'SCOPE#USER#{subject}#MEAL_AGGREGATE#{target.group_id}',
                                   'sk': f'RESULT#{target.result_id}'}),
                    'ConditionExpression': 'attribute_not_exists(pk) OR (group_id = :group AND result_id = :result)',
                    'ExpressionAttributeValues': encode({':group': target.group_id, ':result': target.result_id})}},
            ])
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') == 'TransactionCanceledException' and any(
                    reason.get('Code') == 'ConditionalCheckFailed' for reason in (exc.response.get('CancellationReasons') or [])):
                raise ProfileDeletionConflict from exc
            raise
