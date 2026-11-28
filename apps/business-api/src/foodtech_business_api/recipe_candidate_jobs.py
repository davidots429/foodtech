"""Explicit candidate selection starts a fresh job; no equipment command or safety inheritance."""
import copy
import hashlib
from decimal import Decimal
from uuid import UUID
import rfc8785
from botocore.exceptions import BotoCoreError, ClientError
from foodtech_contracts.recipe_result_view import recipe_proposal_digest
from foodtech_contracts.tonkatsu_batch import PROFILE
from foodtech_contracts.tonkatsu_transaction_conditions import TonkatsuTransactionConditions
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, UUID4, model_validator
from .cooking_store import Conflict, MemoryCookingStore, _job_list_item, _is_transaction_contention
from .recipe_review_store import RecipeReviewStore, CURRENT, PREFIX, FRYER_SOURCE_CONTRACT, _scope
from .recipe_candidate import CANDIDATE_PREFIX


class CandidateJobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID4
    expected_job_version: StrictInt = Field(ge=1, le=2**53-1)
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    planned_portions: StrictInt = Field(ge=1, le=1000)
    thickness_cm: Decimal | None = Field(default=None, gt=0, max_digits=4, decimal_places=2, allow_inf_nan=False)
    actual_load_kg: Decimal | None = Field(default=None, gt=0, max_digits=5, decimal_places=2, allow_inf_nan=False)
    acknowledged: StrictBool

    @model_validator(mode='after')
    def confirmed(self):
        if not self.acknowledged: raise ValueError('explicit new job acknowledgement required')
        return self


def request_hash(body, source_job_id, review_version):
    values = body.model_dump(mode='json')
    for name in ('thickness_cm', 'actual_load_kg'):
        value=getattr(body,name)
        values[name]=format(value.normalize(),'f') if value is not None else None
    return hashlib.sha256(rfc8785.dumps({'schema_version':'candidate-job-request-v1',
        'source_job_id':source_job_id,'review_version':review_version,'input':values})).hexdigest()


