"""Opt-in finite ECS pass over expired, locked source-grant references.

This worker removes authorization references, never source/business data or
Cognito identities. It cannot complete the deletion Registry. Scan observations
are only discovery; each mutation reauthorizes the account and Registry.
"""
import json
import os
import re
import sys
import time

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config

from .principal_enrollment import DynamoPrincipalEnrollment
from .source_grant_deletion import DynamoSourceGrantDeletion
from .source_read_authority import _integer
from .source_read_registrar import SourceGrantConflict
from .subject_identity import valid_subject
from .withdrawal_identity_worker import deadline


def sweep_grants(table, deletion, *, page_size=20, max_pages=50, max_changes=20, clock):
    if type(page_size) is not int or not 1 <= page_size <= 100 or \
            type(max_pages) is not int or not 1 <= max_pages <= 1000 or \
            type(max_changes) is not int or not 1 <= max_changes <= 100:
        raise ValueError('invalid source grant worker bounds')
    totals = {'pages': 0, 'delete_acknowledged': 0, 'empty_observed': 0, 'blocked': 0,
              'scan_finished': False, 'budget_exhausted': False, 'deletion_complete': False}
    cursor, seen = None, set()
    for _ in range(max_pages):
        now = clock()
        if type(now) is not int or not 0 < now < 2**63:
            raise ValueError('invalid source grant worker clock')
        args = {'Limit': page_size, 'ConsistentRead': True,
            'ProjectionExpression': 'pk, #epoch, withdrawal_request_id',
            'ExpressionAttributeNames': {'#epoch': 'epoch'}, 'Select': 'SPECIFIC_ATTRIBUTES',
            'FilterExpression': Attr('pk').begins_with('USER#') & Attr('suspended').eq(True) &
                Attr('withdrawal_status').eq('DELETING') & Attr('withdrawal_deadline').lte(now) &
                Attr('deletion_lock_request').exists() & Attr('source_read_grants').exists() &
                Attr('source_read_grants').ne({})}
        if cursor is not None:
            args['ExclusiveStartKey'] = cursor
        page = table.scan(**args)
        rows = page.get('Items')
        if not isinstance(rows, list) or len(rows) > page_size:
            raise ValueError('invalid source grant discovery page')
        totals['pages'] += 1
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'pk', 'epoch', 'withdrawal_request_id'} or \
                    not isinstance(row['pk'], str) or not row['pk'].startswith('USER#') or \
                    not valid_subject(row['pk'][5:]) or _integer(row['epoch'], 1) is None:
                raise ValueError('invalid source grant discovery candidate')
            while totals['delete_acknowledged'] < max_changes:
                try:
                    result = deletion.step(subject=row['pk'][5:], request_id=row['withdrawal_request_id'],
                                           expected_epoch=int(row['epoch']))
                except SourceGrantConflict:
                    totals['blocked'] += 1
                    break
                if not isinstance(result, dict) or set(result) != {'state', 'remaining_upper_bound', 'deletion_complete'} or \
                        result['deletion_complete'] is not False or type(result['remaining_upper_bound']) is not int or \
                        result['remaining_upper_bound'] < 0 or result['state'] not in {'CATALOG_EMPTY_OBSERVED', 'GRANT_REMOVED'} or \
                        (result['state'] == 'CATALOG_EMPTY_OBSERVED' and result['remaining_upper_bound'] != 0):
                    raise ValueError('invalid source grant deletion result')
                if result['state'] == 'GRANT_REMOVED':
                    totals['delete_acknowledged'] += 1
                if result['state'] == 'CATALOG_EMPTY_OBSERVED':
                    totals['empty_observed'] += 1
                    break
            else:
                totals['budget_exhausted'] = True
                return totals
        key = page.get('LastEvaluatedKey')
        if key is None or key == {}:
            totals['scan_finished'] = True
            return totals
        if not isinstance(key, dict) or set(key) != {'pk'} or not isinstance(key['pk'], str) or \
                not 1 <= len(key['pk'].encode('utf-8')) <= 2048 or key['pk'] in seen:
            raise ValueError('invalid source grant discovery continuation')
        # Auth scan keys can contain session material. Keep only in this pass's
        # memory; never return, log or checkpoint the key or the candidate rows.
        cursor = dict(key)
        seen.add(key['pk'])
    totals['budget_exhausted'] = True
    return totals


