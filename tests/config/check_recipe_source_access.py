"""Check candidate broker/read role boundaries; not live AWS authorization."""
import json
import re
import sys
from pathlib import Path


def main(path, account):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    assert {r['Type'] for r in resources} <= {'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::CDK::Metadata'}
    role, = [r['Properties'] for r in resources if r['Type'] == 'AWS::IAM::Role']
    assert role['RoleName'] == 'foodtech-recipe-source-read-dev'
    policies = [r['Properties'] for r in resources if r['Type'] == 'AWS::IAM::Policy']
    assert len(policies) == 2
    trust = role['AssumeRolePolicyDocument']['Statement']
    allow, = [s for s in trust if s['Effect'] == 'Allow']
    assert allow['Principal']['AWS'] == {'Ref': 'BrokerTaskRoleArn'}
    assert allow['Action'] == 'sts:AssumeRole'
    assert allow['Condition']['StringEquals'] == {
        'aws:PrincipalArn': {'Ref': 'BrokerTaskRoleArn'}, 'aws:RequestedRegion': 'ap-southeast-2'}
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
    assert caller['Roles'] == [{'Fn::Select': [1, {'Fn::Split': ['/', {'Ref': 'BrokerTaskRoleArn'}]}]}]
    statement, catalog = caller['PolicyDocument']['Statement']
    assert statement['Action'] == 'sts:AssumeRole'
    assert statement['Resource'] == template['Outputs']['RecipeSourceReadRoleArn']['Value']
    assert statement['Condition'] == {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'},
        'StringLike': {'sts:RoleSessionName': 'recipe-*'}}
    assert catalog == {'Effect': 'Allow', 'Action': 'dynamodb:GetItem',
        'Resource': f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-source-access-dev',
        'Condition': {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2', 'dynamodb:EnclosingOperation': 'TransactGetItems'},
            'ForAllValues:StringLike': {'dynamodb:LeadingKeys': ['GRANT#*']}, 'Null': {'dynamodb:LeadingKeys': 'false'}}}
    for name in ['BrokerTaskRoleArn', 'GatewayTaskRoleArn']:
        definition = template['Parameters'][name]
        assert 'Default' not in definition
        pattern = definition['AllowedPattern']
        assert re.fullmatch(pattern, f'arn:aws:iam::{account}:role/reviewed-task')
        assert not re.fullmatch(pattern, 'arn:aws:iam::000000000000:role/foreign')
        assert not re.fullmatch(pattern, f'arn:aws:iam::{account}:role/path/nested')
    assert 'DistinctSourceAuthorities' in template['Rules']


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
