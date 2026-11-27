"""Private issuer/envelope round-trip using stored metadata fixtures only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import pickle
from types import SimpleNamespace
import unittest

from foodtech_contracts.combi_read_handoff import CombiReadHandoff, MAX_BYTES
from foodtech_business_api.recipe_scope import RecipeScopeDenied
import test_combi_read_broker as fixtures


class CombiReadHandoffTests(unittest.TestCase):
    def setUp(self):
        self.data = fixtures.CombiReadBrokerTests(); self.data.setUp()
        def response(**args):
            value = self.data.response(**args)
            value['Credentials']['AccessKeyId'] = 'SYNTHETICKEY1234567890'
            return value
        self.data.sts.assume_role.side_effect = response
        self.frozen = SimpleNamespace(access_key='SYNTHETICKEY1234567890',
            secret_key='synthetic-not-a-real-secret', token='synthetic-not-a-real-token')
        self.data.sdk_session.get_credentials.return_value = SimpleNamespace(method='explicit', get_frozen_credentials=lambda: self.frozen)
        self.lease = self.data.issue()

    def handoff(self, **changes):
        args = dict(execution_reader=self.data.data.reader, history_authority=self.data.data.data.authority)
        args.update(changes)
        return self.lease._internal_handoff(self.data.scope, **args)

    def decode(self, raw):
        return CombiReadHandoff._decode_from_internal_tls(raw, clock=lambda: self.data.now)

    def test_private_round_trip_preserves_exact_stored_scope_and_explicit_credentials(self):
        handoff = self.handoff()
        decoded = self.decode(handoff._encode_for_internal_tls())
        self.assertEqual(decoded.scope, self.data.scope)
        self.assertEqual(decoded.read_role_arn, self.data.broker.role)
        self.assertEqual(decoded.assumed_role_arn, self.data.sts.assume_role.call_args.kwargs['RoleArn'].replace(
            'arn:aws:iam::', 'arn:aws:sts::').replace(':role/', ':assumed-role/')+'/'+self.data.sts.assume_role.call_args.kwargs['RoleSessionName'])
        self.assertEqual(decoded._sdk_credentials(), {'aws_access_key_id': self.frozen.access_key,
            'aws_secret_access_key': self.frozen.secret_key, 'aws_session_token': self.frozen.token})

    def test_ordinary_repr_json_pickle_and_deepcopy_never_serialize_the_frame(self):
        handoff = self.handoff()
        self.assertEqual(repr(handoff), 'CombiReadHandoff()')
        for serialize in (json.dumps, pickle.dumps, deepcopy, vars):
            with self.subTest(serialize=serialize), self.assertRaises(TypeError): serialize(handoff)

    def test_duplicate_extra_legacy_or_nonfinite_fields_are_not_accepted(self):
        raw = self.handoff()._encode_for_internal_tls()
        duplicate = raw.replace(b'{"schema":', b'{"schema":"legacy","schema":', 1)
        for bad in (duplicate, raw[:-1]+b',"extra":true}', raw.replace(b'combi-source-read-handoff-v1', b'recipe-source-read-handoff-v2'),
                    raw.replace(b'"execution_version":1', b'"execution_version":NaN'), b'x'*(MAX_BYTES+1), b'\xff', b'[]'):
            with self.subTest(size=len(bad)), self.assertRaisesRegex(ValueError, '^combi read handoff unavailable$'):
                self.decode(bad)

    def test_nested_binding_history_owner_and_snapshot_versions_are_strict(self):
        original = json.loads(self.handoff()._encode_for_internal_tls())
        for field in ('owner', 'history_binding', 'float_version', 'bool_epoch', 'extra_key_array', 'missing_history'):
            value = deepcopy(original)
            if field == 'owner': value['scope']['owner'] = 'another_owner'
            elif field == 'history_binding': value['scope']['history']['binding']['auth_epoch'] = 3
            elif field == 'float_version': value['scope']['execution_version'] = 1.0
            elif field == 'bool_epoch': value['scope']['binding']['auth_epoch'] = True
            elif field == 'extra_key_array': value['scope']['read_partitions'] = ['foreign']
            else: value['scope'].pop('history')
            with self.subTest(field=field), self.assertRaises(ValueError): self.decode(json.dumps(value).encode())

    def test_mismatched_role_session_and_unsafe_credential_characters_fail(self):
        original = json.loads(self.handoff()._encode_for_internal_tls())
        for field in ('role', 'account', 'session_name', 'empty_key', 'secret_newline', 'extra_credentials'):
            value = deepcopy(original)
            if field == 'role': value['read_role_arn'] = value['read_role_arn'].replace('combi-source', 'recipe-source')
            elif field == 'account': value['assumed_role_arn'] = value['assumed_role_arn'].replace('123456789012', '000000000000')
            elif field == 'session_name': value['assumed_role_arn'] += '-other'
            elif field == 'empty_key': value['credentials']['access_key_id'] = ''
            elif field == 'secret_newline': value['credentials']['secret_access_key'] += '\n'
            else: value['credentials']['role'] = 'user-selected'
            with self.subTest(field=field), self.assertRaises(ValueError): self.decode(json.dumps(value).encode())

    def test_changed_history_during_credential_freeze_is_not_encoded(self):
        def freeze():
            self.data.data.data.history['revision'] = 13
            return self.frozen
        self.data.sdk_session.get_credentials.return_value.get_frozen_credentials = freeze
        with self.assertRaises(RecipeScopeDenied): self.handoff()

    def test_expiry_is_rechecked_and_does_not_follow_clock_extension(self):
        handoff = self.handoff(); raw = handoff._encode_for_internal_tls()
        self.data.now += timedelta(seconds=871)
        with self.assertRaises(ValueError): handoff._encode_for_internal_tls()
        with self.assertRaises(ValueError): handoff._sdk_credentials()
        with self.assertRaises(ValueError): self.decode(raw)

    def test_changed_execution_wrong_authority_or_closed_lease_cannot_export(self):
        self.data.data.snapshot = replace(self.data.data.snapshot, version=2)
        with self.assertRaises(RecipeScopeDenied): self.handoff()
        self.data.data.snapshot = replace(self.data.data.snapshot, version=1)
        with self.assertRaises(RecipeScopeDenied): self.handoff(history_authority=object())
        self.lease.close()
        with self.assertRaises(RecipeScopeDenied): self.handoff()

    def test_backward_clock_cannot_extend_an_existing_handoff(self):
        handoff = self.handoff()
        self.data.now += timedelta(seconds=1)
        handoff._validate()
        self.data.now -= timedelta(seconds=1)
        with self.assertRaises(ValueError): handoff._encode_for_internal_tls()
