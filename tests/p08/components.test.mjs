import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createTaskList, errorView, TASK_STAGES} from '../../web/components/task-list.js';
import {createRunProgress, progressView} from '../../web/components/run-progress.js';

function host() {
  const listeners = new Map();
  return {innerHTML: '', addEventListener(k, v) {listeners.set(k, v);},
    removeEventListener(k, v) {if (listeners.get(k) === v) listeners.delete(k);},
    replaceChildren() {this.innerHTML = '';}, contains: () => true, listeners};
}
const fixture = id => JSON.parse(readFileSync(new URL(`../../contracts/fixtures/${id}.json`, import.meta.url)));
const dataset = JSON.parse(readFileSync(new URL('../../contracts/fixtures/DEMO-SET.json', import.meta.url)));
const copy = value => structuredClone(value);
const deferred = () => {let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject};};
function scheduler() {
  let id = 0; const pending = new Map();
  return {pending, setTimeout(fn) {pending.set(++id, fn); return id;}, clearTimeout(key) {pending.delete(key);},
    async tick() {const entries = [...pending.values()]; pending.clear(); for (const fn of entries) await fn(); await new Promise(resolve => setImmediate(resolve));}};
}
function run(overrides = {}) {return {...copy(fixture('FIX-05').run), ...overrides};}
function context(value) {return {taskId: value.task_id, versionId: value.version_id, runId: value.run_id, isVersionCurrent: true, versionNumber: 2};}
function activeRun(overrides = {}) {
  const value = run(); const first = value.items[0];
  return {...value, status: 'running', progress: {total: 1,queued: 0,running: 1,succeeded: 0,failed: 0},
    items: [{...first,status: 'running',comparison_status: null}], ...overrides};
}

test('stage values match the published contract without new business states', () => {
  const schema = JSON.parse(readFileSync(new URL('../../contracts/schemas/tasks/TaskListEntry.json',import.meta.url)));
  assert.deepEqual(Object.keys(TASK_STAGES).sort(), schema.$defs.TaskStage.enum.sort());
});
test('server stage and opaque pagination cursor are forwarded and previous returns to page one', async () => {
  const requests=[]; const tasks=dataset.tasks;
  const controller=createTaskList(host(),{pageSize: 1,client:{getTask:async()=>{},listTasks:async p=>{
    requests.push(p);return {items:[tasks[p.cursor ? 1:0]],next_cursor:p.cursor ? null:'opaque-next'};
  }}});
  await controller.setStage('submitted'); await controller.next(); await controller.previous();
  assert.deepEqual(requests.map(p=>[p.stage,p.limit,p.cursor]),[['submitted',1,null],['submitted',1,'opaque-next'],['submitted',1,null]]);
  assert.equal(controller.getState().page,0);controller.destroy();
});
test('late stage response and late errors cannot overwrite the new filter',async()=>{
  const first=deferred(),second=deferred();let n=0;
  const controller=createTaskList(host(),{client:{getTask:async()=>{},listTasks:()=>++n===1 ? first.promise:second.promise}});
  const a=controller.setStage('prechecking'),b=controller.setStage('submitted');
  second.resolve({items:[dataset.tasks[0]],next_cursor:null});await b;
  first.reject(new Error('old error'));await a;
  assert.equal(controller.getState().stage,'submitted');assert.equal(controller.getState().error,null);controller.destroy();
});
test('FIX-07 empty data differs from unavailable or forbidden task lists',async()=>{
  const node=host();let fail=false;
  const controller=createTaskList(node,{client:{getTask:async()=>{},listTasks:async()=>{
    if(fail)throw {status:403,message:'不允许读取'};return fixture('FIX-07').task_page;
  }}});
  await controller.load();assert.match(node.innerHTML,/暂无可见任务/);
  fail=true;await controller.load();assert.match(node.innerHTML,/当前操作不允许/);assert.doesNotMatch(node.innerHTML,/暂无可见任务/);controller.destroy();
});
test('search is explicitly limited to current page; pagination survives zero matches',async()=>{
  const node=host(),controller=createTaskList(node,{client:{getTask:async()=>{},listTasks:async()=>({items:dataset.tasks,next_cursor:'next'})}});
  await controller.load();controller.setQuery('不存在的关键词');
  assert.match(node.innerHTML,/本页没有匹配任务/);assert.match(node.innerHTML,/搜索仅覆盖本页/);
  assert.equal(controller.getState().nextCursor,'next');controller.destroy();
});
test('task detail late response and duplicate click are suppressed',async()=>{
  const old=deferred();let calls=0;const selected=[];
  const controller=createTaskList(host(),{client:{listTasks:async()=>({items:dataset.tasks,next_cursor:null}),getTask:async id=>{
    calls++;return id===dataset.tasks[0].task_id?old.promise:dataset.tasks[1];
  }},onSelectTask:detail=>selected.push(detail.task_id)});
  await controller.load();const first=controller.selectTask(dataset.tasks[0].task_id);
  await controller.selectTask(dataset.tasks[0].task_id);
  await controller.selectTask(dataset.tasks[1].task_id);
  old.resolve(dataset.tasks[0]);await first;
  assert.equal(calls,2);assert.deepEqual(selected,[dataset.tasks[1].task_id]);controller.destroy();
});
test('destroyed task component discards pending responses and detaches listeners',async()=>{
  const pending=deferred(),node=host();const controller=createTaskList(node,{client:{getTask:async()=>{},listTasks:()=>pending.promise}});
  const promise=controller.load();controller.destroy();pending.resolve({items:dataset.tasks,next_cursor:null});await promise;
  assert.equal(node.innerHTML,'');assert.equal(node.listeners.size,0);
});
test('untrusted task text is escaped; unknown states are not rendered as approved',async()=>{
  const node=host();const controller=createTaskList(node,{client:{getTask:async()=>{},listTasks:async()=>({items:[{...dataset.tasks[0],title:'<img src=x onerror=alert(1)>',stage:'future'}],next_cursor:null})}});
  await controller.load();assert.doesNotMatch(node.innerHTML,/<img /);assert.match(node.innerHTML,/&lt;img/);assert.match(node.innerHTML,/未知状态：future/);controller.destroy();
});
test('FIX-05 completed count includes failures, never turns business missing facts into failures',()=>{
  const value=run(),view=progressView(value);
  assert.equal(view.counts.failed,1);assert.equal(view.completed,value.progress.succeeded+1);
  assert.equal(view.failureIds.length,1);assert.ok(view.businessRows>0);assert.equal(view.percent,100);
});

