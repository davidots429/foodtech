"""Synthetic strict-principal tier transactions on existing development tables.

The cooking table is used only for this run's random TENANT fixture keys. No
real tier, Cognito, feature flag deployment or production role is changed.
"""
import os
from uuid import uuid4

import boto3

from foodtech_business_api.tier_store import DynamoTierStore, TierConflict


def main():
    os.environ.update(AWS_PROFILE='foodtech', FOODTECH_AUTH_TABLE='foodtech-auth-sessions-dev',
                      FOODTECH_PRINCIPAL_PROVISIONING_ENABLED='true')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    auth = session.resource('dynamodb').Table('foodtech-auth-sessions-dev')
    store = DynamoTierStore('foodtech-cooking-jobs-dev')
    old, new, admin = (str(uuid4()) for _ in range(3))
    old_app, new_app = str(uuid4()), str(uuid4())
    auth_keys = [{'pk': 'USER#' + subject} for subject in (old, new, admin)]
    keys = [{'pk': 'TENANT#' + subject, 'sk': sk} for subject, app in ((old, old_app), (new, new_app))
            for sk in ('CURRENT', 'APPLICATION#' + app, 'REVIEW#' + app)]
    if any('Item' in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys) or \
            any('Item' in store.table.get_item(Key=key, ConsistentRead=True) for key in keys):
        raise RuntimeError('generated test identity already exists')
    def application(subject, app):
        return {'subject': subject, 'application_id': app, 'submitted_at': '2026-11-22T00:00:00Z',
                'status': 'REQUESTED', 'tier': 'FREE', 'version': 1, 'synthetic_test': True}
    def must_conflict(action):
        try:
            action()
        except TierConflict:
            return
        raise AssertionError('missing principal must block tier mutation')
    try:
        item = application(old, old_app)
        must_conflict(lambda: store.submit(old, item, 0))
        assert 'Item' not in store.table.get_item(Key={'pk': 'TENANT#' + old, 'sk': 'CURRENT'}, ConsistentRead=True)
        auth.put_item(Item={'pk': 'USER#' + old, 'epoch': 0, 'suspended': False},
                      ConditionExpression='attribute_not_exists(pk)')
        store.submit(old, item, 0)
        must_conflict(lambda: store.decide(old, old_app, admin, 'REJECTED', '2026-11-22T00:00:01Z', 0))
        auth.put_item(Item={'pk': 'USER#' + admin, 'epoch': 0, 'suspended': False},
                      ConditionExpression='attribute_not_exists(pk)')
        original = store.client.transact_write_items
        def lose_principal_after_current_read(**kwargs):
            store.client.transact_write_items = original
            auth.delete_item(Key={'pk': 'USER#' + old})
            return original(**kwargs)
        store.client.transact_write_items = lose_principal_after_current_read
        must_conflict(lambda: store.decide(old, old_app, admin, 'REJECTED', '2026-11-22T00:00:01Z', 0))
        assert store.current(old)['status'] == 'REQUESTED' and store.current(old)['version'] == 1
        assert 'Item' not in auth.get_item(Key={'pk': 'USER#' + old}, ConsistentRead=True)
        assert 'Item' not in store.table.get_item(Key={'pk': 'TENANT#' + old, 'sk': 'REVIEW#' + old_app}, ConsistentRead=True)
        auth.put_item(Item={'pk': 'USER#' + new, 'epoch': 0, 'suspended': False},
                      ConditionExpression='attribute_not_exists(pk)')
        store.submit(new, application(new, new_app), 0)
        result = store.decide(new, new_app, admin, 'APPROVED_FOR_DEMO', '2026-11-22T00:00:02Z', 0)
        assert result['status'] == 'APPROVED_FOR_DEMO' and result['version'] == 2
        assert store.current(old)['status'] == 'REQUESTED'
        print('Missing owner/reviewer and post-read owner loss rejected; initialized independent customer succeeded')
    finally:
        for key in keys:
            store.table.delete_item(Key=key)
        for key in auth_keys:
            auth.delete_item(Key=key)
        assert all('Item' not in store.table.get_item(Key=key, ConsistentRead=True) for key in keys)
        assert all('Item' not in auth.get_item(Key=key, ConsistentRead=True) for key in auth_keys)
        print('All generated tier fixture and principal items removed; absence verified')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Tier principal check failed:', type(error).__name__)
        raise SystemExit(1) from None
