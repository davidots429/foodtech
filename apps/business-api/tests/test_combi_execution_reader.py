"""Stored projected records and real botocore shapes, no actual AWS calls."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
import unittest

from botocore.validate import validate_parameters
from foodtech_business_api.combi_execution_reader import DynamoCombiExecutionReader,EXECUTION_ATTRIBUTES
from foodtech_business_api.recipe_scope import RecipeScopeDenied
import test_combi_read_scope as scope_fixture


class ExecutionClient:
    def __init__(self,data):
        self.data=data
        self.client=data.data.client
        self.row={**asdict(data.execution),'pk':'EXEC#'+data.execution.execution_id,'sk':'META','state':'PENDING','version':1}
        self.calls=[]

    def get_item(self,**args):
        validate_parameters(args,self.client.model.operation_model('GetItem').input_shape)
        self.calls.append(deepcopy(args))
        return {'Item':self.client.encode({name:self.row[name] for name in args['ExpressionAttributeNames'].values() if name in self.row})}


class CombiExecutionReaderTests(unittest.TestCase):
    def setUp(self):
        self.data=scope_fixture.CombiReadScopeTests();self.data.setUp()
        self.client=ExecutionClient(self.data)
        self.reader=DynamoCombiExecutionReader(table_name='foodtech-ai-execution-registry-dev',client=self.client,
            subject=self.data.session.subject,epoch=self.data.session.epoch)

    def test_exact_metadata_projection_and_consistent_get_can_feed_real_history_resolver(self):
        self.data.reader=self.reader
        scope=self.data.resolve()
        self.assertTrue(scope.matches_snapshot(self.reader(self.data.execution.execution_id)))
        for call in self.client.calls:
            self.assertTrue(call['ConsistentRead'])
            self.assertEqual(call['Key'],{'pk':{'S':'EXEC#'+self.data.execution.execution_id},'sk':{'S':'META'}})
            self.assertEqual(set(call['ExpressionAttributeNames'].values()),set(EXECUTION_ATTRIBUTES))
            self.assertFalse({'model_id','credential_id','access_token','refresh_ciphertext'} & set(call['ExpressionAttributeNames'].values()))
        self.assertNotIn(self.data.session.subject,repr(self.reader))

    def test_wrong_owner_epoch_state_binding_or_incomplete_projection_is_denied(self):
        original=deepcopy(self.client.row)
        cases=[{'tenant_id':'foreign'},{'state':'RESERVED'},{'version':True},{'version':Decimal('1E30')},
            {'job_version':True},{'feature':'GENERAL_GUIDE'},{'data_classification':'CUSTOMER'},{'comparison_id':'unexpected'},
            {'pk':'EXEC#foreign'},{'sk':'RESULT'},{'job_id':'foreign'}]
        for changes in cases:
            with self.subTest(changes=changes),self.assertRaises(RecipeScopeDenied):
                self.client.row={**original,**changes};self.reader(self.data.execution.execution_id)
        for field in EXECUTION_ATTRIBUTES:
            self.client.row=deepcopy(original);del self.client.row[field]
            with self.subTest(missing=field),self.assertRaises(RecipeScopeDenied): self.reader(self.data.execution.execution_id)
        self.client.row=original;self.client.row['combi_binding']['auth_epoch']=3
        with self.assertRaises(RecipeScopeDenied): self.reader(self.data.execution.execution_id)

    def test_uuid_validation_precedes_sdk_and_decimal_numbers_normalize(self):
        with self.assertRaises(RecipeScopeDenied): self.reader('not-an-execution')
        self.assertEqual(self.client.calls,[])
        for field in ('version','job_version','prior_job_version'): self.client.row[field]=Decimal(self.client.row[field])
        result=self.reader(self.data.execution.execution_id)
        self.assertIs(type(result.version),int)
        self.assertEqual(result.execution.job_version,4)
