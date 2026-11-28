import { parseCandidateJobReceipt, parseCandidateJobRecovery } from "../../apps/customer-web/src/candidateCookingJob.ts";
import { parseRecipeCandidate } from "../../apps/customer-web/src/recipeCandidate.ts";
import test from 'node:test';
import assert from 'node:assert/strict';
import {parseRecipeResult} from '../../apps/customer-web/src/recipeResult.ts';
import {parseRecipeReview,parseReviewContext,parseReviewPage} from '../../apps/customer-web/src/recipeReview.ts';
const job='00000000-0000-4000-8000-000000000001';
const execution='00000000-0000-4000-8000-000000000002';
const proposal=parseRecipeResult({execution_id:execution,job_id:job,job_version:3,state:'SUCCEEDED',
  result_available:true,execution_available:false,job_changed:false,projection_status:'AVAILABLE',error_code:null,
  proposal:{status:'NEEDS_REVIEW',schema_version:'synthetic-recommendation-output-v1',guard_version:'synthetic-recommendation-guard-v1',
    menu:'JEYUK',data:{temperature_c:'200',duration_seconds:300,rpm:'60',load_kg:'7',vegetable_interval_seconds:60,sauce_interval_seconds:120}}},execution,job).proposal;
const review={schema_version:'recipe-review-v1',version:1,request_id:'00000000-0000-4000-8000-000000000003',
  execution_id:execution,job_id:job,job_version:3,proposal_digest:'a'.repeat(64),proposal,
  decision:'APPROVE',reason:'합성 조건 확인',reviewed_at:'2026-11-27T01:00:00Z'};
const context={execution_id:execution,job_id:job,job_version:3,proposal_digest:'a'.repeat(64),proposal,
  review_version:1,can_review:true,latest_review:review};

test('review context must match exact displayed proposal and current review pointer',()=>{
  assert.equal(parseReviewContext(context,execution,job,3,proposal).review_version,1);
  for(const changed of [{execution_id:job},{job_version:4},{review_version:2},{can_review:'true'},
    {proposal:{...proposal,data:{...proposal.data,temperature_c:'250'}}},{latest_review:null}])
    assert.throws(()=>parseReviewContext({...context,...changed},execution,job,3,proposal));
  const empty={...context,review_version:0,latest_review:null,can_review:false};
  assert.equal(parseReviewContext(empty,execution,job,3,proposal).can_review,false);
});

test('immutable review never renders arbitrary fields, invalid decisions or unsafe numbers',()=>{
  const parsed=parseRecipeReview(review,job);
  assert.equal(parsed.decision,'APPROVE');
  for(const changed of [{job_id:execution},{version:Number.MAX_SAFE_INTEGER+1},{decision:'APPLIED'},
    {reason:' '},{credential:'private-marker'},{reviewed_at:'not-a-date'},
    {proposal:{...proposal,status:'APPROVED'}}]) assert.throws(()=>parseRecipeReview({...review,...changed},job));
});

test('history keeps ascending versions and rejects repeated or malformed continuation',()=>{
  const page={job_id:job,current_job_version:4,items:[review],next_cursor:1};
  assert.equal(parseReviewPage(page,job,null).current_job_version,4);
  for(const changed of [{items:[],next_cursor:1},{next_cursor:2},{items:[review,review]},
    {job_id:execution},{current_job_version:true}]) assert.throws(()=>parseReviewPage({...page,...changed},job,null));
  assert.throws(()=>parseReviewPage(page,job,1));
});

const candidate={schema_version:'recipe-candidate-v1',candidate_id:`recipe-candidate-${job}-r1`,
  status:'CANDIDATE_NOT_APPLIED',provenance:'SYNTHETIC',base_recipe_version:'baseline-v1',equipment_profile:'jeyuk-medium',
  review_version:1,review_request_id:review.request_id,execution_id:execution,job_id:job,job_version:3,
  proposal_digest:review.proposal_digest,proposal,created_at:review.reviewed_at};
