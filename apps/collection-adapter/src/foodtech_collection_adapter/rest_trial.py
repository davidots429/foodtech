"""Finite private REST quarantine check under the actual collector task role.

The caller separately reviews/applies the bucket deny/allow stages. This command
never changes policies, deletes fixtures or treats a simulated denial as proof.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
import re
import signal
import ssl
import sys
from urllib.parse import urlsplit
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import __main__ as collector

REGION = 'ap-southeast-2'
PAYLOAD = b'\xff\xfe\x80'
PROFILE = 'mockprofile-100'
INSTANCE = 'mockprofile-100-a-001'


class RestTrialDeadline(BaseException):
    """Escape ordinary SDK retry/error handlers; handled only at main."""


@dataclass(frozen=True)
class Trial:
    account: str
    role_name: str
    trial_id: str
    attempt: str
    seconds: int
    table: str
    bucket: str
    cursor_key: str

    @property
    def event_id(self):
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
            f'foodtech:rest100:{self.cursor_key}:0:{hashlib.sha256(PAYLOAD).hexdigest()}'))

    @property
    def cursor(self):
        return {'pk': f'MOCK#{INSTANCE}#{self.cursor_key}', 'sk': 'CURSOR'}

    @property
    def manifest(self):
        return {'pk': 'INGESTION_DLQ#' + self.event_id, 'sk': 'MANIFEST'}


def configuration():
    if os.getenv('FOODTECH_REST_TRIAL_ENABLED') != 'true' or \
            os.getenv('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE' or os.getenv('FOODTECH_STAGE') != 'dev':
        raise ValueError('explicit development ECS REST trial required')
    forbidden = ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
        'AWS_SESSION_TOKEN', 'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN', 'AWS_CONTAINER_CREDENTIALS_FULL_URI')
    if any(os.getenv(name) for name in forbidden) or not os.getenv('AWS_CONTAINER_CREDENTIALS_RELATIVE_URI'):
        raise ValueError('only task credential discovery is allowed')
    role = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/(foodtech-collection-task-dev)',
                       os.getenv('FOODTECH_COLLECTION_TASK_ROLE_ARN', ''))
    raw_id = os.getenv('FOODTECH_REST_TRIAL_ID', '')
    trial_id = uuid.UUID(raw_id)
    if not role or trial_id.version != 4 or trial_id.variant != uuid.RFC_4122 or str(trial_id) != raw_id:
        raise ValueError('reviewed task role and canonical trial UUID required')
    table, bucket, queue = (os.getenv(name) for name in
        ('FOODTECH_DATA_TABLE', 'FOODTECH_INGESTION_DLQ_BUCKET', 'FOODTECH_ORDERED_QUEUE_URL'))
    if table != 'foodtech-data-flow-dev' or bucket != f'foodtech-ingestion-quarantine-{role[1]}-dev' or \
            queue != f'https://sqs.{REGION}.amazonaws.com/{role[1]}/foodtech-ordered-dev.fifo':
        raise ValueError('matching development collection resources required')
    url = urlsplit(os.getenv('FOODTECH_MOCK_BASE_URL', ''))
    address = ipaddress.ip_address(url.hostname or '')
    network = ipaddress.ip_network('10.42.0.32/27')
    if url.scheme != 'https' or address.version != 4 or address not in network or \
            int(address) <= int(network.network_address) + 3 or address == network.broadcast_address or \
            url.port != 8443 or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('reviewed private Mock origin required')
    if any(not os.getenv(name) for name in ('FOODTECH_MOCK_CLIENT_ID', 'FOODTECH_MOCK_CLIENT_SECRET', 'FOODTECH_MOCK_CA_FILE')):
        raise ValueError('injected Mock authentication and CA required')
    ssl.create_default_context(cafile=os.environ['FOODTECH_MOCK_CA_FILE'])
    attempt = os.getenv('FOODTECH_REST_TRIAL_ATTEMPT')
    seconds = int(os.getenv('FOODTECH_REST_TRIAL_MAX_SECONDS', ''))
    if attempt not in {'blocked', 'allowed'} or not 10 <= seconds <= 60:
        raise ValueError('explicit reviewed trial attempt and bounds required')
    return Trial(role[1], role[2], raw_id, attempt, seconds, table, bucket, 'resttrial-' + trial_id.hex)


class VerifiedSession:
    """Give every real collector client the same verified provider and config."""
    def __init__(self, session, config):
        self.session, self.config = session, config
        self.clients, self.resources = {}, {}

    def client(self, service):
        if service not in {'dynamodb', 's3', 'sqs'}:
            raise ValueError('unexpected collection service')
        if service not in self.clients:
            config = self.config.merge(Config(signature_version='s3v4')) if service == 's3' else self.config
            self.clients[service] = self.session.client(service, config=config,
                endpoint_url=f'https://{service}.{REGION}.amazonaws.com')
        return self.clients[service]

    def resource(self, service):
        if service != 'dynamodb':
            raise ValueError('unexpected collection resource')
        if service not in self.resources:
            self.resources[service] = self.session.resource(service, config=self.config,
                endpoint_url=f'https://{service}.{REGION}.amazonaws.com')
        return self.resources[service]


def verified_session(trial):
    session = boto3.Session(region_name=REGION)
    credentials = session.get_credentials()
    if credentials is None or credentials.method != 'container-role':
        raise ValueError('ECS task credentials required')
    config = Config(region_name=REGION, retries={'mode': 'standard', 'total_max_attempts': 2},
        connect_timeout=3, read_timeout=5, proxies={}, ignore_configured_endpoint_urls=True,
        account_id_endpoint_mode='disabled')
    identity = session.client('sts', config=config, endpoint_url=f'https://sts.{REGION}.amazonaws.com').get_caller_identity()
    expected = r'arn:aws:sts::' + trial.account + ':assumed-role/' + re.escape(trial.role_name) + r'/[A-Za-z0-9+=,.@_-]{2,64}'
    if identity.get('Account') != trial.account or not re.fullmatch(expected, identity.get('Arn', '')):
        raise ValueError('unexpected collector credentials')
    return VerifiedSession(session, config)


def records(table, trial):
    return tuple(table.get_item(Key=key, ConsistentRead=True).get('Item') for key in (trial.cursor, trial.manifest))


def verify_success(table, trial):
    cursor, manifest = records(table, trial)
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    expected = {'ingestion_event_id': trial.event_id, 'bucket': trial.bucket, 'sha256': digest,
        'object_key': f'ingestion/REST/{PROFILE}/{trial.event_id}/{digest}.bin',
        'ingestion_failure_reason_code': 'UNSUPPORTED_ENCODING', 'missing_components': [],
        'protocol': 'REST', 'profile_id': PROFILE, 'instance_id': INSTANCE, 'cursor_key': trial.cursor_key,
        'source_position': 0, 'provenance': 'SYNTHETIC'}
    if not cursor or isinstance(cursor.get('cursor'), bool) or cursor.get('cursor') != 1 or cursor.get('streams') != [] or \
            not manifest or any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError('trial result does not match reviewed quarantine and cursor')


@contextmanager
def deadline(seconds):
    def expire(*_):
        raise RestTrialDeadline()
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', action='store_true', required=True)
    parser.parse_args(argv)
    try:
        trial = configuration()
    except Exception:
        print('Explicit valid private REST trial configuration is required.', file=sys.stderr)
        return 2
    try:
        with deadline(trial.seconds):
            session = verified_session(trial)
            table = session.resource('dynamodb').Table(trial.table)
            if any(item is not None for item in records(table, trial)):
                raise ValueError('trial records already exist; reconcile before retrying')
            try:
                result = collector.main(['--cursor-key', trial.cursor_key, '--polls', '1',
                    '--profile', PROFILE, '--instance', INSTANCE], session=session, expected_trial_payload=PAYLOAD)
            except ClientError as error:
                if trial.attempt != 'blocked' or error.operation_name != 'PutObject' or \
                        error.response.get('Error', {}).get('Code') != 'AccessDenied' or \
                        error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 403:
                    raise
                if any(item is not None for item in records(table, trial)):
                    raise ValueError('cursor or manifest advanced after denied storage')
                outcome = {'storage_denial_verified': True, 'cursor_records': 0, 'manifest_records': 0}
            else:
                if trial.attempt != 'allowed' or result != 0:
                    raise ValueError('unexpected collection result')
                verify_success(table, trial)
                outcome = {'quarantine_manifest_verified': True, 'cursor_records': 1, 'manifest_records': 1}
    except (RestTrialDeadline, Exception):
        print('Private REST trial failed; reconcile owned fixtures before retrying.', file=sys.stderr)
        return 1
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
