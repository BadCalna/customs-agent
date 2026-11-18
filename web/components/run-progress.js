/** P08: serial polling, context guards, and explicit retry intents. No HTTP writes. */
import {escapeHtml as esc, errorHtml, RUN_KINDS, RUN_STATUSES} from "./task-list.js";

const COUNTS = ["queued", "running", "succeeded", "failed"];
const COMPARISONS = {association_error: "关联异常", supplier_missing: "原码缺失", supplier_invalid: "原码异常",
  needs_confirmation: "待确认 / 补充资料", consistent: "编码一致", inconsistent: "编码不一致"};
const known = (map, key) => map[key] || `未知状态：${key ?? "未提供"}`;
export function progressView(run) {
  const p = run?.progress;
  if (!p || !["total", ...COUNTS].every(k => Number.isSafeInteger(p[k]) && p[k] >= 0)
      || COUNTS.reduce((n, k) => n + p[k], 0) !== p.total) {
    throw new Error("服务端进度计数不一致，不能推算完成率");
  }
  if (!Array.isArray(run.items) || new Set(run.items.map(i => i.item_id)).size !== run.items.length
      || run.items.length > p.total || run.items.some(i => !i.item_id)) throw new Error("执行商品列表格式异常");
  for (const status of COUNTS) {
    if (run.items.filter(i => i.status === status).length > p[status]) throw new Error("逐行状态超出服务端计数");
  }
  const completed = p.succeeded + p.failed;
  const failureIds = run.items.filter(i => i.status === "failed").map(i => i.item_id);
  const businessRows = run.items.filter(i => i.status === "succeeded" &&
    (i.issues?.length || run.kind === "precheck" && i.comparison_status !== "consistent"));
  return {counts: {...p}, completed, percent: p.total ? Math.floor(completed * 100 / p.total) : null,
    failureIds, businessRows: businessRows.length,
    active: ["queued", "running"].includes(run.status)};
}

function rowHtml(item, selected, retryEnabled, kind) {
  const failed = item.status === "failed";
  const location = item.source_location || {};
  const businessText = kind === "expert_check"
    ? (item.issues?.length ? "有核查问题，待专家确认" : "核查执行已完成，详细结论请查看商品详情")
    : kind === "precheck" ? known(COMPARISONS, item.comparison_status) : "详细业务结论请查看商品详情";
  const suggestionLabel = kind === "expert_check" ? "专家建议编码" : kind === "precheck" ? "预审建议编码" : "建议编码";
  return `<li class="p08-run-row ${failed ? "is-failed" : ""}">
    <div class="p08-row-main">${failed ? `<input type="checkbox" data-failed="${esc(item.item_id)}" aria-label="选择第${esc(location.row_number ?? "未知")}行重试" ${selected.includes(item.item_id) ? "checked" : ""} ${retryEnabled ? "" : "disabled"}>` : '<span class="p08-row-dot" aria-hidden="true"></span>'}
      <button type="button" class="p08-row-link" data-item="${esc(item.item_id)}">${esc(location.sheet_name || "原表")} · 第${esc(location.row_number ?? "—")}行</button>
      <span class="p08-row-status">${esc(known(RUN_STATUSES, item.status))}</span></div>
    ${failed ? `<p class="p08-technical">技术失败：${esc(item.error_message || "服务端未提供错误原因")}</p>` :
      item.status === "succeeded" ? `<p class="p08-business">${esc(businessText)}</p>` : ""}
    ${(item.issues || []).map(i => `<p class="p08-issue">${esc(i.message)}${i.missing ? `；待补：${esc(i.missing)}` : ""}</p>`).join("")}
    <small>本轮已尝试 ${esc(item.attempts ?? "—")} 次${item.origin_run_id ? ' · 沿用前次结果' : ''}${item.suggested_hs ? ` · ${suggestionLabel} ${esc(item.suggested_hs)}` : ""}</small></li>`;
}

function aiThinkingHtml(run, view) {
  if (!view.active || run.status !== "running") return "";
  return `<div class="p08-ai-thinking" role="status" aria-live="polite">
    <span class="p08-ai-orb" aria-hidden="true"></span>
    <strong>AI 思考中...</strong>
  </div>`;
}