test('candidate stays un-applied and retains lineage after later cooking or review changes',()=>{
  const value=parseRecipeCandidate({candidate,current_job_version:4,current_review_version:2},review);
  assert.equal(value.candidate.base_recipe_version,'baseline-v1');
  assert.equal(value.candidate.status,'CANDIDATE_NOT_APPLIED');
  assert.equal(value.current_review_version,2);
});
test('candidate rejects altered content, linkage, applied state and untrusted fields',()=>{
  const payload={candidate,current_job_version:3,current_review_version:1};
  for(const changed of [{candidate_id:'other'},{status:'APPLIED'},{job_id:execution},{review_version:2},
    {base_recipe_version:' '},{equipment_profile:''},{private:'private-marker'},{created_at:'2026-12-01T00:00:00Z'},
    {proposal:{...proposal,data:{...proposal.data,temperature_c:'250'}}}])
    assert.throws(()=>parseRecipeCandidate({...payload,candidate:{...candidate,...changed}},review));
  for(const changed of [{current_job_version:2},{current_review_version:0},{current_review_version:true}])
    assert.throws(()=>parseRecipeCandidate({...payload,...changed},review));
  assert.throws(()=>parseRecipeCandidate(payload,{...review,decision:'REJECT'}));
});

const jobInput={request_id:'00000000-0000-4000-8000-000000000005',expected_job_version:3,proposal_digest:review.proposal_digest,
  planned_portions:20,thickness_cm:null,actual_load_kg:'7.00',acknowledged:true};
const selection={schema_version:'recipe-candidate-selection-v1',request_id:jobInput.request_id,request_hash:'b'.repeat(64),
  source_job_id:job,source_job_version:3,review_version:1,candidate};
const child={job_id:jobInput.request_id,recipe_version:candidate.candidate_id,provenance:'SYNTHETIC',menu:'JEYUK',equipment_profile:'jeyuk-medium',
  planned_portions:20,thickness_cm:null,actual_load_kg:'7',source_job_id:null,recook_reason:null,recipe_candidate_selection:selection};
test('new candidate job receipt is tied to the original request and candidate',()=>{
  const raw={job_id:jobInput.request_id,recipe_version:candidate.candidate_id,request_hash:'b'.repeat(64),created:true};
  assert.equal(parseCandidateJobReceipt(raw,candidate,jobInput).job_id,jobInput.request_id);
  for(const change of [{job_id:job},{recipe_version:'other'},{request_hash:'bad'},{created:'true'}])
    assert.throws(()=>parseCandidateJobReceipt({...raw,...change},candidate,jobInput));
});
test('response-loss recovery validates candidate lineage and new measurements without resubmission',()=>{
  assert.equal(parseCandidateJobRecovery({job:child},candidate,review,jobInput).job_id,jobInput.request_id);
  for(const change of [{job_id:job},{planned_portions:19},{actual_load_kg:'8'},{source_job_id:job},
    {recipe_candidate_selection:{...selection,source_job_id:execution}},
    {recipe_candidate_selection:{...selection,candidate:{...candidate,proposal:{...proposal,data:{...proposal.data,temperature_c:'250'}}}}}])
    assert.throws(()=>parseCandidateJobRecovery({job:{...child,...change}},candidate,review,jobInput));
});

test('guarded fryer review and candidate preserve an identical source contract', () => {
  const source_contract = "tonkatsu-guarded-source-v1";
  const fryerProposal = { ...proposal, menu: "TONKATSU", data: { temperature_c: "170", duration_seconds: 300 } };
  const fryer = parseRecipeReview({ ...review, proposal: fryerProposal, source_contract }, job);
  assert.equal(fryer.source_contract, source_contract);
  assert.throws(() => parseRecipeReview({ ...review, source_contract }, job));
  const fryerCandidate = { ...candidate, source_contract, proposal: fryerProposal, equipment_profile: "synthetic-fryer-approved-v2" };
  const payload = { candidate: fryerCandidate, current_job_version: 3, current_review_version: 1 };
  assert.equal(parseRecipeCandidate(payload, fryer).candidate.source_contract, source_contract);
  assert.throws(() => parseRecipeCandidate({ ...payload, candidate: { ...fryerCandidate, source_contract: "other" } }, fryer));
  const { source_contract: _, ...unmarked } = fryerCandidate;
  assert.throws(() => parseRecipeCandidate({ ...payload, candidate: unmarked }, fryer));
});