test('FIX-10 expert run uses explicit issues without treating a null precheck comparison as an error',async()=>{
  const value=fixture('FIX-10').run,node=host();
  assert.equal(value.items[0].comparison_status,null);
  assert.equal(progressView(value).businessRows,1);
  const controller=createRunProgress(node,{client:{getRun:async()=>value}});
  await controller.open(context(value));
  assert.match(node.innerHTML,/有核查问题，待专家确认/);
  assert.match(node.innerHTML,/提交码与要素核对冲突/);
  assert.match(node.innerHTML,/专家建议编码/);
  assert.match(node.innerHTML,/1 项有已返回的核查问题/);
  assert.doesNotMatch(node.innerHTML,/未知状态：未提供|编码一致|编码不一致|原码缺失|预审比较/);
  controller.destroy();
});

test('expert rows with no returned issues do not inflate the count or imply a passing decision',async()=>{
  for(const comparison of [null,'consistent','inconsistent','supplier_missing']) {
    const value=fixture('FIX-10').run,node=host();
    value.items[0].issues=[];value.items[0].comparison_status=comparison;
    assert.equal(progressView(value).businessRows,0);
    const controller=createRunProgress(node,{client:{getRun:async()=>value}});
    await controller.open(context(value));
    assert.match(node.innerHTML,/核查执行已完成，详细结论请查看商品详情/);
    assert.match(node.innerHTML,/未返回问题不代表核查通过/);
    assert.doesNotMatch(node.innerHTML,/编码一致|编码不一致|原码缺失|未知状态：未提供|有业务待核信息|有已返回的核查问题/);
    assert.match(node.innerHTML,/是否通过，以人工审核决定为准/);
    controller.destroy();
  }
});

test('precheck comparison labels and pending counts remain separate from expert findings',async()=>{
  const values={association_error:'关联异常',supplier_missing:'原码缺失',supplier_invalid:'原码异常',
    needs_confirmation:'待确认 / 补充资料',consistent:'编码一致',inconsistent:'编码不一致'};
  for(const [status,label] of Object.entries(values)) {
    const value=run(),node=host();
    value.items=[{...value.items[0],status:'succeeded',comparison_status:status,issues:[]}];
    value.progress={total:1,queued:0,running:0,succeeded:1,failed:0};
    assert.equal(progressView(value).businessRows,status==='consistent'?0:1);
    const controller=createRunProgress(node,{client:{getRun:async()=>value}});
    await controller.open(context(value));
    assert.ok(node.innerHTML.includes(label));
    assert.doesNotMatch(node.innerHTML,/有核查问题|核查执行已完成/);
    controller.destroy();
  }
});

