import {escapeHtml as esc, errorHtml} from './task-list.js';

export function exportSelection(s, role) {
  const run = s.run;
  if (!['operator','expert'].includes(role)) return {reason: '审核导出仅供获得本任务访问权限的业务员或专家使用。'};
  if (s.loading || s.error || s.progressError) return {reason: '请先成功读取所选版本与执行结果。'};
  if (!s.task || !s.versionId || !run || run.task_id !== s.taskId || run.version_id !== s.versionId || run.run_id !== s.runId || run.kind !== s.kind) return {reason: '请选择明确的任务、文件版本、阶段和执行记录。'};
  if (!['succeeded','failed'].includes(run.status)) return {reason: '执行尚未结束或已作废，暂不能导出。'};
  if (!Array.isArray(run.items) || !s.items.length || run.items.length !== s.items.length || new Set(run.items.map(i=>i.item_id)).size !== s.items.length || run.items.some(i=>!s.items.some(x=>x.item_id===i.item_id) || !['succeeded','failed'].includes(i.status))) return {reason: '执行结果未覆盖本版本全部商品，暂不能导出。'};
  let submission = null;
  if (run.kind === 'expert_check') {
    submission = s.history?.versions?.find(v=>v.version_id===s.versionId)?.submissions?.find(x=>x.submission_id===run.submission_id);
    if (!submission) return {reason: '缺少该专家核查对应的提交快照，暂不能导出。'};
    if (s.decisionId && !(submission.decisions || []).some(d=>d.decision_id===s.decisionId)) return {reason: '专家决定与所选提交不匹配。'};
  }
  return {versionId: s.versionId, taskId: s.taskId, runId: run.run_id, runKind: run.kind,
    submissionId: submission?.submission_id || null, decisionId: s.decisionId || null};
}

export function downloadAttachment(attachment, env = globalThis) {
  const url = env.URL.createObjectURL(attachment.blob);
  const anchor = env.document.createElement('a');
  anchor.href = url; anchor.download = attachment.filename;
  try { env.document.body.append(anchor); anchor.click(); }
  finally { anchor.remove(); env.setTimeout(()=>env.URL.revokeObjectURL(url), 1000); }
}

export function createExportActions(host, {client, download = downloadAttachment} = {}) {
  let alive = true, epoch = 0, context = null, key = '', reason = '请选择任务和执行记录。', busy = false, error = null, success = '';
  function draw() {
    if (!alive) return;
    const expert = context?.runKind === 'expert_check';
    host.innerHTML = `<section class="wb-exports" aria-label="审核导出"><h2>审核结果导出</h2>
      <p>文件绑定所选版本及执行记录；导出不会提交、批准、退回或通知。</p>
      <button class="btn" data-export="precheck" ${busy || reason || expert ? 'disabled' : ''}>${busy ? '正在导出…' : '导出审核工作簿'}</button>
      <button class="btn" data-export="issues" ${busy || reason ? 'disabled' : ''}>${busy ? '正在导出…' : '导出补料清单'}</button>
      ${reason ? `<p role="status">${esc(reason)}</p>` : `<p>Run <code>${esc(context.runId)}</code>${context.submissionId ? ` · 提交 <code>${esc(context.submissionId)}</code>` : ''}</p>`}
      ${expert ? '<p>专家工作簿暂不可用：expert_layout_pending，写回列布局尚未确认。补料清单可按所选提交导出。</p>' : ''}
      ${error ? errorHtml(error) : ''}${success ? `<p role="status">已发起下载：${esc(success)}</p>` : ''}</section>`;
  }
  function update(state, role) {
    const selection = exportSelection(state, role);
    const next = JSON.stringify([state.taskId,state.versionId,state.kind,state.runId,state.decisionId,role]);
    if (key !== next || (busy && selection.reason)) {epoch++; busy = false; error = null; success = ''; key = next;}
    context = selection.reason ? null : selection; reason = selection.reason || ''; draw();
  }
  async function start(kind) {
    if (!alive || busy || !context || reason || !['precheck','issues'].includes(kind) || kind === 'precheck' && context.runKind === 'expert_check') return;
    const n = epoch, selected = {...context}; busy = true; error = null; success = ''; draw();
    try {
      const result = await client.exportFile(selected.versionId, {kind, run_id: selected.runId,
        ...(selected.submissionId ? {submission_id: selected.submissionId} : {}), ...(selected.decisionId ? {decision_id: selected.decisionId} : {})});
      if (!alive || n !== epoch || !context || reason) return;
      // The server echoes the bound run. Never turn a mismatched attachment into a successful download.
      if (result.runId !== selected.runId) throw new Error('附件执行引用与所选结果不一致，已取消下载');
      download(result); success = result.filename;
    } catch (e) {if (alive && n === epoch) error = e;}
    finally {if (alive && n === epoch) {busy = false; draw();}}
  }
  function click(e) {const b = e.target.closest('[data-export]'); if (b && host.contains(b) && !b.disabled) void start(b.dataset.export);}
  host.addEventListener('click',click); draw();
  return {update,start,destroy(){alive=false;epoch++;host.removeEventListener('click',click);host.replaceChildren();}};
}