class CandidateJobStore:
    def __init__(self, store):
        self.store=store
        self.reviews=RecipeReviewStore(store)

    def _source(self, pk, review_version, candidate):
        subject, job_id=_scope(pk)
        job=self.reviews._get(pk,'META')
        pointer=self.reviews.current(pk)
        if not job or job.get('owner_subject')!=subject or job.get('job_id')!=job_id or \
                job.get('version')!=candidate['job_version'] or job.get('provenance')!='SYNTHETIC' or pointer['version']!=review_version or \
                not pointer['review'] or pointer['review']['decision']!='APPROVE' or \
                self.reviews.candidate(pk,review_version)!=candidate:
            raise Conflict
        if job.get('recipe_version')!=candidate['base_recipe_version'] or \
                job.get('equipment_profile')!=candidate['equipment_profile'] or job.get('menu')!=candidate['proposal']['menu']:
            raise Conflict
        return job,pointer['review']

    def create(self, source_pk, review_version, candidate, meta, fingerprint, expected_epoch, *, fryer_conditions=None):
        subject,source_id=_scope(source_pk)
        target=f"SCOPE#USER#{subject}#JOB#{meta['job_id']}"
        if target==source_pk:raise Conflict
        if (candidate.get("source_contract") == FRYER_SOURCE_CONTRACT) != (fryer_conditions is not None):
            raise Conflict
        if fryer_conditions is not None:
            if type(fryer_conditions) is not TonkatsuTransactionConditions or isinstance(self.store, MemoryCookingStore):
                raise ValueError("atomic fryer candidate job storage required")
            scope = fryer_conditions.scope
            if (
                fryer_conditions.auth != self.store.auth_table_name or fryer_conditions.cooking != self.store.table.name
                or scope.history.history.owner != subject or scope.binding.auth_epoch != expected_epoch
                or scope.execution_id != candidate["execution_id"] or scope.binding.current.job_id != source_id
                or scope.binding.current.job_version != candidate["job_version"]
            ):
                raise Conflict
        if meta.get('status')!='PENDING' or meta.get('safety_status')!='NOT_MEASURED' or meta.get('safety_version')!=0 or \
                meta.get('evaluation_blocked') is not True or meta.get('evaluation_status')!='NOT_COMPLETED':
            raise ValueError('fresh candidate job safety state required')
        if meta.get('owner_subject')!=subject or meta.get('recipe_version')!=candidate['candidate_id'] or \
                meta.get('recipe_candidate_selection',{}).get('request_hash')!=fingerprint:
            raise ValueError('invalid candidate job binding')
        def reconcile():
            existing=self.reviews._get(target,'META')
            if existing is None:return None
            selection=existing.get('recipe_candidate_selection')
            if existing.get('owner_subject')!=subject or existing.get('job_id')!=meta['job_id'] or \
                    existing.get('recipe_version')!=candidate['candidate_id'] or not isinstance(selection,dict) or \
                    selection.get('schema_version')!='recipe-candidate-selection-v1' or selection.get('request_id')!=meta['job_id'] or \
                    selection.get('request_hash')!=fingerprint or selection.get('candidate')!=candidate or \
                    selection.get('source_job_id')!=source_id or selection.get('source_job_version')!=candidate['job_version'] or \
                    selection.get('review_version')!=review_version:
                raise Conflict
            return False
        if isinstance(self.store,MemoryCookingStore):
            with self.store.lock:
                self._source(source_pk,review_version,candidate)
                found=reconcile()
                if found is not None:return found
                self.store.create(target,copy.deepcopy(meta),expected_epoch)
                return True
        job,review=self._source(source_pk,review_version,candidate)
        found=reconcile()
        if found is not None and fryer_conditions is None: return found
        encode=self.store._item
        def check(pk,sk,values):
            names={'#a'+str(i):k for i,k in enumerate(sorted(values))}
            args={':v'+str(i):values[k] for i,k in enumerate(sorted(values))}
            return {'ConditionCheck':{'TableName':self.store.table.name,'Key':encode({'pk':pk,'sk':sk}),
                'ConditionExpression':' AND '.join('#a'+str(i)+' = :v'+str(i) for i in range(len(values))),
                'ExpressionAttributeNames':names,'ExpressionAttributeValues':encode(args)}}
        history_update = self.store._history_update(target)
        if fryer_conditions is None:
            guards = [self.store._owner_guard(subject, expected_epoch),
                check(source_pk, 'META', {k:job[k] for k in (
                    'owner_subject','job_id','version','recipe_version','equipment_profile','menu','provenance')})]
        else:
            if (job.get("thickness_profile") != meta.get("thickness_profile")
                    or meta.get("thickness_profile") not in PROFILE
                    or meta["planned_portions"] > PROFILE[meta["thickness_profile"]][0]):
                raise Conflict
            guards = fryer_conditions.review_checks()
            if found is None:
                update = history_update["Update"]
                guard_index, roster_check = next(
                    (index, action["ConditionCheck"]) for index, action in enumerate(guards)
                    if (action["ConditionCheck"]["TableName"], action["ConditionCheck"]["Key"]) ==
                       (update["TableName"], update["Key"])
                )
                # DynamoDB permits only one action per row. Fence and increment the roster in that update.
                del guards[guard_index]
                update["ConditionExpression"] = (
                    f"({update['ConditionExpression']}) AND ({roster_check['ConditionExpression']})"
                )
                update["ExpressionAttributeNames"] = roster_check["ExpressionAttributeNames"]
                update["ExpressionAttributeValues"].update(roster_check["ExpressionAttributeValues"])
        guards.extend([
            check(source_pk, CURRENT, {'version':review_version}),
            check(source_pk, CANDIDATE_PREFIX+f'{review_version:019d}', candidate),
            check(source_pk, PREFIX+f'{review_version:019d}', review),
        ])
        if found is not None:
            actions = [*guards, check(target, "META", {
                name: meta[name] for name in ("owner_subject", "job_id", "recipe_version", "recipe_candidate_selection")
            })]
        else:
            actions = [*guards,
                {'Put':{'TableName':self.store.table.name,'Item':encode({'pk':target,'sk':'META',**meta}),
                    'ConditionExpression':'attribute_not_exists(pk)'}},
                {'Put':{'TableName':self.store.table.name,'Item':encode(_job_list_item(target,meta)),
                    'ConditionExpression':'attribute_not_exists(pk)'}}, history_update]
        for action in actions:
            next(iter(action.values()))['ReturnValuesOnConditionCheckFailure'] = 'NONE'
        try:
            self.store._history_transact(actions)
        except self.store.client.exceptions.TransactionCanceledException as error:
            if not _is_transaction_contention(error):
                raise
            reasons = error.response.get('CancellationReasons') or []
            if fryer_conditions is None and len(reasons) == len(actions) and all(
                reason.get('Code') in {None,'None'} for reason in reasons[:len(guards)]
            ):
                found = reconcile()
                if found is not None:
                    return found
            raise Conflict from None
        return found is None


