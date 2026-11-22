"""Read-only review of all three synthesized identity worker role policies.

The export uses synthetic resource bindings. No IAM role is assumed, created or
changed; passing simulations are not deployment or real execution evidence.
"""
import argparse
import json
from pathlib import Path

import boto3

from withdrawal_identity_iam_check import REGION, policy_inputs, scenarios, verify


def role_checks(fixtures):
    account = fixtures['AWS::AccountId']
    task = scenarios(account, 'dev', fixtures['CognitoUserPoolId'], fixtures['RegistryStorageKeyArn'])
    regional = {'ContextKeyName': 'aws:RequestedRegion', 'ContextKeyValues': [REGION], 'ContextKeyType': 'string'}
    def case(name, action, resource, allow, **values):
        return {'name': name, 'action': action, 'resource': resource, 'expected_allow': allow,
                'context': [regional, *({'ContextKeyName': key, 'ContextKeyValues': [value],
                                        'ContextKeyType': 'string'} for key, value in values.items())]}
    repo = fixtures['ImageRepositoryArn']
    logs = f'arn:aws:logs:{REGION}:{account}:log-group:/foodtech/dev/withdrawal-identity:log-stream:synthetic'
    execution = [case('ecr_authorization', 'ecr:GetAuthorizationToken', '*', True)]
    for action in ('BatchGetImage', 'GetDownloadUrlForLayer', 'BatchCheckLayerAvailability'):
        execution.append(case('pull_' + action, 'ecr:' + action, repo, True))
        execution.append(case('other_repository_' + action, 'ecr:' + action, repo + '-other', False))
    for action in ('PutImage', 'CreateRepository'):
        execution.append(case('cannot_' + action, 'ecr:' + action, repo, False))
    for action in ('CreateLogStream', 'PutLogEvents'):
        execution.append(case('worker_' + action, 'logs:' + action, logs, True))
        execution.append(case('other_' + action, 'logs:' + action, logs.replace('/withdrawal-identity', '/other'), False))
    for check_name in ('read_principal', 'AdminDeleteUser', 'registry_storage_decrypt'):
        original = next(check for check in task if check['name'] == check_name)
        execution.append({**original, 'name': 'execution_cannot_' + check_name, 'expected_allow': False})
    wrong_region = case('ecr_other_region', 'ecr:GetAuthorizationToken', '*', False)
    wrong_region['context'] = [{'ContextKeyName': 'aws:RequestedRegion', 'ContextKeyValues': ['us-east-1'], 'ContextKeyType': 'string'}]
    execution.append(wrong_region)
    family = f'arn:aws:ecs:{REGION}:{account}:task-definition/foodtech-withdrawal-identity-dev:1'
    cluster = fixtures['ClusterArn']
    invocation = [case('run_reviewed_family', 'ecs:RunTask', family, True, **{'ecs:cluster': cluster}),
                  case('cannot_run_other_family', 'ecs:RunTask', family.replace('withdrawal-identity', 'other'), False, **{'ecs:cluster': cluster}),
                  case('cannot_run_other_cluster', 'ecs:RunTask', family, False, **{'ecs:cluster': cluster + '-other'}),
                  case('cannot_run_without_cluster', 'ecs:RunTask', family, False)]
    for kind in ('task', 'execution'):
        arn = f'arn:aws:iam::{account}:role/foodtech-withdrawal-identity-{kind}-dev'
        invocation.append(case('pass_' + kind, 'iam:PassRole', arn, True, **{'iam:PassedToService': 'ecs-tasks.amazonaws.com'}))
        invocation.append(case('cannot_pass_' + kind + '_to_lambda', 'iam:PassRole', arn, False, **{'iam:PassedToService': 'lambda.amazonaws.com'}))
        invocation.append(case('cannot_pass_' + kind + '_without_service', 'iam:PassRole', arn, False))
    invocation.append(case('cannot_pass_other_role', 'iam:PassRole', f'arn:aws:iam::{account}:role/foodtech-other', False,
                           **{'iam:PassedToService': 'ecs-tasks.amazonaws.com'}))
    original = next(check for check in task if check['name'] == 'AdminDeleteUser')
    invocation.append({**original, 'name': 'invocation_cannot_delete_identity', 'expected_allow': False})
    return {'task': task, 'execution': execution, 'invoke': invocation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review-dir', type=Path, required=True)
    options = parser.parse_args()
    try:
        fixtures = json.loads((options.review_dir / 'fixtures.json').read_text())
        session = boto3.Session(profile_name='foodtech', region_name=REGION)
        if session.client('sts').get_caller_identity()['Account'] != fixtures['AWS::AccountId']:
            raise ValueError('unexpected development account')
        iam = session.client('iam')
        results = {}
        for kind, checks in role_checks(fixtures).items():
            policy = policy_inputs(json.loads((options.review_dir / (kind + '.json')).read_text()))
            results[kind] = verify(iam, policy, checks)
        print(json.dumps(results, sort_keys=True))
        return 0 if all(result['passed'] for result in results.values()) else 1
    except Exception:
        print('Identity access policy review failed; no AWS permissions were changed.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
