"""Opt-in synthetic tier flow; requires merged deletion progress modules."""
import os
from uuid import uuid4
import boto3
from foodtech_business_api.deletion_progress import DeletionProgressStore
from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.tier_store import DynamoTierStore
from foodtech_business_api.tier_deletion_worker import TierIndexCapture,TierDeletionWorker
from foodtech_business_api.tier_deletion_coordinator import TierDeletionCoordinator
from boto3.dynamodb.conditions import Key
if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_TIER_COORDINATOR_LIVE') != '1':
 raise SystemExit('explicit development profile and FOODTECH_TIER_COORDINATOR_LIVE=1 required')
s=boto3.Session(profile_name='foodtech',region_name='ap-southeast-2')
if s.client('sts').get_caller_identity()['Account']!='573693340100':raise RuntimeError('unexpected account')
db=s.resource('dynamodb');auth=db.Table('foodtech-auth-sessions-dev');reg=db.Table('foodtech-ai-execution-registry-dev');source=db.Table('foodtech-cooking-jobs-dev');progress=db.Table('foodtech-ai-credential-vault-dev')
outputs=s.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs'];key=next(x['OutputValue'] for x in outputs if x['OutputKey']=='CredentialStorageKeyArn')
u,q=str(uuid4()),str(uuid4());ak={'pk':f'USER#{u}'};rk={'pk':'REGISTRY','sk':f'REQ#{q}'};item={'pk':f'TENANT#{u}','sk':'CURRENT','tier':'FREE'}
created=[]
if progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']:raise RuntimeError('request collision')
try:
 for table,value in [(auth,{**ak,'suspended':True,'deletion_lock_request':q}),(reg,{**rk,'status':'DELETING'}),(source,item)]:
  table.put_item(Item=value,ConditionExpression='attribute_not_exists(pk)');created.append((table,{k:value[k] for k in ('pk','sk') if k in value}))
 tier=DynamoTierStore(source.name);tier.auth_table_name=auth.name;tier.deletion_registry_table_name=reg.name
 store=DeletionProgressStore(auth_table=auth.name,registry_table=reg.name,progress_table=progress.name,client=s.client('dynamodb'))
 cipher=DeletionCheckpointCipher(key,'dev',source_table_arn=source.table_arn,kms_client=s.client('kms'))
 capture=TierIndexCapture(tier_store=tier,progress_store=store,cipher=cipher)
 worker=TierDeletionWorker(tier_store=tier,progress_store=store,cipher=cipher,index_capture=capture)
 coordinator=TierDeletionCoordinator(capture=capture,worker=worker)
 for _ in range(20):
  result=coordinator.step(subject=u,request_id=q,limit=10)
  if result['phase']=='VERIFYING':break
 else:raise AssertionError('bounded tier run incomplete')
 assert not source.get_item(Key={'pk':item['pk'],'sk':item['sk']},ConsistentRead=True).get('Item')
 print('tier capture and guarded deletion reached verification; no GSI candidates')
finally:
 cursor=None
 while True:
  query={'KeyConditionExpression':Key('pk').eq(f'REQ#{q}'),'ConsistentRead':True}
  if cursor:query['ExclusiveStartKey']=cursor
  page=progress.query(**query)
  for value in page['Items']:progress.delete_item(Key={'pk':value['pk'],'sk':value['sk']})
  cursor=page.get('LastEvaluatedKey')
  if not cursor:break
 assert not progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']
 for table,k in reversed(created):table.delete_item(Key=k)
 assert all(not table.get_item(Key=k,ConsistentRead=True).get('Item') for table,k in created)
 print('synthetic cleanup verified')
