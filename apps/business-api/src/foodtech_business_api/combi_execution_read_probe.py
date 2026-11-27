"""Finite intended-role authorization probe against a reviewed absent execution key.

No data preparation, writes, STS source lease, user session or model calls.
Run only after separate trial approval and verifying the probe key is absent.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
import os
import re
import signal
import sys
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError,ClientError
from .combi_execution_reader import EXECUTION_ATTRIBUTES


@dataclass(frozen=True,repr=False)
class ProbeConfiguration:
    account: str
    role_id: str
    probe_id: str

    def __repr__(self): return 'ProbeConfiguration()'


def configuration(env):
    if env.get('FOODTECH_COMBI_EXECUTION_READ_PROBE_ENABLED')!='true' or \
            env.get('AWS_EXECUTION_ENV')!='AWS_ECS_FARGATE' or env.get('FOODTECH_STAGE')!='dev':
        raise ValueError('explicit dev Fargate probe required')
    account=env.get('FOODTECH_COMBI_EXECUTION_PROBE_ACCOUNT','')
    role_id=env.get('FOODTECH_COMBI_EXECUTION_PROBE_ROLE_ID','')
    identifier=env.get('FOODTECH_COMBI_EXECUTION_PROBE_ID','')
    if not re.fullmatch('[0-9]{12}',account) or not re.fullmatch('[A-Z0-9]{16,128}',role_id): raise ValueError
    parsed=UUID(identifier)
    if str(parsed)!=identifier or parsed.version!=4: raise ValueError
    return ProbeConfiguration(account,role_id,identifier)


def requests(probe):
    table='foodtech-ai-execution-registry-dev'
    base={'TableName':table,'Key':{'pk':{'S':'EXEC#'+probe.probe_id},'sk':{'S':'META'}},'ConsistentRead':True,
        'ProjectionExpression':', '.join('#e'+str(i) for i in range(len(EXECUTION_ATTRIBUTES))),
        'ExpressionAttributeNames':{'#e'+str(i):name for i,name in enumerate(EXECUTION_ATTRIBUTES)}}
    keys=deepcopy(base);keys.update(ProjectionExpression='pk, sk');keys.pop('ExpressionAttributeNames')
    full=deepcopy(base);full.pop('ProjectionExpression');full.pop('ExpressionAttributeNames')
    forbidden=[]
    for field in ('model_id','credential_id'):
        args=deepcopy(base);args['ProjectionExpression']+=', #forbidden';args['ExpressionAttributeNames']['#forbidden']=field
        forbidden.append(('forbidden_'+field,False,'get_item',args))
    foreign=deepcopy(base);foreign['Key']['pk']['S']='MODEL#COMBI_ROLEPROBE#'+probe.probe_id
    transaction=deepcopy(base);transaction.pop('ConsistentRead')
    query={'TableName':table,'KeyConditionExpression':'pk = :probe','ExpressionAttributeValues':{':probe':base['Key']['pk']},
        'ProjectionExpression':base['ProjectionExpression'],'ExpressionAttributeNames':base['ExpressionAttributeNames'],
        'ConsistentRead':True,'Limit':1}
    return [('metadata_projection',True,'get_item',base),('key_projection',True,'get_item',keys),
        ('no_projection',False,'get_item',full),*forbidden,('other_partition',False,'get_item',foreign),
        ('transaction_read',False,'transact_get_items',{'TransactItems':[{'Get':transaction}]}),
        ('query',False,'query',query)]


def _run(client,probe):
    accepted,denied=[],[]
    for name,allow,operation,args in requests(probe):
        try: response=getattr(client,operation)(**args)
        except ClientError as error:
            # A timeout/throttle/validation error is never authorization evidence.
            if allow or error.response.get('Error',{}).get('Code')!='AccessDeniedException': raise
            denied.append(name)
        else:
            # Never continue to broader negative requests if the reviewed absent
            # key unexpectedly has data. Do not print returned fields or values.
            if not allow or type(response) is not dict or response.get('Item') not in (None,{}):
                raise ValueError('unexpected probe response')
            accepted.append(name)
    return {'schema':'combi-execution-read-probe-v1','expected_role_observed':True,
        'accepted_reads':accepted,'expected_denials':denied,'source_session_verified':False,'model_invocation_verified':False}


def main():
    try: probe=configuration(os.environ)
    except (ValueError,TypeError,AttributeError):
        print('Explicit valid dev Fargate execution-read probe configuration is required.',file=sys.stderr)
        return 2
    clients=[]
    previous=None
    try:
        def deadline(_signum,_frame): raise TimeoutError('probe deadline reached')
        previous=signal.signal(signal.SIGALRM,deadline)
        signal.alarm(180)
        session=boto3.Session(region_name='ap-southeast-2')
        credentials=session.get_credentials()
        if credentials is None or credentials.method!='container-role': raise ValueError
        config=Config(connect_timeout=5,read_timeout=10,proxies={},ignore_configured_endpoint_urls=True,
            account_id_endpoint_mode='disabled',retries={'mode':'standard','total_max_attempts':1})
        sts=session.client('sts',region_name='ap-southeast-2',config=config,endpoint_url='https://sts.ap-southeast-2.amazonaws.com')
        clients.append(sts)
        if sts.meta.endpoint_url!='https://sts.ap-southeast-2.amazonaws.com': raise ValueError
        identity=sts.get_caller_identity()
        match=re.fullmatch(r'arn:aws:sts::'+probe.account+r':assumed-role/foodtech-recipe-broker-dev/([A-Za-z0-9+=,.@_-]{2,64})',identity.get('Arn',''))
        if identity.get('Account')!=probe.account or not match or identity.get('UserId')!=probe.role_id+':'+match[1]: raise ValueError
        client=session.client('dynamodb',region_name='ap-southeast-2',config=config,
            endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        clients.append(client)
        if client.meta.endpoint_url!='https://dynamodb.ap-southeast-2.amazonaws.com': raise ValueError
        result=_run(client,probe)
    except Exception:
        print('Execution metadata read probe failed; no writes or model calls were attempted.',file=sys.stderr)
        return 1
    finally:
        if previous is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM,previous)
        for client in clients:
            try: client.close()
            except (BotoCoreError,ClientError,OSError): pass
    print(json.dumps(result,sort_keys=True))
    return 0


if __name__=='__main__': raise SystemExit(main())
