/** P08: task reads use the shared client; mutations belong to the host page. */
export const TASK_STAGES = Object.freeze({
  uploaded: "已上传", prechecking: "预审中", awaiting_confirmation: "待业务员确认",
  submitted: "已提交专家", approved: "已通过", returned: "已退回",
});
export const RUN_STATUSES = Object.freeze({
  queued: "排队中", running: "执行中", succeeded: "执行完成", failed: "执行失败", superseded: "已作废",
});
export const RUN_KINDS = Object.freeze({ precheck: "AI预审", expert_check: "专家AI核查" });

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}
const esc = escapeHtml;
export function errorView(error) {
  const status = Number(error?.status);
  const title = ({401: "登录已失效", 403: "当前操作不允许", 404: "对象不存在或不可见",
    409: "内容已更新，请刷新", 501: "该功能尚未接通"})[status] || "暂时无法更新";
  return { title, message: String(error?.message || "请检查连接后重试"), requestId: error?.requestId || "" };
}
export function errorHtml(error) {
  const view = errorView(error);
  return `<div class="p08-error" role="alert"><strong>${esc(view.title)}</strong><p>${esc(view.message)}</p>
    ${view.requestId ? `<small>请求标识 ${esc(view.requestId)}</small>` : ""}</div>`;
}
const label = (map, value) => map[value] || `未知状态：${value ?? "未提供"}`;
function taskHtml(task, selected) {
  return `<button type="button" class="p08-task" data-task="${esc(task.task_id)}" aria-current="${task.task_id === selected}">
    <span class="p08-task-top"><span class="p08-stage" data-stage="${esc(task.stage)}">${esc(label(TASK_STAGES, task.stage))}</span>
      <span class="p08-version">${task.current_version_number ? `V${esc(task.current_version_number)}` : "未建立版本"}</span></span>
    <strong>${esc(task.title || "未命名任务")}</strong><span class="p08-task-supplier">${esc(task.supplier || "供应商未提供")}</span>
    <span class="p08-task-foot"><span>${task.latest_run_id ? `${esc(label(RUN_KINDS, task.latest_run_kind))} · ${esc(label(RUN_STATUSES, task.latest_run_status))}` : "尚无执行记录"}</span>
      ${task.result_mode === "mock" ? '<span class="p08-mock">合成样例</span>' : ""}</span></button>`;
}

export function renderTaskList(host, state) {
  const query = state.query.trim().toLocaleLowerCase();
  const visible = state.items.filter(t => [t.title, t.supplier].some(v => String(v || "").toLocaleLowerCase().includes(query)));
  host.innerHTML = `<section class="p08-task-list" aria-label="审核任务" aria-busy="${state.loading}">
    <header class="p08-heading"><div><span class="p08-eyebrow">审核任务</span><h2>我的任务</h2></div>
      <button type="button" class="p08-button" data-action="refresh" ${state.loading ? "disabled" : ""}>刷新</button></header>
    <div class="p08-filters"><label>业务阶段<select data-stage-filter><option value="">全部阶段</option>
      ${Object.entries(TASK_STAGES).map(([value, text]) => `<option value="${value}" ${state.stage === value ? "selected" : ""}>${text}</option>`).join("")}</select></label>
      <form data-search><label>搜索本页任务<input name="query" value="${esc(state.query)}" placeholder="任务名称或供应商"></label><button class="p08-button">搜索</button></form></div>
    ${state.error ? errorHtml(state.error) : ""}${state.selectionError ? errorHtml(state.selectionError) : ""}
    <p class="p08-list-note" role="status">${state.loading ? "正在读取任务…" : `第 ${state.page + 1} 页 · 本页 ${state.items.length} 个任务${query ? ` · 找到 ${visible.length} 个` : ""}`}</p>
    <div class="p08-task-items">${visible.map(t => taskHtml(t, state.selectedId)).join("") ||
      (!state.loading && !state.error ? `<div class="p08-empty"><strong>${query ? "本页没有匹配任务" : state.stage ? "该阶段暂无任务" : "暂无可见任务"}</strong><p>${query ? "搜索仅覆盖本页，可翻页查看或清空关键词。" : "上传或任务分配完成后，刷新即可查看。"}</p></div>` : "")}</div>
    ${state.selecting ? '<p class="p08-list-note" role="status">正在读取所选任务…</p>' : ""}
    <nav class="p08-pagination" aria-label="任务分页"><button class="p08-button" data-action="previous" ${!state.page || state.loading ? "disabled" : ""}>上一页</button>
      <span>第 ${state.page + 1} 页</span><button class="p08-button" data-action="next" ${!state.nextCursor || state.loading ? "disabled" : ""}>下一页</button></nav></section>`;
}

