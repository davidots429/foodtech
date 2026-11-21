"""Restricted, resumable profile scan progress; not deletion completion evidence.

The caller must authenticate and authorize the owner and job, provision a
separate protected table/key and prevent concurrent domain writes before using
any scan result for reconciliation. Scan completion alone proves none of those.
"""
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from boto3.dynamodb.types import Binary

from .legacy_profile_scan import reconcile_legacy_page

_INITIAL = {'pk': '__initial__', 'sk': '__initial__'}
_FINISHED = {'pk': '__finished__', 'sk': '__finished__'}


def _job(job_id):
    try:
        if str(UUID(job_id)) != job_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError('invalid scan job') from None
    return {'pk': f'PROFILE_SCAN#{job_id}', 'sk': 'META'}


@dataclass(frozen=True)
class Progress:
    phase: str
    version: int


class ProfileScanProgressStore:
    def __init__(self, table, cipher):
        self.table, self.cipher = table, cipher

    def start(self, job_id: str, subject: str) -> None:
        key = _job(job_id)
        encrypted = self.cipher.seal(job_id, subject, _INITIAL)
        self.table.put_item(Item={**key, 'phase': 'INITIAL', 'version': 1, 'ciphertext': encrypted},
                            ConditionExpression='attribute_not_exists(pk)')

    def run_page(self, *, job_id: str, subject: str, source_table, profile_store, limit: int) -> Progress:
        key = _job(job_id)
        item = self.table.get_item(Key=key, ConsistentRead=True).get('Item')
        if not isinstance(item, dict) or item.get('pk') != key['pk'] or item.get('sk') != key['sk'] or \
                item.get('phase') not in {'INITIAL', 'CONTINUE'}:
            raise ValueError('scan job is unavailable')
        version = item.get('version')
        if isinstance(version, bool) or not isinstance(version, (int, Decimal)) or \
                (isinstance(version, Decimal) and (not version.is_finite() or version != int(version))):
            raise ValueError('invalid scan job version')
        version = int(version)
        if not 1 <= version < 2**63 - 1:
            raise ValueError('invalid scan job version')
        encrypted = item.get('ciphertext')
        if isinstance(encrypted, Binary):
            encrypted = bytes(encrypted)
        checkpoint = self.cipher.open(job_id, subject, encrypted)
        if item['phase'] == 'INITIAL':
            if checkpoint != _INITIAL:
                raise ValueError('invalid initial checkpoint')
            checkpoint = None
        page = reconcile_legacy_page(source_table, profile_store, subject=subject, limit=limit, checkpoint=checkpoint)
        next_phase = 'CONTINUE' if page.checkpoint is not None else 'SCAN_COMPLETE'
        next_checkpoint = page.checkpoint if page.checkpoint is not None else _FINISHED
        ciphertext = self.cipher.seal(job_id, subject, next_checkpoint)
        self.table.update_item(Key=key, UpdateExpression='SET #phase = :next, #version = :next_version, ciphertext = :ciphertext',
            ConditionExpression='#phase = :old AND #version = :expected',
            ExpressionAttributeNames={'#phase': 'phase', '#version': 'version'},
            ExpressionAttributeValues={':old': item['phase'], ':expected': version, ':next': next_phase,
                                       ':next_version': version + 1, ':ciphertext': ciphertext})
        return Progress(next_phase, version + 1)