def configured_worker():
    if os.getenv('FOODTECH_SOURCE_GRANT_DELETION_ENABLED') != 'true' or \
            os.getenv('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE':
        raise ValueError('explicit ECS source grant deletion configuration required')
    auth = os.getenv('FOODTECH_AUTH_TABLE', '')
    match = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth)
    if not match:
        raise ValueError('explicit source grant deletion resources required')
    stage = match[1]
    catalog, registry = (os.getenv(name, '') for name in (
        'FOODTECH_SOURCE_ACCESS_TABLE', 'FOODTECH_DELETION_REGISTRY_TABLE'))
    if catalog != 'foodtech-source-access-' + stage or registry != 'foodtech-deletion-registry-' + stage:
        raise ValueError('matching-stage source grant deletion resources required')
    role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/(foodtech-source-grant-deletion-task-' +
        re.escape(stage) + ')', os.getenv('FOODTECH_SOURCE_GRANT_DELETION_ROLE_ARN', ''))
    if not role:
        raise ValueError('reviewed source grant deletion role required')
    session = boto3.Session(region_name='ap-southeast-2')
    credentials = session.get_credentials()
    if credentials is None or credentials.method != 'container-role':
        raise ValueError('ECS task credentials required')
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 2}, connect_timeout=5,
        read_timeout=15, proxies={}, ignore_configured_endpoint_urls=True, account_id_endpoint_mode='disabled')
    identity = session.client('sts', config=config, endpoint_url='https://sts.ap-southeast-2.amazonaws.com').get_caller_identity()
    expected = r'arn:aws:sts::' + role[1] + ':assumed-role/' + re.escape(role[2]) + r'/[A-Za-z0-9+=,.@_-]{2,64}'
    if identity.get('Account') != role[1] or not re.fullmatch(expected, identity.get('Arn', '')):
        raise ValueError('unexpected source grant deletion credentials')
    endpoint = 'https://dynamodb.ap-southeast-2.amazonaws.com'
    client = session.client('dynamodb', config=config, endpoint_url=endpoint)
    for name, keys in ((auth, {'pk': 'HASH'}), (catalog, {'pk': 'HASH', 'sk': 'RANGE'}),
                       (registry, {'pk': 'HASH', 'sk': 'RANGE'})):
        description = client.describe_table(TableName=name)['Table']
        if description.get('TableStatus') != 'ACTIVE' or \
                description.get('TableArn') != f'arn:aws:dynamodb:ap-southeast-2:{role[1]}:table/{name}' or \
                {item['AttributeName']: item['KeyType'] for item in description.get('KeySchema', [])} != keys or \
                any({item['AttributeName']: item['AttributeType'] for item in
                     description.get('AttributeDefinitions', [])}.get(key) != 'S' for key in keys):
            raise ValueError('source grant deletion table contract unavailable')
    table = session.resource('dynamodb', config=config, endpoint_url=endpoint).Table(auth)
    DynamoPrincipalEnrollment(table=table).fence()
    return table, DynamoSourceGrantDeletion(auth_table=auth, catalog_table=catalog, registry_table=registry,
        client=client, clock=lambda: int(time.time()))


def main():
    try:
        bounds = [int(os.getenv(name, default)) for name, default in (
            ('FOODTECH_SOURCE_GRANT_PAGE_SIZE', '20'), ('FOODTECH_SOURCE_GRANT_MAX_PAGES', '50'),
            ('FOODTECH_SOURCE_GRANT_MAX_CHANGES', '20'), ('FOODTECH_SOURCE_GRANT_MAX_SECONDS', '600'))]
        page_size, max_pages, max_changes, seconds = bounds
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 1000 or not 1 <= max_changes <= 100 or not 10 <= seconds <= 600:
            raise ValueError
    except ValueError:
        print('Explicit valid source grant worker bounds are required.', file=sys.stderr)
        return 2
    try:
        with deadline(seconds):
            try:
                table, deletion = configured_worker()
            except ValueError:
                print('Explicit valid source grant worker configuration is required.', file=sys.stderr)
                return 2
            result = sweep_grants(table, deletion, page_size=page_size, max_pages=max_pages,
                                  max_changes=max_changes, clock=lambda: int(time.time()))
    except Exception:
        print('Source grant cleanup failed; reconcile before retrying.', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result['blocked'] or result['budget_exhausted'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
