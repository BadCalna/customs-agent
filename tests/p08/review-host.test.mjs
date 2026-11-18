import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkbenchReview} from '../../web/workbench-live.js';
import {createWorkbenchContext} from '../../web/workbench-context.js';
import {installDocument,host,field,click,flush,expertState} from './review-harness.mjs';
installDocument();

function harness(initial=expertState(),role='expert') {
  let current=initial,ticket=1;const node=host(),calls=[],refreshes=[];
  const ctx={store:{get:()=>current},openTask:id=>refreshes.push(id)};
  const client={confirmItem:async(...args)=>{calls.push(['confirm',...args]);return {};},
    decideSubmission:async(...args)=>{calls.push(['decide',...args]);return {};}};
  const view=createWorkbenchReview({host:node,client,getContext:()=>ctx,getUser:()=>({role}),getSession:()=>ticket});
  const render=s=>{current=s;view.render(s);};render(initial);
  return {node,view,calls,client,refreshes,render,setSession:()=>ticket++};
}
function browse(s,item,revision){return {...s,itemId:item,card:{item_id:item,revision,facts:{name:'选中卡片不作为整批标签来源'}}};}

test('first expert render labels every snapshot row by ID before a commodity is selected',()=>{
  const s=expertState(),h=harness(s),html=h.node.child.innerHTML;
  assert.equal(s.card,null);assert.doesNotMatch(html,/未命名商品|第 — 行/);
  for(const item of s.items){assert.match(html,new RegExp(item.name));assert.match(html,new RegExp(`第 ${item.source_location.row_number} 行`));}
  assert.match(html,/data-item-id="third"[\s\S]*?合成丙/);assert.match(html,/data-item-id="first"[\s\S]*?合成甲/);
  assert.equal(s.history.versions[0].submissions[0].items[0].name,undefined,'published snapshot input is not mutated');h.view.destroy();
});
test('expert recode and reason survive card loading, different revisions, and actual approve event',async()=>{
  const s=expertState(),h=harness(s),root=h.node.child;
  field(root,'final-hs','9999999999','second');field(root,'change-reason','合成改码原因','second');field(root,'decision-reason','整批说明');
  for(const next of [browse(s,'first',1),{...s,itemId:'second',card:null},browse(s,'second',2),browse(s,'third',88),{...s,error:Error('商品卡片读取失败')}]){
    h.render(next);assert.equal(h.node.child,root);assert.equal(root.removed,false);
  }
  click(h.node.child,'decide');await flush();
  const [action,submission,payload]=h.calls[0];assert.equal(action,'decide');assert.equal(submission,'S');
  assert.equal(payload.reason,'整批说明');assert.deepEqual(payload.items.find(i=>i.item_id==='second'),{item_id:'second',final_hs:'9999999999',change_reason:'合成改码原因'});
  assert.equal(payload.items.find(i=>i.item_id==='first').final_hs,'1111111111');h.view.destroy();
});
test('returned-item issues and recipient survive browsing all commodities',async()=>{
  const s=expertState(),h=harness(s),root=h.node.child;
  field(root,'decision-mode','return');field(root,'return-to','supplier');field(root,'decision-reason','资料待补');field(root,'issue','请补用途','third');
  for(const item of s.items)h.render(browse(s,item.item_id,item.revision));
  assert.equal(h.node.child,root);assert.doesNotMatch(root.innerHTML,/未命名商品|第 — 行/);
  click(root,'decide');await flush();assert.equal(h.calls[0][2].return_to,'supplier');
  assert.deepEqual(h.calls[0][2].issues,[{item_id:'third',message:'请补用途'}]);h.view.destroy();
});
test('metadata or run refresh in the same submission updates the instance without losing draft',async()=>{
  const s=expertState(),h=harness(s),root=h.node.child;field(root,'final-hs','9999999999','first');field(root,'change-reason','同提交刷新','first');
  const fresh=structuredClone(s);fresh.task.revision++;fresh.items[0].name='合成甲补充名称';fresh.runId=fresh.run.run_id='R2';
  h.render(fresh);assert.equal(h.node.child,root);assert.match(root.innerHTML,/合成甲补充名称/);
  click(root,'decide');await flush();assert.equal(h.calls[0][2].items.find(i=>i.item_id==='first').final_hs,'9999999999');h.view.destroy();
});
test('missing or foreign metadata blocks review rather than presenting unidentified rows',()=>{
  const s=expertState(),h=harness({...s,loading:true});assert.equal(h.node.child,null);assert.match(h.node.innerHTML,/正在读取/);
  h.render({...s,items:s.items.filter(i=>i.item_id!=='second')});assert.equal(h.node.child,null);assert.match(h.node.innerHTML,/品名或来源位置/);
  h.render({...s,items:s.items.map(i=>i.item_id==='second'?{...i,version_id:'foreign'}:i)});assert.equal(h.node.child,null);
  h.render(s);assert.ok(h.node.child);h.view.destroy();
});
test('a new submission and a late response cannot reuse or apply the old draft',async()=>{
  const s=expertState(),h=harness(s),old=h.node.child;field(old,'final-hs','9999999999','first');field(old,'change-reason','旧提交','first');
  let resolve;h.client.decideSubmission=(...args)=>{h.calls.push(['decide',...args]);return new Promise(r=>resolve=r);};
  click(old,'decide');await flush();
  const next=structuredClone(s);next.run.submission_id='S2';next.history.versions[0].submissions[0].submission_id='S2';
  h.render(next);const fresh=h.node.child;assert.notEqual(fresh,old);assert.ok(old.removed);assert.doesNotMatch(fresh.innerHTML,/9999999999|旧提交/);
  field(fresh,'final-hs','8888888888','first');field(fresh,'change-reason','新提交','first');
  resolve({});await flush();assert.deepEqual(h.refreshes,[]);click(fresh,'decide');await flush();
  assert.equal(h.calls[1][1],'S2');assert.equal(h.calls[1][2].items.find(i=>i.item_id==='first').final_hs,'8888888888');
  h.view.destroy();
});
test('operator confirmation remains bound to only the selected commodity',async()=>{
  const s=expertState();s.task.allowed_actions=['read','confirmation.write'];
  const card=id=>({item_id:id,revision:1,facts:{name:id},business_confirmed_hs:{value:null,source_id:null},source_location:{row_number:8}});
  const h=harness({...s,card:card('first'),itemId:'first'},'operator'),old=h.node.child;
  field(old,'confirm-hs','1111111111','first');h.render({...s,card:card('second'),itemId:'second'});
  assert.notEqual(h.node.child,old);field(h.node.child,'confirm-hs','2222222222','second');click(h.node.child,'confirm','second');await flush();
  assert.equal(h.calls[0][0],'confirm');assert.equal(h.calls[0][1],'V');assert.equal(h.calls[0][2],'second');h.view.destroy();
});


