"""Preserve a protected job META snapshot before guarded, idempotent removal.

Call only after independent job and binding residual checks and writer
quiescence. The encrypted snapshot remains for downstream source cleanup;
removal is not whole-account deletion completion.
"""
import base64
import json
from decimal import Decimal
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from .condition_profile_deletion import ProfileDeletionConflict


class JobMetaDeletion:
    def __init__(self, *, inventory, residual, binding_residual):
        if any(component.source_table is not inventory.source_table or
               component.progress_store is not inventory.progress_store or component.cipher is not inventory.cipher
               for component in (residual,binding_residual)):
            raise ValueError('job metadata dependencies do not match')
        self.inventory,self.residual,self.binding_residual=inventory,residual,binding_residual
        self.source_table,self.progress_store,self.cipher=inventory.source_table,inventory.progress_store,inventory.cipher

    @staticmethod
    def _safe(value):
        if isinstance(value,bytes):return {'$binary':base64.b64encode(value).decode('ascii')}
        if isinstance(value,list):return [JobMetaDeletion._safe(x) for x in value]
        if isinstance(value,dict):return {k:JobMetaDeletion._safe(v) for k,v in value.items()}
        return value

    def remove_one(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:raise ValueError('invalid job metadata index')
        for check in (self.residual,self.binding_residual):
            state,_=check._checkpoint(subject,request_id)
            if not state['complete'] or state['residual']:raise ValueError('job residual checks incomplete')
        target=next((x for n,x in enumerate(self.inventory.targets(subject=subject,request_id=request_id)) if n==index),None)
        if target is None:raise ValueError('job metadata target unavailable')
        scope={'subject':subject,'request_id':request_id,'domain':f'JOB_META_{index:012d}'}
        saved=self.progress_store.load_checkpoint(cipher=self.cipher,**scope)
        pk=f'SCOPE#USER#{subject}#JOB#{target.job_id}'
        page=self.source_table.query(KeyConditionExpression=Key('pk').eq(pk),ConsistentRead=True,Limit=2)
        rows=page.get('Items')
        if not isinstance(rows,list) or len(rows)>1 or page.get('LastEvaluatedKey') is not None:
            raise ValueError('job partition still has records')
        if rows and (not isinstance(rows[0],dict) or rows[0].get('pk')!=pk or rows[0].get('sk')!='META'):
            raise ValueError('job partition still has records')
        if saved is None:
            if not rows:raise ValueError('job metadata unavailable')
            meta=rows[0]
            version=meta.get('version')
            if meta.get('pk')!=pk or meta.get('sk')!='META' or meta.get('owner_subject')!=subject or \
                    meta.get('job_id')!=target.job_id or isinstance(version,bool) or \
                    not isinstance(version,(int,Decimal)) or version != int(version) or not 1 <= version < 10**18:
                raise ValueError('invalid job metadata')
            serializer=TypeSerializer()
            encoded={key:self._safe(serializer.serialize(value)) for key,value in meta.items()}
            snapshot={'schema':1,'job_id':target.job_id,'version':int(version),'metadata':encoded}
            raw=json.dumps(snapshot,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')
            if len(raw)>16384:raise ValueError('job metadata snapshot too large')
            self.progress_store.save_checkpoint(cipher=self.cipher,**scope,expected_version=0,
                                                state='IN_PROGRESS',checkpoint=raw)
        else:
            if saved.get('version')!=1 or saved.get('state')!='IN_PROGRESS':raise ValueError('invalid job metadata intent')
            snapshot=self.inventory._json(saved['checkpoint'])
            if set(snapshot)!={'schema','job_id','version','metadata'} or type(snapshot['schema']) is not int or \
                    snapshot['schema']!=1 or snapshot['job_id']!=target.job_id or type(snapshot['version']) is not int or \
                    not 1 <= snapshot['version'] < 10**18 or not isinstance(snapshot['metadata'],dict):
                raise ValueError('invalid job metadata intent')
        serializer=TypeSerializer()
        def encode(values):return {key:serializer.serialize(value) for key,value in values.items()}
        try:
            self.progress_store.client.transact_write_items(TransactItems=[
                {'ConditionCheck':{'TableName':self.progress_store.auth_table,'Key':encode({'pk':f'USER#{subject}'}),
                    'ConditionExpression':'deletion_lock_request = :request AND suspended = :yes',
                    'ExpressionAttributeValues':encode({':request':request_id,':yes':True})}},
                {'ConditionCheck':{'TableName':self.progress_store.registry_table,
                    'Key':encode({'pk':'REGISTRY','sk':f'REQ#{request_id}'}),
                    'ConditionExpression':'#status = :active','ExpressionAttributeNames':{'#status':'status'},
                    'ExpressionAttributeValues':encode({':active':'DELETING'})}},
                {'Delete':{'TableName':self.source_table.name,'Key':encode({'pk':pk,'sk':'META'}),
                    'ConditionExpression':'attribute_not_exists(pk) OR (owner_subject = :owner AND job_id = :id AND #v = :version)',
                    'ExpressionAttributeNames':{'#v':'version'},
                    'ExpressionAttributeValues':encode({':owner':subject,':id':target.job_id,':version':snapshot['version']})}},
            ])
        except ClientError as exc:
            if exc.response.get('Error',{}).get('Code')=='TransactionCanceledException' and any(
                    x.get('Code')=='ConditionalCheckFailed' for x in (exc.response.get('CancellationReasons') or [])):
                raise ProfileDeletionConflict from exc
            raise
        return {'index':index,'state':'VERIFYING'}
