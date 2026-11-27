"""Check disabled-by-default meal refresh deployment boundaries."""
import json
import re
import sys
from pathlib import Path


def main(path: str, expected_mode: str = 'false') -> None:
    assert expected_mode in {'true', 'false'}
    template = json.loads(Path(path).read_text())
    resources = template['Resources'].values()
    tasks = [r['Properties'] for r in resources if r['Type'] == 'AWS::ECS::TaskDefinition']
    rules = [r['Properties'] for r in template['Resources'].values() if r['Type'] == 'AWS::Events::Rule']
    assert len(tasks) == 1 and len(rules) == 2
    task = tasks[0]
    rule, = [r for r in rules if 'ScheduleExpression' in r]
    stopped, = [r for r in rules if 'EventPattern' in r]
    assert stopped['EventPattern']['detail']['group'] == ['foodtech-meal-refresh-dev']
    assert stopped['EventPattern']['detail']['lastStatus'] == ['STOPPED']
    metrics = [r['Properties'] for r in template['Resources'].values() if r['Type'] == 'AWS::Logs::MetricFilter']
    assert len(metrics) == 1 and 'exitCode NOT EXISTS' in metrics[0]['FilterPattern']
    assert len([r for r in template['Resources'].values() if r['Type'] == 'AWS::CloudWatch::Alarm']) == 1
    assert task['TaskRoleArn'] == {'Ref': 'TaskRoleArn'}
    assert task['ExecutionRoleArn'] == {'Ref': 'ExecutionRoleArn'}
    assert task['NetworkMode'] == 'awsvpc' and task['RequiresCompatibilities'] == ['FARGATE']
    container, = task['ContainerDefinitions']
    assert container['Image'] == {'Ref': 'ImageUri'} and container['User'] == '10001:10001'
    assert container['ReadonlyRootFilesystem'] is True
    assert container['Privileged'] is False and container['StopTimeout'] == 30
    assert container['LinuxParameters'] == {'Capabilities': {'Drop': ['ALL']}}
    env = {item['Name']: item['Value'] for item in container['Environment']}
    assert env['FOODTECH_PRINCIPAL_PROVISIONING_ENABLED'] == expected_mode
    assert env['FOODTECH_MEAL_REFRESH_ENABLED'] == 'true'
    assert env['FOODTECH_MEAL_REFRESH_TASK_ROLE_ARN'] == {'Ref': 'TaskRoleArn'}
    assert env['FOODTECH_MEAL_REFRESH_MAX_SECONDS'] == '600'
    assert env['FOODTECH_MEAL_REFRESH_PAGE_SIZE'] == '25'
    assert env['FOODTECH_MEAL_REFRESH_MAX_PAGES_PER_SHARD'] == '100'
    assert 'foodtech-meal-refresh-task-' in template['Parameters']['TaskRoleArn']['AllowedPattern']
    pattern = template['Parameters']['TaskRoleArn']['AllowedPattern']
    account = re.search(r'::([0-9]{12}):', pattern).group(1)
    assert re.fullmatch(pattern, f'arn:aws:iam::{account}:role/foodtech-meal-refresh-task-dev')
    other = '000000000000' if account != '000000000000' else '111111111111'
    assert not re.fullmatch(pattern, f'arn:aws:iam::{other}:role/foodtech-meal-refresh-task-dev')
    separate = template['Rules']['SeparateMealRefreshRoles']['Assertions'][0]['Assert']['Fn::And']
    assert len(separate) == 3
    assert rule['State'] == 'DISABLED'
    target, = rule['Targets']
    assert target['RoleArn'] == {'Ref': 'InvokeRoleArn'}
    assert target['DeadLetterConfig']['Arn'] == {'Ref': 'DeliveryDlqArn'}
    assert target['RetryPolicy']['MaximumRetryAttempts'] == 3
    assert target['EcsParameters']['LaunchType'] == 'FARGATE'
    assert target['EcsParameters']['Group'] == 'foodtech-meal-refresh-dev'
    network = target['EcsParameters']['NetworkConfiguration']['AwsVpcConfiguration']
    assert network['AssignPublicIp'] == 'DISABLED'
    assert network['Subnets'] == {'Ref': 'SubnetIds'} and network['SecurityGroups'] == {'Ref': 'SecurityGroupIds'}
    assert template['Parameters']['ImageUri']['AllowedPattern'].endswith('@sha256:[a-f0-9]{64}$')
    assert all(key not in template['Parameters'][name] for name in ('TaskRoleArn', 'InvokeRoleArn', 'IntervalMinutes')
               for key in ('Default',))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'false')
