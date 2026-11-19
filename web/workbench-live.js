/** Live branch of /workbench/. Reuses P06 entry, P07/P08/P11 modules and shared API. */
import {createClient} from './api.js';
import {createWorkbenchContext, runsFor} from './workbench-context.js';
import {createTaskList, escapeHtml as esc, errorHtml, RUN_KINDS} from './components/task-list.js';
import {createRunProgress} from './components/run-progress.js';
import {createExportActions} from './components/export-actions.js';
import {renderCommodityList} from './components/commodity-list.js';
import {renderCommodityCard} from './components/commodity-card.js';
import {createReviewActions} from './components/review-actions.js';
import {createMessageList} from './components/message-list.js';

/** Bind P11 to immutable review scope; browsing a commodity never replaces an expert draft. */
export function createWorkbenchReview({host, client, getContext, getUser, getSession, refreshTask}) {
  let panel=null, scopeKey='', dataKey='', bound=null;
  function clear() {panel?.destroy();panel=null;bound=null;scopeKey=dataKey='';host.hidden=false;if(host.dataset)delete host.dataset.reviewNotice;host.replaceChildren();}
  // Empty fields during openTask/openVersion are not evidence of a different submission.
  // Keep the instance (including P11's private drafts), but never expose stale write controls.
  function canRetain(s, role) {
    if (!panel || role!==bound.role || getSession()!==bound.ticket || getContext()!==bound.ctx || s.taskId!==bound.taskId) return false;
    if (role==='operator') {
      if (s.versionId && s.versionId!==bound.versionId) return false;
      if (s.itemId && s.itemId!==bound.itemId) return false;
      return !!(s.loading || s.error || (s.itemId===bound.itemId && !s.card));
    }
    if (role!=='expert') return false;
    if ((s.task && s.task.current_version_id!==bound.versionId) || (s.versionId && s.versionId!==bound.versionId)) return false;
    if (!s.task || !s.history || !s.versionId) return true;
    if (s.kind && s.kind!=='expert_check') return false;
    if (!s.run) return !!(s.loading || s.error || s.runId);
    const submission=s.history.versions.find(v=>v.version_id===s.versionId)?.submissions.find(x=>x.submission_id===s.run.submission_id);
    return s.run.kind==='expert_check' && s.run.is_current && submission?.status==='pending' && submission.submission_id===bound.submissionId;
  }
  function binding(s, role) {
    if (!s.task || s.versionId!==s.task.current_version_id || !['operator','expert'].includes(role)) return null;
    const version=(s.task.versions || []).find(v=>v.version_id===s.versionId);
    let submission=null, items=[];
    if (role==='expert') {
      submission=s.history?.versions?.find(v=>v.version_id===s.versionId)?.submissions?.find(x=>x.submission_id===s.run?.submission_id);
      if (!submission || submission.status!=='pending' || s.run?.kind!=='expert_check' || !s.run.is_current) return null;
      if (s.loading || s.progressError) return {notice:'正在读取并核对本版本审核信息，完成后再提交。'};
      const byId=new Map(s.items.map(item=>[item.item_id,item]));
      const rows=submission.items.map(row=>({row,item:byId.get(row.item_id)}));
      if (!rows.length || byId.size!==s.items.length || rows.some(({item})=>
        !item || item.task_id!==s.taskId || item.version_id!==s.versionId || !item.name?.trim() ||
        !item.source_location?.sheet_name || !Number.isSafeInteger(item.source_location.row_number) || item.source_location.row_number<1)) {
        return {notice:'提交中的部分商品缺少本版本品名或来源位置，请刷新任务详情后再审核。'};
      }
      // Enrich labels only. All submitted HS, confirmation IDs and revisions remain the snapshot's values.
      submission={...submission,items:rows.map(({row,item})=>({...row,name:item.name,source_location:{...item.source_location}}))};
      items=rows.map(({item})=>({...item,facts:{name:item.name}}));
    } else {
      if (s.error || !s.card) return null;
      items=[s.card];
    }
    const scope=role==='expert' ? [role,s.taskId,s.versionId,submission.submission_id]
      : [role,s.taskId,s.versionId,s.card.item_id];
    return {scope:JSON.stringify(scope),submissionId:submission?.submission_id,itemId:role==='operator'?s.card.item_id:null,
      data:{task:{...s.task,allowed_actions:(s.task.allowed_actions || []).filter(a=>role==='operator'?a!=='submission.create':true)},
        version,items,submission,expert_check:s.run?{run_id:s.run.run_id,status:s.run.status}:null}};
  }
  function render(s) {
    const role=getUser()?.role, next=binding(s,role);
    if (!next || next.notice) {
      if (canRetain(s,role)) {host.hidden=true;host.dataset.reviewNotice=next?.notice || '审核信息读取未完成，请刷新任务详情后重试。';return;}
      clear();if(next?.notice)host.innerHTML=`<p role="status">${esc(next.notice)}</p>`;return;
    }
    host.hidden=false;
    const signature=JSON.stringify(next.data), sessionScope=JSON.stringify([getSession(),next.scope]);
    if (panel && sessionScope===scopeKey) {
      // Data updates within the same submission preserve P11's draft. Commodity selection is absent here.
      if (signature!==dataKey) {dataKey=signature;panel.update(next.data);}return;
    }
    clear();scopeKey=sessionScope;dataKey=signature;
    const owner=bound={role,taskId:s.taskId,versionId:s.versionId,itemId:next.itemId,submissionId:next.submissionId,
      ctx:getContext(),ticket:getSession(),scope:next.scope};
    let decisionAttempt=null;
    function stillCurrent() {
      const ctx=getContext();
      return bound===owner && !host.hidden && getSession()===owner.ticket && ctx===owner.ctx && binding(ctx.store.get(),getUser()?.role)?.scope===owner.scope;
    }
    panel=createReviewActions({role,data:next.data,api:{
      confirm:payload=>{
        if (!stillCurrent() || !getContext().store.get().task?.allowed_actions?.includes('confirmation.write')) return Promise.reject(new Error('审核上下文正在刷新、已切换或无操作权限；草稿已保留，请核对后再提交。'));
        return client.confirmItem(owner.versionId,owner.itemId,payload);
      },
      submit:()=>Promise.reject(new Error('请使用本批次提交入口，避免只提交选中商品')),
      decide:(payload,expectedSubmissionId)=>{
        if (!stillCurrent() || expectedSubmissionId!==owner.submissionId
            || !getContext().store.get().task?.allowed_actions?.includes('decision.resolve')) return Promise.reject(new Error('审核上下文正在刷新、已切换或无操作权限；草稿已保留，请核对后再提交。'));
        const signature=JSON.stringify(payload);if(decisionAttempt?.signature!==signature)decisionAttempt={signature,key:crypto.randomUUID()};
        return client.decideSubmission(owner.submissionId,payload,decisionAttempt.key);
      }},onChanged:()=>{
        if (getSession()!==owner.ticket || getContext()!==owner.ctx) return;
        const current=owner.ctx.store.get();
        if (current.taskId!==owner.taskId) return;
        const preserve = owner.role==='operator' ? {preserveItem:true, preserveVersionId:owner.versionId, preserveItemId:owner.itemId} : {};
        const reload = () => (typeof refreshTask==='function' ? refreshTask(owner.taskId, preserve) : owner.ctx.openTask(owner.taskId, preserve));
        if (bound===owner) {
          void reload();
          return;
        }
        // An in-flight operator confirm may settle after a concurrent list reconcile cleared s.card;
        // still trigger a post-write read unless the operator switched to another item or version.
        if (owner.role==='operator' && !panel && (!current.versionId || current.versionId===owner.versionId) && !current.itemId) {
          void reload();
        }
      },
      onConflict:()=>{
        if (getSession()!==owner.ticket || getContext()!==owner.ctx) return;
        const current=owner.ctx.store.get();
        if (current.taskId!==owner.taskId) return;
        const preserve = owner.role==='operator' ? {preserveItem:true, preserveVersionId:owner.versionId, preserveItemId:owner.itemId} : {};
        const reload = () => (typeof refreshTask==='function' ? refreshTask(owner.taskId, preserve) : owner.ctx.openTask(owner.taskId, preserve));
        if (stillCurrent() || bound===owner) return reload();
        if (owner.role==='operator' && !panel && (!current.versionId || current.versionId===owner.versionId) && !current.itemId) {
          return reload();
        }
      }});
    panel.mount(host);
  }
  function retainedScope() {
    if (!panel || !bound || bound.role!=='operator' || bound.ctx!==getContext() || bound.ticket!==getSession()) return null;
    return {taskId: bound.taskId, versionId: bound.versionId, itemId: bound.itemId};
  }
  return {render,clear,retainedScope,destroy:clear};
}

