/** Exercise the production host + real P11; Python supplies actual public API payloads. No browser claim. */
import assert from 'node:assert/strict';
import {createWorkbenchReview} from '../../web/workbench-live.js';
import {installDocument,host,field,click,flush} from './review-harness.mjs';
installDocument();
let input='';for await(const chunk of process.stdin)input+=chunk;
const {state,cards,targetItemId,finalHs,reason}=JSON.parse(input);
let current=state,request=null;const node=host();
const ctx={store:{get:()=>current},openTask(){}};
const view=createWorkbenchReview({host:node,getUser:()=>({role:'expert'}),getContext:()=>ctx,getSession:()=>1,
  client:{decideSubmission:async(submission_id,body,key)=>{request={submission_id,body,key};return {};}}});
view.render(current);assert.ok(node.child,'complete submitted batch should mount before selecting a card');
const root=node.child,initialHtml=root.innerHTML;assert.doesNotMatch(initialHtml,/未命名商品|第 — 行/);
for(const item of state.items){assert.ok(initialHtml.includes(item.name));assert.ok(initialHtml.includes(`第 ${item.source_location.row_number} 行`));}
field(root,'final-hs',finalHs,targetItemId);field(root,'change-reason',reason,targetItemId);
for(const card of cards){
  current={...state,itemId:card.item_id,card:null};view.render(current);assert.equal(node.child,root);
  current={...state,itemId:card.item_id,card};view.render(current);assert.equal(node.child,root);
}
click(node.child,'decide');await flush();assert.ok(request);
assert.deepEqual(request.body.items.find(i=>i.item_id===targetItemId),{item_id:targetItemId,final_hs:finalHs,change_reason:reason});
view.destroy();process.stdout.write(JSON.stringify(request));
