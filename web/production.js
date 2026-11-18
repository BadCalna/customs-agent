import {createClient} from "/app/assets/api.js";

const api = createClient({mode: "live"});
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]));
const labels = {uploaded: "已上传", prechecking: "预审中", awaiting_confirmation: "待业务确认",
  submitted: "待专家审核", approved: "已通过", returned: "已退回", queued: "排队中",
  running: "运行中", succeeded: "已完成", failed: "失败", superseded: "已替代",
  consistent: "编码一致", inconsistent: "编码不一致", needs_confirmation: "待核对",
  supplier_missing: "原码缺失", supplier_invalid: "原码异常", association_error: "关联异常"};
const label = value => labels[value] || value || "未产生";
const key = () => crypto.randomUUID();
const state = {me: null, tasks: [], cursor: null, claims: [], claimCursor: null,
  task: null, versionId: null, items: [],
  history: null, importJob: null, run: null, card: null, selectedItemId: null,
  decisionDrafts: new Map(), renderedDecisionScope: null,
  uploadIntent: null, actionKeys: new Map(), generation: 0, timer: null, busy: false};

function notice(message, error = false) {
  const host = $("#notice");
  host.textContent = message;
  host.className = error ? "error" : "";
  host.hidden = !message;
}

function fail(error) {
  if (error?.status === 401) {
    state.me = null;
    showSession();
  }
  notice(error?.message || "请求失败，请刷新后重试", true);
}

async function guarded(action) {
  if (state.busy) return;
  state.busy = true;
  try { await action(); } catch (error) { fail(error); }
  finally { state.busy = false; }
}

function showSession() {
  $("#identity").textContent = state.me ? `${state.me.display_name || state.me.username} · ${state.me.role_label}` : "";
  $("#logout").hidden = !state.me;
  $("#login-panel").hidden = !!state.me;
  $("#workspace").hidden = !state.me;
  $("#upload-area").hidden = state.me?.role !== "supplier";
  $("#claim-area").hidden = state.me?.role !== "operator";
  if (!state.me) {
    state.generation += 1;
    clearTimeout(state.timer);
    state.decisionDrafts.clear();
    state.renderedDecisionScope = null;
    state.task = null;
    state.tasks = [];
    state.claims = [];
    state.claimCursor = null;
    state.history = null;
    state.importJob = null;
    state.run = null;
    state.card = null;
    renderTaskList();
    renderDetail();
  }
}

function renderTaskList() {
  $("#task-list").innerHTML = state.tasks.length ? state.tasks.map(task =>
    `<button class="task-button ${state.task?.task_id === task.task_id ? "selected" : ""}" data-task="${esc(task.task_id)}">
      ${esc(task.title)}<small>V${esc(task.current_version_number || "-")} · ${esc(label(task.stage))}</small></button>`).join("")
    : '<p class="muted">暂无任务</p>';
  $("#more-tasks").hidden = !state.cursor;
}

async function loadTasks(append = false) {
  const page = await api.listTasks({limit: 50, cursor: append ? state.cursor : null});
  state.tasks = append ? [...state.tasks, ...page.items] : page.items;
  state.cursor = page.next_cursor;
  renderTaskList();
  if (state.me?.role === "operator" && !append) await loadClaims();
}

async function loadClaims(append = false) {
  const page = await api.listClaimable({limit: 50, cursor: append ? state.claimCursor : null});
  state.claims = append ? [...state.claims, ...page.items] : page.items;
  state.claimCursor = page.next_cursor;
  $("#more-claims").hidden = !state.claimCursor;
  $("#claim-list").innerHTML = state.claims.length ? state.claims.map(entry =>
      `<div class="claim-row"><small>${esc(entry.task_id)} · ${esc(new Date(entry.created_at).toLocaleString())}</small>
        <button data-claim="${esc(entry.task_id)}">认领</button></div>`).join("") : '<p class="muted">暂无待认领任务</p>';
}

