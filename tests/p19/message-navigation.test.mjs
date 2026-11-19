import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {createMessageList} from '../../web/components/message-list.js';

const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {let resolve;const promise=new Promise(ok=>{resolve=ok;});return {promise,resolve};};
const msg = {message_id:'M1',task_id:'A',version_id:'A1',submission_id:'S1',decision_id:null,kind:'submission.created',
  title:'版本一消息',body:'查看原提交',is_read:false,read_at:null,created_at:'2026-11-19T03:00:00Z',target_url:'/?task=A&version=A1'};
const task = (id='A',current='A2') => ({task_id:id,title:'任务'+id,stage:'submitted',revision:1,current_version_id:current,
  latest_run_id:null,latest_run_kind:'precheck',allowed_actions:['read'],result_mode:'live',supplier:'供应商',
  versions:[{version_id:id+'1',number:1},{version_id:id+'2',number:2},{version_id:id+'3',number:3}]});
const history = id => ({task_id:id,audit_events:[],versions:[1,2,3].map(n=>({version_id:id+n,runs:[],submissions:[],confirmations:[]}))});

test('production inbox and login deep link open the exact historical version without an approval form',async()=>{
  const elements=new Map(),listeners=new Map();
  const element=selector=>{
    if(!elements.has(selector))elements.set(selector,{hidden:false,innerHTML:'',textContent:'',dataset:{},listeners:new Map(),
      addEventListener(k,fn){this.listeners.set(k,fn);},removeEventListener(k){this.listeners.delete(k);},replaceChildren(){this.innerHTML='';},setAttribute(){}});
    return elements.get(selector);
  };
  const api={me:async()=>null,listTasks:async()=>({items:[],next_cursor:null}),listMessages:async()=>({items:[msg],next_cursor:null,unread_count:1}),
    markMessageRead:async()=>({...msg,is_read:true}),getTask:async id=>task(id,id+'2'),listItems:async()=>({items:[],next_cursor:null}),
    taskHistory:async id=>history(id),getImport:async()=>{throw {status:404};}};
  const source=readFileSync(new URL('../../web/production.js',import.meta.url),'utf8').replace(/^import .*;\r?\n/gm,'');
  const ctx=vm.createContext({createClient:()=>api,createMessageList,document:{querySelector:s=>s==='#decision-form'?null:element(s),addEventListener:(k,fn)=>listeners.set(k,fn)},
    location:{search:'?task=A&version=A1'},URLSearchParams,crypto:{randomUUID},FormData:Map,console,setTimeout:()=>1,clearTimeout(){}});
  vm.runInContext(source+'\nglobalThis.hooks={state,showSession,openRequestedVersion};',ctx);await flush();
  ctx.hooks.state.me={id:'E1',username:'expert',role:'expert'};ctx.hooks.showSession();await flush();
  await ctx.hooks.openRequestedVersion();assert.equal(ctx.hooks.state.versionId,'A1');assert.doesNotMatch(element('#task-detail').innerHTML,/id="decision-form"/);
  await vm.runInContext('openTask("B")',ctx);assert.equal(ctx.hooks.state.task.task_id,'B');
  element('#messages-area').listeners.get('click')({target:{closest:()=>({disabled:false,dataset:{messageAction:'open',messageId:'M1'}})}});
  for(let n=0;n<8;n++)await flush();
  assert.equal(ctx.hooks.state.task.task_id,'A');assert.equal(ctx.hooks.state.versionId,'A1');
  api.getTask=async id=>({...task(id,id+'2'),versions:[{version_id:id+'2',number:2}]});
  await assert.rejects(()=>ctx.hooks.openRequestedVersion(),/版本不属于当前任务/);
  assert.notEqual(ctx.hooks.state.versionId,'A2');assert.equal(ctx.hooks.state.reviewLoading,true);
  ctx.hooks.state.me=null;ctx.hooks.showSession();assert.equal(element('#messages-area').innerHTML,'');assert.equal(element('#messages-area').hidden,true);
});

