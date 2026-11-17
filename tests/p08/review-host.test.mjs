import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkbenchReview} from '../../web/workbench-live.js';
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
