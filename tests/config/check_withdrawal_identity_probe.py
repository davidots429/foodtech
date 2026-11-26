"""Check manual read-probe registration without granting or executing access."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


def check(path, existing_logs):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    counts = Counter(value['Type'] for value in resources)
    counts.pop('AWS::CDK::Metadata', None)
    assert counts == +Counter({'AWS::ECS::TaskDefinition': 1, 'AWS::Logs::LogGroup': int(not existing_logs)})
    task, = [value['Properties'] for value in resources if value['Type'] == 'AWS::ECS::TaskDefinition']
    assert task['Family'] == 'foodtech-withdrawal-identity-probe-dev'
    assert task['RequiresCompatibilities'] == ['FARGATE'] and task['NetworkMode'] == 'awsvpc'
    assert task['Cpu'] == '256' and task['Memory'] == '512'
    assert task['TaskRoleArn'] == {'Ref': 'TaskRoleArn'}
    assert task['ExecutionRoleArn'] == {'Ref': 'ExecutionRoleArn'}
    assert task['RuntimePlatform'] == {'OperatingSystemFamily': 'LINUX', 'CpuArchitecture': 'X86_64'}
    container, = task['ContainerDefinitions']
    assert container['EntryPoint'] == ['python', '-m', 'foodtech_business_api.withdrawal_identity_probe']
    assert container['Image'] == {'Ref': 'ImageUri'}
    assert container['User'] == '10001:10001' and container['ReadonlyRootFilesystem'] is True
    env = {value['Name']: value['Value'] for value in container['Environment']}
    account = env['FOODTECH_PROBE_ACCOUNT']
    assert re.fullmatch('[0-9]{12}', account)
    assert env == {'FOODTECH_PROBE_ACCOUNT': account, 'FOODTECH_PROBE_ID': {'Ref': 'ProbeId'},
                   'FOODTECH_IDENTITY_READ_PROBE_ENABLED': 'true'}
    assert not container.get('Secrets') and not container.get('MountPoints') and not container.get('Privileged')
    options = container['LogConfiguration']['Options']
    assert options['awslogs-stream-prefix'] == 'read-probe' and options['awslogs-region'] == 'ap-southeast-2'
    if existing_logs:
        assert options['awslogs-group'] == '/foodtech/dev/withdrawal-identity'
    else:
        group_id, = [key for key, value in template['Resources'].items() if value['Type'] == 'AWS::Logs::LogGroup']
        group = template['Resources'][group_id]
        assert options['awslogs-group'] == {'Ref': group_id}
        assert group['Properties']['LogGroupName'] == '/foodtech/dev/withdrawal-identity'
        assert group['Properties']['RetentionInDays'] == 7
        assert group['DeletionPolicy'] == group['UpdateReplacePolicy'] == 'Retain'
    params = template['Parameters']
    for name in ('TaskRoleArn', 'ExecutionRoleArn', 'ProbeId', 'ImageUri'):
        assert 'Default' not in params[name]
    for kind, name in (('task', 'TaskRoleArn'), ('execution', 'ExecutionRoleArn')):
        assert params[name]['AllowedPattern'] == f'^arn:aws:iam::{account}:role/foodtech-withdrawal-identity-{kind}-dev$'
    image = f'{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/maintenance-dev'
    assert re.fullmatch(params['ImageUri']['AllowedPattern'], image + '@sha256:' + 'a' * 64)
    assert not re.fullmatch(params['ImageUri']['AllowedPattern'], image + ':latest')
    assert not re.fullmatch(params['ImageUri']['AllowedPattern'], image.replace(account, '999999999999') + '@sha256:' + 'a' * 64)
    assert re.fullmatch(params['ProbeId']['AllowedPattern'], 'a55fe880-d10f-4f81-997f-d8a5c97ae147')
    assert not re.fullmatch(params['ProbeId']['AllowedPattern'], 'real-account')
    assert {'ProbeTaskDefinitionArn', 'IdentityLogGroupName'} <= set(template['Outputs'])
    print('Read-only command registration, exact roles/image and absence of execution resources verified.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('template')
    parser.add_argument('--existing-logs', action='store_true')
    args = parser.parse_args()
    check(args.template, args.existing_logs)
