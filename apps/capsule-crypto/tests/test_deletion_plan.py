import unittest
from dataclasses import replace

from foodtech_capsule_crypto.deletion_plan import EncryptedTarget, prepare_targets
from foodtech_capsule_crypto.contract import Entry
from foodtech_capsule_crypto.user_entry import seal_entry
from test_user_key import UserKeyTests


class DeletionPlanTests(UserKeyTests):
    def test_complete_inventory_opens_all_or_returns_no_plan(self):
        record, first = self.generator.generate(tenant=self.tenant, pool_version='pool-v1', shard_id='shard-0',
                                                 use_key=self.seal)
        targets = (EncryptedTarget('BILLING', '2026-11', first),)
        options = dict(tenant=self.tenant, record=record, targets=targets, expected_count=1, key_map=self.key_map,
                       approved_purposes=self.purposes, approved_tenant_purposes=self.tenant_purposes,
                       kms_client=self.kms)
        result = prepare_targets(**options)
        self.assertEqual(result.entries, (self.entry,))
        self.assertNotIn(self.entry.pseudonym_digest, repr(result))
        later = Entry('BILLING', '2026-12', self.entry.pseudonym_digest)
        second = self.unwrapper.use(tenant=self.tenant, record=record,
            use_key=lambda key, rec: seal_entry(key=key, record=rec, key_map=self.key_map, tenant=self.tenant,
                entry=later, approved_purposes=self.purposes, approved_tenant_purposes=self.tenant_purposes))
        invalid_second = EncryptedTarget('BILLING', '2026-12', replace(second, user_key_id='bad'))
        with self.assertRaises(ValueError):
            prepare_targets(**{**options, 'targets': targets + (invalid_second,), 'expected_count': 2})
        for change in ({'expected_count': 2}, {'targets': targets * 2, 'expected_count': 2},
                       {'targets': (replace(targets[0], epoch='2026-10'),)},
                       {'targets': (replace(targets[0], envelope=replace(first, user_key_id='bad')),)},
                       {'tenant': 'invalid'}):
            with self.subTest(change=tuple(change)), self.assertRaises(ValueError):
                prepare_targets(**{**options, **change})

    def test_empty_unkeyed_inventory_requires_zero_count(self):
        args = dict(tenant=self.tenant, record=None, targets=(), expected_count=0, key_map=self.key_map,
                    approved_purposes=self.purposes, approved_tenant_purposes=self.tenant_purposes,
                    kms_client=self.kms)
        self.assertEqual(prepare_targets(**args).entries, ())
        with self.assertRaises(ValueError):
            prepare_targets(**{**args, 'expected_count': 1})


if __name__ == '__main__':
    unittest.main()
