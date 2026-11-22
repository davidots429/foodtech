"""Capture customer-scoped meal aggregate results without trusting the group index.

Scans are not snapshots. Writer fencing and independent residual checks are
required before any deletion can be considered complete.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from boto3.dynamodb.conditions import Attr

from .legacy_profile_scan import LegacyProfilePage
from .profile_deletion_inventory import ProfileDeletionInventory


@dataclass(frozen=True)
class MealAggregateTarget:
    group_id: str = field(repr=False)
    result_id: str = field(repr=False)


class MealAggregateDeletionInventory(ProfileDeletionInventory):
    HEAD_DOMAIN = 'MEAL_AGGREGATE_INVENTORY'

    @staticmethod
    def _domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid meal aggregate inventory page')
        return f'MEAL_AGGREGATE_PAGE_{number:012d}'

    @staticmethod
    def _validate_reference(subject, ref):
        if not isinstance(ref, list) or len(ref) != 2 or any(
                not isinstance(part, str) or not re.fullmatch(r'[0-9a-f]{64}', part) for part in ref):
            raise ValueError('invalid meal aggregate reference')

    @staticmethod
    def _target(ref):
        return MealAggregateTarget(*ref)

    def _scan(self, *, subject, limit, checkpoint):
        try:
            if str(UUID(subject)) != subject or type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid meal aggregate inventory scope') from None
        self._cursor(checkpoint)
        prefix = f'SCOPE#USER#{subject}#MEAL_AGGREGATE#'
        args = {'FilterExpression': Attr('pk').begins_with(prefix) & Attr('sk').begins_with('RESULT#'),
                'ProjectionExpression': 'pk, sk, group_id, result_id', 'ConsistentRead': True, 'Limit': limit}
        if checkpoint is not None:
            args['ExclusiveStartKey'] = checkpoint
        page = self.source_table.scan(**args)
        rows, count = page.get('Items'), page.get('ScannedCount')
        if not isinstance(rows, list) or type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid meal aggregate inventory page')
        references = []
        for row in rows:
            try:
                group_id, result_id = row['group_id'], row['result_id']
                self._validate_reference(subject, [group_id, result_id])
                if row['pk'] != prefix + group_id or row['sk'] != 'RESULT#' + result_id:
                    raise ValueError
                references.append((group_id, result_id))
            except (KeyError, TypeError, ValueError):
                raise ValueError('invalid meal aggregate inventory source') from None
        cursor = page.get('LastEvaluatedKey')
        self._cursor(cursor)
        return LegacyProfilePage(tuple(references), cursor, count)