test('real context reload retains the panel through task/history/items/run and a failed retry',async()=>{
  const s=expertState();s.task.latest_run_kind='expert_check';s.task.latest_run_id='R';s.history.task_id='T';
  s.history.versions[0].runs=[s.run];
  const waits=[],calls=[],node=host();
  const hold=value=>new Promise((resolve,reject)=>waits.push({resolve:()=>resolve(structuredClone(value)),reject}));
  const client={getTask:()=>hold(s.task),getHistory:()=>hold(s.history),listItems:()=>hold({items:s.items}),
    decideSubmission:async(...args)=>{calls.push(args);return {};}};
  const ctx=createWorkbenchContext(client);
  const view=createWorkbenchReview({host:node,client,getContext:()=>ctx,getUser:()=>({role:'expert'}),getSession:()=>1});
  ctx.store.set(s);const off=ctx.store.subscribe(v=>view.render(v));view.render(s);
  const root=node.child;field(root,'final-hs','9999999999','first');field(root,'change-reason','真实加载链路','first');
  let pending=ctx.openTask('T');waits.shift().reject(Error('任务读取失败'));await pending;
  assert.equal(node.child,root);assert.equal(node.hidden,true);
  click(root,'decide');await flush();assert.equal(calls.length,0);
  pending=ctx.openTask('T');
  for(let i=0;i<3;i++){
    assert.equal(node.child,root);assert.equal(node.hidden,true);
    waits.shift().resolve();await flush();
  }
  await pending;assert.equal(node.child,root);assert.equal(node.hidden,true,'run not revalidated yet');
  ctx.acceptProgress({context:{taskId:'T',versionId:'V',runId:'R'},run:s.run});
  assert.equal(node.child,root);assert.equal(node.hidden,false);
  click(root,'decide');await flush();assert.equal(calls[0][1].items.find(i=>i.item_id==='first').final_hs,'9999999999');
  off();view.destroy();ctx.destroy();
});

