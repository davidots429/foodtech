"""Incremental generated-reader/checker contract, not effective IAM proof."""
import hashlib
import json
import re
import sys
from pathlib import Path


def main(path, account):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    assert {r['Type'] for r in resources} <= {'AWS::IAM::Policy', 'AWS::CDK::Metadata'}
    policies = [r for r in resources if r['Type'] == 'AWS::IAM::Policy']
    assert len(policies) == 2
    baseline_file = Path(__file__).resolve().parents[2] / 'infra/cdk/bin/recipe-history-baseline.json'
    baseline = json.loads(baseline_file.read_text())
    digest = hashlib.sha256(baseline_file.read_bytes()).hexdigest()
    expected = {
        'reader': [('dynamodb:GetItem', 'auth-sessions', ['USER#*', 'CONTROL#COOKING_HISTORY_WRITERS']),
                   ('dynamodb:GetItem', 'cooking-jobs', ['SCOPE#USER#*#JOBS', 'SCOPE#USER#*#JOB#*']),
                   ('dynamodb:Query', 'cooking-jobs', ['SCOPE#USER#*#JOBS'])],
        'checker': [('dynamodb:ConditionCheckItem', 'auth-sessions', ['USER#*', 'CONTROL#COOKING_HISTORY_WRITERS']),
                    ('dynamodb:ConditionCheckItem', 'cooking-jobs', ['SCOPE#USER#*#JOBS']),
                    ('dynamodb:ConditionCheckItem', 'source-access', ['GRANT#*'])],
    }
    for resource in policies:
        props = resource['Properties']
        kind = props['PolicyName'].removeprefix('foodtech-recipe-history-').removesuffix('-dev')
        generated = {a for p in baseline['reader' if kind == 'reader' else 'conditions']['Policies']
                     for s in p['Policy']['Statement'] if s['Effect'] == 'Allow' for a in s['Action']}
        role = 'BrokerTaskRoleArn' if kind == 'reader' else 'GatewayTaskRoleArn'
        assert props['Roles'] == [{'Fn::Select': [1, {'Fn::Split': ['/', {'Ref': role}]}]}]
        statements = props['PolicyDocument']['Statement']
        assert len(statements) == 3
        assert resource['Metadata']['GeneratedBaselineSha256'] == digest
        for statement, (action, table, keys) in zip(statements, expected[kind], strict=True):
            assert action in generated
            assert statement['Effect'] == 'Allow' and statement['Action'] == action
            assert statement['Resource'] == f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-{table}-dev'
            equals = {'aws:RequestedRegion': 'ap-southeast-2'}
            if action == 'dynamodb:GetItem': equals['dynamodb:EnclosingOperation'] = 'TransactGetItems'
            elif action == 'dynamodb:Query': equals['dynamodb:Select'] = 'SPECIFIC_ATTRIBUTES'
            else: equals['dynamodb:ReturnValues'] = 'NONE'
            assert statement['Condition'] == {'ForAllValues:StringLike': {'dynamodb:LeadingKeys': keys},
                'Null': {'dynamodb:LeadingKeys': 'false'}, 'StringEquals': equals}
    for name in ['BrokerTaskRoleArn', 'GatewayTaskRoleArn']:
        parameter = template['Parameters'][name]
        assert 'Default' not in parameter
        assert re.fullmatch(parameter['AllowedPattern'], f'arn:aws:iam::{account}:role/reviewed')
        for bad in ['arn:aws:iam::000000000000:role/foreign', f'arn:aws:iam::{account}:role/path/nested']:
            assert not re.fullmatch(parameter['AllowedPattern'], bad)
    assert 'SeparateHistoryAuthorities' in template['Rules']


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
