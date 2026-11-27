"""Check manual meal refresh boundaries without running or granting access."""
from collections import Counter
import json
from pathlib import Path
import re
import sys


def check(path, mode='create'):
    assert mode in {'create', 'existing'}
    template = json.loads(Path(path).read_text())
    resources = template['Resources']
    counts = Counter(r['Type'] for r in resources.values())
    counts.pop('AWS::CDK::Metadata', None)
    assert counts == +Counter({'AWS::ECS::TaskDefinition': 1, 'AWS::Logs::LogGroup': int(mode == 'create')})
    task, = [r['Properties'] for r in resources.values() if r['Type'] == 'AWS::ECS::TaskDefinition']
    assert task['Family'] == 'foodtech-meal-refresh-dev'
    assert task['RequiresCompatibilities'] == ['FARGATE'] and task['NetworkMode'] == 'awsvpc'
    assert task['Cpu'] == '256' and task['Memory'] == '512'
    assert task['RuntimePlatform'] == {'OperatingSystemFamily': 'LINUX', 'CpuArchitecture': 'X86_64'}
    assert task['TaskRoleArn'] == {'Ref': 'TaskRoleArn'}
    assert task['ExecutionRoleArn'] == {'Ref': 'ExecutionRoleArn'}
    container, = task['ContainerDefinitions']
    assert container['EntryPoint'] == ['python', '-m', 'foodtech_business_api.meal_refresh_runtime']
    assert container['Image'] == {'Ref': 'ImageUri'}
    assert container['User'] == '10001:10001' and container['ReadonlyRootFilesystem'] is True
    assert container['Privileged'] is False and container['StopTimeout'] == 30
    assert container['LinuxParameters'] == {'Capabilities': {'Drop': ['ALL']}}
    assert not any(container.get(k) for k in ('Secrets', 'MountPoints', 'PortMappings', 'Command'))
    assert {v['Name']: v['Value'] for v in container['Environment']} == {
        'FOODTECH_MEAL_REFRESH_ENABLED': 'true', 'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED': 'true',
        'FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN': {'Ref': 'TaskRoleArn'},
        'FOODTECH_MEAL_REFRESH_PAGE_SIZE': '25', 'FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD': '100',
        'FOODTECH_MEAL_REFRESH_MAX_SECONDS': '600', 'FOODTECH_COOKING_TABLE': 'foodtech-cooking-jobs-dev',
        'FOODTECH_AUTH_TABLE': 'foodtech-auth-sessions-dev'}
    params = template['Parameters']
    pattern = params['TaskRoleArn']['AllowedPattern']
    account = re.search(r'::([0-9]{12}):', pattern).group(1)
    for name, kind in (('TaskRoleArn', 'task'), ('ExecutionRoleArn', 'execution')):
        assert params[name]['AllowedPattern'] == f'^arn:aws:iam::{account}:role/foodtech-meal-refresh-{kind}-dev$'
        assert 'Default' not in params[name]
    image = f'{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/maintenance-dev'
    image_pattern = params['ImageUri']['AllowedPattern']
    assert 'Default' not in params['ImageUri']
    assert re.fullmatch(image_pattern, image + '@sha256:' + 'a' * 64)
    for bad in (image + ':latest', image.replace(account, '999999999999') + '@sha256:' + 'a' * 64,
                image.replace('maintenance-dev', 'collection-dev') + '@sha256:' + 'a' * 64):
        assert not re.fullmatch(image_pattern, bad)
    options = container['LogConfiguration']['Options']
    assert options['awslogs-region'] == 'ap-southeast-2' and options['awslogs-stream-prefix'] == 'worker'
    if mode == 'existing':
        assert options['awslogs-group'] == '/foodtech/dev/meal-refresh'
    else:
        group_id, = [k for k, v in resources.items() if v['Type'] == 'AWS::Logs::LogGroup']
        group = resources[group_id]
        assert options['awslogs-group'] == {'Ref': group_id}
        assert group['Properties']['LogGroupName'] == '/foodtech/dev/meal-refresh'
        assert group['Properties']['RetentionInDays'] == 7
        assert group['DeletionPolicy'] == group['UpdateReplacePolicy'] == 'Retain'
    assert {'WorkerTaskDefinitionArn', 'WorkerLogGroupName'} <= template['Outputs'].keys()
    print('Manual finite worker, exact roles/digest, strict guards and registration-only resources verified.')


if __name__ == '__main__':
    check(*sys.argv[1:])
