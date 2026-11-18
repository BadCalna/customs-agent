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
test('missing current version in history exits loading with explicit error and failTask clears pending state',async()=>{
  const a=api();a.getHistory=async id=>({task_id:id,versions:[]});
  const ctx=createWorkbenchContext(a);await ctx.openTask('A');
  assert.equal(ctx.store.get().loading,false);assert.match(ctx.store.get().error.message,/缺少当前版本/);
  ctx.beginTask('B');assert.equal(ctx.store.get().loading,true);
  ctx.failTask('B',new Error('读取失败'));assert.equal(ctx.store.get().loading,false);assert.equal(ctx.store.get().error.message,'读取失败');
  ctx.destroy();
});
test('mountWorkbench reconciles refreshed V3, recovers task A after B fails, and keeps card placeholder',async()=>{
  const {mountWorkbench}=await import('../../web/workbench-live.js');
  const original={fetch:globalThis.fetch,document:globalThis.document,location:globalThis.location,addEventListener:globalThis.addEventListener};
  // DOM-lite shim using node-level stubs for workbench IDs
  const elements=new Map();
  const root={
    _html:'',listeners:new Map(),
    set innerHTML(v){
      this._html=v;elements.clear();
      for(const m of v.matchAll(/id="([^"]+)"/g)){
        const el=makeEl(m[1]);elements.set(m[1],el);
      }
    },
    get innerHTML(){return this._html;},
    querySelector(sel){
      if(sel.startsWith('#'))return elements.get(sel.slice(1))||null;
      return null;
    },
  };
  function makeEl(id){
    const listeners=new Map();
    return {
      id,hidden:false,disabled:false,_html:'',_text:'',value:'',dataset:{},children:[],listeners,
      set innerHTML(v){this._html=v;},get innerHTML(){return this._html;},
      set textContent(v){this._text=v;},get textContent(){return this._text;},
      addEventListener(k,v){listeners.set(k,v);},
      removeEventListener(k){listeners.delete(k);},
      contains:()=>true,
      replaceChildren(...items){this.children=items;},
      focus(){},scrollIntoView(){},
      querySelector:()=>null,querySelectorAll:()=>[],
    };
  }
  const jsonResp=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  let listVersion='A2',failB=false;
  const makeRun=(v,r)=>({run_id:r,task_id:'A',version_id:v,kind:'precheck',status:'succeeded',is_current:true,
    progress:{total:1,queued:0,running:0,succeeded:1,failed:0},items:[{item_id:v+'i',status:'succeeded',source_location:{sheet_name:'S',row_number:2},attempts:1}]});
  const makeTaskA=v=>({task_id:'A',title:'任务甲',stage:'awaiting_confirmation',current_version_id:v,current_version_number:Number(v.slice(1)),
    latest_run_id:'A-pre'+v.slice(1),latest_run_kind:'precheck',latest_run_status:'succeeded',revision:1,allowed_actions:['read','confirmation.write','submission.create'],
    versions:[{version_id:'A2',number:2,original_filename:'v2.xlsx',is_current:v==='A2'},...(v==='A3'?[{version_id:'A3',number:3,original_filename:'v3.xlsx',is_current:true}]:[])]});
  const makeTaskB=()=>({task_id:'B',title:'任务乙',stage:'awaiting_confirmation',current_version_id:'B1',current_version_number:1,
    latest_run_id:'B-pre1',latest_run_kind:'precheck',latest_run_status:'succeeded',revision:1,allowed_actions:['read'],
    versions:[{version_id:'B1',number:1,original_filename:'b1.xlsx',is_current:true}]});
  globalThis.document={cookie:'csrftoken=test',createElement:()=>makeEl('dyn'),activeElement:null,getElementById:()=>null};
  globalThis.location={search:''};
  globalThis.addEventListener=()=>{};
  globalThis.fetch=async(url)=>{
    const u=String(url);
    if(u.endsWith('/me'))return jsonResp({username:'op',display_name:'业务员',role:'operator',role_label:'业务员'});
    if(u.includes('/tasks?'))return jsonResp({items:[makeTaskA(listVersion),makeTaskB()],next_cursor:null});
    if(u.endsWith('/tasks/A'))return jsonResp(makeTaskA(listVersion));
    if(u.endsWith('/tasks/B')){if(failB)return jsonResp({error:{code:'server_error',message:'B失败'}},500);return jsonResp(makeTaskB());}
    if(u.endsWith('/tasks/A/history'))return jsonResp({task_id:'A',versions:[
      {version_id:'A2',runs:[makeRun('A2','A-pre2')],submissions:[]},
      ...(listVersion==='A3'?[{version_id:'A3',runs:[makeRun('A3','A-pre3')],submissions:[]}]:[]),
    ]});
    if(u.includes('/versions/A2/items?'))return jsonResp({items:[{item_id:'A2i',task_id:'A',version_id:'A2',name:'甲V2',supplier_hs_raw:'1',image_status:'absent',revision:1,source_location:{sheet_name:'S',row_number:2}}],next_cursor:null});
    if(u.includes('/versions/A3/items?'))return jsonResp({items:[{item_id:'A3i',task_id:'A',version_id:'A3',name:'甲V3',supplier_hs_raw:'1',image_status:'absent',revision:1,source_location:{sheet_name:'S',row_number:2}}],next_cursor:null});
    if(u.endsWith('/runs/A-pre2'))return jsonResp(makeRun('A2','A-pre2'));
    if(u.endsWith('/runs/A-pre3'))return jsonResp(makeRun('A3','A-pre3'));
    return jsonResp({error:{code:'not_found',message:'not found'}},404);
  };
  try{
    const wb=await mountWorkbench(root);
    const until=async(pred,label)=>{for(let i=0;i<60;i++){if(pred())return;await new Promise(r=>setImmediate(r));}assert.fail('timeout: '+label);};
    await until(()=>/任务甲/.test(elements.get('wb-tasks')?.innerHTML||''),'initial list');
    assert.match(elements.get('wb-card').innerHTML,/选择一件商品/);
    // Select A
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'A'},disabled:false})}});
    await until(()=>/V2 · 1 件商品/.test(elements.get('wb-summary')?.textContent||''),'A V2 loaded');
    // Refresh list when server upgraded A to V3 -> right panel reconciles to V3
    listVersion='A3';
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{action:'refresh'},disabled:false})}});
    await until(()=>/V3 · 1 件商品/.test(elements.get('wb-summary')?.textContent||''),'reconciled to V3');
    // Click B which fails -> task-list rolls back to A AND workbench recovers A instead of hanging on B
    failB=true;
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'B'},disabled:false})}});
    await until(()=>elements.get('wb-title').textContent==='任务甲'&&/V3 · 1 件商品/.test(elements.get('wb-summary').textContent),'recovered A after B failed');
    assert.doesNotMatch(elements.get('wb-title').textContent,/正在读取/);
    wb.destroy();
  }finally{
    Object.assign(globalThis,original);
  }
});
test('missing current version in task.versions exits loading with explicit error',async()=>{
  const a=api();a.getTask=async id=>({...task(id),versions:[{version_id:id+'1'}]});
  const ctx=createWorkbenchContext(a);await ctx.openTask('A');
  assert.equal(ctx.store.get().loading,false);assert.match(ctx.store.get().error.message,/缺少当前版本/);
  ctx.destroy();
});
test('mountWorkbench recovers after failed reconcileSelection and blocks submit without submission.create',async()=>{
  const {mountWorkbench}=await import('../../web/workbench-live.js');
  const original={fetch:globalThis.fetch,document:globalThis.document,location:globalThis.location,addEventListener:globalThis.addEventListener};
  const elements=new Map();
  const makeEl=id=>{
    const listeners=new Map();
    return {
      id,hidden:false,disabled:false,_html:'',_text:'',value:'',dataset:{},children:[],listeners,
      set innerHTML(v){this._html=v;},get innerHTML(){return this._html;},
      set textContent(v){this._text=v;},get textContent(){return this._text;},
      addEventListener(k,v){listeners.set(k,v);},
      removeEventListener(k){listeners.delete(k);},
      contains:()=>true,replaceChildren(...items){this.children=items;},focus(){},scrollIntoView(){},
      querySelector:()=>null,querySelectorAll:()=>[],
    };
  };
  const root={
    _html:'',listeners:new Map(),
    set innerHTML(v){this._html=v;elements.clear();for(const m of v.matchAll(/id="([^"]+)"/g))elements.set(m[1],makeEl(m[1]));},
    get innerHTML(){return this._html;},
    querySelector(sel){return sel.startsWith('#')?elements.get(sel.slice(1))||null:null;},
  };
  const jsonResp=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  let stage='awaiting_confirmation',failA=false,getItemCalls=0,submitCalls=0;
  const makeRun=()=>({run_id:'A-pre2',task_id:'A',version_id:'A2',kind:'precheck',status:'succeeded',is_current:true,
    progress:{total:1,queued:0,running:0,succeeded:1,failed:0},items:[{item_id:'A2i',status:'succeeded',source_location:{sheet_name:'S',row_number:2},attempts:1}]});
  const makeTaskA=()=>({task_id:'A',title:'任务甲',stage,current_version_id:'A2',current_version_number:2,
    latest_run_id:'A-pre2',latest_run_kind:'precheck',latest_run_status:'succeeded',revision:1,allowed_actions:['read','confirmation.write'],
    versions:[{version_id:'A2',number:2,original_filename:'v2.xlsx',is_current:true}]});
  globalThis.document={cookie:'csrftoken=test',createElement:()=>makeEl('dyn'),activeElement:null,getElementById:()=>null};
  globalThis.location={search:''};
  globalThis.addEventListener=()=>{};
  globalThis.fetch=async(url)=>{
    const u=String(url);
    if(u.endsWith('/me'))return jsonResp({username:'op',display_name:'业务员',role:'operator',role_label:'业务员'});
    if(u.includes('/tasks?'))return jsonResp({items:[makeTaskA()],next_cursor:null});
    if(u.endsWith('/tasks/A')){if(failA)return jsonResp({error:{code:'server_error',message:'A暂时失败'}},500);return jsonResp(makeTaskA());}
    if(u.endsWith('/tasks/A/history'))return jsonResp({task_id:'A',versions:[{version_id:'A2',runs:[makeRun()],submissions:[]}]});
    if(u.includes('/versions/A2/items?'))return jsonResp({items:[{item_id:'A2i',task_id:'A',version_id:'A2',name:'甲V2',supplier_hs_raw:'1',image_status:'absent',revision:1,source_location:{sheet_name:'S',row_number:2}}],next_cursor:null});
    if(u.includes('/versions/A2/items/')){getItemCalls++;return jsonResp({});}
    if(u.endsWith('/submissions')){submitCalls++;return jsonResp({});}
    if(u.endsWith('/runs/A-pre2'))return jsonResp(makeRun());
    return jsonResp({error:{code:'not_found',message:'not found'}},404);
  };
  try{
    const wb=await mountWorkbench(root);
    const until=async(pred,label)=>{for(let i=0;i<60;i++){if(pred())return;await new Promise(r=>setImmediate(r));}assert.fail('timeout: '+label);};
    await until(()=>/任务甲/.test(elements.get('wb-tasks')?.innerHTML||''),'initial list');
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'A'},disabled:false})}});
    await until(()=>/V2 · 1 件商品/.test(elements.get('wb-summary')?.textContent||''),'A loaded');
    await elements.get('wb-submit').onclick();
    assert.equal(getItemCalls,0);assert.equal(submitCalls,0);
    // Reconcile fails once -> failTask leaves error; subsequent list refresh recovers A
    stage='returned';failA=true;
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{action:'refresh'},disabled:false})}});
    await until(()=>/A暂时失败/.test(elements.get('wb-context-error')?.innerHTML||''),'reconcile failed');
    failA=false;
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{action:'refresh'},disabled:false})}});
    await until(()=>elements.get('wb-title').textContent==='任务甲'&&/V2 · 1 件商品/.test(elements.get('wb-summary').textContent),'recovered A on refresh');
    assert.equal(elements.get('wb-context-error').innerHTML,'');
    wb.destroy();
  }finally{
    Object.assign(globalThis,original);
  }
});

