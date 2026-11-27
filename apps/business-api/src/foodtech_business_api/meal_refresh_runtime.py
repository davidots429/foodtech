"""Finite meal refresh using a verified ECS task identity, never a local profile.

IAM, writer coordination, pending-index backfill and scheduling are independent
deployment gates. A successful sweep is not deletion or index-completeness proof.
"""
import json
import os
import re
import signal
import sys
from contextlib import contextmanager

import boto3
from botocore.config import Config

from .combi_refresh_worker import parse_refresh_targets, refresh_targets, sweep_pending
from .cooking_store import DynamoCookingStore


class MealRefreshDeadline(BaseException):
    """Escape SDK retries and per-record Exception handlers; caught only by main."""


def configured_worker():
    if os.getenv('FOODTECH_MEAL_REFRESH_ENABLED') != 'true' or \
            os.getenv('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE':
        raise ValueError('explicit ECS meal refresh configuration required')
    auth, cooking = (os.getenv(name, '') for name in ('FOODTECH_AUTH_TABLE', 'FOODTECH_COOKING_TABLE'))
    match = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth)
    if not match or cooking != 'foodtech-cooking-jobs-' + match[1] or \
            os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') not in {'true', 'false'}:
        raise ValueError('explicit matching-stage resources and enrollment mode required')
    role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/(foodtech-meal-refresh-task-' +
        re.escape(match[1]) + ')', os.getenv('FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN', ''))
    if not role:
        raise ValueError('reviewed meal refresh task role required')
    session = boto3.Session(region_name='ap-southeast-2')
    credentials = session.get_credentials()
    if credentials is None or credentials.method != 'container-role':
        raise ValueError('ECS task credentials required')
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 2}, connect_timeout=5,
        read_timeout=15, proxies={}, ignore_configured_endpoint_urls=True, account_id_endpoint_mode='disabled')
    identity = session.client('sts', config=config,
        endpoint_url='https://sts.ap-southeast-2.amazonaws.com').get_caller_identity()
    expected = r'arn:aws:sts::' + role[1] + ':assumed-role/' + re.escape(role[2]) + r'/[A-Za-z0-9+=,.@_-]{2,64}'
    if identity.get('Account') != role[1] or not re.fullmatch(expected, identity.get('Arn', '')):
        raise ValueError('unexpected meal refresh credentials')
    endpoint = 'https://dynamodb.ap-southeast-2.amazonaws.com'
    client = session.client('dynamodb', config=config, endpoint_url=endpoint)
    for name, keys in ((auth, {'pk': 'HASH'}), (cooking, {'pk': 'HASH', 'sk': 'RANGE'})):
        table = client.describe_table(TableName=name)['Table']
        if table.get('TableStatus') != 'ACTIVE' or \
                table.get('TableArn') != f'arn:aws:dynamodb:ap-southeast-2:{role[1]}:table/{name}' or \
                {item['AttributeName']: item['KeyType'] for item in table.get('KeySchema', [])} != keys or \
                any({item['AttributeName']: item['AttributeType'] for item in
                    table.get('AttributeDefinitions', [])}.get(key) != 'S' for key in keys):
            raise ValueError('meal refresh table contract unavailable')
        if name == cooking:
            indexes = [x for x in table.get('GlobalSecondaryIndexes', []) if x.get('IndexName') == 'MealAggregatePending']
            if len(indexes) != 1 or indexes[0].get('IndexStatus') != 'ACTIVE' or \
                    indexes[0].get('Backfilling', False) is not False or \
                    {x['AttributeName']: x['KeyType'] for x in indexes[0].get('KeySchema', [])} != {
                        'aggregate_refresh_shard': 'HASH', 'pk': 'RANGE'} or \
                    indexes[0].get('Projection', {}).get('ProjectionType') != 'KEYS_ONLY' or \
                    {x['AttributeName']: x['AttributeType'] for x in table.get('AttributeDefinitions', [])}.get('aggregate_refresh_shard') != 'S':
                raise ValueError('meal refresh pending index unavailable')
    resource = session.resource('dynamodb', config=config, endpoint_url=endpoint)
    store = DynamoCookingStore(cooking, auth, dynamodb_resource=resource, dynamodb_client=client)
    return store, resource.Table(auth)


@contextmanager
def deadline(seconds):
    """Interrupt initialization and the sweep; in-flight SDK shutdown is bounded separately."""
    def expire(*_):
        raise MealRefreshDeadline('meal refresh deadline')
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main():
    try:
        page_size = int(os.getenv('FOODTECH_MEAL_REFRESH_PAGE_SIZE', '25'))
        pages = int(os.getenv('FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD', '100'))
        seconds = int(os.getenv('FOODTECH_MEAL_REFRESH_MAX_SECONDS', '600'))
        if not 1 <= page_size <= 100 or not 1 <= pages <= 10000 or not 10 <= seconds <= 600:
            raise ValueError
        mode = os.getenv('FOODTECH_MEAL_REFRESH_MODE', 'sweep')
        raw_targets = os.getenv('FOODTECH_MEAL_REFRESH_TARGETS')
        if mode == 'targets':
            if os.getenv('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true':
                raise ValueError
            targets = parse_refresh_targets(raw_targets)
        elif mode != 'sweep' or raw_targets is not None:
            raise ValueError
    except (ValueError, RecursionError):
        print('Explicit valid meal refresh bounds are required.', file=sys.stderr)
        return 2
    try:
        with deadline(seconds):
            try:
                store, auth = configured_worker()
            except ValueError:
                print('Explicit valid meal refresh configuration is required.', file=sys.stderr)
                return 2
            result = refresh_targets(store, auth, targets) if mode == 'targets' else \
                sweep_pending(store, auth, page_size=page_size, max_pages_per_shard=pages)
    except (MealRefreshDeadline, Exception):
        print('Meal refresh pass failed; reconcile before retrying.', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
