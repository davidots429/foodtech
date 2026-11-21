"""Opt-in synthetic refresh lease checks on the existing development table."""
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
from foodtech_business_api.auth_store import DynamoAuthStore, Session


def main():
    if os.environ.get('AWS_PROFILE') != 'foodtech':
        raise SystemExit('Use the foodtech development profile')
    session = boto3.Session(profile_name='foodtech', region_name='ap-southeast-2')
    if session.client('sts').get_caller_identity()['Account'] != '573693340100':
        raise SystemExit('Unexpected AWS account')
    store = DynamoAuthStore('foodtech-auth-sessions-dev')
    session_id = 'synthetic-refresh-' + uuid.uuid4().hex
    now = int(time.time())
    try:
        store.save_session(session_id, Session('customer', 'synthetic-subject', None, 'synthetic-csrf',
            now + 600, 0, refresh_ciphertext=b'synthetic-ciphertext', refresh_deadline=now + 600))
        leases = ['synthetic-lease-' + uuid.uuid4().hex for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda lease: store.claim_refresh(session_id, lease, now, now + 30), leases))
        winners = [index for index, row in enumerate(results) if row]
        if len(winners) != 1:
            raise AssertionError('Exactly one concurrent claim must succeed')
        winner = leases[winners[0]]
        if not store.finish_refresh(session_id, winner, 0, now + 1, 'synthetic-access', b'rotated', now + 400):
            raise AssertionError('Valid rotation failed')
        if store.finish_refresh(session_id, winner, 0, now + 2, 'stale', b'stale', now + 400):
            raise AssertionError('Stale rotation succeeded')
        second = 'synthetic-lease-' + uuid.uuid4().hex
        if store.claim_refresh(session_id, second, now + 2, now + 32) is None:
            raise AssertionError('Second lease failed')
        removed = store.take_session(session_id)
        if removed is None or removed.refresh_ciphertext != b'rotated':
            raise AssertionError('Logout did not atomically retrieve the latest token')
        if store.finish_refresh(session_id, second, 1, now + 3, 'stale', b'stale', now + 400):
            raise AssertionError('Logout was undone')
        if store.table.get_item(Key={'pk': 'SESSION#' + session_id}, ConsistentRead=True).get('Item'):
            raise AssertionError('Deleted session was recreated')
        print('synthetic refresh claims, rotation and logout race verified')
    finally:
        store.delete_session(session_id)
        if store.table.get_item(Key={'pk': 'SESSION#' + session_id}, ConsistentRead=True).get('Item'):
            raise AssertionError('Synthetic session cleanup failed')
        print('synthetic session cleanup verified')


if __name__ == '__main__':
    main()
