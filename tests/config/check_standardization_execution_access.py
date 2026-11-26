"""Check ECS-agent-only access; never grant business SDK permissions here."""
import json
import sys
from pathlib import Path


def main(path, account):
    resources = json.loads(Path(path).read_text())['Resources'].values()
    assert all(r['Type'] in {'AWS::IAM::Role', 'AWS::IAM::Policy', 'AWS::CDK::Metadata'} for r in resources)
    role, = [r['Properties'] for r in resources if r['Type'] == 'AWS::IAM::Role']
    assert role['RoleName'] == 'foodtech-standardization-execution-dev'
    assert not role.get('ManagedPolicyArns')
    trust, = role['AssumeRolePolicyDocument']['Statement']
    assert trust['Principal'] == {'Service': 'ecs-tasks.amazonaws.com'}
    assert trust['Condition']['StringEquals']['aws:SourceAccount'] == account
    assert trust['Condition']['ArnLike']['aws:SourceArn'] == f'arn:aws:ecs:ap-southeast-2:{account}:*'
    policy, = [r['Properties']['PolicyDocument'] for r in resources if r['Type'] == 'AWS::IAM::Policy']
    seen = {}
    for statement in policy['Statement']:
        assert statement['Effect'] == 'Allow'
        assert statement['Condition'] == {'StringEquals': {'aws:RequestedRegion': 'ap-southeast-2'}}
        actions = statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']]
        for action in actions:
            assert action not in seen
            seen[action] = statement['Resource']
    repo = f'arn:aws:ecr:ap-southeast-2:{account}:repository/foodtech/maintenance-dev'
    log = f'arn:aws:logs:ap-southeast-2:{account}:log-group:/foodtech/dev/standardization:log-stream:worker/worker/*'
    assert seen == {'ecr:GetAuthorizationToken': '*', 'ecr:BatchGetImage': repo,
        'ecr:GetDownloadUrlForLayer': repo, 'logs:CreateLogStream': log, 'logs:PutLogEvents': log}


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
