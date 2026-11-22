"""Synthetic withdrawal transitions on the existing development auth table.

Uses explicit logical server times, not a seven-day wall-clock wait. Does not
authenticate customers, change Cognito, release an email, or erase user data.
"""
import json
import time
from uuid import uuid4

import boto3

from foodtech_business_api.account_withdrawal import (
    GRACE_SECONDS, DynamoWithdrawalStore, WithdrawalConflict,
)


def main():
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    table = session.resource('dynamodb').Table('foodtech-auth-sessions-dev')
    subject, fresh_subject = str(uuid4()), str(uuid4())
    request, next_request = str(uuid4()), str(uuid4())
    created = []
    now = int(time.time())
    def check_conflict(call):
        try:
            call()
        except WithdrawalConflict:
            return
        raise AssertionError('expected conditional rejection')
    def transition(action, request_id=request, at=now):
        return DynamoWithdrawalStore(table=table).transition(
            subject=subject, request_id=request_id, now=at, action=action)
    try:
        for owner in (subject, fresh_subject):
            key = {'pk': f'USER#{owner}'}
            table.put_item(Item={**key, 'epoch': 0, 'synthetic_withdrawal_test': True},
                           ConditionExpression='attribute_not_exists(pk)')
            created.append(key)
        pending = transition('request')
        assert pending['suspended'] is True and pending['epoch'] == 1
        assert transition('request', at=now + 1)['withdrawal_deadline'] == now + GRACE_SECONDS
        check_conflict(lambda: transition('expire', at=now + GRACE_SECONDS - 1))
        canceled = transition('cancel', at=now + GRACE_SECONDS - 1)
        assert canceled['suspended'] is False and canceled['epoch'] == 2
        check_conflict(lambda: transition('expire', at=now + GRACE_SECONDS))
        pending = transition('request', next_request, now + GRACE_SECONDS)
        deadline = int(pending['withdrawal_deadline'])
        check_conflict(lambda: transition('cancel', next_request, deadline))
        deleting = transition('expire', next_request, deadline)
        assert deleting['withdrawal_status'] == 'DELETING' and deleting['suspended'] is True
        assert table.get_item(Key={'pk': f'USER#{fresh_subject}'}, ConsistentRead=True)['Item']['epoch'] == 0

        # A suspension committed after the strong read must beat cancellation.
        foreign = session.resource('dynamodb').Table(table.name)
        foreign_store = DynamoWithdrawalStore(table=foreign)
        fresh_request = str(uuid4())
        foreign_store.transition(subject=fresh_subject, request_id=fresh_request, now=now, action='request')
        original = foreign.get_item
        # The administrative update remains scoped to the generated test row.
        def raced_read(**kwargs):
            observed = original(**kwargs)
            table.update_item(Key={'pk': f'USER#{fresh_subject}'},
                UpdateExpression='ADD #epoch :one', ExpressionAttributeNames={'#epoch': 'epoch'},
                ExpressionAttributeValues={':one': 1, ':yes': True},
                ConditionExpression='synthetic_withdrawal_test = :yes')
            return observed
        foreign.get_item = raced_read
        check_conflict(lambda: foreign_store.transition(subject=fresh_subject, request_id=fresh_request,
                                                        now=now + 1, action='cancel'))
        actual = table.get_item(Key={'pk': f'USER#{fresh_subject}'}, ConsistentRead=True)['Item']
        assert actual['suspended'] is True and actual['withdrawal_status'] == 'PENDING'
        print(json.dumps({'withdrawal_grace_boundary': 'passed', 'cancellation': 'passed',
                          'stale_epoch_rejected': 'passed', 'separate_account_preserved': 'passed'}))
    finally:
        for key in created:
            table.delete_item(Key=key, ConditionExpression='synthetic_withdrawal_test = :yes',
                              ExpressionAttributeValues={':yes': True})
        if created:
            assert all('Item' not in table.get_item(Key=key, ConsistentRead=True) for key in created)
            print('Synthetic account rows removed and absence verified')


if __name__ == '__main__':
    main()
