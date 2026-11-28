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
        with Stubber(self.client) as stub, patch('foodtech_contracts.dynamo_prior_history.MAX_JOBS', 2):
            self.control(stub); self.listing(stub)
            with self.assertRaises(PriorHistoryUnavailable):
                self.authority.observe(**self.scope)
            stub.assert_no_pending_responses()



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

    def test_fryer_read_scope_joins_execution_history_reverse_bindings_and_source_versions(self):
        from foodtech_business_api.recipe_scope import RecipeScopeDenied
        from foodtech_business_api.tonkatsu_read_scope import DynamoTonkatsuReadScope
        from foodtech_contracts.dynamo_tonkatsu_scope import DynamoTonkatsuReadScope as GatewayResolver
        from foodtech_contracts.dynamo_tonkatsu_scope import TonkatsuScopeUnavailable
        from foodtech_contracts.tonkatsu_execution_binding import BINDING_SCHEMA

        streams = self.fryer_fixture()
        execution_id = str(uuid4())
        resolver = DynamoTonkatsuReadScope(execution_table="foodtech-ai-execution-registry-dev",
                                           data_table="foodtech-data-flow-dev", history=self.authority)
        gateway = GatewayResolver(execution_table=resolver.execution_table, data_table=resolver.data_table,
                                  history=self.authority)
        row = {
            "pk": f"EXEC#{execution_id}", "sk": "META", "execution_id": execution_id,
            "tenant_id": self.owner, "feature": "RECIPE_CORRECTION", "data_classification": "SYNTHETIC",
            "state": "PENDING", "version": 2,
            "job_id": self.current, "job_version": 1, "prior_job_id": self.prior, "prior_job_version": 1,
            "tonkatsu_binding": {
                "schema_version": BINDING_SCHEMA, "auth_epoch": 2,
                "current": {"job_id": self.current, "job_version": 1, "source_revision": 8},
                "prior": {"job_id": self.prior, "job_version": 1, "source_revision": 7},
            },
        }

        def links(stub, *, foreign=False, stale=False):
            items = []
            for stream, job_id, revision in zip(streams, (self.current, self.prior), (8, 7)):
                owner = "foreign" if foreign else self.owner
                items.append({"Item": self.encode({"pk": stream, "sk": "BINDING",
                    "job_id": job_id, "job_pk": f"SCOPE#USER#{owner}#JOB#{job_id}"})})
                items.append({"Item": self.encode({"pk": stream, "sk": "SOURCE#REVISION",
                                                   "revision": revision + int(stale)})})
            stub.add_response("transact_get_items", {"Responses": items}, expected_params={"TransactItems": [
                {"Get": {"TableName": table, "Key": self.encode({"pk": stream, "sk": sort}),
                         "ProjectionExpression": ANY, **extra}}
                for stream in streams for table, sort, extra in (
                    (self.authority.cooking, "BINDING", {}),
                    (resolver.data_table, "SOURCE#REVISION", {"ExpressionAttributeNames": {"#revision": "revision"}}),
                )
            ]})

        def execution(stub, payload):
            stub.add_response("get_item", {"Item": self.encode(payload)}, expected_params={
                "TableName": resolver.execution_table, "Key": self.encode({"pk": row["pk"], "sk": "META"}),
                "ProjectionExpression": ANY, "ExpressionAttributeNames": ANY, "ConsistentRead": True,
            })

        for scenario in ("stable", "foreign-owner", "foreign-link", "source-race", "execution-race"):
            with self.subTest(scenario=scenario), Stubber(self.client) as stub:
                execution(stub, {**row, "tenant_id": "foreign"} if scenario == "foreign-owner" else row)
                if scenario != "foreign-owner":
                    self.observation(stub)
                    links(stub, foreign=scenario == "foreign-link")
                    if scenario != "foreign-link":
                        self.observation(stub)
                        links(stub, stale=scenario == "source-race")
                        if scenario != "source-race":
                            execution(stub, {**row, "version": 3} if scenario == "execution-race" else row)
                request = {"owner": self.owner, "epoch": 2, "execution_id": execution_id}
                if scenario == "stable":
                    scope = resolver.resolve(**request)
                    self.assertEqual(scope.history.streams, streams)
                    self.assertEqual(scope.execution_version, 2)
                else:
                    with self.assertRaises(RecipeScopeDenied):
                        resolver.resolve(**request)
                stub.assert_no_pending_responses()

        with Stubber(self.client) as stub:
            execution(stub, row)
            self.observation(stub)
            links(stub)
            self.observation(stub)
            links(stub)
            execution(stub, row)
            from_registry = gateway.resolve_execution(execution_id)
            self.assertEqual(from_registry, scope)
            stub.assert_no_pending_responses()

        with Stubber(self.client) as stub:
            self.observation(stub)
            links(stub)
            self.observation(stub)
            pending = gateway.plan_execution(owner=self.owner, epoch=2, execution_id=execution_id,
                current_job_id=self.current, prior_job_id=self.prior, job_versions=(1, 1))
            self.assertEqual(pending.binding, from_registry.binding)
            self.assertEqual(pending.execution_version, 1)
            stub.assert_no_pending_responses()

        with Stubber(self.client) as stub:
            execution(stub, {**row, "state": "RESERVED"})
            with self.assertRaises(TonkatsuScopeUnavailable):
                gateway.resolve_execution(execution_id)
            stub.assert_no_pending_responses()

    def test_fryer_broker_limits_table_keys_and_withholds_lease_after_scope_change(self):
        from datetime import datetime, timezone, timedelta
        from unittest.mock import Mock
        from dataclasses import dataclass, replace

        from foodtech_business_api.tonkatsu_read_broker import TonkatsuReadBroker
        from foodtech_business_api.tonkatsu_read_scope import DynamoTonkatsuReadScope, TonkatsuReadScope
        from foodtech_business_api.recipe_scope import RecipeScopeDenied
        from foodtech_contracts.tonkatsu_execution_binding import TonkatsuExecutionBinding, TonkatsuJobReference

        streams = self.fryer_fixture()
        with Stubber(self.client) as stub:
            self.observation(stub)
            witness = self.authority.observe_tonkatsu(**self.scope)
        binding = TonkatsuExecutionBinding(TonkatsuJobReference(self.current, 1, 8),
                                           TonkatsuJobReference(self.prior, 1, 7), 2)
        scope = TonkatsuReadScope(str(uuid4()), 1, binding, witness)
        resolver = DynamoTonkatsuReadScope(execution_table="foodtech-ai-execution-registry-dev",
                                           data_table="foodtech-data-flow-dev", history=self.authority)
        now = datetime.fromtimestamp(1000, timezone.utc)
        session_factory = Mock()
        session = session_factory.return_value
        session.get_credentials.return_value.method = "explicit"
        sts = Mock()
        role_name = "foodtech-tonkatsu-source-read-dev"

        def assume(**request):
            return {"AssumedRoleUser": {"Arn": "arn:aws:sts::123456789012:assumed-role/" +
                                                role_name + "/" + request["RoleSessionName"]},
                    "Credentials": {"AccessKeyId": "synthetic", "SecretAccessKey": "synthetic",
                                    "SessionToken": "synthetic", "Expiration": now + timedelta(seconds=900)}}

        sts.assume_role.side_effect = assume
        broker = TonkatsuReadBroker(account="123456789012", stage="dev",
            read_role_arn="arn:aws:iam::123456789012:role/" + role_name, sts=sts,
            resolver=resolver, session_factory=session_factory, clock=lambda: now)
        args = {"owner": self.owner, "epoch": 2, "execution_id": scope.execution_id}
        with patch.object(resolver, "resolve", side_effect=[scope, replace(scope, execution_version=2)]):
            with self.assertRaises(RecipeScopeDenied):
                broker.issue(**args)
        session_factory.assert_not_called()
        with patch.object(resolver, "resolve", return_value=scope):
            assume_count = sts.assume_role.call_count
            with self.assertRaises(RecipeScopeDenied):
                broker.issue(**args, deadline=now + timedelta(seconds=29))
            self.assertEqual(sts.assume_role.call_count, assume_count)
            verified, lease = broker.issue(**args, deadline=now + timedelta(seconds=60))
            self.assertEqual(verified, scope)
            self.assertEqual(lease.expires_at.timestamp(), 1060)
            lease.resource(verified)
            frozen = session.get_credentials.return_value.get_frozen_credentials.return_value
            frozen.access_key, frozen.secret_key, frozen.token = ("synthetic-key", "synthetic-secret", "synthetic-token")
            frame = lease._internal_handoff(verified)
            wire = frame._encode_for_internal_tls()
        from foodtech_ai_gateway.tonkatsu_read_handoff import TonkatsuHandoffDenied, _accept

        role_id = "AROA" + "A" * 17
        consumer = Mock()
        consumer_session = consumer.return_value
        consumer_session.get_credentials.return_value.method = "explicit"
        consumer_sts = consumer_session.client.return_value
        consumer_sts.meta.endpoint_url = "https://sts.ap-southeast-2.amazonaws.com"
        consumer_session.resource.return_value.meta.client.meta.endpoint_url = "https://dynamodb.ap-southeast-2.amazonaws.com"
        identity = {"Account": "123456789012", "Arn": frame.assumed_role,
                    "UserId": role_id + ":" + frame.assumed_role.rsplit("/", 1)[1]}
        consumer_sts.get_caller_identity.return_value = identity
        args_accept = dict(expected_role=broker.role, expected_role_id=role_id,
                           session_factory=consumer, clock=lambda: now)
        with self.assertRaises(TonkatsuHandoffDenied):
            _accept(wire, resolve_scope=lambda: None, **args_accept)
        with self.assertRaises(TonkatsuHandoffDenied):
            _accept(wire, resolve_scope=lambda: replace(scope, execution_version=2), **args_accept)
        consumer.assert_not_called()
        consumer_sts.get_caller_identity.return_value = {**identity, "UserId": "wrong"}
        with self.assertRaises(TonkatsuHandoffDenied):
            _accept(wire, resolve_scope=lambda: scope, **args_accept)
        consumer_session.resource.assert_not_called()
        consumer_sts.get_caller_identity.return_value = identity
        reads = _accept(wire, resolve_scope=lambda: scope, **args_accept)
        resource = consumer_session.resource.return_value
        cooking_table = resource.Table.return_value
        cooking_table.get_item.side_effect = [
            response
            for pk, job in zip(scope.job_partitions, (self.current, self.prior))
            for response in (
                {"Item": {"pk": pk, "sk": "META", "owner_subject": self.owner, "job_id": job, "version": 1,
                          "safety_status": "FAILED", "evaluation_blocked": True}},
                {},
            )
        ]

        @dataclass(frozen=True)
        class TestSourceObservation:
            name: str
            recorded_safety_status: str = "FAILED"
            quality_summary: object = None

        with patch("foodtech_ai_gateway.tonkatsu_read_handoff.observe_scoped_tonkatsu_source",
                   side_effect=[TestSourceObservation("current-input"), TestSourceObservation("prior-input")]) as observe:
            observations = reads.observe_inputs()
            self.assertEqual(tuple(row.name for row in observations), ("current-input", "prior-input"))
            self.assertTrue(all(row.quality_summary.status == "BLOCKED_BY_SAFETY" for row in observations))
            self.assertEqual([call.kwargs["scoped_stream"] for call in observe.call_args_list], list(streams))
            self.assertEqual([call.kwargs["expected_revision"] for call in observe.call_args_list], [8, 7])
        from botocore.validate import validate_parameters

        update = {"Update": {"TableName": resolver.execution_table,
            "Key": self.encode({"pk": "EXEC#" + scope.execution_id, "sk": "META"}),
            "UpdateExpression": "SET #state = :next", "ConditionExpression": "#state = :before",
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": self.encode({":before": "PENDING", ":next": "RESERVED"})}}
        pending, checks = reads.guard_transaction(update, execution_id=scope.execution_id, completed_calls=0)
        reserved, same_checks = reads.guard_transaction(update, execution_id=scope.execution_id, completed_calls=2)
        self.assertEqual(update["Update"]["ConditionExpression"], "#state = :before")
        self.assertTrue(pending["Update"]["ConditionExpression"].startswith("(#state = :before) AND ("))
        self.assertEqual(checks, same_checks)
        self.assertNotEqual(pending, reserved)
        shape = self.client.meta.service_model.operation_model("TransactWriteItems").input_shape
        validate_parameters({"TransactItems": [pending, *checks]}, shape)
        actual_keys = [(operation["ConditionCheck"]["TableName"],
                        operation["ConditionCheck"]["Key"]["pk"]["S"],
                        operation["ConditionCheck"]["Key"].get("sk", {}).get("S"))
                       for operation in checks]
        self.assertEqual(len(actual_keys), len(set(actual_keys)))
        self.assertEqual(actual_keys[:3], [
            (self.authority.auth, "USER#" + self.owner, None),
            (self.authority.auth, "CONTROL#COOKING_HISTORY_WRITERS", None),
            (self.authority.cooking, f"SCOPE#USER#{self.owner}#JOBS", "HISTORY#REVISION"),
        ])
        for stream, ref in zip(streams, (binding.current, binding.prior), strict=True):
            key = (resolver.data_table, stream, "SOURCE#REVISION")
            revision = checks[actual_keys.index(key)]["ConditionCheck"]
            self.assertEqual(revision["ExpressionAttributeValues"], {":fryer0": {"N": str(ref.source_revision)}})
        for operation in checks:
            self.assertEqual(operation["ConditionCheck"]["ReturnValuesOnConditionCheckFailure"], "NONE")
        with self.assertRaises(ValueError):
            reads.guard_transaction(update, execution_id=str(uuid4()), completed_calls=0)
        reads.close()
        with self.assertRaises(ValueError):
            reads.guard_transaction(update, execution_id=scope.execution_id, completed_calls=0)
        consumer_session.resource.return_value.meta.client.close.assert_called_once()
        self.assertNotIn(self.owner.encode(), wire)
        self.assertNotIn(b"synthetic-secret", repr(frame).encode())
        request = sts.assume_role.call_args.kwargs
        self.assertEqual(request["DurationSeconds"], 900)
        policy = json.loads(request["Policy"])
        cooking, data = policy["Statement"]
        for statement, expected in ((cooking, (*scope.job_partitions, *streams)), (data, streams)):
            self.assertEqual(statement["Action"], ["dynamodb:GetItem", "dynamodb:Query"])
            self.assertEqual(statement["Condition"]["ForAllValues:StringEquals"]["dynamodb:LeadingKeys"], list(expected))
            self.assertEqual(statement["Condition"]["Null"], {"dynamodb:LeadingKeys": "false"})
        self.assertTrue(cooking["Resource"].endswith(":table/foodtech-cooking-jobs-dev"))
        self.assertTrue(data["Resource"].endswith(":table/foodtech-data-flow-dev"))
        now += timedelta(seconds=100)
        with self.assertRaises(RecipeScopeDenied):
            lease.resource(scope)
        lease.close()
        session.resource.return_value.meta.client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
