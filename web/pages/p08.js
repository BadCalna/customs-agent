/* =============================================================================
 * web/pages/p08.js —— P08 开发验收页面：任务列表、阶段筛选与执行进度（只读）
 *
 * 入口 `/tasks/` 与 /demo/、/workbench/、/uploads/ 同规格：只在 DEBUG=True 时可用，
 * 云端正式前端需要另做（本页不作为生产部署承诺）。
 *
 * 1. 数据只经 web/api.js 的 createClient 读取：?mode=live（默认）走 /api/v1，
 *    ?mode=fixture 走 contracts/fixtures/DEMO-SET.json 的演示样例（不校验权限）。
 * 2. 页面本身不提供任何写动作：不传 onRetry，也不调用上传 / 认领 / 确认 / 提交 /
 *    决定 / 导出端点；run-progress 的重试按钮因此保持“未接通”的禁用状态。
 * 3. 选中任务后才读取执行记录；错误一律走组件已有的 errorView / errorHtml 分类，
 *    没有执行记录按空态展示，不当成失败，也不把未知状态猜成业务状态。
 *
 * 本文件同时导出纯逻辑（模式解析、任务→进度请求、空态文案、错误分类、选择序号），
 * 这些函数不触碰 DOM，可被 Node 直接导入做行为测试。
 * ========================================================================== */

import {createClient} from "../api.js";
import {createTaskList, errorHtml, errorView, escapeHtml} from "../components/task-list.js";
import {createRunProgress} from "../components/run-progress.js";

export const MODE_LIVE = "live";
export const MODE_FIXTURE = "fixture";

/** 与 web/app.js 一致：只有 ?mode=fixture 才读契约样例，缺省与未知值都按 live。 */
export function parseMode(search = "") {
  const requested = new URLSearchParams(String(search || "")).get("mode");
  return requested === MODE_FIXTURE ? MODE_FIXTURE : MODE_LIVE;
}

export function modeLabel(mode) {
  return mode === MODE_FIXTURE ? "FIXTURE 样例 · 不校验权限 · 不写库" : "LIVE API /api/v1 · 只读";
}

/** 数据来源说明：两种模式都显式说明“合成样例”的含义，避免 mock 被当成真实结论。 */
export function modeNotice(mode) {
  return mode === MODE_FIXTURE
    ? "当前为 FIXTURE 模式：任务与执行数据来自 contracts/fixtures/DEMO-SET.json，不校验权限、不写库。标为「合成样例」的结果 result_mode=mock，不是真实 AI 审核结论。"
    : "当前为 LIVE 模式：数据来自 /api/v1 只读接口。结果中 result_mode=mock 的部分会标为「合成样例」，不是真实 AI 审核结论。";
}

