"""Protected discovery of reverse bindings, including those without job META.

A scan is not a snapshot; stop legacy writers and verify residuals separately.
Only an authenticated maintenance worker may use the returned targets.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from boto3.dynamodb.conditions import Attr
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from .condition_profile_deletion import ProfileDeletionConflict
from .legacy_profile_scan import LegacyProfilePage
from .profile_deletion_inventory import ProfileDeletionInventory


@dataclass(frozen=True)
class BindingTarget:
    stream: str = field(repr=False)
    job_id: str = field(repr=False)


class JobBindingInventory(ProfileDeletionInventory):
    HEAD_DOMAIN='JOB_BINDING_INVENTORY'

    @staticmethod
    def _domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid job binding inventory page')
        return f'JOB_BINDING_PAGE_{number:012d}'

    @staticmethod
    def _validate_reference(subject, ref):
        if not isinstance(ref,list) or len(ref) != 2 or not isinstance(ref[0],str) or \
                not 1 <= len(ref[0]) <= 1024 or not ref[0].startswith(('JEYUK#','STREAM#')) or \
                not isinstance(ref[1],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',ref[1]):
            raise ValueError('invalid job binding reference')

    @staticmethod
    def _target(ref):
        return BindingTarget(*ref)

    def _scan(self, *, subject, limit, checkpoint):
        try:
            if str(UUID(subject)) != subject or type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid job binding scan scope') from None
        self._cursor(checkpoint)
        owner=f'SCOPE#USER#{subject}#JOB#'
        args={'FilterExpression':Attr('job_pk').begins_with(owner) & Attr('sk').eq('BINDING'),
              'ProjectionExpression':'pk, sk, job_pk, job_id','ConsistentRead':True,'Limit':limit}
        if checkpoint is not None: args['ExclusiveStartKey']=checkpoint
        page=self.source_table.scan(**args)
        rows,count=page.get('Items'),page.get('ScannedCount')
        if not isinstance(rows,list) or type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid job binding scan')
        refs=[]
        for row in rows:
            try:
                ref=[row['pk'],row['job_id']]
                self._validate_reference(subject,ref)
                if row['sk'] != 'BINDING' or row['job_pk'] != owner+ref[1]: raise ValueError
                refs.append(tuple(ref))
            except (KeyError,TypeError,ValueError):
                raise ValueError('invalid job binding source') from None
        cursor=page.get('LastEvaluatedKey');self._cursor(cursor)
        return LegacyProfilePage(tuple(refs),cursor,count)

    def remove_one(self, *, subject, request_id, index):
        if type(index) is not int or not 0 <= index < 10**12:
            raise ValueError('invalid job binding index')
        target=next((target for n,target in enumerate(self.targets(subject=subject,request_id=request_id)) if n==index),None)
        if target is None: raise ValueError('job binding target unavailable')
        pk=f'SCOPE#USER#{subject}#JOB#{target.job_id}'
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
                {'Delete':{'TableName':self.source_table.name,'Key':encode({'pk':target.stream,'sk':'BINDING'}),
                    'ConditionExpression':'attribute_not_exists(pk) OR (job_pk = :job AND job_id = :id)',
                    'ExpressionAttributeValues':encode({':job':pk,':id':target.job_id})}},
            ])
        except ClientError as exc:
            if exc.response.get('Error',{}).get('Code') == 'TransactionCanceledException' and any(
                    item.get('Code') == 'ConditionalCheckFailed' for item in (exc.response.get('CancellationReasons') or [])):
                raise ProfileDeletionConflict from exc
            raise
        return {'index':index,'state':'VERIFYING'}
