"""Restricted migration scan for legacy profile references.

A scan reads the table, even when filtering to one owner. Never expose its raw
checkpoint to customers or log it: the checkpoint can contain another owner's
key. This is not a consistent snapshot or deletion-completeness proof. Run only
in a separately authorized maintenance worker with protected checkpoints.
"""
import re
from dataclasses import dataclass, field

from boto3.dynamodb.conditions import Attr

from .cooking_store import _profile_listing


@dataclass(frozen=True)
class LegacyProfilePage:
    references: tuple[tuple[str, str], ...] = field(repr=False)
    checkpoint: dict | None = field(repr=False)
    scanned_count: int


def scan_legacy_profiles(table, *, subject: str, limit: int, checkpoint: dict | None = None) -> LegacyProfilePage:
    """Read one bounded scan page. Empty filtered pages can still have a checkpoint."""
    if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', subject) or \
            type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid legacy scan scope')
    if checkpoint is not None and (not isinstance(checkpoint, dict) or set(checkpoint) != {'pk', 'sk'} or
            any(not isinstance(value, str) or not value or len(value) > 2048 for value in checkpoint.values())):
        raise ValueError('invalid legacy scan checkpoint')
    prefix = f'SCOPE#USER#{subject}#CONDITION_PROFILE#'
    args = {'FilterExpression': Attr('pk').begins_with(prefix) & Attr('sk').begins_with('VERSION#'),
            'ProjectionExpression': 'pk, sk, profile_id, profile_version', 'ConsistentRead': True, 'Limit': limit}
    if checkpoint is not None:
        args['ExclusiveStartKey'] = checkpoint
    page = table.scan(**args)
    items, count = page.get('Items'), page.get('ScannedCount')
    if not isinstance(items, list) or type(count) is not int or not 0 <= count <= limit:
        raise ValueError('invalid legacy scan result')
    references = []
    for item in items:
        try:
            profile_id, version = item['profile_id'], item['profile_version']
            _profile_listing(subject, {'profile_id': profile_id, 'profile_version': version})
            if item['pk'] != prefix + profile_id or item['sk'] != 'VERSION#' + version:
                raise ValueError
            references.append((profile_id, version))
        except (KeyError, TypeError, ValueError):
            raise ValueError('invalid legacy profile') from None
    next_key = page.get('LastEvaluatedKey')
    if next_key is not None and (not isinstance(next_key, dict) or set(next_key) != {'pk', 'sk'} or
            any(not isinstance(value, str) or not value or len(value) > 2048 for value in next_key.values())):
        raise ValueError('invalid legacy scan checkpoint')
    return LegacyProfilePage(tuple(references), next_key, count)


def reconcile_legacy_page(table, store, *, subject: str, limit: int,
                          checkpoint: dict | None = None) -> LegacyProfilePage:
    """Advance only after every reference in the page was reconciled.

    A failed page may have made partial idempotent writes; retry the same input
    checkpoint. A successful scan is not a consistent multi-page snapshot.
    """
    page = scan_legacy_profiles(table, subject=subject, limit=limit, checkpoint=checkpoint)
    for profile_id, version in page.references:
        store.ensure_condition_profile_listed(subject, profile_id, version)
    return page
