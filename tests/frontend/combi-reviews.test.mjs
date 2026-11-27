import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { parseCombiReview, parseCombiReviewContext, parseCombiReviewPage, parseCombiReviewReceipt } from '../../apps/customer-web/src/combiReview.ts';
import { CombiProposalReview } from '../../apps/customer-web/src/CombiProposalReview.tsx';
const scope={executionId:'00000000-0000-4000-8000-000000000001',mealId:'00000000-0000-4000-8000-000000000002',jobId:'00000000-0000-4000-8000-000000000003',menu:'RICE'};
const proposal={status:'NEEDS_REVIEW',schema_version:'synthetic-combi-recommendation-output-v1',guard_version:'synthetic-combi-recommendation-guard-v1',menu:'RICE',data:{temperature_c:'100',duration_seconds:1500,climate_setpoint_percent:'100',fan_level:4,pan_positions:[{rack:1,shelf:1}],effective_water_ratio:'1.20',soak_seconds:1800,drain_seconds:300}};
const review={schema_version:'combi-recipe-review-v1',version:1,request_id:'00000000-0000-4000-8000-000000000004',execution_id:scope.executionId,meal_id:scope.mealId,menu:scope.menu,job_id:scope.jobId,job_version:3,proposal_digest:'a'.repeat(64),proposal,decision:'APPROVE',reason:'합성 조건 검토',reviewed_at:'2026-11-28T01:00:00Z'};
const context={execution_id:scope.executionId,meal_id:scope.mealId,menu:scope.menu,job_id:scope.jobId,job_version:3,proposal_digest:review.proposal_digest,proposal,review_version:1,latest_review:review,can_review:true};

test('review context matches displayed meal, menu, job, version and exact proposal',()=>{
  assert.equal(parseCombiReviewContext(context,scope,3,proposal).review_version,1);
  for(const change of [{meal_id:scope.jobId},{menu:'MACKEREL'},{job_id:scope.mealId},{execution_id:scope.jobId},
    {job_version:4},{can_review:'true'},{proposal_digest:'bad'},{review_version:2},{latest_review:null},
    {proposal:{...proposal,data:{...proposal.data,pan_positions:[{rack:1,shelf:2}]}}}])
    assert.throws(()=>parseCombiReviewContext({...context,...change},scope,3,proposal));
  assert.equal(parseCombiReviewContext({...context,review_version:0,latest_review:null,can_review:false},scope,3,proposal).can_review,false);
});

test('review parser rejects arbitrary data and unearned application/safety states',()=>{
  const parsed=parseCombiReview(review,scope);
  assert.equal(parsed.schema_version,'combi-recipe-review-v1');assert.equal(parsed.decision,'APPROVE');
  for(const change of [{schema_version:'recipe-review-v1'},{decision:'APPLIED'},{meal_id:scope.jobId},{menu:'MACKEREL'},
    {credential:'private-marker'},{reason:' '},{reason:' 앞 공백'},{version:true},{job_version:2**53},
    {reviewed_at:'2026-11-28'},{proposal:{...proposal,status:'APPROVED'}}])assert.throws(()=>parseCombiReview({...review,...change},scope));
  const raw=structuredClone(review);const copied=parseCombiReview(raw,scope);raw.proposal.data.pan_positions[0].shelf=2;
  assert.equal(copied.proposal.data.pan_positions[0].shelf,1);
});

test('menu history is bounded, advancing and preserves prior execution lineage',()=>{
  const second={...review,version:2,execution_id:review.request_id,decision:'REJECT'};
  assert.equal(parseCombiReviewPage({items:[review,second],next_cursor:2},scope,null).items[1].execution_id,review.request_id);
  for(const raw of [{items:[review,review],next_cursor:1},{items:[],next_cursor:1},
    {items:[review],next_cursor:2},{items:Array(51).fill(review),next_cursor:null},
    {items:[{...review,menu:'MACKEREL'}],next_cursor:null}])assert.throws(()=>parseCombiReviewPage(raw,scope,null));
  assert.throws(()=>parseCombiReviewPage({items:[review],next_cursor:null},scope,1));
});

test('rendered review controls explain the meaning of approval and expose history lookup',()=>{
  const html=renderToStaticMarkup(createElement(CombiProposalReview,{scope,version:3,proposal,csrf:'fixture'}));
  for(const label of ['권고 내용 검토','현재 검토 기록 확인','검토 이력 조회','안전 합격 판정이 아닙니다'])assert(html.includes(label),label);
  assert(!html.includes('장비 적용'));assert(!html.includes('csrf'));assert(!html.includes('fixture'));
});


test('save receipt matches the exact request and cannot grant adoption or hide altered proposal',()=>{
  const initial={review_version:0,latest_review:null,can_review:true,proposal_digest:review.proposal_digest,proposal};
  const request={request_id:review.request_id,decision:review.decision,reason:review.reason};
  const receipt={review,adoption_available:false};
  assert.equal(parseCombiReviewReceipt(receipt,scope,3,initial,request).version,1);
  for(const raw of [null,[],{...receipt,adoption_available:true},{...receipt,private:'marker'},
    {...receipt,review:{...review,request_id:scope.mealId}},{...receipt,review:{...review,decision:'REJECT'}},
    {...receipt,review:{...review,version:2}},{...receipt,review:{...review,proposal_digest:'b'.repeat(64)}}])
    assert.throws(()=>parseCombiReviewReceipt(raw,scope,3,initial,request));
});
