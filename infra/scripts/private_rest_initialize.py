"""Inspect or explicitly initialize the two reviewed synthetic REST Secrets.

Default mode reads metadata only. Values stay in this process and AWS Secrets
Manager; they are never accepted as arguments, printed, or written to files.
"""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.paginate import Paginator

from private_rest_material import generate, restore

ACCOUNT = '573693340100'
REGION = 'ap-southeast-2'
STACK = 'FoodtechPrivateRestSecrets-dev'


class InitializationDeadline(BaseException):
    pass


class Arguments(argparse.ArgumentParser):
    def error(self, message):
        # Unknown arguments might contain an accidentally pasted secret.
        raise ValueError('invalid initialization arguments')


@dataclass(frozen=True)
class Store:
    kind: str
    arn: str
    key: str

    @property
    def name(self):
        return f'foodtech/dev/private-rest-{self.kind}'


def configuration(argv):
    parser = Arguments(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--reviewed-template-sha256', required=True)
    parser.add_argument('--max-seconds', type=int, default=120)
    for kind in ('mock', 'collector'):
        parser.add_argument(f'--{kind}-secret-arn', required=True)
        parser.add_argument(f'--{kind}-key-arn', required=True)
    options = parser.parse_args(argv)
    request = uuid.UUID(options.request_id)
    if str(request) != options.request_id or request.version != 4 or request.variant != uuid.RFC_4122 or \
            not re.fullmatch(r'[a-f0-9]{64}', options.reviewed_template_sha256) or not 10 <= options.max_seconds <= 120:
        raise ValueError('reviewed request, template and bounds required')
    if not hasattr(signal, 'SIGALRM'):
        raise ValueError('this bounded operator command requires a POSIX host')
    verify_operator_environment()
    stores = []
    for kind in ('mock', 'collector'):
        arn, key = getattr(options, f'{kind}_secret_arn'), getattr(options, f'{kind}_key_arn')
        if not re.fullmatch(fr'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:foodtech/dev/private-rest-{kind}-[A-Za-z0-9]{{6}}', arn) or \
                not re.fullmatch(fr'arn:aws:kms:{REGION}:{ACCOUNT}:key/[a-f0-9]{{8}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{4}}-[a-f0-9]{{12}}', key):
            raise ValueError('complete matching development ARNs required')
        stores.append(Store(kind, arn, key))
    if stores[0].key == stores[1].key:
        raise ValueError('independent storage keys required')
    return options, stores


def verify_operator_environment():
    forbidden = ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_SECURITY_TOKEN',
        'AWS_WEB_IDENTITY_TOKEN_FILE', 'AWS_ROLE_ARN', 'AWS_CONTAINER_CREDENTIALS_RELATIVE_URI',
        'AWS_CONTAINER_CREDENTIALS_FULL_URI', 'AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE', 'BOTO_CONFIG')
    if any(os.getenv(name) for name in forbidden) or any(os.getenv(name, 'foodtech') != 'foodtech' for name in ('AWS_PROFILE', 'AWS_DEFAULT_PROFILE')):
        raise ValueError('only the reviewed foodtech login profile is allowed')


class VerifiedOperator:
    def __init__(self):
        for name in ('boto3', 'botocore', 'urllib3'):
            logging.getLogger(name).setLevel(logging.WARNING)
        self.session = boto3.Session(profile_name='foodtech', region_name=REGION)
        credentials = self.session.get_credentials()
        if credentials is None or credentials.method != 'login':
            raise ValueError('interactive foodtech login credentials required')
        self.config = Config(region_name=REGION, connect_timeout=3, read_timeout=5,
            retries={'mode': 'standard', 'total_max_attempts': 2}, proxies={},
            ignore_configured_endpoint_urls=True, account_id_endpoint_mode='disabled')
        identity = self.session.client('sts', config=self.config, verify=True,
            endpoint_url=f'https://sts.{REGION}.amazonaws.com').get_caller_identity()
        if identity.get('Account') != ACCOUNT or not re.fullmatch(
                fr'arn:aws:sts::{ACCOUNT}:assumed-role/AccountFullAccessRole/[A-Za-z0-9+=,.@_-]{{2,64}}', identity.get('Arn', '')):
            raise ValueError('unexpected development operator')
        self.clients = {}

    def client(self, service):
        if service not in {'cloudformation', 'kms', 'secretsmanager'}:
            raise ValueError('unexpected initialization service')
        if service not in self.clients:
            self.clients[service] = self.session.client(service, config=self.config, verify=True,
                endpoint_url=f'https://{service}.{REGION}.amazonaws.com')
        return self.clients[service]


