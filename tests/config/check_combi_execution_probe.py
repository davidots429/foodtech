"""Finite manual definition: reject additional services, capabilities or arbitrary targets."""
import json
from pathlib import Path
import re
import sys


def main(path,account):
    t=json.loads(Path(path).read_text());resources=list(t['Resources'].values())
    assert {r['Type'] for r in resources}<={'AWS::ECS::TaskDefinition','AWS::CDK::Metadata'}
    tasks=[r for r in resources if r['Type']=='AWS::ECS::TaskDefinition'];assert len(tasks)==1
    task=tasks[0];p=task['Properties']
    assert p['Family']=='foodtech-combi-execution-read-probe-dev'
    assert p['RequiresCompatibilities']==['FARGATE'] and p['NetworkMode']=='awsvpc'
    assert p['Cpu']=='256' and p['Memory']=='512'
    assert p['TaskRoleArn']=={'Ref':'TaskRoleArn'} and p['ExecutionRoleArn']=={'Ref':'ExecutionRoleArn'}
    assert p['RuntimePlatform']=={'OperatingSystemFamily':'LINUX','CpuArchitecture':'X86_64'}
    c,=p['ContainerDefinitions']
    assert set(c)=={'Name','Image','Essential','User','ReadonlyRootFilesystem','Privileged','StopTimeout','LinuxParameters',
        'EntryPoint','Environment','LogConfiguration'}
    assert c['Image']=={'Ref':'ImageUri'} and c['Name']=='worker' and c['User']=='10001:10001'
    assert c['Essential'] is True and c['ReadonlyRootFilesystem'] is True and c['Privileged'] is False
    assert c['StopTimeout']==30 and c['LinuxParameters']=={'Capabilities':{'Drop':['ALL']}}
    assert c['EntryPoint']==['python','-m','foodtech_business_api.combi_execution_read_probe']
    assert {v['Name']:v['Value'] for v in c['Environment']}=={
        'FOODTECH_COMBI_EXECUTION_READ_PROBE_ENABLED':'true','FOODTECH_STAGE':'dev',
        'FOODTECH_COMBI_EXECUTION_PROBE_ACCOUNT':account,'FOODTECH_COMBI_EXECUTION_PROBE_ROLE_ID':{'Ref':'BrokerRoleId'},
        'FOODTECH_COMBI_EXECUTION_PROBE_ID':{'Ref':'ProbeId'},'PYTHONDONTWRITEBYTECODE':'1'}
    assert c['LogConfiguration']=={'LogDriver':'awslogs','Options':{'awslogs-group':'/foodtech/dev/meal-refresh',
        'awslogs-region':'ap-southeast-2','awslogs-stream-prefix':'worker'}}
    values={'TaskRoleArn':f'arn:aws:iam::{account}:role/foodtech-recipe-broker-dev',
        'ExecutionRoleArn':f'arn:aws:iam::{account}:role/foodtech-meal-refresh-execution-dev',
        'BrokerRoleId':'AROASYNTHETICBROKERID','ProbeId':'a55fe880-d10f-4f81-997f-d8a5c97ae147',
        'ImageUri':f'{account}.dkr.ecr.ap-southeast-2.amazonaws.com/foodtech/maintenance-dev@sha256:'+'a'*64}
    for name,value in values.items():
        param=t['Parameters'][name]
        assert 'Default' not in param and re.fullmatch(param['AllowedPattern'],value)
    for name,bad in (('TaskRoleArn',values['ExecutionRoleArn']),('ExecutionRoleArn',values['TaskRoleArn']),
        ('ImageUri',values['ImageUri'].replace('@sha256:'+'a'*64,':latest')),
        ('ProbeId',values['ProbeId'].upper()),('BrokerRoleId','invalid')):
        assert not re.fullmatch(t['Parameters'][name]['AllowedPattern'],bad)
    assert task['Metadata']['com.aws.cloudformation.Context']['must']
    assert t['Metadata']['AWSToolsMetrics']['AWSAgentToolkit']=='aws-cloudformation@3'
    assert 0<len(t['Description'].encode())<=1024


if __name__=='__main__': main(sys.argv[1],sys.argv[2])
