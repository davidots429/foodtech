"""Check the opt-in collection publisher, never deploy it."""
import json
from pathlib import Path
import re
import sys


def check(path, mode):
    assert mode in {'create', 'existing'}
    template = json.loads(Path(path).read_text())
    resources = template['Resources']
    assert {x['Type'] for x in resources.values()} <= {
        'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::ECR::Repository', 'AWS::CDK::Metadata'}
    role_id, = [k for k, r in resources.items() if r['Type'] == 'AWS::IAM::Role']
    role = resources[role_id]['Properties']
    assert role['RoleName'] == 'foodtech-collection-publish-dev' and role['MaxSessionDuration'] == 3600
    assert not role.get('ManagedPolicyArns') and not role.get('PermissionsBoundary')
    trust, = role['AssumeRolePolicyDocument']['Statement']
    assert trust['Action'] == 'sts:AssumeRoleWithWebIdentity' and trust['Effect'] == 'Allow'
    assert trust['Principal'] == {'Federated': {'Ref': 'GitHubOidcProviderArn'}}
    assert trust['Condition'] == {'StringEquals': {'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
        'token.actions.githubusercontent.com:sub': {'Ref': 'VerifiedGitHubSubject'}}}
    params = template['Parameters']
    assert all('Default' not in params[name] for name in ('VerifiedGitHubSubject', 'GitHubOidcProviderArn'))
    assert set(params['VerifiedGitHubSubject']['AllowedValues']) == {'repo:davidots429/foodtech:ref:refs/heads/main',
        'repo:davidots429@182712727/foodtech@1439543152:ref:refs/heads/main'}
    account = re.search(r'::([0-9]{12}):', params['GitHubOidcProviderArn']['AllowedPattern']).group(1)
    assert params['GitHubOidcProviderArn']['AllowedPattern'] == (
        f'^arn:aws:iam::{account}:oidc-provider/token[.]actions[.]githubusercontent[.]com$')
    expected_arn = f'arn:aws:ecr:ap-southeast-2:{account}:repository/foodtech/collection-dev'
    repositories = [(k, r) for k, r in resources.items() if r['Type'] == 'AWS::ECR::Repository']
    assert len(repositories) == int(mode == 'create')
    if repositories:
        _, repo = repositories[0]
        assert repo['DeletionPolicy'] == repo['UpdateReplacePolicy'] == 'Retain'
        assert repo['Properties']['RepositoryName'] == 'foodtech/collection-dev'
        assert repo['Properties']['ImageTagMutability'] == 'IMMUTABLE'
        assert repo['Properties']['ImageScanningConfiguration'] == {'ScanOnPush': True}
        assert repo['Properties']['EncryptionConfiguration'] == {'EncryptionType': 'AES256'}

    def resolve(value):
        if isinstance(value, str): return value
        if value == {'Ref': 'AWS::Partition'}: return 'aws'
        if set(value) == {'Fn::GetAtt'}:
            logical, attribute = value['Fn::GetAtt']
            assert resources[logical]['Type'] == 'AWS::ECR::Repository' and attribute == 'Arn'
            return expected_arn
        separator, parts = value['Fn::Join']
        return separator.join(resolve(x) for x in parts)

    binding, = [x['Properties'] for x in resources.values() if x['Type'] == 'AWS::IAM::Policy']
    assert binding['Roles'] == [{'Ref': role_id}]
    statements = binding['PolicyDocument']['Statement']
    assert len(statements) == 2
    actions = set()
    for statement in statements:
        assert statement['Effect'] == 'Allow'
        assert statement['Condition'] == {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}}
        current = statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']]
        actions.update(current)
        if statement['Resource'] == '*':
            assert current == ['ecr:GetAuthorizationToken']
        else:
            assert resolve(statement['Resource']) == expected_arn and 'ecr:GetAuthorizationToken' not in current
    assert actions == {'ecr:GetAuthorizationToken', 'ecr:BatchCheckLayerAvailability', 'ecr:InitiateLayerUpload',
        'ecr:UploadLayerPart', 'ecr:CompleteLayerUpload', 'ecr:PutImage', 'ecr:BatchGetImage',
        'ecr:DescribeImages', 'ecr:DescribeRepositories'}


if __name__ == '__main__':
    check(*sys.argv[1:])
