import io
import json
from contextlib import redirect_stdout,redirect_stderr
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters
from foodtech_business_api.combi_execution_reader import DynamoCombiExecutionReader
from foodtech_business_api.combi_execution_read_probe import configuration,requests,main
from foodtech_business_api.recipe_scope import RecipeScopeDenied

ACCOUNT='123456789012'
ROLE_ID='AROASYNTHETICBROKERID'
PROBE='a55fe880-d10f-4f81-997f-d8a5c97ae147'
ENV={'FOODTECH_COMBI_EXECUTION_READ_PROBE_ENABLED':'true','AWS_EXECUTION_ENV':'AWS_ECS_FARGATE','FOODTECH_STAGE':'dev',
    'FOODTECH_COMBI_EXECUTION_PROBE_ACCOUNT':ACCOUNT,'FOODTECH_COMBI_EXECUTION_PROBE_ROLE_ID':ROLE_ID,
    'FOODTECH_COMBI_EXECUTION_PROBE_ID':PROBE}


class ProbeClient:
    def __init__(self):
        self.calls=[];self.error_code='AccessDeniedException';self.unexpected_allow=False;self.item=None;self.fail_first=False
        self.meta=SimpleNamespace(endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com')
        self.close=Mock();self.model=get_session().get_service_model('dynamodb')

    def call(self,operation,args):
        validate_parameters(args,self.model.operation_model(operation).input_shape)
        self.calls.append((operation,args))
        allowed=operation=='GetItem' and args['Key']['pk']['S'].startswith('EXEC#') and \
            'ProjectionExpression' in args and '#forbidden' not in args.get('ExpressionAttributeNames',{})
        if (not allowed or self.fail_first) and not self.unexpected_allow:
            raise ClientError({'Error':{'Code':self.error_code,'Message':'PRIVATE_ERROR_VALUE'}},operation)
        return {'Item':self.item} if self.item is not None else {}

    def get_item(self,**args): return self.call('GetItem',args)
    def transact_get_items(self,**args): return self.call('TransactGetItems',args)
    def query(self,**args): return self.call('Query',args)


class CombiExecutionReadProbeTests(unittest.TestCase):
    def run_main(self,*,env=ENV,method='container-role',identity=None,client=None):
        client=client or ProbeClient();sts=Mock();sts.meta.endpoint_url='https://sts.ap-southeast-2.amazonaws.com'
        sts.get_caller_identity.return_value=identity or {'Account':ACCOUNT,
            'Arn':f'arn:aws:sts::{ACCOUNT}:assumed-role/foodtech-recipe-broker-dev/probe-session','UserId':ROLE_ID+':probe-session'}
        sdk=Mock();sdk.get_credentials.return_value=SimpleNamespace(method=method)
        sdk.client.side_effect=lambda service,**kwargs:sts if service=='sts' else client
        out,err=io.StringIO(),io.StringIO()
        with patch.dict('os.environ',env,clear=True),patch('foodtech_business_api.combi_execution_read_probe.boto3.Session',return_value=sdk) as factory,\
                redirect_stdout(out),redirect_stderr(err): code=main()
        return code,out.getvalue(),err.getvalue(),sdk,sts,client,factory

    def test_two_allowed_reads_and_six_exact_denials_emit_counts_without_record_data(self):
        code,out,err,sdk,sts,client,factory=self.run_main()
        self.assertEqual((code,err),(0,''))
        value=json.loads(out)
        self.assertEqual(len(value['accepted_reads']),2);self.assertEqual(len(value['expected_denials']),6)
        self.assertFalse(value['source_session_verified']);self.assertFalse(value['model_invocation_verified'])
        self.assertEqual(len(client.calls),8)
        self.assertEqual({x[0] for x in client.calls},{'GetItem','TransactGetItems','Query'})
        for secret in (PROBE,ROLE_ID,'PRIVATE_ERROR_VALUE'): self.assertNotIn(secret,out+err)
        factory.assert_called_once_with(region_name='ap-southeast-2')
        client.close.assert_called_once();sts.close.assert_called_once()
        sdk.resource.assert_not_called()
        for call in sdk.client.call_args_list:
            self.assertEqual(call.kwargs['config'].retries['total_max_attempts'],1)
            self.assertEqual(call.kwargs['config'].proxies,{})

    def test_positive_request_exactly_matches_actual_execution_reader_projection(self):
        probe=configuration(ENV)
        _,_,_,expected=requests(probe)[0]
        client=Mock();client.get_item.return_value={}
        reader=DynamoCombiExecutionReader(table_name='foodtech-ai-execution-registry-dev',client=client,subject='synthetic_owner',epoch=0)
        with self.assertRaises(RecipeScopeDenied): reader(PROBE)
        self.assertEqual(client.get_item.call_args.kwargs,expected)

    def test_invalid_or_disabled_configuration_never_discovers_credentials(self):
        for changes in ({'FOODTECH_COMBI_EXECUTION_READ_PROBE_ENABLED':'false'},{'AWS_EXECUTION_ENV':'AWS_ECS_EC2'},
            {'FOODTECH_STAGE':'prod'},{'FOODTECH_COMBI_EXECUTION_PROBE_ACCOUNT':'invalid'},
            {'FOODTECH_COMBI_EXECUTION_PROBE_ID':PROBE.upper()},{'FOODTECH_COMBI_EXECUTION_PROBE_ID':'invalid'},
            {'FOODTECH_COMBI_EXECUTION_PROBE_ROLE_ID':'invalid'}):
            with self.subTest(changes=changes):
                code,out,_,_,_,_,factory=self.run_main(env={**ENV,**changes})
                self.assertEqual((code,out),(2,''));factory.assert_not_called()

    def test_unexpected_data_stops_before_any_broader_probe_and_never_logs_values(self):
        client=ProbeClient();client.item={'pk':{'S':'PRIVATE_ITEM_VALUE'}}
        code,out,err,*_=self.run_main(client=client)
        self.assertEqual((code,out),(1,''));self.assertEqual(len(client.calls),1)
        self.assertNotIn('PRIVATE_ITEM_VALUE',err);client.close.assert_called_once()

    def test_missing_access_throttles_validation_errors_or_unexpected_allow_are_not_success(self):
        for kind in ('first_denied','throttle','not_found','unexpected_allow'):
            client=ProbeClient()
            if kind=='first_denied': client.fail_first=True
            elif kind=='throttle': client.error_code='ThrottlingException'
            elif kind=='not_found': client.error_code='ResourceNotFoundException'
            else: client.unexpected_allow=True
            with self.subTest(kind=kind):
                code,out,err,*_=self.run_main(client=client)
                self.assertEqual((code,out),(1,''));self.assertNotIn('PRIVATE_ERROR_VALUE',err)

    def test_developer_wrong_role_or_recreated_identity_cannot_read(self):
        for method in ('env','shared-credentials-file','assume-role','login',None):
            code,out,_,sdk,_,client,_=self.run_main(method=method)
            self.assertEqual((code,out),(1,''));sdk.client.assert_not_called();self.assertEqual(client.calls,[])
        valid={'Account':ACCOUNT,'Arn':f'arn:aws:sts::{ACCOUNT}:assumed-role/foodtech-recipe-broker-dev/probe-session','UserId':ROLE_ID+':probe-session'}
        for changes in ({'Account':'999999999999'},{'Arn':valid['Arn'].replace('recipe-broker','ai-gateway')},
                        {'UserId':'AROARECREATEDBROKER1:probe-session'}):
            with self.subTest(changes=changes):
                code,out,_,_,sts,client,_=self.run_main(identity={**valid,**changes})
                self.assertEqual((code,out),(1,''));self.assertEqual(client.calls,[]);sts.close.assert_called_once()

    def test_deadline_aborts_without_logging_sdk_values_and_restores_signal_handler(self):
        import signal
        previous=signal.getsignal(signal.SIGALRM)
        client=ProbeClient()
        def timeout(**kwargs):
            signal.getsignal(signal.SIGALRM)(signal.SIGALRM,None)
        client.get_item=Mock(side_effect=timeout)
        code,out,err,_,sts,_,_=self.run_main(client=client)
        self.assertEqual((code,out),(1,''))
        self.assertNotIn('deadline',err)
        self.assertIs(signal.getsignal(signal.SIGALRM),previous)
        self.assertEqual(signal.alarm(0),0)
        client.close.assert_called_once();sts.close.assert_called_once()

    def test_redirected_endpoint_is_not_used_for_metadata_reads(self):
        client=ProbeClient();client.meta.endpoint_url='https://redirect.invalid'
        code,out,_,_,sts,_,_=self.run_main(client=client)
        self.assertEqual((code,out),(1,''));self.assertEqual(client.calls,[])
        client.close.assert_called_once();sts.close.assert_called_once()