export async function mountWorkbench(root) {
  const api = createClient({mode:'live'});
  let user = null, context = null, tasks = null, progress = null, exports = null, review = null, messages = null;
  let unsubscribe = null, alive = true, session = 0, runKey = '', cardKey = undefined, submitIntent = null, submitting = false, pendingReconcile = false, selectionIntent = null, refreshTask = null, messageTarget = null, messageNavigation = 0;
  const $ = selector => root.querySelector(selector);
  function reset() {
    session++; messageNavigation++; unsubscribe?.(); tasks?.destroy(); progress?.destroy(); exports?.destroy(); context?.destroy(); review?.destroy(); messages?.destroy(); messages=null;
    context=tasks=progress=exports=review=null;unsubscribe=null;runKey='';cardKey=undefined;submitIntent=null;submitting=false;pendingReconcile=false;selectionIntent=null;refreshTask=null;messageTarget=null;
  }
  function gate(error = null) {
    reset(); user=null;
    root.innerHTML = `<main class="wb-login"><h1>关务审核工作台</h1><p>使用已分配的业务账号登录。</p><form id="wb-login">
      <label>账号<input name="username" autocomplete="username" required></label><label>密码<input name="password" type="password" autocomplete="current-password" required></label>
      <button class="btn primary" type="submit">登录</button></form><div id="wb-auth-error">${error ? errorHtml(error) : ''}</div></main>`;
    $('#wb-login').onsubmit = async e => {
      e.preventDefault(); const form=e.target, fields=new FormData(form), ticket=session;form.querySelector('button').disabled=true;
      try {const me=await api.login(fields.get('username'),fields.get('password'));if(alive&&ticket===session){user=me;await workspace();}}
      catch(e){if(alive&&ticket===session){$('#wb-auth-error').innerHTML=errorHtml(e);form.querySelector('button').disabled=false;}}
    };
  }
  function expire(error) { if (error?.status===401 && alive && user) {gate(error);return true;}return false; }
  // The same client is passed to every component. Auth expiry tears down all polling and pending context.
  const client = new Proxy(api,{get(target,key){const value=target[key];return typeof value==='function' ? async(...args)=>{
    const ticket=session;
    try{return await value(...args);}catch(error){if(ticket===session)expire(error);throw error;}
  }:value;}});
  function operatorPreserve(taskId) {
    if (user?.role !== 'operator') return {};
    const s = context?.store.get();
    if (s?.taskId === taskId && s.versionId && s.itemId) {
      return {preserveItem: true, preserveVersionId: s.versionId, preserveItemId: s.itemId};
    }
    const retained = review?.retainedScope?.();
    if (retained && retained.taskId === taskId && retained.versionId && retained.itemId) {
      return {preserveItem: true, preserveVersionId: retained.versionId, preserveItemId: retained.itemId};
    }
    return {preserveItem: true};
  }
  function render(s) {
    if(!alive||!user||!context)return;
    const version=(s.task?.versions || []).find(v=>v.version_id===s.versionId);
    const next=JSON.stringify([s.taskId,s.versionId,s.kind,s.runId]);
    if(next!==runKey){
      runKey=next;
      void progress.open({taskId:s.taskId,versionId:s.versionId,kind:s.kind,runId:s.runId,
        versionNumber:version?.number,isVersionCurrent:s.versionId===s.task?.current_version_id});
      // progress.open 同步通过 onStateChange 写回 store 并触发内层 render；此处若状态已更新则直接让出，防止旧快照覆盖新视图。
      if(context.store.get()!==s)return;
    }
    $('#wb-title').textContent=s.task?.title || (s.loading?'正在读取任务…':'选择左侧任务');
    $('#wb-context-error').innerHTML=s.error?errorHtml(s.error):'';
    const runs=runsFor(s.history,s.versionId,s.kind);
    const submission=s.history?.versions?.find(v=>v.version_id===s.versionId)?.submissions?.find(x=>x.submission_id===s.run?.submission_id);
    $('#wb-toolbar').innerHTML=s.task?`<label>文件版本<select id="wb-version">${(s.task.versions || []).map(v=>`<option value="${esc(v.version_id)}" ${v.version_id===s.versionId?'selected':''}>V${v.number} ${esc(v.original_filename)}${v.is_current?' · 当前':''}</option>`).join('')}</select></label>
      <label>执行阶段<select id="wb-kind">${Object.entries(RUN_KINDS).map(([k,v])=>`<option value="${k}" ${k===s.kind?'selected':''}>${v}</option>`).join('')}</select></label>
      <label>执行记录<select id="wb-run"><option value="">${runs.length?'请选择执行记录':'本版本该阶段暂无执行'}</option>${runs.map(r=>`<option value="${esc(r.run_id)}" ${r.run_id===s.runId?'selected':''}>${esc(r.created_at)} · ${esc(r.status)} · ${esc(r.run_id.slice(0,8))}${r.is_current?' · 阶段有效':''}</option>`).join('')}</select></label>
      ${submission?`<label>专家决定<select id="wb-decision"><option value="">仅所选提交，不绑定决定</option>${(submission.decisions || []).map(d=>`<option value="${esc(d.decision_id)}" ${d.decision_id===s.decisionId?'selected':''}>${esc(d.action)} · ${esc(d.decision_id.slice(0,8))}</option>`).join('')}</select></label>`:''}
      <button class="btn" id="wb-refresh">刷新任务详情</button>`:s.taskId?'<button class="btn" id="wb-refresh">刷新任务详情</button>':'';
    $('#wb-version')?.addEventListener('change',e=>{messageNavigation++;messageTarget=null;void context.openVersion(e.target.value);});
    $('#wb-kind')?.addEventListener('change',e=>context.chooseKind(e.target.value));
    $('#wb-run')?.addEventListener('change',e=>context.chooseRun(e.target.value));
    $('#wb-decision')?.addEventListener('change',e=>context.chooseDecision(e.target.value));
    $('#wb-refresh')?.addEventListener('click',()=>void refreshTask?.(s.taskId,operatorPreserve(s.taskId)));
    $('#wb-summary').textContent=s.versionId?`V${version?.number ?? '?'} · ${s.loading?'正在读取商品…':s.items.length+' 件商品'} · ${s.versionId===s.task?.current_version_id?'当前版本':'历史版本，只读'}。商品卡片展示该版本阶段有效结果；运行明细及导出严格使用上方所选 Run。`:'';
    exports.update(s,user.role);
    renderCommodityList($('#wb-items'),{items:s.items,selectedItemId:s.itemId,onSelectItem:id=>void context.selectItem(id),emptyTitle:s.loading?'正在读取商品':'本版本暂无商品',emptyHint:'上传完成后刷新任务。'});
    if(cardKey!==s.card){cardKey=s.card;
      renderCommodityCard($('#wb-card'),{card:s.card,contractNotes:true,onOpenRun:runId=>{
        const found=s.history?.versions?.find(v=>v.version_id===s.versionId)?.runs?.find(r=>r.run_id===runId);
        if(found)context.chooseRun(runId,found.kind);
      },onSelectImage:image=>{if(image.download_url)(typeof window!=='undefined'?window:globalThis).open?.(image.download_url,'_blank','noopener');},
      onSelectSource:source=>{$('#wb-source').textContent=`${source.label} · ${source.sourceId || '未提供来源'}`;}});
    }
    if(!s.card)$('#wb-source').textContent='';
    $('#wb-submit').hidden=user.role!=='operator';
    $('#wb-submit').disabled=submitting||s.loading||!!s.error||!s.items.length||s.versionId!==s.task?.current_version_id||!s.task.allowed_actions?.includes('submission.create');
    review.render(s);
    if ($('#wb-review').hidden) $('#wb-summary').textContent=[$('#wb-summary').textContent,`${user.role==='expert'?'专家':''}草稿已保留，暂不可提交。${$('#wb-review').dataset.reviewNotice}`].filter(Boolean).join(' ');
  }
  async function submitBatch() {
    if(submitting||!context)return;
    const s=context.store.get(),ticket=session;
    if(!s.task||s.loading||s.error||s.versionId!==s.task.current_version_id||!s.items.length||!s.task.allowed_actions?.includes('submission.create'))return;
    const matches=()=>alive&&ticket===session&&context?.store.get().taskId===s.taskId&&context.store.get().versionId===s.versionId;
    submitting=true;render(s);$('#wb-write-note').textContent='正在读取本版本全部确认记录…';
    try {
      const cards=[]; // Sequential bounded reads; never construct a partial submission from the selected card.
      for(const item of s.items){cards.push(await client.getItem(s.versionId,item.item_id));if(!matches())return;}
      if(cards.some((c,i)=>c.task_id!==s.taskId||c.version_id!==s.versionId||c.item_id!==s.items[i].item_id||!c.business_confirmed_hs?.value||!c.business_confirmed_hs.source_id))throw new Error('本批次仍有商品缺少业务确认或确认引用，请逐项确认后提交。');
      const payload={version_id:s.versionId,revision:s.task.revision,items:cards.map(c=>({item_id:c.item_id,confirmation_id:c.business_confirmed_hs.source_id}))};
      const signature=JSON.stringify([s.taskId,payload]);if(submitIntent?.signature!==signature)submitIntent={signature,key:crypto.randomUUID()};
      await client.submitTask(s.taskId,payload,submitIntent.key);
      if(matches()){submitIntent=null;$('#wb-write-note').textContent='服务端已接收本批次提交，正在刷新。';await (refreshTask ? refreshTask(s.taskId) : context.openTask(s.taskId));}
    }catch(error){if(matches())$('#wb-write-note').textContent=error.message;}
    finally{if(alive&&ticket===session&&context){submitting=false;render(context.store.get());}}
  }
  async function workspace() {
    reset();const ticket=session;
    root.innerHTML=`<header class="wb-header"><h1>关务审核工作台</h1><span>${esc(user.display_name||user.username)} · ${esc(user.role_label||user.role)}</span><a href="/uploads/">上传 / 认领</a><button class="btn" id="wb-messages-toggle" aria-expanded="false" aria-controls="wb-messages">消息 <span id="wb-messages-count" class="p19-badge">0</span></button><button class="btn" id="wb-logout">退出</button></header>
      <section id="wb-messages" hidden></section>
      <p class="wb-banner">真实会话与 API。AI 为 mock / inline；合成结果会醒目标注，不构成申报依据。后台队列与失败行重试待 P13 交付。</p>
      <main class="wb-layout"><aside id="wb-tasks"></aside><section class="wb-detail"><h1 id="wb-title">选择左侧任务</h1><div id="wb-context-error"></div><div id="wb-toolbar" class="wb-toolbar"></div><p id="wb-summary"></p>
      <div class="wb-results"><div id="wb-progress"></div><div id="wb-exports"></div></div>
      <div class="wb-goods"><section aria-label="本版本商品" id="wb-items"></section><section id="wb-card" tabindex="-1" aria-label="商品卡片"></section></div><p id="wb-source"></p>
      <button class="btn primary" id="wb-submit" hidden>提交本批次专家审核</button><p id="wb-write-note" role="status"></p><div id="wb-review"></div></section></main>`;
    context=createWorkbenchContext(client);
    async function openMessageVersion(message) {
      const request=++messageNavigation, ctx=context;
      if(!alive || ticket!==session || !ctx || !message.task_id || !message.version_id)return;
      const target=messageTarget={taskId:message.task_id,versionId:message.version_id};
      await ctx.openTask(message.task_id);
      if(!alive || ticket!==session || context!==ctx || request!==messageNavigation || messageTarget!==target)return;
      const s=ctx.store.get();
      if(s.taskId!==message.task_id)return;
      if(s.error)throw s.error;
      if(!s.task?.versions?.some(v=>v.version_id===message.version_id)
          || !s.history?.versions?.some(v=>v.version_id===message.version_id)) {
        const error=new Error('消息关联版本不存在或不可见；未跳转到其他版本。');
        ctx.failTask(message.task_id,error);throw error;
      }
      await ctx.openVersion(message.version_id);
    }
    if(client.listMessages && client.markMessageRead) {
      messages=createMessageList($('#wb-messages'),{client,onOpen:openMessageVersion,
        onCount:count=>{if(alive&&ticket===session)$('#wb-messages-count').textContent=String(count);},
        onError:error=>{expire(error);}});
      void messages.load();
    }
    $('#wb-messages-toggle').onclick=()=>{
      const host=$('#wb-messages');host.hidden=!host.hidden;
      $('#wb-messages-toggle').setAttribute?.('aria-expanded',String(!host.hidden));
      if(!host.hidden)void messages?.load();
    };
    review=createWorkbenchReview({host:$('#wb-review'),client,getContext:()=>context,getUser:()=>user,getSession:()=>session,refreshTask:(id,opts)=>refreshCurrentTask(id,opts)});
    exports=createExportActions($('#wb-exports'),{client});
    progress=createRunProgress($('#wb-progress'),{client,onStateChange:p=>context?.acceptProgress(p),onSelectItem:async identity=>{
      const ctx=context;await ctx.selectItem(identity.item_id,identity);
      if(context===ctx&&ctx.store.get().card?.item_id===identity.item_id){$('#wb-card').scrollIntoView({block:'start',behavior:'smooth'});$('#wb-card').focus();}
    }});
    async function refreshCurrentTask(id, opts) {
      if (!alive || !context || ticket !== session) return;
      messageNavigation++;
      const pinned=messageTarget?.taskId===id ? messageTarget : null;
      await context.openTask(id, opts);
      if (!alive || !context || ticket !== session) return;
      if(pinned && messageTarget===pinned && context.store.get().taskId===id) {
        if(context.store.get().task?.versions?.some(v=>v.version_id===pinned.versionId)
            && context.store.get().history?.versions?.some(v=>v.version_id===pinned.versionId)) await context.openVersion(pinned.versionId);
        else context.failTask(id,new Error('消息关联版本不存在或不可见；未跳转到其他版本。'));
      }
      if (pendingReconcile) {
        pendingReconcile = false;
        await reconcileSelection(tasks.getState().items);
      }
    }
    refreshTask = refreshCurrentTask;
    async function reconcileSelection(items) {
      if (!alive || !tasks || !context || ticket !== session) return;
      // An inbox link pins its historical version until the user selects a different task/version.
      if(messageTarget && context.store.get().taskId===messageTarget.taskId)return;
      const listState = tasks.getState();
      if (!listState.selectedId) return;
      if (listState.selecting) { pendingReconcile = true; return; }
      const s = context.store.get();
      if (s.loading && (!s.task || !s.history)) { pendingReconcile = true; return; }
      if (s.task && s.task.task_id !== listState.selectedId) { pendingReconcile = true; return; }
      const entry = (Array.isArray(items) ? items : []).find(row => row && row.task_id === listState.selectedId);
      if (!entry) return;
      if (s.task && s.history && !s.error) {
        const versionChanged = (entry.current_version_id || null) !== (s.task.current_version_id || null);
        const runChanged = (entry.latest_run_id || null) !== (s.task.latest_run_id || null);
        const stageChanged = (entry.stage || null) !== (s.task.stage || null);
        if (!versionChanged && !runChanged && !stageChanged) return;
      }
      const keep = operatorPreserve(listState.selectedId);
      selectionIntent = (keep.preserveVersionId && keep.preserveItemId)
        ? {taskId: listState.selectedId, versionId: keep.preserveVersionId, itemId: keep.preserveItemId} : null;
      await tasks.selectTask(listState.selectedId);
    }
    tasks=createTaskList($('#wb-tasks'),{
      client,
      onSelectionStart:id=>{
        messageNavigation++;
        messageTarget=null;
        if (!selectionIntent || selectionIntent.taskId !== id) selectionIntent = null;
        $('#wb-write-note').textContent='';context.beginTask(id);
      },
      onSelectTask:t=>{
        const keep = selectionIntent && selectionIntent.taskId === t.task_id ? selectionIntent : null;
        selectionIntent = null;
        return context.acceptTask(t, keep ? {preserveVersionId: keep.versionId, preserveItemId: keep.itemId} : {});
      },
      onLoaded:items=>reconcileSelection(items),
      onSelectionSettled:async(settledId,{taskId,ok,error})=>{
        selectionIntent = null;
        if (!alive || !context || ticket !== session) return;
        if(messageTarget && context.store.get().taskId===messageTarget.taskId)return;
        if (!ok) {
          pendingReconcile = false;
          if (settledId && settledId !== taskId) await refreshCurrentTask(settledId);
          else if (!settledId) context.beginTask(null);
          else context.failTask(taskId, error);
          return;
        }
        if (!pendingReconcile) return;
        pendingReconcile = false;
        await reconcileSelection(tasks.getState().items);
      },
    });
    unsubscribe=context.store.subscribe(render);render(context.store.get());
    $('#wb-submit').onclick=submitBatch;
    $('#wb-logout').onclick=async()=>{
      reset();user=null;root.innerHTML='<main class="wb-login"><p>正在退出…</p></main>';
      let failure=null;try{await api.logout();}catch(error){failure=error;}
      if(alive)gate(failure);
    };
    await tasks.load();
    if(ticket!==session||!context)return;
    const requestedParams=new URLSearchParams(location.search), requested=requestedParams.get('task');
    if(requested){
      const versionId=requestedParams.get('version');
      if(versionId) {
        try {await openMessageVersion({task_id:requested,version_id:versionId});}
        catch(error) {if(alive&&ticket===session&&context?.store.get().taskId===requested)context.failTask(requested,error);}
      }
      else if(tasks.getState().items.some(t=>t.task_id===requested)) await tasks.selectTask(requested);
      else await refreshCurrentTask(requested);
    }
  }
  const hostWin = typeof window !== 'undefined' ? window : globalThis;
  function destroy(){alive=false;hostWin.removeEventListener?.('pagehide',destroy);reset();}
  hostWin.addEventListener?.('pagehide',destroy,{once:true});
  try {user=await api.me();if(!alive)return;if(user)await workspace();else gate();}catch(error){gate(error);}
  return {destroy};
}
