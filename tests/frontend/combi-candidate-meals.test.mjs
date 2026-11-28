import test from 'node:test';import assert from 'node:assert/strict';import {readFileSync} from 'node:fs';
import {createElement} from 'react';import {renderToStaticMarkup} from 'react-dom/server';
import {parseCandidateMealReceipt} from '../../apps/customer-web/src/combiCandidateMeal.ts';
import {CombiCandidateMeal} from '../../apps/customer-web/src/CombiCandidateMeal.tsx';
import {CombiCandidateLoad} from '../../apps/customer-web/src/CombiCandidateLoad.tsx';
const fixture = menu=>JSON.parse(readFileSync(new URL(`../fixtures/combi-candidate-meals/${menu}.json`,import.meta.url),'utf8'));
for(const menu of ['rice','mackerel']){
 test(`${menu}: exact Python-produced adoption receipt and advanced retry can be read`,()=>{
  const f=fixture(menu);assert.equal(parseCandidateMealReceipt(f.receipt,f.candidate,f.review,f.input).created,true);
  const retry=structuredClone(f.receipt);retry.created=false;retry.meal.meal.version=9;retry.meal.meal.status='RICE_COOKING';
  assert.equal(parseCandidateMealReceipt(retry,f.candidate,f.review,f.input).created,false);
 });
 test(`${menu}: altered source, target, programme, weights and request cannot advertise creation`,()=>{
  const f=fixture(menu),change=[
   r=>r.request_id=f.candidate.meal_id,r=>r.meal_id=f.candidate.meal_id,r=>r.extra='unknown',
   r=>r.meal.meal.recipe_candidate_selection.source_meal_version=4,
   r=>r.meal.meal.recipe_candidate_selection.request_hash='b'.repeat(64),
   r=>r.meal.meal.recipe_candidate_selection.candidate.proposal.data.pan_positions[0].shelf=9,
   r=>r.meal.meal.recipe_candidate_selection.review.reason='different',
   r=>r.meal[menu].target_program_seconds=123,r=>r.meal.rice.added_water_kg='999',
   r=>r.meal.mackerel.job_id=r.meal.rice.job_id,r=>r.meal.transition.transition_id=r.meal.rice.job_id,
  ];
  for(const mutate of change){const raw=structuredClone(f.receipt);mutate(raw);assert.throws(()=>parseCandidateMealReceipt(raw,f.candidate,f.review,f.input));}
  assert.throws(()=>parseCandidateMealReceipt(f.receipt,f.candidate,f.review,{...f.input,proposal_digest:'0'.repeat(64)}));
 });
}
test('fresh input forms do not prefill source measurements or TOTP/API credentials',()=>{
 const f=fixture('rice');const html=renderToStaticMarkup(createElement(CombiCandidateMeal,{candidate:f.candidate,review:f.review,csrf:'private-csrf'}));
 for(const text of ['새 마른쌀 중량','실제 추가할 물','새 식사배치 생성','새 전처리·적재·안전 확인'])assert(html.includes(text));
 assert(!html.includes('private-csrf'));assert(!html.includes('value="2.4"'));
 const load=renderToStaticMarkup(createElement(CombiCandidateLoad,{positions:[{rack:2,shelf:2}],large:true,busy:false,onSubmit:async()=>{}}));
 for(const text of ['논리 랙 2 / 2단','비어 있어도 설치한 랙','제품 중량','두께','이번 빈 팬 총중량'])assert(load.includes(text));
 assert(!load.includes('value="0.13"'));
});
