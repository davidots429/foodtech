import test from 'node:test';
import assert from 'node:assert/strict';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { parseCombiStatus, parseCombiResult, combiRecordPath, readCombiRecord } from '../../apps/customer-web/src/combiRecipeResult.ts';
import { CombiRecommendation, CombiProposalDetails } from '../../apps/customer-web/src/CombiRecommendation.tsx';
const scope={executionId:'00000000-0000-4000-8000-000000000001',mealId:'00000000-0000-4000-8000-000000000002',
  jobId:'00000000-0000-4000-8000-000000000003',menu:'RICE'};
const pending={execution_id:scope.executionId,job_id:scope.jobId,meal_id:scope.mealId,menu:'RICE',job_version:3,
  state:'PENDING',result_available:false,execution_available:false,manual_invocation_enabled:false,job_changed:false};
const proposal={status:'NEEDS_REVIEW',schema_version:'synthetic-combi-recommendation-output-v1',
  guard_version:'synthetic-combi-recommendation-guard-v1',menu:'RICE',data:{temperature_c:'100',duration_seconds:1500,
    climate_setpoint_percent:'100',fan_level:4,pan_positions:[{rack:1,shelf:1}],effective_water_ratio:'1.20',soak_seconds:1800,drain_seconds:300}};
const result={...pending,state:'SUCCEEDED',result_available:true,projection_status:'AVAILABLE',error_code:null,proposal};

test('exact meal/menu/job/execution scope and read-only flags are required',()=>{
  assert.equal(parseCombiStatus(pending,scope).menu,'RICE');
  for(const changed of [{meal_id:scope.jobId},{menu:'MACKEREL'},{job_id:scope.mealId},{execution_id:scope.jobId},
    {execution_available:true},{manual_invocation_enabled:true},{job_version:2**53},{result_available:true}])
    assert.throws(()=>parseCombiStatus({...pending,...changed},scope));
  assert.throws(()=>combiRecordPath({...scope,executionId:'../foreign'},'result'));
  assert.throws(()=>combiRecordPath(scope,'invoke'));
  assert.match(combiRecordPath(scope,'result'),new RegExp(`/result\\?meal_id=${scope.mealId}&menu=RICE$`));
});

test('bounded rice proposal strips private top-level output and deep copies pan layout',()=>{
  const raw=structuredClone({...result,response_text:'private-marker',credential:'private-marker'});
  const parsed=parseCombiResult(raw,scope);
  assert.equal(parsed.proposal.status,'NEEDS_REVIEW');
  assert.equal(JSON.stringify(parsed).includes('private-marker'),false);
  raw.proposal.data.pan_positions[0].shelf=2;
  assert.equal(parsed.proposal.data.pan_positions[0].shelf,1);
  for(const changed of [{menu:'MACKEREL'},{status:'APPROVED'},{schema_version:'synthetic-recommendation-output-v1'},
    {guard_version:'future'},{secret:'private-marker'}])
    assert.throws(()=>parseCombiResult({...result,proposal:{...proposal,...changed}},scope));
});

test('mackerel exposes transition settings without rice-only fields',()=>{
  const data={temperature_c:'220',duration_seconds:700,climate_setpoint_percent:'10',fan_level:3,
    pan_positions:[{rack:1,shelf:2}],transition_duration_seconds:180};
  const raw={...result,menu:'MACKEREL',proposal:{...proposal,menu:'MACKEREL',data}};
  assert.equal(parseCombiResult(raw,{...scope,menu:'MACKEREL'}).proposal.data.transition_duration_seconds,180);
  assert.throws(()=>parseCombiResult(raw,scope));
  assert.throws(()=>parseCombiResult({...raw,proposal:{...raw.proposal,data:{...data,soak_seconds:1800}}},{...scope,menu:'MACKEREL'}));
});

test('invalid numbers, nested fields and oversized/duplicate layouts are rejected',()=>{
  for(const [field,value] of [['temperature_c','NaN'],['temperature_c','100<script>'],['effective_water_ratio','1E9999'],
    ['duration_seconds',true],['fan_level',1.5],['fan_level',2**31],['pan_positions',[]],
    ['pan_positions',[{rack:1,shelf:0}]],['pan_positions',[{rack:true,shelf:1}]],
    ['pan_positions',[{rack:1,shelf:1,private:'marker'}]],['pan_positions',[{rack:1,shelf:1},{rack:1,shelf:1}]],
    ['pan_positions',Array.from({length:21},(_,i)=>({rack:1,shelf:i+1}))],['credential','private-marker']])
    assert.throws(()=>parseCombiResult({...result,proposal:{...proposal,data:{...proposal.data,[field]:value}}},scope));
});

test('failure/unavailable states carry no provider message or arbitrary candidate',()=>{
  for(const [state,projection_status,error_code] of [['PENDING','NOT_READY',null],['RESERVED','NOT_READY',null],
    ['FAILED','FAILED','RECIPE_EXECUTION_FAILED'],['SUCCEEDED','UNAVAILABLE','RECIPE_RESULT_UNAVAILABLE']]) {
    const raw={...pending,state,result_available:['FAILED','SUCCEEDED'].includes(state),projection_status,error_code,proposal:null};
    assert.equal(parseCombiResult(raw,scope).projection_status,projection_status);
    assert.throws(()=>parseCombiResult({...raw,proposal},scope));
    assert.throws(()=>parseCombiResult({...raw,error_code:'private-marker'},scope));
  }
});

test('read orchestration only requests status and stored result; stale generations stop',async()=>{
  const paths=[];
  const saved=await readCombiRecord(scope,async path=>{paths.push(path);return path.includes('/status?')?result:result;});
  assert.equal(saved.result.proposal.menu,'RICE');
  assert.equal(paths.length,2);assert(paths.every(p=>!p.includes('invoke')&&!p.includes('source')));
  let current=true;let count=0;
  assert.equal(await readCombiRecord(scope,async()=>{count++;current=false;return result;},()=>current),null);
  assert.equal(count,1);
  count=0;current=true;
  assert.equal(await readCombiRecord(scope,async()=>{count++;if(count===2)current=false;return result;},()=>current),null);
  assert.equal(count,2);
  assert.equal((await readCombiRecord(scope,async()=>pending)).result,null);
  await assert.rejects(()=>readCombiRecord(scope,async()=>({...result,meal_id:scope.jobId})));
});

test('rendered controls and proposal labels communicate read-only review and units',()=>{
  const controls=renderToStaticMarkup(createElement(CombiRecommendation,{mealId:scope.mealId,jobId:scope.jobId,menu:'RICE'}));
  assert.match(controls,/밥 AI 권고 기록/);assert.match(controls,/권고 실행 ID/);assert.match(controls,/안전 확인/);
  assert.doesNotMatch(controls,/>실행<|>적용<|>승인</);
  const html=renderToStaticMarkup(createElement(CombiProposalDetails,{proposal:parseCombiResult(result,scope).proposal}));
  for(const label of ['100 °C','1500초','상대 설정값','4단계','1800초','300초','권장 팬 배치','운영자 검토 필요'])
    assert(html.includes(label),label);
  assert(!html.includes('response_text'));
});
