"""Protected discovery of subject-scoped meal META records.

A completed scan is not a snapshot or evidence of complete account deletion.
"""
import re
from dataclasses import dataclass, field
from uuid import UUID

from .legacy_meal_scan import scan_legacy_meals
from .legacy_profile_scan import LegacyProfilePage
from .profile_deletion_inventory import ProfileDeletionInventory


@dataclass(frozen=True)
class MealTarget:
    meal_id: str = field(repr=False)


class MealDeletionInventory(ProfileDeletionInventory):
    HEAD_DOMAIN = 'MEAL_INVENTORY'

    @staticmethod
    def _domain(number):
        if type(number) is not int or not 1 <= number < 10**12:
            raise ValueError('invalid meal inventory page')
        return f'MEAL_INVENTORY_PAGE_{number:012d}'

    @staticmethod
    def _validate_reference(subject, ref):
        if not isinstance(ref, list) or len(ref) != 2 or not isinstance(ref[0], str) or \
                not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', ref[0]) or ref[1] != '':
            raise ValueError('invalid meal inventory reference')

    @staticmethod
    def _target(ref):
        return MealTarget(ref[0])

    def _scan(self, *, subject, limit, checkpoint):
        try:
            if str(UUID(subject)) != subject or type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid meal inventory scope') from None
        found = scan_legacy_meals(self.source_table, subject=subject, limit=limit, checkpoint=checkpoint)
        return LegacyProfilePage(tuple((meal_id, '') for meal_id in found.references),
                                 found.checkpoint, found.scanned_count)