async function allItems(versionId) {
  const rows = [];
  let cursor = null;
  do {
    const page = await api.listItems(versionId, {limit: 100, cursor});
    rows.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return rows;
}

function currentHistoryVersion() {
  return state.history?.versions.find(version => version.version_id === state.versionId);
}

function pendingSubmission() {
  return currentHistoryVersion()?.submissions.findLast(submission => submission.status === "pending");
}

function decisionScope(submission) {
  return JSON.stringify([state.task.task_id, state.versionId, submission.submission_id]);
}

function decisionDraft(scope) {
  if (!state.decisionDrafts.has(scope))
    state.decisionDrafts.set(scope, {action: "approve", approveFields: {}, returnFields: {}});
  return state.decisionDrafts.get(scope);
}

function captureDecisionDraft() {
  const form = $("#decision-form");
  if (!state.renderedDecisionScope || !form) return;
  const fields = Object.fromEntries(new FormData(form));
  const draft = decisionDraft(state.renderedDecisionScope);
  draft[fields.action === "return" ? "returnFields" : "approveFields"] = fields;
}

function activeRunId() {
  if (state.versionId === state.task?.current_version_id) return state.task.latest_run_id;
  return currentHistoryVersion()?.runs.at(-1)?.run_id || null;
}

function runMatchesScope(run, taskId, versionId, runId) {
  return !!run && run.task_id === taskId && run.version_id === versionId && run.run_id === runId;
}

async function openTask(taskId, versionId = null, itemId = null) {
  clearTimeout(state.timer);
  const generation = ++state.generation;
  // Navigation invalidates retry immediately, even while getTask is still pending.
  state.run = null;
  renderRun();
  const task = await api.getTask(taskId);
  if (generation !== state.generation) return;
  if (task.task_id !== taskId) throw new Error("任务响应归属不匹配，请刷新后重试");
  state.task = task;
  renderTaskList();
  await openVersion(versionId || task.current_version_id, itemId);
}

async function openVersion(versionId, itemId = null) {
  clearTimeout(state.timer);
  const generation = ++state.generation;
  const taskId = state.task?.task_id;
  const isCurrent = () => generation === state.generation
    && state.task?.task_id === taskId && state.versionId === versionId;
  state.versionId = versionId;
  state.items = [];
  state.run = null;
  state.importJob = null;
  state.card = null;
  state.selectedItemId = null;
  renderDetail();
  if (!versionId) return;
  if (!taskId || !state.task.versions.some(version => version.version_id === versionId))
    throw new Error("版本不属于当前任务，请刷新后重试");
  const [items, history, importJob] = await Promise.all([
    allItems(versionId), api.taskHistory(taskId),
    api.getImport(versionId).catch(error => { if (error.status === 404) return null; throw error; }),
  ]);
  if (!isCurrent()) return;
  state.items = items;
  state.history = history;
  state.importJob = importJob;
  const runId = activeRunId();
  const run = runId ? await api.getRun(runId) : null;
  if (!isCurrent()) return;
  if (run && !runMatchesScope(run, taskId, versionId, runId))
    throw new Error("运行结果归属不匹配，请刷新后重试");
  state.run = run;
  renderDetail();
  if (itemId) await openItem(itemId);
  schedulePoll(generation);
}

async function openItem(itemId) {
  const generation = state.generation;
  const card = await api.getItem(state.versionId, itemId);
  if (generation !== state.generation) return;
  state.card = card;
  state.selectedItemId = itemId;
  renderItem();
}

function schedulePoll(generation) {
  clearTimeout(state.timer);
  const active = state.importJob?.parse_status === "staged" || ["queued", "running"].includes(state.run?.status);
  if (!active) return;
  state.timer = setTimeout(async () => {
    if (generation !== state.generation) return;
    try {
      const taskId = state.task?.task_id;
      const versionId = state.versionId;
      const runId = state.run?.run_id;
      const [job, run] = await Promise.all([
        state.importJob ? api.getImport(versionId) : null,
        runId ? api.getRun(runId) : null,
      ]);
      if (generation !== state.generation || state.task?.task_id !== taskId || state.versionId !== versionId) return;
      if (run && !runMatchesScope(run, taskId, versionId, runId))
        throw new Error("运行结果归属不匹配，请刷新后重试");
      const importFinished = state.importJob?.parse_status === "staged" && job?.parse_status !== "staged";
      const runFinished = ["queued", "running"].includes(state.run?.status)
        && run && !["queued", "running"].includes(run.status);
      state.importJob = job;
      state.run = run;
      if (importFinished || runFinished) await openTask(state.task.task_id, state.versionId, state.selectedItemId);
      else { renderRun(); schedulePoll(generation); }
    } catch (error) { if (generation === state.generation) fail(error); }
  }, 2500);
}

function renderTask() {
  const host = $("#task-detail");
  captureDecisionDraft();
  const task = state.task;
  host.hidden = !task;
  if (!task) {
    host.innerHTML = "";
    state.renderedDecisionScope = null;
    return;
  }
  const current = state.versionId === task.current_version_id;
  const live = task.result_mode === "live";
  const submission = pendingSubmission();
  const versions = task.versions.map(version =>
    `<button data-version="${esc(version.version_id)}" class="${version.version_id === state.versionId ? "current" : ""}">
      V${esc(version.number)} · ${esc(version.original_filename)}</button>`).join("");
  const operatorActions = state.me.role === "operator" && current && live ? `
    <div class="actions"><button data-action="precheck" ${task.precheck_allowed ? "" : "disabled"}>重新预审</button>
      <button data-action="submit" class="primary" ${state.items.length && !["submitted", "approved"].includes(task.stage) ? "" : "disabled"}>提交专家审核</button></div>
    ${task.stage !== "approved" && task.stage !== "submitted" ? `<form id="return-form" class="action-form"><h3>退回供应商补料</h3>
      <label>原因<textarea name="reason" required></textarea></label><button type="submit" class="danger">退回</button></form>` : ""}` : "";
  const scope = state.me.role === "expert" && current && live && submission ? decisionScope(submission) : null;
  const decision = scope ? decisionPanel(submission, decisionDraft(scope)) : "";
  host.innerHTML = `<div class="section-heading"><h1>${esc(task.title)}</h1><span class="status ${esc(task.stage)}">${esc(label(task.stage))}</span></div>
    <div class="meta"><span>任务 ${esc(task.task_id)}</span><span>供应商 ${esc(task.supplier)}</span>
      <span>修订 ${esc(task.revision)}</span></div>
    ${!live ? '<p class="warning">当前结果为模拟数据，不能用于正式审核。</p>' : ""}
    <div class="version-list">${versions}</div>
    ${state.importJob ? `<p>解析：${esc(label(state.importJob.parse_status))} · 商品 ${esc(state.importJob.item_count)} 条
      · <a href="${esc(state.importJob.original_download_url)}">下载原件</a></p>
      ${Object.keys(state.importJob.error || {}).length ? `<p class="error">${esc(state.importJob.error.message || "解析失败，请修正文件后上传新版本")}</p>` : ""}` : ""}
    ${operatorActions}${decision}`;
  state.renderedDecisionScope = scope;
}

function decisionPanel(submission, draft) {
  const isApprove = draft.action === "approve";
  const approve = draft.approveFields;
  const returned = draft.returnFields;
  const names = new Map(state.items.map(item => [item.item_id, item.name]));
  return `<div class="action-form"><h3>专家决定</h3><div class="actions">
      <button data-decision-mode="approve" class="${isApprove ? "primary" : ""}">整批通过</button>
      <button data-decision-mode="return" class="${!isApprove ? "primary" : ""}">退回补料</button></div>
    <form id="decision-form" class="action-form"><input type="hidden" name="action" value="${draft.action}">
      ${isApprove ? `<div class="table-wrap"><table><thead><tr><th>商品</th><th>提交编码</th><th>专家终值</th><th>改码原因</th></tr></thead><tbody>
        ${submission.items.map(line => `<tr class="decision-row"><td>${esc(names.get(line.item_id) || line.item_id)}</td>
          <td>${esc(line.submitted_hs)}</td><td><input name="final-${esc(line.item_id)}" value="${esc(approve[`final-${line.item_id}`] ?? line.submitted_hs)}" pattern="[0-9]{10}" required></td>
          <td><input class="reason" name="reason-${esc(line.item_id)}" value="${esc(approve[`reason-${line.item_id}`] ?? "")}" placeholder="改码时必填"></td></tr>`).join("")}
        </tbody></table></div><label>审核意见<textarea name="reason">${esc(approve.reason ?? "")}</textarea></label>` : `
        <label>退回对象<select name="return_to"><option value="operator" ${returned.return_to === "supplier" ? "" : "selected"}>业务员</option><option value="supplier" ${returned.return_to === "supplier" ? "selected" : ""}>供应商</option></select></label>
        <label>退回原因<textarea name="reason" required>${esc(returned.reason ?? "")}</textarea></label>
        <label>具体问题<textarea name="issue" required>${esc(returned.issue ?? "")}</textarea></label>`}
      <button class="${isApprove ? "primary" : "danger"}" type="submit">${isApprove ? "确认整批通过" : "确认退回"}</button></form></div>`;
}

function renderRun() {
  const host = $("#run-detail");
  const run = state.run;
  host.hidden = !run && state.importJob?.parse_status !== "staged";
  if (!run) {
    host.innerHTML = state.importJob?.parse_status === "staged" ? '<p class="muted">正在解析单证...</p>' : "";
    return;
  }
  const p = run.progress;
  const active = ["queued", "running"].includes(run.status);
  host.innerHTML = `<div class="section-heading"><h2>${run.kind === "expert_check" ? "专家 AI 辅助核查" : "AI 预审"}</h2>
      <span class="status ${esc(run.status)}">${esc(label(run.status))}</span></div>
    ${state.importJob?.parse_status === "staged" ? '<p class="muted">正在解析单证...</p>'
      : run.status === "running" ? '<div class="ai-thinking" role="status"><i></i><i></i><i></i>AI 思考中...</div>'
      : active ? '<p class="muted">等待 AI 处理...</p>' : ""}
    <div class="run-counts"><span>总数 <b>${esc(p.total)}</b></span><span>完成 <b>${esc(p.succeeded)}</b></span>
      <span>失败 <b>${esc(p.failed)}</b></span><span>处理中 <b>${esc(p.running + p.queued)}</b></span></div>
    ${run.result_mode === "mock" ? '<p class="warning">模拟结果，不可作为审核依据。</p>' : ""}
    ${run.retry_allowed && run.retry_item_ids.length ? `<button data-action="retry">重试失败行（${esc(run.retry_item_ids.length)}）</button>` : ""}
    <ul class="run-rows">${run.items.map(row => `<li><a href="#item-detail" data-item="${esc(row.item_id)}">${esc(row.source_location.sheet_name)} 第${esc(row.source_location.row_number)}行</a>
      · ${esc(label(row.status))}${row.error_message ? ` · <span class="error">${esc(row.error_message)}</span>` : ""}
      ${row.comparison_status ? ` · ${esc(label(row.comparison_status))}` : ""}</li>`).join("")}</ul>`;
}

function renderItems() {
  const host = $("#item-area");
  host.hidden = !state.task || !state.versionId;
  if (host.hidden) return;
  $("#item-list").innerHTML = state.items.length ? `<div class="table-wrap"><table><thead><tr>
    <th>来源</th><th>商品</th><th>原编码</th><th>AI 建议</th><th>业务确认</th><th>专家终值</th><th>比较</th>
    </tr></thead><tbody>${state.items.map(item => `<tr data-item="${esc(item.item_id)}">
      <td>${esc(item.source_location.sheet_name)} ${esc(item.source_location.row_number)}</td><td>${esc(item.name)}</td>
      <td>${esc(item.supplier_hs_raw || "-")}</td><td>${esc(item.precheck_suggested_hs || "-")}</td>
      <td>${esc(item.business_confirmed_hs || "-")}</td><td>${esc(item.expert_final_hs || "-")}</td>
      <td>${esc(label(item.comparison_status))}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="muted">该版本暂无商品。</p>';
}

function renderItem() {
  const host = $("#item-detail");
  const card = state.card;
  host.hidden = !card;
  if (!card) return;
  const current = state.versionId === state.task.current_version_id;
  const confirm = state.me.role === "operator" && current && state.task.result_mode === "live"
    && !["submitted", "approved"].includes(state.task.stage);
  host.innerHTML = `<div class="section-heading"><h2>${esc(card.facts.name)}</h2>
      <span class="muted">${esc(card.source_location.sheet_name)} · 第${esc(card.source_location.row_number)}行</span></div>
    ${card.result_mode === "mock" ? '<p class="warning">本商品预审来自模拟结果，不能用于正式审核。</p>' : ""}
    <div class="facts"><div><small>供应商原编码</small>${esc(card.facts.supplier_hs_raw || "未填写")}</div>
      <div><small>AI 建议编码</small>${esc(card.precheck_suggested_hs.value || "未给出")}</div>
      <div><small>业务确认编码</small>${esc(card.business_confirmed_hs.value || "未确认")}</div>
      <div><small>提交编码</small>${esc(card.submitted_hs.value || "未提交")}</div>
      <div><small>专家终值</small>${esc(card.expert_final_hs.value || "未决定")}</div>
      <div><small>申报要素原文</small>${esc(card.facts.supplier_elements_raw || "未填写")}</div></div>
    ${card.issues.map(issue => `<p class="issue">${esc(issue.message)}${issue.missing ? ` · 待补：${esc(issue.missing)}` : ""}</p>`).join("")}
    ${card.evidence_status === "version_unverified" ? '<p class="warning">税则依据待人工核验。</p>' : ""}
    ${card.latest_expert_check?.findings?.length ? `<h3>专家 AI 辅助核查</h3>${card.latest_expert_check.findings.map(finding =>
      `<div class="finding"><b>${esc(finding.aspect)}</b> · ${esc(finding.verdict)} · ${esc(finding.reason)}</div>`).join("")}` : ""}
    ${card.images.map(image => `<div class="finding">附图 ${esc(image.source_cell)} · ${esc(image.status)}
      ${image.download_url ? `<a href="${esc(image.download_url)}" target="_blank" rel="noopener">查看</a><br><img class="item-image" src="${esc(image.download_url)}" alt="商品附图">` : ""}
      ${image.error_message ? `<span class="error">${esc(image.error_message)}</span>` : ""}</div>`).join("")}
    ${confirm ? `<form id="confirmation-form" class="action-form"><h3>业务员确认</h3>
      <div class="form-row"><label>确认编码<input name="hs" value="${esc(card.business_confirmed_hs.value || card.precheck_suggested_hs.value || "")}" pattern="[0-9]{10}" required></label>
      <label>确认说明<input name="reason" value=""></label><button class="primary" type="submit">保存确认</button></div></form>` : ""}`;
}

function renderHistory() {
  const host = $("#history-area");
  host.hidden = !state.history;
  if (!state.history) return;
  const events = state.history.audit_events.slice(0, 25);
  host.innerHTML = `<details><summary>操作记录（最近 ${esc(events.length)} 条）</summary><ul class="timeline">
    ${events.map(event => `<li><small>${esc(new Date(event.created_at).toLocaleString())} · ${esc(event.actor)}</small>
      <div>${esc(event.action)}${event.reason ? ` · ${esc(event.reason)}` : ""}</div></li>`).join("")}</ul></details>`;
}

function renderDetail() { renderTask(); renderRun(); renderItems(); renderItem(); renderHistory(); }

function actionKey(name, payload, resourceId = null) {
  const identity = JSON.stringify([state.task?.task_id || "new", state.versionId || "none", name, resourceId, payload]);
  if (!state.actionKeys.has(identity)) state.actionKeys.set(identity, key());
  return state.actionKeys.get(identity);
}

async function upload(file, target, confirmDuplicate = false) {
  if (target === "replace" && !state.task) throw new Error("先选择需要补料的任务");
  const taskId = target === "replace" ? state.task.task_id : null;
  const versionId = target === "replace" ? state.task.current_version_id : null;
  const signature = `${file.name}:${file.size}:${file.lastModified}:${taskId}:${versionId}`;
  if (!state.uploadIntent || state.uploadIntent.signature !== signature)
    state.uploadIntent = {signature, key: key(), file, target, taskId, versionId, confirmed: false};
  if (confirmDuplicate && !state.uploadIntent.confirmed) {
    state.uploadIntent.key = key();
    state.uploadIntent.confirmed = true;
  }
  notice("正在登记上传...");
  try {
    const result = await api.uploadFile(file, {taskId, currentVersionId: versionId,
      confirmDuplicate, key: state.uploadIntent.key});
    state.uploadIntent = null;
    $("#duplicate-area").hidden = true;
    $("#upload-form").reset();
    notice("已受理上传，解析与预审进度将自动更新。");
    await loadTasks();
    await openTask(result.task_id, result.version_id);
  } catch (error) {
    if (error.code === "duplicate_file") {
      $("#duplicate-area").hidden = false;
      notice("文件与已有版本相同。确认后会创建新版本。");
      return;
    }
    throw error;
  }
}

async function submitTask() {
  const task = state.task;
  const history = currentHistoryVersion();
  const latest = new Map();
  for (const row of history.confirmations) latest.set(row.item_id, row);
  if (!state.items.length || state.items.some(item => !latest.has(item.item_id)))
    throw new Error("还有商品未确认，请逐项保存确认值后提交");
  const payload = {version_id: state.versionId, revision: task.revision,
    items: state.items.map(item => ({item_id: item.item_id, confirmation_id: latest.get(item.item_id).confirmation_id}))};
  await api.submitTask(task.task_id, payload, actionKey("submit", payload));
  notice("已提交专家审核。");
  await openTask(task.task_id);
}

async function decide(form) {
  const submission = pendingSubmission();
  const fields = new FormData(form);
  let payload;
  if (fields.get("action") === "approve") {
    payload = {action: "approve", reason: fields.get("reason"), items: submission.items.map(line => ({
      item_id: line.item_id, final_hs: fields.get(`final-${line.item_id}`),
      change_reason: fields.get(`reason-${line.item_id}`) || "",
    }))};
    if (payload.items.some((line, index) => line.final_hs !== submission.items[index].submitted_hs && !line.change_reason.trim()))
      throw new Error("专家改码时必须填写对应商品的改码原因");
  } else {
    payload = {action: "return", return_to: fields.get("return_to"), reason: fields.get("reason"),
      issues: [{message: fields.get("issue")}]};
  }
  await api.decideSubmission(submission.submission_id, payload,
    actionKey("decision", payload, submission.submission_id));
  state.decisionDrafts.delete(decisionScope(submission));
  state.renderedDecisionScope = null;
  notice(payload.action === "approve" ? "整批审核已通过。" : "已退回补料。");
  await openTask(state.task.task_id);
}

$("#login-form").addEventListener("submit", event => {
  event.preventDefault();
  guarded(async () => {
    const data = new FormData(event.target);
    state.me = await api.login(data.get("username"), data.get("password"));
    event.target.reset();
    showSession();
    notice("");
    await loadTasks();
  });
});
$("#logout").addEventListener("click", () => guarded(async () => {
  await api.logout();
  state.me = null;
  showSession();
  notice("已退出");
}));
$("#refresh").addEventListener("click", () => guarded(async () => {
  await loadTasks();
  if (state.task) await openTask(state.task.task_id, state.versionId, state.selectedItemId);
}));
$("#more-tasks").addEventListener("click", () => guarded(() => loadTasks(true)));
$("#more-claims").addEventListener("click", () => guarded(() => loadClaims(true)));
$("#upload-form").addEventListener("submit", event => {
  event.preventDefault();
  guarded(async () => {
    const form = new FormData(event.target);
    const file = form.get("file");
    if (file?.size) await upload(file, form.get("target"));
  });
});
$("#duplicate-confirm").addEventListener("click", () => guarded(async () => {
  const intent = state.uploadIntent;
  if (!intent) return;
  if (intent.taskId !== (intent.target === "replace" ? state.task?.task_id : null)
      || intent.versionId !== (intent.target === "replace" ? state.task?.current_version_id : null))
    throw new Error("任务版本已变化，请重新选择文件上传");
  await upload(intent.file, intent.target, true);
}));
$("#upload-form").addEventListener("change", () => {
  state.uploadIntent = null;
  $("#duplicate-area").hidden = true;
});
document.addEventListener("click", event => {
  const target = event.target.closest("[data-task],[data-version],[data-item],[data-claim],[data-action],[data-decision-mode]");
  if (!target) return;
  if (target.dataset.decisionMode) {
    captureDecisionDraft();
    if (!state.renderedDecisionScope) return;
    decisionDraft(state.renderedDecisionScope).action = target.dataset.decisionMode;
    renderTask();
    return;
  }
  guarded(async () => {
    if (target.dataset.task) await openTask(target.dataset.task);
    else if (target.dataset.version) await openVersion(target.dataset.version);
    else if (target.dataset.item) await openItem(target.dataset.item);
    else if (target.dataset.claim) {
      await api.claimTask(target.dataset.claim, actionKey("claim", target.dataset.claim));
      await loadTasks();
      await openTask(target.dataset.claim);
    } else if (target.dataset.action === "precheck") {
      const payload = {revision: state.task.revision};
      await api.startPrecheck(state.versionId, payload, actionKey("precheck", payload));
      await openTask(state.task.task_id);
    } else if (target.dataset.action === "submit") await submitTask();
    else if (target.dataset.action === "retry") {
      const run = state.run;
      if (!runMatchesScope(run, state.task?.task_id, state.versionId, activeRunId())
          || !run.retry_allowed || !run.retry_item_ids?.length)
        throw new Error("当前运行不可重试，请刷新后重试");
      const taskId = state.task.task_id;
      await api.retryRun(run.run_id, run.retry_item_ids,
        actionKey("retry", {run: run.run_id, items: run.retry_item_ids}));
      await openTask(taskId);
    }
  });
});
document.addEventListener("submit", event => {
  if (!["confirmation-form", "decision-form", "return-form"].includes(event.target.id)) return;
  event.preventDefault();
  guarded(async () => {
    if (event.target.id === "confirmation-form") {
      const data = new FormData(event.target);
      const itemId = state.card.item_id;
      await api.confirmItem(state.versionId, itemId, {confirmed_hs: data.get("hs"),
        reason: data.get("reason") || "", expected_revision: state.card.revision});
      notice("确认已保存。");
      await openTask(state.task.task_id, state.versionId, itemId);
    } else if (event.target.id === "decision-form") await decide(event.target);
    else {
      const data = new FormData(event.target);
      const payload = {version_id: state.versionId, revision: state.task.revision,
        reason: data.get("reason"), issues: [{message: data.get("reason")}]};
      await api.returnTask(state.task.task_id, payload, actionKey("return", payload));
      notice("已退回供应商补料。");
      await openTask(state.task.task_id);
    }
  });
});

api.me().then(async me => {
  state.me = me;
  showSession();
  if (me) await loadTasks();
}).catch(fail);
