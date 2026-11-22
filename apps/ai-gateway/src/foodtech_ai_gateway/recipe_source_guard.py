"""Strong-read recheck of a synthetic Jeyuk execution's recorded source versions.

This detects changed or missing records; it does not certify history coverage,
validate sensor samples, establish the immediately previous job, or authorize
an AI request. Those checks must be performed independently.
"""
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from foodtech_contracts.jeyuk_process import PROFILES
from foodtech_contracts.jeyuk_mock import decode_jeyuk_mock_sample
from foodtech_contracts.jeyuk_receipt import matches_jeyuk_snapshot

from .policy import Execution, Feature

MAX_SOURCE_ROWS = max(profile.time_bands[-1] for profile in PROFILES.values())
MAX_RECEIPTS = 100


@dataclass(frozen=True)
class BoundRecipeSourceGuard:
    """Adapt a pre-authorized execution scope to the Gateway guard interface.

    A trusted caller must supply the execution and scope after authenticating
    and authorizing them. This adapter does not authenticate the caller, obtain
    credentials, or enforce AWS-level isolation.
    """
    guard: object = field(repr=False)
    execution: Execution = field(repr=False)
    job_partitions: tuple[str, str] = field(repr=False)
    source_partitions: tuple[str, str] = field(repr=False)

    def verify(self, execution: Execution) -> bool:
        if not isinstance(execution, Execution) or execution != self.execution:
            return False
        return self.guard.verify_scoped(execution, self.job_partitions, self.source_partitions) is True

    def settlement_checks(self):
        if type(self.guard) is not DynamoRecipeSourceGuard:
            raise ValueError('DynamoDB source guard is required')
        return self.guard.settlement_checks(self.execution, self.job_partitions, self.source_partitions)


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
        return self._verify(execution, None)

    def verify_scoped(self, execution: Execution, job_partitions, source_partitions) -> bool:
        """Reject changed bindings before accessing a different source partition.

        This code-level bound is not a replacement for AWS IAM isolation.
        """
        if not self._valid_scope(execution, job_partitions, source_partitions):
            return False
        return self._verify(execution, source_partitions)

    @staticmethod
    def _valid_scope(execution, job_partitions, source_partitions):
        if not isinstance(execution, Execution) or execution.feature != Feature.RECIPE_CORRECTION or \
                not isinstance(execution.tenant_id, str) or \
                not re.fullmatch(r'[A-Za-z0-9-]{1,128}', execution.tenant_id):
            return False
        subject = execution.tenant_id
        expected_jobs = (f'SCOPE#USER#{subject}#JOB#{execution.job_id}',
                         f'SCOPE#USER#{subject}#JOB#{execution.prior_job_id}')
        if not isinstance(job_partitions, tuple) or job_partitions != expected_jobs or \
                not isinstance(source_partitions, tuple) or len(source_partitions) != 2 or \
                any(not isinstance(value, str) or not re.fullmatch(r'JEYUK#[A-Za-z0-9_-]{1,128}', value)
                    for value in source_partitions) or source_partitions[0] == source_partitions[1]:
            return False
        try:
            if any(not isinstance(value, str) or str(UUID(value)) != value
                   for value in (execution.job_id, execution.prior_job_id)) or execution.job_id == execution.prior_job_id:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        return all(_positive_integer(value) is not None for value in
                   (execution.job_version, execution.prior_job_version,
                    execution.jeyuk_source_revision, execution.prior_source_revision))

    def settlement_checks(self, execution: Execution, job_partitions, source_partitions):
        """Build version and binding checks for a later DynamoDB transaction.

        The caller must verify raw records separately and ensure every writer
        increments the checked revisions. No AWS permission is granted here.
        """
        if not self._valid_scope(execution, job_partitions, source_partitions):
            raise ValueError('invalid recipe settlement scope')
        bindings = ((execution.job_id, execution.job_version, execution.jeyuk_source_revision),
                    (execution.prior_job_id, execution.prior_job_version, execution.prior_source_revision))
        snapshot = self._read_snapshot(execution.tenant_id, bindings, source_partitions)
        if snapshot is None or snapshot != self._read_snapshot(execution.tenant_id, bindings, source_partitions):
            raise ValueError('recipe source changed or is unavailable')
        serializer = TypeSerializer()
        encode = lambda values: {key: serializer.serialize(value) for key, value in values.items()}
        checks = []
        for index, (job_id, version, revision, job_pk, source_pk) in enumerate(zip(
                (execution.job_id, execution.prior_job_id),
                (execution.job_version, execution.prior_job_version),
                (execution.jeyuk_source_revision, execution.prior_source_revision), job_partitions, source_partitions)):
            batch = source_pk.removeprefix('JEYUK#')
            checks.append({'ConditionCheck': {'TableName': self.cooking.name,
                'Key': encode({'pk': job_pk, 'sk': 'META'}),
                'ConditionExpression': '#owner = :owner AND #job = :job AND #version = :version AND '
                    '#menu = :menu AND #provenance = :synthetic AND #binding.#source = :source AND '
                    '#binding.#device = :device AND #binding.#batch = :batch',
                'ExpressionAttributeNames': {'#owner': 'owner_subject', '#job': 'job_id', '#version': 'version',
                    '#menu': 'menu', '#provenance': 'provenance', '#binding': 'source_binding',
                    '#source': 'source_id', '#device': 'device_id', '#batch': 'batch_id'},
                'ExpressionAttributeValues': encode({':owner': execution.tenant_id, ':job': job_id, ':version': version,
                    ':menu': 'JEYUK', ':synthetic': 'SYNTHETIC', ':source': 'synthetic-rest',
                    ':device': 'mockprofile-110-a-001', ':batch': batch})}})
            checks.append({'ConditionCheck': {'TableName': self.cooking.name,
                'Key': encode({'pk': source_pk, 'sk': 'BINDING'}),
                'ConditionExpression': 'job_pk = :pk AND job_id = :job',
                'ExpressionAttributeValues': encode({':pk': job_pk, ':job': job_id})}})
            checks.append({'ConditionCheck': {'TableName': self.data.name,
                'Key': encode({'pk': source_pk, 'sk': 'SOURCE#REVISION'}),
                'ConditionExpression': '#revision = :revision', 'ExpressionAttributeNames': {'#revision': 'revision'},
                'ExpressionAttributeValues': encode({':revision': revision})}})
            raw, receipts = snapshot[index][4:6]
            receipt = next((item for item in receipts if item.get('pk') == source_pk and
                            isinstance(item.get('sk'), str) and item['sk'].startswith('COLLECTION#RECEIPT#') and
                            matches_jeyuk_snapshot(item, batch, raw)), None)
            if receipt is None:
                raise ValueError('matching receipt is unavailable')
            fields = ('receipt_version', 'provenance', 'profile_id', 'fixture_id', 'starting_cursor',
                      'ending_cursor', 'record_count', 'ordered_raw_sha256')
            checks.append({'ConditionCheck': {'TableName': self.data.name,
                'Key': encode({'pk': source_pk, 'sk': receipt['sk']}),
                'ConditionExpression': ' AND '.join(f'#f{i} = :v{i}' for i in range(len(fields))),
                'ExpressionAttributeNames': {f'#f{i}': name for i, name in enumerate(fields)},
                'ExpressionAttributeValues': encode({f':v{i}': receipt[name] for i, name in enumerate(fields)})}})
        return checks

    def _verify(self, execution: Execution, source_partitions) -> bool:
        subject = execution.tenant_id
        if not isinstance(subject, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', subject):
            return False
        bindings = ((execution.job_id, execution.job_version, execution.jeyuk_source_revision),
                    (execution.prior_job_id, execution.prior_job_version, execution.prior_source_revision))
        if not all(job and _positive_integer(version) and _positive_integer(revision)
                   for job, version, revision in bindings) or bindings[0][0] == bindings[1][0]:
            return False
        first = self._read_snapshot(subject, bindings, source_partitions)
        return first is not None and first == self._read_snapshot(subject, bindings, source_partitions)

    def _read_snapshot(self, subject, bindings, source_partitions=None):
        observed = []
        for index, (job_id, expected_version, expected_revision) in enumerate(bindings):
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
            if source_partitions is not None and f'JEYUK#{batch}' != source_partitions[index]:
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
        if observed[0][0] == observed[1][0]:
            return None
        try:
            current_start = datetime.fromisoformat(observed[0][1]['actual_placed_at'].replace('Z', '+00:00'))
            prior_start = datetime.fromisoformat(observed[1][1]['actual_placed_at'].replace('Z', '+00:00'))
            prior_end = datetime.fromisoformat(observed[1][1]['actual_lifted_at'].replace('Z', '+00:00'))
            if not prior_start < current_start or prior_end > current_start:
                return None
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            return None
        return tuple(observed)

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
