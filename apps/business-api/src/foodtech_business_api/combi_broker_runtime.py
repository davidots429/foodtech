"""Explicit independent broker container identity and owned SDK clients."""
from datetime import datetime,timezone
import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError,ClientError
from .combi_delegation import CombiInvocationDelegation
from .combi_execution_reader import DynamoCombiExecutionReader
from .combi_prior_history import DynamoCombiPriorHistoryAuthority
from .combi_read_broker import CombiReadBroker
from .combi_read_delivery import CombiDeliverySettings
from .recipe_scope import RecipeScopeDenied


class CombiBrokerRuntime:
    def __init__(self,broker,reader,history,gateway,clients):
        self.broker,self.reader,self.history,self.gateway=broker,reader,history,gateway
        self._clients=clients

    def __repr__(self): return 'CombiBrokerRuntime()'

    def close(self):
        clients,self._clients=self._clients,[]
        seen=set()
        for client in clients:
            if id(client) in seen: continue
            seen.add(id(client))
            try: client.close()
            except (BotoCoreError,ClientError,OSError): pass


def configured_combi_broker_runtime(delegation):
    clients=[]
    try:
        if type(delegation) is not CombiInvocationDelegation: raise ValueError
        delegation.require_live(time.time())
        env=os.environ;stage=env.get('FOODTECH_STAGE','')
        arn=env.get('FOODTECH_COMBI_BROKER_TASK_ROLE_ARN','')
        role_id=env.get('FOODTECH_COMBI_BROKER_TASK_ROLE_ID','')
        match=re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/foodtech-recipe-broker-([a-z0-9-]{1,32})',arn)
        if not match or match[2]!=stage or not re.fullmatch('[A-Z0-9]{16,128}',role_id) or \
                env.get('FOODTECH_COMBI_BROKER_ENABLED')!='true' or env.get('AWS_EXECUTION_ENV')!='AWS_ECS_FARGATE' or \
                env.get('FOODTECH_PRINCIPAL_PROVISIONING_ENABLED')!='true' or \
                env.get('FOODTECH_AI_REGISTRY_TABLE')!='foodtech-ai-execution-registry-'+stage or \
                env.get('FOODTECH_AUTH_TABLE')!='foodtech-auth-sessions-'+stage:
            raise ValueError
        gateway=CombiDeliverySettings(env['FOODTECH_COMBI_GATEWAY_URL'],env['FOODTECH_COMBI_GATEWAY_SERVER_CERT'],
            env['FOODTECH_COMBI_BROKER_CLIENT_CERT'],env['FOODTECH_COMBI_BROKER_CLIENT_KEY'])
        session=boto3.Session(region_name='ap-southeast-2')
        credentials=session.get_credentials()
        if credentials is None or credentials.method!='container-role': raise ValueError
        config=Config(connect_timeout=5,read_timeout=15,proxies={},ignore_configured_endpoint_urls=True,
            account_id_endpoint_mode='disabled',retries={'mode':'standard','total_max_attempts':2})
        sts=session.client('sts',region_name='ap-southeast-2',config=config,endpoint_url='https://sts.ap-southeast-2.amazonaws.com')
        clients.append(sts)
        if sts.meta.endpoint_url!='https://sts.ap-southeast-2.amazonaws.com': raise ValueError
        identity=sts.get_caller_identity()
        assumed=re.fullmatch(r'arn:aws:sts::'+match[1]+r':assumed-role/foodtech-recipe-broker-'+stage+r'/([A-Za-z0-9+=,.@_-]{2,64})',identity.get('Arn',''))
        if not assumed or identity.get('Account')!=match[1] or identity.get('UserId')!=role_id+':'+assumed[1]: raise ValueError
        client=session.client('dynamodb',region_name='ap-southeast-2',config=config,
            endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        clients.append(client)
        if client.meta.endpoint_url!='https://dynamodb.ap-southeast-2.amazonaws.com': raise ValueError
        reader=DynamoCombiExecutionReader(table_name=env['FOODTECH_AI_REGISTRY_TABLE'],client=client,
            subject=delegation.subject,epoch=delegation.epoch)
        history=DynamoCombiPriorHistoryAuthority(auth_table=env['FOODTECH_AUTH_TABLE'],cooking_table='foodtech-cooking-jobs-'+stage,
            account_id=match[1],client=client,clock=time.time)
        broker=CombiReadBroker(account=match[1],stage=stage,read_role_arn=f'arn:aws:iam::{match[1]}:role/foodtech-combi-source-read-{stage}',
            sts=sts,clock=lambda:datetime.now(timezone.utc))
        delegation.require_live(time.time())
        return CombiBrokerRuntime(broker,reader,history,gateway,clients)
    except (ValueError,TypeError,KeyError,AttributeError,BotoCoreError,ClientError,OSError):
        for client in clients:
            try: client.close()
            except (BotoCoreError,ClientError,OSError): pass
        raise RecipeScopeDenied('combi broker runtime unavailable') from None
