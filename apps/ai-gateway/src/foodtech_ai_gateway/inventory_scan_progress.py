"""Encrypted, lock-bound AI inventory progress; not deletion completion proof."""
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from boto3.dynamodb.types import Binary, TypeSerializer

from .legacy_tenant_inventory import reconcile_reserved_page

_INITIAL = {'pk': '__initial__', 'sk': '__initial__'}
_FINISHED = {'pk': '__finished__', 'sk': '__finished__'}


@dataclass(frozen=True)
class InventoryProgress:
    phase: str
    version: int


class InventoryScanProgressStore:
    def __init__(self, table, client, cipher, *, auth_table: str, deletion_registry_table: str):
        if not auth_table or not deletion_registry_table or not getattr(table, 'name', None) or \
                len({auth_table,deletion_registry_table,table.name}) != 3:
            raise ValueError('separate auth and progress tables required')
        self.table, self.client, self.cipher, self.auth_table = table, client, cipher, auth_table
        self.deletion_registry_table = deletion_registry_table
        self.serializer = TypeSerializer()

    def _av(self, data):
        return {key: self.serializer.serialize(value) for key, value in data.items()}

    def _job(self, job_id):
        try:
            if str(UUID(job_id)) != job_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError('invalid inventory job') from None
        return {'pk': f'AI_SCAN#{job_id}', 'sk': 'META'}

    def _lock(self, subject, request_id):
        return {'ConditionCheck': {'TableName': self.auth_table, 'Key': self._av({'pk': f'USER#{subject}'}),
                                   'ConditionExpression': 'deletion_lock_request = :request AND suspended = :yes',
                                   'ExpressionAttributeValues': self._av({':request': request_id, ':yes':True})}}

    def _request(self, request_id):
        return {'ConditionCheck': {'TableName':self.deletion_registry_table,
            'Key':self._av({'pk':'REGISTRY','sk':f'REQ#{request_id}'}),
            'ConditionExpression':'#status = :active','ExpressionAttributeNames':{'#status':'status'},
            'ExpressionAttributeValues':self._av({':active':'DELETING'})}}

    def start(self, job_id, subject, request_id, source_arn):
        key = self._job(job_id)
        ciphertext = self.cipher.seal(job_id, subject, _INITIAL, request_id=request_id,
                                      phase='INITIAL', version=1, source_table=source_arn)
        self.client.transact_write_items(TransactItems=[self._lock(subject, request_id),self._request(request_id),
            {'Put': {'TableName': self.table.name, 'Item': self._av({**key, 'phase': 'INITIAL', 'version': 1,
                                                                   'ciphertext': ciphertext}),
                     'ConditionExpression': 'attribute_not_exists(pk)'}}])

    def _read(self, job_id, subject, request_id, source_arn):
        key = self._job(job_id)
        item = self.table.get_item(Key=key, ConsistentRead=True).get('Item')
        if not isinstance(item, dict) or item.get('pk') != key['pk'] or item.get('sk') != key['sk'] or \
                item.get('phase') not in {'INITIAL', 'CONTINUE', 'SCAN_COMPLETE'}:
            raise ValueError('inventory progress unavailable')
        version = item.get('version')
        if isinstance(version, bool) or not isinstance(version, (int, Decimal)) or \
                (isinstance(version, Decimal) and (not version.is_finite() or version != int(version))):
            raise ValueError('invalid inventory version')
        version = int(version)
        if not 1 <= version < 2**63:
            raise ValueError('invalid inventory version')
        ciphertext = item.get('ciphertext')
        if isinstance(ciphertext, Binary):
            ciphertext = bytes(ciphertext)
        checkpoint = self.cipher.open(job_id, subject, ciphertext, request_id=request_id,
                                      phase=item['phase'], version=version, source_table=source_arn)
        if item['phase'] in {'INITIAL', 'SCAN_COMPLETE'}:
            if checkpoint != (_INITIAL if item['phase'] == 'INITIAL' else _FINISHED):
                raise ValueError('invalid inventory checkpoint')
            checkpoint = None
        return InventoryProgress(item['phase'], version), checkpoint

    def read_progress(self, job_id, subject, request_id, source_arn):
        return self._read(job_id, subject, request_id, source_arn)[0]

    def run_page(self, *, job_id, subject, request_id, source_table, limit):
        source_arn = getattr(source_table, 'table_arn', None)
        source_name = getattr(source_table, 'name', None)
        if not isinstance(source_arn, str) or not isinstance(source_name, str) or \
                source_name in {self.auth_table, self.deletion_registry_table, self.table.name}:
            raise ValueError('separate source, auth and progress tables required')
        progress, checkpoint = self._read(job_id, subject, request_id, source_arn)
        if progress.phase == 'SCAN_COMPLETE' or progress.version == 2**63 - 1:
            raise ValueError('inventory cannot advance')
        page = reconcile_reserved_page(source_table, self.client, auth_table=self.auth_table,
            registry_table=source_name, deletion_registry_table=self.deletion_registry_table, tenant=subject, request_id=request_id, limit=limit, checkpoint=checkpoint)
        phase = 'CONTINUE' if page.checkpoint is not None else 'SCAN_COMPLETE'
        next_checkpoint = page.checkpoint if page.checkpoint is not None else _FINISHED
        ciphertext = self.cipher.seal(job_id, subject, next_checkpoint, request_id=request_id,
                                      phase=phase, version=progress.version + 1, source_table=source_arn)
        self.client.transact_write_items(TransactItems=[self._lock(subject, request_id),self._request(request_id),
            {'Update': {'TableName': self.table.name, 'Key': self._av(self._job(job_id)),
                        'ConditionExpression': '#phase = :old AND #version = :expected',
                        'UpdateExpression': 'SET #phase = :next, #version = :next_version, ciphertext = :ciphertext',
                        'ExpressionAttributeNames': {'#phase': 'phase', '#version': 'version'},
                        'ExpressionAttributeValues': self._av({':old': progress.phase, ':expected': progress.version,
                            ':next': phase, ':next_version': progress.version + 1, ':ciphertext': ciphertext})}}])
        return InventoryProgress(phase, progress.version + 1)
