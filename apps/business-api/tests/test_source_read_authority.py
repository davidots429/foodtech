"""No AWS calls: independent control records must authorize both sources."""
import copy
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from foodtech_business_api.recipe_scope import RecipeReadScope
from foodtech_business_api.source_read_authority import DynamoSourceReadAuthority


class SourceReadAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.scope = RecipeReadScope('opaque_subject', str(uuid4()), ('job-current', 'job-prior'),
            ('JEYUK#jeyuk-current', 'JEYUK#jeyuk-prior'), (2, 1), (10, 9), 'PENDING', 1, 3)
        self.refs = [str(uuid4()), str(uuid4())]
        self.principal = {'pk': 'USER#opaque_subject', 'epoch': 3, 'suspended': False,
            'source_read_grants': dict(zip(self.scope.source_partitions, self.refs))}
        self.grants = [{'pk': 'GRANT#' + ref, 'sk': 'META', 'schema': 1, 'grant_id': ref,
            'source_partition': source, 'permission': 'RECIPE_SOURCE_READ', 'state': 'ACTIVE',
            'version': 1, 'expires_at': 2000} for ref, source in zip(self.refs, self.scope.source_partitions)]
        self.serializer, self.client = TypeSerializer(), MagicMock()
        self.authority = DynamoSourceReadAuthority(auth_table='foodtech-auth-sessions-dev',
            catalog_table='foodtech-source-access-dev', client=self.client, clock=lambda: 1000)
        self.responses()

    def encode(self, row):
        return {key: self.serializer.serialize(value) for key, value in row.items()}

    def responses(self, current=None, grants=None):
        self.client.get_item.return_value = {'Item': self.encode(self.principal)}
        self.client.transact_get_items.return_value = {'Responses': [
            {'Item': self.encode(current if current is not None else self.principal)},
            *({'Item': self.encode(g)} for g in (grants if grants is not None else self.grants))]}

    def test_separate_grants_and_auth_epoch_are_verified_in_one_transaction(self):
        self.assertTrue(self.authority.authorize_read(self.scope))
        self.assertTrue(self.client.get_item.call_args.kwargs['ConsistentRead'])
        items = self.client.transact_get_items.call_args.kwargs['TransactItems']
        self.assertEqual([item['Get']['TableName'] for item in items],
            ['foodtech-auth-sessions-dev', 'foodtech-source-access-dev', 'foodtech-source-access-dev'])
        self.assertNotIn('opaque_subject', repr(self.grants))
        self.assertNotIn('owner_subject', repr(self.grants))

    def test_snapshot_preserves_versions_for_later_atomic_reauthorization(self):
        self.grants[1]['version'] = 2
        self.responses()
        receipt = self.authority.authorization_snapshot(self.scope)
        self.assertEqual(receipt.grant_ids, tuple(self.refs))
        self.assertEqual(receipt.grant_versions, (1, 2))
        self.assertEqual(receipt.expires_at, (2000, 2000))
        self.assertEqual(receipt.auth_epoch, self.scope.auth_epoch)
        self.assertNotIn(self.scope.owner, repr(receipt))
        self.assertIsNone(self.authority.authorization_snapshot(replace(self.scope, source_partitions=([], []))))

    def test_uninitialized_suspended_locked_or_wrong_epoch_cannot_look_up_grants(self):
        original = copy.deepcopy(self.principal)
        variants = [{k: v for k, v in original.items() if k != 'epoch'}, {**original, 'epoch': 4},
            {**original, 'epoch': True}, {**original, 'suspended': True},
            {**original, 'deletion_lock_request': str(uuid4())},
            {**original, 'pk': 'USER#foreign'}]
        for principal in variants:
            with self.subTest(principal=principal):
                self.principal = principal
                self.responses()
                self.assertFalse(self.authority.authorize_read(self.scope))
        self.client.transact_get_items.assert_not_called()

    def test_no_reference_or_guessed_batch_binding_never_grants_read(self):
        for mapping in [{}, {'JEYUK#jeyuk-foreign': self.refs[0]},
                {self.scope.source_partitions[0]: self.refs[0], self.scope.source_partitions[1]: self.refs[0]},
                {source: 'not-a-grant' for source in self.scope.source_partitions}]:
            self.principal['source_read_grants'] = mapping
            self.responses()
            self.assertFalse(self.authority.authorize_read(self.scope))
        self.client.transact_get_items.assert_not_called()

    def test_auth_revocation_or_reference_change_during_lookup_is_denied(self):
        for current in [{**self.principal, 'epoch': 4}, {**self.principal, 'suspended': True},
                {**self.principal, 'deletion_lock_request': 'request'},
                {**self.principal, 'source_read_grants': {source: str(uuid4()) for source in self.scope.source_partitions}}]:
            self.responses(current=current)
            self.assertFalse(self.authority.authorize_read(self.scope))

    def test_missing_revoked_wrong_source_expired_or_unexpected_grant_is_denied(self):
        for changes in [{'state': 'REVOKED'}, {'source_partition': 'JEYUK#jeyuk-foreign'},
                {'expires_at': 1000}, {'expires_at': True}, {'version': 0},
                {'schema': 2}, {'permission': 'SOURCE_DELETE'}, {'owner_subject': 'raw-subject'}]:
            grants = copy.deepcopy(self.grants)
            grants[1].update(changes)
            self.responses(grants=grants)
            self.assertFalse(self.authority.authorize_read(self.scope))
        self.responses()
        self.client.transact_get_items.return_value['Responses'][1] = {}
        self.assertFalse(self.authority.authorize_read(self.scope))

    def test_expiry_during_verification_bad_clock_and_missing_scope_epoch_are_denied(self):
        times = iter([1000, 2000])
        self.authority.clock = lambda: next(times)
        self.assertFalse(self.authority.authorize_read(self.scope))
        for now in [float('nan'), float('inf'), True, -1]:
            self.authority.clock = lambda now=now: now
            self.assertFalse(self.authority.authorize_read(self.scope))
        self.client.reset_mock()
        self.assertFalse(self.authority.authorize_read(replace(self.scope, auth_epoch=None)))
        self.client.get_item.assert_not_called()

    def test_separate_same_stage_tables_are_required(self):
        with self.assertRaises(ValueError):
            DynamoSourceReadAuthority(auth_table='foodtech-auth-sessions-dev',
                catalog_table='foodtech-source-access-demo', client=self.client)
