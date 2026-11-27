"""Reject expansion of the opt-in meal refresh authority boundaries."""
import copy
import hashlib
import json
import re
import sys
from pathlib import Path


def values(value):
    return value if isinstance(value, list) else [value]


def check(template, account):
    resources = template['Resources']
    assert {r['Type'] for r in resources.values()} <= {'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::CDK::Metadata'}
    roles = {k: v for k, v in resources.items() if v['Type'] == 'AWS::IAM::Role'}
    policies = [v for v in resources.values() if v['Type'] == 'AWS::IAM::Policy']
    assert len(roles) == len(policies) == 3
    names = {r['Properties']['RoleName']: k for k, r in roles.items()}
    assert set(names) == {f'foodtech-meal-refresh-{kind}-dev' for kind in ('task', 'execution', 'invoke')}
    role_ids = {kind: names[f'foodtech-meal-refresh-{kind}-dev'] for kind in ('task', 'execution', 'invoke')}
    by_role = {}
    for resource in policies:
        props = resource['Properties']
        assert len(props['Roles']) == 1
        role = props['Roles'][0]['Ref']
        assert role in roles and role not in by_role
        by_role[role] = resource
    for kind, role_id in role_ids.items():
        props = roles[role_id]['Properties']
        assert not props.get('ManagedPolicyArns') and not props.get('Policies')
        statements = props['AssumeRolePolicyDocument']['Statement']
        assert len(statements) == 1
        trust = statements[0]
        assert trust['Effect'] == 'Allow' and trust['Action'] == 'sts:AssumeRole'
        expected = {'StringEquals': {'aws:SourceAccount': account}}
        if kind == 'invoke':
            assert trust['Principal'] == {'Service': 'events.amazonaws.com'}
            expected['ArnEquals'] = {'aws:SourceArn': {'Ref': 'ScheduleRuleArn'}}
        else:
            assert trust['Principal'] == {'Service': 'ecs-tasks.amazonaws.com'}
            expected['ArnLike'] = {'aws:SourceArn': f'arn:aws:ecs:ap-southeast-2:{account}:*'}
        assert trust['Condition'] == expected
    arn = f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-'
    auth, cooking = arn + 'auth-sessions-dev', arn + 'cooking-jobs-dev'
    meals, results = 'SCOPE#USER#*#COMBI_MEAL#*', 'SCOPE#USER#*#MEAL_AGGREGATE#*'
    task = by_role[role_ids['task']]
    statements = task['Properties']['PolicyDocument']['Statement']
    assert len(statements) == 8
    expected = [
        ({'dynamodb:DescribeTable'}, {auth, cooking}, None, False),
        ({'dynamodb:GetItem', 'dynamodb:ConditionCheckItem'}, {auth}, ['USER#*'], False),
        ({'dynamodb:GetItem', 'dynamodb:ConditionCheckItem'}, {cooking}, [meals, results], False),
        ({'dynamodb:Query'}, {cooking}, [meals], False),
        ({'dynamodb:Query'}, {cooking + '/index/MealAggregatePending'},
         [f'PENDING#{n:02x}' for n in range(16)], False),
        ({'dynamodb:PutItem'}, {cooking}, [meals, results, 'SCOPE#USER#*#MEAL_AGGREGATE_GROUPS'], True),
        ({'dynamodb:UpdateItem', 'dynamodb:DeleteItem'}, {cooking}, [meals], True),
        ({'sts:GetCallerIdentity'}, {'*'}, None, False),
    ]
    for statement, (actions, targets, keys, transaction) in zip(statements, expected, strict=True):
        assert statement['Effect'] == 'Allow'
        assert set(values(statement['Action'])) == actions and set(values(statement['Resource'])) == targets
        condition = {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}}
        if keys is not None:
            condition.update({'ForAllValues:StringLike': {'dynamodb:LeadingKeys': keys},
                              'Null': {'dynamodb:LeadingKeys': 'false'}})
        if transaction:
            condition['StringEquals']['dynamodb:EnclosingOperation'] = 'TransactWriteItems'
        assert statement['Condition'] == condition
    baseline = Path(__file__).resolve().parents[2] / 'infra/cdk/bin/meal-refresh-baseline.json'
    assert task['Metadata']['GeneratedBaselineSha256'] == hashlib.sha256(baseline.read_bytes()).hexdigest()
    generated = {a for s in json.loads(baseline.read_text())['Statement'] if s['Effect'] == 'Allow' for a in values(s['Action'])}
    assert all(set(values(s['Action'])) <= generated for s in statements)
    execution = by_role[role_ids['execution']]['Properties']['PolicyDocument']['Statement']
    assert len(execution) == 3
    execution_expected = [({'ecr:GetAuthorizationToken'}, '*'),
        ({'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer'}, f'arn:aws:ecr:ap-southeast-2:{account}:repository/foodtech/maintenance-dev'),
        ({'logs:CreateLogStream', 'logs:PutLogEvents'}, f'arn:aws:logs:ap-southeast-2:{account}:log-group:/foodtech/dev/meal-refresh:log-stream:worker/worker/*')]
    for statement, (actions, target) in zip(execution, execution_expected, strict=True):
        assert statement['Effect'] == 'Allow' and set(values(statement['Action'])) == actions
        assert statement['Resource'] == target
        assert statement['Condition'] == {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}}
    invoke = by_role[role_ids['invoke']]['Properties']['PolicyDocument']['Statement']
    assert len(invoke) == 2
    assert invoke[0] == {'Action': 'ecs:RunTask', 'Effect': 'Allow',
        'Resource': f'arn:aws:ecs:ap-southeast-2:{account}:task-definition/foodtech-meal-refresh-dev:*',
        'Condition': {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}, 'ArnEquals': {'ecs:cluster': {'Ref': 'ClusterArn'}}}}
    assert invoke[1] == {'Action': 'iam:PassRole', 'Effect': 'Allow',
        'Resource': [{'Fn::GetAtt': [role_ids[k], 'Arn']} for k in ('task', 'execution')],
        'Condition': {'StringEquals': {'iam:PassedToService': 'ecs-tasks.amazonaws.com'}}}
    for name, good, bad in [
        ('ClusterArn', f'arn:aws:ecs:ap-southeast-2:{account}:cluster/reviewed', f'arn:aws:ecs:us-east-1:{account}:cluster/foreign'),
        ('ScheduleRuleArn', f'arn:aws:events:ap-southeast-2:{account}:rule/reviewed', f'arn:aws:events:ap-southeast-2:{account}:rule/custom-bus/unreviewed'),
    ]:
        param = template['Parameters'][name]
        assert 'Default' not in param and re.fullmatch(param['AllowedPattern'], good)
        assert not re.fullmatch(param['AllowedPattern'], bad)
    return role_ids, by_role


