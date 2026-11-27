import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.job_partition_deletion import delete_one_job_record


class JobPartitionDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject, self.request = str(uuid4()), str(uuid4())
        self.pk=f'SCOPE#USER#{self.subject}#JOB#job'
        self.table=Mock(); self.table.name='cooking'; self.table.table_arn='arn:source'
        self.client=Mock()
        self.progress=SimpleNamespace(auth_table='auth',registry_table='registry',progress_table='progress',
                                      client=self.client,load_checkpoint=Mock())
        self.args=dict(source_table=self.table,progress_store=self.progress,cipher=SimpleNamespace(source_table_arn='arn:source'),
                       subject=self.subject,request_id=self.request,job_id='job')
        self.meta={'pk':self.pk,'sk':'META','owner_subject':self.subject,'job_id':'job'}

    def test_deletes_one_non_meta_with_lock_and_owner_checks(self):
        for sk in ('EVENT#1','RECIPE_REVIEW#CURRENT','RECIPE_REVIEW#VERSION#0000000000000000001',
                   'RECIPE_CANDIDATE#REVIEW#0000000000000000001'):
            with self.subTest(sk=sk):
                self.table.query.return_value={'Items':[{'pk':self.pk,'sk':sk},self.meta]}
                self.assertEqual(delete_one_job_record(**self.args),{'state':'IN_PROGRESS','removed':True})
                ops=self.client.transact_write_items.call_args.kwargs['TransactItems']
                self.assertEqual(ops[2]['ConditionCheck']['Key']['sk'], {'S':'META'})
                self.assertEqual(ops[3]['Delete']['Key']['sk'], {'S':sk})
                self.assertEqual(ops[1]['ConditionCheck']['ExpressionAttributeValues'][':active'],{'S':'DELETING'})

    def test_meta_only_never_claims_completion_or_deletes(self):
        self.table.query.return_value={'Items':[self.meta]}
        self.assertEqual(delete_one_job_record(**self.args),{'state':'META_ONLY','removed':False})
        self.client.transact_write_items.assert_not_called()
        for page in ({'Items':[]},{'Items':[self.meta],'LastEvaluatedKey':{'pk':self.pk,'sk':'META'}},
                     {'Items':[{**self.meta,'owner_subject':'other'}]},
                     {'Items':[{'pk':'other','sk':'EVENT#1'}]}):
            self.table.query.return_value=page
            with self.assertRaises(ValueError): delete_one_job_record(**self.args)

    def test_invalid_scope_and_transaction_conflict_fail_closed(self):
        with self.assertRaises(ValueError): delete_one_job_record(**{**self.args,'job_id':'job#other'})
        self.table.query.return_value={'Items':[{'pk':self.pk,'sk':'EVENT#1'}]}
        self.client.transact_write_items.side_effect=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict): delete_one_job_record(**self.args)


if __name__ == '__main__': unittest.main()
