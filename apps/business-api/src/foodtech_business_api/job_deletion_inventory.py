"""Protected discovery of legacy subject-keyed cooking job META records.

A table scan is not a snapshot. An authenticated worker must stop writers and
perform independent residual, index and reverse-binding checks before completion.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from boto3.dynamodb.conditions import Attr

from .legacy_profile_scan import LegacyProfilePage
from .profile_deletion_inventory import ProfileDeletionInventory


@dataclass(frozen=True)
class JobTarget:
    job_id: str = field(repr=False)


class JobDeletionInventory(ProfileDeletionInventory):
    HEAD_DOMAIN = 'JOB_INVENTORY'

    @staticmethod
    def _domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid job inventory page')
        return f'JOB_INVENTORY_PAGE_{number:012d}'

    @staticmethod
    def _validate_reference(subject, ref):
        if not isinstance(ref, list) or len(ref) != 2 or not isinstance(ref[0], str) or \
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', ref[0]) or ref[1] != '':
            raise ValueError('invalid job inventory reference')

    @staticmethod
    def _target(ref):
        return JobTarget(ref[0])

    def _scan(self, *, subject, limit, checkpoint):
        try:
            if str(UUID(subject)) != subject or type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid job inventory scope') from None
        self._cursor(checkpoint)
        prefix=f'SCOPE#USER#{subject}#JOB#'
        args={'FilterExpression': Attr('pk').begins_with(prefix) & Attr('sk').eq('META'),
              'ProjectionExpression':'pk, sk, owner_subject, job_id', 'ConsistentRead':True, 'Limit':limit}
        if checkpoint is not None:
            args['ExclusiveStartKey']=checkpoint
        result=self.source_table.scan(**args)
        rows, count=result.get('Items'),result.get('ScannedCount')
        if not isinstance(rows,list) or type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid job inventory scan')
        refs=[]
        for row in rows:
            try:
                job_id=row['job_id']
                self._validate_reference(subject, [job_id, ''])
                if row['pk'] != prefix+job_id or row['sk'] != 'META' or row['owner_subject'] != subject:
                    raise ValueError
                refs.append((job_id, ''))
            except (KeyError, TypeError, ValueError):
                raise ValueError('invalid job inventory source') from None
        cursor=result.get('LastEvaluatedKey')
        self._cursor(cursor)
        return LegacyProfilePage(tuple(refs), cursor, count)
