"""Offline capability ceilings for catalog cleanup; not live authorization."""
import json
import re
import sys
from pathlib import Path


def main(path, account):
    template = json.loads(Path(path).read_text())
    resources = template['Resources']
    assert {r['Type'] for r in resources.values()} <= {
        'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy', 'AWS::CDK::Metadata'}
    boundary_id, = [k for k, v in resources.items() if v['Type'] == 'AWS::IAM::ManagedPolicy']
    boundary = resources[boundary_id]
    doc = boundary['Properties']['PolicyDocument']
    # IAM excludes whitespace; our ASCII compact form is a conservative bound.
    assert len(json.dumps(doc, separators=(',', ':'))) <= 6144
    # CloudFormation Ref placeholders can be shorter than their actual ARNs.
    # Check the rendered policy too (IAM managed-policy character quota).
    # https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html
    key = f'arn:aws:kms:ap-southeast-2:{account}:key/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    refs = {'RegistryStorageKeyArn':key, 'DynamoDbManagedKeyArn':key,
            'AWS::Partition':'aws', 'AWS::Region':'ap-southeast-2',
            'AWS::AccountId':account, 'AWS::URLSuffix':'amazonaws.com'}
    def render(value):
        if isinstance(value,list):return [render(v) for v in value]
        if isinstance(value,dict):
            if set(value)=={'Ref'}:return refs[value['Ref']]
            if set(value)=={'Fn::Join'}:
                separator,items=value['Fn::Join']
                return separator.join(render(item) for item in items)
            return {name:render(item) for name,item in value.items()}
        return value
    rendered = json.dumps(render(doc), separators=(',', ':'))
    assert 'Fn::' not in rendered and len(rendered) <= 6144, 'rendered boundary exceeds IAM size limit'
    purposes = boundary['Metadata']['PurposeBySid']
    statements = {purposes[s['Sid']]: s for s in doc['Statement']}
    assert len(statements) == 15
    roles = [v['Properties'] for v in resources.values() if v['Type'] == 'AWS::IAM::Role']
    assert len(roles) == 2
    task, = [r for r in roles if r['RoleName'] == 'foodtech-source-grant-deletion-task-dev']
    agent, = [r for r in roles if r['RoleName'] == 'foodtech-source-grant-deletion-execution-dev']
    assert task['PermissionsBoundary'] == {'Ref': boundary_id}
    assert 'PermissionsBoundary' not in agent
    for role in roles:
        trust, = role['AssumeRolePolicyDocument']['Statement']
        assert trust['Principal'] == {'Service': 'ecs-tasks.amazonaws.com'}
        assert trust['Action'] == 'sts:AssumeRole'
        assert trust['Condition'] == {'StringEquals': {'aws:SourceAccount': account},
            'ArnLike': {'aws:SourceArn': f'arn:aws:ecs:ap-southeast-2:{account}:*'}}
    table = lambda name: f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-{name}-dev'
    auth, registry, catalog = map(table, ['auth-sessions', 'deletion-registry', 'source-access'])
    for name, resource, actions in [
        ('NeverWriteAuthorization', auth, ['dynamodb:GetItem', 'dynamodb:Scan', 'dynamodb:DescribeTable', 'dynamodb:ConditionCheckItem']),
        ('NeverWriteRegistry', registry, ['dynamodb:GetItem', 'dynamodb:DescribeTable', 'dynamodb:ConditionCheckItem']),
        ('NeverCreateOrModifyGrant', catalog, ['dynamodb:GetItem', 'dynamodb:DescribeTable', 'dynamodb:DeleteItem']),
    ]:
        s = statements[name]
        assert s['Effect'] == 'Deny' and s['Resource'] == resource
        assert set(s['NotAction']) == set(actions)
    for name in ['ReadLockedPrincipalProjection', 'ReadRegistryStatusOnly', 'ReadGrantMetadataOnly']:
        s = statements[name]
        assert s['Action'] == 'dynamodb:GetItem'
        assert s['Condition']['StringEquals']['dynamodb:EnclosingOperation'] == 'TransactGetItems'
        assert s['Condition']['StringEquals']['dynamodb:Select'] == 'SPECIFIC_ATTRIBUTES'
        assert s['Condition']['Null'] == {'dynamodb:LeadingKeys': 'false', 'dynamodb:Attributes': 'false'}
    for name in ['CheckLockedPrincipalOnly', 'CheckRegistryStatusOnly']:
        s = statements[name]
        assert s['Action'] == 'dynamodb:ConditionCheckItem'
        assert s['Condition']['StringEquals']['dynamodb:ReturnValues'] == 'NONE'
        assert 'dynamodb:EnclosingOperation' not in s['Condition']['StringEquals']
    delete = statements['RemoveCatalogGrantWithinTransaction']
    assert delete['Resource'] == catalog and delete['Action'] == 'dynamodb:DeleteItem'
    assert delete['Condition']['StringEquals']['dynamodb:EnclosingOperation'] == 'TransactWriteItems'
    assert delete['Condition']['StringEquals']['dynamodb:ReturnValues'] == 'NONE'
    assert delete['Condition']['ForAllValues:StringLike'] == {'dynamodb:LeadingKeys': ['GRANT#*']}
    assert delete['Condition']['Null'] == {'dynamodb:LeadingKeys': 'false'}
    for s in doc['Statement']:
        if s['Effect'] != 'Allow':
            continue
        assert s['Condition']['StringEquals']['aws:RequestedRegion'] == 'ap-southeast-2'
        for operator, values in s['Condition'].items():
            if operator.startswith('ForAllValues:'):
                for key in values:
                    assert s['Condition']['Null'][key] == 'false'
        actions = s['Action'] if isinstance(s['Action'], list) else [s['Action']]
        assert not set(actions) & {'dynamodb:PutItem', 'dynamodb:UpdateItem', 'dynamodb:BatchWriteItem', 'iam:PassRole', 'ecs:RunTask'}
    for name, parameter, tables in [
        ('RegistrySseOnly', 'RegistryStorageKeyArn', ['foodtech-deletion-registry-dev']),
        ('ManagedTableSseOnly', 'DynamoDbManagedKeyArn', ['foodtech-auth-sessions-dev', 'foodtech-source-access-dev']),
    ]:
        s = statements[name]
        assert s['Action'] == 'kms:Decrypt' and s['Resource'] == {'Ref': parameter}
        condition = s['Condition']['StringEquals']
        assert condition['kms:EncryptionContext:aws:dynamodb:subscriberId'] == account
        actual = condition['kms:EncryptionContext:aws:dynamodb:tableName']
        assert (actual if isinstance(actual, list) else [actual]) == tables
        assert 'dynamodb.ap-southeast-2.' in json.dumps(condition['kms:ViaService'])
        definition = template['Parameters'][parameter]
        assert 'Default' not in definition
        assert re.fullmatch(definition['AllowedPattern'], f'arn:aws:kms:ap-southeast-2:{account}:key/' + 'a' * 36)
        assert not re.fullmatch(definition['AllowedPattern'], 'arn:aws:kms:ap-southeast-2:000000000000:key/' + 'a' * 36)
    policies = [v['Properties'] for v in resources.values() if v['Type'] == 'AWS::IAM::Policy']
    assert len(policies) == 2
    generated, = [p for p in policies if p['Roles'] == [{'Ref': next(k for k, v in resources.items() if v.get('Properties') is task)}]]
    baseline = json.loads(Path('bin/source-grant-deletion-baseline.json').read_text().replace('${ACCOUNT}', account))
    def normalized(document):
        document = json.loads(json.dumps(document))
        document.pop('Id', None)
        for statement in document['Statement']:
            for key in ['Action', 'Resource']:
                if not isinstance(statement[key], list):
                    statement[key] = [statement[key]]
        return document
    assert normalized(generated['PolicyDocument']) == normalized(baseline)
    execution, = [p for p in policies if p is not generated]
    actions = set()
    for s in execution['PolicyDocument']['Statement']:
        a = s['Action'] if isinstance(s['Action'], list) else [s['Action']]
        actions.update(a)
        assert s['Condition']['StringEquals']['aws:RequestedRegion'] == 'ap-southeast-2'
        if 'ecr:BatchGetImage' in a:
            assert s['Resource'] == f'arn:aws:ecr:ap-southeast-2:{account}:repository/foodtech/maintenance-dev'
        if 'logs:PutLogEvents' in a:
            assert '/foodtech/dev/source-grant-deletion:*' in json.dumps(s['Resource'])
    assert actions == {'ecr:GetAuthorizationToken', 'ecr:BatchGetImage', 'ecr:GetDownloadUrlForLayer',
        'ecr:BatchCheckLayerAvailability', 'logs:CreateLogStream', 'logs:PutLogEvents'}


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
