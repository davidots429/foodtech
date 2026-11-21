"""Strong-read recheck of a synthetic Jeyuk execution's recorded source versions.

This detects changed or missing records; it does not certify history coverage,
validate sensor samples, establish the immediately previous job, or authorize
an AI request. Those checks must be performed independently.
"""
import re
from decimal import Decimal
from uuid import UUID

import boto3

from .policy import Execution


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        return None
    if value != int(value) or not 1 <= value <= 10**18:
        return None
    return int(value)


class DynamoRecipeSourceGuard:
    def __init__(self, cooking_table: str, data_table: str, *, resource=None):
        if not isinstance(cooking_table, str) or not isinstance(data_table, str) or \
                not re.fullmatch(r'foodtech-cooking-jobs-[a-z0-9-]{1,32}', cooking_table) or \
                not re.fullmatch(r'foodtech-data-flow-[a-z0-9-]{1,32}', data_table):
            raise ValueError('separate cooking and source tables are required')
        resource = resource or boto3.Session(region_name='ap-southeast-2').resource('dynamodb')
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
        observed = []
        for job_id, expected_version, expected_revision in bindings:
            try:
                if not isinstance(job_id, str) or str(UUID(job_id)) != job_id:
                    return False
            except ValueError:
                return False
            pk = f'SCOPE#USER#{subject}#JOB#{job_id}'
            job = self.cooking.get_item(Key={'pk': pk, 'sk': 'META'}, ConsistentRead=True).get('Item')
            if not job or job.get('owner_subject') != subject or job.get('job_id') != job_id or \
                    _positive_integer(job.get('version')) != expected_version or job.get('menu') != 'JEYUK' or \
                    job.get('provenance') != 'SYNTHETIC':
                return False
            binding = job.get('source_binding')
            if not isinstance(binding, dict) or binding.get('source_id') != 'synthetic-rest' or \
                    binding.get('device_id') != 'mockprofile-110-a-001':
                return False
            batch = binding.get('batch_id')
            if not isinstance(batch, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', batch):
                return False
            source = self.data.get_item(Key={'pk': f'JEYUK#{batch}', 'sk': 'SOURCE#REVISION'},
                                        ConsistentRead=True).get('Item')
            if not source or _positive_integer(source.get('revision')) != expected_revision:
                return False
            observed.append(batch)
        return observed[0] != observed[1]