/** Mount once. Host auth/session changes must destroy and remount this controller. */
export function createTaskList(host, { client, onSelectTask = () => {}, onSelectionStart = () => {}, pageSize = 10 } = {}) {
  if (!host || !client?.listTasks || !client?.getTask) throw new TypeError("需要容器和统一任务client");
  let state = {items: [], stage: "", query: "", page: 0, nextCursor: null,
    loading: false, error: null, selectionError: null, selectedId: null, selecting: false};
  let alive = true, token = 0, selectionToken = 0;
  let cursors = [null];
  const draw = () => { if (alive) renderTaskList(host, state); };
  async function load(page = state.page) {
    if (!alive) return;
    const request = ++token;
    const cursor = cursors[page];
    state = {...state, loading: true, error: null}; draw();
    try {
      const result = await client.listTasks({stage: state.stage, limit: pageSize, cursor});
      if (!alive || request !== token) return;
      if (!Array.isArray(result?.items) || !result.items.every(t => typeof t.task_id === "string" && t.task_id)) {
        throw new Error("任务响应格式异常，未显示为成功");
      }
      state = {...state, items: result.items, nextCursor: result.next_cursor ?? null, page, loading: false};
      cursors = cursors.slice(0, page + 1);
      if (state.nextCursor) cursors[page + 1] = state.nextCursor;
    } catch (error) {
      if (!alive || request !== token) return;
      state = {...state, error, loading: false};
    }
    draw();
  }
  async function selectTask(taskId) {
    if (!alive || !state.items.some(t => t.task_id === taskId)) return;
    if (state.selecting && taskId === state.selectedId) return;
    const request = ++selectionToken;
    onSelectionStart(taskId);
    state = {...state, selectedId: taskId, selecting: true, selectionError: null}; draw();
    try {
      const detail = await client.getTask(taskId);
      if (!alive || request !== selectionToken) return;
      if (detail?.task_id !== taskId) throw new Error("所选任务响应身份不匹配");
      await onSelectTask(detail);
    } catch (error) {
      if (!alive || request !== selectionToken) return;
      state = {...state, selectionError: error};
    }
    if (!alive || request !== selectionToken) return;
    state = {...state, selecting: false}; draw();
  }
  function setStage(stage) {
    if (!alive) return;
    if (stage && !Object.hasOwn(TASK_STAGES, stage)) throw new TypeError("未知阶段筛选");
    selectionToken += 1; cursors = [null];
    onSelectionStart(null);
    state = {...state, stage, page: 0, query: "", items: [], nextCursor: null, selectedId: null, selecting: false, selectionError: null};
    return load(0);
  }
  function setQuery(query) { state = {...state, query: String(query)}; draw(); }
  function next() { if (!state.loading && state.nextCursor) return load(state.page + 1); }
  function previous() { if (!state.loading && state.page) return load(state.page - 1); }
  function click(event) {
    const button = event.target.closest("button");
    if (!button || !host.contains(button) || button.disabled) return;
    if (button.dataset.task) void selectTask(button.dataset.task);
    const actions = {refresh: () => load(), next, previous};
    if (actions[button.dataset.action]) void actions[button.dataset.action]();
  }
  function change(event) { if (event.target.matches("[data-stage-filter]")) void setStage(event.target.value); }
  function submit(event) {
    if (!event.target.matches("[data-search]")) return;
    event.preventDefault(); setQuery(new FormData(event.target).get("query") || "");
  }
  host.addEventListener("click", click); host.addEventListener("change", change); host.addEventListener("submit", submit);
  draw();
  return {load, setStage, setQuery, next, previous, selectTask, getState: () => ({...state}),
    destroy() { alive = false; token += 1; selectionToken += 1;
      host.removeEventListener("click", click); host.removeEventListener("change", change); host.removeEventListener("submit", submit); host.replaceChildren(); }};
}
