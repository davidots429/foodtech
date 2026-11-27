"""Reject widening of the incremental broker GetItem policy and target role."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys


def check(template,account):
    resources=list(template['Resources'].values())
    assert {r['Type'] for r in resources}<={'AWS::IAM::Policy','AWS::CDK::Metadata'}
    policies=[r for r in resources if r['Type']=='AWS::IAM::Policy']
    assert len(policies)==1
    policy=policies[0];props=policy['Properties']
    assert props['PolicyName']=='foodtech-combi-execution-reader-dev'
    assert props['Roles']==[{'Fn::Select':[1,{'Fn::Split':['/',{'Ref':'BrokerTaskRoleArn'}]}]}]
    attributes=['pk','sk','execution_id','tenant_id','feature','data_classification','job_id','job_version',
        'prior_job_id','prior_job_version','comparison_id','jeyuk_source_revision','prior_source_revision','combi_binding','state','version']
    assert props['PolicyDocument']=={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'dynamodb:GetItem',
        'Resource':f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-ai-execution-registry-dev',
        'Condition':{'ForAllValues:StringLike':{'dynamodb:LeadingKeys':['EXEC#????????-????-????-????-????????????']},
            'ForAllValues:StringEquals':{'dynamodb:Attributes':attributes},
            'Null':{'dynamodb:LeadingKeys':'false','dynamodb:Attributes':'false','dynamodb:EnclosingOperation':'true'},
            'StringEquals':{'aws:RequestedRegion':'ap-southeast-2','dynamodb:Select':'SPECIFIC_ATTRIBUTES'}}}]}
    parameter=template['Parameters']['BrokerTaskRoleArn']
    assert 'Default' not in parameter
    assert re.fullmatch(parameter['AllowedPattern'],f'arn:aws:iam::{account}:role/foodtech-recipe-broker-dev')
    for name in ('gateway','business-api','foodtech-combi-source-read-dev','foodtech-recipe-source-read-dev','path/foodtech-recipe-broker-dev','foodtech-recipe-broker-prod'):
        assert not re.fullmatch(parameter['AllowedPattern'],f'arn:aws:iam::{account}:role/{name}')
    assert not re.fullmatch(parameter['AllowedPattern'],'arn:aws:iam::000000000000:role/foodtech-recipe-broker-dev')
    root=Path(__file__).resolve().parents[2]
    baseline=root/'infra/cdk/bin/combi-execution-read-baseline.json'
    actions={a for p in json.loads(baseline.read_text())['Policies'] for s in p['Policy']['Statement'] if s['Effect']=='Allow' for a in s['Action']}
    assert 'dynamodb:GetItem' in actions
    assert policy['Metadata']['GeneratedBaselineSha256']==hashlib.sha256(baseline.read_bytes()).hexdigest()
    assert policy['DeletionPolicy']==policy['UpdateReplacePolicy']=='Retain'
    assert policy['Metadata']['com.aws.cloudformation.Context']['must']
    assert template['Metadata']['AWSToolsMetrics']['AWSAgentToolkit']=='aws-cloudformation@3'
    assert 0<len(template['Description'].encode())<=1024
    # Cross-language allowlist must follow the actual SDK reader, not an
    # independently edited list that silently broadens privileges.
    source=ast.parse((root/'apps/business-api/src/foodtech_business_api/combi_execution_reader.py').read_text())
    values=[ast.literal_eval(n.value) for n in source.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='EXECUTION_ATTRIBUTES' for t in n.targets)]
    assert values==[tuple(attributes)]


def main():
    template=json.loads(Path(sys.argv[1]).read_text())
    check(template,sys.argv[2])


if __name__=='__main__': main()
