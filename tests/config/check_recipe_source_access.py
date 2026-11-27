"""Check candidate broker/read role boundaries; not live AWS authorization."""
import json
import re
import sys
from pathlib import Path


def main(path, account, broker_mode="existing"):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    assert {r['Type'] for r in resources} <= {'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::CDK::Metadata'}
    assert broker_mode in {'existing', 'create'}
    roles = {key: r['Properties'] for key, r in template['Resources'].items() if r['Type'] == 'AWS::IAM::Role'}
    assert len(roles) == (2 if broker_mode == 'create' else 1)
    role_id, role = next((key, value) for key, value in roles.items() if value['RoleName'] == 'foodtech-recipe-source-read-dev')
    broker_arn = {'Ref': 'BrokerTaskRoleArn'}
    if broker_mode == 'create':
        broker_id, broker = next((key, value) for key, value in roles.items() if value['RoleName'] == 'foodtech-recipe-broker-dev')
        assert 'Policies' not in broker and 'ManagedPolicyArns' not in broker
        assert broker['AssumeRolePolicyDocument']['Statement'] == [{
            'Action': 'sts:AssumeRole', 'Effect': 'Allow', 'Principal': {'Service': 'ecs-tasks.amazonaws.com'},
            'Condition': {'StringEquals': {'aws:SourceAccount': account},
                'ArnLike': {'aws:SourceArn': f'arn:aws:ecs:ap-southeast-2:{account}:*'}}}]
        assert broker_id in template['Resources'][role_id]['DependsOn']
        assert 'BrokerTaskRoleArn' not in template['Parameters']
        broker_arn = f'arn:aws:iam::{account}:role/foodtech-recipe-broker-dev'
        assert template['Outputs']['ReviewedBrokerTaskRoleArn']['Value'] == broker_arn
        assert 'Fn::GetAtt' not in json.dumps(template['Rules'])
    assert role['RoleName'] == 'foodtech-recipe-source-read-dev'
    policies = [r['Properties'] for r in resources if r['Type'] == 'AWS::IAM::Policy']
    assert len(policies) == 2
    trust = role['AssumeRolePolicyDocument']['Statement']
    allow, = [s for s in trust if s['Effect'] == 'Allow']
    assert allow['Principal']['AWS'] == broker_arn
    assert allow['Action'] == 'sts:AssumeRole'
    assert allow['Condition']['StringEquals'] == {
        'aws:PrincipalArn': broker_arn, 'aws:RequestedRegion': 'ap-southeast-2'}
    assert allow['Condition']['StringLike'] == {'sts:RoleSessionName': 'recipe-*'}
    denied = [s for s in trust if s['Effect'] == 'Deny']
    assert any(s['Principal'] == {'AWS': {'Ref': 'GatewayTaskRoleArn'}} and
        s['Action'] == 'sts:AssumeRole' for s in denied)
    assert any(s['Principal'] == {'AWS': '*'} and s['Action'] == 'sts:TagSession' for s in denied)
    read, = [p for p in policies if all(s['Action'] != 'sts:AssumeRole'
        for s in p['PolicyDocument']['Statement'])]
    statements = read['PolicyDocument']['Statement']
    assert len(statements) == 2
    for s, table, keys in zip(statements, ['cooking-jobs', 'data-flow'],
            [['SCOPE#USER#*#JOB#*', 'JEYUK#jeyuk-*'], ['JEYUK#jeyuk-*']], strict=True):
        assert sorted(s['Action']) == ['dynamodb:GetItem', 'dynamodb:Query']
        assert s['Resource'] == f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-{table}-dev'
        assert s['Condition'] == {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': keys},
            'Null': {'dynamodb:LeadingKeys': 'false'}, 'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}}
    caller, = [p for p in policies if p is not read]
    assert caller['Roles'] == ([{'Ref': broker_id}] if broker_mode == 'create' else
        [{'Fn::Select': [1, {'Fn::Split': ['/', {'Ref': 'BrokerTaskRoleArn'}]}]}])
    if broker_mode == 'create':
        assert caller['Roles'] == [{'Ref': broker_id}]  # Ref enforces creation ordering.
    statement, catalog = caller['PolicyDocument']['Statement']
    assert statement['Action'] == 'sts:AssumeRole'
    assert statement['Resource'] == template['Outputs']['RecipeSourceReadRoleArn']['Value']
    assert statement['Condition'] == {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'},
        'StringLike': {'sts:RoleSessionName': 'recipe-*'}}
    assert catalog == {'Effect': 'Allow', 'Action': 'dynamodb:GetItem',
        'Resource': f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-source-access-dev',
        'Condition': {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2', 'dynamodb:EnclosingOperation': 'TransactGetItems'},
            'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['GRANT#*']}, 'Null': {'dynamodb:LeadingKeys': 'false'}}}
    for name in (['GatewayTaskRoleArn'] if broker_mode == 'create' else ['BrokerTaskRoleArn', 'GatewayTaskRoleArn']):
        definition = template['Parameters'][name]
        assert 'Default' not in definition
        pattern = definition['AllowedPattern']
        assert re.fullmatch(pattern, f'arn:aws:iam::{account}:role/reviewed-task')
        assert not re.fullmatch(pattern, 'arn:aws:iam::000000000000:role/foreign')
        assert not re.fullmatch(pattern, f'arn:aws:iam::{account}:role/path/nested')
    assert 'DistinctSourceAuthorities' in template['Rules']


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "existing")
