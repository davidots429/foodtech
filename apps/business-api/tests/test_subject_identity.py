import unittest
from uuid import uuid4

from foodtech_business_api.account_withdrawal import withdrawal_change
from foodtech_business_api.auth_store import MemoryStore
from foodtech_business_api.identity_vault import IdentityVault
from foodtech_business_api.subject_identity import valid_subject


class SubjectIdentityTests(unittest.TestCase):
    def test_opaque_subject_is_preserved_across_enrollment_withdrawal_and_vault_preparation(self):
        subject = 'Cognito_opaque-subject-Vnext'
        self.assertTrue(valid_subject(subject))
        store = MemoryStore()
        record = store.enroll_principal(subject, store.enrollment_fence())
        self.assertEqual(record['pk'], 'USER#' + subject)
        request = str(uuid4())
        change = withdrawal_change(record, subject=subject, request_id=request, now=1800000000, action='request')
        self.assertEqual(change['withdrawal_status'], 'PENDING')
        vault = IdentityVault(auth_table='auth', vault_table='vault', capsule_table='capsule',
                              approved_purposes=frozenset({'BILLING'}), client=object())
        prepared = vault.prepare(authenticated_subject=subject, request_id=request, expected_epoch=0)
        self.assertEqual(prepared.subject, subject)
        with self.assertRaises(ValueError):
            withdrawal_change(record, subject=subject, request_id='opaque-request', now=1800000000, action='request')

    def test_key_delimiters_whitespace_and_unbounded_values_are_rejected_without_normalization(self):
        for value in ('', 'x' * 129, 'user#job', 'user/job', ' user', 'user ', 'email@example.invalid', None, 5):
            with self.subTest(value=value):
                self.assertFalse(valid_subject(value))
        self.assertTrue(valid_subject('A' * 128))


if __name__ == '__main__':
    unittest.main()
