/* 实际页面 JS 的确定性请求时序测试，不替代真实浏览器验收。 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const crypto=require('node:crypto');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../web/pages/p06.js'),'utf8')
 .replace(/^import .*;\r?\n/gm,'')
 .replace('const api = createClient({mode:"live"});','const api = globalThis.TEST_API;');

async function harness(){
 const elements=new Map(),calls=[],exportCalls=[],precheckCalls=[],retryCalls=[],progressOpens=[],timers=new Map();
 let progressOptions,timerId=0;
 function el(selector){if(!elements.has(selector))elements.set(selector,{hidden:false,disabled:false,value:'',files:[],textContent:'',innerHTML:'',listeners:{},addEventListener(event,fn){this.listeners[event]=fn},insertAdjacentHTML(_,text){this.innerHTML+=text}});return elements.get(selector);}
 const api={me:async()=>null,getTask:async id=>({task_id:id,title:id,current_version_id:'V'+id,versions:[]}),
  listTasks:async()=>({items:[],next_cursor:null}),listItems:async()=>({items:[],next_cursor:null}),getImport:async()=>{throw {status:404}},
  startPrecheck(...args){precheckCalls.push(args);return Promise.resolve({run_id:'new-run'});},
  retryRun(...args){retryCalls.push(args);return Promise.resolve({run_id:'retried-run'});},
  uploadFile(file,params){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});calls.push({file,params,resolve,reject});return promise;},
  createExport(versionId,payload){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});exportCalls.push({versionId,payload,resolve,reject});return promise;}};
 const createdLinks=[];
 const ctx={TEST_API:api,document:{querySelector:el,querySelectorAll:()=>[],addEventListener(){},createElement(){const node={click(){createdLinks.push(this)},remove(){}};return node;},body:{appendChild(){}}},URL:class extends URL {static createObjectURL(){return 'blob:1'}static revokeObjectURL(){}},setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout(id){timers.delete(id);},location:{href:'http://localhost/uploads/'},createRunProgress:(_host,options)=>{progressOptions=options;return {open:async context=>{progressOpens.push(context)}}},crypto:{randomUUID:crypto.randomUUID},console};
 vm.createContext(ctx);vm.runInContext(source+'\nglobalThis.hooks={upload,openTask,openVersion,renderExports,exportResult,hideExports,state:()=>({exporting,latestReport})};',ctx);
 await new Promise(r=>setImmediate(r));
 vm.runInContext('me={role:"supplier"};task={task_id:"A",title:"A",current_version_id:"VA",versions:[]};epoch=1;',ctx);
 el('#upload-target').value='current';el('#file').files=[Object.freeze({name:'A.xlsx',size:123,lastModified:100})];
 return {ctx,el,calls,exportCalls,createdLinks,api,precheckCalls,retryCalls,progressOpens,timers,get progressOptions(){return progressOptions}};
}
const duplicate={status:409,code:'duplicate_file',details:{version_number:1}};
(async()=>{
 // 切换任务后的迟到 duplicate_file 必须丢弃，确认按钮不可再发请求。
 const a=await harness();const pending=a.ctx.hooks.upload();await a.ctx.hooks.openTask('B');
 a.calls[0].reject(duplicate);await pending;
 assert.equal(a.el('#duplicate').hidden,true);await a.el('#confirm-duplicate').onclick();
 assert.equal(a.calls.length,1);assert.equal(vm.runInContext('task.task_id',a.ctx),'B');
 // 同一确认意图固定原任务、原版本、原文件；网络失败后复用同一确认幂等键。
 const b=await harness();const first=b.ctx.hooks.upload();b.calls[0].reject(duplicate);await first;
 assert.equal(b.el('#duplicate').hidden,false);
 const confirmation=b.el('#confirm-duplicate').onclick();b.calls[1].reject({status:0,message:'offline'});await confirmation;
 assert.equal(b.calls[1].params.taskId,'A');assert.equal(b.calls[1].params.currentVersionId,'VA');
 assert.equal(b.calls[1].file,b.calls[0].file);assert.equal(b.calls[1].params.confirmDuplicate,true);
 assert.notEqual(b.calls[0].params.key,b.calls[1].params.key);
 const retry=b.el('#confirm-duplicate').onclick();b.calls[2].reject({status:0,message:'offline'});await retry;
 assert.equal(b.calls[1].params.key,b.calls[2].params.key);
 // 文件变化使旧意图失效，迟到提示不能授权新文件。
 const c=await harness();const old=c.ctx.hooks.upload();c.el('#file').onchange();c.calls[0].reject(duplicate);await old;
 assert.equal(c.el('#duplicate').hidden,true);await c.el('#confirm-duplicate').onclick();assert.equal(c.calls.length,1);
 // 迟到成功也不能跳回旧任务或清除新任务选中的文件。
 const d=await harness();const uploaded=d.ctx.hooks.upload();await d.ctx.hooks.openTask('B');
 d.calls[0].resolve({task_id:'A',version_id:'VA2',version_number:2,item_count:3,member_count:3,precheck_status:'succeeded'});await uploaded;
 assert.equal(vm.runInContext('task.task_id',d.ctx),'B');assert.equal(d.el('#file').files[0].name,'A.xlsx');
 // 导出过程中切换版本/隐藏导出区后，立即允许新上下文导出，旧导出的迟到结算不得重置新导出的 exporting 锁；且数值字段做 HTML 转义。
 const e=await harness();
 vm.runInContext('me={role:"operator"};version="VA";',e.ctx);
 e.ctx.hooks.renderExports({version_id:'VA',version_number:'1<b>',precheck_run_id:'RA',precheck_status:'succeeded',result_mode:'<mock>'});
 assert.match(e.el('#exports').innerHTML,/1&lt;b&gt;/);
 assert.match(e.el('#exports').innerHTML,/结果来源 未提供或未知/);
 assert.doesNotMatch(e.el('#exports').innerHTML,/<mock>|&lt;mock&gt;/i);
 assert.equal(vm.runInContext(`sourceLabel('live')`,e.ctx),'LIVE');
 assert.equal(vm.runInContext(`sourceLabel(null)`,e.ctx),'未提供或未知');
 const expA=e.ctx.hooks.exportResult('precheck');
 assert.equal(e.ctx.hooks.state().exporting,true);
 e.ctx.hooks.hideExports();
 assert.equal(e.ctx.hooks.state().exporting,false);
 vm.runInContext('version="VB";',e.ctx);
 e.ctx.hooks.renderExports({version_id:'VB',version_number:2,precheck_run_id:'RB',precheck_status:'succeeded',result_mode:'mock'});
 const expB=e.ctx.hooks.exportResult('precheck');
 assert.equal(e.exportCalls.length,2);
 e.exportCalls[0].reject({status:500,code:'boom',message:'late error'});await expA;
 assert.equal(e.ctx.hooks.state().exporting,true);
 e.exportCalls[1].resolve({runId:'RB',contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',blob:{},filename:'out.xlsx',resultMode:'mock'});await expB;
 assert.equal(e.ctx.hooks.state().exporting,false);
 assert.equal(e.createdLinks.length,1);
 // 合并回归：staged 导入轮询完成后进度、重新预审和正式导出同时保留。
 const q=await harness();let staged=true;
 const report=()=>({version_id:'VA',version_number:1,precheck_run_id:'RA',precheck_status:staged?'queued':'succeeded',parse_status:staged?'staged':'succeeded',result_mode:'mock',assignment_status:'claimed',item_count:1,member_count:1,image_counts:{available:0,absent:1,read_failed:0},error:{},report:{}});
 q.api.getTask=async id=>({task_id:id,title:id,current_version_id:'V'+id,latest_run_id:'R'+id,precheck_allowed:true,revision:7,versions:[{version_id:'V'+id,number:1,is_current:true}]});
 q.api.getImport=async()=>report();
 vm.runInContext('me={role:"operator"};',q.ctx);
 await q.ctx.hooks.openTask('A');
 assert.equal(q.el('#rerun').hidden,true);
 assert.match(q.el('#exports').innerHTML,/预审尚未完成/);
 assert.equal(q.timers.size,1);
 staged=false;const tick=[...q.timers.values()][0];q.timers.clear();tick();await new Promise(r=>setImmediate(r));
 assert.equal(q.el('#rerun').hidden,false);
 assert.equal(q.progressOpens.at(-1).runId,'RA');
 assert.doesNotMatch(q.el('#exports').innerHTML,/disabled/);
 await q.progressOptions.onRetry({run_id:'RA',item_ids:['item-A'],idempotency_key:'retry-key'});
 assert.deepEqual(q.retryCalls[0],['RA',['item-A'],'retry-key']);
 q.api.startPrecheck=(...args)=>{q.precheckCalls.push(args);throw new Error('offline')};
 await q.el('#rerun').onclick();await q.el('#rerun').onclick();
 assert.equal(q.precheckCalls.length,2);assert.equal(q.precheckCalls[0][2],q.precheckCalls[1][2]);
 assert.equal(q.precheckCalls[0][0],'VA');assert.equal(q.precheckCalls[0][1].revision,7);
 // 当前上下文的完成响应只能刷新一次；重新选过同一任务后旧回调也不能触发刷新。
 q.api.getRun=async()=>({run_id:'RA',task_id:'A',version_id:'VA',status:'succeeded'});
 await q.progressOptions.client.getRun('RA');
 assert.equal(q.timers.size,1);
 await q.ctx.hooks.openTask('B');
 const lateRefresh=[...q.timers.values()][0];q.timers.clear();lateRefresh();await new Promise(r=>setImmediate(r));
 assert.equal(vm.runInContext('task.task_id',q.ctx),'B');
 console.log('PASS: late duplicate/success discarded; immutable confirmation; stable retry key; file change invalidates intent; export ticket isolated; queue poll/rerun/retry and export coexist');
})().catch(error=>{console.error(error);process.exitCode=1});