test('history, item-list and run failures keep the same draft suspended until successful revalidation',async()=>{
  for(const stage of ['history','items','run']){
    const s=expertState();s.task.latest_run_kind='expert_check';s.task.latest_run_id='R';s.history.task_id='T';s.history.versions[0].runs=[s.run];
    let failing=true;const calls=[],node=host();
    const client={getTask:async()=>s.task,getHistory:async()=>{if(failing&&stage==='history')throw Error('history failed');return s.history;},
      listItems:async()=>{if(failing&&stage==='items')throw Error('items failed');return {items:s.items};},
      decideSubmission:async(...args)=>{calls.push(args);return {};}};
    const ctx=createWorkbenchContext(client),view=createWorkbenchReview({host:node,client,getContext:()=>ctx,getUser:()=>({role:'expert'}),getSession:()=>1});
    ctx.store.set(s);const off=ctx.store.subscribe(v=>view.render(v));view.render(s);const root=node.child;
    field(root,'final-hs','9999999999','first');field(root,'change-reason','失败重试保留','first');
    await ctx.openTask('T');
    if(stage==='run')ctx.acceptProgress({context:{taskId:'T',versionId:'V',runId:'R'},run:null,error:Error('run failed')});
    assert.equal(node.child,root,stage);assert.equal(node.hidden,true,stage);click(root,'decide');await flush();assert.equal(calls.length,0);
    failing=false;await ctx.openTask('T');ctx.acceptProgress({context:{taskId:'T',versionId:'V',runId:'R'},run:s.run});
    assert.equal(node.child,root);assert.equal(node.hidden,false);click(root,'decide');await flush();
    assert.equal(calls[0][1].items.find(i=>i.item_id==='first').change_reason,'失败重试保留');
    off();view.destroy();ctx.destroy();
  }
});

test('loading and missing metadata suspend rather than prune approve and return drafts',async()=>{
  for(const mode of ['approve','return']){
    const s=expertState(),h=harness(s),root=h.node.child;
    field(root,'final-hs','9999999999','first');field(root,'change-reason','保留改码','first');
    if(mode==='return'){field(root,'decision-mode','return');field(root,'return-to','supplier');field(root,'decision-reason','保留退回原因');field(root,'issue','保留问题','third');}
    for(const next of [{...s,loading:true},{...s,items:[]},{...s,run:null,progressError:Error('断线') }]){
      h.render(next);assert.equal(h.node.child,root);assert.equal(h.node.hidden,true);
      click(root,'decide');await flush();assert.equal(h.calls.length,0);
    }
    h.render(s);assert.equal(h.node.child,root);assert.equal(h.node.hidden,false);
    click(root,'decide');await flush();const body=h.calls[0][2];
    if(mode==='approve')assert.equal(body.items.find(i=>i.item_id==='first').change_reason,'保留改码');
    else {assert.equal(body.return_to,'supplier');assert.equal(body.reason,'保留退回原因');assert.deepEqual(body.issues,[{item_id:'third',message:'保留问题'}]);}
    h.view.destroy();
  }
});

test('a destroyed panel cannot refresh a new instance even after returning to the same scope',async()=>{
  const s=expertState(),h=harness(s),old=h.node.child;let resolve;
  h.client.decideSubmission=()=>new Promise(r=>resolve=r);click(old,'decide');await flush();
  const changed=structuredClone(s);changed.run.submission_id='S2';changed.history.versions[0].submissions[0].submission_id='S2';
  h.render(changed);h.render(s);assert.notEqual(h.node.child,old);
  resolve({});await flush();assert.deepEqual(h.refreshes,[]);h.view.destroy();
});

