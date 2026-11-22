// Standalone: node --test tests/optimization/test_insights.mjs (no npm install).
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFile} from 'node:fs/promises';
const js = await readFile(new URL('../../web/review-insights.js', import.meta.url), 'utf8');
const {summarizeReview: summarize, renderReviewInsights: render} = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
const task = {task_id:'t', current_version_id:'v2', result_mode:'live', versions:[{version_id:'v1', number:1, original_filename:'旧版.xlsx'}, {version_id:'v2',number:2}]};
const base = {task, versionId:'v1'};
const row = (id, data = {}) => ({task_id:'t', version_id:'v1', item_id:id, name:`商品 ${id}`, result_mode:'live',
  comparison_status:null, precheck_suggested_hs:null, business_confirmed_hs:null, submitted_hs:null, expert_final_hs:null, ...data});
const job = {task_id:'t', version_id:'v1', parse_status:'succeeded'};
const html = data => { const host = {}; render(host, data); return host.innerHTML; };

test('full-list denominator, historical scope and independent—not cumulative—stage values', () => {
  const values = [row('a',{comparison_status:'consistent', supplier_hs_raw:'ABC', expert_final_hs:'0012345678'}),
    row('b',{comparison_status:'supplier_missing', business_confirmed_hs:'0012345678'}),row('c',{submitted_hs:'0012345678'}),row('d')];
  const m=summarize({...base, items:values});
  assert.equal(m.total,4); assert.deepEqual(m.scope,{version:'V1',kind:'历史版本',file:'旧版.xlsx'});
  assert.deepEqual(m.coverage.map(x=>x.recorded),[0,1,1,1]); // explicit null is not a previous-stage fallback
  assert.deepEqual(m.distribution.map(x=>[x.label,x.count]),[['原码缺失',1],['编码一致',1],['暂未有结论',2]]);
  assert.deepEqual(m.examples.map(x=>x.id),['b']);
});

test('unknown, absent and null conclusions remain separate; each item counted once', () => {
  const statuses=['consistent','inconsistent','needs_confirmation','supplier_invalid','supplier_missing','association_error','future','__proto__',undefined,null];
  const rows=statuses.map((value,i)=>row(String(i),{comparison_status:value}));
  const m=summarize({...base,items:rows});
  assert.equal(m.distribution.reduce((n,g)=>n+g.count,0),m.total);
  assert.equal(m.distribution.length,9);
  assert.equal(m.distribution.find(g=>g.label==='结论未识别').count,2);
  assert.equal(m.distribution.find(g=>g.label==='结论字段未提供').count,1);
  assert.equal(m.distribution.find(g=>g.label==='暂未有结论').count,1);
  assert.deepEqual(m.examples.map(x=>x.id),['1','2','3']);
});

test('API null and blank are no recorded code; absent/invalid fields make the numerator unknown', () => {
  const data={...base, items:[row('a',{precheck_suggested_hs:' ', business_confirmed_hs:undefined, submitted_hs:777}),row('b',{submitted_hs:'0000000000'})]};
  const m=summarize(data); assert.deepEqual(m.coverage.map(x=>[x.recorded,x.unknown]),[[0,0],[0,1],[1,1],[0,0]]);
  const output=html(data);
  assert.match(output,/业务确认<\/span><b>未能统计/); assert.match(output,/至少 1 条/); assert.match(output,/1 条未提供该字段/);
  assert.doesNotMatch(output,/NaN|Infinity/);
});

test('empty, missing, loading, failed, parse pending and superseded are distinct, without percentages', () => {
  assert.equal(summarize({}).state,'hidden'); assert.equal(summarize({task}).state,'hidden');
  const cases=[ [{items:[]},'empty'], [{items:null},'unavailable'], [{items:[],loading:true},'loading'],
    [{items:[],loading:true,error:'network'},'unavailable'], [{items:[],importJob:{...job,parse_status:'staged',item_count:0}},'waiting'],
    [{items:[],importJob:{...job,parse_status:'failed',item_count:0}},'unavailable'], [{items:[],importJob:{...job,parse_status:'superseded'}},'empty'] ];
  for (const [args,state] of cases) {
    assert.equal(summarize({...base,...args}).state,state);
    assert.doesNotMatch(html({...base,...args}),/ri-total|<meter|\/ 0|0 条商品/);
  }
});

test('fail closed on wrong scope, duplicate identity or parsable count disagreeing with complete list', () => {
  const cases = [{items:[row('a'),row('a')]},{items:[row('a',{version_id:'v2'})]}, {items:[row('a',{task_id:null})]},
    {items:[{}]}, {items:[null]}, {items:[row('a',{item_id:' '})]}, {versionId:'no-such-version',items:[]},
    {items:[row('a')],importJob:{...job, item_count:0}}, {items:[row('a')],importJob:{...job,version_id:'v2'}},
    {items:[],run:{task_id:'wrong',version_id:'v1'}}];
  for (const data of cases) assert.equal(summarize({...base,...data}).state,'unavailable');
});

test('source mode respects selected rows or scoped run, never current task metadata for a historical version', () => {
  const data={...base,task:{...task,result_mode:'mock'},items:[row('a',{result_mode:'mock'})]};
  assert.equal(summarize(data).mock,false); // no actual precheck value or conclusion
  assert.equal(summarize({...data,items:[row('a',{result_mode:'mock',comparison_status:'consistent'})]}).mock,true);
  assert.equal(summarize({...data,run:{task_id:'t',version_id:'v1',result_mode:'mock'}}).mock,true);
  assert.equal(summarize({...data,items:[row('a',{result_mode:undefined,precheck_suggested_hs:'0000000000'})]}).unverified,true);
});

test('host cleared on logout; untrusted names, IDs, labels and original file cannot inject markup', () => {
  const untrusted='<img src=x onerror=oops()>', id='\"><svg onload=oops()>';
  const data={...base,task:{...task,versions:[{version_id:'v1',number:1,original_filename:untrusted}]},items:[row(id,{name:untrusted,comparison_status:'inconsistent',source_location:{sheet_name:untrusted,row_number:5}})]};
  const host={}; render(host,data);
  assert.match(host.innerHTML,/&lt;img/); assert.match(host.innerHTML,/data-item="&quot;&gt;&lt;svg/);
  assert.doesNotMatch(host.innerHTML,/<img|<svg/); assert.equal(host.hidden,false);
  render(host,{}); assert.equal(host.hidden,true); assert.equal(host.innerHTML,'');
});
