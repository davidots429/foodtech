import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4
from foodtech_business_api.combi_broker_runtime import configured_combi_broker_runtime
from foodtech_business_api.combi_delegation import CombiInvocationDelegation
from foodtech_business_api.recipe_scope import RecipeScopeDenied


class CombiBrokerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.delegation=CombiInvocationDelegation('synthetic_owner',2,str(uuid4()),int(time.time())+500)
        self.env={'FOODTECH_STAGE':'dev','AWS_EXECUTION_ENV':'AWS_ECS_FARGATE',
            'FOODTECH_COMBI_BROKER_ENABLED':'true','FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'true',
            'FOODTECH_AI_REGISTRY_TABLE':'foodtech-ai-execution-registry-dev','FOODTECH_AUTH_TABLE':'foodtech-auth-sessions-dev',
            'FOODTECH_COMBI_BROKER_TASK_ROLE_ARN':'arn:aws:iam::123456789012:role/foodtech-recipe-broker-dev',
            'FOODTECH_COMBI_BROKER_TASK_ROLE_ID':'AROASYNTHETICBROKERID',
            'FOODTECH_COMBI_GATEWAY_URL':'https://gateway.internal','FOODTECH_COMBI_GATEWAY_SERVER_CERT':'server.pem',
            'FOODTECH_COMBI_BROKER_CLIENT_CERT':'broker.pem','FOODTECH_COMBI_BROKER_CLIENT_KEY':'broker.key'}
        self.sdk=Mock();self.sdk.get_credentials.return_value=SimpleNamespace(method='container-role')
        self.sts,self.client=Mock(),Mock()
        self.sts.meta.endpoint_url='https://sts.ap-southeast-2.amazonaws.com'
        self.client.meta.endpoint_url='https://dynamodb.ap-southeast-2.amazonaws.com'
        self.identity={'Account':'123456789012','Arn':'arn:aws:sts::123456789012:assumed-role/foodtech-recipe-broker-dev/session-1',
            'UserId':self.env['FOODTECH_COMBI_BROKER_TASK_ROLE_ID']+':session-1'}
        self.sts.get_caller_identity.return_value=self.identity
        self.sdk.client.side_effect=lambda name,**args:self.sts if name=='sts' else self.client

    def configure(self,**changes):
        with patch.dict('os.environ',{**self.env,**changes}),patch('foodtech_business_api.combi_broker_runtime.boto3.Session',return_value=self.sdk):
            return configured_combi_broker_runtime(self.delegation)

    def test_separate_verified_broker_clients_own_minimal_reader_and_history(self):
        runtime=self.configure()
        self.assertIs(runtime.reader.client,self.client)
        self.assertEqual(runtime.reader.subject,self.delegation.subject)
        self.assertIs(runtime.history.client,self.client)
        self.assertIs(runtime.broker.sts,self.sts)
        self.sdk.resource.assert_not_called()
        self.client.get_item.assert_not_called()
        runtime.close();runtime.close()
        self.sts.close.assert_called_once();self.client.close.assert_called_once()
        for call in self.sdk.client.call_args_list:
            self.assertEqual(call.kwargs['region_name'],'ap-southeast-2')
            self.assertTrue(call.kwargs['config'].ignore_configured_endpoint_urls)
            self.assertEqual(call.kwargs['config'].proxies,{})

    def test_wrong_or_disabled_deployment_rejected_before_sdk(self):
        for change in ({'FOODTECH_COMBI_BROKER_ENABLED':'false'},{'FOODTECH_STAGE':'other'},
            {'FOODTECH_PRINCIPAL_PROVISIONING_ENABLED':'false'},{'AWS_EXECUTION_ENV':''},
            {'FOODTECH_COMBI_BROKER_TASK_ROLE_ARN':'arn:aws:iam::123456789012:role/gateway-dev'},
            {'FOODTECH_COMBI_BROKER_TASK_ROLE_ID':'invalid'},{'FOODTECH_AUTH_TABLE':'other'}):
            with self.subTest(change=change),self.assertRaises(RecipeScopeDenied): self.configure(**change)
        self.sdk.client.assert_not_called()

    def test_developer_identity_recreated_role_or_endpoint_redirect_cannot_read(self):
        for method,changes in (('env',{}),('container-role',{'UserId':'AROARECREATEDROLE123:session-1'}),
                ('container-role',{'Account':'999999999999'})):
            self.sdk.get_credentials.return_value.method=method
            self.sts.get_caller_identity.return_value={**self.identity,**changes}
            with self.subTest(method=method,changes=changes),self.assertRaises(RecipeScopeDenied): self.configure()
        self.client.get_item.assert_not_called()
        self.sdk.get_credentials.return_value.method='container-role';self.sts.get_caller_identity.return_value=self.identity
        self.client.meta.endpoint_url='https://redirect.invalid'
        with self.assertRaises(RecipeScopeDenied): self.configure()
        self.client.close.assert_called_once()
