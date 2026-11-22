"""Observe remaining customer meal-aggregate rows without declaring deletion complete."""
import re

from .subject_identity import valid_subject

from boto3.dynamodb.conditions import Attr

from .legacy_profile_scan import LegacyProfilePage
from .profile_deletion_residual import ProfileResidualScan


class MealAggregateResidualScan(ProfileResidualScan):
    DOMAIN = 'MEAL_AGGREGATE_RESIDUAL'

    def _scan(self, *, subject, limit, checkpoint):
        try:
            if not valid_subject(subject) or type(limit) is not int or not 1 <= limit <= 100:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError('invalid meal aggregate residual scope') from None
        self.inventory._cursor(checkpoint)
        prefix = f'SCOPE#USER#{subject}#MEAL_AGGREGATE#'
        args = {'FilterExpression': Attr('pk').begins_with(prefix), 'ProjectionExpression': 'pk, sk',
                'ConsistentRead': True, 'Limit': limit}
        if checkpoint is not None:
            args['ExclusiveStartKey'] = checkpoint
        page = self.source_table.scan(**args)
        rows, count = page.get('Items'), page.get('ScannedCount')
        if not isinstance(rows, list) or type(count) is not int or not 0 <= count <= limit:
            raise ValueError('invalid meal aggregate residual page')
        refs = []
        for row in rows:
            try:
                pk, sk = row['pk'], row['sk']
                if not isinstance(pk, str) or not pk.startswith(prefix) or \
                        not re.fullmatch(r'[0-9a-f]{64}', pk[len(prefix):]) or \
                        not isinstance(sk, str) or not 1 <= len(sk) <= 1024:
                    raise ValueError
                refs.append((pk[len(prefix):], sk))
            except (KeyError, TypeError, ValueError):
                raise ValueError('invalid meal aggregate residual record') from None
        cursor = page.get('LastEvaluatedKey')
        self.inventory._cursor(cursor)
        return LegacyProfilePage(tuple(refs), cursor, count)

    def repair_one(self, **_kwargs):
        raise ValueError('meal aggregate residual repair requires separate ownership verification')
