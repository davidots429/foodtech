"""One bounded ECS standardization pass using the reviewed task identity.

No Cognito, schedule, account deletion or infrastructure mutation. Raw SQS
references/results and SDK errors never leave this command's process output.
"""
from contextlib import contextmanager
import json
import os
import re
import signal
import sys

import boto3
from botocore.config import Config

from .aws_flow import DynamoStandardizationWorker


def settings(env):
    if env.get('FOODTECH_STANDARDIZATION_RUNTIME_ENABLED') != 'true' or \
            env.get('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED') != 'true' or \
            env.get('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE':
        raise ValueError('explicit strict ECS runtime required')
    account, stage = env.get('FOODTECH_WORKER_ACCOUNT', ''), env.get('FOODTECH_WORKER_STAGE', '')
    if not re.fullmatch(r'[0-9]{12}', account) or not re.fullmatch(r'[a-z0-9-]{1,32}', stage):
        raise ValueError('invalid runtime project')
    expected = {
        'FOODTECH_DATA_TABLE': f'foodtech-data-flow-{stage}',
        'FOODTECH_COOKING_TABLE': f'foodtech-cooking-jobs-{stage}',
        'FOODTECH_AUTH_TABLE': f'foodtech-auth-sessions-{stage}',
        'FOODTECH_ORDERED_QUEUE_URL': f'https://sqs.ap-southeast-2.amazonaws.com/{account}/foodtech-ordered-{stage}.fifo',
        'FOODTECH_STANDARDIZATION_DLQ_URL': f'https://sqs.ap-southeast-2.amazonaws.com/{account}/foodtech-standardizationdlq-{stage}.fifo',
    }
    if any(env.get(name) != value for name, value in expected.items()):
        raise ValueError('runtime resources do not match reviewed project')
    role = re.fullmatch(r'arn:aws:iam::' + account + r':role/([A-Za-z0-9+=,.@_-]{1,64})',
                        env.get('FOODTECH_WORKER_TASK_ROLE_ARN', ''))
    if not role:
        raise ValueError('explicit reviewed task role required')
    seconds = int(env.get('FOODTECH_WORKER_MAX_SECONDS', '600'))
    if not 10 <= seconds <= 600:
        raise ValueError('invalid runtime deadline')
    return account, role[1], seconds


@contextmanager
def deadline(seconds):
    def expire(*_):
        raise TimeoutError('bounded worker deadline')
    prior = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prior)


def configured_worker(account, role_name):
    session = boto3.Session(region_name='ap-southeast-2')
    credentials = session.get_credentials()
    if credentials is None or credentials.method != 'container-role':
        raise ValueError('ECS task credentials required')
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 2},
                    connect_timeout=5, read_timeout=15, ignore_configured_endpoint_urls=True)
    identity = session.client('sts', config=config).get_caller_identity()
    expected = rf'arn:aws:sts::{account}:assumed-role/{re.escape(role_name)}/[A-Za-z0-9+=,.@_-]{{2,64}}'
    if identity.get('Account') != account or not re.fullmatch(expected, identity.get('Arn', '')):
        raise ValueError('unexpected ECS task identity')
    return DynamoStandardizationWorker(os.environ['FOODTECH_DATA_TABLE'],
        os.environ['FOODTECH_ORDERED_QUEUE_URL'], os.environ['FOODTECH_STANDARDIZATION_DLQ_URL'],
        aws_session=session, aws_config=config)


def main():
    try:
        account, role, seconds = settings(os.environ)
    except (TypeError, ValueError):
        print('Explicit valid standardization runtime configuration is required.', file=sys.stderr)
        return 2
    try:
        with deadline(seconds):
            results = configured_worker(account, role).run_once()
            if not isinstance(results, list) or len(results) > 10 or any(not isinstance(x, dict) for x in results):
                raise ValueError('invalid bounded processing result')
    except Exception:
        print('Standardization pass failed; reconcile durable records before retrying.', file=sys.stderr)
        return 1
    # Count only; references, normalized values, receipt handles and keys stay private.
    print(json.dumps({'schema': 1, 'messages_processed': len(results), 'pass_finished': True}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
