"""Read-only policy simulation for the deployed collection task role.

An actual ECS task is still required to establish runtime access.
"""
import json
import os

import boto3

ACCOUNT = '573693340100'
REGION = 'ap-southeast-2'


def scenarios(bucket):
    table = f'arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/foodtech-data-flow-dev'
    queue = f'arn:aws:sqs:{REGION}:{ACCOUNT}:foodtech-ordered-dev.fifo'
    obj = f'arn:aws:s3:::{bucket}'
    return [
        ('read', 'dynamodb:GetItem', table, None, True),
        ('write', 'dynamodb:PutItem', table, None, True),
        ('update', 'dynamodb:UpdateItem', table, None, True),
        ('query', 'dynamodb:Query', table, None, True),
        ('delete_stream', 'dynamodb:DeleteItem', table, 'STREAM#synthetic', True),
        ('delete_other', 'dynamodb:DeleteItem', table, 'INGESTION_DLQ#synthetic', False),
        ('delete_without_key', 'dynamodb:DeleteItem', table, None, False),
        ('send', 'sqs:SendMessage', queue, None, True),
        ('receive', 'sqs:ReceiveMessage', queue, None, False),
        ('quarantine_write', 's3:PutObject', obj + '/ingestion/synthetic', None, True),
        ('other_object_write', 's3:PutObject', obj + '/other/synthetic', None, False),
        ('quarantine_delete', 's3:DeleteObject', obj + '/ingestion/synthetic', None, False),
    ]


def simulate(iam, simulation, action, resource, key):
    context = [{'ContextKeyName': 'aws:RequestedRegion', 'ContextKeyValues': [REGION],
                'ContextKeyType': 'string'}]
    if key:
        context.append({'ContextKeyName': 'dynamodb:LeadingKeys', 'ContextKeyValues': [key],
                        'ContextKeyType': 'stringList'})
    operation = 'simulate_principal_policy' if 'PolicySourceArn' in simulation else 'simulate_custom_policy'
    pages = iam.get_paginator(operation).paginate(**simulation, ActionNames=[action], ResourceArns=[resource],
        ContextEntries=context, PaginationConfig={'PageSize': 100})
    results = [result for page in pages for result in page['EvaluationResults']]
    if len(results) != 1:
        raise ValueError('unexpected IAM simulation response')
    result = results[0]
    return {'decision': result['EvalDecision'],
            'organizations_allowed': result.get('OrganizationsDecisionDetail', {}).get('AllowedByOrganizations')}


def main():
    if os.getenv('AWS_PROFILE') != 'foodtech':
        raise SystemExit('explicit development profile required')
    session = boto3.Session(profile_name='foodtech', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise SystemExit('unexpected AWS account')
    stack = session.client('cloudformation').describe_stacks(StackName='FoodtechMinimalDataFlow-dev')['Stacks'][0]
    outputs = {item['OutputKey']: item['OutputValue'] for item in stack.get('Outputs', [])}
    role = outputs.get('CollectionAdapterTaskRoleArn')
    bucket = outputs.get('IngestionDlqBucketName')
    if not role or not bucket:
        raise SystemExit('collection role or quarantine bucket is not deployed')
    iam = session.client('iam')
    role_name = role.rsplit('/', 1)[1]
    inline = [name for page in iam.get_paginator('list_role_policies').paginate(RoleName=role_name)
              for name in page['PolicyNames']]
    attached = [item for page in iam.get_paginator('list_attached_role_policies').paginate(RoleName=role_name)
                for item in page['AttachedPolicies']]
    if len(inline) != 1 or attached:
        raise ValueError('collection role policy inventory requires review')
    policy = iam.get_role_policy(RoleName=role_name, PolicyName=inline[0])['PolicyDocument']
    summary = {}
    for name, action, resource, key, expected in scenarios(bucket):
        identity = simulate(iam, {'PolicyInputList': [json.dumps(policy)]}, action, resource, key)
        effective = simulate(iam, {'PolicySourceArn': role}, action, resource, key)
        summary[name] = {'identity': identity['decision'], 'effective': effective['decision'],
                         'organizations_allowed': effective['organizations_allowed'], 'expected_allow': expected}
    print(json.dumps(summary, sort_keys=True))
    if any((entry['identity'] == 'allowed') != entry['expected_allow'] or
           (entry['effective'] == 'allowed') != entry['expected_allow'] for entry in summary.values()):
        raise SystemExit('collection role policy or effective permissions do not match; inspect deployment and Organizations')


if __name__ == '__main__':
    main()