def negative_cases(template, account):
    def mutation(kind):
        candidate = copy.deepcopy(template)
        ids, docs = check(candidate, account)
        task = docs[ids['task']]['Properties']['PolicyDocument']['Statement']
        execution = docs[ids['execution']]['Properties']['PolicyDocument']['Statement']
        invoke = docs[ids['invoke']]['Properties']['PolicyDocument']['Statement']
        if kind == 0: task[1]['Action'].append('dynamodb:PutItem')
        elif kind == 1: task[1]['Resource'] = '*'
        elif kind == 2: del task[1]['Condition']['Null']
        elif kind == 3: del task[5]['Condition']['StringEquals']['dynamodb:EnclosingOperation']
        elif kind == 4: task.append({'Effect': 'Allow', 'Action': 'kms:Decrypt', 'Resource': '*'})
        elif kind == 5: task[4]['Condition']['ForAllValues:StringLike']['dynamodb:LeadingKeys'] = ['PENDING#*']
        elif kind == 6: candidate['Resources'][ids['task']]['Properties']['AssumeRolePolicyDocument']['Statement'][0]['Condition']['StringEquals']['aws:SourceAccount'] = '*'
        elif kind == 7: execution.append({'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue', 'Resource': '*'})
        elif kind == 8: invoke[1]['Resource'] = '*'
        elif kind == 9: invoke[0]['Resource'] = '*'
        elif kind == 10: invoke[1]['Condition']['StringEquals']['iam:PassedToService'] = 'lambda.amazonaws.com'
        elif kind == 11: del candidate['Resources'][ids['invoke']]['Properties']['AssumeRolePolicyDocument']['Statement'][0]['Condition']['ArnEquals']
        elif kind == 12: candidate['Resources']['UnexpectedTask'] = {'Type': 'AWS::ECS::TaskDefinition'}
        elif kind == 13: candidate['Resources'][ids['task']]['Properties']['ManagedPolicyArns'] = ['arn:aws:iam::aws:policy/AdministratorAccess']
        elif kind == 14: execution[2]['Resource'] = '*'
        return candidate
    for kind in range(15):
        try:
            check(mutation(kind), account)
        except AssertionError:
            continue
        raise AssertionError(f'unsafe variant accepted: {kind}')


def main():
    template = json.loads(Path(sys.argv[1]).read_text())
    check(template, sys.argv[2])
    negative_cases(template, sys.argv[2])
    print('Meal refresh IAM boundaries verified; 15 unsafe variants rejected.')


if __name__ == '__main__':
    main()
