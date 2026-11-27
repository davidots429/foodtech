"""Frozen IAM shape checks, not an effective-account permission proof."""
from copy import deepcopy
from fnmatch import fnmatchcase
import hashlib
import json
import re
import sys
from pathlib import Path


def validate(template, account):
    resources = template['Resources']
    assert {r['Type'] for r in resources.values()} <= {'AWS::IAM::Role', 'AWS::IAM::ManagedPolicy', 'AWS::IAM::Policy', 'AWS::CDK::Metadata'}
    roles = [(k, r) for k, r in resources.items() if r['Type'] == 'AWS::IAM::Role']
    boundaries = [(k, r) for k, r in resources.items() if r['Type'] == 'AWS::IAM::ManagedPolicy']
    assert len(roles) == len(boundaries) == 1
    role_id, role = roles[0]; boundary_id, boundary = boundaries[0]
    rp, bp = role['Properties'], boundary['Properties']
    assert rp['RoleName'] == 'foodtech-combi-source-read-dev'
    assert rp.get('PermissionsBoundary') == {'Ref': boundary_id}
    trust = rp['AssumeRolePolicyDocument']['Statement']
    assert len(trust) == 3
    assert trust[0] == {'Action': 'sts:AssumeRole', 'Effect': 'Allow', 'Principal': {'AWS': {'Ref': 'BrokerTaskRoleArn'}},
        'Condition': {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}, 'StringLike': {'sts:RoleSessionName': 'combi-*'}}}
    assert trust[1] == {'Action': 'sts:AssumeRole', 'Effect': 'Deny', 'Principal': {'AWS': {'Ref': 'GatewayTaskRoleArn'}}}
    assert trust[2] == {'Action': 'sts:TagSession', 'Effect': 'Deny', 'Principal': {'AWS': '*'}}
    assert 'Policies' not in rp
    assert rp['ManagedPolicyArns'] == [{'Ref': boundary_id}]
    read = bp['PolicyDocument']['Statement']
    assert len(read) == 1
    meal = 'SCOPE#USER#*#COMBI_MEAL#????????-????-????-????-????????????'
    roster = 'SCOPE#USER#*#COMBI_MEALS'
    regional = {'aws:RequestedRegion': 'ap-southeast-2'}
    def keys(values): return {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': values}, 'Null': {'dynamodb:LeadingKeys': 'false'}}
    def arn(table): return f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-{table}-dev'
    assert read[0] == {'Effect': 'Allow', 'Action': ['dynamodb:GetItem', 'dynamodb:Query'], 'Resource': arn('cooking-jobs'),
        'Condition': {**keys([meal, meal+'#SOURCE']), 'StringEquals': regional}}
    sample = 'SCOPE#USER#customer_1#COMBI_MEAL#00000000-0000-4000-8000-000000000001'
    patterns = read[0]['Condition']['ForAllValues:StringLike']['dynamodb:LeadingKeys']
    for key in (sample, sample+'#SOURCE'): assert any(fnmatchcase(key, p) for p in patterns)
    for key in (sample+'#LIVE', sample+'#SOURCE#EXTRA', 'SCOPE#USER#customer_1#COMBI_MEALS',
                'JEYUK#jeyuk-batch', 'USER#customer_1', 'GRANT#x'):
        assert not any(fnmatchcase(key, p) for p in patterns)
    baseline_file = Path(__file__).resolve().parents[2]/'infra/cdk/bin/combi-read-baseline.json'
    baseline = json.loads(baseline_file.read_text()); digest = hashlib.sha256(baseline_file.read_bytes()).hexdigest()
    policies = [r for r in resources.values() if r['Type'] == 'AWS::IAM::Policy']
    assert len(policies) == 2
    for resource in (role, boundary, *policies): assert resource['DeletionPolicy'] == resource['UpdateReplacePolicy'] == 'Retain'
    for resource in policies:
        props = resource['Properties']; broker = props['PolicyName'] == 'foodtech-combi-read-broker-dev'
        expected_name = 'foodtech-combi-read-broker-dev' if broker else 'foodtech-combi-read-conditions-dev'
        assert props['PolicyName'] == expected_name
        parameter = 'BrokerTaskRoleArn' if broker else 'GatewayTaskRoleArn'
        assert props['Roles'] == [{'Fn::Select': [1, {'Fn::Split': ['/', {'Ref': parameter}]}]}]
        assert resource['Metadata']['GeneratedBaselineSha256'] == digest
        statements = props['PolicyDocument']['Statement']
        assert len(statements) == (4 if broker else 2)
        generated = {a for p in baseline['issuer_history' if broker else 'conditions']['Policies']
            for s in p['Policy']['Statement'] if s['Effect'] == 'Allow' for a in s['Action']}
        expected = [('auth-sessions', ['USER#*', 'CONTROL#COMBI_HISTORY_WRITERS']), ('cooking-jobs', [roster, meal])]
        if broker:
            assert statements[0] == {'Action': 'sts:AssumeRole', 'Effect': 'Allow', 'Resource': {'Fn::GetAtt': [role_id, 'Arn']},
                'Condition': {'StringEquals': regional, 'StringLike': {'sts:RoleSessionName': 'combi-*'}}}
            statements = statements[1:]
            expected.append(('cooking-jobs', [roster, meal]))
        for index, (statement, (table, values)) in enumerate(zip(statements, expected, strict=True)):
            action = ('dynamodb:Query' if index == 2 else 'dynamodb:GetItem') if broker else 'dynamodb:ConditionCheckItem'
            equals = {**regional, **({'dynamodb:Select': 'SPECIFIC_ATTRIBUTES'} if index == 2 else
                {'dynamodb:EnclosingOperation': 'TransactGetItems'})} if broker else {**regional, 'dynamodb:ReturnValues': 'NONE'}
            assert action in generated
            assert statement == {'Effect': 'Allow', 'Action': action, 'Resource': arn(table),
                'Condition': {**keys(values), 'StringEquals': equals}}
    assert 'SeparateCombiAuthorities' in template['Rules']
    for parameter in ('BrokerTaskRoleArn', 'GatewayTaskRoleArn'):
        p = template['Parameters'][parameter]
        assert 'Default' not in p and re.fullmatch(p['AllowedPattern'], f'arn:aws:iam::{account}:role/reviewed')
        assert not re.fullmatch(p['AllowedPattern'], f'arn:aws:iam::{account}:role/path/nested')


def main(path, account):
    template = json.loads(Path(path).read_text()); validate(template, account)
    role_id = next(k for k, r in template['Resources'].items() if r['Type'] == 'AWS::IAM::Role')
    rejected = 0
    for mutation in ('boundary', 'gateway_trust', 'tag_deny', 'source_region', 'source_null', 'source_keys', 'gateway_return'):
        value = deepcopy(template); role = value['Resources'][role_id]['Properties']
        if mutation == 'boundary': role.pop('PermissionsBoundary')
        elif mutation == 'gateway_trust': role['AssumeRolePolicyDocument']['Statement'][1]['Effect'] = 'Allow'
        elif mutation == 'tag_deny': role['AssumeRolePolicyDocument']['Statement'].pop()
        elif mutation.startswith('source_'):
            boundary_id = next(k for k, r in value['Resources'].items() if r['Type'] == 'AWS::IAM::ManagedPolicy')
            condition = value['Resources'][boundary_id]['Properties']['PolicyDocument']['Statement'][0]['Condition']
            if mutation == 'source_region': condition['StringEquals']['aws:RequestedRegion'] = '*'
            elif mutation == 'source_null': condition.pop('Null')
            else: condition['ForAllValues:StringLike']['dynamodb:LeadingKeys'].append('*')
        else: value['Resources']['GatewayConditions']['Properties']['PolicyDocument']['Statement'][0]['Condition']['StringEquals']['dynamodb:ReturnValues'] = 'ALL_OLD'
        try: validate(value, account)
        except AssertionError: rejected += 1
        else: raise AssertionError('unsafe mutation accepted: '+mutation)
    assert rejected == 7
    print('Combi IAM definition and 7 unsafe mutations checked; effective access remains unverified')


if __name__ == '__main__': main(sys.argv[1], sys.argv[2])
