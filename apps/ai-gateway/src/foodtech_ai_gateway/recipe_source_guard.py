"""Strong-read recheck of a synthetic Jeyuk execution's recorded source versions.

This detects changed or missing records; it does not certify history coverage,
validate sensor samples, establish the immediately previous job, or authorize
an AI request. Those checks must be performed independently.
"""
import re
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from boto3.dynamodb.conditions import Key
from foodtech_contracts.jeyuk_process import PROFILES
from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_receipt import matches_jeyuk_snapshot

from .policy import Execution

MAX_SOURCE_ROWS = max(profile.time_bands[-1] for profile in PROFILES.values())
MAX_RECEIPTS = 100


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        return None
    if value != int(value) or not 1 <= value <= 10**18:
        return None
    return int(value)


def _matches_job_timeline(job: dict, raw: list[dict]) -> bool:
    try:
        placed = datetime.fromisoformat(job['actual_placed_at'].replace('Z', '+00:00'))
        lifted = datetime.fromisoformat(job['actual_lifted_at'].replace('Z', '+00:00'))
        duration = _positive_integer(job.get('actual_cooking_seconds'))
        if placed.utcoffset() is None or lifted.utcoffset() is None or duration is None or \
                len(raw) != duration or lifted - placed != timedelta(seconds=duration):
            return False
        return all(decode_jeyuk_mock_sample(row['raw']).observed_at == placed + timedelta(seconds=index)
                   for index, row in enumerate(raw, 1))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return False


class DynamoRecipeSourceGuard:
    def __init__(self, cooking_table: str, data_table: str, *, resource):
        if not isinstance(cooking_table, str) or not isinstance(data_table, str) or \
                not re.fullmatch(r'foodtech-cooking-jobs-[a-z0-9-]{1,32}', cooking_table) or \
                not re.fullmatch(r'foodtech-data-flow-[a-z0-9-]{1,32}', data_table):
            raise ValueError('separate cooking and source tables are required')
        if resource is None:
            raise ValueError('an explicitly scoped DynamoDB resource is required')
        self.cooking = resource.Table(cooking_table)
        self.data = resource.Table(data_table)

    def verify(self, execution: Execution) -> bool:
        """Return false for unknown, mismatched, or no-longer-current references."""
        subject = execution.tenant_id
        if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', subject):
            return False
        bindings = ((execution.job_id, execution.job_version, execution.jeyuk_source_revision),
                    (execution.prior_job_id, execution.prior_job_version, execution.prior_source_revision))
        if not all(job and _positive_integer(version) and _positive_integer(revision)
                   for job, version, revision in bindings) or bindings[0][0] == bindings[1][0]:
            return False
        first = self._read_snapshot(subject, bindings)
        return first is not None and first == self._read_snapshot(subject, bindings)

    def _read_snapshot(self, subject, bindings):
        observed = []
        for job_id, expected_version, expected_revision in bindings:
            try:
                if not isinstance(job_id, str) or str(UUID(job_id)) != job_id:
                    return None
            except ValueError:
                return None
            pk = f'SCOPE#USER#{subject}#JOB#{job_id}'
            job = self.cooking.get_item(Key={'pk': pk, 'sk': 'META'}, ConsistentRead=True).get('Item')
            if not job or job.get('owner_subject') != subject or job.get('job_id') != job_id or \
                    _positive_integer(job.get('version')) != expected_version or job.get('menu') != 'JEYUK' or \
                    job.get('provenance') != 'SYNTHETIC':
                return None
            binding = job.get('source_binding')
            if not isinstance(binding, dict) or binding.get('source_id') != 'synthetic-rest' or \
                    binding.get('device_id') != 'mockprofile-110-a-001':
                return None
            batch = binding.get('batch_id')
            if not isinstance(batch, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', batch):
                return None
            reverse = self.cooking.get_item(Key={'pk': f'JEYUK#{batch}', 'sk': 'BINDING'},
                                            ConsistentRead=True).get('Item')
            if not reverse or reverse.get('job_pk') != pk or reverse.get('job_id') != job_id:
                return None
            source = self.data.get_item(Key={'pk': f'JEYUK#{batch}', 'sk': 'SOURCE#REVISION'},
                                        ConsistentRead=True).get('Item')
            if not source or _positive_integer(source.get('revision')) != expected_revision:
                return None
            if expected_revision > MAX_SOURCE_ROWS:
                return None
            raw = self._read_prefix(batch, 'RAW#', expected_revision)
            receipts = self._read_prefix(batch, 'COLLECTION#RECEIPT#', MAX_RECEIPTS)
            if raw is None or receipts is None or len(raw) != expected_revision or \
                    not any(matches_jeyuk_snapshot(receipt, batch, raw) for receipt in receipts) or \
                    not _matches_job_timeline(job, raw):
                return None
            observed.append((batch, deepcopy(job), deepcopy(reverse), deepcopy(source), deepcopy(raw), deepcopy(receipts)))
        return tuple(observed) if observed[0][0] != observed[1][0] else None

    def _read_prefix(self, batch: str, prefix: str, limit: int) -> list[dict] | None:
        rows = []
        paginator = self.data.meta.client.get_paginator('query')
        for page in paginator.paginate(TableName=self.data.name,
                KeyConditionExpression=Key('pk').eq(f'JEYUK#{batch}') & Key('sk').begins_with(prefix),
                ConsistentRead=True, PaginationConfig={'PageSize': min(limit + 1, 100)}):
            rows.extend(page.get('Items', []))
            if len(rows) > limit:
                return None
        return rows
