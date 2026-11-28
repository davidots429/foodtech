import copy
import hashlib
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from foodtech_business_api.prior_history_authority import CONTRACT, DynamoPriorHistoryAuthority, PriorHistoryUnavailable


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.owner, self.current, self.prior, self.older = 'synthetic-owner', str(uuid4()), str(uuid4()), str(uuid4())
        self.client = boto3.client('dynamodb', region_name='ap-southeast-2',
            aws_access_key_id='synthetic', aws_secret_access_key='synthetic')
        self.authority = DynamoPriorHistoryAuthority(auth_table='foodtech-auth-sessions-dev',
            cooking_table='foodtech-cooking-jobs-dev', account_id='123456789012', client=self.client, clock=lambda: 1000)
        self.serializer = TypeSerializer()
        self.jobs = {}
        for job_id, day, status in [(self.older, 1, 'COMPLETED'), (self.prior, 2, 'FAILED'), (self.current, 3, 'RUNNING')]:
            self.jobs[job_id] = {'pk': f'SCOPE#USER#{self.owner}#JOB#{job_id}', 'sk': 'META',
                'job_id': job_id, 'owner_subject': self.owner, 'version': 1, 'menu': 'JEYUK', 'provenance': 'SYNTHETIC',
                'equipment_profile': 'jeyuk-small', 'status': status, 'created_at': f'2026-01-0{day}T00:00:00Z',
                'actual_placed_at': f'2026-01-0{day}T00:00:00Z', 'actual_lifted_at': f'2026-01-0{day}T00:05:00Z',
                'source_binding': {'source_id': 'synthetic-rest', 'device_id': 'mockprofile-110-a-001', 'batch_id': 'jeyuk-' + job_id}}
        self.scope = dict(owner=self.owner, auth_epoch=2, current_job_id=self.current, prior_job_id=self.prior, job_versions=(1, 1))
        digest = self.roster_hash()
        generation = str(uuid4())
        self.controls = [
            {'pk': 'USER#' + self.owner, 'epoch': 2, 'suspended': False},
            {'pk': 'CONTROL#COOKING_HISTORY_WRITERS', 'state': 'ENFORCED', 'version': 3, 'generation': generation,
             'writer_contract': CONTRACT, 'cooking_table_arn': self.authority.cooking_arn,
             'manifest_sha256': 'a' * 64, 'verified_at': 900, 'valid_until': 1100},
            {'state': 'ACTIVE', 'version': 2,
             'generation': generation, 'writer_contract': CONTRACT, 'coverage_scope': 'ALL_RETAINED_JOBS',
             'coverage_revision': 4, 'baseline_roster_sha256': digest, 'proof_sha256': 'b' * 64,
             'writer_manifest_sha256': 'a' * 64},
            {'pk': f'SCOPE#USER#{self.owner}#JOBS', 'sk': 'HISTORY#REVISION', 'revision': 4},
        ]
        self.controls[0]['history_coverage'] = self.controls.pop(2)

    def encode(self, row):
        return {k: self.serializer.serialize(v) for k, v in row.items()}

    def roster_hash(self):
        return hashlib.sha256(json.dumps(sorted((key, row['created_at']) for key, row in self.jobs.items()),
                                         separators=(',', ':')).encode('ascii')).hexdigest()

    def control(self, stub, rows=None):
        stub.add_response('transact_get_items', {'Responses': [{'Item': self.encode(row)} for row in (rows or self.controls)]})

    def listing(self, stub):
        rows = [{'pk': f'SCOPE#USER#{self.owner}#JOBS', 'sk': 'CREATED#' + row['created_at'] + '#' + job_id,
                 'job_id': job_id, 'created_at': row['created_at']} for job_id, row in self.jobs.items()]
        stub.add_response('query', {'Items': [self.encode(row) for row in rows]})

    def metadata(self, stub):
        stub.add_response('transact_get_items', {'Responses': [{'Item': self.encode(self.jobs[key])} for key in sorted(self.jobs)]})

    def observation(self, stub, after=None):
        self.control(stub)
        self.listing(stub)
        self.metadata(stub)
        self.control(stub, after)

    def test_protected_fixture_and_latest_failed_job_produce_internal_witness(self):
        with Stubber(self.client) as stub:
            self.observation(stub)
            witness = self.authority.observe(**self.scope)
            stub.assert_no_pending_responses()
        self.assertEqual(witness.prior_job_id, self.prior)
        self.assertEqual(witness.history_revision, 4)
        self.assertEqual(witness.roster_sha256, self.roster_hash())
        self.assertNotIn(self.owner, repr(witness))

    def test_missing_unfenced_expired_wrong_generation_or_partial_coverage_never_proves_history(self):
        for index, changed in [(0, {'suspended': True}), (0, {'epoch': 3}), (1, {'state': 'UNVERIFIED'}),
                               (1, {'valid_until': 1000}), ('coverage', {'generation': str(uuid4())}),
                               ('coverage', {'coverage_scope': 'SINCE_ACTIVATION'}), ('coverage', {'writer_manifest_sha256': 'c' * 64})]:
            with self.subTest(changed=changed), Stubber(self.client) as stub:
                rows = copy.deepcopy(self.controls)
                (rows[0]['history_coverage'] if index == 'coverage' else rows[index]).update(changed)
                self.control(stub, rows)
                with self.assertRaises(PriorHistoryUnavailable):
                    self.authority.observe(**self.scope)
                stub.assert_no_pending_responses()
        with Stubber(self.client) as stub:
            stub.add_response('transact_get_items', {'Responses': [{}, {}, {}]})
            with self.assertRaises(PriorHistoryUnavailable):
                self.authority.observe(**self.scope)

    def test_old_nonlatest_job_cannot_be_accepted_as_prior(self):
        with Stubber(self.client) as stub:
            self.control(stub); self.listing(stub); self.metadata(stub)
            with self.assertRaises(PriorHistoryUnavailable):
                self.authority.observe(**{**self.scope, 'prior_job_id': self.older})
            stub.assert_no_pending_responses()

    def test_missing_end_or_ambiguous_order_blocks_witness(self):
        for changed in [{'actual_lifted_at': None}, {'actual_placed_at': self.jobs[self.current]['actual_placed_at']}]:
            with self.subTest(changed=changed), Stubber(self.client) as stub:
                original = copy.deepcopy(self.jobs[self.prior]); self.jobs[self.prior].update(changed)
                self.control(stub); self.listing(stub); self.metadata(stub)
                with self.assertRaises(PriorHistoryUnavailable):
                    self.authority.observe(**self.scope)
                self.jobs[self.prior] = original

    def test_omitted_legacy_listing_is_not_accepted_at_certified_baseline(self):
        del self.jobs[self.older]
        with Stubber(self.client) as stub:
            self.control(stub); self.listing(stub); self.metadata(stub)
            with self.assertRaisesRegex(PriorHistoryUnavailable, 'baseline'):
                self.authority.observe(**self.scope)
            stub.assert_no_pending_responses()

    def test_revision_or_protected_control_change_during_read_blocks_witness(self):
        for index, changed in [(2, {'revision': 5}), (1, {'version': 4}), ('coverage', {'version': 3})]:
            with self.subTest(changed=changed), Stubber(self.client) as stub:
                after = copy.deepcopy(self.controls)
                (after[0]['history_coverage'] if index == 'coverage' else after[index]).update(changed)
                self.observation(stub, after)
                with self.assertRaises(PriorHistoryUnavailable):
                    self.authority.observe(**self.scope)
                stub.assert_no_pending_responses()

    def test_revalidation_reobserves_and_compares_exact_witness(self):
        with Stubber(self.client) as stub:
            self.observation(stub); self.observation(stub)
            witness = self.authority.observe(**self.scope)
            self.assertEqual(self.authority.revalidate(witness), witness)
            stub.assert_no_pending_responses()


    def test_pagination_reads_all_listings_before_selecting_prior(self):
        rows = [{'pk': f'SCOPE#USER#{self.owner}#JOBS', 'sk': 'CREATED#' + row['created_at'] + '#' + job_id,
                 'job_id': job_id, 'created_at': row['created_at']} for job_id, row in self.jobs.items()]
        cursor = self.encode({key: rows[1][key] for key in ['pk', 'sk']})
        with Stubber(self.client) as stub:
            self.control(stub)
            stub.add_response('query', {'Items': [self.encode(row) for row in rows[:2]], 'LastEvaluatedKey': cursor})
            stub.add_response('query', {'Items': [self.encode(rows[2])]}, expected_params={
                'TableName': self.authority.cooking, 'KeyConditionExpression': ANY,
                'ExpressionAttributeValues': ANY, 'ProjectionExpression': ANY, 'ConsistentRead': True,
                'Limit': 50, 'ExclusiveStartKey': cursor})
            self.metadata(stub); self.control(stub)
            self.assertEqual(self.authority.observe(**self.scope).prior_job_id, self.prior)
            stub.assert_no_pending_responses()

    def test_foreign_metadata_owner_is_rejected(self):
        self.jobs[self.prior]['owner_subject'] = 'another-owner'
        with Stubber(self.client) as stub:
            self.control(stub); self.listing(stub); self.metadata(stub)
            with self.assertRaises(PriorHistoryUnavailable):
                self.authority.observe(**self.scope)
            stub.assert_no_pending_responses()

    def test_query_bound_is_a_rejection_not_a_truncated_history_proof(self):
        with Stubber(self.client) as stub, patch('foodtech_business_api.prior_history_authority.MAX_JOBS', 2):
            self.control(stub); self.listing(stub)
            with self.assertRaises(PriorHistoryUnavailable):
                self.authority.observe(**self.scope)
            stub.assert_no_pending_responses()


