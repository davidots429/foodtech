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
