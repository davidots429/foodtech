"""Finite read-only ECS role probe, without identity release or deletion writes.

Accepted reads prove only that these requests can execute. They do not establish
enrollment readiness, existing deletion records, Cognito access or KMS decryption.
"""
import json
import os
import re
import sys
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def configuration(env):
    if env.get('FOODTECH_IDENTITY_READ_PROBE_ENABLED') != 'true' or \
            env.get('AWS_EXECUTION_ENV') != 'AWS_ECS_FARGATE':
        raise ValueError('explicit Fargate probe configuration required')
    account, probe = env.get('FOODTECH_PROBE_ACCOUNT', ''), env.get('FOODTECH_PROBE_ID', '')
    if not re.fullmatch(r'[0-9]{12}', account):
        raise ValueError('invalid probe account')
    parsed = UUID(probe)
    if str(parsed) != probe or parsed.version != 4:
        raise ValueError('invalid probe identifier')
    return account, probe


def requests(resource, probe):
    auth = resource.Table('foodtech-auth-sessions-dev')
    # The extra ROLEPROBE# component cannot be a valid application subject or
    # withdrawal UUID. Only projected keys/metadata can be returned, never logs.
    principal = 'USER#ROLEPROBE#' + probe
    return (
        ('enrollment_key_projection', True, lambda: auth.get_item(
            Key={'pk': 'CONTROL#PRINCIPAL_ENROLLMENT'}, ProjectionExpression='pk', ConsistentRead=True)),
        ('principal_registry_transaction', True, lambda: resource.meta.client.transact_get_items(TransactItems=[
            {'Get': {'TableName': auth.name, 'Key': {'pk': principal}, 'ProjectionExpression': 'pk'}},
            {'Get': {'TableName': 'foodtech-deletion-registry-dev',
                     'Key': {'pk': 'REGISTRY', 'sk': 'REQ#ROLEPROBE#' + probe}, 'ProjectionExpression': 'pk, sk'}},
        ])),
        ('bounded_metadata_scan', True, lambda: auth.scan(Limit=1, Select='SPECIFIC_ATTRIBUTES',
            ProjectionExpression='#pk, #request, #epoch', FilterExpression='#pk = :probe',
            ExpressionAttributeNames={'#pk': 'pk', '#request': 'withdrawal_request_id', '#epoch': 'epoch'},
            ExpressionAttributeValues={':probe': principal})),
        ('session_read_denied', False, lambda: auth.get_item(
            Key={'pk': 'SESSION#ROLEPROBE#' + probe}, ProjectionExpression='pk', ConsistentRead=True)),
        ('direct_principal_read_denied', False, lambda: auth.get_item(
            Key={'pk': principal}, ProjectionExpression='pk', ConsistentRead=True)),
        ('other_table_read_denied', False, lambda: resource.meta.client.transact_get_items(TransactItems=[
            {'Get': {'TableName': 'foodtech-cooking-jobs-dev', 'Key': {'pk': principal, 'sk': 'META'},
                     'ProjectionExpression': 'pk'}}])),
    )


def main():
    try:
        account, probe = configuration(os.environ)
    except (ValueError, TypeError, AttributeError):
        print('Explicit valid Fargate read-probe configuration is required.', file=sys.stderr)
        return 2
    try:
        config = Config(retries={'mode': 'standard', 'total_max_attempts': 2}, connect_timeout=5, read_timeout=10,
                        ignore_configured_endpoint_urls=True)
        session = boto3.Session(region_name='ap-southeast-2')
        credentials = session.get_credentials()
        if credentials is None or credentials.method != 'container-role':
            raise ValueError('ECS container credentials required')
        caller = session.client('sts', config=config).get_caller_identity()
        expected = rf'arn:aws:sts::{account}:assumed-role/foodtech-withdrawal-identity-task-dev/[A-Za-z0-9+=,.@_-]{{2,64}}'
        if caller.get('Account') != account or not re.fullmatch(expected, caller.get('Arn', '')):
            raise ValueError('unexpected probe identity')
        resource = session.resource('dynamodb', config=config)
        accepted, denied = [], []
        for name, allow, invoke in requests(resource, probe):
            try:
                response = invoke()
            except ClientError as error:
                # IAM AccessDeniedException is not a modeled DynamoDB exception.
                # Only this exact authorization rejection passes a negative case.
                if allow or error.response.get('Error', {}).get('Code') != 'AccessDeniedException':
                    raise
                denied.append(name)
            else:
                if not allow or not isinstance(response, dict):
                    raise ValueError('unexpected read-probe outcome')
                accepted.append(name)
    except Exception:
        # SDK responses, role session IDs, keys, returned items and exception
        # messages must not reach logs. No write API is used even on failure.
        print('Identity read probe failed; no write operations were attempted.', file=sys.stderr)
        return 1
    print(json.dumps({'schema': 1, 'expected_role_observed': True, 'accepted_reads': accepted,
        'expected_denials': denied, 'identity_release_verified': False, 'deletion_complete': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