/** 任务详情里的版本号；取不到就返回 null，不猜。 */
export function versionNumber(task, versionId) {
  const versions = Array.isArray(task?.versions) ? task.versions : [];
  const number = versions.find((entry) => entry?.version_id === versionId)?.number;
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

/**
 * 任务详情 → 执行进度请求（只读）。
 * - run：任务带 latest_run_id 且有当前版本，可直接按 run_id + version_id 读取；
 * - empty：尚无执行记录，是空态不是失败；
 * - invalid：详情自身不一致，无法确认执行归属，明确停下而不是凭猜测读某个 run。
 */
export function planRunRequest(task) {
  if (!task || typeof task.task_id !== "string" || !task.task_id) {
    return {kind: "invalid", message: "任务详情缺少任务编号，无法读取执行进度。"};
  }
  if (!task.latest_run_id) {
    return {kind: "empty", message: "该任务尚无执行记录；执行开始后会在此显示进度。没有执行记录不代表执行失败。"};
  }
  if (!task.current_version_id) {
    return {kind: "invalid", message: "任务已有执行编号，但详情缺少当前版本编号，无法确认执行归属，已停止读取。"};
  }
  return {
    kind: "run",
    context: {
      taskId: task.task_id,
      versionId: task.current_version_id,
      runId: task.latest_run_id,
      versionNumber: versionNumber(task, task.current_version_id),
      // 这是“所选版本就是任务当前版本”的陈述；执行记录归属仍由组件按 task_id / version_id 校验。
      isVersionCurrent: true,
    },
  };
}

/** 任务详情摘要（纯文本，插入页面时按 textContent 使用，不再拼 HTML）。 */
export function versionSummary(task) {
  if (!task || !task.task_id) return "尚未选择任务";
  const versions = Array.isArray(task.versions) ? task.versions : [];
  const current = versions.find((entry) => entry?.version_id === task.current_version_id);
  const head = current ? `当前版本 V${current.number}` : "任务尚无当前版本";
  const filename = current?.original_filename ? ` · ${current.original_filename}` : "";
  const total = versions.length ? ` · 共 ${versions.length} 个版本` : "";
  const latest = task.latest_run_id ? " · 显示当前版本最新一轮执行" : " · 尚无执行记录";
  return `${head}${filename}${total}${latest}`;
}

/** 所选任务的合成样例标记：只有服务端真的给了 result_mode=mock 才出现。 */
export function selectionMarkers(task) {
  const mock = task?.result_mode === "mock";
  return {
    mock,
    title: mock ? "合成样例（result_mode=mock）" : "",
    text: mock
      ? "所选任务的最新结果标为合成样例，不是真实 AI 审核结论，不构成申报依据；执行完成也不等于审核通过。"
      : "",
  };
}

/** 页面级提示块：kind 决定标题与 role，所有外部文本都转义。 */
export function noticeHtml({kind = "info", title = "", message = ""} = {}) {
  const headings = {info: "说明", empty: "尚无执行记录", invalid: "无法确认执行归属", error: "暂时无法更新"};
  const alert = kind === "invalid" || kind === "error";
  return `<div class="p08-notice is-${escapeHtml(kind)}" role="${alert ? "alert" : "status"}">`
    + `<strong>${escapeHtml(title || headings[kind] || headings.info)}</strong>`
    + `<p>${escapeHtml(message)}</p></div>`;
}

/** 身份/会话展示：错误复用组件已有的 errorView 分类，不伪造成功。 */
export function identityView({me = null, error = null, mode = MODE_LIVE} = {}) {
  if (error) return {kind: "error", label: `身份读取失败：${errorView(error).title}`};
  if (!me) {
    return {
      kind: "anonymous",
      label: mode === MODE_FIXTURE ? "演示账号（FIXTURE 模式不校验身份）" : "未登录：LIVE 模式读取任务需要登录会话",
    };
  }
  const name = me.display_name || me.username || "未知账号";
  return {kind: "user", label: `${name} · ${me.role_label || me.role || "角色未提供"}`};
}

/**
 * 装配页面。返回的 ready 完成“读身份 + 读第一页任务”后可等待；
 * 之后的一切读取都由列表交互触发，页面自己不发写请求。
 */
export function mountP08Page({client, mode = MODE_LIVE, taskHost, runHost, identityHost = null,
  noticeHost = null, summaryHost = null, modeHost = null, pageSize = 10} = {}) {
  if (!taskHost || !runHost) throw new TypeError("需要任务列表与执行进度容器");
  if (!client || typeof client.listTasks !== "function" || typeof client.getTask !== "function"
    || typeof client.getRun !== "function") throw new TypeError("需要统一只读 client");

  const state = {mode, selectedId: null, detail: null, identityError: null, selectionNotice: ""};
  let alive = true;

  if (modeHost) {
    modeHost.textContent = modeLabel(mode);
    modeHost.className = mode === MODE_FIXTURE ? "badge gold" : "badge teal";
  }

  // 只读挂载：不传 onRetry / retryAllowed，重试按钮保持组件默认的“未接通”禁用态。
  const progress = createRunProgress(runHost, {client});
  const tasks = createTaskList(taskHost, {client, pageSize, onSelectTask: selectTask, onLoaded: reconcileSelection});

  function paintNotice() {
    if (!noticeHost) return;
    noticeHost.innerHTML = [
      `<div class="p08-notice is-info" role="note"><strong>数据来源与样例说明</strong>`
        + `<p>${escapeHtml(modeNotice(mode))}</p></div>`,
      state.selectionNotice,
      state.identityError ? errorHtml(state.identityError) : "",
    ].join("");
  }

  function paintSummary() {
    if (summaryHost) summaryHost.textContent = versionSummary(state.detail);
  }

  /**
   * 列表刷新成功后核对已选任务。
   *
   * 任务列表与右侧详情/进度是两路数据：另一个标签页上传新版本后，列表「刷新」会拿到新的
   * current_version_id / latest_run_id，但右侧仍是上一次选择时的详情，会继续把旧版本
   * 说成「当前版本」。列表的刷新按钮调用的是组件内部 load()，因此核对必须挂在这个回调上。
   */
  async function reconcileSelection(items) {
    if (!alive || !state.selectedId || !state.detail) return;
    const entry = (Array.isArray(items) ? items : []).find((row) => row && row.task_id === state.selectedId);
    if (!entry) return; // 当前页没有该任务：不动右侧，避免把「不在本页」误当成「已删除」
    const detail = state.detail;
    const versionChanged = (entry.current_version_id || null) !== (detail.current_version_id || null);
    const runChanged = (entry.latest_run_id || null) !== (detail.latest_run_id || null);
    if (!versionChanged && !runChanged) return;
    // 重读详情后再更新右侧与进度上下文；不能拿旧详情继续自称「当前版本」。
    await tasks.selectTask(state.selectedId);
  }

  async function selectTask(detail) {
    if (!alive) return;
    state.selectedId = detail?.task_id || null;
    state.detail = detail;
    paintSummary();
    const plan = planRunRequest(detail);
    const markers = selectionMarkers(detail);
    state.selectionNotice = plan.kind === "run"
      ? (markers.mock ? noticeHtml({kind: "info", title: markers.title, message: markers.text}) : "")
      : noticeHtml({kind: plan.kind === "invalid" ? "invalid" : "empty", message: plan.message});
    paintNotice();
    if (plan.kind !== "run") {
      // 没有执行记录：收起进度面板（组件空态文案是“等待选择”，留在页面上会误导），
      // 由页面提示块说明这是空态而不是失败。
      runHost.hidden = true;
      await progress.open(null);
      return;
    }
    runHost.hidden = false;
    // 旧选择的执行响应由 run-progress 的 epoch 丢弃，页面不再二次渲染它。
    await progress.open(plan.context);
  }

  async function loadIdentity() {
    try {
      const me = await client.me();
      if (!alive) return null;
      state.identityError = null;
      if (identityHost) identityHost.textContent = identityView({me, mode}).label;
      return me;
    } catch (error) {
      if (!alive) return null;
      state.identityError = error;
      if (identityHost) identityHost.textContent = identityView({error, mode}).label;
      paintNotice();
      return null;
    }
  }

  async function load() {
    paintNotice();
    await tasks.load();
    return tasks.getState();
  }

  const ready = (async () => {
    paintNotice();
    await loadIdentity();
    if (alive) await load();
  })();

  return {
    ready,
    load,
    selectTask: (taskId) => tasks.selectTask(taskId),
    tasks,
    progress,
    getState: () => ({...state, alive}),
    destroy() {
      alive = false;
      tasks.destroy();
      progress.destroy();
      if (noticeHost) noticeHost.innerHTML = "";
      if (runHost) runHost.hidden = true;
    },
  };
}

function bootstrap() {
  const mode = parseMode(globalThis.location?.search || "");
  const client = createClient({mode});
  const byId = (id) => globalThis.document.getElementById(id);
  const page = mountP08Page({
    client,
    mode,
    taskHost: byId("p08-tasks"),
    runHost: byId("p08-run"),
    identityHost: byId("p08-identity"),
    noticeHost: byId("p08-notice"),
    summaryHost: byId("p08-summary"),
    modeHost: byId("p08-mode"),
    pageSize: 10,
  });
  globalThis.addEventListener("pagehide", () => page.destroy(), {once: true});
  return page;
}

// 只有真实页面才挂载；Node 里导入本模块只拿到纯逻辑与 mountP08Page，不会触发 DOM 操作。
if (typeof document !== "undefined") bootstrap();
