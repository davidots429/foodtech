import copy
import time
import unittest
from types import SimpleNamespace
from uuid import uuid4

from botocore.exceptions import ReadTimeoutError

from foodtech_business_api.account_withdrawal import withdrawal_change
from foodtech_business_api.guarded_deletion import VaultSnapshot
from foodtech_business_api.withdrawal_dispatch import WithdrawalDispatcher, WithdrawalScanCursor


class WithdrawalDispatchTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.now = int(time.time())
        self.record = {'pk': f'USER#{self.subject}', 'epoch': 0}
        self.record.update(withdrawal_change(self.record, subject=self.subject, request_id=self.request,
            now=self.now - 604801, action='request'))
        self.table = SimpleNamespace(name='auth', get_item=self.read, scan=self.scan)
        self.identity = SimpleNamespace(auth_table='auth', vault_table='vault', capsule_table='capsule',
                                        resolve_deletion=self.resolve)
        self.deletion = SimpleNamespace(auth_table='auth', vault_table='vault', capsule_table='capsule',
                                       lock_and_register=self.lock)
        self.buffers, self.calls, self.scans = [], [], []
        self.fail = False
        self.after_commit_failure = False
        self.worker = WithdrawalDispatcher(auth_table=self.table, identity=self.identity,
            cipher=SimpleNamespace(seal=self.seal), deletion=self.deletion, clock=lambda: self.now)
        self.worker.withdrawal = SimpleNamespace(transition=self.transition)

    def read(self, **kwargs):
        self.assertTrue(kwargs['ConsistentRead'])
        return {'Item': copy.deepcopy(self.record)} if self.record else {}

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        return {'Items': [{'pk': f'USER#{self.subject}', 'withdrawal_request_id': self.request}]}

    def transition(self, **kwargs):
        changes = withdrawal_change(self.record, **kwargs)
        if changes:
            self.record.update(changes)
        return copy.deepcopy(self.record)

    def resolve(self, **kwargs):
        self.assertEqual(kwargs['withdrawal_request_id'], self.request)
        self.assertEqual(kwargs['expected_epoch'], self.record['epoch'])
        self.assertEqual(self.record['withdrawal_status'], 'DELETING')
        value = bytearray(b'synthetic-internal-identifier')
        self.buffers.append(value)
        return VaultSnapshot(self.subject, str(uuid4()), 1, 'A' * 43, 1, self.record['epoch'],
            self.request, self.record['withdrawal_requested_at'], self.record['withdrawal_deadline']), value

    def seal(self, request_id, user, locator):
        self.assertEqual(request_id, self.request)
        self.buffers.append(locator)
        return {'internal_user_id': b'synthetic-ciphertext', 'capsule_locator': b'synthetic-locator-ciphertext'}

    def lock(self, **kwargs):
        self.calls.append(kwargs)
        if self.after_commit_failure:
            self.record['deletion_lock_request'] = self.request
        if self.fail or self.after_commit_failure:
            raise ReadTimeoutError(endpoint_url='https://synthetic.invalid')

    def test_expiry_dispatch_locks_without_claiming_erasure_and_clears_buffers(self):
        result = self.worker.step(subject=self.subject, request_id=self.request)
        self.assertEqual(result, {'state': 'LOCKED'})
        self.assertEqual(self.record['withdrawal_status'], 'DELETING')
        self.assertEqual(len(self.calls), 1)
        self.assertNotEqual(self.calls[0]['attempt_id'], self.request)
        self.assertTrue(all(not any(buffer) for buffer in self.buffers))
        self.assertNotIn('complete', result)

    def test_timeout_before_commit_can_retry_with_new_attempt(self):
        self.fail = True
        page = self.worker.scan_page()
        self.assertEqual(page['results'], [{'state': 'RECONCILIATION_REQUIRED'}])
        self.assertTrue(all(not any(buffer) for buffer in self.buffers))
        self.fail = False
        self.assertEqual(self.worker.step(subject=self.subject, request_id=self.request), {'state': 'LOCKED'})
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.calls[0]['attempt_id'], self.calls[1]['attempt_id'])

    def test_uncertain_committed_lock_requires_independent_recovery(self):
        self.after_commit_failure = True
        self.assertEqual(self.worker.scan_page()['results'], [{'state': 'RECONCILIATION_REQUIRED'}])
        self.assertEqual(self.worker.step(subject=self.subject, request_id=self.request), {'state': 'RECOVERY_REQUIRED'})
        self.assertEqual(len(self.calls), 1)

    def test_cancelled_foreign_missing_and_future_requests_do_not_lock(self):
        self.assertEqual(self.worker.step(subject=self.subject, request_id=str(uuid4())), {'state': 'STALE'})
        self.record['withdrawal_deadline'] = self.now + 100
        self.assertEqual(self.worker.step(subject=self.subject, request_id=self.request), {'state': 'NOT_DUE'})
        self.record['withdrawal_status'] = 'CANCELED'
        self.assertEqual(self.worker.step(subject=self.subject, request_id=self.request), {'state': 'STALE'})
        self.record = None
        self.assertEqual(self.worker.step(subject=self.subject, request_id=self.request), {'state': 'STALE'})
        self.assertEqual(self.calls, [])

    def test_empty_scan_page_does_not_discard_sensitive_continuation(self):
        token = 'SESSION#synthetic-session-marker'
        self.table.scan = lambda **kwargs: {'Items': [], 'LastEvaluatedKey': {'pk': token}}
        result = self.worker.scan_page(limit=3)
        self.assertEqual(result['results'], [])
        self.assertNotIn(token, repr(result))
        cursor = result['next_cursor']
        self.table.scan = self.scan
        self.worker.scan_page(limit=3, cursor=cursor)
        self.assertEqual(self.scans[0]['ExclusiveStartKey'], {'pk': token})
        self.assertEqual(self.scans[0]['Limit'], 3)
        self.assertEqual(self.scans[0]['ProjectionExpression'], 'pk, withdrawal_request_id')
        with self.assertRaises(ValueError):
            self.worker.scan_page(cursor=WithdrawalScanCursor('other-auth', cursor.key))

    def test_mismatched_domains_and_invalid_candidate_fail_closed(self):
        self.identity.auth_table = 'other-auth'
        with self.assertRaises(ValueError):
            WithdrawalDispatcher(auth_table=self.table, identity=self.identity, cipher=None, deletion=self.deletion)
        self.table.scan = lambda **kwargs: {'Items': [{'pk': 'SESSION#sensitive', 'withdrawal_request_id': self.request}]}
        result = self.worker.scan_page()
        self.assertEqual(result['results'], [{'state': 'BLOCKED'}])
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
