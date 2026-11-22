"""Observe leftover job records, including those lacking job META.

A scan is not a snapshot. It requires stopped writers, and cannot establish
account deletion or discover records in other keyspaces and stores.
"""
import re

from .subject_identity import valid_subject
from boto3.dynamodb.conditions import Attr
from .legacy_profile_scan import LegacyProfilePage
from .profile_deletion_residual import ProfileResidualScan


class JobResidualScan(ProfileResidualScan):
    DOMAIN='JOB_RESIDUAL'
    INCLUDE_META=False

    def _scan(self, *, subject, limit, checkpoint):
        try:
            if not valid_subject(subject) or type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError
        except (TypeError,ValueError,AttributeError):
            raise ValueError('invalid job residual scope') from None
        self.inventory._cursor(checkpoint)
        prefix=f'SCOPE#USER#{subject}#JOB#'
        condition=Attr('pk').begins_with(prefix)
        if not self.INCLUDE_META:condition=condition & Attr('sk').ne('META')
        args={'FilterExpression':condition,
              'ProjectionExpression':'pk, sk','ConsistentRead':True,'Limit':limit}
        if checkpoint is not None:args['ExclusiveStartKey']=checkpoint
        page=self.source_table.scan(**args)
        rows,count=page.get('Items'),page.get('ScannedCount')
        if not isinstance(rows,list) or type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid job residual page')
        refs=[]
        for row in rows:
            try:
                pk,sk=row['pk'],row['sk']
                if not isinstance(pk,str) or not pk.startswith(prefix) or \
                        not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',pk[len(prefix):]) or \
                        not isinstance(sk,str) or not 1 <= len(sk) <= 1024 or (not self.INCLUDE_META and sk=='META'):raise ValueError
                refs.append((pk[len(prefix):],sk))
            except (KeyError,TypeError,ValueError):raise ValueError('invalid job residual record') from None
        cursor=page.get('LastEvaluatedKey');self.inventory._cursor(cursor)
        return LegacyProfilePage(tuple(refs),cursor,count)

    def repair_one(self, **_kwargs):
        raise ValueError('job residual repair requires separate ownership verification')


class JobFinalResidualScan(JobResidualScan):
    DOMAIN='JOB_FINAL_RESIDUAL'
    INCLUDE_META=True
