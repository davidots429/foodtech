"""Check disabled-by-default meal refresh deployment boundaries."""
import json
import sys
from pathlib import Path


def main(path: str) -> None:
    template = json.loads(Path(path).read_text())
    resources = template['Resources'].values()
    tasks = [r['Properties'] for r in resources if r['Type'] == 'AWS::ECS::TaskDefinition']
    rules = [r['Properties'] for r in template['Resources'].values() if r['Type'] == 'AWS::Events::Rule']
    assert len(tasks) == 1 and len(rules) == 1
    task, rule = tasks[0], rules[0]
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
    network = target['EcsParameters']['NetworkConfiguration']['AwsVpcConfiguration']
    assert network['AssignPublicIp'] == 'DISABLED'
    assert network['Subnets'] == {'Ref': 'SubnetIds'} and network['SecurityGroups'] == {'Ref': 'SecurityGroupIds'}
    assert template['Parameters']['ImageUri']['AllowedPattern'].endswith('@sha256:[a-f0-9]{64}$')
    assert all(key not in template['Parameters'][name] for name in ('TaskRoleArn', 'InvokeRoleArn', 'IntervalMinutes')
               for key in ('Default',))


if __name__ == '__main__':
    main(sys.argv[1])
