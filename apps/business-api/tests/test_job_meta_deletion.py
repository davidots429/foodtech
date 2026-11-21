import json
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.job_meta_deletion import JobMetaDeletion
from test_profile_deletion_inventory import Progress


class MetaDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject,self.request=str(uuid4()),str(uuid4());self.source=Mock()
        self.source.name='cooking';self.source.table_arn='arn:source';self.progress=Progress();self.progress.client=Mock()
        self.cipher=SimpleNamespace(source_table_arn='arn:source')
        self.inventory=SimpleNamespace(source_table=self.source,progress_store=self.progress,cipher=self.cipher,
            targets=lambda **kw:iter([SimpleNamespace(job_id='job')]),_json=json.loads)
        self.residual=SimpleNamespace(source_table=self.source,progress_store=self.progress,cipher=self.cipher,
                                      _checkpoint=Mock(return_value=({'complete':True,'residual':False},1)))
        self.bindings=SimpleNamespace(source_table=self.source,progress_store=self.progress,cipher=self.cipher,
                                      _checkpoint=Mock(return_value=({'complete':True,'residual':False},1)))
        self.worker=JobMetaDeletion(inventory=self.inventory,residual=self.residual,binding_residual=self.bindings)
        self.scope={'subject':self.subject,'request_id':self.request,'index':0}
        self.meta={'pk':f'SCOPE#USER#{self.subject}#JOB#job','sk':'META','job_id':'job',
                   'owner_subject':self.subject,'version':Decimal(3),'source_binding':{'batch_id':'synthetic'}}

    def test_snapshot_saved_before_delete_and_retry_with_missing_meta(self):
        self.source.query.side_effect=[{'Items':[self.meta]},{'Items':[]}]
        self.assertEqual(self.worker.remove_one(**self.scope),{'index':0,'state':'VERIFYING'})
        saved=self.progress.items[(self.subject,self.request,'JOB_META_000000000000')]
        self.assertIn('synthetic',saved['checkpoint'].decode())
        self.assertEqual(self.worker.remove_one(**self.scope),{'index':0,'state':'VERIFYING'})
        self.assertEqual(self.progress.client.transact_write_items.call_count,2)
        self.assertIn('#v = :version',self.progress.client.transact_write_items.call_args.kwargs['TransactItems'][2]['Delete']['ConditionExpression'])

    def test_residual_or_remaining_record_blocks(self):
        self.residual._checkpoint.return_value=({'complete':False,'residual':True},1)
        with self.assertRaises(ValueError):self.worker.remove_one(**self.scope)
        self.residual._checkpoint.return_value=({'complete':True,'residual':False},1)
        self.source.query.return_value={'Items':[{'pk':self.meta['pk'],'sk':'EVENT#1'}]}
        with self.assertRaises(ValueError):self.worker.remove_one(**self.scope)
        self.assertFalse(self.progress.items);self.progress.client.transact_write_items.assert_not_called()

    def test_conflicting_transaction_is_not_treated_as_removed(self):
        self.source.query.return_value={'Items':[self.meta]}
        self.progress.client.transact_write_items.side_effect=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):self.worker.remove_one(**self.scope)
        self.assertIn((self.subject,self.request,'JOB_META_000000000000'),self.progress.items)

    def test_batch_retains_each_snapshot_across_partial_failure(self):
        self.inventory.targets=lambda **kw:iter([SimpleNamespace(job_id='job'),SimpleNamespace(job_id='other')])
        other={**self.meta,'pk':f'SCOPE#USER#{self.subject}#JOB#other','job_id':'other'}
        self.source.query.side_effect=[{'Items':[self.meta]},{'Items':[other]},{'Items':[]},{'Items':[other]}]
        self.progress.client.transact_write_items.side_effect=[None,RuntimeError('temporary'),None,None]
        with self.assertRaises(RuntimeError):self.worker.remove_batch(subject=self.subject,request_id=self.request,start=0,count=2)
        self.assertEqual(self.worker.remove_batch(subject=self.subject,request_id=self.request,start=0,count=2),
                         {'count':2,'state':'VERIFYING'})
        self.assertIn((self.subject,self.request,'JOB_META_000000000000'),self.progress.items)
        self.assertIn((self.subject,self.request,'JOB_META_000000000001'),self.progress.items)


if __name__=='__main__':unittest.main()
