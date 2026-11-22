// Exercise the actual production controller with manually ordered network completions.
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {renderReviewInsights} from '../../web/review-insights.js';

const source = readFileSync(new URL('../../web/production.js', import.meta.url), 'utf8').replace(/^import .*;\r?\n/gm, '');
const flush = () => new Promise(ok => setImmediate(ok));
function gate() {let resolve, reject; const promise = new Promise((ok, no) => {resolve = ok; reject = no;}); return {resolve, reject, promise};}
const task = id => ({task_id:id, title:`ＴＯＯＬ <${id}>`, supplier:'东海', stage:'submitted', result_mode:'live',
  versions:[{version_id:`v${id}`,number:1}], current_version_id:`v${id}`, allowed_actions:['read','decision.resolve']});
const page = (items, next_cursor = null) => ({items, next_cursor});
const item = (id, name) => ({item_id:name, task_id:id,version_id:`v${id}`,name, source_location:{row_number:1,sheet_name:'表'}});
function card(id, name) {return {...item(id,name), facts:{name}, images:[],issues:[],
  precheck_suggested_hs:{}, business_confirmed_hs:{}, submitted_hs:{}, expert_final_hs:{}};}
async function harness(overrides = {}, search = '') {
  const elements = new Map(), delegated = {}, redirected = [], recorded = [], timers = new Map(); let seq=0;
  const el = selector => {
    if (selector === '#decision-form') return null;
    if (!elements.has(selector)) elements.set(selector,{innerHTML:'', textContent:'', attrs:{}, handlers:{},
      addEventListener(k,fn){this.handlers[k]=fn;},setAttribute(k,v){this.attrs[k]=v;},focus(){this.focused=true;},reset(){}});
    return elements.get(selector);
  };
  const api = {me: async () => ({role:'expert',id:'reader'}), listTasks:async()=>page([]), getTask:async id=>task(id),
    listItems:async()=>page([]), getImport:async()=>{throw {status:404};}, logout:async()=>{},
    taskHistory:async id=>({task_id:id,versions:[{version_id:`v${id}`,confirmations:[],runs:[],submissions:[]}],audit_events:[]}), ...overrides};
  const ctx = vm.createContext({createClient:()=>api,renderReviewInsights,URLSearchParams,crypto:{randomUUID},
    history:{replaceState:(_,__,p)=>recorded.push(p)},location:{search,replace:p=>redirected.push(p)},
    setTimeout(fn){timers.set(++seq,fn); return seq;},clearTimeout(id){timers.delete(id);},
    document:{querySelector:el,addEventListener:(name,fn)=>{delegated[name]=fn;}}});
  vm.runInContext(`${source}\nglobalThis.h={state,loadTasks,openTask,openItem,fail};`,ctx);
  await flush();
  return {...ctx.h,api,el,redirected,recorded,timers,
    event:(selector,name,value)=>el(selector).handlers[name]({target:{value}}),
    click:dataset=>delegated.click({target:{closest:()=>({dataset})},preventDefault(){}})};
}

test('keyword matches loaded, authorized pages only; NFKC tokens, escaping, duplicate rows and cursor', async()=>{
  const calls=[]; const h=await harness({listTasks:async params=>{calls.push(params);return params.cursor ? page([task('B'),task('C')]) : page([task('A'),task('B')],'p2');}});
  h.event('#task-search','input','tool 东海 C');
  assert.match(h.el('#task-list').innerHTML,/没有匹配/);
  assert.match(h.el('#task-count').textContent,/已加载 2 项 · 匹配 0 项 · 还有下一页/);
  assert.equal(calls.length,1);
  h.event('#more-tasks','click'); await flush();
  assert.equal(calls[1].cursor,'p2'); assert.match(h.el('#task-count').textContent,/已加载 3 项 · 匹配 1 项/);
  assert.match(h.el('#task-list').innerHTML,/&lt;C&gt;/); assert.equal(h.el('#more-tasks').hidden,true);
  h.event('#task-reset','click'); assert.equal(h.el('#task-reset').disabled,true);assert.equal(h.el('#task-search').focused,true);
});

