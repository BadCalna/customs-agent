import test from 'node:test';
import assert from 'node:assert/strict';
import {createMessageList} from '../../web/components/message-list.js';
import {createClient} from '../../web/api.js';

const deferred = () => {let resolve, reject; const promise = new Promise((ok, fail) => {resolve=ok;reject=fail;}); return {promise,resolve,reject};};
const message = (id='M1', overrides={}) => ({message_id:id, task_id:'T1', version_id:'V1', submission_id:'S1', decision_id:null,
  kind:'submission.created', title:'待审核', body:'请查看本次提交', is_read:false, read_at:null, created_at:'2026-11-19T03:00:00+00:00', target_url:'/?task=T1&version=V1', ...overrides});
const page = (items, overrides={}) => ({items, next_cursor:null, unread_count:items.filter(m=>!m.is_read).length, ...overrides});
function host() {return {innerHTML:'',listeners:new Map(), addEventListener(k,fn){this.listeners.set(k,fn);},removeEventListener(k){this.listeners.delete(k);},replaceChildren(){this.innerHTML='';}};}
const client = () => ({listMessages:async()=>page([message()]),markMessageRead:async id=>message(id,{is_read:true,read_at:'2026-11-19T03:00:01+00:00'})});

test('refresh and pagination consume server cursor and unread count; repeated IDs are not duplicated', async()=>{
  const h=host(), requests=[], api=client();
  api.listMessages=async p=>{requests.push(p);return p.cursor?page([message('M1'),message('M2')],{unread_count:8}):page([message()],{next_cursor:'next',unread_count:9});};
  const list=createMessageList(h,{client:api});
  await list.load(); assert.equal(list.getState().unreadCount,9);
  await list.load(true); assert.equal(requests[1].cursor,'next');
  assert.deepEqual(list.getState().items.map(m=>m.message_id),['M1','M2']);assert.equal(list.getState().unreadCount,8);
  list.destroy();
});

test('failed read write preserves unread state; success updates only the server-confirmed message',async()=>{
  const h=host(), api=client(), wait=deferred();api.markMessageRead=()=>wait.promise;
  const list=createMessageList(h,{client:api});await list.load();
  const saving=list.markRead('M1');assert.equal(list.getState().items[0].is_read,false);assert.match(h.innerHTML,/正在标记/);
  wait.reject(new Error('保存失败'));await saving;
  assert.equal(list.getState().items[0].is_read,false);assert.equal(list.getState().unreadCount,1);assert.match(h.innerHTML,/保存失败/);
  api.markMessageRead=async id=>message(id,{is_read:true});await list.markRead('M1');
  assert.equal(list.getState().items[0].is_read,true);assert.equal(list.getState().unreadCount,0);list.destroy();
});

test('mark read rejects mismatched identity and duplicate in-flight clicks',async()=>{
  const h=host(), api=client(), wait=deferred();let calls=0;
  api.markMessageRead=()=>{calls++;return wait.promise;};
  const list=createMessageList(h,{client:api});await list.load();const saving=list.markRead('M1');await list.markRead('M1');
  assert.equal(calls,1);wait.resolve(message('M2',{is_read:true}));await saving;
  assert.equal(list.getState().items[0].is_read,false);assert.match(h.innerHTML,/不符/);list.destroy();
});

test('an obsolete refresh cannot restore unread after a successful read',async()=>{
  const h=host(),api=client(),wait=deferred(),list=createMessageList(h,{client:api});await list.load();
  api.listMessages=()=>wait.promise;const refresh=list.load();await list.markRead('M1');
  wait.resolve(page([message()]));await refresh;
  assert.equal(list.getState().items[0].is_read,true);assert.equal(list.getState().unreadCount,0);list.destroy();
});