if __name__ == '__main__':
    unittest.main()

    def fryer_fixture(self):
        from foodtech_contracts.tonkatsu_prior_history import fryer_stream

        for job_id, row in self.jobs.items():
            row.update(menu="TONKATSU", equipment_profile="synthetic-fryer-approved-v2")
            row["source_binding"].update(device_id="mockprofile-100-a-001", batch_id=f"batch-a-{job_id}")
        self.jobs[self.current]["status"] = "COMPLETED"
        return tuple(fryer_stream(self.jobs[job_id]["source_binding"]) for job_id in (self.current, self.prior))

    def test_fryer_witness_derives_separate_streams_and_requires_typed_revalidation(self):
        streams = self.fryer_fixture()
        with Stubber(self.client) as stub:
            self.observation(stub)
            self.observation(stub)
            witness = self.authority.observe_tonkatsu(**self.scope)
            self.assertEqual(witness.streams, streams)
            self.assertEqual(self.authority.revalidate_tonkatsu(witness), witness)
            with self.assertRaises(ValueError):
                self.authority.revalidate(witness)
            for stream in streams:
                self.assertNotIn(stream, repr(witness))
            stub.assert_no_pending_responses()

    def test_fryer_rejects_skipped_job_overlapping_reheat_and_reused_source(self):
        self.fryer_fixture()
        for scenario in ("skipped", "reheat", "reused", "mixed-menu"):
            with self.subTest(scenario=scenario), Stubber(self.client) as stub:
                saved = copy.deepcopy(self.jobs)
                scope = dict(self.scope)
                if scenario == "skipped":
                    scope["prior_job_id"] = self.older
                elif scenario == "reheat":
                    self.jobs[self.older]["latest_tonkatsu_reheat"] = {
                        "started_at": "2026-01-01T23:59:00Z", "lifted_at": "2026-01-02T00:01:00Z",
                    }
                elif scenario == "reused":
                    self.jobs[self.prior]["source_binding"] = self.jobs[self.current]["source_binding"]
                else:
                    self.jobs[self.prior]["menu"] = "JEYUK"
                self.control(stub)
                self.listing(stub)
                self.metadata(stub)
                with self.assertRaises(PriorHistoryUnavailable):
                    self.authority.observe_tonkatsu(**scope)
                stub.assert_no_pending_responses()
                self.jobs = saved

    def test_fryer_revalidation_rejects_mutated_stream_or_history_revision(self):
        self.fryer_fixture()
        with Stubber(self.client) as stub:
            self.observation(stub)
            witness = self.authority.observe_tonkatsu(**self.scope)
            self.jobs[self.prior]["source_binding"]["batch_id"] = "batch-a-changed"
            self.observation(stub)
            with self.assertRaisesRegex(PriorHistoryUnavailable, "witness changed"):
                self.authority.revalidate_tonkatsu(witness)
            after = copy.deepcopy(self.controls)
            after[2]["revision"] += 1
            self.observation(stub, after)
            with self.assertRaisesRegex(PriorHistoryUnavailable, "coverage changed"):
                self.authority.observe_tonkatsu(**self.scope)
            stub.assert_no_pending_responses()
