import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.job_binding_inventory import JobBindingInventory
from test_profile_deletion_inventory import Progress


class BindingInventoryTests(unittest.TestCase):
    def setUp(self):
        self.subject,self.request=str(uuid4()),str(uuid4()); self.source=Mock()
        self.source.name='cooking';self.source.table_arn='arn:source'
        self.progress=Progress();self.progress.client=Mock();self.cipher=SimpleNamespace(source_table_arn='arn:source')
        self.inventory=JobBindingInventory(source_table=self.source,progress_store=self.progress,cipher=self.cipher)
        self.scope={'subject':self.subject,'request_id':self.request}
        self.row={'pk':'JEYUK#jeyuk-test','sk':'BINDING',
                  'job_pk':f'SCOPE#USER#{self.subject}#JOB#job','job_id':'job'}

    def test_capture_and_delete_orphan_under_matching_owner(self):
        self.source.scan.return_value={'Items':[self.row],'ScannedCount':1}
        self.assertTrue(self.inventory.capture_page(**self.scope,limit=20)['complete'])
        targets=list(self.inventory.targets(**self.scope))
        self.assertEqual(len(targets),1);self.assertNotIn('jeyuk-test',repr(targets))
        self.assertEqual(self.inventory.remove_one(**self.scope,index=0),{'index':0,'state':'VERIFYING'})
        operations=self.progress.client.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(operations[2]['Delete']['Key']['pk'],{'S':'JEYUK#jeyuk-test'})
        self.assertIn('job_pk = :job',operations[2]['Delete']['ConditionExpression'])

    def test_bad_binding_fails_before_capture_and_conflicting_delete_fails(self):
        self.source.scan.return_value={'Items':[{**self.row,'job_pk':'SCOPE#USER#other#JOB#job'}],'ScannedCount':1}
        with self.assertRaises(ValueError):self.inventory.capture_page(**self.scope,limit=20)
        self.assertFalse(self.progress.items)
        self.source.scan.return_value={'Items':[self.row],'ScannedCount':1}
        self.inventory.capture_page(**self.scope,limit=20)
        self.progress.client.transact_write_items.side_effect=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):self.inventory.remove_one(**self.scope,index=0)


if __name__=='__main__':unittest.main()
