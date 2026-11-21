"""Opt-in synthetic job partition check; requires merged deletion progress modules."""
import os
from uuid import uuid4
from types import SimpleNamespace
import boto3
from foodtech_business_api.deletion_progress import DeletionProgressStore
from foodtech_business_api.deletion_checkpoint_cipher import DeletionCheckpointCipher
from foodtech_business_api.job_partition_deletion import delete_one_job_record
from foodtech_business_api.job_deletion_inventory import JobDeletionInventory
from foodtech_business_api.job_deletion_worker import JobDeletionWorker
from foodtech_business_api.job_binding_inventory import JobBindingInventory
from foodtech_business_api.job_deletion_coordinator import JobDeletionCoordinator
from foodtech_business_api.job_deletion_residual import JobResidualScan
from foodtech_business_api.job_binding_residual import JobBindingResidualScan
from foodtech_business_api.job_meta_deletion import JobMetaDeletion
from boto3.dynamodb.conditions import Key
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
if os.getenv('AWS_PROFILE') != 'foodtech' or os.getenv('FOODTECH_JOB_DELETION_LIVE') != '1':
 raise SystemExit('explicit development profile and FOODTECH_JOB_DELETION_LIVE=1 required')
s=boto3.Session(profile_name='foodtech',region_name='ap-southeast-2')
if s.client('sts').get_caller_identity()['Account']!='573693340100': raise RuntimeError('unexpected account')
db=s.resource('dynamodb'); auth=db.Table('foodtech-auth-sessions-dev'); reg=db.Table('foodtech-ai-execution-registry-dev'); src=db.Table('foodtech-cooking-jobs-dev'); progress=db.Table('foodtech-ai-credential-vault-dev')
outputs=s.client('cloudformation').describe_stacks(StackName='FoodtechCredentialVault-dev')['Stacks'][0]['Outputs']; key=next(x['OutputValue'] for x in outputs if x['OutputKey']=='CredentialStorageKeyArn')
u,q=str(uuid4()),str(uuid4()); pk=f'SCOPE#USER#{u}#JOB#job'; other=f'SCOPE#USER#{uuid4()}#JOB#other'
ak={'pk':f'USER#{u}'}; rk={'pk':'REGISTRY','sk':f'REQ#{q}'}
entries=[(auth,{**ak,'suspended':True,'deletion_lock_request':q}),(reg,{**rk,'status':'DELETING'}),
 (src,{'pk':pk,'sk':'META','owner_subject':u,'job_id':'job','version':1,'menu':'JEYUK',
       'source_binding':{'source_id':'synthetic-rest','device_id':'mockprofile-110-a-001','batch_id':'jeyuk-'+u}}),
 (src,{'pk':'JEYUK#jeyuk-'+u,'sk':'BINDING','job_pk':pk,'job_id':'job'}),
 (src,{'pk':'JEYUK#jeyuk-orphan-'+u,'sk':'BINDING','job_pk':f'SCOPE#USER#{u}#JOB#orphan','job_id':'orphan'}),(src,{'pk':pk,'sk':'EVENT#1'}),
 (src,{'pk':pk,'sk':'ASSESSMENT#1'}),(src,{'pk':other,'sk':'EVENT#1'}),
 (src,{'pk':f'SCOPE#USER#{u}#JOBS','sk':'CREATED#2026-01-01#job','job_id':'job','created_at':'2026-01-01'}),
 (src,{'pk':f'SCOPE#USER#{u}#JOBS','sk':'CREATED#2026-01-01#orphan','job_id':'orphan','created_at':'2026-01-01'}),
 (src,{'pk':f'SCOPE#USER#{u}#JOBS','sk':'HISTORY#REVISION','revision':2})]
created=[]
if progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']:
 raise RuntimeError('synthetic request already exists')