test('mountWorkbench preserves operator selected item and P11 draft across reconcileSelection and #wb-refresh',async()=>{
  const {mountWorkbench}=await import('../../web/workbench-live.js');
  const original={fetch:globalThis.fetch,document:globalThis.document,location:globalThis.location,addEventListener:globalThis.addEventListener};
  const elements=new Map();
  const makeEl=id=>{
    const listeners=new Map();
    return {
      id,hidden:false,disabled:false,_html:'',_text:'',value:'',dataset:{},children:[],listeners,
      set innerHTML(v){this._html=v;if(id==='wb-toolbar')for(const m of v.matchAll(/id="([^"]+)"/g))elements.set(m[1],makeEl(m[1]));},get innerHTML(){return this._html;},
      set textContent(v){this._text=v;},get textContent(){return this._text;},
      addEventListener(k,v){listeners.set(k,v);},
      removeEventListener(k){listeners.delete(k);},
      contains:()=>true,replaceChildren(...items){this.children=items;},focus(){},scrollIntoView(){},
      querySelector:()=>null,querySelectorAll:()=>[],remove(){},
    };
  };
  const root={
    _html:'',listeners:new Map(),
    set innerHTML(v){this._html=v;elements.clear();for(const m of v.matchAll(/id="([^"]+)"/g))elements.set(m[1],makeEl(m[1]));},
    get innerHTML(){return this._html;},
    querySelector(sel){return sel.startsWith('#')?elements.get(sel.slice(1))||null:null;},
  };
  const jsonResp=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  let stage='awaiting_confirmation',revision=1,failTaskA=false,confirmPayloads=[];
  const makeRun=()=>({run_id:'A-pre2',task_id:'A',version_id:'A2',kind:'precheck',status:'succeeded',is_current:true,
    progress:{total:1,queued:0,running:0,succeeded:1,failed:0},items:[{item_id:'A2i',status:'succeeded',source_location:{sheet_name:'S',row_number:2},attempts:1}]});
  const makeTaskA=()=>({task_id:'A',title:'任务甲',stage,current_version_id:'A2',current_version_number:2,
    latest_run_id:'A-pre2',latest_run_kind:'precheck',latest_run_status:'succeeded',revision:1,allowed_actions:['read','confirmation.write'],
    versions:[{version_id:'A2',number:2,original_filename:'v2.xlsx',is_current:true}]});
  const makeItem=()=>({item_id:'A2i',task_id:'A',version_id:'A2',name:'甲V2',supplier_hs_raw:'1',image_status:'absent',revision,source_location:{sheet_name:'S',row_number:2},facts:{name:'甲V2'}});
  globalThis.document={cookie:'csrftoken=testtoken',createElement:()=>makeEl('dyn'),activeElement:null,getElementById:()=>null};
  globalThis.location={search:''};
  globalThis.addEventListener=()=>{};
  globalThis.fetch=async(url,init)=>{
    const u=String(url);
    if(u.endsWith('/me'))return jsonResp({username:'op',display_name:'业务员',role:'operator',role_label:'业务员'});
    if(u.includes('/tasks?'))return jsonResp({items:[makeTaskA()],next_cursor:null});
    if(u.endsWith('/tasks/A')){if(failTaskA)return jsonResp({error:{code:'server_error',message:'A暂时失败'}},502);return jsonResp(makeTaskA());}
    if(u.endsWith('/tasks/A/history'))return jsonResp({task_id:'A',versions:[{version_id:'A2',runs:[makeRun()],submissions:[]}]});
    if(u.includes('/versions/A2/items?'))return jsonResp({items:[makeItem()],next_cursor:null});
    if(u.endsWith('/versions/A2/items/A2i'))return jsonResp(makeItem());
    if(u.endsWith('/versions/A2/items/A2i/confirmation')){confirmPayloads.push(JSON.parse(init.body));return jsonResp({revision:revision+1,status:'confirmed'});}
    if(u.endsWith('/runs/A-pre2'))return jsonResp(makeRun());
    return jsonResp({error:{code:'not_found',message:'not found'}},404);
  };
  try{
    const wb=await mountWorkbench(root);
    const until=async(pred,label)=>{for(let i=0;i<60;i++){if(pred())return;await new Promise(r=>setImmediate(r));}assert.fail('timeout: '+label);};
    await until(()=>/任务甲/.test(elements.get('wb-tasks')?.innerHTML||''),'initial list');
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'A'},disabled:false})}});
    await until(()=>/V2 · 1 件商品/.test(elements.get('wb-summary')?.textContent||''),'A loaded');
    elements.get('wb-items').listeners.get('click')({target:{closest:sel=>sel==='[data-item-id]'?{dataset:{itemId:'A2i'}}:null}});
    await until(()=>elements.get('wb-review').children.length===1,'item panel mounted');
    const reviewRoot=elements.get('wb-review').children[0];
    const hsInput={dataset:{raField:'confirm-hs',item:'A2i'},value:'8501109900'};
    const reasonInput={dataset:{raField:'confirm-reason',item:'A2i'},value:'保留草稿测试'};
    reviewRoot.listeners.get('input')({target:{closest:sel=>sel==='[data-ra-field]'?hsInput:null}});
    reviewRoot.listeners.get('input')({target:{closest:sel=>sel==='[data-ra-field]'?reasonInput:null}});

    // Trigger list refresh reconciliation (stage changed) -> item & draft survive
    stage='returned';revision=2;
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{action:'refresh'},disabled:false})}});
    await until(()=>!elements.get('wb-review').hidden && /expected_revision=2/.test(reviewRoot.innerHTML),'reconciled preserving draft');
    assert.equal(elements.get('wb-review').children[0],reviewRoot);

    // Click #wb-refresh -> item & draft survive as well (including after a transient 502 failure)
    failTaskA=true;
    elements.get('wb-refresh').listeners.get('click')();
    await until(()=>/A暂时失败/.test(elements.get('wb-context-error')?.innerHTML||''),'transient refresh failed');
    assert.equal(elements.get('wb-review').children[0],reviewRoot);
    assert.equal(elements.get('wb-review').hidden,true);

    failTaskA=false;revision=3;
    elements.get('wb-refresh').listeners.get('click')();
    await until(()=>!elements.get('wb-review').hidden && /expected_revision=3/.test(reviewRoot.innerHTML),'#wb-refresh preserved draft');
    assert.equal(elements.get('wb-review').children[0],reviewRoot);

    // Click confirm -> sends expected_revision=3 and preserved draft
    reviewRoot.listeners.get('click')({target:{closest:sel=>sel==='[data-ra-act]'?{dataset:{raAct:'confirm',item:'A2i'}}:null}});
    await until(()=>confirmPayloads.length===1,'confirm submitted');
    assert.equal(confirmPayloads[0].expected_revision,3);
    assert.equal(confirmPayloads[0].confirmed_hs,'8501109900');
    assert.equal(confirmPayloads[0].reason,'保留草稿测试');
    wb.destroy();
  }finally{
    Object.assign(globalThis,original);
  }
});

