"""Opt-in strict principal fencing against isolated synthetic development keys.

Uses production cooking/worker guards in real DynamoDB transactions. This
does not prove deployed role permissions, old-writer shutdown or full erasure.
"""
import argparse
import json
import os
from uuid import uuid4

import boto3
from botocore.config import Config
from boto3.dynamodb.types import TypeSerializer

from foodtech_business_api.cooking_store import DynamoCookingStore
from foodtech_standardization_worker.process_bridge import DynamoProcessBridge
from foodtech_standardization_worker.process_restatement import DynamoProcessRestatement
from foodtech_standardization_worker.principal_write_guard import principal_write_guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-synthetic-writes', action='store_true', required=True)
    parser.parse_args()
    if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true':
        raise RuntimeError('explicit development profile and strict principal mode required')
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 2}, connect_timeout=5,
                    read_timeout=15, ignore_configured_endpoint_urls=True)
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts', config=config).get_caller_identity()['Account'] != '573693340100':
        raise RuntimeError('unexpected development project')
    resource = session.resource('dynamodb', config=config)
    auth, data = resource.Table('foodtech-auth-sessions-dev'), resource.Table('foodtech-cooking-jobs-dev')
    raw = session.client('dynamodb', config=config)
    token, serializer = uuid4().hex, TypeSerializer()
    subject = 'synthetic-principal-fence-' + token
    key, target = {'pk': 'USER#' + subject}, {'pk': 'SYNTHETIC#PRINCIPAL_FENCE#' + token, 'sk': 'CHECK'}
    store = DynamoCookingStore(data.name, auth.name)
    assert store.require_principal is True
    bridges = []
    for cls in (DynamoProcessBridge, DynamoProcessRestatement):
        # Exercise the production guard without touching live stream bindings.
        bridge = cls.__new__(cls)
        bridge.auth_table_name, bridge.serializer = auth.name, serializer
        bridges.append(bridge)
    job = {'owner_subject': subject, 'job_id': 'synthetic-job'}
    job_pk = 'SCOPE#USER#' + subject + '#JOB#synthetic-job'
    guards = [store._owner_guard(subject, 0), store._derived_owner_guard(subject, None),
              *(bridge._owner_guard(job_pk, job) for bridge in bridges),
              principal_write_guard(auth.name, subject)]
    item = {**target, 'fixture_run': token}
    put = {'Put': {'TableName': data.name,
                  'Item': {name: serializer.serialize(value) for name, value in item.items()},
                  'ConditionExpression': 'attribute_not_exists(pk) OR fixture_run = :run',
                  'ExpressionAttributeValues': {':run': {'S': token}}}}
    cases = [('absent', None, False), ('missing_epoch', {}, False),
             ('string_epoch', {'epoch': '0'}, False), ('bool_epoch', {'epoch': False}, False),
             ('negative_epoch', {'epoch': -1}, False), ('overflow_epoch', {'epoch': 2**63}, False),
             ('suspended', {'epoch': 0, 'suspended': True}, False),
             ('deletion_locked', {'epoch': 0, 'deletion_lock_request': str(uuid4())}, False),
             ('initialized', {'epoch': 0, 'suspended': False}, True)]
    checked = 0
    try:
        if auth.get_item(Key=key, ConsistentRead=True).get('Item') or data.get_item(Key=target, ConsistentRead=True).get('Item'):
            raise RuntimeError('synthetic fixture key collision')
        for _, fields, allowed in cases:
            if fields is not None:
                auth.put_item(Item={**key, **fields, 'fixture_run': token},
                    ConditionExpression='attribute_not_exists(pk) OR fixture_run = :run',
                    ExpressionAttributeValues={':run': token})
            for guard in guards:
                try:
                    raw.transact_write_items(TransactItems=[guard, put])
                except raw.exceptions.TransactionCanceledException as error:
                    codes = [reason.get('Code') for reason in error.response.get('CancellationReasons', [])]
                    if allowed or codes != ['ConditionalCheckFailed', 'None']:
                        raise
                else:
                    if not allowed:
                        raise AssertionError('uninitialized or locked writer was accepted')
                checked += 1
            if not allowed and data.get_item(Key=target, ConsistentRead=True).get('Item'):
                raise AssertionError('rejected transaction left a write')
        # A stale client cannot write after account epoch advancement, while
        # fresh requests and background derivations retain the live account.
        auth.put_item(Item={**key, 'epoch': 1, 'fixture_run': token},
            ConditionExpression='fixture_run = :run', ExpressionAttributeValues={':run': token})
        try:
            raw.transact_write_items(TransactItems=[store._owner_guard(subject, 0), put])
        except raw.exceptions.TransactionCanceledException as error:
            assert [r.get('Code') for r in error.response['CancellationReasons']] == ['ConditionalCheckFailed', 'None']
        else:
            raise AssertionError('stale session epoch was accepted')
        raw.transact_write_items(TransactItems=[store._owner_guard(subject, 1), put])
        # Removal after an earlier valid observation must not recreate data.
        auth.delete_item(Key=key, ConditionExpression='fixture_run = :run', ExpressionAttributeValues={':run': token})
        for guard in guards:
            try:
                raw.transact_write_items(TransactItems=[guard, put])
            except raw.exceptions.TransactionCanceledException as error:
                assert [r.get('Code') for r in error.response['CancellationReasons']] == ['ConditionalCheckFailed', 'None']
            else:
                raise AssertionError('removed principal was accepted')
            checked += 1
    finally:
        for table, fixture_key in ((data, target), (auth, key)):
            try:
                table.delete_item(Key=fixture_key, ConditionExpression='fixture_run = :run',
                                  ExpressionAttributeValues={':run': token})
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                if table.get_item(Key=fixture_key, ConsistentRead=True).get('Item'):
                    raise RuntimeError('refusing to clean a foreign fixture') from None
        if auth.get_item(Key=key, ConsistentRead=True).get('Item') or data.get_item(Key=target, ConsistentRead=True).get('Item'):
            raise AssertionError('synthetic fixture cleanup incomplete')
    print(json.dumps({'guard_cases': checked, 'stale_epoch_rejected': True,
                      'fresh_epoch_accepted': True, 'synthetic_cleanup_verified': True,
                      'deployed_writer_quiescence_verified': False}))


if __name__ == '__main__':
    main()
