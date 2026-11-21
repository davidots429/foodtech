"""Prepare deletion targets from a stable, restricted Capsule inventory.

This is an internal planning primitive. It does not authenticate callers, read
storage, persist a plan, delete records, or prove that an inventory is complete.
The caller must use a stable locked inventory and keep outputs inside the
separately authorized deletion boundary.
"""
from dataclasses import dataclass, field

from .contract import Entry, validate_tenant
from .user_entry import UserEntryEnvelope, open_entry
from .user_key import UserKeyRecord, UserKeyUnwrapper


@dataclass(frozen=True)
class EncryptedTarget:
    purpose: str
    epoch: str
    envelope: UserEntryEnvelope = field(repr=False)


@dataclass(frozen=True)
class DeletionTargets:
    entries: tuple[Entry, ...] = field(repr=False)


def prepare_targets(*, tenant: str, record: UserKeyRecord | None, targets: tuple[EncryptedTarget, ...],
                    expected_count: int, key_map: dict, approved_purposes: frozenset[str],
                    approved_tenant_purposes: frozenset[str], kms_client=None) -> DeletionTargets:
    """Open every target or return no plan; reject missing or repeated entries.

    ``expected_count`` and the entries must come from one completed stable
    inventory. This function cannot establish that provenance itself. Plaintext
    targets must never be logged or exposed outside the trusted worker.
    """
    validate_tenant(tenant, approved_tenant_purposes)
    if type(expected_count) is not int or expected_count < 0 or not isinstance(targets, tuple) or \
            len(targets) != expected_count or (record is None and targets):
        raise ValueError('invalid deletion inventory')
    if record is None:
        return DeletionTargets(())
    seen = set()
    for target in targets:
        if not isinstance(target, EncryptedTarget) or not isinstance(target.purpose, str) or \
                not isinstance(target.epoch, str) or (target.purpose, target.epoch) in seen:
            raise ValueError('invalid deletion inventory')
        seen.add((target.purpose, target.epoch))
    unwrapper = UserKeyUnwrapper(key_map, approved_tenant_purposes, kms_client=kms_client)
    def open_all(key, key_record):
        return DeletionTargets(tuple(open_entry(key=key, record=key_record, key_map=key_map, tenant=tenant,
            envelope=target.envelope, expected_purpose=target.purpose, expected_epoch=target.epoch,
            approved_purposes=approved_purposes, approved_tenant_purposes=approved_tenant_purposes)
            for target in targets))
    return unwrapper.use(tenant=tenant, record=record, use_key=open_all)