export function renderRunProgress(host, state, {retryConnected = false} = {}) {
  const run = state.run;
  const view = run ? progressView(run) : null;
  const historical = run && (!state.context?.isVersionCurrent || run.is_current !== true);
  const canRetry = !!(run && !historical && !view.active && !state.error && state.retryAllowed && retryConnected && !state.retrying && !state.loading);
  host.innerHTML = `<section class="p08-run" aria-label="执行进度" aria-busy="${state.loading}">
    <header class="p08-heading"><div><span class="p08-eyebrow">${run?.kind === "expert_check" ? "物流审核参考" : "业务员参考"}</span><h2>${run ? esc(known(RUN_KINDS, run.kind)) : "运行进度"}</h2></div>
      <button class="p08-button" data-action="refresh" ${!state.context?.runId || state.loading || state.retrying ? "disabled" : ""}>${state.error ? "重新连接" : "刷新进度"}</button></header>
    ${state.context ? `<p class="p08-run-context">${state.context.versionNumber ? `V${esc(state.context.versionNumber)}` : "所选版本"} · <code>${esc(state.context.runId)}</code></p>` : ""}
    ${state.error ? errorHtml(state.error) : ""}
    ${state.error && run ? '<p class="p08-stale">更新已暂停，以下是上次读取的结果。</p>' : ""}
    ${state.loading ? '<p class="p08-list-note" role="status">正在读取服务端进度…</p>' : ""}
    ${!run && !state.loading ? `<div class="p08-empty"><strong>${state.context?.runId ? "暂未取得执行记录" : "选择任务后查看运行进度"}</strong><p>按所选文件版本查看，旧版本结果保留为历史。</p></div>` : ""}
    ${run ? `<div class="p08-run-flags"><span class="p08-stage">${esc(known(RUN_STATUSES, run.status))}</span>${run.result_mode === "mock" ? '<span class="p08-mock">合成样例 · 非真实AI结论</span>' : ""}
      ${historical ? '<span class="p08-history">历史执行 · 只读</span>' : ""}</div>
      ${aiThinkingHtml(run, view)}
      <div class="p08-progress-label"><strong>${view.completed}<span> / ${view.counts.total} 项已结束</span></strong><span>${view.percent === null ? "无商品" : `${view.percent}%`}</span></div>
      <div class="p08-meter" role="progressbar" aria-label="技术执行进度" aria-valuemin="0" aria-valuemax="${view.counts.total || 1}" aria-valuenow="${view.completed}">
        ${COUNTS.map(k => `<span class="is-${k}" style="width:${view.counts.total ? view.counts[k] * 100 / view.counts.total : 0}%"></span>`).join("")}</div>
      <dl class="p08-counts">${COUNTS.map(k => `<div><dt>${esc(known(RUN_STATUSES, k))}</dt><dd>${view.counts[k]}</dd></div>`).join("")}</dl>
      <p class="p08-explanation">执行完成表示分析已结束。是否通过，以人工审核决定为准。</p>
      ${run.kind === "expert_check" ? '<p class="p08-explanation">问题数量仅统计已返回的核查问题，未返回问题不代表核查通过。要素、图片、退税及监管的详细结论请查看商品详情。</p>' : ""}
      ${view.businessRows ? `<p class="p08-business-summary">已返回的成功行中，${view.businessRows} 项${run.kind === "expert_check" ? "有已返回的核查问题。核查问题" : "有业务待核信息。待补资料"}不计入技术失败。</p>` : ""}
      ${view.counts.failed ? `<div class="p08-retry"><strong>技术失败 ${view.counts.failed} 项</strong><p>仅重试已返回的失败行，保留成功行及原失败记录。</p>
        <button class="p08-button" data-action="select-failed" ${canRetry ? "" : "disabled"}>选择可重试失败行</button>
        <button class="p08-button p08-primary" data-action="retry" ${canRetry && state.selected.length ? "" : "disabled"}>${state.retrying ? "正在提交重试…" : `重试所选 ${state.selected.length} 项`}</button>
        ${!retryConnected ? '<p>重试入口待后台接通：P13 重试接口与服务端授权能力尚未发布，当前未发送重试请求。</p>' : !state.retryAllowed ? '<p>当前未获得重试授权。</p>' : historical ? '<p>历史执行不能从这里重试。</p>' : view.active ? '<p>本轮仍在执行，结束后可重试失败行。</p>' : ""}</div>` : ""}
      ${state.retryError ? errorHtml(state.retryError) : ""}
      <ul class="p08-run-rows">${run.items.map(i => rowHtml(i, state.selected, canRetry, run.kind)).join("")}</ul>
      ${run.items.length < view.counts.total ? '<p class="p08-list-note">当前逐行明细未全部返回，计数仍以服务端汇总为准。</p>' : ""}` : ""}</section>`;
}

