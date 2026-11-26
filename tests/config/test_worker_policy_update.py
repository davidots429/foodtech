import copy
import importlib.util
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[2] / 'infra/scripts/review_worker_policy_update.py'
SPEC = importlib.util.spec_from_file_location('worker_policy_review', PATH)
reviewer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reviewer)


class WorkerPolicyReviewTests(unittest.TestCase):
    def pair(self):
        account = '123456789012'
        data = f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-data-flow-dev'
        auth = f'arn:aws:dynamodb:ap-southeast-2:{account}:table/foodtech-auth-sessions-dev'
        old = {'Resources': {
            'Worker': {'Type': 'AWS::IAM::Role', 'Properties': {'Description': 'standardization worker'}},
            'Policy': {'Type': 'AWS::IAM::Policy', 'Properties': {'Roles': [{'Ref': 'Worker'}],
                'PolicyDocument': {'Version': '2012-10-17', 'Statement': [
                    {'Effect': 'Allow', 'Action': ['dynamodb:GetItem'], 'Resource': data}]}}}}}
        new = copy.deepcopy(old)
        new['Resources']['Policy']['Properties']['PolicyDocument']['Statement'].extend([
            {'Effect': 'Allow', 'Action': 'dynamodb:Query', 'Resource': data},
            {'Effect': 'Allow', 'Action': 'dynamodb:ConditionCheckItem', 'Resource': auth}])
        return old, new, account

    def test_exact_two_permissions_are_reviewable_without_provisioning_resources(self):
        result = reviewer.review(*self.pair())
        self.assertTrue(result['reviewed'])
        self.assertFalse(result['runtime_verified'])
        self.assertEqual(result['added_resources'], [])
        self.assertEqual(result['removed_resources'], [])

    def test_missing_existing_collection_resources_and_new_roles_are_rejected(self):
        for mode in ('remove', 'add'):
            old, new, account = self.pair()
            if mode == 'remove':
                old['Resources']['ExistingCollector'] = {'Type': 'AWS::IAM::Role'}
            else:
                new['Resources']['UnexpectedRole'] = {'Type': 'AWS::IAM::Role'}
            with self.assertRaises(ValueError):
                reviewer.review(old, new, account)

    def test_auth_read_wildcards_and_policy_reattachment_are_rejected(self):
        for variant in ('auth_read', 'wildcard', 'attachment', 'trust'):
            old, new, account = self.pair()
            props = new['Resources']['Policy']['Properties']
            if variant == 'auth_read':
                props['PolicyDocument']['Statement'][-1]['Action'] = 'dynamodb:GetItem'
            elif variant == 'wildcard':
                props['PolicyDocument']['Statement'][-1]['Resource'] = '*'
            elif variant == 'attachment':
                props['Roles'] = [{'Ref': 'OtherRole'}]
            else:
                new['Resources']['Worker']['Properties']['AssumeRolePolicyDocument'] = {'Statement': []}
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                reviewer.review(old, new, account)

    def test_output_and_deployment_contract_changes_are_rejected(self):
        old, new, account = self.pair()
        new['Outputs'] = {'Unexpected': {'Value': 'changed'}}
        with self.assertRaises(ValueError):
            reviewer.review(old, new, account)


if __name__ == '__main__':
    unittest.main()
