"""Atomic owner fence shared by live decisions and late-source restatements.

Strict provisioning rejects removed/uninitialized principals as well as locks.
This is a modern-writer contract, not evidence that legacy writers stopped.
"""
import os

from boto3.dynamodb.types import TypeSerializer


def principal_write_guard(auth_table, owner):
    if not isinstance(auth_table, str) or not auth_table or not isinstance(owner, str) or not owner:
        raise ValueError('authentication table and job owner are required')
    values = {':false': False}
    check = {'TableName': auth_table, 'Key': {'pk': {'S': f'USER#{owner}'}},
        'ConditionExpression': '(attribute_not_exists(suspended) OR suspended = :false) '
                               'AND attribute_not_exists(deletion_lock_request)'}
    if os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED', 'false') == 'true':
        check['ConditionExpression'] += ' AND attribute_exists(pk) AND #epoch >= :zero AND #epoch < :max'
        check['ExpressionAttributeNames'] = {'#epoch': 'epoch'}
        values.update({':zero': 0, ':max': 2**63})
    serializer = TypeSerializer()
    check['ExpressionAttributeValues'] = {key: serializer.serialize(value) for key, value in values.items()}
    return {'ConditionCheck': check}