/** Context is mandatory; a parent version switch must call open() (even with no run). */
export function createRunProgress(host, {client, onRetry = null, retryAllowed = false,
  onSelectItem = () => {}, onStateChange = () => {}, intervalMs = 2500, scheduler = globalThis} = {}) {
  if (!host || !client?.getRun) throw new TypeError("需要容器和统一执行client");
  let state = {context: null, run: null, loading: false, error: null, retryError: null,
    retrying: false, retryAllowed: !!retryAllowed, selected: []};
  let alive = true, epoch = 0, request = 0, timer = null, retryIntent = null;
  const connected = typeof onRetry === "function";
  const draw = () => { if (alive) { renderRunProgress(host, state, {retryConnected: connected}); onStateChange({...state}); } };
  function clearTimer() { if (timer !== null) scheduler.clearTimeout(timer); timer = null; }
  function matches(run, context) {
    return run?.run_id === context.runId && run.task_id === context.taskId && run.version_id === context.versionId && (!context.kind || run.kind === context.kind);
  }
  async function refresh() {
    if (!alive || !state.context?.runId || state.loading || state.retrying) return;
    clearTimer();
    const generation = epoch, ticket = ++request, context = {...state.context};
    state = {...state, loading: true, error: null}; draw();
    try {
      const run = await client.getRun(context.runId);
      if (!alive || generation !== epoch || ticket !== request) return;
      if (!matches(run, context)) throw new Error("执行记录不属于所选任务或版本，已停止更新");
      const view = progressView(run);
      state = {...state, run, loading: false,
        retryAllowed: typeof run.retry_allowed === "boolean" ? run.retry_allowed : state.retryAllowed,
        selected: state.selected.filter(id => view.failureIds.includes(id) && (!Array.isArray(run.retry_item_ids) || run.retry_item_ids.includes(id)))};
      draw();
      if (view.active && context.isVersionCurrent) timer = scheduler.setTimeout(() => { timer = null; void refresh(); }, Math.max(250, intervalMs));
    } catch (error) {
      if (!alive || generation !== epoch || ticket !== request) return;
      state = {...state, loading: false, error}; draw(); // A failed poll pauses until an explicit refresh.
    }
  }
  function open(context) {
    if (!alive) return;
    clearTimer(); epoch += 1; request += 1; retryIntent = null;
    if (context?.runId && (!context.taskId || !context.versionId)) throw new TypeError("执行必须带任务及版本身份");
    state = {...state, context: context ? {...context} : null, run: null, loading: false,
      error: null, retryError: null, retrying: false, selected: []};
    draw(); return refresh();
  }
  function selectFailed(ids) {
    if (!alive || state.retrying || !state.run) return;
    const failed = progressView(state.run).failureIds;
    const selected = [...new Set(ids)].filter(id => failed.includes(id)).sort();
    if (selected.join("|") !== state.selected.join("|")) retryIntent = null;
    state = {...state, selected}; draw();
  }
  async function retry() {
    if (!alive || state.retrying || state.loading || state.error || !state.run || !connected || !state.retryAllowed
        || !state.context.isVersionCurrent || state.run.is_current !== true || progressView(state.run).active || !state.selected.length) return;
    const generation = epoch, context = {...state.context};
    retryIntent ||= {task_id: context.taskId, version_id: context.versionId, run_id: context.runId,
      item_ids: [...state.selected], idempotency_key: globalThis.crypto.randomUUID()};
    state = {...state, retrying: true, retryError: null}; draw();
    try {
      const result = await onRetry({...retryIntent, item_ids: [...retryIntent.item_ids]});
      if (!alive || generation !== epoch) return;
      if (!result?.run_id || result.run_id === context.runId
          || (result.task_id && result.task_id !== context.taskId)
          || (result.version_id && result.version_id !== context.versionId)) {
        throw new Error("未取得本版本的新执行引用，请核对服务端；原失败记录保留");
      }
      state = {...state, retrying: false};
      await open({...context, runId: result.run_id}); // Read the new run; never rewrite old successes/failures.
    } catch (retryError) {
      if (!alive || generation !== epoch) return;
      state = {...state, retrying: false, retryError}; draw();
    }
  }
  function click(event) {
    const button = event.target.closest("button");
    if (!button || !host.contains(button) || button.disabled) return;
    if (button.dataset.item && state.context) onSelectItem({task_id: state.context.taskId,
      version_id: state.context.versionId, run_id: state.context.runId, item_id: button.dataset.item});
    if (button.dataset.action === "refresh") void refresh();
    if (button.dataset.action === "select-failed") selectFailed(progressView(state.run).failureIds);
    if (button.dataset.action === "retry") void retry();
  }
  function change(event) {
    const input = event.target;
    if (input.matches("[data-failed]")) selectFailed(input.checked ? [...state.selected, input.dataset.failed] : state.selected.filter(id => id !== input.dataset.failed));
  }
  host.addEventListener("click", click); host.addEventListener("change", change); draw();
  return {open, refresh, retry, selectFailed, getState: () => ({...state}),
    destroy() { alive = false; epoch += 1; request += 1; clearTimer();
      host.removeEventListener("click", click); host.removeEventListener("change", change); host.replaceChildren(); }};
}
