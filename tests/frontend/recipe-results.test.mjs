import test from 'node:test';
import assert from 'node:assert/strict';
import { parseRecipeStatus, parseRecipeResult, canInvokeRecipe } from '../../apps/customer-web/src/recipeResult.ts';
import { api, ApiError, postWithoutBody } from '../../apps/customer-web/src/cookingApi.ts';

const id='00000000-0000-4000-8000-000000000001';
const job='00000000-0000-4000-8000-000000000002';
const status={execution_id:id,job_id:job,job_version:3,state:'PENDING',result_available:false,
  execution_available:false,job_changed:false,manual_invocation_enabled:true};
const proposal={status:'NEEDS_REVIEW',schema_version:'synthetic-recommendation-output-v1',
  guard_version:'synthetic-recommendation-guard-v1',menu:'JEYUK',data:{temperature_c:'200',rpm:'60',
    duration_seconds:300,load_kg:'7',vegetable_interval_seconds:60,sauce_interval_seconds:120}};
const result={...status,state:'SUCCEEDED',result_available:true,projection_status:'AVAILABLE',proposal,error_code:null};

test('only same-job pending manual requests are offered; status is not approval',()=>{
  const pending=parseRecipeStatus(status,id,job);
  assert.equal(canInvokeRecipe(pending,id,job,'JEYUK'),true);
  for(const changed of [{state:'RESERVED'},{state:'SUCCEEDED',result_available:true},{state:'FAILED',result_available:true},
    {job_changed:true},{manual_invocation_enabled:false}])
    assert.equal(canInvokeRecipe(parseRecipeStatus({...status,...changed},id,job),id,job,'JEYUK'),false);
  assert.equal(canInvokeRecipe(pending,'foreign',job,'JEYUK'),false);
  assert.equal(canInvokeRecipe(pending,id,'foreign','JEYUK'),false);
  assert.equal(canInvokeRecipe(pending,id,job,'TONKATSU'),false);
  assert.equal(parseRecipeStatus({...status,manual_invocation_enabled:undefined},id,job).manual_invocation_enabled,false);
  for(const changed of [{execution_available:true},{job_id:'foreign'},{execution_id:'foreign'},
    {job_version:Number.MAX_SAFE_INTEGER+1},{manual_invocation_enabled:'true'},{state:'SUCCEEDED'}])
    assert.throws(()=>parseRecipeStatus({...status,...changed},id,job));
});

test('proposal projection exposes only expected numeric fields and review state',()=>{
  const parsed=parseRecipeResult({...result,response_text:'private-marker',credential:'private-marker'},id,job);
  assert.equal(parsed.proposal.status,'NEEDS_REVIEW');
  assert.equal(parsed.proposal.menu,'JEYUK');
  assert.equal(JSON.stringify(parsed).includes('private-marker'),false);
  assert.equal(canInvokeRecipe(parsed,id,job,'JEYUK'),false);
  for(const p of [{...proposal,status:'APPROVED'},{...proposal,menu:'TONKATSU'},
    {...proposal,data:{...proposal.data,credential:'private-marker'}},
    {...proposal,data:{...proposal.data,rpm:'Infinity'}},{...proposal,data:{...proposal.data,temperature_c:'<script>'}},
    {...proposal,data:{...proposal.data,duration_seconds:true}},{...proposal,guard_version:'unknown'}])
    assert.throws(()=>parseRecipeResult({...result,proposal:p},id,job));
  const fishless={...proposal,menu:'TONKATSU',data:{temperature_c:'170.00',duration_seconds:300}};
  assert.equal(parseRecipeResult({...result,proposal:fishless},id,job).proposal.menu,'TONKATSU');
});

test('unfinished, failed and unavailable results cannot carry an arbitrary body',()=>{
  for(const [state,projection,error]of[['PENDING','NOT_READY',null],['RESERVED','NOT_READY',null],
    ['FAILED','FAILED','RECIPE_EXECUTION_FAILED'],['SUCCEEDED','UNAVAILABLE','RECIPE_RESULT_UNAVAILABLE']]){
    const payload={...status,state,result_available:['SUCCEEDED','FAILED'].includes(state),projection_status:projection,
      error_code:error,proposal:null};
    assert.equal(parseRecipeResult(payload,id,job).proposal,null);
    assert.throws(()=>parseRecipeResult({...payload,proposal:{private:'marker'}},id,job));
  }
});

test('empty POST carries CSRF and no JSON body; existing GET and JSON POST remain unchanged',async t=>{
  const original=globalThis.fetch;
  t.after(()=>{globalThis.fetch=original;});
  const calls=[];
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return new Response(JSON.stringify({ok:true}),{status:200});};
  await postWithoutBody('/api/customer/ai/recipe-executions/'+id+'/invoke','csrf');
  await api('/read');
  await api('/existing','csrf',{value:1});
  assert.equal(calls.length,3);
  assert.equal(calls[0].options.method,'POST');
  assert.equal(calls[0].options.body,undefined);
  assert.equal(calls[0].options.headers['X-CSRF-Token'],'csrf');
  assert.equal(calls[0].options.headers['Content-Type'],undefined);
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(calls[1].options.method,'GET');
  assert.equal(calls[2].options.method,'POST');
  assert.equal(calls[2].options.body,'{"value":1}');
  assert.equal(calls[2].options.headers['Content-Type'],'application/json');
});

test('ambiguous transport failure sends once and does not expose the thrown detail',async t=>{
  const original=globalThis.fetch;
  t.after(()=>{globalThis.fetch=original;});
  let calls=0;
  globalThis.fetch=async()=>{calls++;throw new Error('private-marker');};
  await assert.rejects(postWithoutBody('/invoke','csrf'),error=>error instanceof ApiError&&error.status===0&&!error.message.includes('private-marker'));
  assert.equal(calls,1);
});
