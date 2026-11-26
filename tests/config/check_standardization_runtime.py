"""Require a finite manual worker with reviewed roles and no IAM/network creation."""
import json
import sys
from pathlib import Path


def main(path, account):
    template = json.loads(Path(path).read_text())
    resources = template['Resources'].values()
    assert all(r['Type'] in {'AWS::ECS::TaskDefinition', 'AWS::Logs::LogGroup', 'AWS::CDK::Metadata'} for r in resources)
    task, = [r['Properties'] for r in resources if r['Type'] == 'AWS::ECS::TaskDefinition']
    assert task['Family'] == 'foodtech-standardization-dev'
    assert task['Cpu'] == '256' and task['Memory'] == '512'
    assert task['NetworkMode'] == 'awsvpc' and task['RequiresCompatibilities'] == ['FARGATE']
    assert task['RuntimePlatform'] == {'CpuArchitecture': 'X86_64', 'OperatingSystemFamily': 'LINUX'}
    assert task['TaskRoleArn'] == {'Ref': 'TaskRoleArn'} and task['ExecutionRoleArn'] == {'Ref': 'ExecutionRoleArn'}
    params = template['Parameters']
    assert len(params['TaskRoleArn']['AllowedValues']) == 1
    assert 'StandardizationWorkerTask' in params['TaskRoleArn']['AllowedValues'][0]
    for name in ('TaskRoleArn', 'ExecutionRoleArn', 'ImageUri'):
        assert 'Default' not in params[name]
    container, = task['ContainerDefinitions']
    assert container['User'] == '10001:10001' and container['ReadonlyRootFilesystem'] is True
    assert container['EntryPoint'] == ['python', '-m', 'foodtech_standardization_worker.runtime']
    assert not container.get('Command') and not container.get('Secrets') and not container.get('MountPoints')
    assert container['Image'] == {'Ref': 'ImageUri'}
    env = {item['Name']: item['Value'] for item in container['Environment']}
    assert env['FOODTECH_WORKER_ACCOUNT'] == account and env['FOODTECH_WORKER_STAGE'] == 'dev'
    assert env['FOODTECH_STANDARDIZATION_RUNTIME_ENABLED'] == 'true'
    assert env['FOODTECH_PRINCIPAL_PROVISIONING_ENABLED'] == 'true'
    assert env['FOODTECH_WORKER_MAX_SECONDS'] == '600'
    assert env['FOODTECH_WORKER_TASK_ROLE_ARN'] == {'Ref': 'TaskRoleArn'}
    assert env['FOODTECH_ORDERED_QUEUE_URL'] == f'https://sqs.ap-southeast-2.amazonaws.com/{account}/foodtech-ordered-dev.fifo'
    assert env['FOODTECH_STANDARDIZATION_DLQ_URL'] == f'https://sqs.ap-southeast-2.amazonaws.com/{account}/foodtech-standardizationdlq-dev.fifo'
    for r in resources:
        if r['Type'] == 'AWS::Logs::LogGroup':
            assert r['Properties']['RetentionInDays'] == 7 and r['DeletionPolicy'] == 'Retain'


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
