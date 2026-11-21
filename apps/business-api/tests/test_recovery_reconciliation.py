import unittest

from foodtech_contracts.recovery_reconciliation import (
    Reapplication, StoreResult, Tombstone, assess_recovery,
)


class RecoveryReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tombstones = (Tombstone("old-request", "COMPLETED"), Tombstone("active-request", "DELETING"))
        self.results = (
            Reapplication("old-request", True, "NOT_FOUND", (
                StoreResult("vault", "NOT_FOUND", True), StoreResult("prompt-versions", "NOT_FOUND", True))),
            Reapplication("active-request", True, "DELETED", (
                StoreResult("vault", "DELETED", True), StoreResult("prompt-versions", "NOT_FOUND", True))),
        )
        self.inputs = {"tombstones": self.tombstones, "results": self.results,
                       "required_stores": frozenset({"vault", "prompt-versions"}),
                       "registry_snapshot_complete": True, "current_registry_confirmed": True,
                       "common_recovery_set_confirmed": True, "isolated_environment_confirmed": True,
                       "store_inventory_complete": True}

    def test_all_retained_requests_and_all_stores_must_be_accounted_for(self):
        result = assess_recovery(**self.inputs)
        self.assertTrue(result.eligible)
        self.assertEqual((result.tombstone_count, result.deleted_count, result.absent_count), (2, 1, 1))
        self.assertIn("RESULT_SET_MISMATCH", assess_recovery(**{**self.inputs, "results": self.results[1:]}).reasons)
        self.assertIn("RESULT_SET_MISMATCH", assess_recovery(**{**self.inputs, "results": self.results + self.results[:1]}).reasons)
        self.assertIn("INVALID_REGISTRY_SNAPSHOT", assess_recovery(**{
            **self.inputs, "tombstones": self.tombstones + self.tombstones[:1]}).reasons)

    def test_incomplete_registry_or_recovery_set_never_passes(self):
        for key in ("registry_snapshot_complete", "current_registry_confirmed", "common_recovery_set_confirmed",
                    "isolated_environment_confirmed", "store_inventory_complete"):
            with self.subTest(key=key):
                self.assertFalse(assess_recovery(**{**self.inputs, key: False}).eligible)
        self.assertIn("STORE_INVENTORY_UNVERIFIED", assess_recovery(**{
            **self.inputs, "required_stores": frozenset()}).reasons)

    def test_capsule_missing_store_and_unverified_residual_block_recovery(self):
        invalid = (
            Reapplication("old-request", False, "NOT_FOUND", (StoreResult("vault", "NOT_FOUND", True),)),
            Reapplication("active-request", True, "DELETED", (
                StoreResult("vault", "DELETED", True), StoreResult("prompt-versions", "NOT_FOUND", False))),
        )
        result = assess_recovery(**{**self.inputs, "results": invalid})
        self.assertFalse(result.eligible)
        self.assertTrue({"CAPSULE_UNAVAILABLE", "STORE_EVIDENCE_MISMATCH", "STORE_VERIFICATION_INCOMPLETE"}
                        <= set(result.reasons))

    def test_aggregate_counts_do_not_hide_failed_or_contradictory_store_results(self):
        results = (self.results[0], Reapplication("active-request", True, "DELETED", (
            StoreResult("vault", "FAILED", True), StoreResult("prompt-versions", "NOT_FOUND", True))))
        result = assess_recovery(**{**self.inputs, "results": results})
        self.assertEqual(result.deleted_count + result.absent_count, result.tombstone_count)
        self.assertFalse(result.eligible)
        self.assertIn("STORE_VERIFICATION_INCOMPLETE", result.reasons)
        self.assertIn("OUTCOME_INCONSISTENT", result.reasons)

    def test_empty_verified_registry_can_pass_without_inventing_a_request(self):
        result = assess_recovery(**{**self.inputs, "tombstones": (), "results": ()})
        self.assertTrue(result.eligible)
        self.assertEqual(result.tombstone_count, 0)


if __name__ == "__main__":
    unittest.main()
