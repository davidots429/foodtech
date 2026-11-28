"""Prepare restore targets using current Tombstones and an isolated Capsule copy."""

from .deletion_plan import prepare_from_resolved_inventory


class RestoredCapsulePlanWorker:
    """Supply derived targets to an internal recovery action.

    The coordinator must establish a common restore point and prevent writes to
    the restored set during verification. A downstream mutation must verify the
    active restore hold atomically with its write.
    """

    def __init__(self, *, locators, inventory, tenant_resolver, key_map,
                 approved_tenant_purposes, kms_client):
        registry_boundary = locators.live_registry_arn.split(":table/", 1)[0]
        capsule_boundary = inventory.restored_table_arn.split(":table/", 1)[0]
        if registry_boundary != capsule_boundary or kms_client is None:
            raise ValueError("Capsule recovery requires same-account storage and an explicit KMS client")
        self.locators = locators
        self.inventory = inventory
        self.tenant_resolver = tenant_resolver
        self.key_map = key_map
        self.approved_tenant_purposes = approved_tenant_purposes
        self.kms_client = kms_client

    def use_targets(self, expected, action) -> None:
        """Deliver all targets only after Capsule validation and a fresh Registry check."""
        if not callable(action):
            raise ValueError("explicit Capsule recovery action required")

        def prepare(internal_id, locator):
            targets = prepare_from_resolved_inventory(
                inventory=self.inventory, locator=locator.decode("ascii"),
                request_id=expected.tombstone.request_id, internal_id=internal_id,
                tenant_resolver=self.tenant_resolver, key_map=self.key_map,
                approved_purposes=self.inventory.allowed_purposes,
                approved_tenant_purposes=self.approved_tenant_purposes,
                kms_client=self.kms_client,
            )
            self.locators.require_current(expected)
            action(targets)

        self.locators.use(expected, prepare)
