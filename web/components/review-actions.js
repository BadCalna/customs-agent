/* =============================================================================
 * web/components/review-actions.js —— 处置表单（P11）
 *
 * 范围：业务员「确认 HS / 整批提交」和专家「通过 / 退回」两组表单。
 * 只做视图与请求编排，不拼请求地址、不读 cookie、不保存令牌、不写本地存储。
 *
 * 唯一的请求出口是调用方传入的 api 对象。三个写动作集中在 WRITE_CALLS，
 * 组件内部不再有第二处写请求：
 *
 *   confirm({confirmed_hs, reason, expected_revision})
 *     → PATCH /api/v1/versions/{version_id}/items/{item_id}/confirmation（P09）
 *   submit({version_id, revision, items: [{item_id, confirmation_id}]})
 *     → POST  /api/v1/tasks/{task_id}/submissions（P09）
 *   decide({action, reason, return_to?, items[] | issues: [{item_id, message}]})
 *     → POST  /api/v1/submissions/{submission_id}/decisions（P10）
 *
 * 请求体字段与 D3 第 2 节的输入列一致；路径参数由调用方在挂载 api adapter 时
 * 从上下文取得。后端 confirm/submit/decide 已由 P09/P10 实现，未接线或 501 时
 * 组件仍要如实显示「未实现（501）」，不伪造成功。
 *
 * 失败处理约定（D3 第 6 节、FIX-08）：
 * - 任何失败都不清空草稿，按钮从 pending 恢复；
 * - 409 → 提示「内容已被他人修改，已刷新」，并调用 onConflict / onRefresh；
 * - 501 → 「未实现（501）」，显示 details.owner，不显示业务结论；
 * - 401 / 403 分开提示；CSRF 失败单独说明。
 *
 * 草稿作用域（M-01）：确认草稿按「任务 + 版本」、专家决定草稿按「任务 + 版本 + 提交」
 * 隔离。同一份提交刷新时沿用草稿；切到另一份提交时不带上一份快照的终值/原因/问题，
 * 切回原提交时恢复它自己的草稿——不能只按 item_id 判断。
 * 实现是两套 Map：`state.confirmBuckets`（两段键）与 `state.buckets`（三段键）。
 *
 * 在飞输入（R-05）：选择「成功后只清理仍等于提交时快照的字段」，不禁用本次提交涉及的
 * 输入。理由：请求期间用户仍要核对/记录，禁用会让输入直接丢失；按字段比较既能清掉
 * 已提交内容，又保证请求返回前的新输入不被较早的响应抹掉。清理还要绑定提交时的作用域：
 * 请求在飞时切到另一份提交/版本，响应回来不得按旧快照清当前作用域的草稿，也不能把
 * 别人的成功反馈显示成当前视图的结果。
 *
 * 修订号（R-06）：任务 / 商品 / 提交修订号都按原始值校验必填与整数类型，null、空串、
 * 缺失和小数一律不发请求，不做 Number() 隐式转换。
 *
 * 最终状态只读服务器响应，HTTP 2xx 也只表述为「服务端已响应」，不写成审批通过。
 * ========================================================================== */

const WRITE_METHODS = Object.freeze(["confirm", "submit", "decide"]);

const ACTION_LABEL = Object.freeze({
  confirm: "确认 HS",
  submit: "整批提交",
  decide: "专家决定",
});

const ROLE_LABEL = Object.freeze({ operator: "业务员", expert: "专家" });

const TASK_STAGE_LABEL = Object.freeze({
  uploaded: "已上传",
  prechecking: "预审中",
  awaiting_confirmation: "待业务员确认",
  submitted: "已提交专家",
  approved: "已通过",
  returned: "已退回",
});

const TASK_STAGE_TONE = Object.freeze({
  uploaded: "",
  prechecking: "azure",
  awaiting_confirmation: "gold",
  submitted: "azure",
  approved: "jade",
  returned: "cinnabar",
});

const RETURN_TARGET_LABEL = Object.freeze({ supplier: "供应商", operator: "业务员" });

/* 写动作的唯一集中点：这里恰好三个箭头函数，每个调用一次传入的 api。 */
const WRITE_CALLS = Object.freeze({
  confirm: (api, payload) => api.confirm(payload),
  submit: (api, payload) => api.submit(payload),
  decide: (api, payload) => api.decide(payload),
});

let instanceSeq = 0;

/* ------------------------------- 小工具 ---------------------------------- */

const text = (value) => String(value ?? "").trim();

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

const shortId = (value) => (value ? String(value).slice(0, 8) : "");

function badge(label, tone = "") {
  return `<span class="badge ${esc(tone)}">${esc(label)}</span>`;
}

function fmtTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  try {
    return new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(date);
  } catch {
    return date.toISOString();
  }
}

function stageText(card, key) {
  const stage = card && card[key] ? card[key] : {};
  const value = text(stage.value);
  const source = stage.source_id ? String(stage.source_id) : "";
  if (value) return { text: value, empty: false, source, note: "" };
  if (source) {
    return {
      text: "未给出",
      empty: true,
      source,
      note: "该阶段已执行但没有值；与“尚未执行”不同，不用别的阶段补位。",
    };
  }
  return { text: "—", empty: true, source: "", note: "" };
}

function stageRow(label, stage) {
  return `<div class="ra-stage">
    <dt>${esc(label)}</dt>
    <dd class="mono ${stage.empty ? "is-empty" : ""}">${esc(stage.text)}</dd>
    <span class="ra-stage__src mono">${stage.source ? `↳ ${esc(shortId(stage.source))}` : ""}</span>
    ${stage.note ? `<span class="ra-stage__note">${esc(stage.note)}</span>` : ""}
  </div>`;
}

function errorLine(id, message) {
  return `<p class="ra-error" id="${esc(id)}" role="alert">${esc(message)}</p>`;
}

/**
 * 修订号校验（R-06）：只承认 >= 1 的整数原值或整数字符串。
 *
 * 先 Number() 再 Number.isInteger() 会把 null / "" / " " 变成 0 并照常发送；
 * 这里对原始值判断类型，缺字段、空串、小数、非数字字符串都返回 ok:false，
 * 调用方据此拦截请求。0 与负数同样是契约外取值（后端 `Field(ge=1)`），
 * 一并拦在发请求之前，不靠服务端 422 兜底。
 */
function revisionOf(raw) {
  const usable = (value) => Number.isSafeInteger(value) && value >= 1;
  if (typeof raw === "number") {
    return usable(raw) ? { ok: true, value: raw } : { ok: false, value: null };
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!/^\d+$/.test(trimmed)) return { ok: false, value: null };
    const parsed = Number(trimmed);
    return usable(parsed) ? { ok: true, value: parsed } : { ok: false, value: null };
  }
  return { ok: false, value: null };
}

/* 草稿作用域（M-01）：确认草稿按「任务 + 版本」两段键；专家决定草稿按「任务 + 版本 +
 * 提交」三段键。缺哪一项都当成独立的一档，不跨档复用。
 *
 * 为什么确认草稿不带 submission：确认发生在提交之前，页面刷新时随时可能补上 submission
 * 字段（接 P09 后很常见）。两套草稿若共用三段键，scope 从 `t::v::` 变成 `t::v::sub-9`
 * 就会把还没提交的确认值整档丢掉。
 *
 * 为什么决定草稿必须带提交：同一版本可能有多份提交快照，S1 的终值/原因/问题不能带进 S2。 */
function confirmScopeOf(data) {
  const value = data && typeof data === "object" ? data : {};
  const taskId = value.task ? text(value.task.task_id) : "";
  const versionId = text((value.version && value.version.version_id) || (value.task && value.task.current_version_id));
  return `${taskId}::${versionId}`;
}

