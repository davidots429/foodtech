"""Reject publisher privileges beyond the reviewed main-branch image boundary."""
import argparse
import json
from pathlib import Path


def check(path, create_provider):
    template = json.loads(Path(path).read_text())
    resources = template['Resources']
    assert all(value['Type'] in {'AWS::IAM::Role', 'AWS::IAM::Policy',
        'AWS::IAM::OIDCProvider', 'AWS::CDK::Metadata'} for value in resources.values())
    role, = [value['Properties'] for value in resources.values() if value['Type'] == 'AWS::IAM::Role']
    assert role['RoleName'] == 'foodtech-maintenance-publish-dev' and role['MaxSessionDuration'] == 3600
    assert not role.get('ManagedPolicyArns')
    trust, = role['AssumeRolePolicyDocument']['Statement']
    assert trust['Action'] == 'sts:AssumeRoleWithWebIdentity' and trust['Effect'] == 'Allow'
    assert trust['Condition'] == {'StringEquals': {
        'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
        'token.actions.githubusercontent.com:sub': {'Ref': 'VerifiedGitHubSubject'}}}
    subject = template['Parameters']['VerifiedGitHubSubject']
    assert 'Default' not in subject
    assert set(subject['AllowedValues']) == {'repo:davidots429/foodtech:ref:refs/heads/main',
        'repo:davidots429@182712727/foodtech@1439543152:ref:refs/heads/main'}
    providers = {key: value for key, value in resources.items() if value['Type'] == 'AWS::IAM::OIDCProvider'}
    assert len(providers) == int(create_provider)
    if create_provider:
        key, value = next(iter(providers.items()))
        assert value['Properties']['ClientIdList'] == ['sts.amazonaws.com']
        assert value['Properties']['Url'] == 'https://token.actions.githubusercontent.com'
        assert set(value['Properties']) <= {'ClientIdList', 'Url', 'Tags'}
        assert value['DeletionPolicy'] == value['UpdateReplacePolicy'] == 'Retain'
        assert trust['Principal'] == {'Federated': {'Fn::GetAtt': [key, 'Arn']}}
        assert 'GitHubOidcProviderArn' not in template['Parameters']
    else:
        assert trust['Principal'] == {'Federated': {'Ref': 'GitHubOidcProviderArn'}}
        assert 'Default' not in template['Parameters']['GitHubOidcProviderArn']
    policy, = [value['Properties']['PolicyDocument'] for value in resources.values() if value['Type'] == 'AWS::IAM::Policy']
    statements = policy['Statement']
    assert len(statements) == 2
    actions = set()
    for statement in statements:
        assert statement['Effect'] == 'Allow'
        assert statement['Condition'] == {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}}
        current = statement['Action']
        current = [current] if isinstance(current, str) else current
        actions.update(current)
        if statement['Resource'] == '*':
            assert current == ['ecr:GetAuthorizationToken']
        else:
            assert 'ecr:GetAuthorizationToken' not in current
            value = statement['Resource']
            if isinstance(value, dict):
                separator, parts = value['Fn::Join']
                value = separator.join('aws' if part == {'Ref': 'AWS::Partition'} else part for part in parts)
            assert value.startswith('arn:aws:ecr:ap-southeast-2:') and value.endswith(':repository/foodtech/maintenance-dev')
            assert '*' not in value
    assert actions == {'ecr:GetAuthorizationToken', 'ecr:BatchCheckLayerAvailability', 'ecr:InitiateLayerUpload',
        'ecr:UploadLayerPart', 'ecr:CompleteLayerUpload', 'ecr:PutImage', 'ecr:BatchGetImage', 'ecr:DescribeImages'}
    print('Manual publisher trust and single-repository privilege boundary verified; no AWS role applied.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template')
    parser.add_argument('--create-provider', action='store_true')
    args = parser.parse_args()
    check(args.template, args.create_provider)