def canonical_hash(template):
    return hashlib.sha256(json.dumps(template, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def policy_shape(document):
    if set(document) - {'Version', 'Id', 'Statement'}:
        raise ValueError('unexpected policy fields')
    statements = []
    def values(value):
        return sorted(value if isinstance(value, list) else [value])
    for statement in document['Statement']:
        result = dict(statement)
        for key in ('Action', 'NotAction', 'Resource', 'NotResource'):
            if key in result:
                result[key] = values(result[key])
        if isinstance(result.get('Principal'), dict):
            result['Principal'] = {key: values(value) for key, value in result['Principal'].items()}
        if 'Condition' in result:
            result['Condition'] = {operator: {key: values(value) for key, value in fields.items()}
                                   for operator, fields in result['Condition'].items()}
        statements.append(result)
    return {'Version': document['Version'], 'Statement': sorted(statements, key=lambda s: json.dumps(s, sort_keys=True))}


def expected_policy(store):
    text = (Path(__file__).resolve().parents[1] / 'containers/private-rest-key-policy.json').read_text()
    return json.loads(text.replace('__ACCOUNT__', ACCOUNT).replace('__REGION__', REGION).replace('__SECRET_NAME__', store.name))


def verify_stack(cf, stores, template_hash):
    stacks = cf.describe_stacks(StackName=STACK)['Stacks']
    if len(stacks) != 1 or stacks[0]['StackStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
        raise ValueError('completed reviewed stack required')
    stack = stacks[0]
    if stack['StackName'] != STACK or not re.fullmatch(fr'arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/{STACK}/[A-Za-z0-9-]+', stack['StackId']):
        raise ValueError('unexpected stack ownership')
    tags = {item['Key']: item['Value'] for item in stack.get('Tags', [])}
    if tags.get('Project') != 'foodtech' or tags.get('Environment') != 'dev':
        raise ValueError('reviewed stack tags required')
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    expected = {store.kind.capitalize() + suffix: value for store in stores
                for suffix, value in (('SecretArn', store.arn), ('SecretKeyArn', store.key))}
    if outputs != expected:
        raise ValueError('stack outputs differ from reviewed ARNs')
    template = cf.get_template(StackName=STACK, TemplateStage='Original')['TemplateBody']
    template = json.loads(template) if isinstance(template, str) else template
    if canonical_hash(template) != template_hash:
        raise ValueError('submitted template differs from review')
    resources = cf.describe_stack_resources(StackName=STACK)['StackResources']
    owned = {item['LogicalResourceId']: item for item in resources if item['ResourceType'] != 'AWS::CDK::Metadata'}
    if set(owned) != {'MockKey', 'MockSecret', 'CollectorKey', 'CollectorSecret'}:
        raise ValueError('unexpected stack resources')
    for store in stores:
        for suffix, physical, resource_type in (('Key', store.key.rsplit('/', 1)[1], 'AWS::KMS::Key'),
                                               ('Secret', store.arn, 'AWS::SecretsManager::Secret')):
            item = owned[store.kind.capitalize() + suffix]
            if item['PhysicalResourceId'] != physical or item['ResourceType'] != resource_type or \
                    item['ResourceStatus'] not in {'CREATE_COMPLETE', 'UPDATE_COMPLETE'}:
                raise ValueError('resource ownership or readiness differs')


def verify_store(sm, kms, store, request_id, progress, *, policy_factory=expected_policy, required_stages=("AWSCURRENT",)):
    description = sm.describe_secret(SecretId=store.arn)
    tags = {item['Key']: item['Value'] for item in description.get('Tags', [])}
    if description.get('ARN') != store.arn or description.get('Name') != store.name or description.get('KmsKeyId') != store.key or \
            description.get('DeletedDate') or description.get('OwningService') or description.get('RotationEnabled') or \
            description.get('ReplicationStatus') or tags.get('Project') != 'foodtech' or tags.get('Environment') != 'dev':
        raise ValueError('Secret ownership, storage key or state differs')
    key = kms.describe_key(KeyId=store.key)['KeyMetadata']
    if any(key.get(name) != value for name, value in {'Arn': store.key, 'AWSAccountId': ACCOUNT,
            'KeyId': store.key.rsplit('/', 1)[1], 'KeyManager': 'CUSTOMER', 'KeyState': 'Enabled', 'Enabled': True,
            'KeySpec': 'SYMMETRIC_DEFAULT', 'KeyUsage': 'ENCRYPT_DECRYPT', 'Origin': 'AWS_KMS', 'MultiRegion': False}.items()):
        raise ValueError('unexpected storage key')
    if not kms.get_key_rotation_status(KeyId=store.key).get('KeyRotationEnabled'):
        raise ValueError('key rotation must be enabled')
    actual = json.loads(kms.get_key_policy(KeyId=store.key, PolicyName='default')['Policy'])
    if policy_shape(actual) != policy_shape(policy_factory(store)):
        raise ValueError('key policy differs from review')
    versions = []
    # This API has NextToken/MaxResults but no packaged SDK paginator model.
    # Use the SDK paginator engine with the API's explicit token contract.
    pager = Paginator(sm.list_secret_version_ids, {'input_token': 'NextToken', 'output_token': 'NextToken',
        'limit_key': 'MaxResults', 'result_key': 'Versions'}, sm.meta.service_model.operation_model('ListSecretVersionIds'))
    for page in pager.paginate(SecretId=store.arn, IncludeDeprecated=True, PaginationConfig={'PageSize': 2}):
        for item in page.get('Versions', []):
            progress['observed_secret_versions'] += 1
            versions.append(item)
            if len(versions) > 1:
                raise ValueError('existing versions require separate reconciliation')
    if versions and (versions[0].get('VersionId') != request_id or set(versions[0].get('VersionStages') or []) != set(required_stages)):
        raise ValueError('foreign or noncurrent initialization must not be overwritten')
    return bool(versions)


def read_value(sm, store, request_id, *, required_stages=("AWSCURRENT",)):
    result = sm.get_secret_value(SecretId=store.arn, VersionId=request_id)
    if result.get('ARN') != store.arn or result.get('Name') != store.name or result.get('VersionId') != request_id or \
            set(result.get('VersionStages') or []) != set(required_stages) or not isinstance(result.get('SecretString'), str):
        raise ValueError('unexpected value version')
    return json.loads(result['SecretString'])


def write_value(sm, store, request_id, value, progress, *, candidate_stage=None):
    payload = json.dumps(value, sort_keys=True, separators=(',', ':'))
    if len(payload.encode()) > 65536:
        raise ValueError('material exceeds Secret size limit')
    progress['attempted_secret_writes'] += 1
    stage_options = {"VersionStages": [candidate_stage]} if candidate_stage else {}
    required_stages = {"AWSCURRENT", candidate_stage} if candidate_stage else {"AWSCURRENT"}
    result = sm.put_secret_value(SecretId=store.arn, ClientRequestToken=request_id, SecretString=payload, **stage_options)
    progress['acknowledged_secret_writes'] += 1
    if result.get('ARN') != store.arn or result.get('Name') != store.name or result.get('VersionId') != request_id or \
            set(result.get('VersionStages') or []) != required_stages:
        raise ValueError('unexpected upload receipt; reconcile before retrying')


def execute(sdk, options, stores, progress):
    cf, kms, sm = (sdk.client(name) for name in ('cloudformation', 'kms', 'secretsmanager'))
    progress['phase'] = 'stack_metadata'
    verify_stack(cf, stores, options.reviewed_template_sha256)
    initialized = []
    for store in stores:
        progress['phase'] = 'store_metadata_' + store.kind
        initialized.append(verify_store(sm, kms, store, options.request_id, progress))
    if initialized == [False, True]:
        raise ValueError('unexpected partial ordering requires reconciliation')
    if not options.apply:
        return {'mode': 'inspect', 'request_id': options.request_id,
                'matching_version_counts': [int(value) for value in initialized], 'values_read': False}
    progress['phase'] = 'resume_or_generate'
    material = restore(read_value(sm, stores[0], options.request_id)) if initialized[0] else generate()
    for store, exists, value in zip(stores, initialized, (material.mock, material.collector)):
        if not exists:
            progress['phase'] = 'write_' + store.kind
            write_value(sm, store, options.request_id, value, progress)
    # A verified readback is required, including after a previous partial write.
    progress['phase'] = 'readback_mock'
    recovered = restore(read_value(sm, stores[0], options.request_id))
    progress['phase'] = 'readback_collector'
    collector = read_value(sm, stores[1], options.request_id)
    if recovered.mock != material.mock or collector != material.collector:
        raise ValueError('stored pair differs; no replacement is attempted')
    return {'mode': 'initialized', 'initialized_secret_count': 2, 'request_id': options.request_id,
            'resumed': any(initialized), 'material': recovered.metadata()}


@contextmanager
def deadline(seconds):
    def expire(*_):
        raise InitializationDeadline()
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main(argv=None):
    try:
        options, stores = configuration(argv)
    except Exception:
        print('Explicit valid private REST initialization arguments are required.', file=sys.stderr)
        return 2
    progress = {'phase': 'identity', 'attempted_secret_writes': 0, 'acknowledged_secret_writes': 0,
                'observed_secret_versions': 0}
    try:
        with deadline(options.max_seconds):
            result = execute(VerifiedOperator(), options, stores, progress)
    except (InitializationDeadline, KeyboardInterrupt, Exception) as error:
        print(json.dumps({'status': 'not_verified', 'request_id': options.request_id, **progress,
            'failure_type': type(error).__name__,
            'reconciliation_required': bool(progress['attempted_secret_writes'] or progress['observed_secret_versions'])}), file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
