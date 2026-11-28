"""Internal retained-Combi coverage registration behind real writer controls.

This never creates ENFORCED writer attestations or infers them from scans,
caller booleans, a roster counter or an STS identity. Independent deployment/IAM
verification remains mandatory, including exclusive atomic writers and protected
USER coverage fields. No client factory, role, CLI, route or activation exists.
All writes are limited to one protected field on an existing active USER, with
writer/revision/account CAS. Lost responses require exact read-only recovery.
"""
import re
from uuid import uuid4
from boto3.dynamodb.types import TypeSerializer,TypeDeserializer
from botocore.exceptions import BotoCoreError,ClientError
from foodtech_contracts.combi_history_witness import (CONTROL_KEY,COVERAGE_FIELD,HISTORY_CONTRACT,
    COVERAGE_SCOPE,canonical_uuid,history_integer,roster_digest)
from .combi_roster_audit import DynamoCombiRosterAudit,CombiRosterObservation,MAX_MEALS,MAX_SCAN_ITEMS
from .subject_identity import valid_subject


class CombiHistoryCoverageUnavailable(Exception):
    pass


def _digest(value):
    return type(value) is str and re.fullmatch('[a-f0-9]{64}',value) is not None


class DynamoCombiHistoryCoverageRegistrar:
    def __init__(self,*,auth_table,cooking_table,account_id,client,store,clock):
        stage=re.fullmatch('foodtech-auth-sessions-([a-z0-9-]{1,32})',auth_table) if type(auth_table) is str else None
        if not stage or cooking_table!='foodtech-cooking-jobs-'+stage[1] or type(account_id) is not str or \
                not re.fullmatch('[0-9]{12}',account_id) or client is None or store.client is not client or \
                store.table.name!=cooking_table or not callable(clock):
            raise ValueError('explicit same-stage privileged Combi registrar dependencies required')
        self.auth,self.cooking,self.client,self.store,self.clock=auth_table,cooking_table,client,store,clock
        self.cooking_arn=f'arn:aws:dynamodb:ap-southeast-2:{account_id}:table/{cooking_table}'
        self.serializer,self.decoder=TypeSerializer(),TypeDeserializer()
        self.audit=DynamoCombiRosterAudit(store=store)

    def __repr__(self):
        return 'DynamoCombiHistoryCoverageRegistrar()'

    def _encode(self,row):
        return {k:self.serializer.serialize(v) for k,v in row.items()}

    def _now(self):
        now=self.clock()
        if type(now) is not int or not 0<now<2**63:raise ValueError('trusted integer clock required')
        return now

    def _controls(self,owner,epoch):
        response=self.client.transact_get_items(TransactItems=[{'Get':{
            'TableName':self.auth,'Key':self._encode({'pk':'USER#'+owner}),
            'ProjectionExpression':'pk, #epoch, suspended, deletion_lock_request, #coverage',
            'ExpressionAttributeNames':{'#epoch':'epoch','#coverage':COVERAGE_FIELD}}}, {'Get':{
            'TableName':self.auth,'Key':self._encode({'pk':CONTROL_KEY}),
            'ProjectionExpression':'pk, #state, #version, #generation, writer_contract, cooking_table_arn, manifest_sha256, verified_at, valid_until',
            'ExpressionAttributeNames':{'#state':'state','#version':'version','#generation':'generation'}}}]).get('Responses')
        if type(response) is not list or len(response)!=2:raise ValueError('control snapshot unavailable')
        user,writer=[{k:self.decoder.deserialize(v) for k,v in row['Item'].items()} for row in response]
        now=self._now()
        if user.get('pk')!='USER#'+owner or history_integer(user['epoch'],0,2**63-1)!=epoch or \
                user.get('suspended',False) is not False or 'deletion_lock_request' in user or \
                writer.get('pk')!=CONTROL_KEY or writer.get('state')!='ENFORCED' or \
                writer.get('writer_contract')!=HISTORY_CONTRACT or writer.get('cooking_table_arn')!=self.cooking_arn or \
                not canonical_uuid(writer.get('generation')) or not _digest(writer.get('manifest_sha256')) or \
                not history_integer(writer['verified_at'],1,2**63-1)<=now<history_integer(writer['valid_until'],1,2**63-1):
            raise ValueError('independently verified Combi writer/account controls required')
        history_integer(writer['version'])
        old=user.get(COVERAGE_FIELD)
        if old is not None:
            if type(old) is not dict:raise ValueError('invalid previous Combi coverage')
            history_integer(old['version'])
        return user,writer

    def _receipt(self,owner,epoch,registration_id,expected=None):
        user,writer=self._controls(owner,epoch);coverage=user[COVERAGE_FIELD]
        fields={'state','version','generation','writer_contract','coverage_scope','coverage_revision',
            'baseline_roster_sha256','proof_sha256','writer_manifest_sha256','registration_id','registration_epoch','record_count'}
        if type(coverage) is not dict or set(coverage)!=fields or coverage.get('state')!='ACTIVE' or \
                coverage.get('registration_id')!=registration_id or \
                history_integer(coverage['registration_epoch'],0,2**63-1)!=epoch or \
                coverage.get('generation')!=writer['generation'] or coverage.get('writer_contract')!=HISTORY_CONTRACT or \
                coverage.get('coverage_scope')!=COVERAGE_SCOPE or \
                coverage.get('writer_manifest_sha256')!=writer['manifest_sha256'] or \
                any(not _digest(coverage.get(k)) for k in ('baseline_roster_sha256','proof_sha256')):
            raise ValueError('exact Combi registration receipt unavailable')
        version=history_integer(coverage['version']);revision=history_integer(coverage['coverage_revision'],0)
        count=history_integer(coverage['record_count'],0,MAX_MEALS)
        if (count==0 and coverage['baseline_roster_sha256']!=roster_digest([])) or \
                (count>0 and (revision==0 or coverage['baseline_roster_sha256']==roster_digest([]))) or \
                (expected is not None and coverage!=expected):
            raise ValueError('inconsistent or replaced Combi registration receipt')
        return {'coverage_version':version,'record_count':count,'receipt_only':True,'immediately_previous_verified':False}

    @staticmethod
    def _scope(owner,epoch,registration_id):
        if not valid_subject(owner) or type(epoch) is not int or not 0<=epoch<2**63 or not canonical_uuid(registration_id):
            raise ValueError('explicit trusted owner, epoch and fresh registration UUID required')

    def register(self,*,owner,auth_epoch,registration_id,max_scanned=MAX_SCAN_ITEMS):
        """Audit internally and CAS once. Never retry an uncertain registration.

        The trusted operator retains a fresh operation ID before calling. An
        unavailable recovery is not proof of a failed write or permission to
        rerun an audit with the same ID. This method cannot attest deployment.
        """
        self._scope(owner,auth_epoch,registration_id)
        if type(max_scanned) is not int or not 1<=max_scanned<=MAX_SCAN_ITEMS:raise ValueError('bounded audit required')
        try:
            before=self._controls(owner,auth_epoch);user,writer=before;old=user.get(COVERAGE_FIELD)
            if old is not None and (old.get('registration_id')==registration_id or history_integer(old['version'])>=10**18):
                raise ValueError('use exact receipt recovery, not registration replay')
            observed=self.audit.observe(owner=owner,max_scanned=max_scanned)
            if type(observed) is not CombiRosterObservation or observed.owner!=owner or self._controls(owner,auth_epoch)!=before:
                raise ValueError('Combi controls changed during retained audit')
            coverage={'state':'ACTIVE','version':history_integer(old['version'])+1 if old else 1,
                'generation':writer['generation'],'writer_contract':HISTORY_CONTRACT,'coverage_scope':COVERAGE_SCOPE,
                'coverage_revision':observed.revision,'baseline_roster_sha256':observed.roster_sha256,
                'proof_sha256':observed.proof_sha256,'writer_manifest_sha256':writer['manifest_sha256'],
                'registration_id':registration_id,'registration_epoch':auth_epoch,'record_count':observed.count}
            now=self._now()
            if not history_integer(writer['verified_at'],1,2**63-1)<=now<history_integer(writer['valid_until'],1,2**63-1):
                raise ValueError('Combi writer verification expired')
            condition='attribute_exists(pk) AND #epoch = :epoch AND (attribute_not_exists(suspended) OR suspended = :false) AND attribute_not_exists(deletion_lock_request) AND '
            condition+='attribute_not_exists(#coverage)' if old is None else '#coverage = :old'
            values={':epoch':auth_epoch,':false':False,':coverage':coverage}
            if old is not None:values[':old']=old
            history={'TableName':self.cooking,'Key':self._encode({'pk':f'SCOPE#USER#{owner}#COMBI_MEALS','sk':'HISTORY#REVISION'}),
                'ConditionExpression':'revision = :revision' if observed.revision_present else 'attribute_not_exists(pk)',
                'ReturnValuesOnConditionCheckFailure':'NONE'}
            if observed.revision_present:history['ExpressionAttributeValues']=self._encode({':revision':observed.revision})
            self.client.transact_write_items(ClientRequestToken=str(uuid4()),TransactItems=[{'Update':{
                'TableName':self.auth,'Key':self._encode({'pk':'USER#'+owner}),'UpdateExpression':'SET #coverage = :coverage',
                'ConditionExpression':condition,'ExpressionAttributeNames':{'#epoch':'epoch','#coverage':COVERAGE_FIELD},
                'ExpressionAttributeValues':self._encode(values),'ReturnValuesOnConditionCheckFailure':'NONE'}}, {'ConditionCheck':{
                'TableName':self.auth,'Key':self._encode({'pk':CONTROL_KEY}),
                'ConditionExpression':'#state = :enforced AND #version = :version AND #generation = :generation AND '
                    'writer_contract = :contract AND cooking_table_arn = :table AND manifest_sha256 = :manifest AND '
                    'verified_at = :verified AND verified_at <= :now AND valid_until = :until AND valid_until > :now',
                'ExpressionAttributeNames':{'#state':'state','#version':'version','#generation':'generation'},
                'ExpressionAttributeValues':self._encode({':enforced':'ENFORCED',':version':writer['version'],
                    ':generation':writer['generation'],':contract':HISTORY_CONTRACT,':table':self.cooking_arn,
                    ':manifest':writer['manifest_sha256'],':verified':writer['verified_at'],':until':writer['valid_until'],':now':now}),
                'ReturnValuesOnConditionCheckFailure':'NONE'}},{'ConditionCheck':history}])
            return self._receipt(owner,auth_epoch,registration_id,expected=coverage)
        except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError,BotoCoreError,ClientError):
            raise CombiHistoryCoverageUnavailable('Combi coverage registration unavailable') from None

    def recover(self,*,owner,auth_epoch,registration_id):
        """Historical receipt read only: no audit, write or prior-order permit."""
        self._scope(owner,auth_epoch,registration_id)
        try:return self._receipt(owner,auth_epoch,registration_id)
        except (ValueError,TypeError,KeyError,AttributeError,ArithmeticError,BotoCoreError,ClientError):
            raise CombiHistoryCoverageUnavailable('Combi coverage receipt unavailable') from None