test('expert technical failure stays a failure and clicking its row retains expert run identity',async()=>{
  const value=fixture('FIX-10').run,node=host(),clicked=[];
  value.items[0]={...value.items[0],status:'failed',issues:[],error_message:'专家模型连接超时'};
  value.progress={total:1,queued:0,running:0,succeeded:0,failed:1};
  const controller=createRunProgress(node,{client:{getRun:async()=>value},onSelectItem:item=>clicked.push(item)});
  await controller.open(context(value));
  assert.equal(progressView(value).businessRows,0);
  assert.match(node.innerHTML,/技术失败：专家模型连接超时/);
  assert.doesNotMatch(node.innerHTML,/核查执行已完成|未知状态：未提供|编码一致/);
  const button={dataset:{item:value.items[0].item_id},disabled:false};
  node.listeners.get('click')({target:{closest:()=>button}});
  assert.deepEqual(clicked,[{task_id:value.task_id,version_id:value.version_id,run_id:value.run_id,item_id:value.items[0].item_id}]);
  controller.destroy();
});
test('inconsistent or negative progress and duplicate rows are rejected rather than shown complete',()=>{
  for(const value of [run({progress:{total:8,queued:0,running:0,succeeded:8,failed:1}}),
    run({progress:{total:8,queued:-1,running:1,succeeded:7,failed:1}}),run({items:[run().items[0],run().items[0]]})]) {
    assert.throws(()=>progressView(value));
  }
});
test('zero total has no fabricated percent',()=>{
  assert.equal(progressView(run({items:[],progress:{total:0,queued:0,running:0,succeeded:0,failed:0}})).percent,null);
});
test('polling is serial, consumes only server data, and stops at terminal status',async()=>{
  const clock=scheduler(),node=host();let calls=0;
  const controller=createRunProgress(node,{scheduler:clock,client:{getRun:async()=>++calls===1?activeRun():run()}});
  await controller.open(context(run()));assert.equal(clock.pending.size,1);assert.match(node.innerHTML,/0<span> \/ 1/);
  await clock.tick();assert.equal(calls,2);assert.equal(clock.pending.size,0);assert.equal(controller.getState().run.status,run().status);controller.destroy();
});
test('FIX-09 switching to V2 prevents late V1 run from replacing V2',async()=>{
  const first=deferred(),old=fixture('FIX-09').run,newer=run();
  const controller=createRunProgress(host(),{client:{getRun:async id=>id===old.run_id?first.promise:newer}});
  const a=controller.open(context(old));await controller.open(context(newer));
  first.resolve(old);await a;assert.equal(controller.getState().run.run_id,newer.run_id);controller.destroy();
});
test('wrong task/version/run identities are rejected and never polled',async()=>{
  for(const key of ['task_id','version_id','run_id']) {
    const node=host(),clock=scheduler();const controller=createRunProgress(node,{scheduler:clock,client:{getRun:async()=>activeRun({[key]:'wrong'})}});
    await controller.open(context(run()));assert.equal(controller.getState().run,null);assert.match(node.innerHTML,/不属于所选任务或版本/);assert.equal(clock.pending.size,0);controller.destroy();
  }
});
test('failed poll preserves last snapshot, pauses, and manual reconnect resumes',async()=>{
  let n=0;const node=host(),clock=scheduler();
  const controller=createRunProgress(node,{scheduler:clock,client:{getRun:async()=>{if(++n===2)throw new Error('网络断开');return activeRun();}}});
  await controller.open(context(run()));await clock.tick();
  assert.equal(clock.pending.size,0);assert.match(node.innerHTML,/以下是上次读取的结果/);assert.equal(controller.getState().run.status,'running');
  await controller.refresh();assert.equal(clock.pending.size,1);controller.destroy();assert.equal(clock.pending.size,0);
});
test('historical version and superseded runs stay read-only even with retry permission',async()=>{
  for(const variation of ['version','run']) {
    let writes=0;const value=run({is_current:variation!=='run'}),node=host(),clock=scheduler();
    const controller=createRunProgress(node,{client:{getRun:async()=>value},scheduler:clock,retryAllowed:true,onRetry:async()=>{writes++;}});
    await controller.open({...context(value),isVersionCurrent:variation!=='version'});controller.selectFailed(progressView(value).failureIds);await controller.retry();
    assert.equal(writes,0);assert.equal(clock.pending.size,0);assert.match(node.innerHTML,/历史执行 · 只读/);controller.destroy();
  }
});
test('only technical failed IDs are selected; repeated retry clicks issue one intent',async()=>{
  const value=run(),pending=deferred(),intents=[];const controller=createRunProgress(host(),{
    client:{getRun:async()=>value},retryAllowed:true,onRetry:intent=>{intents.push(intent);return pending.promise;}});
  await controller.open(context(value));controller.selectFailed(value.items.map(i=>i.item_id));
  const a=controller.retry(),b=controller.retry();await b;
  assert.deepEqual(intents[0].item_ids,progressView(value).failureIds);assert.equal(intents.length,1);
  pending.reject({status:501,message:'尚未实现'});await a;assert.equal(controller.getState().selected.length,1);controller.destroy();
});
test('FIX-08 conflict and 501 failures keep selection and reuse idempotency key',async()=>{
  const value=run(),intents=[],node=host();let n=0;
  const controller=createRunProgress(node,{client:{getRun:async()=>value},retryAllowed:true,onRetry:async intent=>{
    intents.push(intent);throw ++n===1?{status:409,...fixture('FIX-08').error.error}:{status:501,message:'后台未接通'};
  }});
  await controller.open(context(value));controller.selectFailed(progressView(value).failureIds);
  await controller.retry();assert.match(node.innerHTML,/内容已更新，请刷新/);
  await controller.retry();assert.match(node.innerHTML,/该功能尚未接通/);
  assert.equal(intents[0].idempotency_key,intents[1].idempotency_key);assert.equal(controller.getState().selected.length,1);controller.destroy();
});
test('accepted retry loads a new run without altering old successful rows',async()=>{
  const old=run(),before=copy(old),fresh=activeRun({run_id:'new-run'});
  const controller=createRunProgress(host(),{client:{getRun:async id=>id===old.run_id?old:fresh},retryAllowed:true,
    onRetry:async()=>({run_id:'new-run'})});
  await controller.open(context(old));controller.selectFailed(progressView(old).failureIds);await controller.retry();
  assert.equal(controller.getState().run.run_id,'new-run');assert.deepEqual(old,before);controller.destroy();
});
test('late retry result cannot reopen an old version after a version switch',async()=>{
  const old=run(),fresh=run({run_id:'v3-run',version_id:'v3'}),pending=deferred();
  const controller=createRunProgress(host(),{client:{getRun:async id=>id===old.run_id?old:fresh},retryAllowed:true,onRetry:()=>pending.promise});
  await controller.open(context(old));controller.selectFailed(progressView(old).failureIds);const a=controller.retry();
  await controller.open(context(fresh));pending.resolve({run_id:'old-retry'});await a;
  assert.equal(controller.getState().run.run_id,'v3-run');controller.destroy();
});
test('destroy clears timers, ignores pending run, and removes handlers',async()=>{
  const value=activeRun(),clock=scheduler(),node=host(),pending=deferred();let n=0;
  const controller=createRunProgress(node,{scheduler:clock,client:{getRun:async()=>++n===1?value:pending.promise}});
  await controller.open(context(value));assert.equal(clock.pending.size,1);
  const request=controller.refresh();controller.destroy();pending.resolve(run());await request;
  assert.equal(clock.pending.size,0);assert.equal(node.innerHTML,'');assert.equal(node.listeners.size,0);
});
test('unwired or unauthorized retry cannot write; missing fact messages are escaped',async()=>{
  const value=run();value.items[0].issues=[{message:'<script>alert(1)</script>',missing:'用途'}];
  const node=host();let writes=0;const controller=createRunProgress(node,{client:{getRun:async()=>value},onRetry:async()=>{writes++;}});
  await controller.open(context(value));controller.selectFailed(progressView(value).failureIds);await controller.retry();
  assert.equal(writes,0);assert.match(node.innerHTML,/&lt;script&gt;/);assert.doesNotMatch(node.innerHTML,/<script>/);controller.destroy();
});
test('authentication, permission, missing, conflict and unimplemented errors remain distinct',()=>{
  assert.equal(new Set([401,403,404,409,501].map(status=>errorView({status}).title)).size,5);
});
