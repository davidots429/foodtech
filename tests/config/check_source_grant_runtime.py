"""Manual source cleanup definition: no runtime or effective-role assertion."""
import json
import re
import sys
from pathlib import Path


def main(path, account, mode='create'):
    template = json.loads(Path(path).read_text())
    resources = list(template['Resources'].values())
    allowed = {'AWS::ECS::TaskDefinition', 'AWS::CDK::Metadata'} | ({'AWS::Logs::LogGroup'} if mode == 'create' else set())
    assert {r['Type'] for r in resources} <= allowed
    task, = [r for r in resources if r['Type'] == 'AWS::ECS::TaskDefinition']
    definition = task['Properties']
    assert definition['Family'] == 'foodtech-source-grant-deletion-dev'
    assert definition['RequiresCompatibilities'] == ['FARGATE'] and definition['NetworkMode'] == 'awsvpc'
    assert definition['RuntimePlatform'] == {'OperatingSystemFamily':'LINUX','CpuArchitecture':'X86_64'}
    assert (definition['Cpu'],definition['Memory']) == ('256','512')
    assert definition['TaskRoleArn'] == {'Ref':'TaskRoleArn'} and definition['ExecutionRoleArn'] == {'Ref':'ExecutionRoleArn'}
    worker, = definition['ContainerDefinitions']
    assert worker['Name'] == 'worker' and worker['Image'] == {'Ref':'ImageUri'} and worker['Essential'] is True
    assert worker['EntryPoint'] == ['python','-m','foodtech_business_api.source_grant_deletion_worker']
    assert worker['User'] == '10001:10001' and worker['ReadonlyRootFilesystem'] is True and worker['Privileged'] is False
    assert worker['LinuxParameters'] == {'Capabilities':{'Drop':['ALL']}} and worker['StopTimeout'] == 30
    assert not any(worker.get(name) for name in ['Secrets','MountPoints','VolumesFrom','DockerSecurityOptions','PortMappings','Command'])
    env = {row['Name']:row['Value'] for row in worker['Environment']}
    assert len(env) == len(worker['Environment'])
    assert env == {'FOODTECH_SOURCE_GRANT_DELETION_ENABLED':'true',
        'FOODTECH_SOURCE_GRANT_DELETION_ROLE_ARN':{'Ref':'TaskRoleArn'},
        'FOODTECH_AUTH_TABLE':'foodtech-auth-sessions-dev','FOODTECH_SOURCE_ACCESS_TABLE':'foodtech-source-access-dev',
        'FOODTECH_DELETION_REGISTRY_TABLE':'foodtech-deletion-registry-dev',
        'FOODTECH_SOURCE_GRANT_PAGE_SIZE':'20','FOODTECH_SOURCE_GRANT_MAX_PAGES':'50',
        'FOODTECH_SOURCE_GRANT_MAX_CHANGES':'20','FOODTECH_SOURCE_GRANT_MAX_SECONDS':'600'}
    assert worker['LogConfiguration']['LogDriver'] == 'awslogs'
    options = worker['LogConfiguration']['Options']
    assert options['awslogs-region'] == 'ap-southeast-2' and options['awslogs-stream-prefix'] == 'worker'
    logs = [r for r in resources if r['Type'] == 'AWS::Logs::LogGroup']
    if mode == 'create':
        log, = logs
        logical_id, = [name for name, resource in template['Resources'].items() if resource is log]
        assert options['awslogs-group'] == {'Ref':logical_id}
        assert log['Properties']['LogGroupName'] == '/foodtech/dev/source-grant-deletion'
        assert log['Properties']['RetentionInDays'] == 7 and log['DeletionPolicy'] == log['UpdateReplacePolicy'] == 'Retain'
        assert 'com.aws.cloudformation.Context' in log['Metadata']
    else:
        assert not logs and options['awslogs-group'] == '/foodtech/dev/source-grant-deletion'
    for name, suffix in [('TaskRoleArn','task'),('ExecutionRoleArn','execution')]:
        p = template['Parameters'][name]
        assert 'Default' not in p
        good = f'arn:aws:iam::{account}:role/foodtech-source-grant-deletion-{suffix}-dev'
        assert re.fullmatch(p['AllowedPattern'],good)
        for bad in [good.replace(account,'000000000000'),good+'-other',good.replace('-'+suffix+'-','-other-')]:
            assert not re.fullmatch(p['AllowedPattern'],bad)
    image = template['Parameters']['ImageUri']
    assert 'Default' not in image and len(image['AllowedValues']) == 1
    assert re.fullmatch(rf'{account}\.dkr\.ecr\.ap-southeast-2\.amazonaws\.com/foodtech/maintenance-dev@sha256:[a-f0-9]{{64}}',image['AllowedValues'][0])
    assert template['Metadata']['AWSToolsMetrics']['AWSAgentToolkit'] == 'aws-cloudformation@3'
    assert task['Metadata']['com.aws.cloudformation.Context']['must']
    assert len(template['Description'].encode()) <= 1024


if __name__ == '__main__':
    main(*sys.argv[1:])
