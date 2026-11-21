"""Fail-closed assessment of deletion reapplication to an isolated recovery set.

This evaluates supplied evidence only. It does not acquire a registry snapshot,
delete data, verify stores, or authorize a deployment.
"""
from dataclasses import dataclass
from typing import Literal


TOMBSTONE_STATES = frozenset({"DELETING", "COMPLETED", "RECONCILING", "RECONCILE_FAILED"})
OUTCOMES = frozenset({"DELETED", "NOT_FOUND"})


@dataclass(frozen=True)
class Tombstone:
    request_id: str
    status: str


@dataclass(frozen=True)
class StoreResult:
    store: str
    outcome: Literal["DELETED", "NOT_FOUND", "FAILED", "UNKNOWN"]
    residual_verified: bool


@dataclass(frozen=True)
class Reapplication:
    request_id: str
    capsule_recovered: bool
    outcome: Literal["DELETED", "NOT_FOUND", "FAILED", "UNKNOWN"]
    stores: tuple[StoreResult, ...]


@dataclass(frozen=True)
class RecoveryAssessment:
    eligible: bool
    tombstone_count: int
    deleted_count: int
    absent_count: int
    reasons: tuple[str, ...]


def assess_recovery(*, tombstones: tuple[Tombstone, ...], results: tuple[Reapplication, ...],
                    required_stores: frozenset[str], registry_snapshot_complete: bool,
                    current_registry_confirmed: bool, common_recovery_set_confirmed: bool,
                    isolated_environment_confirmed: bool, store_inventory_complete: bool) -> RecoveryAssessment:
    """Compare all retained registry entries to per-request and per-store evidence.

    Snapshot completeness/currentness, isolation and recovery-set consistency must
    be established by the caller using durable evidence; booleans are not proof.
    No timestamp filtering is applied to retained tombstones.
    """
    reasons: set[str] = set()
    if registry_snapshot_complete is not True or current_registry_confirmed is not True:
        reasons.add("REGISTRY_UNVERIFIED")
    if common_recovery_set_confirmed is not True or isolated_environment_confirmed is not True:
        reasons.add("RECOVERY_SET_UNVERIFIED")
    if store_inventory_complete is not True or not required_stores or \
            any(not isinstance(store, str) or not store.strip() for store in required_stores):
        reasons.add("STORE_INVENTORY_UNVERIFIED")
    requests = [item.request_id for item in tombstones]
    valid_requests = all(isinstance(item.request_id, str) and item.request_id.strip() and
                         isinstance(item.status, str) and item.status in TOMBSTONE_STATES for item in tombstones)
    if not valid_requests or len(set(requests)) != len(requests):
        reasons.add("INVALID_REGISTRY_SNAPSHOT")
    result_ids = [item.request_id for item in results]
    valid_results = all(isinstance(value, str) and value.strip() for value in result_ids)
    if not valid_requests or not valid_results or len(set(result_ids)) != len(result_ids) or set(result_ids) != set(requests):
        reasons.add("RESULT_SET_MISMATCH")
    deleted = absent = 0
    for result in results:
        if result.capsule_recovered is not True:
            reasons.add("CAPSULE_UNAVAILABLE")
        if result.outcome == "DELETED":
            deleted += 1
        elif result.outcome == "NOT_FOUND":
            absent += 1
        else:
            reasons.add("REAPPLICATION_INCOMPLETE")
        store_names = [store.store for store in result.stores]
        if not all(isinstance(name, str) and name.strip() for name in store_names) or \
                len(set(store_names)) != len(store_names) or set(store_names) != required_stores:
            reasons.add("STORE_EVIDENCE_MISMATCH")
        if any(not isinstance(store.outcome, str) or store.outcome not in OUTCOMES or
               store.residual_verified is not True for store in result.stores):
            reasons.add("STORE_VERIFICATION_INCOMPLETE")
        if result.outcome == "NOT_FOUND" and any(store.outcome != "NOT_FOUND" for store in result.stores):
            reasons.add("OUTCOME_INCONSISTENT")
        if result.outcome == "DELETED" and not any(store.outcome == "DELETED" for store in result.stores):
            reasons.add("OUTCOME_INCONSISTENT")
    if deleted + absent != len(tombstones):
        reasons.add("COUNT_MISMATCH")
    return RecoveryAssessment(not reasons, len(tombstones), deleted, absent, tuple(sorted(reasons)))
