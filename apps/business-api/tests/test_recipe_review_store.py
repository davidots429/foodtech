import copy
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from foodtech_business_api.cooking_store import Conflict, DynamoCookingStore, MemoryCookingStore
from foodtech_business_api.recipe_review_store import CURRENT, PREFIX, RecipeReviewStore, validate_review
from foodtech_contracts.recipe_result_view import recipe_proposal_digest


JOB = '00000000-0000-4000-8000-000000000001'
EXECUTION = '00000000-0000-4000-8000-000000000002'
PK = f'SCOPE#USER#customer#JOB#{JOB}'
PROPOSAL = {'status': 'NEEDS_REVIEW', 'schema_version': 'synthetic-recommendation-output-v1',
    'guard_version': 'synthetic-recommendation-guard-v1', 'menu': 'JEYUK',
    'data': {'temperature_c': '200', 'duration_seconds': 300, 'rpm': '60', 'load_kg': '7',
             'vegetable_interval_seconds': 60, 'sauce_interval_seconds': 120}}


def record(version=1):
    return {'schema_version': 'recipe-review-v1', 'version': version, 'request_id': str(uuid4()),
        'execution_id': EXECUTION, 'job_id': JOB, 'job_version': 3,
        'proposal_digest': recipe_proposal_digest(PROPOSAL), 'proposal': copy.deepcopy(PROPOSAL),
        'decision': 'APPROVE', 'reason': '합성 조리조건을 확인했습니다.', 'reviewed_at': '2026-11-27T01:00:00Z'}


class RecipeReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.cooking = MemoryCookingStore()
        self.meta = {'pk': PK, 'sk': 'META', 'owner_subject': 'customer', 'job_id': JOB, 'version': 3,
                     'status': 'COMPLETED', 'safety_status': 'PASSED', 'menu': 'JEYUK',
                     'recipe_version': 'baseline-v1', 'equipment_profile': 'jeyuk-medium', 'provenance': 'SYNTHETIC'}
        self.cooking.items[PK, 'META'] = copy.deepcopy(self.meta)
        self.reviews = RecipeReviewStore(self.cooking)

    def test_review_is_immutable_and_does_not_change_cooking_or_safety_state(self):
        value = record()
        saved = self.reviews.save(PK, 3, 0, value, 0)
        self.assertEqual(self.cooking.items[PK, 'META'], self.meta)
        self.assertEqual(self.reviews.current(PK), {'version': 1, 'review': value})
        saved['proposal']['data']['temperature_c'] = '999'
        self.assertEqual(self.reviews.current(PK)['review']['proposal']['data']['temperature_c'], '200')
        retry = {**value, 'reviewed_at': '2026-11-27T02:00:00Z'}
        self.assertEqual(self.reviews.save(PK, 3, 0, retry, 0), value)
        self.assertEqual(len(self.cooking.items), 4)
        changed = {**value, 'reason': '다른 사유'}
        with self.assertRaises(Conflict):
            self.reviews.save(PK, 3, 0, changed, 0)

    def test_concurrent_reviews_have_one_winner_and_new_version_preserves_history(self):
        def submit(value):
            try:
                self.reviews.save(PK, 3, 0, value, 0)
                return 'saved'
            except Conflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as executor:
            result = list(executor.map(submit, [record(), record()]))
        self.assertCountEqual(result, ['saved', 'conflict'])
        first = self.reviews.current(PK)['review']
        second = {**record(2), 'decision': 'REJECT'}
        self.reviews.save(PK, 3, 1, second, 0)
        self.assertEqual(self.reviews.current(PK)['review'], second)
        self.assertEqual(self.cooking.items[PK, PREFIX + f'{1:019d}']['request_id'], first['request_id'])

    def test_wrong_job_version_owner_or_digest_cannot_write(self):
        for kind in ('changed', 'owner', 'digest', 'job'):
            self.cooking.items[PK, 'META'] = copy.deepcopy(self.meta)
            value = record()
            if kind == 'changed': self.cooking.items[PK, 'META']['version'] = 4
            if kind == 'owner': self.cooking.items[PK, 'META']['owner_subject'] = 'foreign'
            if kind == 'digest': value['proposal_digest'] = '0' * 64
            if kind == 'job': value['job_id'] = str(uuid4())
            before = copy.deepcopy(self.cooking.items)
            with self.subTest(kind=kind), self.assertRaises((Conflict, ValueError)):
                self.reviews.save(PK, 3, 0, value, 0)
            self.assertEqual(self.cooking.items, before)

    def test_corrupt_pointer_or_extra_secret_fields_fail_closed(self):
        for changed in ({'decision': []}, {'reason': ' '}, {'version': True}, {'reviewed_at': '2026-11-27'},
                        {'credential': 'private-marker'}, {'proposal': {**PROPOSAL, 'credential': 'private-marker'}}):
            with self.subTest(changed=list(changed)), self.assertRaises(ValueError):
                validate_review({**record(), **changed})
        self.cooking.items[PK, CURRENT] = {'pk': PK, 'sk': CURRENT, 'version': 1}
        with self.assertRaises(ValueError):
            self.reviews.current(PK)

    def test_approval_creates_candidate_with_original_lineage_without_applying_it(self):
        approved = record()
        self.reviews.save(PK, 3, 0, approved, 0)
        candidate = self.reviews.candidate(PK, 1)
        self.assertEqual(candidate['status'], 'CANDIDATE_NOT_APPLIED')
        self.assertEqual(candidate['base_recipe_version'], 'baseline-v1')
        self.assertEqual(candidate['candidate_id'], 'recipe-candidate-' + JOB + '-r1')
        self.assertEqual(candidate['proposal'], approved['proposal'])
        self.assertEqual(self.cooking.items[PK, 'META'], self.meta)
        self.reviews.save(PK, 3, 1, {**record(2), 'decision': 'REJECT'}, 0)
        self.assertEqual(self.reviews.candidate(PK, 1), candidate)
        self.assertIsNone(self.reviews.candidate(PK, 2))
        self.cooking.items[PK, 'META'].update(version=4, recipe_version='new-baseline')
        self.assertEqual(self.reviews.candidate(PK, 1), candidate)

    def test_missing_or_invalid_lineage_never_saves_partial_approval(self):
        for changed in ({'recipe_version': None}, {'recipe_version': ' '}, {'provenance': 'REAL'},
                        {'menu': 'TONKATSU'}, {'equipment_profile': ''}):
            with self.subTest(changed=changed):
                self.cooking.items = {(PK, 'META'): {**self.meta, **changed}}
                before = copy.deepcopy(self.cooking.items)
                with self.assertRaises(ValueError): self.reviews.save(PK, 3, 0, record(), 0)
                self.assertEqual(self.cooking.items, before)

    def test_legacy_approval_is_readable_but_retry_does_not_invent_candidate(self):
        value = record()
        self.cooking.items[PK, PREFIX + f'{1:019d}'] = {'pk': PK, 'sk': PREFIX + f'{1:019d}', **value}
        self.cooking.items[PK, CURRENT] = {'pk': PK, 'sk': CURRENT, 'version': 1}
        self.assertEqual(self.reviews.current(PK)['review'], value)
        self.assertIsNone(self.reviews.candidate(PK, 1))
        before = copy.deepcopy(self.cooking.items)
        with self.assertRaises(Conflict): self.reviews.save(PK, 3, 0, value, 0)
        self.assertEqual(self.cooking.items, before)

    def test_corrupt_candidate_does_not_become_successful_retry_or_leak_fields(self):
        from foodtech_business_api.recipe_candidate import CANDIDATE_PREFIX
        value = record()
        self.reviews.save(PK, 3, 0, value, 0)
        key = (PK, CANDIDATE_PREFIX + f'{1:019d}')
        self.cooking.items[key]['credential'] = 'private-marker'
        with self.assertRaises(ValueError): self.reviews.candidate(PK, 1)
        with self.assertRaises(ValueError): self.reviews.save(PK, 3, 0, value, 0)

    def dynamo(self):
        cooking = object.__new__(DynamoCookingStore)
        cooking.table, cooking.client = MagicMock(), MagicMock()
        cooking.table.name = 'cooking'
        cooking.auth_table_name, cooking.require_principal = 'auth', True
        cooking.serializer = TypeSerializer()
        cooking.table.get_item.return_value = {'Item': copy.deepcopy(self.meta)}
        class Canceled(ClientError):
            pass
        cooking.client.exceptions.TransactionCanceledException = Canceled
        return cooking, Canceled

    def test_dynamo_transaction_joins_auth_job_and_independent_pointer_without_meta_write(self):
        cooking, _ = self.dynamo()
        value = record()
        self.assertEqual(RecipeReviewStore(cooking).save(PK, 3, 0, value, 5), value)
        actions = cooking.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(actions), 5)
        auth, job, pointer, saved, candidate = (next(iter(action.values())) for action in actions)
        self.assertEqual(candidate['Item']['status'], {'S': 'CANDIDATE_NOT_APPLIED'})
        import botocore.session
        from botocore.validate import validate_parameters
        model = botocore.session.get_session().get_service_model('dynamodb').operation_model('TransactWriteItems')
        validate_parameters({'TransactItems': actions}, model.input_shape)
        self.assertEqual(auth['Key'], {'pk': {'S': 'USER#customer'}})
        self.assertIn('attribute_exists(pk)', auth['ConditionExpression'])
        self.assertIn('deletion_lock_request', auth['ConditionExpression'])
        self.assertEqual(auth['ExpressionAttributeValues'][':expected'], {'N': '5'})
        self.assertEqual(job['Key']['sk'], {'S': 'META'})
        self.assertEqual(pointer['Key']['sk'], {'S': CURRENT})
        self.assertEqual(saved['Item']['sk'], {'S': PREFIX + f'{1:019d}'})
        self.assertTrue(all(next(iter(action.values()))['ReturnValuesOnConditionCheckFailure'] == 'NONE' for action in actions))

    def test_fryer_approval_atomically_rechecks_both_sources_and_terminal_execution(self):
        from botocore.session import get_session
        from botocore.validate import validate_parameters
        from foodtech_contracts.prior_history_witness import PriorHistoryWitness
        from foodtech_contracts.tonkatsu_execution_binding import TonkatsuExecutionBinding, TonkatsuJobReference
        from foodtech_contracts.tonkatsu_prior_history import TonkatsuPriorHistoryWitness, fryer_stream
        from foodtech_contracts.tonkatsu_read_scope import TonkatsuReadScope
        from foodtech_contracts.tonkatsu_result_view import fryer_review_scope_payload, parse_fryer_review_scope
        from foodtech_contracts.tonkatsu_transaction_conditions import TonkatsuTransactionConditions

        cooking, Canceled = self.dynamo()
        cooking.table.name = "foodtech-cooking-jobs-dev"
        cooking.auth_table_name = "foodtech-auth-sessions-dev"
        cooking.table.get_item.return_value = {"Item": {**self.meta, "menu": "TONKATSU",
            "equipment_profile": "synthetic-fryer-approved-v2"}}
        prior = str(uuid4())
        binding = TonkatsuExecutionBinding(TonkatsuJobReference(JOB, 3, 7), TonkatsuJobReference(prior, 2, 9), 5)
        history = PriorHistoryWitness("customer", JOB, prior, (3, 2), 5, 1, 1, 1, str(uuid4()),
            "a" * 64, "b" * 64, 1, "a" * 64, "c" * 64)
        streams = tuple(fryer_stream({"source_id": "synthetic-rest", "device_id": "mockprofile-100-a-001",
                                     "batch_id": f"batch-a-{name}"}) for name in ("current", "prior"))
        scope = TonkatsuReadScope(EXECUTION, 3, binding, TonkatsuPriorHistoryWitness(history, streams, 900, 1100))
        wire = fryer_review_scope_payload(scope, account="123456789012", stage="dev")
        self.assertEqual(parse_fryer_review_scope(wire), scope)
        conditions = TonkatsuTransactionConditions(scope=scope, account=wire["account"], stage=wire["stage"], clock=lambda: 1000)
        proposal = {**PROPOSAL, "menu": "TONKATSU", "data": {"temperature_c": "170", "duration_seconds": 300}}
        value = {**record(), "proposal": proposal, "proposal_digest": recipe_proposal_digest(proposal)}
        self.assertEqual(RecipeReviewStore(cooking).save(PK, 3, 0, value, 5, fryer_conditions=conditions), value)
        actions = cooking.client.transact_write_items.call_args.kwargs["TransactItems"]
        model = get_session().get_service_model("dynamodb").operation_model("TransactWriteItems")
        validate_parameters({"TransactItems": actions}, model.input_shape)
        checks = [action["ConditionCheck"] for action in actions if "ConditionCheck" in action]
        self.assertEqual((len(checks), len(actions)), (11, 14))
        self.assertEqual([x["Key"]["sk"]["S"] for x in checks
                          if x["TableName"] == "foodtech-ai-execution-registry-dev"], ["META", "RESULT"])
        self.assertEqual(len([x for x in checks if x["TableName"] == "foodtech-data-flow-dev"]), 2)
        self.assertEqual(sum(x["Key"]["pk"] == {"S": "USER#customer"} for x in checks), 1)
        self.assertEqual(actions[-1]["Put"]["Item"]["status"], {"S": "CANDIDATE_NOT_APPLIED"})

        reasons = [{"Code": "None"} for _ in actions]
        reasons[5] = {"Code": "ConditionalCheckFailed"}
        reasons[-2] = {"Code": "ConditionalCheckFailed"}
        cooking.client.transact_write_items.side_effect = Canceled({
            "Error": {"Code": "TransactionCanceledException"}, "CancellationReasons": reasons,
        }, "TransactWriteItems")
        cooking.table.get_item.reset_mock()
        with self.assertRaises(Conflict):
            RecipeReviewStore(cooking).save(PK, 3, 0, value, 5, fryer_conditions=conditions)
        cooking.table.get_item.assert_called_once_with(Key={"pk": PK, "sk": "META"}, ConsistentRead=True)

    def test_dynamo_auth_failure_never_becomes_successful_idempotent_retry(self):
        cooking, Canceled = self.dynamo()
        value = record()
        cooking.client.transact_write_items.side_effect = Canceled({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'ConditionalCheckFailed'}, {'Code': 'None'},
                                    {'Code': 'ConditionalCheckFailed'}, {'Code': 'ConditionalCheckFailed'}, {'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        with self.assertRaises(Conflict):
            RecipeReviewStore(cooking).save(PK, 3, 0, value, 0)
        cooking.table.get_item.assert_called_once_with(Key={'pk': PK, 'sk': 'META'}, ConsistentRead=True)

    def test_dynamo_duplicate_reconciles_exact_immutable_record_only(self):
        cooking, Canceled = self.dynamo()
        value = record()
        cooking.client.transact_write_items.side_effect = Canceled({'Error': {'Code': 'TransactionCanceledException'},
            'CancellationReasons': [{'Code': 'None'}, {'Code': 'None'},
                                    {'Code': 'ConditionalCheckFailed'}, {'Code': 'ConditionalCheckFailed'}, {'Code': 'ConditionalCheckFailed'}]}, 'TransactWriteItems')
        from foodtech_business_api.recipe_candidate import CANDIDATE_PREFIX, build_candidate
        candidate = build_candidate(value, self.meta)
        def get(**args):
            sk = args['Key']['sk']
            row = (self.meta if sk == 'META' else
                   {'pk': PK, 'sk': sk, **candidate} if sk.startswith(CANDIDATE_PREFIX) else
                   {'pk': PK, 'sk': sk, **value})
            return {'Item': row}
        cooking.table.get_item.side_effect = get
        self.assertEqual(RecipeReviewStore(cooking).save(PK, 3, 0, value, 0), value)
        self.assertEqual(cooking.table.get_item.call_count, 4)


if __name__ == '__main__':
    unittest.main()
