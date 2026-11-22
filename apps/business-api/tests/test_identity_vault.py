import unittest
import copy
import time
from dataclasses import replace
from uuid import uuid4

import boto3
from botocore.stub import ANY, Stubber
from foodtech_business_api.identity_vault import IdentityVault, IdentityVaultConflict


class Client:
    def __init__(self):
        self.requests = []
        self.responses = []

    def transact_write_items(self, **kwargs):
        self.requests.append(kwargs)

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        return {"Item": self.responses.pop(0)}


class IdentityVaultTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.vault = IdentityVault(auth_table="auth", vault_table="vault", capsule_table="capsule",
                                   approved_purposes=frozenset({"BILLING"}), client=self.client)
        self.subject, self.request_id = str(uuid4()), str(uuid4())
        self.prepared = self.vault.prepare(authenticated_subject=self.subject, request_id=self.request_id, expected_epoch=0)

    def items(self):
        p = self.prepared
        principal = {"pk": {"S": f"USER#{self.subject}"}, "epoch": {"N": "0"}, "vault_id": {"S": p.vault_id}}
        vault = {"pk": {"S": p.vault_id}, "sk": {"S": "META"}, "state": {"S": "ACTIVE"},
                 "revision": {"N": "1"}, "internal_user_id": {"S": p.internal_id}, "capsule_locator": {"S": p.locator},
                 "provision_request_id": {"S": p.request_id}}
        capsule = {"pk": {"S": p.locator}, "sk": {"S": "META"}, "state": {"S": "ACTIVE"}, "revision": {"N": "1"}}
        return principal, vault, capsule

    def test_random_identifiers_and_atomic_link_do_not_expose_private_values_in_auth(self):
        p = self.prepared
        self.assertNotEqual(p.internal_id, self.subject)
        self.assertNotIn(p.internal_id, repr(p))
        self.assertNotIn(p.locator, repr(p))
        self.vault.commit(authenticated_subject=self.subject, prepared=p)
        request = self.client.requests[0]
        self.assertEqual(request["ClientRequestToken"], self.request_id)
        auth, vault, capsule = request["TransactItems"]
        self.assertIn("attribute_exists(pk)", auth["Update"]["ConditionExpression"])
        self.assertEqual(auth["Update"]["ExpressionAttributeValues"][":vault"], {"S": p.vault_id})
        self.assertNotIn(p.locator, str(auth))
        self.assertNotIn(p.internal_id, str(auth))
        self.assertEqual(vault["Put"]["Item"]["internal_user_id"], {"S": p.internal_id})
        self.assertEqual(capsule["Put"]["Item"]["pk"], {"S": p.locator})

    def test_resolve_checks_owner_and_versions_without_exposing_in_repr(self):
        principal, vault, capsule = self.items()
        self.client.responses = [principal, vault, capsule, principal]
        snapshot, internal_id = self.vault.resolve_deletion(authenticated_subject=self.subject, expected_epoch=0)
        self.assertEqual(bytes(internal_id), self.prepared.internal_id.encode())
        self.assertEqual((snapshot.vault_id, snapshot.vault_revision, snapshot.capsule_revision, snapshot.auth_epoch),
                         (self.prepared.vault_id, 1, 1, 0))
        self.assertNotIn(self.prepared.locator, repr(snapshot))
        internal_id[:] = b"\0" * len(internal_id)

    def test_fresh_service_can_resolve_persisted_link_without_prepared_request(self):
        principal, vault, capsule = self.items()
        client = Client()
        client.responses = [principal, vault, capsule, principal]
        restarted = IdentityVault(auth_table="auth", vault_table="vault", capsule_table="capsule",
                                  approved_purposes=frozenset({"BILLING"}), client=client)
        snapshot, internal_id = restarted.resolve_deletion(authenticated_subject=self.subject, expected_epoch=0)
        try:
            self.assertEqual(snapshot.vault_id, self.prepared.vault_id)
            self.assertEqual(bytes(internal_id), self.prepared.internal_id.encode())
        finally:
            internal_id[:] = b"\0" * len(internal_id)

    def test_owner_change_suspension_and_stale_capsule_fail_closed(self):
        principal, vault, capsule = self.items()
        cases = ([{**principal, "suspended": {"BOOL": True}}],
                 [{**principal, "suspended": {"NULL": True}}],
                 [principal, {**vault, "state": {"S": "DELETION_LOCKED"}}],
                 [principal, vault, {**capsule, "state": {"S": "DELETION_LOCKED"}}],
                 [principal, vault, capsule, {**principal, "vault_id": {"S": str(uuid4())}}],
                 [principal, vault, capsule, {**principal, "epoch": {"N": "1"}}])
        for responses in cases:
            with self.subTest(responses=len(responses)):
                self.client.responses = list(responses)
                with self.assertRaises(IdentityVaultConflict):
                    self.vault.resolve_deletion(authenticated_subject=self.subject, expected_epoch=0)

    def test_lost_response_verification_checks_the_exact_link_and_owner(self):
        principal, vault, capsule = self.items()
        self.client.responses = [principal, vault, capsule, principal, vault, principal, vault, capsule, principal]
        self.assertIsNone(self.vault.verify_committed(authenticated_subject=self.subject, prepared=self.prepared))
        self.client.responses = [principal, vault, capsule, principal, vault]
        with self.assertRaises(IdentityVaultConflict):
            self.vault.verify_committed(authenticated_subject=self.subject,
                                        prepared=replace(self.prepared, internal_id=str(uuid4())))
        self.client.responses = [principal, vault, capsule, principal, vault]
        with self.assertRaises(IdentityVaultConflict):
            self.vault.verify_committed(authenticated_subject=self.subject,
                                        prepared=replace(self.prepared, request_id=str(uuid4())))
        with self.assertRaises(ValueError):
            self.vault.verify_committed(authenticated_subject=str(uuid4()), prepared=self.prepared)
        self.client.responses = [{**principal, "deletion_lock_request": {"S": str(uuid4())}}]
        with self.assertRaises(IdentityVaultConflict):
            self.vault.verify_committed(authenticated_subject=self.subject, prepared=self.prepared)

    def test_lost_response_verification_rechecks_lock_after_vault_read(self):
        principal, vault, capsule = self.items()
        locked = {**principal, 'deletion_lock_request': {'S': str(uuid4())}, 'suspended': {'BOOL': True}}
        self.client.responses = [principal, vault, capsule, principal, vault, locked]
        with self.assertRaises(IdentityVaultConflict):
            self.vault.verify_committed(authenticated_subject=self.subject, prepared=self.prepared)
        self.client.responses = [principal, vault, capsule, principal, vault, principal,
                                 {**vault, 'revision': {'N': '2'}}, capsule, principal]
        with self.assertRaises(IdentityVaultConflict):
            self.vault.verify_committed(authenticated_subject=self.subject, prepared=self.prepared)

    def test_prepared_action_tampering_and_owner_mismatch_fail_before_write(self):
        action = {"Put": {**self.prepared.capsule_action["Put"], "TableName": "wrong"}}
        for prepared, subject in ((replace(self.prepared, capsule_action=action), self.subject),
                                  (self.prepared, str(uuid4()))):
            with self.subTest(subject=subject), self.assertRaises(ValueError):
                self.vault.commit(authenticated_subject=subject, prepared=prepared)
        self.assertEqual(self.client.requests, [])

    def test_botocore_accepts_atomic_creation_request(self):
        client = boto3.client("dynamodb", region_name="ap-southeast-2", aws_access_key_id="synthetic",
                              aws_secret_access_key="synthetic")
        vault = IdentityVault(auth_table="auth", vault_table="vault", capsule_table="capsule",
                              approved_purposes=frozenset({"BILLING"}), client=client)
        with Stubber(client) as stubber:
            stubber.add_response("transact_write_items", {}, {"ClientRequestToken": self.request_id, "TransactItems": ANY})
            vault.commit(authenticated_subject=self.subject, prepared=self.prepared)
            stubber.assert_no_pending_responses()


    def test_due_withdrawal_resolves_only_the_matching_expired_request(self):
        principal, vault, capsule = self.items()
        requested = int(time.time()) - 604801
        principal.update({'epoch': {'N': '2'}, 'suspended': {'BOOL': True},
            'withdrawal_status': {'S': 'DELETING'}, 'withdrawal_request_id': {'S': self.request_id},
            'withdrawal_requested_at': {'N': str(requested)},
            'withdrawal_deadline': {'N': str(requested + 604800)}, 'withdrawal_epoch': {'N': '1'}})
        self.client.responses = [principal, vault, capsule, copy.deepcopy(principal)]
        snapshot, internal_id = self.vault.resolve_deletion(authenticated_subject=self.subject,
            expected_epoch=2, withdrawal_request_id=self.request_id)
        self.assertEqual(snapshot.withdrawal_request_id, self.request_id)
        self.assertEqual(snapshot.withdrawal_deadline, requested + 604800)
        internal_id[:] = b'\x00' * len(internal_id)
        for changes in ({'withdrawal_status': {'S': 'PENDING'}}, {'withdrawal_status': {'S': 'CANCELED'}},
                        {'withdrawal_request_id': {'S': str(uuid4())}},
                        {'withdrawal_deadline': {'N': str(int(time.time()) + 100)}}):
            self.client.responses = [{**principal, **changes}]
            with self.subTest(changes=changes), self.assertRaises(IdentityVaultConflict):
                self.vault.resolve_deletion(authenticated_subject=self.subject,
                    expected_epoch=2, withdrawal_request_id=self.request_id)
        self.client.responses = [principal]
        with self.assertRaises(IdentityVaultConflict):
            self.vault.resolve_deletion(authenticated_subject=self.subject, expected_epoch=2)


if __name__ == "__main__":
    unittest.main()
