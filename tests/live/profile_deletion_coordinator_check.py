"""Opt-in synthetic integration check after the privacy progress modules are merged.

Uses existing development resources only; no infrastructure deployment.
"""
import os
from uuid import uuid4
import boto3
from foodtech_business_api.deletion_progress import DeletionProgressStore
from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.profile_deletion_inventory import ProfileDeletionInventory
from foodtech_business_api.profile_deletion_worker import ProfileDeletionWorker
from foodtech_business_api.profile_deletion_residual import ProfileResidualScan
from foodtech_business_api.profile_deletion_coordinator import ProfileDeletionCoordinator
if os.environ.get('AWS_PROFILE') != 'foodtech' or os.environ.get('FOODTECH_PROFILE_DELETION_LIVE') != '1':
 raise SystemExit('explicit development profile and FOODTECH_PROFILE_DELETION_LIVE=1 required')
s=boto3.Session(profile_name='foodtech',region_name='ap-southeast-2')
if s.client('sts').get_caller_identity()['Account']!='573693340100': raise RuntimeError('unexpected account')
db=s.resource('dynamodb'); auth=db.Table('foodtech-auth-sessions-dev'); reg=db.Table('foodtech-ai-execution-registry-dev'); src=db.Table('foodtech-cooking-jobs-dev'); progress=db.Table('foodtech-ai-credential-vault-dev')
outputs=s.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']
key=next(x['OutputValue'] for x in outputs if x['OutputKey']=='CredentialStorageKeyArn')
u,q=str(uuid4()),str(uuid4()); ak={'pk':f'USER#{u}'}; rk={'pk':'REGISTRY','sk':f'REQ#{q}'}; owner=f'SCOPE#USER#{u}'
keys=[{'pk':f'{owner}#CONDITION_PROFILE#p','sk':f'VERSION#{v}'} for v in ('v1','v2')]+[{'pk':f'{owner}#CONDITION_PROFILES','sk':f'PROFILE#p#VERSION#{v}'} for v in ('v1','v2')]
orphan={'pk':f'{owner}#CONDITION_PROFILES','sk':'PROFILE#orphan#VERSION#v1'}
created=[]
if progress.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']:
 raise RuntimeError('synthetic progress key already exists')
try:
 for table,item in [(auth,{**ak,'suspended':True,'deletion_lock_request':q}),(reg,{**rk,'status':'DELETING'})]+[(src,{**k,'profile_id':'p','profile_version':('v1' if i%2==0 else 'v2')}) for i,k in enumerate(keys)]+[(src,{**orphan,'profile_id':'orphan','profile_version':'v1'})]:
  table.put_item(Item=item,ConditionExpression='attribute_not_exists(pk)'); created.append((table,{k:item[k] for k in ('pk','sk') if k in item}))
 store=DeletionProgressStore(auth_table=auth.name,registry_table=reg.name,progress_table=progress.name,client=s.client('dynamodb'))
 cipher=DeletionCheckpointCipher(key,'dev',source_table_arn=src.table_arn,kms_client=s.client('kms'))
 inventory=ProfileDeletionInventory(source_table=src,progress_store=store,cipher=cipher)
 worker=ProfileDeletionWorker(inventory=inventory,progress_store=store,cipher=cipher,source_table=src)
 residual=ProfileResidualScan(source_table=src,progress_store=store,cipher=cipher)
 coordinator=ProfileDeletionCoordinator(inventory=inventory,worker=worker,residual=residual)
 phases=set(); inserted=False
 for _ in range(1000):
  result=coordinator.step(subject=u,request_id=q,limit=100)
  phases.add(result['phase'])
  if result['phase']=='FINAL_LISTING' and not inserted:
   src.put_item(Item={**orphan,'profile_id':'orphan','profile_version':'v1'},ConditionExpression='attribute_not_exists(pk)')
   inserted=True
  if result['residual_found']: raise AssertionError('unexpected residual')
  if result['phase']=='VERIFYING': break
 else: raise AssertionError('bounded coordinator incomplete')
 assert {'DELETE','ORPHANS','RESIDUAL','FINAL_LISTING','VERIFYING'} <= phases
 assert all(not src.get_item(Key=k,ConsistentRead=True).get('Item') for k in keys+[orphan])
 print('bounded coordinator captured, removed and rechecked synthetic profiles and orphan')
finally:
 cursor=None
 while True:
  args={'KeyConditionExpression':boto3.dynamodb.conditions.Key('pk').eq(f'REQ#{q}'),'ConsistentRead':True}
  if cursor: args['ExclusiveStartKey']=cursor
  result=progress.query(**args)
  for item in result['Items']: progress.delete_item(Key={'pk':item['pk'],'sk':item['sk']})
  cursor=result.get('LastEvaluatedKey')
  if not cursor: break
 for table,k in reversed(created): table.delete_item(Key=k)
 assert not progress.query(KeyConditionExpression=boto3.dynamodb.conditions.Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']
 assert all(not table.get_item(Key=k,ConsistentRead=True).get('Item') for table,k in created)
 print('synthetic cleanup verified')
