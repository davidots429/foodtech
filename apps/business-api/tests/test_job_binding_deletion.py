import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4
from botocore.exceptions import ClientError
from foodtech_business_api.condition_profile_deletion import ProfileDeletionConflict
from foodtech_business_api.job_binding_deletion import delete_job_binding


class BindingDeletionTests(unittest.TestCase):
    def setUp(self):
        self.subject,self.request=str(uuid4()),str(uuid4())
        self.source=Mock();self.source.name='cooking';self.source.table_arn='arn:source'
        self.client=Mock();self.progress=SimpleNamespace(auth_table='auth',registry_table='registry',progress_table='progress',
                                                        client=self.client,load_checkpoint=Mock())
        self.args=dict(source_table=self.source,progress_store=self.progress,cipher=SimpleNamespace(source_table_arn='arn:source'),
                       subject=self.subject,request_id=self.request,job_id='job')
        self.meta={'owner_subject':self.subject,'job_id':'job','menu':'JEYUK','source_binding':{
            'source_id':'synthetic-rest','device_id':'mockprofile-110-a-001','batch_id':'jeyuk-test'}}

    def test_jeyuk_and_tonkatsu_bindings_are_derived_and_guarded(self):
        for meta, prefix in ((self.meta,'JEYUK#jeyuk-test'),
          ({**self.meta,'menu':'TONKATSU','source_binding':{'source_id':'synthetic-rest',
            'device_id':'mockprofile-100-a-001','batch_id':'batch-a-test'}},'STREAM#')):
            self.source.get_item.return_value={'Item':meta}
            self.assertEqual(delete_job_binding(**self.args),{'state':'VERIFYING'})
            ops=self.client.transact_write_items.call_args.kwargs['TransactItems']
            self.assertTrue(ops[3]['Delete']['Key']['pk']['S'].startswith(prefix))
            self.assertIn('source_binding = :binding',ops[2]['ConditionCheck']['ConditionExpression'])
            self.assertIn('job_pk = :job',ops[3]['Delete']['ConditionExpression'])

    def test_missing_binding_and_invalid_owner_fail_closed(self):
        self.source.get_item.return_value={'Item':{**self.meta,'source_binding':None}}
        self.assertEqual(delete_job_binding(**self.args),{'state':'UNBOUND'})
        for meta in ({**self.meta,'owner_subject':'other'}, {**self.meta,'source_binding':{'batch_id':'other'}}):
            self.source.get_item.return_value={'Item':meta}
            with self.assertRaises(ValueError):delete_job_binding(**self.args)
        self.client.transact_write_items.assert_not_called()

    def test_conflicting_reverse_binding_is_not_deleted(self):
        self.source.get_item.return_value={'Item':self.meta}
        self.client.transact_write_items.side_effect=ClientError({'Error':{'Code':'TransactionCanceledException'},
            'CancellationReasons':[{'Code':'ConditionalCheckFailed'}]},'TransactWriteItems')
        with self.assertRaises(ProfileDeletionConflict):delete_job_binding(**self.args)


if __name__=='__main__':unittest.main()
