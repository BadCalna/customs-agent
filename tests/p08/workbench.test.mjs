import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkbenchContext, defaultRun} from '../../web/workbench-context.js';
import {createTaskList} from '../../web/components/task-list.js';

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const host=()=>({innerHTML:'',addEventListener(){},removeEventListener(){},replaceChildren(){},contains:()=>true});
const task=id=>({task_id:id,current_version_id:id+'2',latest_run_id:id+'-pre2',latest_run_kind:'precheck',versions:[{version_id:id+'1'},{version_id:id+'2'}]});
const history=id=>({task_id:id,versions:[1,2].map(n=>({version_id:id+n,runs:[{run_id:id+'-pre'+n,kind:'precheck',is_current:true},{run_id:id+'-expert'+n,kind:'expert_check',is_current:true}],submissions:[]}))});
const item=(id,v)=>({item_id:id,task_id:v[0],version_id:v});
const api=()=>({getTask:async id=>task(id),getHistory:async id=>history(id),listItems:async v=>({items:[item(v+'i',v)],next_cursor:null}),getItem:async(v,id)=>({...item(id,v),facts:{name:id}})});

test('run selection is version and stage scoped; multiple current runs are ambiguous',()=>{
  const t=task('A'),h=history('A');assert.equal(defaultRun(t,h,'A1','expert_check'),'A-expert1');
  h.versions[0].runs.push({run_id:'ambiguous',kind:'expert_check',is_current:true});
  assert.equal(defaultRun(t,h,'A1','expert_check'),null);
  assert.equal(defaultRun(t,h,'A2','expert_check'),'A-expert2');
  assert.equal(defaultRun(t,h,'unknown','precheck'),null);
});
test('task intent immediately invalidates old details before slow getTask completes',async()=>{
  const a=api(),ctx=createWorkbenchContext(a);await ctx.openTask('A');const wait=deferred();a.getTask=id=>id==='B'?wait.promise:Promise.resolve(task(id));
  const list=createTaskList(host(),{client:{...a,listTasks:async()=>({items:[task('A'),task('B')]})},onSelectionStart:ctx.beginTask,onSelectTask:ctx.acceptTask});
  await list.load();const pending=list.selectTask('B');assert.equal(ctx.store.get().task,null);assert.equal(ctx.store.get().runId,null);
  wait.resolve(task('B'));await pending;assert.equal(ctx.store.get().versionId,'B2');list.destroy();ctx.destroy();
});
test('late task history errors cannot replace newer task',async()=>{
  const a=api(),wait=deferred();a.getHistory=id=>id==='A'?wait.promise:Promise.resolve(history(id));
  const ctx=createWorkbenchContext(a),old=ctx.openTask('A');await new Promise(r=>setImmediate(r));await ctx.openTask('B');
  wait.reject(new Error('old failure'));await old;assert.equal(ctx.store.get().taskId,'B');assert.equal(ctx.store.get().error,null);ctx.destroy();
});
test('version pagination late response and old card cannot cross task/version/run selection',async()=>{
  const a=api(),ctx=createWorkbenchContext(a);await ctx.openTask('A');const wait=deferred();a.listItems=v=>v==='A1'?wait.promise:Promise.resolve({items:[item(v+'i',v)]});
  const old=ctx.openVersion('A1');await ctx.openVersion('A2');wait.resolve({items:[item('OLD','A1')]});await old;
  assert.equal(ctx.store.get().items[0].item_id,'A2i');
  const card=deferred();a.getItem=()=>card.promise;const pending=ctx.selectItem('A2i');ctx.chooseKind('expert_check');card.resolve({...item('A2i','A2')});await pending;
  assert.equal(ctx.store.get().card,null);assert.equal(ctx.store.get().runId,'A-expert2');ctx.destroy();
});
test('missing run clears previous progress; old poll and mismatched kind rejected',async()=>{
  const ctx=createWorkbenchContext(api());await ctx.openTask('A');
  ctx.acceptProgress({context:{taskId:'A',versionId:'A2',runId:'A-pre2'},run:{kind:'precheck'}});
  assert.ok(ctx.store.get().run);ctx.chooseRun(null);assert.equal(ctx.store.get().run,null);
  ctx.acceptProgress({context:{taskId:'A',versionId:'A2',runId:'A-pre2'},run:{kind:'precheck'}});assert.equal(ctx.store.get().run,null);
  ctx.chooseRun('A-expert2','expert_check');ctx.acceptProgress({context:{taskId:'A',versionId:'A2',runId:'A-expert2'},run:{kind:'precheck'}});
  assert.match(ctx.store.get().progressError.message,/阶段/);ctx.destroy();
});
test('all item cursors are consumed and foreign identities rejected',async()=>{
  const a=api(),requests=[];a.listItems=async(v,p)=>{requests.push(p.cursor);return {items:[item(p.cursor?'second':'first',v)],next_cursor:p.cursor?null:'opaque'};};
  const ctx=createWorkbenchContext(a);await ctx.openTask('A');assert.equal(ctx.store.get().items.length,2);assert.deepEqual(requests,[null,'opaque']);
  a.listItems=async()=>({items:[item('wrong','B2')]});await ctx.openVersion('A1');assert.match(ctx.store.get().error.message,/身份/);ctx.destroy();
});
test('row navigation enforces task/version/run/item and teardown rejects pending reads',async()=>{
  const a=api(),ctx=createWorkbenchContext(a);await ctx.openTask('A');
  await ctx.selectItem('A2i',{task_id:'A',version_id:'A1',run_id:'A-pre2'});assert.equal(ctx.store.get().card,null);
  await ctx.selectItem('A2i',{task_id:'A',version_id:'A2',run_id:'A-pre2'});assert.equal(ctx.store.get().card.item_id,'A2i');
  const wait=deferred();a.getItem=()=>wait.promise;const pending=ctx.selectItem('A2i');ctx.destroy();wait.resolve(item('A2i','A2'));await pending;assert.equal(ctx.store.get().card,null);
});
