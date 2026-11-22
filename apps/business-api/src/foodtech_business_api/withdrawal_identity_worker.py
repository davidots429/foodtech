"""One explicitly enabled identity-release pass; never whole-account erasure.

Run only with a dedicated worker role. Scheduling, rollout, IAM and independent
business-data/backup erasure remain deployment responsibilities.
"""
import json
import os
import re
import sys

import boto3
from botocore.config import Config

from .principal_enrollment import DynamoPrincipalEnrollment
from .withdrawal_identity import CognitoWithdrawalRelease, IdentityReleaseCursor


def sweep_identities(worker, *, page_size=20, max_pages=1000):
    if type(page_size) is not int or not 1 <= page_size <= 100 or \
            type(max_pages) is not int or not 1 <= max_pages <= 10000:
        raise ValueError('invalid identity worker bounds')
    totals = {'pages': 0, 'identity_removed': 0, 'blocked': 0, 'reconciliation_required': 0,
              'scan_finished': False, 'deletion_complete': False}
    cursor, seen = None, set()
    for _ in range(max_pages):
        page = worker.scan_page(limit=page_size, cursor=cursor)
        if not isinstance(page, dict) or set(page) != {'results', 'next_cursor'} or not isinstance(page['results'], list):
            raise ValueError('invalid identity worker page')
        for result in page['results']:
            if not isinstance(result, dict) or set(result) != {'state', 'deletion_complete'} or \
                    result['deletion_complete'] is not False or result['state'] not in {
                        'COGNITO_REMOVED', 'BLOCKED', 'RECONCILIATION_REQUIRED'}:
                raise ValueError('invalid identity worker result')
            name = {'COGNITO_REMOVED': 'identity_removed', 'BLOCKED': 'blocked',
                    'RECONCILIATION_REQUIRED': 'reconciliation_required'}[result['state']]
            totals[name] += 1
        totals['pages'] += 1
        cursor = page['next_cursor']
        if cursor is None:
            totals['scan_finished'] = True
            return totals
        if not isinstance(cursor, IdentityReleaseCursor) or cursor.table_name != worker.auth_table.name or \
                not isinstance(cursor.key, dict) or set(cursor.key) != {'pk'} or \
                not isinstance(cursor.key['pk'], str) or not 1 <= len(cursor.key['pk']) <= 2048:
            raise ValueError('invalid identity worker continuation')
        # Sensitive cursor keys stay only in memory, without JSON serialization,
        # logs, HTTP exposure or persistent checkpoints.
        token = cursor.key['pk']
        if token in seen:
            raise RuntimeError('identity worker pagination did not advance')
        seen.add(token)
    raise RuntimeError('identity worker page limit exceeded')


def configured_worker():
    if os.getenv('FOODTECH_WITHDRAWAL_IDENTITY_WORKER_ENABLED') != 'true':
        raise ValueError('identity worker is not explicitly enabled')
    auth = os.getenv('FOODTECH_AUTH_TABLE', '')
    registry = os.getenv('FOODTECH_DELETION_REGISTRY_TABLE', '')
    pool = os.getenv('FOODTECH_COGNITO_USER_POOL_ID', '')
    auth_match = re.fullmatch(r'foodtech-auth-sessions-([a-z0-9-]{1,32})', auth)
    registry_match = re.fullmatch(r'foodtech-deletion-registry-([a-z0-9-]{1,32})', registry)
    if not auth_match or not registry_match or auth_match[1] != registry_match[1] or \
            not re.fullmatch(r'ap-southeast-2_[A-Za-z0-9]+', pool):
        raise ValueError('explicit matching-stage identity worker resources are required')
    session = boto3.Session(region_name='ap-southeast-2')
    config = Config(retries={'mode': 'standard', 'total_max_attempts': 3}, connect_timeout=5, read_timeout=15)
    table = session.resource('dynamodb', config=config).Table(auth)
    enrollment = DynamoPrincipalEnrollment(table=table)
    # No automatic bootstrap/reset and no empty-pass success with a missing gate.
    enrollment.fence()
    return CognitoWithdrawalRelease(auth_table=table, registry_table=registry, enrollment=enrollment,
        cognito_client=session.client('cognito-idp', config=config), pool_id=pool)


def main():
    try:
        page_size = int(os.getenv('FOODTECH_WITHDRAWAL_IDENTITY_PAGE_SIZE', '20'))
        max_pages = int(os.getenv('FOODTECH_WITHDRAWAL_IDENTITY_MAX_PAGES', '1000'))
        if not 1 <= page_size <= 100 or not 1 <= max_pages <= 10000:
            raise ValueError
        # Check configuration before any AWS client or credential discovery.
        worker = configured_worker()
    except ValueError:
        print('Explicit valid identity worker configuration is required.', file=sys.stderr)
        return 2
    except Exception:
        print('Identity worker initialization failed.', file=sys.stderr)
        return 1
    try:
        result = sweep_identities(worker, page_size=page_size, max_pages=max_pages)
    except Exception:
        # Never expose keys, subject/request IDs, SDK values or tracebacks.
        print('Identity release pass failed; reconciliation may be required.', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result['blocked'] or result['reconciliation_required'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