test('concurrent list refresh during in-flight getHistory defers reconcileSelection instead of aborting preserveItem',async()=>{
  const {mountWorkbench}=await import('../../web/workbench-live.js');
  const original={fetch:globalThis.fetch,document:globalThis.document,location:globalThis.location,addEventListener:globalThis.addEventListener};
  const elements=new Map();
  const makeEl=id=>{
    const listeners=new Map();
    return {
      id,hidden:false,disabled:false,_html:'',_text:'',value:'',dataset:{},children:[],listeners,
      set innerHTML(v){this._html=v;if(id==='wb-toolbar')for(const m of v.matchAll(/id="([^"]+)"/g))elements.set(m[1],makeEl(m[1]));},get innerHTML(){return this._html;},
      set textContent(v){this._text=v;},get textContent(){return this._text;},
      addEventListener(k,v){listeners.set(k,v);},
      removeEventListener(k){listeners.delete(k);},
      contains:()=>true,replaceChildren(...items){this.children=items;},focus(){},scrollIntoView(){},
      querySelector:()=>null,querySelectorAll:()=>[],remove(){},
    };
  };
  const root={
    _html:'',listeners:new Map(),
    set innerHTML(v){this._html=v;elements.clear();for(const m of v.matchAll(/id="([^"]+)"/g))elements.set(m[1],makeEl(m[1]));},
    get innerHTML(){return this._html;},
    querySelector(sel){return sel.startsWith('#')?elements.get(sel.slice(1))||null:null;},
  };
  const jsonResp=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
  let holdHistory=null,stage='awaiting_confirmation',historyCalls=0;
  const makeRun=()=>({run_id:'A-pre2',task_id:'A',version_id:'A2',kind:'precheck',status:'succeeded',is_current:true,
    progress:{total:1,queued:0,running:0,succeeded:1,failed:0},items:[{item_id:'A2i',status:'succeeded',source_location:{sheet_name:'S',row_number:2},attempts:1}]});
  const makeTaskA=()=>({task_id:'A',title:'任务甲',stage,current_version_id:'A2',current_version_number:2,
    latest_run_id:'A-pre2',latest_run_kind:'precheck',latest_run_status:'succeeded',revision:1,allowed_actions:['read','confirmation.write'],
    versions:[{version_id:'A2',number:2,original_filename:'v2.xlsx',is_current:true}]});
  const makeItem=()=>({item_id:'A2i',task_id:'A',version_id:'A2',name:'甲V2',supplier_hs_raw:'1',image_status:'absent',revision:1,source_location:{sheet_name:'S',row_number:2},facts:{name:'甲V2'}});
  globalThis.document={cookie:'csrftoken=testtoken',createElement:()=>makeEl('dyn'),activeElement:null,getElementById:()=>null};
  globalThis.location={search:''};
  globalThis.addEventListener=()=>{};
  globalThis.fetch=async(url)=>{
    const u=String(url);
    if(u.endsWith('/me'))return jsonResp({username:'op',display_name:'业务员',role:'operator',role_label:'业务员'});
    if(u.includes('/tasks?'))return jsonResp({items:[makeTaskA()],next_cursor:null});
    if(u.endsWith('/tasks/A'))return jsonResp(makeTaskA());
    if(u.endsWith('/tasks/A/history')){
      historyCalls++;
      if(holdHistory){await holdHistory;}
      return jsonResp({task_id:'A',versions:[{version_id:'A2',runs:[makeRun()],submissions:[]}]});
    }
    if(u.includes('/versions/A2/items?'))return jsonResp({items:[makeItem()],next_cursor:null});
    if(u.endsWith('/versions/A2/items/A2i'))return jsonResp(makeItem());
    if(u.endsWith('/runs/A-pre2'))return jsonResp(makeRun());
    return jsonResp({error:{code:'not_found',message:'not found'}},404);
  };
  try{
    const wb=await mountWorkbench(root);
    const until=async(pred,label)=>{for(let i=0;i<60;i++){if(pred())return;await new Promise(r=>setImmediate(r));}assert.fail('timeout: '+label);};
    await until(()=>/任务甲/.test(elements.get('wb-tasks')?.innerHTML||''),'initial list');
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'A'},disabled:false})}});
    await until(()=>/V2 · 1 件商品/.test(elements.get('wb-summary')?.textContent||''),'A loaded');
    elements.get('wb-items').listeners.get('click')({target:{closest:sel=>sel==='[data-item-id]'?{dataset:{itemId:'A2i'}}:null}});
    await until(()=>elements.get('wb-review').children.length===1,'item mounted');
    const reviewRoot=elements.get('wb-review').children[0];
    const hsInput={dataset:{raField:'confirm-hs',item:'A2i'},value:'8501109900'};
    reviewRoot.listeners.get('input')({target:{closest:sel=>sel==='[data-ra-field]'?hsInput:null}});

    // Start #wb-refresh while holding getHistory in flight (s.task is set, s.history is null)
    let releaseHistory;
    holdHistory=new Promise(r=>{releaseHistory=r;});
    elements.get('wb-refresh').listeners.get('click')();
    await until(()=>historyCalls===2,'history in flight during refresh');
    // Concurrent list refresh with unchanged stage must defer instead of calling selectTask(A) and aborting openTask
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{action:'refresh'},disabled:false})}});
    await new Promise(r=>setImmediate(r));
    assert.equal(historyCalls,2);
    holdHistory=null;
    releaseHistory();
    await until(()=>!elements.get('wb-review').hidden && /expected_revision=1/.test(reviewRoot.innerHTML),'refresh finished preserving item and draft');
    assert.equal(elements.get('wb-review').children[0],reviewRoot);
    assert.equal(historyCalls,2);
    wb.destroy();
  }finally{
    Object.assign(globalThis,original);
  }
});