test('an in-flight success during refresh starts a post-write read instead of trusting the earlier snapshot',async()=>{
  const s=expertState(),h=harness(s),root=h.node.child;let resolve;
  h.client.decideSubmission=()=>new Promise(r=>resolve=r);
  click(root,'decide');await flush();h.render({...s,loading:true});assert.equal(h.node.hidden,true);
  resolve({});await flush();assert.deepEqual(h.refreshes,['T']);h.view.destroy();
});

test('task, version and login-session changes create a fresh review scope',()=>{
  for(const kind of ['task','version','session']){
    const s=expertState(),h=harness(s),old=h.node.child;field(old,'final-hs','9999999999','first');field(old,'change-reason','旧作用域','first');
    const next=structuredClone(s);
    if(kind==='task'){next.taskId=next.task.task_id='T2';next.items.forEach(i=>i.task_id='T2');}
    if(kind==='version'){next.versionId=next.task.current_version_id=next.task.versions[0].version_id=next.history.versions[0].version_id='V2';next.items.forEach(i=>i.version_id='V2');}
    if(kind==='session')h.setSession();
    h.render(next);assert.notEqual(h.node.child,old,kind);assert.doesNotMatch(h.node.child.innerHTML,/9999999999|旧作用域/);h.view.destroy();
  }
});

test('operator confirmation draft survives 409 conflict reload and concurrent refresh races',async()=>{
  let revision=1, conflictNext=true, confirmCalls=[];
  const base=expertState();
  base.history.task_id='T';
  base.task.allowed_actions=['read','confirmation.write'];
  const items=[{task_id:'T',version_id:'V',item_id:'first',name:'合成甲',revision:1,source_location:{sheet_name:'申报要素',row_number:8,cells:'A8:J8'},facts:{name:'合成甲'}}];
  const client={
    getTask:async()=>base.task,
    getHistory:async()=>base.history,
    listItems:async()=>({items:items.map(i=>({...i,revision})),next_cursor:null}),
    getItem:async(v,id)=>({...items.find(i=>i.item_id===id),revision}),
    confirmItem:async(v,id,payload)=>{
      confirmCalls.push(payload);
      if(conflictNext){const err=new Error('revision conflict');err.status=409;err.code='conflict';throw err;}
      return {revision:revision+1,status:'confirmed'};
    },
    decideSubmission:async()=>({}),
  };
  const node=host(), ctx=createWorkbenchContext(client);
  const view=createWorkbenchReview({host:node,client,getContext:()=>ctx,getUser:()=>({role:'operator'}),getSession:()=>1});
  const off=ctx.store.subscribe(s=>view.render(s));
  await ctx.openTask('T');
  await ctx.selectItem('first');
  const root=node.child;
  field(root,'confirm-hs','8501109900','first');
  field(root,'confirm-reason','业务员冲突测试','first');

  // Server moved to revision 2; 409 reloads card preserving item & draft
  revision=2;
  click(root,'confirm','first');
  await flush();
  assert.equal(confirmCalls[0].expected_revision,1);
  assert.equal(node.child,root);
  assert.equal(node.hidden,false);
  assert.equal(ctx.store.get().card.revision,2);

  // Retry succeeds using bumped revision and preserved draft
  conflictNext=false;
  click(root,'confirm','first');
  await flush();
  assert.equal(confirmCalls[1].expected_revision,2);
  assert.equal(confirmCalls[1].confirmed_hs,'8501109900');
  assert.equal(confirmCalls[1].reason,'业务员冲突测试');

  // Simulate an in-flight confirm settling after a concurrent refresh cleared s.card/panel
  let releaseLate;
  const gate=new Promise(r=>{releaseLate=r;});
  client.confirmItem=async()=>{await gate;const err=new Error('late conflict');err.status=409;throw err;};
  field(root,'confirm-hs','8501109901','first');
  field(root,'confirm-reason','并发刷新测试','first');
  click(root,'confirm','first');
  await flush();
  await ctx.openTask('T'); // Clears itemId/card because preserveItem wasn't passed
  assert.equal(ctx.store.get().itemId,null);
  assert.equal(node.child,null);
  releaseLate();
  await flush();
  assert.equal(ctx.store.get().itemId,'first');
  assert.equal(ctx.store.get().card.revision,2);

  off();view.destroy();ctx.destroy();
});
