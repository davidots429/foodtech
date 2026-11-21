"""Opt-in AI coordinator check; requires merged deletion progress modules."""
import os
from uuid import uuid4
from boto3.dynamodb.conditions import Key
import boto3
from foodtech_business_api.deletion_progress import DeletionProgressStore
from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_ai_gateway.tenant_inventory_capture import TenantInventoryCapture
from foodtech_ai_gateway.tenant_partition_worker import TenantPartitionWorker
from foodtech_ai_gateway.execution_quiescence import ExecutionQuiescence
from foodtech_ai_gateway.credential_deletion_plan import CredentialDeletionPlan
from foodtech_ai_gateway.ai_deletion_coordinator import AiDeletionCoordinator
if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_AI_COORDINATOR_LIVE') != '1':
 raise SystemExit('explicit development profile and FOODTECH_AI_COORDINATOR_LIVE=1 required')
s=boto3.Session(profile_name='foodtech',region_name='ap-southeast-2')
if s.client('sts').get_caller_identity()['Account']!='573693340100':raise RuntimeError('unexpected account')
db=s.resource('dynamodb');auth=db.Table('foodtech-auth-sessions-dev');registry=db.Table('foodtech-cooking-jobs-dev');source=db.Table('foodtech-ai-execution-registry-dev');vault=db.Table('foodtech-ai-credential-vault-dev');progress=db.Table('foodtech-data-flow-dev')
outputs=s.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs'];key=next(x['OutputValue'] for x in outputs if x['OutputKey']=='CredentialStorageKeyArn')
u,q,x=str(uuid4()),str(uuid4()),uuid4().hex;ak={'pk':f'USER#{u}'};rk={'pk':'REGISTRY','sk':f'REQ#{q}'};sk={'pk':f'CONSENT#{x}','sk':'META'}
created=[]
if progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']:raise RuntimeError('request collision')
try:
 for table,item in [(auth,{**ak,'suspended':True,'deletion_lock_request':q}),(registry,{**rk,'status':'DELETING'}),(source,{**sk,'tenant_id':u})]:
  table.put_item(Item=item,ConditionExpression='attribute_not_exists(pk)');created.append((table,{k:item[k] for k in ('pk','sk') if k in item}))
 store=DeletionProgressStore(auth_table=auth.name,registry_table=registry.name,progress_table=progress.name,client=s.client('dynamodb'))
 cipher=DeletionCheckpointCipher(key,'dev',source_table_arn=source.table_arn,kms_client=s.client('kms'))
 capture=TenantInventoryCapture(source_table=source,progress_store=store,cipher=cipher)
 worker=TenantPartitionWorker(capture=capture,progress_store=store,cipher=cipher,source_table=source)
 observer=ExecutionQuiescence(capture=capture,progress_store=store,cipher=cipher,source_table=source)
 creds=CredentialDeletionPlan(capture=capture,progress_store=store,cipher=cipher,source_table=source,vault_table=vault)
 coordinator=AiDeletionCoordinator(capture=capture,worker=worker,observer=observer,credentials=creds)
 for _ in range(1000):
  result=coordinator.step(subject=u,request_id=q,limit=100)
  if result.get('writer_quiescence_required'):break
  if result['phase']=='VERIFYING':raise AssertionError('quiescence gate bypassed')
 else:raise AssertionError('bounded AI cleanup incomplete')
 assert result['phase']=='QUIESCENCE' and not result['execution_blocked']
 assert source.get_item(Key=sk,ConsistentRead=True).get('Item')
 print('captured consent; observed clear still requires writer quiescence and preserves data')
finally:
 cursor=None
 while True:
  args={'KeyConditionExpression':Key('pk').eq(f'REQ#{q}'),'ConsistentRead':True}
  if cursor:args['ExclusiveStartKey']=cursor
  page=progress.query(**args)
  for item in page['Items']:progress.delete_item(Key={'pk':item['pk'],'sk':item['sk']})
  cursor=page.get('LastEvaluatedKey')
  if not cursor:break
 assert not progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']
 for table,k in reversed(created):table.delete_item(Key=k)
 assert all(not table.get_item(Key=k,ConsistentRead=True).get('Item') for table,k in created)
 print('synthetic cleanup verified')
