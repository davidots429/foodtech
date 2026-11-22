"""Remove only an isolated user job-history revision under a deletion lock.

The listing partition must be otherwise empty. Stopped writers and independent
residual checks are required; an empty partition is not a completion receipt.
"""
from decimal import Decimal
from uuid import UUID

from .subject_identity import valid_subject
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from .condition_profile_deletion import ProfileDeletionConflict


def remove_job_history(*, source_table,progress_store,cipher,subject,request_id):
    try:
        if not valid_subject(subject) or str(UUID(request_id))!=request_id:raise ValueError
    except (ValueError,TypeError,AttributeError):raise ValueError('invalid job history scope') from None
    if source_table.table_arn != getattr(cipher,'source_table_arn',None) or source_table.name in {
            progress_store.auth_table,progress_store.registry_table,progress_store.progress_table}:
        raise ValueError('job source does not match history scope')
    progress_store.load_checkpoint(cipher=cipher,subject=subject,request_id=request_id,domain='JOB_HISTORY')
    pk=f'SCOPE#USER#{subject}#JOBS'
    page=source_table.query(KeyConditionExpression=Key('pk').eq(pk),ConsistentRead=True,Limit=2)
    rows=page.get('Items')
    if not isinstance(rows,list) or len(rows)>1 or page.get('LastEvaluatedKey') is not None:
        raise ValueError('job history partition not empty')
    if not rows:return {'state':'VERIFYING'}
    row=rows[0];revision=row.get('revision') if isinstance(row,dict) else None
    if not isinstance(row,dict) or row.get('pk')!=pk or row.get('sk')!='HISTORY#REVISION' or \
            isinstance(revision,bool) or not isinstance(revision,(int,Decimal)) or \
            revision != int(revision) or not 1 <= revision <= 10**18:
        raise ValueError('invalid job history revision')
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
            {'Delete':{'TableName':source_table.name,'Key':encode({'pk':pk,'sk':'HISTORY#REVISION'}),
                'ConditionExpression':'attribute_not_exists(pk) OR #revision = :revision',
                'ExpressionAttributeNames':{'#revision':'revision'},
                'ExpressionAttributeValues':encode({':revision':revision})}},
        ])
    except ClientError as exc:
        if exc.response.get('Error',{}).get('Code')=='TransactionCanceledException' and any(
                x.get('Code')=='ConditionalCheckFailed' for x in (exc.response.get('CancellationReasons') or [])):
            raise ProfileDeletionConflict from exc
        raise
    return {'state':'VERIFYING'}
