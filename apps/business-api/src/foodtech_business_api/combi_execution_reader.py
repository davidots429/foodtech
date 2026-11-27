"""Broker-only strongly consistent execution metadata; no Gateway package dependency."""
from dataclasses import dataclass
import re

from boto3.dynamodb.types import TypeDeserializer
from foodtech_contracts.combi_execution_binding import CombiExecutionBinding,parse_combi_binding,validate_combi_execution
from foodtech_contracts.combi_history_witness import canonical_uuid,history_integer
from .recipe_scope import RecipeScopeDenied
from .subject_identity import valid_subject

EXECUTION_ATTRIBUTES = ('pk','sk','execution_id','tenant_id','feature','data_classification','job_id','job_version',
    'prior_job_id','prior_job_version','comparison_id','jeyuk_source_revision','prior_source_revision',
    'combi_binding','state','version')


@dataclass(frozen=True,repr=False)
class StoredCombiExecution:
    execution_id: str
    tenant_id: str
    combi_binding: CombiExecutionBinding
    job_id: str
    job_version: int
    prior_job_id: str
    prior_job_version: int
    feature: str = 'RECIPE_CORRECTION'
    data_classification: str = 'SYNTHETIC'
    comparison_id: None = None
    jeyuk_source_revision: None = None
    prior_source_revision: None = None

    def __repr__(self): return 'StoredCombiExecution()'


@dataclass(frozen=True,repr=False)
class StoredCombiSnapshot:
    execution: StoredCombiExecution
    state: str
    version: int

    def __repr__(self): return 'StoredCombiSnapshot()'


class DynamoCombiExecutionReader:
    def __init__(self, *, table_name, client, subject, epoch):
        if type(table_name) is not str or not re.fullmatch('foodtech-ai-execution-registry-[a-z0-9-]{1,32}',table_name) or \
                client is None or not valid_subject(subject) or type(epoch) is not int or not 0 <= epoch < 2**63:
            raise ValueError('explicit owner-bound execution reader required')
        self.table,self.client,self.subject,self.epoch=table_name,client,subject,epoch
        self.decoder=TypeDeserializer()

    def __repr__(self): return 'DynamoCombiExecutionReader()'

    def __call__(self, identifier):
        if not canonical_uuid(identifier): raise RecipeScopeDenied('combi execution unavailable')
        # Explicit low-level client also serves the existing transaction/history
        # contracts. Serialize once; never use a resource client's transformers.
        response=self.client.get_item(TableName=self.table,Key={'pk':{'S':'EXEC#'+identifier},'sk':{'S':'META'}},
            ConsistentRead=True,ProjectionExpression=', '.join('#e'+str(i) for i in range(len(EXECUTION_ATTRIBUTES))),
            ExpressionAttributeNames={'#e'+str(i):name for i,name in enumerate(EXECUTION_ATTRIBUTES)})
        try:
            encoded=response.get('Item')
            if type(encoded) is not dict or set(encoded)!=set(EXECUTION_ATTRIBUTES): raise ValueError
            row={key:self.decoder.deserialize(value) for key,value in encoded.items()}
            binding=parse_combi_binding(row['combi_binding'])
            if row['pk']!='EXEC#'+identifier or row['sk']!='META' or row['execution_id']!=identifier or \
                    row['tenant_id']!=self.subject or type(binding) is not CombiExecutionBinding or binding.auth_epoch!=self.epoch or \
                    row['feature']!='RECIPE_CORRECTION' or row['data_classification']!='SYNTHETIC' or \
                    row['state']!='PENDING' or any(row[k] is not None for k in
                        ('comparison_id','jeyuk_source_revision','prior_source_revision')): raise ValueError
            execution=StoredCombiExecution(identifier,self.subject,binding,row['job_id'],
                history_integer(row['job_version'],1,10**18),row['prior_job_id'],history_integer(row['prior_job_version'],1,10**18))
            validate_combi_execution(execution)
            return StoredCombiSnapshot(execution,'PENDING',history_integer(row['version'],1,2**63-1))
        except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError):
            raise RecipeScopeDenied('combi execution unavailable') from None
