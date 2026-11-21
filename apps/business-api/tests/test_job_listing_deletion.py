import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.job_listing_deletion import clean_one_job_listing


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.subject,self.request=str(uuid4()),str(uuid4());self.source=Mock()
        self.source.name='cooking';self.source.table_arn='arn:source';self.client=Mock()
        self.progress=SimpleNamespace(auth_table='auth',registry_table='registry',progress_table='progress',
                                      client=self.client,load_checkpoint=Mock())
        self.args=dict(source_table=self.source,progress_store=self.progress,cipher=SimpleNamespace(source_table_arn='arn:source'),
                       subject=self.subject,request_id=self.request)
        self.row={'pk':f'SCOPE#USER#{self.subject}#JOBS','sk':'CREATED#2026-01-01#job',
                  'job_id':'job','created_at':'2026-01-01'}

    def test_removes_one_exact_listing_with_both_guards(self):
        self.source.query.return_value={'Items':[self.row]}
        self.assertEqual(clean_one_job_listing(**self.args),{'removed':True,'state':'IN_PROGRESS'})
        ops=self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertIn('suspended',ops[0]['ConditionCheck']['ConditionExpression'])
        self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'],{'S':'DELETING'})
        self.assertIn('created_at = :created',ops[2]['Delete']['ConditionExpression'])

    def test_empty_is_observation_and_malformed_entry_fails(self):
        self.source.query.return_value={'Items':[]}
        self.assertEqual(clean_one_job_listing(**self.args),{'removed':False,'state':'VERIFYING'})
        self.source.query.return_value={'Items':[{**self.row,'job_id':'other'}]}
        with self.assertRaises(ValueError):clean_one_job_listing(**self.args)
        self.client.transact_write_items.assert_not_called()

    def test_changed_lock_or_listing_conflicts(self):
        self.source.query.return_value={'Items':[self.row]}
        self.client.transact_write_items.side_effect=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):clean_one_job_listing(**self.args)


if __name__=='__main__':unittest.main()
