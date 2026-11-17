/** Live branch of /workbench/. Reuses P06 entry, P07/P08/P11 modules and shared API. */
import {createClient} from './api.js';
import {createWorkbenchContext, runsFor} from './workbench-context.js';
import {createTaskList, escapeHtml as esc, errorHtml, RUN_KINDS} from './components/task-list.js';
import {createRunProgress} from './components/run-progress.js';
import {createExportActions} from './components/export-actions.js';
import {renderCommodityList} from './components/commodity-list.js';
import {renderCommodityCard} from './components/commodity-card.js';
import {createReviewActions} from './components/review-actions.js';

export async function mountWorkbench(root) {
  const api = createClient({mode:'live'});
  let user = null, context = null, tasks = null, progress = null, exports = null, review = null;
  let unsubscribe = null, alive = true, session = 0, runKey = '', cardKey = null, reviewKey = '', submitIntent = null, submitting = false;
  const $ = selector => root.querySelector(selector);
  function reset() {
    session++; unsubscribe?.(); tasks?.destroy(); progress?.destroy(); exports?.destroy(); context?.destroy(); review?.destroy();
    context=tasks=progress=exports=review=null;unsubscribe=null;runKey='';cardKey=null;reviewKey='';submitIntent=null;submitting=false;
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
  function clearReview() {review?.destroy();review=null;reviewKey='';$('#wb-review')?.replaceChildren();}
  function renderReview(s) {
    const current=s.versionId===s.task?.current_version_id;
    const selected=s.history?.versions.find(v=>v.version_id===s.versionId);
    const submission=selected?.submissions.find(x=>x.submission_id===s.run?.submission_id);
    const target=user?.role==='expert' ? submission?.submission_id : s.card?.item_id;
    const key=JSON.stringify([s.taskId,s.versionId,target,s.card?.revision,s.task?.revision,s.runId,s.run?.is_current,!!s.error,user?.role]);
    if(key===reviewKey)return;
    clearReview();reviewKey=key;
    if(!current || !s.task || s.error || !['operator','expert'].includes(user?.role))return;
    if(user.role==='operator' && !s.card)return;
    if(user.role==='expert' && (!submission || submission.status!=='pending' || s.run?.kind!=='expert_check' || !s.run.is_current))return;
    // One selected item per confirmation adapter: P11 confirm payload deliberately omits item_id.
    const bound={taskId:s.taskId,versionId:s.versionId,runId:s.runId,itemId:s.card?.item_id,submissionId:submission?.submission_id,ctx:context,ticket:session};
    let decisionAttempt=null;
    const stillCurrent=()=>session===bound.ticket && context===bound.ctx && context?.store.get().taskId===bound.taskId && context?.store.get().versionId===bound.versionId && context?.store.get().runId===bound.runId && (user?.role==='expert' || context?.store.get().itemId===bound.itemId);
    review=createReviewActions({role:user.role, data:{task:{...s.task,allowed_actions:s.task.allowed_actions.filter(a=>user.role==='operator'?a!=='submission.create':true)},
      version:s.task.versions.find(v=>v.version_id===s.versionId),items:s.card?[s.card]:[],submission,
      expert_check:s.run ? {run_id:s.run.run_id,status:s.run.status} : null},api:{
      confirm:payload=>client.confirmItem(bound.versionId,bound.itemId,payload),
      submit:()=>Promise.reject(new Error('请使用本批次提交入口，避免只提交选中商品')),
      decide:payload=>{
        const signature=JSON.stringify(payload);if(decisionAttempt?.signature!==signature)decisionAttempt={signature,key:crypto.randomUUID()};
        return client.decideSubmission(bound.submissionId,payload,decisionAttempt.key);
      }},onChanged:()=>{if(stillCurrent())void context.openTask(bound.taskId);},onConflict:()=>{if(stillCurrent())return context.openTask(bound.taskId);}});
    review.mount($('#wb-review'));
  }
  function render(s) {
    if(!alive||!user||!context)return;
    $('#wb-title').textContent=s.task?.title || (s.loading?'正在读取任务…':'选择左侧任务');
    $('#wb-context-error').innerHTML=s.error?errorHtml(s.error):'';
    const version=s.task?.versions.find(v=>v.version_id===s.versionId);
    const runs=runsFor(s.history,s.versionId,s.kind);
    const submission=s.history?.versions.find(v=>v.version_id===s.versionId)?.submissions.find(x=>x.submission_id===s.run?.submission_id);
    $('#wb-toolbar').innerHTML=s.task?`<label>文件版本<select id="wb-version">${s.task.versions.map(v=>`<option value="${esc(v.version_id)}" ${v.version_id===s.versionId?'selected':''}>V${v.number} ${esc(v.original_filename)}${v.is_current?' · 当前':''}</option>`).join('')}</select></label>
      <label>执行阶段<select id="wb-kind">${Object.entries(RUN_KINDS).map(([k,v])=>`<option value="${k}" ${k===s.kind?'selected':''}>${v}</option>`).join('')}</select></label>
      <label>执行记录<select id="wb-run"><option value="">${runs.length?'请选择执行记录':'本版本该阶段暂无执行'}</option>${runs.map(r=>`<option value="${esc(r.run_id)}" ${r.run_id===s.runId?'selected':''}>${esc(r.created_at)} · ${esc(r.status)} · ${esc(r.run_id.slice(0,8))}${r.is_current?' · 阶段有效':''}</option>`).join('')}</select></label>
      ${submission?`<label>专家决定<select id="wb-decision"><option value="">仅所选提交，不绑定决定</option>${submission.decisions.map(d=>`<option value="${esc(d.decision_id)}" ${d.decision_id===s.decisionId?'selected':''}>${esc(d.action)} · ${esc(d.decision_id.slice(0,8))}</option>`).join('')}</select></label>`:''}
      <button class="btn" id="wb-refresh">刷新任务详情</button>`:'';
    $('#wb-version')?.addEventListener('change',e=>void context.openVersion(e.target.value));
    $('#wb-kind')?.addEventListener('change',e=>context.chooseKind(e.target.value));
    $('#wb-run')?.addEventListener('change',e=>context.chooseRun(e.target.value));
    $('#wb-decision')?.addEventListener('change',e=>context.chooseDecision(e.target.value));
    $('#wb-refresh')?.addEventListener('click',()=>void context.openTask(s.taskId));
    $('#wb-summary').textContent=s.versionId?`V${version?.number ?? '?'} · ${s.loading?'正在读取商品…':s.items.length+' 件商品'} · ${s.versionId===s.task?.current_version_id?'当前版本':'历史版本，只读'}。商品卡片展示该版本阶段有效结果；运行明细及导出严格使用上方所选 Run。`:'';
    const next=JSON.stringify([s.taskId,s.versionId,s.kind,s.runId]);
    if(next!==runKey){runKey=next;void progress.open({taskId:s.taskId,versionId:s.versionId,kind:s.kind,runId:s.runId,
      versionNumber:version?.number,isVersionCurrent:s.versionId===s.task?.current_version_id});}
    exports.update(s,user.role);
    renderCommodityList($('#wb-items'),{items:s.items,selectedItemId:s.itemId,onSelectItem:id=>void context.selectItem(id),emptyTitle:s.loading?'正在读取商品':'本版本暂无商品',emptyHint:'上传完成后刷新任务。'});
    if(cardKey!==s.card){cardKey=s.card;
      renderCommodityCard($('#wb-card'),{card:s.card,contractNotes:true,onOpenRun:runId=>{
        const found=s.history?.versions.find(v=>v.version_id===s.versionId)?.runs.find(r=>r.run_id===runId);
        if(found)context.chooseRun(runId,found.kind);
      },onSelectImage:image=>{if(image.download_url)window.open(image.download_url,'_blank','noopener');},
      onSelectSource:source=>{$('#wb-source').textContent=`${source.label} · ${source.sourceId || '未提供来源'}`;}});
    }
    if(!s.card)$('#wb-source').textContent='';
    $('#wb-submit').hidden=user.role!=='operator';
    $('#wb-submit').disabled=submitting||s.loading||!!s.error||!s.items.length||s.versionId!==s.task?.current_version_id||!s.task.allowed_actions.includes('submission.create');
    renderReview(s);
  }
  async function submitBatch() {
    if(submitting||!context)return;
    const s=context.store.get(),ticket=session;
    if(!s.task||s.loading||s.error||s.versionId!==s.task.current_version_id||!s.items.length)return;
    const matches=()=>alive&&ticket===session&&context?.store.get().taskId===s.taskId&&context.store.get().versionId===s.versionId;
    submitting=true;render(s);$('#wb-write-note').textContent='正在读取本版本全部确认记录…';
    try {
      const cards=[]; // Sequential bounded reads; never construct a partial submission from the selected card.
      for(const item of s.items){cards.push(await client.getItem(s.versionId,item.item_id));if(!matches())return;}
      if(cards.some((c,i)=>c.task_id!==s.taskId||c.version_id!==s.versionId||c.item_id!==s.items[i].item_id||!c.business_confirmed_hs?.value||!c.business_confirmed_hs.source_id))throw new Error('本批次仍有商品缺少业务确认或确认引用，请逐项确认后提交。');
      const payload={version_id:s.versionId,revision:s.task.revision,items:cards.map(c=>({item_id:c.item_id,confirmation_id:c.business_confirmed_hs.source_id}))};
      const signature=JSON.stringify([s.taskId,payload]);if(submitIntent?.signature!==signature)submitIntent={signature,key:crypto.randomUUID()};
      await client.submitTask(s.taskId,payload,submitIntent.key);
      if(matches()){submitIntent=null;$('#wb-write-note').textContent='服务端已接收本批次提交，正在刷新。';await context.openTask(s.taskId);}
    }catch(error){if(matches())$('#wb-write-note').textContent=error.message;}
    finally{if(alive&&ticket===session&&context){submitting=false;render(context.store.get());}}
  }
  async function workspace() {
    reset();const ticket=session;
    root.innerHTML=`<header class="wb-header"><h1>关务审核工作台</h1><span>${esc(user.display_name||user.username)} · ${esc(user.role_label||user.role)}</span><a href="/uploads/">上传 / 认领</a><button class="btn" id="wb-logout">退出</button></header>
      <p class="wb-banner">真实会话与 API。AI 为 mock / inline；合成结果会醒目标注，不构成申报依据。后台队列与失败行重试待 P13 交付。</p>
      <main class="wb-layout"><aside id="wb-tasks"></aside><section class="wb-detail"><h1 id="wb-title">选择左侧任务</h1><div id="wb-context-error"></div><div id="wb-toolbar" class="wb-toolbar"></div><p id="wb-summary"></p>
      <div class="wb-results"><div id="wb-progress"></div><div id="wb-exports"></div></div>
      <div class="wb-goods"><section aria-label="本版本商品" id="wb-items"></section><section id="wb-card" tabindex="-1" aria-label="商品卡片"></section></div><p id="wb-source"></p>
      <button class="btn primary" id="wb-submit" hidden>提交本批次专家审核</button><p id="wb-write-note" role="status"></p><div id="wb-review"></div></section></main>`;
    context=createWorkbenchContext(client);
    exports=createExportActions($('#wb-exports'),{client});
    progress=createRunProgress($('#wb-progress'),{client,onStateChange:p=>context?.acceptProgress(p),onSelectItem:async identity=>{
      const ctx=context;await ctx.selectItem(identity.item_id,identity);
      if(context===ctx&&ctx.store.get().card?.item_id===identity.item_id){$('#wb-card').scrollIntoView({block:'start',behavior:'smooth'});$('#wb-card').focus();}
    }});
    tasks=createTaskList($('#wb-tasks'),{client,onSelectionStart:id=>{clearReview();$('#wb-write-note').textContent='';context.beginTask(id);},onSelectTask:t=>context.acceptTask(t)});
    unsubscribe=context.store.subscribe(render);render(context.store.get());
    $('#wb-submit').onclick=submitBatch;
    $('#wb-logout').onclick=async()=>{
      reset();user=null;root.innerHTML='<main class="wb-login"><p>正在退出…</p></main>';
      let failure=null;try{await api.logout();}catch(error){failure=error;}
      if(alive)gate(failure);
    };
    await tasks.load();
    if(ticket!==session||!context)return;
    const requested=new URLSearchParams(location.search).get('task');if(requested)await context.openTask(requested);
  }
  function destroy(){alive=false;reset();}
  window.addEventListener('pagehide',destroy,{once:true});
  try {user=await api.me();if(!alive)return;if(user)await workspace();else gate();}catch(error){gate(error);}
  return {destroy};
}
