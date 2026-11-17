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
 const elements=new Map(),calls=[];
 function el(selector){if(!elements.has(selector))elements.set(selector,{hidden:false,disabled:false,value:'',files:[],textContent:'',innerHTML:'',listeners:{},addEventListener(event,fn){this.listeners[event]=fn},insertAdjacentHTML(_,text){this.innerHTML+=text}});return elements.get(selector);}
 const api={me:async()=>null,getTask:async id=>({task_id:id,title:id,current_version_id:'V'+id,versions:[]}),
  listTasks:async()=>({items:[],next_cursor:null}),listItems:async()=>({items:[],next_cursor:null}),getImport:async()=>{throw {status:404}},
  uploadFile(file,params){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});calls.push({file,params,resolve,reject});return promise;}};
 const ctx={TEST_API:api,document:{querySelector:el,addEventListener(){}},crypto:{randomUUID:crypto.randomUUID},console,
  createRunProgress:()=>({open:async()=>{}}),setTimeout,clearTimeout,URL,location:{href:'http://localhost/uploads/'}};
 vm.createContext(ctx);vm.runInContext(source+'\nglobalThis.hooks={upload,openTask};',ctx);
 await new Promise(r=>setImmediate(r));
 vm.runInContext('me={role:"supplier"};task={task_id:"A",title:"A",current_version_id:"VA",versions:[]};epoch=1;',ctx);
 el('#upload-target').value='current';el('#file').files=[Object.freeze({name:'A.xlsx',size:123,lastModified:100})];
 return {ctx,el,calls};
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
 console.log('PASS: late duplicate/success discarded; immutable confirmation; stable retry key; file change invalidates intent');
})().catch(error=>{console.error(error);process.exitCode=1});
