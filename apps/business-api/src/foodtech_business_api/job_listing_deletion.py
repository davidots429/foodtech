"""Bounded, locked cleanup of user job listing entries.

The caller must have stopped writers and handled job records first. A listing
observation is not proof that jobs, revisions, or other stores are deleted.
"""
import re
from uuid import UUID
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from .condition_profile_deletion import ProfileDeletionConflict


def clean_one_job_listing(*, source_table, progress_store, cipher, subject, request_id):
    try:
        if str(UUID(subject)) != subject or str(UUID(request_id)) != request_id: raise ValueError
    except (ValueError,TypeError,AttributeError):
        raise ValueError('invalid job listing scope') from None
    if source_table.table_arn != getattr(cipher,'source_table_arn',None) or source_table.name in {
            progress_store.auth_table,progress_store.registry_table,progress_store.progress_table}:
        raise ValueError('job source does not match listing scope')
    progress_store.load_checkpoint(cipher=cipher,subject=subject,request_id=request_id,domain='JOB_LISTING')
    pk=f'SCOPE#USER#{subject}#JOBS'
    page=source_table.query(KeyConditionExpression=Key('pk').eq(pk)&Key('sk').begins_with('CREATED#'),
                            ConsistentRead=True,Limit=1)
    rows=page.get('Items')
    if not isinstance(rows,list) or len(rows)>1:raise ValueError('invalid job listing page')
    if not rows:
        if page.get('LastEvaluatedKey') is not None:raise ValueError('incomplete job listing page')
        return {'removed':False,'state':'VERIFYING'}
    row=rows[0]
    try:
        job_id,created=row['job_id'],row['created_at']
        if not isinstance(job_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',job_id) or \
                not isinstance(created,str) or not 1 <= len(created) <= 128 or '#' in created or \
                row['pk'] != pk or row['sk'] != f'CREATED#{created}#{job_id}':raise ValueError
    except (KeyError,TypeError,ValueError):raise ValueError('invalid job listing') from None
    serializer=TypeSerializer()
    def encode(values):return {key:serializer.serialize(value) for key,value in values.items()}
    try:
        progress_store.client.transact_write_items(TransactItems=[
            {'ConditionCheck':{'TableName':progress_store.auth_table,'Key':encode({'pk':f'USER#{subject}'}),
                'ConditionExpression':'deletion_lock_request = :request AND suspended = :yes',
                'ExpressionAttributeValues':encode({':request':request_id,':yes':True})}},
            {'ConditionCheck':{'TableName':progress_store.registry_table,
                'Key':encode({'pk':'REGISTRY','sk':f'REQ#{request_id}'}),
                'ConditionExpression':'#status = :active','ExpressionAttributeNames':{'#status':'status'},
                'ExpressionAttributeValues':encode({':active':'DELETING'})}},
            {'Delete':{'TableName':source_table.name,'Key':encode({'pk':pk,'sk':row['sk']}),
                'ConditionExpression':'attribute_not_exists(pk) OR (job_id = :id AND created_at = :created)',
                'ExpressionAttributeValues':encode({':id':job_id,':created':created})}},
        ])
    except ClientError as exc:
        if exc.response.get('Error',{}).get('Code')=='TransactionCanceledException' and any(
                item.get('Code')=='ConditionalCheckFailed' for item in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'removed':True,'state':'IN_PROGRESS'}
