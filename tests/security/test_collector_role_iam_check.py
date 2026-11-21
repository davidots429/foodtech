import importlib.util
from pathlib import Path
import unittest


path = Path(__file__).resolve().parents[1] / 'live/collector_role_iam_check.py'
spec = importlib.util.spec_from_file_location('collector_role_iam_check', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakePaginator:
    def __init__(self, owner, name):
        self.owner, self.name = owner, name

    def paginate(self, **kwargs):
        self.owner.calls.append((self.name, kwargs))
        return [{'EvaluationResults': [{'EvalDecision': 'explicitDeny',
                 'OrganizationsDecisionDetail': {'AllowedByOrganizations': False}}]}]


class FakeIam:
    def __init__(self):
        self.calls = []

    def get_paginator(self, name):
        return FakePaginator(self, name)


class CollectorRoleSimulationTests(unittest.TestCase):
    def test_key_context_and_organization_result_are_preserved(self):
        iam = FakeIam()
        result = module.simulate(iam, {'PolicySourceArn': 'role'}, 'dynamodb:DeleteItem', 'table', 'STREAM#test')
        self.assertEqual(result, {'decision': 'explicitDeny', 'organizations_allowed': False})
        self.assertEqual(iam.calls[0][0], 'simulate_principal_policy')
        self.assertEqual(iam.calls[0][1]['ContextEntries'][0]['ContextKeyValues'], ['STREAM#test'])
        module.simulate(iam, {'PolicyInputList': ['policy']}, 'dynamodb:DeleteItem', 'table', None)
        self.assertEqual(iam.calls[1][0], 'simulate_custom_policy')
        self.assertEqual(iam.calls[1][1]['ContextEntries'], [])

    def test_scenarios_include_restricted_resource_and_missing_key(self):
        cases = {case[0]: case for case in module.scenarios('synthetic-bucket')}
        self.assertTrue(cases['delete_stream'][-1])
        self.assertFalse(cases['delete_without_key'][-1])
        self.assertTrue(cases['quarantine_write'][2].endswith('/ingestion/synthetic'))
        self.assertFalse(cases['other_object_write'][-1])


if __name__ == '__main__':
    unittest.main()
