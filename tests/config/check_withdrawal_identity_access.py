"""Inspect the opt-in role stack and optionally export private review fixtures.

Exports retain the synthesized policy, resolving only CloudFormation references.
Synthetic Pool/key/repository/cluster values do not represent deployed resources.
"""
import argparse
import json
import os
from pathlib import Path


def inspect(path, output=None):
    path = Path(path)
    template = json.loads(path.read_text())
    manifest = json.loads((path.parent / 'manifest.json').read_text())
    artifact, = [item for item in manifest['artifacts'].values()
                 if item.get('properties', {}).get('templateFile') == path.name]
    account, region = artifact['environment'].removeprefix('aws://').split('/')
    assert region == 'ap-southeast-2' and account.isdigit() and len(account) == 12
    params = {'AWS::AccountId': account, 'AWS::Region': region, 'AWS::Partition': 'aws',
              'AWS::URLSuffix': 'amazonaws.com', 'CognitoUserPoolId': 'ap-southeast-2_SyntheticPool',
              'RegistryStorageKeyArn': f'arn:aws:kms:{region}:{account}:key/11111111-1111-4111-8111-111111111111',
              'ImageRepositoryArn': f'arn:aws:ecr:{region}:{account}:repository/foodtech/identity-worker',
              'ClusterArn': f'arn:aws:ecs:{region}:{account}:cluster/foodtech-synthetic'}
    resources = template['Resources']
    def resolve(value):
        if isinstance(value, list):
            return [resolve(child) for child in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {'Ref'}:
            return params[value['Ref']]
        if set(value) == {'Fn::Join'}:
            separator, parts = value['Fn::Join']
            return separator.join(resolve(part) for part in parts)
        if set(value) == {'Fn::GetAtt'}:
            logical_id, attr = value['Fn::GetAtt']
            assert resources[logical_id]['Type'] == 'AWS::IAM::Role' and attr == 'Arn'
            return f'arn:aws:iam::{account}:role/' + resources[logical_id]['Properties']['RoleName']
        return {key: resolve(child) for key, child in value.items()}
    roles = {key: resolve(r['Properties']) for key, r in resources.items() if r['Type'] == 'AWS::IAM::Role'}
    assert len(roles) == 3
    assert all(not role.get('ManagedPolicyArns') and not role.get('Policies') for role in roles.values())
    policies = {}
    for r in resources.values():
        if r['Type'] == 'AWS::IAM::Policy':
            ids = r['Properties']['Roles']
            assert len(ids) == 1 and set(ids[0]) == {'Ref'}
            key = ids[0]['Ref']
            assert key in roles and key not in policies
            policies[key] = resolve(r['Properties']['PolicyDocument'])
    assert set(policies) == set(roles)
    by_kind = {kind: key for kind in ('task', 'execution', 'invoke') for key, role in roles.items()
               if role['RoleName'] == 'foodtech-withdrawal-identity-' + kind + '-dev'}
    assert len(by_kind) == 3
    for kind in ('task', 'execution'):
        trust, = roles[by_kind[kind]]['AssumeRolePolicyDocument']['Statement']
        assert trust['Principal'] == {'Service': 'ecs-tasks.amazonaws.com'}
        assert trust['Condition']['StringEquals']['aws:SourceAccount'] == account
        assert trust['Condition']['ArnLike']['aws:SourceArn'] == f'arn:aws:ecs:{region}:{account}:*'
    trust, = roles[by_kind['invoke']]['AssumeRolePolicyDocument']['Statement']
    assert trust['Principal'] == {'Service': 'events.amazonaws.com'}
    assert trust['Condition']['StringEquals']['aws:SourceAccount'] == account
    assert trust['Condition']['ArnEquals']['aws:SourceArn'] == f'arn:aws:events:{region}:{account}:rule/foodtech-withdrawal-identity-dev'
    for key, policy in policies.items():
        for statement in policy['Statement']:
            actions = statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']]
            assert '*' not in actions
            if statement['Resource'] == '*':
                assert key == by_kind['execution'] and actions == ['ecr:GetAuthorizationToken']
    task = policies[by_kind['task']]
    actions = {action for s in task['Statement'] for action in (s['Action'] if isinstance(s['Action'], list) else [s['Action']])}
    assert actions == {'dynamodb:GetItem', 'dynamodb:Scan', 'dynamodb:UpdateItem', 'dynamodb:ConditionCheckItem',
                       'cognito-idp:AdminGetUser', 'cognito-idp:AdminDeleteUser', 'kms:Decrypt'}
    for name in ('CognitoUserPoolId', 'RegistryStorageKeyArn', 'ImageRepositoryArn', 'ClusterArn'):
        assert 'Default' not in template['Parameters'][name]
    assert not any(r['Type'] in {'AWS::ECS::Service', 'AWS::ECS::TaskDefinition', 'AWS::Events::Rule'} for r in resources.values())
    if output:
        output = Path(output)
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        for kind, key in by_kind.items():
            target = output / (kind + '.json')
            target.write_text(json.dumps(policies[key], indent=2))
            os.chmod(target, 0o600)
        target = output / 'fixtures.json'
        target.write_text(json.dumps(params))
        os.chmod(target, 0o600)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template')
    parser.add_argument('--review-output')
    args = parser.parse_args()
    inspect(args.template, args.review_output)
