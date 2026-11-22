import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

path = Path(__file__).resolve().parents[1] / 'live/withdrawal_identity_iam_check.py'
spec = importlib.util.spec_from_file_location('withdrawal_identity_iam_check', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class IdentityPolicyGateTests(unittest.TestCase):
    def test_broad_allow_is_rejected_and_result_contains_no_resource_or_key(self):
        checks = module.scenarios('123456789012', 'dev', 'ap-southeast-2_SyntheticPool')
        def simulate(**kwargs):
            return {'EvaluationResults': [{'EvalActionName': kwargs['ActionNames'][0],
                'EvalResourceName': kwargs['ResourceArns'][0], 'EvalDecision': 'allowed'}]}
        result = module.verify(SimpleNamespace(simulate_custom_policy=simulate), ['synthetic-policy'], checks)
        self.assertFalse(result['passed'])
        self.assertFalse(result['runtime_verified'])
        failures = {row['name'] for row in result['checks'] if not row['passed']}
        self.assertTrue({'auth_DeleteItem_denied', 'registry_UpdateItem_denied', 'scan_all_attributes_denied',
                         'unrelated_GetItem_denied', 'other_pool_AdminDeleteUser_denied',
                         'arbitrary_storage_decrypt_denied', 'missing_update_key_denied'} <= failures)
        self.assertNotIn('123456789012', json.dumps(result))
        self.assertNotIn('USER#synthetic', json.dumps(result))

    def test_deny_everything_is_not_a_working_policy(self):
        checks = module.scenarios('123456789012', 'dev', 'ap-southeast-2_SyntheticPool')
        def simulate(**kwargs):
            return {'EvaluationResults': [{'EvalActionName': kwargs['ActionNames'][0],
                'EvalResourceName': kwargs['ResourceArns'][0], 'EvalDecision': 'implicitDeny'}]}
        result = module.verify(SimpleNamespace(simulate_custom_policy=simulate), ['synthetic-policy'], checks)
        self.assertFalse(result['passed'])
        self.assertTrue(any(not row['passed'] and row['expected_allow'] for row in result['checks']))

    def test_malformed_or_partial_simulation_never_passes(self):
        check = module.scenarios('123456789012', 'dev', 'ap-southeast-2_SyntheticPool')[0]
        for response in ({}, {'EvaluationResults': [], 'IsTruncated': True},
                         {'EvaluationResults': [{'EvalActionName': check['action'],
                             'EvalResourceName': check['resource'], 'EvalDecision': 'unknown'}]}):
            with self.subTest(response=response), self.assertRaises(ValueError):
                module.verify(SimpleNamespace(simulate_custom_policy=lambda **_: response), ['synthetic-policy'], [check])

    def test_candidate_wrapper_preserves_documents_and_refuses_nonidentity(self):
        # Test fixture only, not a candidate for deployment.
        document = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Deny', 'Action': '*', 'Resource': '*'}]}
        wrapped = {'Policies': [{'PolicyType': 'Identity', 'Policy': document}]}
        self.assertEqual(json.loads(module.policy_inputs(wrapped)[0]), document)
        wrapped['Policies'][0]['PolicyType'] = 'Resource'
        with self.assertRaises(ValueError):
            module.policy_inputs(wrapped)


if __name__ == '__main__':
    unittest.main()