test('logout teardown rejects late list/write responses and callbacks to a replacement inbox',async()=>{
  const h=host(),api=client(),wait=deferred(),counts=[];api.listMessages=()=>wait.promise;
  const old=createMessageList(h,{client:api,onCount:n=>counts.push(n)}),read=old.load();old.destroy();
  const replacement=createMessageList(h,{client:{...client(),listMessages:async()=>page([message('NEW')])}});await replacement.load();
  const before=counts.length;wait.resolve(page([message('OLD')]));await read;
  assert.equal(counts.length,before);assert.match(h.innerHTML,/NEW/);assert.doesNotMatch(h.innerHTML,/OLD/);replacement.destroy();
  const h2=host(),api2=client(),write=deferred();api2.markMessageRead=()=>write.promise;
  const list=createMessageList(h2,{client:api2});await list.load();const saving=list.markRead('M1');list.destroy();
  write.resolve(message('M1',{is_read:true}));await saving;assert.equal(h2.innerHTML,'');
});

test('new refresh supersedes older read and invalid response is shown as error',async()=>{
  const h=host(),api=client(),wait=deferred();api.listMessages=()=>wait.promise;
  const list=createMessageList(h,{client:api}),old=list.load();api.listMessages=async()=>page([message('LATEST')]);await list.load();
  wait.resolve(page([message('OLD')]));await old;assert.equal(list.getState().items[0].message_id,'LATEST');
  api.listMessages=async()=>({items:[],next_cursor:null,unread_count:'0'});await list.load();assert.match(h.innerHTML,/格式异常/);list.destroy();
});

test('open uses immutable message task/version and never trusts arbitrary target_url; text is escaped',async()=>{
  const h=host(),api=client(),opened=[];
  api.listMessages=async()=>page([message('M1',{title:'<img onerror="evil()">',body:'<script>evil()</script>',target_url:'javascript:evil()'})]);
  const list=createMessageList(h,{client:api,onOpen:m=>opened.push(m)});await list.load();await list.open('M1');
  assert.equal(opened[0].task_id,'T1');assert.equal(opened[0].version_id,'V1');assert.equal(Object.isFrozen(opened[0]),true);
  assert.doesNotMatch(h.innerHTML,/<script>|<img|javascript:/);assert.match(h.innerHTML,/&lt;script&gt;/);list.destroy();
});

test('DOM read/open controls call only their matching message operation',async()=>{
  const h=host(),api=client(),opened=[],list=createMessageList(h,{client:api,onOpen:m=>opened.push(m)});await list.load();
  h.listeners.get('click')({target:{closest:()=>({disabled:false,dataset:{messageAction:'open',messageId:'M1'}})}});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(opened.length,1);assert.equal(list.getState().items[0].is_read,false);
  list.destroy();assert.equal(h.listeners.has('click'),false);
});

test('shared live API uses GET query and CSRF-protected PATCH; fixture refuses real writes',async()=>{
  const original={fetch:globalThis.fetch,document:globalThis.document},calls=[];
  globalThis.document={cookie:'csrftoken=csrf-test'};
  globalThis.fetch=async(url,options={})=>{calls.push({url,options});return new Response(JSON.stringify(String(url).includes('fixture')?{messages:[message()]}:options.method==='PATCH'?message('M/1',{is_read:true}):page([message()])),{headers:{'Content-Type':'application/json'}});};
  try {
    const api=createClient({mode:'live'});await api.listMessages({cursor:'a/b',limit:20,unread_only:true});await api.markMessageRead('M/1');
    assert.equal(calls[0].url,'/api/v1/messages?cursor=a%2Fb&limit=20&unread_only=true');
    assert.equal(calls[1].url,'/api/v1/messages/M%2F1');assert.equal(calls[1].options.method,'PATCH');assert.equal(calls[1].options.headers['X-CSRFToken'],'csrf-test');assert.deepEqual(JSON.parse(calls[1].options.body),{is_read:true});
    const fixture=createClient({mode:'fixture',fixtureUrl:'/fixture'});assert.equal((await fixture.listMessages()).unread_count,1);
    await assert.rejects(()=>fixture.markMessageRead('M1'),error=>error.status===501);assert.equal((await fixture.listMessages()).items[0].is_read,false);
  } finally {Object.assign(globalThis,original);}
});
