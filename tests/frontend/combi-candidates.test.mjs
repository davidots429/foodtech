import test from 'node:test';import assert from 'node:assert/strict';
import {createElement} from 'react';import {renderToStaticMarkup} from 'react-dom/server';
import {parseCombiCandidate,parseCombiCandidateReceipt} from '../../apps/customer-web/src/combiCandidate.ts';
import {CombiCandidateView} from '../../apps/customer-web/src/CombiCandidateView.tsx';
const scope={executionId:'00000000-0000-4000-8000-000000000001',mealId:'00000000-0000-4000-8000-000000000002',jobId:'00000000-0000-4000-8000-000000000003',menu:'RICE'};
const proposal={status:'NEEDS_REVIEW',schema_version:'synthetic-combi-recommendation-output-v1',guard_version:'synthetic-combi-recommendation-guard-v1',menu:'RICE',data:{temperature_c:'100',duration_seconds:1800,climate_setpoint_percent:'100',fan_level:4,pan_positions:[{rack:1,shelf:1}],effective_water_ratio:'1.20',soak_seconds:1800,drain_seconds:300}};
const review={schema_version:'combi-recipe-review-v1',version:1,request_id:'00000000-0000-4000-8000-000000000004',execution_id:scope.executionId,meal_id:scope.mealId,menu:'RICE',job_id:scope.jobId,job_version:3,proposal_digest:'a'.repeat(64),proposal,decision:'APPROVE',reason:'합성 검토',reviewed_at:'2026-11-28T01:00:00Z'};
const candidate={schema_version:'combi-recipe-candidate-v1',candidate_id:`combi-${scope.mealId}-rice-r1`,status:'CANDIDATE_NOT_APPLIED',provenance:'SYNTHETIC',base_recipe_version:null,equipment_size:'MEDIUM_FIXED_RACK_10',meal_id:scope.mealId,menu:'RICE',job_id:scope.jobId,job_version:3,execution_id:scope.executionId,review_version:1,review_request_id:review.request_id,proposal_digest:review.proposal_digest,proposal,reviewed_at:review.reviewed_at,recorded_at:'2026-11-28T02:00:00Z'};
const receipt={candidate,current_meal_version:3,current_review_version:1,adoption_available:false};

test('candidate preserves exact approved review, copied programme and unknown base label',()=>{
 const raw=structuredClone(candidate),value=parseCombiCandidate(raw,scope,review);
 assert.equal(value.status,'CANDIDATE_NOT_APPLIED');assert.equal(value.base_recipe_version,null);
 raw.proposal.data.pan_positions[0].shelf=2;assert.equal(value.proposal.data.pan_positions[0].shelf,1);
 assert.equal(parseCombiCandidate({...candidate,base_recipe_version:'baseline-v1'},scope,review).base_recipe_version,'baseline-v1');
});
test('changed lineage, fields, programme, approval and timestamp cannot become a candidate',()=>{
 for(const change of [{candidate_id:'foreign'},{status:'APPLIED'},{provenance:'REAL'},{menu:'MACKEREL'},
  {meal_id:scope.jobId},{job_id:scope.mealId},{execution_id:review.request_id},{review_version:2},{review_request_id:scope.jobId},
  {job_version:4},{proposal_digest:'b'.repeat(64)},{credential:'private-marker'},{base_recipe_version:' '},
  {equipment_size:'UNKNOWN'},{recorded_at:'2026-11-28'},{recorded_at:'2026-11-28T00:00:00Z'},
  {proposal:{...proposal,data:{...proposal.data,pan_positions:[{rack:1,shelf:2}]}}}])
  assert.throws(()=>parseCombiCandidate({...candidate,...change},scope,review));
 assert.throws(()=>parseCombiCandidate(candidate,scope,{...review,decision:'REJECT'}));
});
test('historical candidate receipt stays unapplied and cannot advertise adoption',()=>{
 const parsed=parseCombiCandidateReceipt({...receipt,current_meal_version:4,current_review_version:2},scope,review);
 assert.equal(parsed.candidate.review_version,1);assert.equal(parsed.adoption_available,false);
 for(const change of [{adoption_available:true},{current_review_version:0},{current_meal_version:true},{secret:'private-marker'}])
  assert.throws(()=>parseCombiCandidateReceipt({...receipt,...change},scope,review));
});
test('candidate controls separate explicit preservation from application and rejected records',()=>{
 const html=renderToStaticMarkup(createElement(CombiCandidateView,{scope,review,csrf:'fixture',canPreserve:true}));
 for(const phrase of ['미적용 레시피 후보','이 검토의 후보 조회','후보 버전 보존','현재 식사배치·기존 레시피·안전 판정은 바꾸지 않습니다'])assert(html.includes(phrase));
 assert(!html.includes('장비 적용'));assert(!html.includes('fixture'));
 const history=renderToStaticMarkup(createElement(CombiCandidateView,{scope,review,csrf:'fixture'}));
 assert(!history.includes('후보 버전 보존'));assert(history.includes('이 검토의 후보 조회'));
 assert.equal(renderToStaticMarkup(createElement(CombiCandidateView,{scope,review:{...review,decision:'REJECT'},csrf:'fixture'})),'');
});