try:
 for table,item in entries:
  table.put_item(Item=item,ConditionExpression='attribute_not_exists(pk)');created.append((table,{k:item[k] for k in ('pk','sk') if k in item}))
 store=DeletionProgressStore(auth_table=auth.name,registry_table=reg.name,progress_table=progress.name,client=s.client('dynamodb'))
 cipher=DeletionCheckpointCipher(key,'dev',source_table_arn=src.table_arn,kms_client=s.client('kms'))
 args=dict(source_table=src,progress_store=store,cipher=cipher,subject=u,request_id=q,job_id='job')
 inventory=JobDeletionInventory(source_table=src,progress_store=store,cipher=cipher)
 for _ in range(1000):
  found=inventory.capture_page(subject=u,request_id=q,limit=20)
  if found['complete']: break
 else: raise AssertionError('bounded job scan incomplete')
 assert [item.job_id for item in inventory.targets(subject=u,request_id=q)]==['job']
 worker=JobDeletionWorker(inventory=inventory,source_table=src,progress_store=store,cipher=cipher)
 bindings=JobBindingInventory(source_table=src,progress_store=store,cipher=cipher)
 for _ in range(1000):
  captured=bindings.capture_page(subject=u,request_id=q,limit=20)
  if captured['complete']:break
 else:raise AssertionError('bounded binding scan incomplete')
 targets=list(bindings.targets(subject=u,request_id=q))
 orphan_index=next(i for i,target in enumerate(targets) if target.job_id=='orphan')
 orphan_key={'pk':'JEYUK#jeyuk-orphan-'+u,'sk':'BINDING'}
 src.update_item(Key=orphan_key,UpdateExpression='SET job_pk=:other',ExpressionAttributeValues={':other':other})
 try:bindings.remove_one(subject=u,request_id=q,index=orphan_index)
 except ProfileDeletionConflict:pass
 else:raise AssertionError('changed orphan owner accepted')
 src.update_item(Key=orphan_key,UpdateExpression='SET job_pk=:pk',ExpressionAttributeValues={':pk':f'SCOPE#USER#{u}#JOB#orphan'})
 bindings.remove_one(subject=u,request_id=q,index=orphan_index)
 bindings.remove_one(subject=u,request_id=q,index=orphan_index)
 assert not src.get_item(Key=orphan_key,ConsistentRead=True).get('Item')
 binding_key={'pk':'JEYUK#jeyuk-'+u,'sk':'BINDING'}
 src.update_item(Key=binding_key,UpdateExpression='SET job_pk=:other',ExpressionAttributeValues={':other':other})
 try: worker.remove_binding(subject=u,request_id=q,index=0)
 except ProfileDeletionConflict: pass
 else: raise AssertionError('other job binding accepted')
 src.update_item(Key=binding_key,UpdateExpression='SET job_pk=:pk',ExpressionAttributeValues={':pk':pk})
 assert worker.remove_binding(subject=u,request_id=q,index=0)['state']=='VERIFYING'
 assert worker.remove_binding(subject=u,request_id=q,index=0)['state']=='VERIFYING'
 assert not src.get_item(Key=binding_key,ConsistentRead=True).get('Item')
 reg.update_item(Key=rk,UpdateExpression='SET #s=:s',ExpressionAttributeNames={'#s':'status'},ExpressionAttributeValues={':s':'PAUSED'})
 stale=SimpleNamespace(auth_table=auth.name,registry_table=reg.name,progress_table=progress.name,client=store.client,load_checkpoint=lambda **kw:None)
 try: delete_one_job_record(**{**args,'progress_store':stale})
 except ProfileDeletionConflict: pass
 else: raise AssertionError('inactive request accepted')
 assert src.get_item(Key={'pk':pk,'sk':'ASSESSMENT#1'},ConsistentRead=True).get('Item')
 reg.update_item(Key=rk,UpdateExpression='SET #s=:s',ExpressionAttributeNames={'#s':'status'},ExpressionAttributeValues={':s':'DELETING'})
 src.put_item(Item={**binding_key,'job_pk':pk,'job_id':'job'},ConditionExpression='attribute_not_exists(pk)')
 src.put_item(Item={**orphan_key,'job_pk':f'SCOPE#USER#{u}#JOB#orphan','job_id':'orphan'},ConditionExpression='attribute_not_exists(pk)')
 residual=JobResidualScan(source_table=src,progress_store=store,cipher=cipher)
 binding_residual=JobBindingResidualScan(source_table=src,progress_store=store,cipher=cipher)
 meta_worker=JobMetaDeletion(inventory=inventory,residual=residual,binding_residual=binding_residual)
 coordinator=JobDeletionCoordinator(jobs=inventory,worker=worker,bindings=bindings,residual=residual,binding_residual=binding_residual,metadata=meta_worker)
 orphan_record={'pk':f'SCOPE#USER#{u}#JOB#lost','sk':'EVENT#1'}
 inserted=observed=binding_inserted=binding_observed=False
 remaining_binding={'pk':'JEYUK#jeyuk-late-'+u,'sk':'BINDING'}
 for _ in range(1000):
  result=coordinator.step(subject=u,request_id=q,limit=100)
  if result['phase']=='RESIDUAL' and not inserted:
   src.put_item(Item=orphan_record,ConditionExpression='attribute_not_exists(pk)');created.append((src,orphan_record));inserted=True
  if result['phase']=='BINDING_RESIDUAL' and not binding_inserted:
   src.put_item(Item={**remaining_binding,'job_pk':pk,'job_id':'job'},ConditionExpression='attribute_not_exists(pk)');created.append((src,remaining_binding));binding_inserted=True
  if result.get('residual_found') and result['phase']=='RESIDUAL':
   observed=True;src.delete_item(Key=orphan_record)
  if result.get('residual_found') and result['phase']=='BINDING_RESIDUAL':
   binding_observed=True;src.delete_item(Key=remaining_binding)
  if result['phase']=='VERIFYING':break
 else:raise AssertionError('bounded job cleanup incomplete')
 assert observed and binding_observed
 assert not src.get_item(Key=binding_key,ConsistentRead=True).get('Item')
 assert not src.get_item(Key=orphan_key,ConsistentRead=True).get('Item')
 assert not src.get_item(Key={'pk':f'SCOPE#USER#{u}#JOBS','sk':'CREATED#2026-01-01#job'},ConsistentRead=True).get('Item')
 assert not src.get_item(Key={'pk':f'SCOPE#USER#{u}#JOBS','sk':'CREATED#2026-01-01#orphan'},ConsistentRead=True).get('Item')
 assert not src.get_item(Key={'pk':f'SCOPE#USER#{u}#JOBS','sk':'HISTORY#REVISION'},ConsistentRead=True).get('Item')
 assert meta_worker.remove_one(subject=u,request_id=q,index=0)['state']=='VERIFYING'
 assert meta_worker.remove_one(subject=u,request_id=q,index=0)['state']=='VERIFYING'
 assert not src.get_item(Key={'pk':pk,'sk':'META'},ConsistentRead=True).get('Item')
 snapshot=store.load_checkpoint(cipher=cipher,subject=u,request_id=q,domain='JOB_META_000000000000')
 assert b'source_binding' in snapshot['checkpoint']
 assert src.get_item(Key={'pk':other,'sk':'EVENT#1'},ConsistentRead=True).get('Item')
 print('locked job record deletion, inactive request rejection and other partition isolation verified')
finally:
 cursor=None
 while True:
  query={'KeyConditionExpression':Key('pk').eq(f'REQ#{q}'),'ConsistentRead':True}
  if cursor: query['ExclusiveStartKey']=cursor
  page=progress.query(**query)
  for item in page['Items']: progress.delete_item(Key={'pk':item['pk'],'sk':item['sk']})
  cursor=page.get('LastEvaluatedKey')
  if not cursor: break
 assert not progress.query(KeyConditionExpression=Key('pk').eq(f'REQ#{q}'),ConsistentRead=True)['Items']
 for table,k in reversed(created): table.delete_item(Key=k)
 assert all(not table.get_item(Key=k,ConsistentRead=True).get('Item') for table,k in created)
 print('synthetic cleanup verified')