test('whole-queue stage filtering starts at page 1; delayed append cannot replace a newer query',async()=>{
  const old=gate(), calls=[]; const h=await harness({listTasks:async p=>{calls.push(p);return p.stage ? page([task('approved-only')]) : p.cursor ? old.promise : page([task('X')],'old-next');}});
  h.event('#more-tasks','click');h.event('#task-stage','change','approved');await flush();
  old.resolve(page([task('leak')]));await flush();
  assert.equal(calls.at(-1).stage,'approved');assert.equal(calls.at(-1).cursor,null);
  assert.match(h.el('#task-list').innerHTML,/approved-only/);assert.doesNotMatch(h.el('#task-list').innerHTML,/leak|<X>/);
});

test('latest delegated task click wins even over a stale failure; version insights receive all pages',async()=>{
  const old=gate(); const h=await harness({getTask:async id=>id==='A'?old.promise:task(id),listItems:async(v,{cursor})=>cursor?page([item('B','b2')]):page([item('B','b1')],'p2')});
  h.click({task:'A'});h.click({task:'B'});await flush();
  old.reject(new Error('obsolete A'));await flush();
  assert.equal(h.state.task.task_id,'B');assert.equal(h.recorded.at(-1),'/?task=B&version=vB');assert.equal(h.el('#task-detail').focused,true);
  assert.match(h.el('#review-insights').innerHTML,/2<small>条商品/);assert.doesNotMatch(h.el('#notice').textContent,/obsolete/);
});

test('late card within same version never replaces newer item or focus; wrong card rejected',async()=>{
  const old=gate();const h=await harness({getItem:async(v,id)=>id==='i1'?old.promise:card('B','i2')});
  await h.openTask('B');h.click({item:'i1'});h.click({item:'i2'});await flush();old.resolve(card('B','i1'));await flush();
  assert.equal(h.state.selectedItemId,'i2');assert.equal(h.el('#item-detail').focused,true);
  h.api.getItem=async()=>card('wrong','i3');await assert.rejects(h.openItem('i3'),/归属不匹配/);assert.equal(h.el('#item-detail').innerHTML,'');
});

test('duplicate cursor fails closed; clears previous items, history and insights rather than showing stale counts',async()=>{
  const h=await harness({listItems:async()=>page([item('B','item')])});await h.openTask('B');
  h.api.listItems=async()=>page([item('B','item')],'loop');await assert.rejects(h.openTask('B'),/未完整加载/);
  assert.equal(h.state.reviewLoading,true);assert.equal(h.el('#history-area').innerHTML,'');assert.equal(h.el('#item-list').innerHTML,'');
  assert.match(h.el('#review-insights').innerHTML,/未能取得完整明细/); assert.match(h.el('#detail-status').textContent,/刷新/);
});

test('permission missing: no expert form. Foreign import poll fails closed and is never displayed',async()=>{
  const h=await harness({getTask:async id=>({...task(id),allowed_actions:undefined}),
    taskHistory:async id=>({task_id:id,versions:[{version_id:`v${id}`,runs:[],submissions:[{submission_id:'s',status:'pending',items:[]}]}],audit_events:[]}),
    getImport:async()=>({task_id:'A',version_id:'vA',parse_status:'staged'})});
  await h.openTask('A');assert.doesNotMatch(h.el('#task-detail').innerHTML,/decision-form/);
  h.api.getImport=async()=>({task_id:'foreign',version_id:'vA',parse_status:'succeeded'});
  await [...h.timers.values()][0]();assert.equal(h.state.importJob,null);assert.match(h.el('#notice').textContent,/解析记录归属不匹配/);
});

test('anonymous or expired sessions preserve only task/version; explicit logout drops all private state and links',async()=>{
  const anon=await harness({me:async()=>null},'?task=A&version=vA&next=https://other.test/');assert.deepEqual(anon.redirected,['/login/?task=A&version=vA']);
  const h=await harness();await h.openTask('B');h.fail({status:401,message:'已过期'});assert.equal(h.redirected.at(-1),'/login/?task=B&version=vB');
  assert.equal(h.state.task,null);assert.equal(h.el('#task-detail').innerHTML,'');
  const out=await harness();await out.openTask('B');out.event('#logout','click');await flush();assert.deepEqual(out.redirected,['/login/']);assert.equal(out.state.me,null);
});
