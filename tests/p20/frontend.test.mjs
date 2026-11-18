import test from 'node:test';
import assert from 'node:assert/strict';
import {createClient,attachmentFilename,ApiError} from '../../web/api.js';
import {createExportActions,exportSelection,downloadAttachment} from '../../web/components/export-actions.js';
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const host=()=>({innerHTML:'',addEventListener(){},removeEventListener(){},replaceChildren(){},contains:()=>true});
const state=(id='A',version='V1')=>({taskId:id,task:{task_id:id},versionId:version,kind:'precheck',runId:id+'R',items:[{item_id:id+'i'}],run:{task_id:id,version_id:version,run_id:id+'R',kind:'precheck',status:'succeeded',items:[{item_id:id+'i',status:'succeeded'}]}});
const attachment=id=>({runId:id+'R',filename:'测试结果_审核工作簿.xlsx',blob:new Blob(['PK\x03\x04synthetic'])});

test('UTF8 Chinese and quoted filenames are parsed safely; traversal and non-attachment rejected',()=>{
 assert.equal(attachmentFilename("attachment; filename*=utf-8''%E6%B5%8B%E8%AF%95.xlsx"),'测试.xlsx');
 assert.equal(attachmentFilename('attachment; filename="audit.xlsx"'),'audit.xlsx');
 assert.throws(()=>attachmentFilename('attachment; filename="../../audit.xlsx"'),e=>e instanceof ApiError&&e.code==='missing_export_filename');
 assert.throws(()=>attachmentFilename('attachment; filename="..\\..\\win.ini"'),e=>e instanceof ApiError&&e.code==='missing_export_filename');
 assert.throws(()=>attachmentFilename('inline; filename="audit.xlsx"'),ApiError);
 assert.throws(()=>attachmentFilename('inline'),ApiError);
 assert.throws(()=>attachmentFilename('attachment; filename=""'),ApiError);
});
test('POST attachment uses current CSRF; JSON/HTML/non-XLSX do not become files',async()=>{
 const original=globalThis.fetch,doc=globalThis.document;
 globalThis.document={cookie:'csrftoken=rotated'};const calls=[];let response;
 globalThis.fetch=async(url,options)=>{calls.push({url,options});return response;};
 try{
  const client=createClient();response=new Response('PK\x03\x04synthetic',{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':"attachment; filename*=utf-8''%E6%B5%8B%E8%AF%95.xlsx",'X-Export-Run':'AR'}});
  const result=await client.exportFile('V1',{kind:'issues',run_id:'AR'});assert.equal(result.filename,'测试.xlsx');assert.equal(calls[0].options.headers['X-CSRFToken'],'rotated');assert.equal(calls[0].options.credentials,'same-origin');
  for(const status of [401,403,404,409,422,500,200]){
   response=new Response(JSON.stringify({error:{code:'refused',message:'拒绝'},request_id:'request'}),{status,headers:{'Content-Type':'application/json'}});
   await assert.rejects(client.exportFile('V1',{}),e=>e.code==='refused'&&e.status===(status===200?502:status));
  }
  response=new Response('<html>error</html>',{status:502});await assert.rejects(client.exportFile('V1',{}));
  response=new Response('{"error":"fake"}',{headers:{'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','Content-Disposition':'attachment; filename="bad.xlsx"'}});await assert.rejects(client.exportFile('V1',{}),e=>e.code==='unexpected_export_type');
  globalThis.fetch=async()=>{throw new TypeError('offline');};await assert.rejects(client.exportFile('V1',{}),e=>e.status===0&&e.code==='network_error');
 }finally{globalThis.fetch=original;globalThis.document=doc;}
});
test('export binds historical version and exact run, prevents double click and discards switched response',async()=>{
 const h=host(),pending=deferred(),calls=[],downloads=[];
 const ui=createExportActions(h,{client:{exportFile:(v,p)=>{calls.push([v,p]);return pending.promise;}},download:x=>downloads.push(x)});
 ui.update(state('A','historical'),'operator');const first=ui.start('precheck');await ui.start('issues');assert.equal(calls.length,1);assert.deepEqual(calls[0],['historical',{kind:'precheck',run_id:'AR'}]);
 ui.update(state('B'),'operator');pending.resolve(attachment('A'));await first;assert.equal(downloads.length,0);assert.doesNotMatch(h.innerHTML,/已发起下载/);ui.destroy();
});
test('expert snapshot and decision binding; workbook is disabled until layout confirmed',async()=>{
 const s=state();s.kind=s.run.kind='expert_check';s.run.submission_id='S';
 assert.match(exportSelection(s,'expert').reason,/提交快照/);
 s.history={versions:[{version_id:'V1',submissions:[{submission_id:'S',decisions:[{decision_id:'D'}]}]}]};s.decisionId='D';
 let calls=[];const h=host(),ui=createExportActions(h,{client:{exportFile:async(v,p)=>{calls.push([v,p]);return attachment('A');}},download(){}});
 ui.update(s,'expert');assert.match(h.innerHTML,/expert_layout_pending/);await ui.start('precheck');assert.equal(calls.length,0);
 await ui.start('issues');assert.deepEqual(calls[0],['V1',{kind:'issues',run_id:'AR',submission_id:'S',decision_id:'D'}]);ui.destroy();
});
test('roles, identity mismatch, unfinished and incomplete result set disable export',()=>{
 for(const change of [s=>s.run.version_id='WRONG',s=>s.run.status='running',s=>s.run.status='superseded',s=>s.run.items=[],s=>s.progressError=Error('failed'),s=>s.run.kind='expert_check']){
  const s=state();change(s);assert.ok(exportSelection(s,'operator').reason);
 }
 assert.ok(exportSelection(state(),'supplier').reason);
});
test('mismatched attachment, errors and destruction do not download or show success',async()=>{
 let downloaded=0;const h=host(),pending=deferred();let response=attachment('B');
 const ui=createExportActions(h,{client:{exportFile:async()=>response},download(){downloaded++;}});ui.update(state(),'operator');await ui.start('issues');assert.equal(downloaded,0);assert.match(h.innerHTML,/附件执行引用/);
 response=Promise.reject(new ApiError(409,'run_superseded','执行已作废'));await ui.start('issues');assert.doesNotMatch(h.innerHTML,/已发起下载/);ui.destroy();
 const later=createExportActions(host(),{client:{exportFile:()=>pending.promise},download(){downloaded++;}});later.update(state(),'operator');const old=later.start('issues');later.destroy();pending.resolve(attachment('A'));await old;assert.equal(downloaded,0);
});
test('Blob URL is released after successful click and after click error',()=>{
 const events=[];const env={URL:{createObjectURL:()=>{events.push('create');return 'blob:local';},revokeObjectURL:u=>events.push(u)},document:{body:{append:()=>events.push('append')},createElement:()=>({click(){events.push('click');},remove(){events.push('remove');}})},setTimeout:fn=>fn()};
 downloadAttachment(attachment('A'),env);assert.deepEqual(events,['create','append','click','remove','blob:local']);
 events.length=0;env.document.createElement=()=>({click(){throw Error('click failed');},remove(){events.push('remove');}});
 assert.throws(()=>downloadAttachment(attachment('A'),env),/click failed/);assert.deepEqual(events,['create','append','remove','blob:local']);
});
test('same-version reload entering loading state cancels in-flight export download',async()=>{
 const h=host(),pending=deferred(),downloads=[];
 const ui=createExportActions(h,{client:{exportFile:()=>pending.promise},download:x=>downloads.push(x)});
 const ready=state('A','V1');ui.update(ready,'operator');
 const active=ui.start('issues');
 ui.update({...ready,loading:true},'operator');
 pending.resolve(attachment('A'));await active;
 assert.equal(downloads.length,0);assert.doesNotMatch(h.innerHTML,/已发起下载/);ui.destroy();
});
test('ensureCsrf re-reads cleared cookie and logout fetches CSRF when cookie is missing',async()=>{
 const original=globalThis.fetch,doc=globalThis.document;
 const calls=[];let cookie='';
 globalThis.document={get cookie(){return cookie;}};
 globalThis.fetch=async(url,options)=>{
  calls.push({url:String(url),method:options.method,headers:options.headers||{}});
  if(String(url).endsWith('/me')){cookie='csrftoken=fresh-token';return new Response(JSON.stringify({user:null}),{status:200,headers:{'Content-Type':'application/json'}});}
  return new Response(JSON.stringify({ok:true}),{status:200,headers:{'Content-Type':'application/json'}});
 };
 try{
  const client=createClient();
  await client.logout();
  assert.deepEqual(calls.map(c=>[c.method,c.url]),[['GET','/api/v1/me'],['POST','/api/v1/auth/logout']]);
  assert.equal(calls[1].headers['X-CSRFToken'],'fresh-token');
  cookie='csrftoken=second-token';
  await client.claimTask('T1','k1');
  assert.equal(calls[2].headers['X-CSRFToken'],'second-token');
  cookie='';
  await client.claimTask('T1','k2');
  assert.equal(calls[3].url,'/api/v1/me');
  assert.equal(calls[4].headers['X-CSRFToken'],'fresh-token');
 }finally{globalThis.fetch=original;globalThis.document=doc;}
});
