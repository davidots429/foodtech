import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.job_history_deletion import remove_job_history


class HistoryDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject,self.request=str(uuid4()),str(uuid4());self.source=Mock()
        self.source.name='cooking';self.source.table_arn='arn:source';self.client=Mock()
        self.progress=SimpleNamespace(auth_table='auth',registry_table='registry',progress_table='progress',
                                      client=self.client,load_checkpoint=Mock())
        self.args=dict(source_table=self.source,progress_store=self.progress,cipher=SimpleNamespace(source_table_arn='arn:source'),
                       subject=self.subject,request_id=self.request)
        self.row={'pk':f'SCOPE#USER#{self.subject}#JOBS','sk':'HISTORY#REVISION','revision':Decimal(2)}

    def test_guarded_delete_and_absent_retry(self):
        self.source.query.return_value={'Items':[self.row]}
        self.assertEqual(remove_job_history(**self.args),{'state':'VERIFYING'})
        operations=self.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(operations[2]['Delete']['ExpressionAttributeValues'][':revision'],{'N':'2'})
        self.source.query.return_value={'Items':[]}
        self.assertEqual(remove_job_history(**self.args),{'state':'VERIFYING'})

    def test_remaining_listing_invalid_revision_and_changed_state_fail(self):
        for result in ({'Items':[self.row,{'pk':self.row['pk'],'sk':'CREATED#x#job'}]},
                       {'Items':[{**self.row,'revision':True}]},
                       {'Items':[self.row],'LastEvaluatedKey':{'pk':'x','sk':'y'}}):
            self.source.query.return_value=result
            with self.assertRaises(ValueError):remove_job_history(**self.args)
        self.source.query.return_value={'Items':[self.row]}
        self.client.transact_write_items.side_effect=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):remove_job_history(**self.args)


if __name__=='__main__':unittest.main()
