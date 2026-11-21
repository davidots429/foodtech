"""Check disabled-by-default meal refresh deployment boundaries."""
import json
import sys
from pathlib import Path


def main(path: str) -> None:
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
    main(sys.argv[1])
