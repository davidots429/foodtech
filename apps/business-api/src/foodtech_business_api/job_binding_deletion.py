"""Guarded removal of a job's reverse binding using its retained META only.

An authenticated worker must stop writers and verify legacy orphan bindings
separately. This primitive does not remove source data or mark deletion complete.
"""
import re
from uuid import UUID

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from foodtech_contracts import stream_binding_key

from .condition_profile_deletion import ProfileDeletionConflict


def delete_job_binding(*, source_table, progress_store, cipher, subject, request_id, job_id):
    try:
        if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id or \
                not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',job_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid job binding scope') from None
    if source_table.table_arn != getattr(cipher,'source_table_arn',None) or source_table.name in {
            progress_store.auth_table,progress_store.registry_table,progress_store.progress_table}:
        raise ValueError('job source does not match deletion scope')
    progress_store.load_checkpoint(cipher=cipher,subject=subject,request_id=request_id,domain='JOB_BINDING')
    pk=f'SCOPE#USER#{subject}#JOB#{job_id}'
    meta=source_table.get_item(Key={'pk':pk,'sk':'META'},ConsistentRead=True).get('Item')
    if not isinstance(meta,dict) or meta.get('owner_subject') != subject or meta.get('job_id') != job_id:
        raise ValueError('job binding source unavailable')
    binding=meta.get('source_binding')
    if binding is None:
        return {'state':'UNBOUND'}
    menu=meta.get('menu')
    if not isinstance(binding,dict) or set(binding) != {'source_id','device_id','batch_id'} or \
            not all(isinstance(x,str) and 1 <= len(x) <= 80 for x in binding.values()) or \
            menu not in {'JEYUK','TONKATSU'}:
        raise ValueError('invalid job binding')
    prefix, device=('jeyuk-','mockprofile-110-a-001') if menu == 'JEYUK' else ('batch-a-','mockprofile-100-a-001')
    if binding['source_id'] != 'synthetic-rest' or binding['device_id'] != device or \
            not re.fullmatch(re.escape(prefix)+r'[A-Za-z0-9-]+',binding['batch_id']):
        raise ValueError('invalid job binding')
    stream=f"JEYUK#{binding['batch_id']}" if menu == 'JEYUK' else stream_binding_key(**binding)
    serializer=TypeSerializer()
    def encode(values): return {key:serializer.serialize(value) for key,value in values.items()}
    try:
        progress_store.client.transact_write_items(TransactItems=[
            {'ConditionCheck':{'TableName':progress_store.auth_table,'Key':encode({'pk':f'USER#{subject}'}),
                'ConditionExpression':'deletion_lock_request = :request AND suspended = :yes',
                'ExpressionAttributeValues':encode({':request':request_id,':yes':True})}},
            {'ConditionCheck':{'TableName':progress_store.registry_table,
                'Key':encode({'pk':'REGISTRY','sk':f'REQ#{request_id}'}),
                'ConditionExpression':'#status = :active','ExpressionAttributeNames':{'#status':'status'},
                'ExpressionAttributeValues':encode({':active':'DELETING'})}},
            {'ConditionCheck':{'TableName':source_table.name,'Key':encode({'pk':pk,'sk':'META'}),
                'ConditionExpression':'owner_subject = :owner AND job_id = :id AND #menu = :menu AND source_binding = :binding',
                'ExpressionAttributeNames':{'#menu':'menu'},
                'ExpressionAttributeValues':encode({':owner':subject,':id':job_id,':menu':menu,':binding':binding})}},
            {'Delete':{'TableName':source_table.name,'Key':encode({'pk':stream,'sk':'BINDING'}),
                'ConditionExpression':'attribute_not_exists(pk) OR (job_pk = :job AND job_id = :id)',
                'ExpressionAttributeValues':encode({':job':pk,':id':job_id})}},
        ])
    except ClientError as exc:
        if exc.response.get('Error',{}).get('Code') == 'TransactionCanceledException' and any(
                item.get('Code') == 'ConditionalCheckFailed' for item in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'state':'VERIFYING'}
