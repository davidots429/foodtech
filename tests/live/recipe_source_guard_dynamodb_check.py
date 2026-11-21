"""Opt-in synthetic version recheck against existing development tables."""
import os
import uuid
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

from foodtech_ai_gateway.policy import Execution, Feature
from foodtech_ai_gateway.recipe_source_guard import DynamoRecipeSourceGuard


def main():
    if os.environ.get('AWS_PROFILE') != 'foodtech':
        raise SystemExit('Use the foodtech development profile')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('Unexpected AWS account')
    resource = session.resource('dynamodb')
    jobs, sources = resource.Table('foodtech-cooking-jobs-dev'), resource.Table('foodtech-data-flow-dev')
    suffix = uuid.uuid4().hex
    owner = f'guard-test-{suffix}'
    current, prior = str(uuid.uuid4()), str(uuid.uuid4())
    source_ids = [f'guard-{suffix}-current', f'guard-{suffix}-prior']
    execution = Execution(str(uuid.uuid4()), owner, Feature.RECIPE_CORRECTION, 'synthetic', None, 'SYNTHETIC',
        Decimal('0'), job_id=current, job_version=7, prior_job_id=prior, prior_job_version=3,
        jeyuk_source_revision=30, prior_source_revision=29)
    created = []
    try:
        for job, version, batch, revision in zip((current, prior), (7, 3), source_ids, (30, 29)):
            job_key = {'pk': f'SCOPE#USER#{owner}#JOB#{job}', 'sk': 'META'}
            source_key = {'pk': f'JEYUK#{batch}', 'sk': 'SOURCE#REVISION'}
            jobs.put_item(Item={**job_key, 'owner_subject': owner, 'job_id': job, 'version': version,
                'menu': 'JEYUK', 'provenance': 'SYNTHETIC', 'source_binding': {'source_id': 'synthetic-rest',
                'device_id': 'mockprofile-110-a-001', 'batch_id': batch}}, ConditionExpression=Attr('pk').not_exists())
            created.append((jobs, job_key))
            binding_key = {'pk': f'JEYUK#{batch}', 'sk': 'BINDING'}
            jobs.put_item(Item={**binding_key, 'job_pk': job_key['pk'], 'job_id': job},
                          ConditionExpression=Attr('pk').not_exists())
            created.append((jobs, binding_key))
            sources.put_item(Item={**source_key, 'revision': revision}, ConditionExpression=Attr('pk').not_exists())
            created.append((sources, source_key))
        guard = DynamoRecipeSourceGuard(jobs.name, sources.name, resource=resource)
        if not guard.verify(execution): raise AssertionError('matching versions rejected')
        sources.update_item(Key={'pk': f'JEYUK#{source_ids[1]}', 'sk': 'SOURCE#REVISION'},
                            UpdateExpression='ADD revision :one', ExpressionAttributeValues={':one': 1})
        if guard.verify(execution): raise AssertionError('changed previous source accepted')
        print('synthetic source and job version recheck verified')
    finally:
        for table, key in reversed(created): table.delete_item(Key=key)
        if any(table.get_item(Key=key, ConsistentRead=True).get('Item') for table, key in created):
            raise RuntimeError('synthetic records remain')
        print('synthetic source guard cleanup verified')


if __name__ == '__main__': main()