for(const navigation of ['task','version']) {
  test(`late message navigation cannot override a newer manual ${navigation} choice on the same task`,async()=>{
    const {mountWorkbench}=await import('../../web/workbench-live.js');
    const original={fetch:globalThis.fetch,document:globalThis.document,location:globalThis.location,addEventListener:globalThis.addEventListener,removeEventListener:globalThis.removeEventListener};
    const elements=new Map(),blocked=deferred();let didBlock=false,held=false;
    function register(html) {for(const m of html.matchAll(/id="([^"]+)"/g))elements.set(m[1],makeEl(m[1]));}
    function makeEl(id) {
      return {id,hidden:false,disabled:false,_html:'',textContent:'',dataset:{},listeners:new Map(),
        set innerHTML(value){this._html=value;register(value);},get innerHTML(){return this._html;},
        addEventListener(k,fn){this.listeners.set(k,fn);},removeEventListener(k){this.listeners.delete(k);},replaceChildren(){this._html='';},contains:()=>true,
        setAttribute(){},querySelector:()=>null,querySelectorAll:()=>[],focus(){},scrollIntoView(){}};
    }
    const root={_html:'',set innerHTML(value){this._html=value;elements.clear();register(value);},get innerHTML(){return this._html;},querySelector:selector=>elements.get(selector.slice(1))||null};
    const json=data=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
    globalThis.document={cookie:'csrftoken=test',createElement:()=>makeEl('new'),activeElement:null,getElementById:()=>null};
    globalThis.location={search:''};globalThis.addEventListener=()=>{};globalThis.removeEventListener=()=>{};
    globalThis.fetch=async url=>{
      const u=String(url);
      if(u.endsWith('/me'))return json({id:'E1',username:'expert',role:'expert'});
      if(u.includes('/messages'))return json({items:[msg],next_cursor:null,unread_count:1});
      if(u.includes('/tasks?'))return json({items:[task('A','A2')],next_cursor:null});
      if(u.endsWith('/tasks/A'))return json(task('A','A2'));
      if(u.endsWith('/history')) {
        if(navigation==='task'&&!held){held=didBlock=true;return blocked.promise;}
        return json(history('A'));
      }
      if(u.includes('/versions/')) {
        if(navigation==='version'&&u.includes('/versions/A2/')&&!held){held=didBlock=true;return blocked.promise;}
        return json({items:[],next_cursor:null});
      }
      throw new Error('unexpected '+u);
    };
    const until=async predicate=>{for(let n=0;n<60;n++){if(predicate())return;await flush();}assert.fail('navigation did not settle');};
    let wb;
    try {
      wb=await mountWorkbench(root);await flush();
      elements.get('wb-messages').listeners.get('click')({target:{closest:()=>({dataset:{messageAction:'open',messageId:'M1'},disabled:false})}});
      await until(()=>didBlock);
      const expected=navigation==='task'?'V2':'V3';
      if(navigation==='task')elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'A'},disabled:false})}});
      else elements.get('wb-version').listeners.get('change')({target:{value:'A3'}});
      await until(()=>elements.get('wb-summary').textContent.startsWith(expected));
      blocked.resolve(json(navigation==='task'?history('A'):{items:[],next_cursor:null}));
      for(let n=0;n<10;n++)await flush();
      assert.ok(elements.get('wb-summary').textContent.startsWith(expected),`manual ${navigation} was replaced: ${elements.get('wb-summary').textContent}`);
    } finally {wb?.destroy();Object.assign(globalThis,original);}
  });
}

test('workbench message opens a historical version and list refresh cannot replace it with another selected task or newer version',async()=>{
  const {mountWorkbench}=await import('../../web/workbench-live.js');
  const original={fetch:globalThis.fetch,document:globalThis.document,location:globalThis.location,addEventListener:globalThis.addEventListener,removeEventListener:globalThis.removeEventListener};
  const elements=new Map();
  const makeEl=id=>({id,hidden:false,disabled:false,innerHTML:'',textContent:'',dataset:{},listeners:new Map(),
    addEventListener(k,fn){this.listeners.set(k,fn);},removeEventListener(k){this.listeners.delete(k);},replaceChildren(){this.innerHTML='';},contains:()=>true,
    setAttribute(){},querySelector:()=>null,querySelectorAll:()=>[],focus(){},scrollIntoView(){}});
  const root={_html:'',set innerHTML(value){this._html=value;elements.clear();for(const match of value.matchAll(/id="([^"]+)"/g))elements.set(match[1],makeEl(match[1]));},
    get innerHTML(){return this._html;},querySelector:sel=>elements.get(sel.slice(1))||null};
  let current='A2',messageVersion='A1';
  globalThis.document={cookie:'csrftoken=test',createElement:()=>makeEl('new'),activeElement:null,getElementById:()=>null};
  globalThis.location={search:''};globalThis.addEventListener=()=>{};globalThis.removeEventListener=()=>{};
  globalThis.fetch=async(url)=>{
    const u=String(url),json=data=>new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
    if(u.endsWith('/me'))return json({id:'E1',username:'expert',role:'expert'});
    if(u.includes('/messages'))return json({items:[{...msg,version_id:messageVersion}],next_cursor:null,unread_count:1});
    if(u.includes('/tasks?'))return json({items:[task('A',current),task('B','B2')],next_cursor:null});
    if(u.endsWith('/history'))return json(history(u.includes('/tasks/A/')?'A':'B'));
    if(u.includes('/versions/'))return json({items:[],next_cursor:null});
    if(u.endsWith('/tasks/A'))return json(task('A',current));
    if(u.endsWith('/tasks/B'))return json(task('B','B2'));
    throw new Error('unexpected '+u);
  };
  const until=async predicate=>{for(let n=0;n<50;n++){if(predicate())return;await flush();}assert.fail('navigation did not settle');};
  try {
    const wb=await mountWorkbench(root);
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{task:'B'},disabled:false})}});
    await until(()=>elements.get('wb-title').textContent==='任务B'&&/V2/.test(elements.get('wb-summary').textContent));
    elements.get('wb-messages').listeners.get('click')({target:{closest:()=>({dataset:{messageAction:'open',messageId:'M1'},disabled:false})}});
    await until(()=>elements.get('wb-title').textContent==='任务A'&&/V1.*历史版本/.test(elements.get('wb-summary').textContent));
    current='A3';
    elements.get('wb-tasks').listeners.get('click')({target:{closest:()=>({dataset:{action:'refresh'},disabled:false})}});
    for(let n=0;n<8;n++)await flush();
    assert.equal(elements.get('wb-title').textContent,'任务A');assert.match(elements.get('wb-summary').textContent,/V1.*历史版本/);
    messageVersion='MISSING';
    elements.get('wb-messages').listeners.get('click')({target:{closest:()=>({dataset:{messageAction:'refresh'},disabled:false})}});
    for(let n=0;n<4;n++)await flush();
    elements.get('wb-messages').listeners.get('click')({target:{closest:()=>({dataset:{messageAction:'open',messageId:'M1'},disabled:false})}});
    await until(()=>/消息关联版本不存在/.test(elements.get('wb-context-error').innerHTML));
    assert.equal(elements.get('wb-summary').textContent,'');
    wb.destroy();
  } finally {Object.assign(globalThis,original);}
});