async def create_candidate_job(request, source_job_id: UUID, review_version: int, body: CandidateJobRequest,
                               *, tonkatsu=False):
    from .cooking import NewJob, new_job_meta, _read, _store_call, _context
    if not 1<=review_version<=2**53-1:
        raise HTTPException(422,'후보 버전이 올바르지 않습니다.')
    before,store,pk,job,_=await _read(request,str(source_job_id),write=True)
    identity = (before.subject, before.epoch, before.csrf)
    try:
        adapter=CandidateJobStore(store)
        candidate=await _store_call(store,adapter.reviews.candidate,pk,review_version)
        if candidate is None:raise HTTPException(404,'저장된 후보 버전이 없습니다.')
        if candidate['job_version']!=body.expected_job_version or job['version']!=body.expected_job_version or \
                candidate['proposal_digest']!=body.proposal_digest:
            raise Conflict
        if (candidate.get("source_contract") == FRYER_SOURCE_CONTRACT) != tonkatsu:
            raise Conflict
        guard = {}
        if tonkatsu:
            from .ai import _recipe_record_data
            payload, current_session, current_store, _, _ = await _recipe_record_data(
                request, UUID(candidate["execution_id"]), source_job_id, result_view=True,
                tonkatsu=True, include_review_scope=True,
            )
            if identity != (current_session.subject, current_session.epoch, current_session.csrf):
                raise HTTPException(401, "로그인 상태가 변경되었습니다.", headers={"Cache-Control": "no-store"})
            if (
                current_store is not store or payload["projection_status"] != "AVAILABLE"
                or payload["job_version"] != body.expected_job_version
                or recipe_proposal_digest(payload["proposal"]) != body.proposal_digest
                or "fryer_conditions" not in payload
            ):
                raise Conflict
            guard["fryer_conditions"] = payload["fryer_conditions"]
        new=NewJob(recipe_version=candidate['candidate_id'],equipment_profile=candidate['equipment_profile'],
            planned_portions=body.planned_portions,thickness_cm=body.thickness_cm,actual_load_kg=body.actual_load_kg)
        meta=new_job_meta(identity[0],new,job_id=str(body.request_id))
        fingerprint=request_hash(body,str(source_job_id),review_version)
        meta['recipe_candidate_selection']={'schema_version':'recipe-candidate-selection-v1',
            'request_id':str(body.request_id),'request_hash':fingerprint,'source_job_id':str(source_job_id),
            'source_job_version':body.expected_job_version,'review_version':review_version,'candidate':candidate}
        created=await _store_call(store,adapter.create,pk,review_version,candidate,meta,fingerprint,identity[1], **guard)
    except Conflict:
        raise HTTPException(409,'후보·검토·작업 또는 생성 요청이 변경되었습니다. 생성 요청 ID로 작업을 조회해 주세요.',headers={'Cache-Control':'no-store'}) from None
    except (ValueError, BotoCoreError, ClientError):
        raise HTTPException(503,'후보 작업을 확인하지 못했습니다.',headers={'Cache-Control':'no-store'}) from None
    after,after_store=await _context(request)
    if after_store is not store:
        raise HTTPException(503,'저장소 구성이 변경되었습니다.',headers={'Cache-Control':'no-store'})
    if identity != (after.subject, after.epoch, after.csrf):
        raise HTTPException(401,'로그인 상태가 변경되었습니다.',headers={'Cache-Control':'no-store'})
    return JSONResponse({'job_id':meta['job_id'],'recipe_version':meta['recipe_version'],
        'request_hash':fingerprint,'created':created},status_code=201,headers={'Cache-Control':'no-store'})