function scopeOf(data) {
  const value = data && typeof data === "object" ? data : {};
  const submissionId = value.submission ? text(value.submission.submission_id) : "";
  return `${confirmScopeOf(value)}::${submissionId}`;
}

/* ----------------------------- 组件主体 ---------------------------------- */

/**
 * 创建一块处置表单。调用方负责请求上下文与刷新，组件只提交操作意图。
 *
 * @param {object} options
 * @param {object} options.api 必须实现 confirm / submit / decide 三个方法
 * @param {"operator"|"expert"} [options.role]
 * @param {object} [options.data] 视图数据（任务、版本、商品卡片、提交快照）
 * @param {Function} [options.onConflict] 409 后重新拉取的回调
 * @param {Function} [options.onRefresh] onConflict 的兜底别名
 * @param {Function} [options.onChanged] 写动作成功后通知调用方刷新
 * @returns {{mount: Function, update: Function, destroy: Function, getDraft: Function}}
 */
export function createReviewActions(options = {}) {
  const api = options.api;
  if (!api || WRITE_METHODS.some((name) => typeof api?.[name] !== "function")) {
    throw new Error("createReviewActions 需要 api 对象实现 confirm / submit / decide 三个方法");
  }

  const uid = `ra-${++instanceSeq}`;
  let root = null;

  const initialData = options.data && typeof options.data === "object" ? options.data : {};

  const state = {
    role: options.role === "expert" ? "expert" : "operator",
    data: initialData,
    /* 决定草稿作用域（三段）与确认草稿作用域（两段）分开记（P2）。 */
    scopeKey: scopeOf(initialData),
    confirmScopeKey: confirmScopeOf(initialData),
    /* scopeKey → 决定草稿 { mode, return_to, reason, rows: Map } */
    buckets: new Map(),
    /* confirmScopeKey → 确认草稿 Map<itemId, {hs, reason}> */
    confirmBuckets: new Map(),
    fieldErrors: {},
    pending: "",
    feedback: null,
  };

  /* ----------------------------- 数据访问 ------------------------------- */

  const task = () => state.data.task || {};
  const version = () => state.data.version || {};
  const items = () => (Array.isArray(state.data.items) ? state.data.items : []);
  const submission = () => state.data.submission || null;
  const itemById = (itemId) => items().find((item) => item.item_id === itemId) || null;

  function allowed(action) {
    const list = task().allowed_actions;
    if (!Array.isArray(list)) return null; // 未提供：只展示入口，权限由服务端判定
    return list.includes(action);
  }

  /* --------------------------- 草稿作用域（M-01） ------------------------- */

  const DRAFT_SCOPE_LIMIT = 12;

  function pruneScopeMap(map, keepKey) {
    // 只保留最近若干份作用域草稿，防止长时间会话无限增长；不同作用域之间永不互借。
    if (map.size <= DRAFT_SCOPE_LIMIT) return;
    for (const key of map.keys()) {
      if (key !== keepKey) {
        map.delete(key);
        break;
      }
    }
  }

  /** 决定草稿档：任务 + 版本 + 提交（三段键）。 */
  function bucket() {
    if (!state.buckets.has(state.scopeKey)) {
      state.buckets.set(state.scopeKey, { mode: "approve", return_to: "", reason: "", rows: new Map() });
      pruneScopeMap(state.buckets, state.scopeKey);
    }
    return state.buckets.get(state.scopeKey);
  }

  /** 确认草稿档：任务 + 版本（两段键），刷新带上 submission 也不丢（P2）。 */
  function confirmBucket() {
    if (!state.confirmBuckets.has(state.confirmScopeKey)) {
      state.confirmBuckets.set(state.confirmScopeKey, new Map());
      pruneScopeMap(state.confirmBuckets, state.confirmScopeKey);
    }
    return state.confirmBuckets.get(state.confirmScopeKey);
  }

  function confirmDraft(itemId) {
    const drafts = confirmBucket();
    if (!drafts.has(itemId)) drafts.set(itemId, { hs: "", reason: "" });
    return drafts.get(itemId);
  }

  function decisionRow(itemId) {
    const rows = bucket().rows;
    if (!rows.has(itemId)) rows.set(itemId, { final_hs: "", change_reason: "", issue: "" });
    return rows.get(itemId);
  }

  const decisionDraft = () => bucket();

  function submissionRow(itemId) {
    const current = submission();
    const rows = current && Array.isArray(current.items) ? current.items : [];
    return rows.find((row) => row.item_id === itemId) || null;
  }

  function pruneDrafts() {
    const itemIds = new Set(items().map((item) => item.item_id));
    const confirmCurrent = confirmBucket();
    for (const key of confirmCurrent.keys()) if (!itemIds.has(key)) confirmCurrent.delete(key);
    const submissionIds = new Set(
      (submission() && Array.isArray(submission().items) ? submission().items : []).map((row) => row.item_id),
    );
    const decisionCurrent = bucket();
    for (const key of decisionCurrent.rows.keys()) if (!submissionIds.has(key)) decisionCurrent.rows.delete(key);
  }

  /* ---------------------- 在飞请求的草稿快照（R-05） --------------------- */

  function confirmDraftSnapshot(itemId) {
    const draft = confirmDraft(itemId);
    return { hs: draft.hs, reason: draft.reason };
  }

  function clearConfirmDraftIfUnchanged(itemId, submittedDraft) {
    const draft = confirmDraft(itemId);
    if (text(draft.hs) === text(submittedDraft.hs)) draft.hs = "";
    if (text(draft.reason) === text(submittedDraft.reason)) draft.reason = "";
  }

  function decisionDraftSnapshot(snapRows) {
    const current = decisionDraft();
    const rows = new Map();
    for (const row of snapRows) {
      const draft = decisionRow(row.item_id);
      rows.set(row.item_id, { final_hs: draft.final_hs, change_reason: draft.change_reason, issue: draft.issue });
    }
    return { reason: current.reason, rows };
  }

  function clearDecisionDraftsIfUnchanged(submittedDraft, mode) {
    const current = decisionDraft();
    if (text(current.reason) === text(submittedDraft.reason)) current.reason = "";
    for (const [itemId, saved] of submittedDraft.rows) {
      const draft = decisionRow(itemId);
      if (mode === "approve") {
        if (text(draft.final_hs) === text(saved.final_hs)) draft.final_hs = "";
        if (text(draft.change_reason) === text(saved.change_reason)) draft.change_reason = "";
      } else if (text(draft.issue) === text(saved.issue)) {
        draft.issue = "";
      }
    }
  }

  /* ----------------------------- 反馈与错误 ----------------------------- */

  /**
   * 在飞响应的作用域绑定（R-05 补强，P2）：草稿归属于提交时的作用域。响应回来时如果
   * 用户已经切到另一份提交/版本，按旧快照去清新作用域里「值恰好相同」的字段，会清掉
   * 别的视图的新输入，并用别人的成功反馈冒充当前视图的结果。因此：
   * - 作用域未变 → 返回 null，调用方照原逻辑清理并提示；
   * - 作用域已变 → 不清理，只给一条「响应不属于当前作用域」的说明。
   * 确认草稿用两段键（任务+版本），决定草稿用三段键（任务+版本+提交）。
   */
  function offScopeFeedback(action, submittedScope, currentScope, response) {
    if (submittedScope === currentScope) return null;
    const info = appliedFeedback(action, response);
    const responseLines = info.lines.filter((line) => !line.startsWith("已清理的草稿仅限"));
    return {
      tone: "info",
      title: `${ACTION_LABEL[action]}已返回，但当前视图已切换到另一份作用域`,
      lines: [
        `这次响应属于作用域 ${submittedScope || "（空）"}，当前作用域是 ${currentScope || "（空）"}。`,
        "为避免误清新作用域的草稿，本次没有清理任何输入，也不把它算作当前视图的结果。",
        ...responseLines,
      ],
      requestId: info.requestId,
    };
  }

  function setFeedback(tone, title, lines, requestId = "") {
    state.feedback = { tone, title, lines: lines.filter(Boolean), requestId: requestId || "" };
  }

  function classify(status, code) {
    if (status === 401) return "unauthenticated";
    if (status === 403) return code === "csrf_failed" ? "csrf" : "forbidden";
    if (status === 404) return "missing";
    if (status === 409) return "conflict";
    if (status === 501) return "not_implemented";
    if (status === 400 || status === 422) return "invalid";
    return "failure";
  }

  function errorInfo(error) {
    const status = Number(error && error.status ? error.status : 0) || 0;
    const body = error && error.error ? error.error : {};
    const code = String((error && error.code) || body.code || "");
    return {
      status,
      code,
      kind: (error && error.kind) || classify(status, code),
      message: String((error && error.message) || body.message || "请求失败"),
      details: (error && error.details) || body.details || {},
      requestId: (error && (error.requestId || error.request_id)) || "",
    };
  }

  async function refreshAfterConflict(context) {
    const callback = options.onConflict || options.onRefresh;
    if (typeof callback !== "function") {
      return { ok: false, reason: "调用方没有提供 onConflict / onRefresh 回调" };
    }
    try {
      await callback(context);
      return { ok: true, reason: "" };
    } catch (error) {
      return { ok: false, reason: String((error && error.message) || "刷新回调异常") };
    }
  }

  async function failureFeedback(action, payload, error) {
    const info = errorInfo(error);
    const kept = "草稿已保留；按钮已恢复可用。";
    const lines = [];

    if (info.kind === "conflict") {
      const refreshed = await refreshAfterConflict({ action, payload, error });
      if (refreshed.ok) {
        lines.push(info.message);
        lines.push("已触发调用方重新拉取；请核对最新修订后再次提交。", kept);
        return { feedback: { tone: "error", title: "内容已被他人修改，已刷新", lines, requestId: info.requestId } };
      }
      lines.push(info.message);
      lines.push(`自动刷新未完成：${refreshed.reason}`, kept);
      return {
        feedback: {
          tone: "error",
          title: "内容已被他人修改，请刷新后重试",
          lines,
          requestId: info.requestId,
        },
      };
    }

    if (info.kind === "not_implemented") {
      lines.push(info.message);
      if (info.details && info.details.owner) lines.push(`后端标注的交付任务包：${info.details.owner}`);
      lines.push("该端点当前返回 501；本响应不代表任何业务动作已执行。", kept);
      return { feedback: { tone: "error", title: "未实现（501）", lines, requestId: info.requestId } };
    }

    if (info.kind === "forbidden") {
      lines.push(info.message);
      lines.push("服务端拒绝该动作（403）；allowed_actions 只用于展示，不能替代服务端校验。", kept);
      return { feedback: { tone: "error", title: "没有该动作的权限（403）", lines, requestId: info.requestId } };
    }

    if (info.kind === "csrf") {
      lines.push(info.message);
      lines.push("CSRF 令牌失效或缺失（403 csrf_failed）；刷新页面重新登录后再试。", kept);
      return { feedback: { tone: "error", title: "CSRF 校验失败（403）", lines, requestId: info.requestId } };
    }

    if (info.kind === "unauthenticated") {
      lines.push(info.message);
      lines.push("登录已失效或未登录（401）；重新登录后再试。", kept);
      return { feedback: { tone: "error", title: "登录已失效（401）", lines, requestId: info.requestId } };
    }

    if (info.kind === "missing") {
      lines.push(info.message);
      lines.push("对象不存在或不在可见范围（404）；不区分“无权限”和“不存在”。", kept);
      return { feedback: { tone: "error", title: "对象不存在或不可见（404）", lines, requestId: info.requestId } };
    }

    if (info.kind === "invalid") {
      lines.push(info.message);
      const fields = info.details && info.details.fields;
      if (Array.isArray(fields) && fields.length) lines.push(`字段问题：${fields.join("；")}`);
      lines.push("请求未通过服务端校验；请按提示修正后重试。", kept);
      return { feedback: { tone: "error", title: "请求未通过校验（400/422）", lines, requestId: info.requestId } };
    }

    lines.push(info.message);
    lines.push("未收到可确认的业务响应；请检查网络或稍后重试，不要重复点击。", kept);
    return { feedback: { tone: "error", title: "请求失败", lines, requestId: info.requestId } };
  }

  function responseInfo(response) {
    const body = response && typeof response === "object" ? response : {};
    const item = body.item && typeof body.item === "object" ? body.item : {};
    const sub = body.submission && typeof body.submission === "object" ? body.submission : {};
    const decision = body.decision && typeof body.decision === "object" ? body.decision : {};
    const status =
      body.status ||
      body.submission_status ||
      body.task_stage ||
      item.task_stage ||
      item.submission_status ||
      decision.action ||
      "";
    const revision = body.revision ?? item.revision ?? sub.revision ?? "";
    const submissionId = body.submission_id || sub.submission_id || "";
    return { status: String(status || ""), revision: String(revision ?? ""), submissionId: String(submissionId || "") };
  }

  function appliedFeedback(action, response) {
    const info = responseInfo(response);
    const lines = [];
    lines.push(info.status ? `服务端返回状态：${info.status}` : "服务端响应未包含可识别状态字段");
    lines.push(info.revision ? `服务器修订：${info.revision}` : "服务器未返回修订号");
    if (info.submissionId) lines.push(`submission_id：${info.submissionId}`);
    lines.push("本页只呈现服务端返回值，不把 HTTP 2xx 写成业务通过；请以重新拉取的数据为准。");
    lines.push("已清理的草稿仅限与本次提交时相同（之后未被继续修改）的字段；请求期间的新输入保留。");
    return { tone: "info", title: `${ACTION_LABEL[action]}：服务端已响应`, lines, requestId: response && response.request_id ? response.request_id : "" };
  }

  function notify(name, detail) {
    const handler = options[name];
    if (typeof handler !== "function") return;
    try {
      const result = handler(detail);
      if (result && typeof result.catch === "function") result.catch(() => {});
    } catch {
      // 调用方回调异常不能覆盖写动作的真实结果，组件只记录动作本身。
    }
  }

  /* ------------------------------- 渲染 --------------------------------- */

  function permissionNotice(action, label) {
    const grant = allowed(action);
    if (grant === false) {
      return `<p class="ra-notice ra-notice--warn" role="note">当前 allowed_actions 不含 <span class="mono">${esc(action)}</span>，${esc(label)}按钮已禁用。这只是展示提示，服务端仍会逐次校验权限。</p>`;
    }
    if (grant === null) {
      return `<p class="ra-notice" role="note">未提供 allowed_actions；本组件只展示入口，是否允许由服务端判定。</p>`;
    }
    return "";
  }

  function renderFeedback() {
    const feedback = state.feedback;
    if (!feedback) return "";
    const isError = feedback.tone === "error";
    return `<div class="ra-feedback ra-feedback--${esc(feedback.tone)}" role="${isError ? "alert" : "status"}" aria-live="${isError ? "assertive" : "polite"}" tabindex="-1">
      <b class="ra-feedback__title">${esc(feedback.title)}</b>
      <ul class="ra-feedback__lines">${feedback.lines.map((line) => `<li>${esc(line)}</li>`).join("")}</ul>
      ${feedback.requestId ? `<div class="ra-feedback__meta mono">request_id=${esc(feedback.requestId)}</div>` : ""}
    </div>`;
  }

  function renderContext() {
    const currentTask = task();
    const currentVersion = version();
    const currentSubmission = submission();
    const resultMode = currentTask.result_mode || (items().find((item) => item.result_mode) || {}).result_mode || "";
    const stage = currentTask.stage || "";
    return `<header class="ra-head">
      <div class="ra-head__id">
        <div class="ra-kicker">P11 处置表单 · ${esc(ROLE_LABEL[state.role])}</div>
        <h2 class="ra-title">${esc(currentTask.title || "未提供任务标题")}</h2>
        <div class="ra-meta mono">
          <span>task ${esc(shortId(currentTask.task_id))}</span>
          <span>V${esc(currentVersion.number ?? "—")} · ${esc(shortId(currentVersion.version_id || currentTask.current_version_id))}</span>
          <span>任务修订 ${esc(currentTask.revision ?? "—")}</span>
          ${currentSubmission ? `<span>提交 ${esc(shortId(currentSubmission.submission_id))} · rev ${esc(currentSubmission.revision ?? "—")}</span>` : ""}
        </div>
      </div>
      <div class="ra-badges">
        ${badge(TASK_STAGE_LABEL[stage] || stage || "阶段未知", TASK_STAGE_TONE[stage] || "")}
        ${resultMode === "mock" ? badge("MOCK", "gold") : ""}
        ${currentTask.current_submission_status ? badge(`提交 ${currentTask.current_submission_status}`, "teal") : ""}
      </div>
    </header>`;
  }

  function lastChangeText(card) {
    const entries = Array.isArray(card.confirmations) ? card.confirmations : [];
    const last = entries[0];
    if (last) {
      const before = text(last.previous_hs) || "（空）";
      const after = text(last.confirmed_hs) || "（空）";
      const reason = text(last.reason) || "未填原因";
      const when = last.created_at ? ` · ${fmtTime(last.created_at)}` : "";
      const who = last.created_by ? ` · ${last.created_by}` : "";
      return `上次修改：${before} → ${after} · ${reason}${when}${who}`;
    }
    const submitted = text(card.submitted_hs && card.submitted_hs.value);
    const final = text(card.expert_final_hs && card.expert_final_hs.value);
    if (submitted && final && submitted !== final) {
      return `专家改码对照（FIX-10）：${submitted} → ${final}；改码原因不在 ItemCard 中，见专家决定历史 change_reason。`;
    }
    return "尚无修改记录。";
  }

  function renderConfirmRow(card) {
    const itemId = card.item_id;
    const draft = confirmDraft(itemId);
    const inputId = `${uid}-confirm-hs-${itemId}`;
    const reasonId = `${uid}-confirm-reason-${itemId}`;
    const hsError = state.fieldErrors[`${itemId}:hs`];
    const reasonError = state.fieldErrors[`${itemId}:reason`];
    const current = stageText(card, "business_confirmed_hs");
    const suggestion = stageText(card, "precheck_suggested_hs");
    const pending = state.pending === `confirm:${itemId}`;
    const disabled = Boolean(state.pending) || allowed("confirmation.write") === false;

    return `<li class="ra-item" data-item-id="${esc(itemId)}">
      <div class="ra-item__head">
        <span class="ra-rowno mono">第 ${esc(card.source_location ? card.source_location.row_number : "—")} 行</span>
        <span class="ra-name">${esc(card.facts ? card.facts.name : card.name || "（未命名商品）")}</span>
        <span class="ra-loc mono">${esc(card.source_location ? card.source_location.sheet_name : "")} ${esc(card.source_location ? card.source_location.cells : "")}</span>
        ${badge(`rev ${card.revision ?? "—"}`, "teal")}
      </div>

      <dl class="ra-stages">
        ${stageRow("供应商原码", { text: text(card.facts && card.facts.supplier_hs_raw) || "（空白，保留原样）", empty: !text(card.facts && card.facts.supplier_hs_raw), source: "", note: "原文只读，任何阶段都不回写" })}
        ${stageRow("预审建议", suggestion)}
        ${stageRow("当前确认", current)}
        ${stageRow("已提交", stageText(card, "submitted_hs"))}
        ${stageRow("专家终值", stageText(card, "expert_final_hs"))}
      </dl>

      <p class="ra-lastchange">${esc(lastChangeText(card))}</p>

      <div class="ra-formgrid">
        <div class="ra-field">
          <label for="${esc(inputId)}">确认 HS <span class="ra-req">必填</span></label>
          <div class="ra-inputrow">
            <input id="${esc(inputId)}" class="mono" type="text" inputmode="numeric" autocomplete="off"
              data-ra-field="confirm-hs" data-item="${esc(itemId)}" value="${esc(draft.hs)}"
              placeholder="输入待审 HS" aria-invalid="${hsError ? "true" : "false"}"
              ${hsError ? `aria-describedby="${esc(`${inputId}-error`)}"` : `aria-describedby="${esc(`${inputId}-hint`)}"`}>
            ${suggestion.text !== "—" && suggestion.text !== "未给出"
              ? `<button type="button" class="btn ra-btn" data-ra-act="use-suggestion" data-item="${esc(itemId)}" data-value="${esc(suggestion.text)}">显式采用预审建议</button>`
              : ""}
          </div>
          ${hsError ? errorLine(`${inputId}-error`, hsError) : `<p class="ra-hint" id="${esc(`${inputId}-hint`)}">不会自动带入预审建议；点按钮才填入，输入框内容始终由操作者决定。</p>`}
        </div>

        <div class="ra-field">
          <label for="${esc(reasonId)}">修改原因 <span class="ra-note">${current.text !== "—" && current.text !== "未给出" ? "修改已有确认值时必填" : "首次确认可留空"}</span></label>
          <input id="${esc(reasonId)}" type="text" autocomplete="off"
            data-ra-field="confirm-reason" data-item="${esc(itemId)}" value="${esc(draft.reason)}"
            placeholder="例如：按申报要素重新归类" aria-invalid="${reasonError ? "true" : "false"}"
            ${reasonError ? `aria-describedby="${esc(`${reasonId}-error`)}"` : ""}>
          ${reasonError ? errorLine(`${reasonId}-error`, reasonError) : ""}
        </div>

        <div class="ra-field ra-field--action">
          <button type="button" class="btn btn-primary ra-btn" data-ra-act="confirm" data-item="${esc(itemId)}"
            ${disabled ? "disabled" : ""} aria-busy="${pending}">
            ${pending ? "处理中…" : `确认第 ${esc(card.source_location ? card.source_location.row_number : "—")} 行`}
          </button>
          <p class="ra-hint mono">expected_revision=${esc(card.revision ?? "—")}</p>
        </div>
      </div>
    </li>`;
  }

  function renderOperator() {
    const rows = items();
    return `${renderContext()}
      ${renderFeedback()}
      <section class="ra-panel" aria-labelledby="${uid}-confirm-title">
        <div class="ra-panel__head">
          <h3 id="${uid}-confirm-title">业务员确认 HS</h3>
          <span class="ra-hint">逐行确认；修改已有确认值必须填写原因</span>
        </div>
        ${permissionNotice("confirmation.write", "确认")}
        ${rows.length
          ? `<ul class="ra-items">${rows.map(renderConfirmRow).join("")}</ul>`
          : `<div class="ra-empty" role="status"><b>当前版本没有商品行</b><p>没有可确认的对象；不要用其他版本或预审建议补位。</p></div>`}
      </section>
      ${renderSubmitSection()}`;
  }

  /**
   * 整批提交快照：预览与真实提交共用一份判断，避免「按钮能点、点了被拦」的两套规则。
   * 修订号在这里做原始值校验（R-06），任务与逐行商品修订号都不允许隐式转换。
   */
  function submitSnapshot() {
    const rows = items().map((card) => {
      const confirmed = card.business_confirmed_hs || {};
      return {
        card,
        itemId: card.item_id,
        submittedHs: text(confirmed.value),
        confirmationId: text(confirmed.source_id),
        revision: revisionOf(card.revision),
      };
    });
    return {
      rows,
      missing: rows.filter((row) => !row.submittedHs || !row.confirmationId),
      invalidRevisions: rows.filter((row) => !row.revision.ok),
      taskRevision: revisionOf(task().revision),
      versionId: text(version().version_id || task().current_version_id),
    };
  }

  function renderSubmitSection() {
    const { rows, missing, invalidRevisions, taskRevision, versionId } = submitSnapshot();
    const blocked =
      !rows.length || missing.length > 0 || invalidRevisions.length > 0 || !taskRevision.ok || !versionId ||
      allowed("submission.create") === false;
    const pending = state.pending === "submit";

    const body = rows.map((row) => {
      const card = row.card;
      const isMissing = !row.submittedHs || !row.confirmationId;
      return `<tr class="${isMissing ? "ra-row--blocked" : ""}">
        <td class="mono">${esc(card.source_location ? card.source_location.row_number : "—")}</td>
        <td>${esc(card.facts ? card.facts.name : card.name || "（未命名商品）")}</td>
        <td class="mono">${esc(text(card.facts && card.facts.supplier_hs_raw) || "（空白）")}</td>
        <td class="mono ${row.submittedHs ? "" : "is-empty"}">${esc(row.submittedHs || "—")}</td>
        <td>${isMissing
          ? badge("缺少确认", "cinnabar")
          : `<span class="mono">确认 ${esc(shortId(row.confirmationId))}</span>`}</td>
        <td class="mono">${row.revision.ok ? esc(row.revision.value) : badge("修订号无效", "cinnabar")}</td>
      </tr>`;
    }).join("");

    return `<section class="ra-panel" aria-labelledby="${uid}-submit-title">
      <div class="ra-panel__head">
        <h3 id="${uid}-submit-title">整批提交给专家</h3>
        <span class="ra-hint">整批快照；不做部分提交</span>
      </div>
      ${permissionNotice("submission.create", "整批提交")}
      <p class="ra-summary" id="${uid}-submit-summary" role="status" aria-live="polite">
        将提交 <b>${rows.length - missing.length}</b> 行；缺少确认 <b>${missing.length}</b> 行。
        提交携带任务修订 <span class="mono">${esc(taskRevision.ok ? taskRevision.value : "—")}</span>（字段名 revision）。
      </p>
      <div class="ra-table-wrap">
        <table class="ra-table">
          <caption class="sr-only">整批提交快照预览：逐行显示将提交的 HS、确认记录引用和商品修订</caption>
          <thead><tr>
            <th scope="col">行</th><th scope="col">商品</th><th scope="col">供应商原码</th>
            <th scope="col">将提交 HS</th><th scope="col">确认来源</th><th scope="col">商品修订</th>
          </tr></thead>
          <tbody>${body || `<tr><td colspan="6">没有商品行</td></tr>`}</tbody>
        </table>
      </div>
      ${missing.length
        ? `<div class="ra-blocker" role="alert">
            <b>不能整批提交：${missing.length} 行缺少业务确认</b>
            <ul>${missing.map((row) => `<li>第 ${esc(row.card.source_location ? row.card.source_location.row_number : "—")} 行 · ${esc(row.card.facts ? row.card.facts.name : row.card.name || "未命名商品")}</li>`).join("")}</ul>
            <p>组件不会用预审建议、供应商原码、专家建议、旧提交或空值补位；请先逐行确认。</p>
          </div>`
        : `<p class="ra-hint">快照只取 <span class="mono">business_confirmed_hs</span> 的值与确认记录引用；预审建议、供应商原码和专家建议都不会进入提交。</p>`}
      ${invalidRevisions.length
        ? `<div class="ra-blocker" role="alert">
            <b>不能整批提交：${invalidRevisions.length} 行商品修订号不是整数</b>
            <ul>${invalidRevisions.map((row) => `<li>第 ${esc(row.card.source_location ? row.card.source_location.row_number : "—")} 行</li>`).join("")}</ul>
            <p>修订号缺失、空串或小数都不发送请求；请重新拉取商品后再提交。</p>
          </div>`
        : ""}
      ${!taskRevision.ok ? `<p class="ra-error" role="alert">缺少可用的任务修订号（revision 必须是整数），不能安全生成 revision。</p>` : ""}
      ${!versionId ? `<p class="ra-error" role="alert">缺少版本标识（version_id），不能整批提交。</p>` : ""}
      <div class="ra-actions">
        <button type="button" class="btn btn-primary ra-btn" data-ra-act="submit"
          ${blocked ? "disabled" : ""} aria-busy="${pending}" aria-describedby="${uid}-submit-summary">
          ${pending ? "提交中…" : "整批提交"}
        </button>
        <span class="ra-hint">服务端返回的提交状态才是最终状态；本页不推断审批结果。</span>
      </div>
    </section>`;
  }

  function renderExpert() {
    const currentSubmission = submission();
    if (!currentSubmission) {
      return `${renderContext()}
        ${renderFeedback()}
        <section class="ra-panel" aria-labelledby="${uid}-decision-title">
          <div class="ra-panel__head"><h3 id="${uid}-decision-title">专家通过 / 退回</h3></div>
          <div class="ra-empty" role="status">
            <b>当前没有待审提交快照</b>
            <p>通过和退回都是针对一份不可变提交快照的决定；没有提交时按钮不可用，也不从商品卡片的别阶段值拼一份“提交”。</p>
          </div>
        </section>`;
    }

    const mode = decisionDraft().mode;
    const snapRows = Array.isArray(currentSubmission.items) ? currentSubmission.items : [];
    const pending = state.pending === "decide";
    const disabled = Boolean(state.pending) || allowed("decision.resolve") === false || !snapRows.length;
    const expertCheck = state.data.expert_check || null;

    return `${renderContext()}
      ${renderFeedback()}
      <section class="ra-panel" aria-labelledby="${uid}-decision-title">
        <div class="ra-panel__head">
          <h3 id="${uid}-decision-title">专家通过 / 退回</h3>
          <span class="ra-hint">整批决定；退回需对象、原因和逐商品问题</span>
        </div>
        ${permissionNotice("decision.resolve", "专家决定")}
        ${expertCheck && expertCheck.target_hs
          ? `<p class="ra-notice ra-notice--gold">本次专家 AI 核查目标：<span class="mono">${esc(expertCheck.target_hs)}</span>${expertCheck.status ? ` · 状态 ${esc(expertCheck.status)}` : ""}。若改码，该核查结论不外推到新终值（FIX-10）。</p>`
          : ""}

        <fieldset class="ra-choice">
          <legend>处置方式 <span class="ra-req">必选</span></legend>
          <label class="ra-choice__option">
            <input id="${uid}-mode-approve" type="radio" name="${uid}-mode" value="approve" data-ra-field="decision-mode" ${mode === "approve" ? "checked" : ""}>
            <span><b>通过</b><span class="ra-hint">逐行终值；未改的行等于提交值</span></span>
          </label>
          <label class="ra-choice__option">
            <input id="${uid}-mode-return" type="radio" name="${uid}-mode" value="return" data-ra-field="decision-mode" ${mode === "return" ? "checked" : ""}>
            <span><b>退回</b><span class="ra-hint">退回对象二选一 + 逐商品问题</span></span>
          </label>
        </fieldset>

        ${mode === "approve" ? renderApprove(snapRows) : renderReturn(snapRows)}

        <div class="ra-actions">
          <button type="button" class="btn btn-primary ra-btn" data-ra-act="decide" ${disabled ? "disabled" : ""} aria-busy="${pending}">
            ${pending ? "处理中…" : mode === "approve" ? "提交通过决定" : "提交退回决定"}
          </button>
          <span class="ra-hint mono">revision=${esc(currentSubmission.revision ?? "—")}（提交修订，仅展示）</span>
        </div>
      </section>`;
  }

  function renderApprove(snapRows) {
    const rows = snapRows.map((row) => {
      const card = itemById(row.item_id);
      const draft = decisionRow(row.item_id);
      const submitted = text(row.submitted_hs);
      const typed = text(draft.final_hs);
      const effective = typed || submitted;
      const changed = Boolean(typed) && typed !== submitted;
      const finalError = state.fieldErrors[`${row.item_id}:final`];
      const reasonError = state.fieldErrors[`${row.item_id}:change`];
      const inputId = `${uid}-final-${row.item_id}`;
      const reasonId = `${uid}-change-${row.item_id}`;
      return `<li class="ra-item" data-item-id="${esc(row.item_id)}">
        <div class="ra-item__head">
          <span class="ra-rowno mono">第 ${esc(row.source_location ? row.source_location.row_number : card && card.source_location ? card.source_location.row_number : "—")} 行</span>
          <span class="ra-name">${esc(card && card.facts ? card.facts.name : row.name || "（未命名商品）")}</span>
          <span id="${esc(`${uid}-changed-${row.item_id}`)}" class="badge ${changed ? "gold" : ""}">${changed ? "改码" : "未改"}</span>
        </div>
        <dl class="ra-stages">
          ${stageRow("提交值（只读）", { text: submitted || "—", empty: !submitted, source: "", note: "专家核查与终值的基准，不能在本页修改" })}
        </dl>
        <div class="ra-formgrid">
          <div class="ra-field">
            <label for="${esc(inputId)}">专家终值 <span class="ra-note">留空即等于提交值</span></label>
            <input id="${esc(inputId)}" class="mono" type="text" inputmode="numeric" autocomplete="off"
              data-ra-field="final-hs" data-item="${esc(row.item_id)}" value="${esc(draft.final_hs)}"
              placeholder="${esc(submitted)}" aria-invalid="${finalError ? "true" : "false"}"
              ${finalError ? `aria-describedby="${esc(`${inputId}-error`)}"` : ""}>
            <p class="ra-effective" id="${esc(`${uid}-effective-${row.item_id}`)}">终值：${esc(effective || "—")}${changed ? "（改码）" : "（等于提交值）"}</p>
            ${finalError ? errorLine(`${inputId}-error`, finalError) : ""}
          </div>
          <div class="ra-field">
            <label for="${esc(reasonId)}">改码原因 <span class="ra-note">改码必填；未改可留空</span></label>
            <input id="${esc(reasonId)}" type="text" autocomplete="off"
              data-ra-field="change-reason" data-item="${esc(row.item_id)}" value="${esc(draft.change_reason)}"
              placeholder="说明为何不采用提交值" aria-invalid="${reasonError ? "true" : "false"}"
              ${reasonError ? `aria-describedby="${esc(`${reasonId}-error`)}"` : ""}>
            ${reasonError ? errorLine(`${reasonId}-error`, reasonError) : ""}
          </div>
        </div>
      </li>`;
    });

    return `<div class="ra-subhead">
        <h4>逐行终值</h4>
        <span class="ra-hint">未改的行终值等于提交值；前后值与改码原因由服务端留痕，组件不用 AI 建议补位。</span>
      </div>
      <ul class="ra-items">${rows.join("")}</ul>
      <div class="ra-field">
        <label for="${uid}-decision-reason">整批说明 <span class="ra-note">可选</span></label>
        <textarea id="${uid}-decision-reason" rows="2" data-ra-field="decision-reason"
          placeholder="例如：已核对申报要素与附图">${esc(decisionDraft().reason)}</textarea>
      </div>`;
  }

  function renderReturn(snapRows) {
    const targetError = state.fieldErrors["decision:target"];
    const reasonError = state.fieldErrors["decision:reason"];
    const issueErrors = snapRows.some((row) => state.fieldErrors[`${row.item_id}:issue`]);

    return `<div class="ra-subhead">
        <h4>退回对象与逐商品问题</h4>
        <span class="ra-hint">退回必须选择对象、填写原因，并至少写一条逐商品问题。</span>
      </div>
      <fieldset class="ra-choice"${targetError ? ` aria-describedby="${uid}-target-error"` : ""}>
        <legend>退回对象 <span class="ra-req">二选一</span></legend>
        <label class="ra-choice__option">
          <input id="${uid}-return-supplier" type="radio" name="${uid}-return-to" value="supplier" data-ra-field="return-to" ${decisionDraft().return_to === "supplier" ? "checked" : ""}>
          <span><b>供应商</b><span class="ra-hint">补料重传，整表重新预审</span></span>
        </label>
        <label class="ra-choice__option">
          <input id="${uid}-return-operator" type="radio" name="${uid}-return-to" value="operator" data-ra-field="return-to" ${decisionDraft().return_to === "operator" ? "checked" : ""}>
          <span><b>业务员</b><span class="ra-hint">由业务员处理确认或补料</span></span>
        </label>
      </fieldset>
      ${targetError ? errorLine(`${uid}-target-error`, targetError) : ""}

      <ul class="ra-items">
        ${snapRows.map((row) => {
          const card = itemById(row.item_id);
          const draft = decisionRow(row.item_id);
          const issueError = state.fieldErrors[`${row.item_id}:issue`];
          const issueId = `${uid}-issue-${row.item_id}`;
          return `<li class="ra-item" data-item-id="${esc(row.item_id)}">
            <div class="ra-item__head">
              <span class="ra-rowno mono">第 ${esc(row.source_location ? row.source_location.row_number : card && card.source_location ? card.source_location.row_number : "—")} 行</span>
              <span class="ra-name">${esc(card && card.facts ? card.facts.name : row.name || "（未命名商品）")}</span>
              <span class="mono ra-stage__src">提交值 ${esc(text(row.submitted_hs) || "—")}</span>
            </div>
            <div class="ra-field">
              <label for="${esc(issueId)}">该商品问题 <span class="ra-note">至少一条；不填的行不进入决定明细</span></label>
              <textarea id="${esc(issueId)}" rows="2" data-ra-field="issue" data-item="${esc(row.item_id)}"
                placeholder="例如：材质与规格型号不一致，需补充说明" aria-invalid="${issueError ? "true" : "false"}">${esc(draft.issue)}</textarea>
              ${issueError ? errorLine(`${issueId}-error`, issueError) : ""}
            </div>
          </li>`;
        }).join("")}
      </ul>
      ${issueErrors ? `<p class="ra-error" role="alert">至少填写一条逐商品问题；空问题不能作为退回依据。</p>` : ""}

      <div class="ra-field">
        <label for="${uid}-return-reason">退回原因 <span class="ra-req">必填</span></label>
        <textarea id="${uid}-return-reason" rows="3" data-ra-field="decision-reason"
          placeholder="说明整批退回的原因；不要只写“资料不全”" aria-invalid="${reasonError ? "true" : "false"}">${esc(decisionDraft().reason)}</textarea>
        ${reasonError ? errorLine(`${uid}-return-reason-error`, reasonError) : ""}
      </div>`;
  }

  function render() {
    if (!root) return;
    const focus = captureFocus();
    root.innerHTML = `<div class="ra-stack">${state.role === "operator" ? renderOperator() : renderExpert()}</div>`;
    restoreFocus(focus);
  }

  function captureFocus() {
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (!active || !root || !root.contains(active)) return null;
    return {
      id: active.id || "",
      act: (active.dataset && active.dataset.raAct) || "",
      field: (active.dataset && active.dataset.raField) || "",
    };
  }

  function restoreFocus(snapshot) {
    if (!snapshot) return;
    let node = snapshot.id ? document.getElementById(snapshot.id) : null;
    if (!node && snapshot.act) node = root.querySelector(`[data-ra-act="${snapshot.act}"]`);
    if (!node && snapshot.field) node = root.querySelector(`[data-ra-field="${snapshot.field}"]`);
    if (node && typeof node.focus === "function") {
      try {
        node.focus({ preventScroll: true });
      } catch {
        node.focus();
      }
    }
  }

  function focusFirstError() {
    if (!root) return;
    const node = root.querySelector('[aria-invalid="true"]') || root.querySelector(".ra-feedback");
    if (node && typeof node.focus === "function") {
      try {
        node.focus({ preventScroll: true });
      } catch {
        node.focus();
      }
    }
  }

  /* ------------------------------ 交互动作 ------------------------------ */

  function updateEffectivePreview(itemId) {
    if (!root) return;
    const row = submissionRow(itemId);
    if (!row) return;
    const draft = decisionRow(itemId);
    const submitted = text(row.submitted_hs);
    const typed = text(draft.final_hs);
    const effective = typed || submitted;
    const changed = Boolean(typed) && typed !== submitted;
    const effectiveNode = document.getElementById(`${uid}-effective-${itemId}`);
    if (effectiveNode) {
      effectiveNode.textContent = `终值：${effective || "—"}${changed ? "（改码）" : "（等于提交值）"}`;
    }
    const changedNode = document.getElementById(`${uid}-changed-${itemId}`);
    if (changedNode) {
      changedNode.textContent = changed ? "改码" : "未改";
      changedNode.className = `badge ${changed ? "gold" : ""}`;
    }
  }

  async function confirmItem(itemId) {
    if (state.pending) return;
    const card = itemById(itemId);
    if (!card) return;
    const draft = confirmDraft(itemId);
    const value = text(draft.hs);
    const reason = text(draft.reason);
    const current = text(card.business_confirmed_hs && card.business_confirmed_hs.value);
    const revision = revisionOf(card.revision);
    const errors = {};

    if (!value) errors[`${itemId}:hs`] = "请填写确认 HS；组件不会自动带入预审建议。";
    else if (value === current) errors[`${itemId}:hs`] = "与当前确认值相同，无需重复确认。";
    if (current && value && value !== current && !reason) {
      errors[`${itemId}:reason`] = "修改已有确认值必须填写修改原因（D2 Confirmation.reason）。";
    }
    if (!revision.ok) errors[`${itemId}:hs`] = "缺少商品修订号（revision 不是整数），不能安全提交 expected_revision。";
    if (allowed("confirmation.write") === false) {
      errors[`${itemId}:hs`] = "当前 allowed_actions 不含 confirmation.write；按钮禁用只是提示，服务端仍会校验。";
    }

    if (Object.keys(errors).length) {
      state.fieldErrors = errors;
      state.feedback = null;
      render();
      focusFirstError();
      return;
    }

    state.fieldErrors = {};
    state.pending = `confirm:${itemId}`;
    render();

    const submittedDraft = confirmDraftSnapshot(itemId);
    // 快照提交时的作用域：响应回来时若用户已切到别的任务/版本，不许按旧快照清理（R-05）。
    const submittedScope = state.confirmScopeKey;
    const payload = { confirmed_hs: value, reason, expected_revision: revision.value };
    try {
      const response = await WRITE_CALLS.confirm(api, payload);
      const offScope = offScopeFeedback("confirm", submittedScope, state.confirmScopeKey, response);
      if (offScope) {
        state.feedback = offScope;
      } else {
        state.feedback = appliedFeedback("confirm", response);
        // R-05：只清理仍等于提交时快照的字段；请求期间继续输入的内容保留。
        clearConfirmDraftIfUnchanged(itemId, submittedDraft);
      }
      notify("onChanged", { action: "confirm", payload, response });
    } catch (error) {
      const result = await failureFeedback("confirm", payload, error);
      state.feedback = result.feedback;
      // 失败路径不清草稿：保留用户输入与原因，便于按 409/501 提示修正后重试。
    } finally {
      state.pending = "";
      render();
      if (state.feedback && state.feedback.tone === "error") focusFirstError();
    }
  }

  async function submitBatch() {
    if (state.pending) return;
    const { rows, missing, invalidRevisions, taskRevision, versionId } = submitSnapshot();
    const problems = [];

    if (!rows.length) problems.push("当前版本没有商品行，不能整批提交。");
    if (!taskRevision.ok) problems.push("缺少可用的任务修订号（revision 必须是整数），不能安全提交 revision。");
    if (invalidRevisions.length) {
      const rowsText = invalidRevisions
        .map((row) => `第 ${row.card.source_location ? row.card.source_location.row_number : "—"} 行`)
        .join("；");
      problems.push(`${invalidRevisions.length} 行商品修订号不是整数（${rowsText}）；不能生成整批提交快照。`);
    }
    if (!versionId) problems.push("缺少版本标识，不能整批提交。");
    if (allowed("submission.create") === false) {
      problems.push("当前 allowed_actions 不含 submission.create；按钮禁用只是提示，服务端仍会校验。");
    }
    if (missing.length) {
      const names = missing
        .map((row) => `第 ${row.card.source_location ? row.card.source_location.row_number : "—"} 行 ${row.card.facts ? row.card.facts.name : row.card.name || "未命名商品"}`)
        .join("；");
      problems.push(`${missing.length} 行缺少业务确认或确认记录引用：${names}。`);
      problems.push("组件不会用预审建议、供应商原码、专家建议、旧提交或空值补位。");
    }

    if (problems.length) {
      state.fieldErrors = {};
      setFeedback("error", "整批提交已被拦截", problems);
      render();
      focusFirstError();
      return;
    }

    state.fieldErrors = {};
    state.pending = "submit";
    render();

    const revision = taskRevision.value;
    const payload = {
      version_id: versionId,
      revision,
      items: rows.map((row) => ({
        item_id: row.itemId,
        confirmation_id: row.confirmationId,
      })),
    };
    try {
      const response = await WRITE_CALLS.submit(api, payload);
      state.feedback = appliedFeedback("submit", response);
      notify("onChanged", { action: "submit", payload, response });
    } catch (error) {
      const result = await failureFeedback("submit", payload, error);
      state.feedback = result.feedback;
    } finally {
      state.pending = "";
      render();
      if (state.feedback && state.feedback.tone === "error") focusFirstError();
    }
  }

  async function submitDecision() {
    if (state.pending) return;
    const snapshot = submission();
    const snapRows = snapshot && Array.isArray(snapshot.items) ? snapshot.items : [];
    const mode = decisionDraft().mode;
    const submissionRevision = revisionOf(snapshot && snapshot.revision);
    const problems = [];
    const fieldErrors = {};

    if (!snapshot || !snapshot.submission_id) problems.push("当前没有待审提交快照，不能提交专家决定。");
    if (!snapRows.length) problems.push("提交快照没有逐行明细，不能整批决定。");
    if (!submissionRevision.ok) {
      problems.push("提交快照的修订号（revision 不是整数）不可用，不能安全生成决定；请重新拉取提交快照。");
    }
    if (allowed("decision.resolve") === false) {
      problems.push("当前 allowed_actions 不含 decision.resolve；按钮禁用只是提示，服务端仍会校验。");
    }

    let payload = null;

    if (mode === "approve") {
      const rows = snapRows.map((row) => {
        const draft = decisionRow(row.item_id);
        const submitted = text(row.submitted_hs);
        const typed = text(draft.final_hs);
        const final = typed || submitted;
        const changed = final !== submitted;
        return { row, draft, submitted, final, changed, reason: text(draft.change_reason) };
      });

      for (const entry of rows) {
        const label = entry.row.source_location ? `第 ${entry.row.source_location.row_number} 行` : entry.row.item_id;
        if (!entry.submitted) fieldErrors[`${entry.row.item_id}:final`] = `${label} 缺少提交值，不能生成终值；不从别的阶段补位。`;
        else if (!entry.final) fieldErrors[`${entry.row.item_id}:final`] = `${label} 终值不能为空。`;
        if (entry.changed && !entry.reason) {
          fieldErrors[`${entry.row.item_id}:change`] = `${label} 改码必须填写原因（R08 DecisionItem.change_reason）。`;
        }
      }

      payload = {
        action: "approve",
        reason: text(decisionDraft().reason),
        items: rows.map((entry) => ({
          item_id: entry.row.item_id,
          final_hs: entry.final,
          change_reason: entry.changed ? entry.reason : "",
        })),
      };
    } else {
      const target = decisionDraft().return_to;
      const reason = text(decisionDraft().reason);
      const issueRows = snapRows
        .map((row) => ({ row, issue: text(decisionRow(row.item_id).issue) }))
        .filter((entry) => entry.issue);

      if (!RETURN_TARGET_LABEL[target]) fieldErrors["decision:target"] = "退回必须选择退回对象：供应商或业务员。";
      if (!reason) fieldErrors["decision:reason"] = "退回必须填写原因。";
      if (!issueRows.length) problems.push("退回必须至少填写一条逐商品问题；空问题不能作为退回依据。");

      payload = {
        action: "return",
        return_to: target,
        reason,
        issues: issueRows.map((entry) => ({ item_id: entry.row.item_id, message: entry.issue })),
      };
    }

    const errorKeys = Object.keys(fieldErrors);
    if (errorKeys.length || problems.length) {
      state.fieldErrors = fieldErrors;
      if (problems.length) setFeedback("error", "专家决定已被拦截", problems);
      else state.feedback = null;
      render();
      focusFirstError();
      return;
    }

    // 提交时的草稿快照：成功后只清这些值，请求期间的新输入保留（R-05）。
    const submittedDraft = decisionDraftSnapshot(snapRows);
    // 作用域快照：响应回来时若已切到另一份提交，不能按 S1 的快照清 S2 的草稿（R-05/P2）。
    const submittedScope = state.scopeKey;
    state.fieldErrors = {};
    state.pending = "decide";
    render();

    try {
      const response = await WRITE_CALLS.decide(api, payload);
      const offScope = offScopeFeedback("decide", submittedScope, state.scopeKey, response);
      if (offScope) {
        state.feedback = offScope;
      } else {
        state.feedback = appliedFeedback("decide", response);
        clearDecisionDraftsIfUnchanged(submittedDraft, mode);
      }
      notify("onChanged", { action: "decide", payload, response });
    } catch (error) {
      const result = await failureFeedback("decide", payload, error);
      state.feedback = result.feedback;
    } finally {
      state.pending = "";
      render();
      if (state.feedback && state.feedback.tone === "error") focusFirstError();
    }
  }

  /* ------------------------------ 事件绑定 ------------------------------ */

  function onFieldEvent(event) {
    const field = event.target.closest("[data-ra-field]");
    if (!field || !root || !root.contains(field)) return;
    const itemId = field.dataset.item || "";
    const name = field.dataset.raField;

    if (name === "confirm-hs") confirmDraft(itemId).hs = field.value;
    else if (name === "confirm-reason") confirmDraft(itemId).reason = field.value;
    else if (name === "decision-mode") {
      decisionDraft().mode = field.value === "return" ? "return" : "approve";
      render();
      return;
    } else if (name === "return-to") decisionDraft().return_to = field.value;
    else if (name === "decision-reason") decisionDraft().reason = field.value;
    else if (name === "final-hs") {
      decisionRow(itemId).final_hs = field.value;
      updateEffectivePreview(itemId);
    } else if (name === "change-reason") decisionRow(itemId).change_reason = field.value;
    else if (name === "issue") decisionRow(itemId).issue = field.value;
  }

  function onClick(event) {
    const button = event.target.closest("[data-ra-act]");
    if (!button || !root || !root.contains(button)) return;
    const action = button.dataset.raAct;
    const itemId = button.dataset.item || "";

    if (action === "use-suggestion") {
      const draft = confirmDraft(itemId);
      draft.hs = button.dataset.value || "";
      const input = document.getElementById(`${uid}-confirm-hs-${itemId}`);
      if (input) {
        input.value = draft.hs;
        input.focus();
      }
      return;
    }
    if (action === "confirm") {
      void confirmItem(itemId);
      return;
    }
    if (action === "submit") {
      void submitBatch();
      return;
    }
    if (action === "decide") {
      void submitDecision();
    }
  }

  /* ------------------------------- 生命周期 ----------------------------- */

  function mount(container) {
    if (!container || typeof container.replaceChildren !== "function") {
      throw new Error("createReviewActions().mount 需要一个 DOM 容器");
    }
    if (root) destroy();
    root = document.createElement("div");
    root.className = "ra-root";
    root.dataset.raRole = state.role;
    container.replaceChildren(root);
    root.addEventListener("click", onClick);
    root.addEventListener("input", onFieldEvent);
    root.addEventListener("change", onFieldEvent);
    render();
    return controller;
  }

  function update(data) {
    const next = data && typeof data === "object" ? data : {};
    state.fieldErrors = {};
    // M-01：作用域变了就换到该提交自己的草稿档；同一份提交刷新（scope 不变）沿用草稿。
    // 切回旧作用域时恢复它自己的草稿，而不是把最近一次编辑结果带过去。
    // 确认草稿只按「任务+版本」换档：刷新时带上 submission 不会丢掉未提交的确认值（P2）。
    state.scopeKey = scopeOf(next);
    state.confirmScopeKey = confirmScopeOf(next);
    state.data = next;
    pruneDrafts();
    render();
    return controller;
  }

  function destroy() {
    if (!root) return;
    root.removeEventListener("click", onClick);
    root.removeEventListener("input", onFieldEvent);
    root.removeEventListener("change", onFieldEvent);
    root.remove();
    root = null;
  }

  function clearFeedback() {
    state.feedback = null;
    state.fieldErrors = {};
    render();
    return controller;
  }

  function getDraft() {
    const confirmDrafts = confirmBucket();
    const current = bucket();
    return {
      scope: state.scopeKey,
      confirmScope: state.confirmScopeKey,
      confirm: Object.fromEntries(Array.from(confirmDrafts.entries()).map(([key, value]) => [key, { ...value }])),
      decision: {
        mode: current.mode,
        return_to: current.return_to,
        reason: current.reason,
        rows: Object.fromEntries(Array.from(current.rows.entries()).map(([key, value]) => [key, { ...value }])),
      },
      pending: state.pending,
    };
  }

  const controller = { mount, update, destroy, clearFeedback, getDraft };
  return controller;
}
