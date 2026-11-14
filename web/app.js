/* =============================================================================
 * web/app.js —— 组件演示壳渲染层
 *
 * 只做三件事：把 state 画成任务列表 / 商品卡片 / 执行进度；把点击转成
 * workspace 调用；把 501 与错误如实显示。它不直接发请求，也不推断业务状态。
 *
 * 演示壳故意不实现任何写动作：allowed_actions 只展示按钮，点击提示未实现。
 * ========================================================================== */

import { createClient } from "./api.js";
import { createWorkspace } from "./state.js";

const DEMO_PASSWORD = "customs-demo-only";
const DEMO_ACCOUNTS = [
  { username: "operator_a", name: "业务员甲", note: "可见两个契约演示任务" },
  { username: "operator_b", name: "业务员乙", note: "只看到 P01 演示任务" },
  { username: "expert", name: "专家", note: "被授权任务；写动作未实现" },
  { username: "supplier_a", name: "供应商甲", note: "自己上传的任务" },
  { username: "supplier_b", name: "供应商乙", note: "验证越权：别人的任务 404" },
];

const TASK_STAGE = {
  uploaded: { label: "已上传", tone: "" },
  prechecking: { label: "预审中", tone: "azure" },
  awaiting_confirmation: { label: "待确认", tone: "gold" },
  submitted: { label: "已提交", tone: "azure" },
  approved: { label: "已通过", tone: "jade" },
  returned: { label: "已退回", tone: "cinnabar" },
};

const COMPARISON = {
  association_error: { label: "关联异常", tone: "cinnabar" },
  supplier_missing: { label: "原码缺失", tone: "" },
  supplier_invalid: { label: "原码异常", tone: "cinnabar" },
  needs_confirmation: { label: "待确认", tone: "gold" },
  consistent: { label: "一致", tone: "jade" },
  inconsistent: { label: "不一致", tone: "gold" },
};

const IMAGE = {
  available: { label: "有图", tone: "jade" },
  absent: { label: "无图", tone: "" },
  read_failed: { label: "读图失败", tone: "cinnabar" },
};

const EVIDENCE = {
  obtained: { label: "已取得依据", tone: "jade" },
  not_found: { label: "未查得", tone: "gold" },
  query_failed: { label: "查询失败", tone: "cinnabar" },
  version_unverified: { label: "版本未核", tone: "gold" },
};

const RUN_STATUS = {
  queued: { label: "排队", tone: "" },
  running: { label: "执行中", tone: "azure" },
  succeeded: { label: "成功", tone: "jade" },
  failed: { label: "失败", tone: "cinnabar" },
};

const RUN_KIND = { precheck: "预审", expert_check: "专家核查" };

const STAGES = [
  ["precheck_suggested_hs", "预审建议"],
  ["business_confirmed_hs", "业务确认"],
  ["submitted_hs", "已提交"],
  ["expert_suggested_hs", "专家建议"],
  ["expert_final_hs", "专家终值"],
];

const ACTION_LABEL = {
  read: "读取",
  "version.create": "新建版本",
  "confirmation.write": "确认编码",
  "submission.create": "提交审核",
  "decision.resolve": "审核决定",
};

const root = document.querySelector("#app");
const mode = new URLSearchParams(location.search).get("mode") === "fixture" ? "fixture" : "live";
const workspace = createWorkspace(createClient({ mode }));
const { store } = workspace;

/* ------------------------------ 渲染工具 --------------------------------- */

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

const badge = ({ label, tone }) => `<span class="badge ${tone || ""}">${esc(label)}</span>`;

const loading = (text) => `<div class="loading">${esc(text)}</div>`;

function errorBlock(error, scope) {
  const messages = {
    unauthenticated: ["登录已失效", "刷新页面重新登录"],
    forbidden: ["没有该动作的权限", "allowed_actions 只用于展示，服务端仍会拒绝"],
    csrf: ["CSRF 校验失败", "登录后令牌已轮换；刷新页面重试"],
    missing: ["对象不存在或不在可见范围", "越权读取返回 404，不泄露是否存在"],
    not_implemented: ["该动作尚未实现", "写端点由后续任务包交付"],
    conflict: ["修订冲突", "刷新后按最新 revision 重试"],
    invalid: ["请求参数不合法", "检查 limit、stage 或 cursor"],
  };
  const [title, hint] = messages[error.kind] || ["加载失败", error.message || "未知错误"];
  return `<div class="state error">
    <b>${esc(title)}</b>
    <div>${esc(error.message || "")}</div>
    <div class="hint">${esc(hint)}${error.requestId ? ` · request_id=${esc(error.requestId)}` : ""}</div>
    <button class="btn" data-act="retry" data-scope="${scope}" style="margin-top:10px">重试</button>
  </div>`;
}

const empty = (title, hint) => `<div class="state"><b>${esc(title)}</b><div class="hint">${esc(hint)}</div></div>`;

function comparisonBadge(status, item, compact = false) {
  if (status) return badge(COMPARISON[status] || { label: status, tone: "" });
  if (item && item.latest_run_id) {
    // 列表列宽有限用短标签，完整含义放在 title 里
    return `<span class="badge cinnabar" title="执行技术失败，未产生比较结论">${compact ? "未出结论" : "未出结论（技术失败）"}</span>`;
  }
  return badge({ label: "未执行", tone: "" });
}

function stageRow(key, label, card) {
  const stage = card[key] || {};
  const emptyValue = !stage.value;
  const executed = Boolean(stage.source_id);
  const text = emptyValue ? (executed ? "未给出建议" : "—") : stage.value;
  return `<div class="stage">
    <span class="stage-label">${esc(label)}</span>
    <span class="stage-value ${emptyValue ? "empty" : ""}">${esc(text)}</span>
    <span class="stage-src">${stage.source_id ? `↳ ${esc(String(stage.source_id).slice(0, 8))}` : ""}</span>
    ${emptyValue && executed ? `<span class="stage-note">已执行，引擎未给出该阶段值；与“尚未执行”不同</span>` : ""}
  </div>`;
}

/* ------------------------------- 顶栏 ----------------------------------- */

function renderTopbar(state) {
  const account = state.me;
  return `<header class="topbar">
    <div class="brand">
      <svg width="26" height="26" viewBox="0 0 30 30" fill="none" aria-hidden="true">
        <rect x="1" y="1" width="28" height="28" rx="4" stroke="#0E4B4F" stroke-width="1.3"/>
        <path d="M8 10h10M8 15h13M8 20h7" stroke="#0E4B4F" stroke-width="1.3" stroke-linecap="round"/>
        <path d="M22 18.5v6M19 21.5h6" stroke="#BE3E2B" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <div>
        <div class="brand-name">关务审核工作台</div>
        <div class="brand-sub">P02 组件演示壳 · 契约 v1</div>
      </div>
    </div>
    <div class="topbar-spacer"></div>
    <div class="mode-switch" role="group" aria-label="数据来源模式">
      <button data-act="mode" data-mode="live" aria-pressed="${state.mode === "live"}">LIVE 接口</button>
      <button data-act="mode" data-mode="fixture" aria-pressed="${state.mode === "fixture"}">FIXTURE 样例</button>
    </div>
    ${account ? `<div class="account">
      <span class="avatar">${esc((account.display_name || account.username).slice(0, 1))}</span>
      <span><div>${esc(account.display_name || account.username)}</div>
      <div class="account-role">${esc(account.role_label || account.role || "")}</div></span>
      <button class="btn" data-act="logout">退出</button>
    </div>` : ""}
  </header>`;
}

function renderBanner(state) {
  if (state.mode === "fixture") {
    return `<div class="mock-banner"><b>FIXTURE 模式</b>
      <span>读取 contracts/fixtures/DEMO-SET.json，只用于看组件形状。</span>
      <span class="fixture-warn">本模式不校验权限、不写库，不能当作接口可用性证明。</span></div>`;
  }
  return `<div class="mock-banner"><b>MOCK 结果</b>
    <span>接口与数据库是真的，AI 归类结果是 mock 固定场景。</span>
    <span>不构成归类或税率依据；证据 source_type=synthetic。</span></div>`;
}

/* ------------------------------- 登录门 --------------------------------- */

function renderGate(state) {
  return `<main class="gate"><div class="gate-card">
    <div class="gate-kicker">Customs Review Ledger · Demo</div>
    <h1>选择演示角色进入</h1>
    <p class="lede">本页是 P02 的组件演示壳：只读查询真实接口，写动作尚未实现。
      统一密码 <span class="mono">${DEMO_PASSWORD}</span>，账号来自 <span class="mono">make seed-demo</span>。</p>
    <div class="role-grid">
      ${DEMO_ACCOUNTS.map((account) => `<button class="role-row" data-act="login" data-user="${account.username}">
        <b>${esc(account.name)}</b><span>${esc(account.username)} · ${esc(account.note)}</span>
      </button>`).join("")}
    </div>
    ${state.busy ? loading("登录中…") : ""}
    ${state.authError ? `<div class="gate-error">${esc(state.authError.message || "登录失败")}</div>` : ""}
    <div class="gate-foot">没有后端时可在右上角切到 FIXTURE 模式查看组件形状。</div>
  </div></main>`;
}

/* ------------------------------ 任务列表 -------------------------------- */

function renderTasks(state) {
  if (state.tasksLoading && !state.tasks.length) return loading("任务加载中…");
  if (state.tasksError) return errorBlock(state.tasksError, "tasks");
  if (!state.tasks.length) {
    return empty("当前账号没有可见任务", "授权由任务归属决定；换账号可对比 FIX-07 的越权行为");
  }
  const rows = state.tasks.map((task) => {
    const stage = TASK_STAGE[task.stage] || { label: task.stage, tone: "" };
    const run = task.latest_run_id
      ? `${RUN_KIND[task.latest_run_kind] || task.latest_run_kind || "执行"} · ${(RUN_STATUS[task.latest_run_status] || {}).label || "—"}`
      : "尚未执行";
    return `<button class="task-item" data-act="task" data-id="${esc(task.task_id)}" aria-current="${state.taskId === task.task_id}">
      <h3>${esc(task.title)}</h3>
      <div class="task-meta">
        ${badge(stage)}
        ${badge({ label: `V${task.current_version_number ?? "—"}`, tone: "teal" })}
        <span>${esc(task.supplier)}</span>
        ${badge({ label: task.result_mode === "mock" ? "MOCK" : "LIVE", tone: task.result_mode === "mock" ? "gold" : "jade" })}
      </div>
      <div class="task-run">最近执行：${esc(run)}</div>
    </button>`;
  }).join("");
  const more = state.tasksCursor
    ? `<button class="btn" data-act="more-tasks" style="width:100%">加载更多</button>`
    : "";
  return rows + more;
}

/* ------------------------------ 商品列表 -------------------------------- */

function renderItems(state) {
  if (!state.taskId) return empty("选择左侧任务", "选中任务后显示当前版本的商品行");
  if (state.itemsError) return errorBlock(state.itemsError, "items");
  if (!state.items.length) {
    return state.versionId ? loading("商品加载中…") : empty("该任务没有版本", "V1 由上传任务创建");
  }
  return state.items.map((item) => {
    const raw = item.supplier_hs_raw || "";
    const suggested = item.precheck_suggested_hs || "";
    return `<button class="item-row" data-act="item" data-id="${esc(item.item_id)}" aria-current="${state.itemId === item.item_id}">
      <span class="row-no">${item.source_location.row_number}</span>
      <span class="name" title="${esc(item.name)}">${esc(item.name)}</span>
      <span class="code ${raw ? "" : "empty"}">${raw ? esc(raw) : "（空白）"}</span>
      <span class="code ${suggested ? "" : "empty"}">${suggested ? esc(suggested) : "—"}</span>
      ${comparisonBadge(item.comparison_status, item, true)}
    </button>`;
  }).join("");
}

function renderVersions(state) {
  const task = state.task;
  if (!task || !task.versions) return "";
  return `<div class="version-tabs" role="tablist" aria-label="文件版本">
    ${task.versions.map((version) => `<button class="version-tab" role="tab"
      data-act="version" data-id="${esc(version.version_id)}"
      aria-current="${state.versionId === version.version_id}">
      V${version.number}${version.is_current ? `<span class="cur"> · 当前</span>` : ""}
    </button>`).join("")}
    ${state.versionId && state.items.length ? `<button class="btn" data-act="run" data-id="${esc(latestRunId(state))}" style="margin-left:auto">查看执行</button>` : ""}
  </div>`;
}

function latestRunId(state) {
  const run = state.items.find((item) => item.latest_run_id);
  return run ? run.latest_run_id : "";
}

/* -------------------------------- 卡片 ---------------------------------- */

function renderCard(state) {
  if (!state.itemId) return empty("选择一件商品", "这里显示完整卡片：事实、五阶段编码、问题与证据");
  if (state.cardError) return errorBlock(state.cardError, "card");
  if (!state.card) return loading("卡片加载中…");
  const card = state.card;
  const facts = card.facts || {};
  const attributes = Object.entries(facts.attributes || {});

  return `<div class="card">
    <div class="card-head">
      <div>
        <h2>${esc(facts.name)}</h2>
        <div class="card-loc">${esc(card.source_location.sheet_name)} · 第 ${card.source_location.row_number} 行 · ${esc(card.source_location.cells)} · rev ${card.revision}</div>
      </div>
      <div class="card-badges">
        ${badge({ label: card.result_mode === "mock" ? "MOCK" : "LIVE", tone: card.result_mode === "mock" ? "gold" : "jade" })}
        ${comparisonBadge(card.comparison_status, card)}
        ${badge(IMAGE[card.image_status] || { label: card.image_status, tone: "" })}
      </div>
    </div>

    <div class="section">
      <div class="section-title">编码阶段（空值与未执行分开显示）</div>
      <div class="stages">${STAGES.map(([key, label]) => stageRow(key, label, card)).join("")}</div>
    </div>

    <div class="section">
      <div class="section-title">申报事实（原文，不被结果回写）</div>
      <dl class="facts">
        <dt>供应商原码</dt>
        <dd class="${facts.supplier_hs_raw ? "mono" : "blank"}">${facts.supplier_hs_raw ? esc(facts.supplier_hs_raw) : "（空白，保留原样）"}</dd>
        <dt>申报要素</dt>
        <dd>${esc(facts.supplier_elements_raw || "—")}</dd>
        <dt>供应商退税率</dt>
        <dd class="${facts.supplier_tax_rate_raw ? "" : "blank"}">${esc(facts.supplier_tax_rate_raw || "（未提供）")}</dd>
        ${attributes.map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(String(value))}</dd>`).join("")}
      </dl>
    </div>

    <div class="section">
      <div class="section-title">结果指标</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${badge(EVIDENCE[card.evidence_status] || { label: "证据状态未产生", tone: "" })}
        ${badge({ label: card.confidence === null || card.confidence === undefined ? "置信度 —" : `置信度 ${card.confidence}`, tone: card.confidence === null || card.confidence === undefined ? "" : "teal" })}
        ${card.latest_run_id ? `<button class="btn" data-act="run" data-id="${esc(card.latest_run_id)}">查看该次执行</button>` : ""}
      </div>
      ${card.confidence === null || card.confidence === undefined ? `<div class="hint" style="font-size:10.5px;color:var(--ink-4);margin-top:4px">未给置信度是结论，不是缺字段；mock 置信度未经校准。</div>` : `<div class="hint" style="font-size:10.5px;color:var(--ink-4);margin-top:4px">mock 固定值，未经校准，不是准确率。</div>`}
    </div>

    ${card.issues && card.issues.length ? `<div class="section">
      <div class="section-title">问题（${card.issues.length}）</div>
      <div class="issues">${card.issues.map((issue) => `<div class="issue">
        <div><span class="code">${esc(issue.code)}</span> ${esc(issue.message)}</div>
        <div class="meta">${issue.field ? `字段 ${esc(issue.field)}` : ""}${issue.missing ? ` · 缺 ${esc(issue.missing)}` : ""}${issue.suggested_role ? ` · 建议由 ${esc(issue.suggested_role)} 补充` : ""}</div>
      </div>`).join("")}</div>
    </div>` : ""}

    ${card.images && card.images.length ? `<div class="section">
      <div class="section-title">附图（${card.images.length}）</div>
      <div class="evidence">${card.images.map((image) => `<div class="evidence-item">
        <span class="src">${esc(image.status)}</span> ${esc(image.source_sheet)} · ${esc(image.source_cell)}
        ${image.error_message ? `<div class="note">${esc(image.error_message)}</div>` : ""}
      </div>`).join("")}</div>
    </div>` : ""}

    ${card.evidence && card.evidence.length ? `<div class="section">
      <div class="section-title">依据（${card.evidence.length}）</div>
      <div class="evidence">${card.evidence.map((entry) => `<div class="evidence-item">
        <span class="src">${esc(entry.source_type)} · ${esc(entry.status)}</span> ${esc(entry.title)}
        <div class="note">${esc(entry.document || "")}${entry.locator ? ` · ${esc(entry.locator)}` : ""}${entry.note ? ` · ${esc(entry.note)}` : ""}</div>
      </div>`).join("")}</div>
    </div>` : ""}

    ${card.latest_expert_check ? `<div class="section">
      <div class="section-title">最近一次专家核查</div>
      <dl class="facts">
        <dt>核查目标</dt><dd class="mono">${esc(card.latest_expert_check.target_hs)}</dd>
        <dt>执行</dt><dd class="mono">${esc(String(card.latest_expert_check.run_id).slice(0, 8))}</dd>
        <dt>结论</dt><dd>${(card.latest_expert_check.findings || []).map((finding) => badge({ label: `${finding.aspect}: ${finding.verdict}`, tone: finding.verdict === "conflict" ? "cinnabar" : finding.verdict === "matched" ? "jade" : "gold" })).join(" ")}</dd>
      </dl>
    </div>` : ""}

    <div class="section">
      <div class="section-title">允许动作（演示壳只展示，写动作未实现）</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${(card.allowed_actions || []).filter((action) => action !== "read").map((action) =>
          `<button class="btn" data-act="pending" data-label="${esc(ACTION_LABEL[action] || action)}">${esc(ACTION_LABEL[action] || action)}</button>`
        ).join("") || `<span style="font-size:11.5px;color:var(--ink-4)">当前账号没有写动作</span>`}
      </div>
    </div>
  </div>`;
}

/* ------------------------------ 执行抽屉 -------------------------------- */

function renderDrawer(state) {
  if (!state.runId) return "";
  const body = state.runError
    ? errorBlock(state.runError, "run")
    : !state.run
      ? loading("执行详情加载中…")
      : renderRun(state.run, state);
  return `<aside class="drawer" aria-label="执行详情">
    <div class="drawer-head">
      <h2>执行详情</h2>
      <span class="mono" style="font-size:10px;color:var(--ink-4)">${esc(state.runId.slice(0, 8))}</span>
      <button class="icon-btn" data-act="close-run" aria-label="关闭">✕</button>
    </div>
    <div class="drawer-body">${body}</div>
  </aside>`;
}

function renderRun(run, state) {
  const progress = run.progress || {};
  const total = Math.max(1, progress.total || 0);
  const percent = (value) => `${Math.round(((value || 0) / total) * 100)}%`;
  // 执行详情的逐行结果不带商品名（契约如此），能用当前版本列表补名就补，否则缩略 item_id。
  const nameById = new Map((state.items || []).map((item) => [item.item_id, item.name]));
  const labelOf = (row) => row.name || nameById.get(row.item_id) || `${String(row.item_id).slice(0, 8)}…`;
  const rows = (run.items || []).map((row) => `<div class="run-row ${row.status === "failed" ? "failed" : ""}">
    <span class="row-no">${row.source_location ? row.source_location.row_number : "—"}</span>
    <span class="name" title="${esc(row.item_id)}">${esc(labelOf(row))}</span>
    <span>${row.status === "failed" ? badge({ label: "技术失败", tone: "cinnabar" }) : comparisonBadge(row.comparison_status, { latest_run_id: run.run_id }, true)}</span>
    ${row.error_message ? `<span class="why">${esc(row.error_message)}</span>` : ""}
  </div>`).join("");

  return `<div class="section">
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
        ${badge({ label: RUN_KIND[run.kind] || run.kind, tone: "teal" })}
        ${badge(RUN_STATUS[run.status] || { label: run.status, tone: "" })}
        ${run.is_current ? badge({ label: "版本内当前执行", tone: "jade" }) : badge({ label: "历史执行", tone: "" })}
        ${badge({ label: run.result_mode === "mock" ? "MOCK" : "LIVE", tone: run.result_mode === "mock" ? "gold" : "jade" })}
      </div>
      <div class="progress" role="img" aria-label="执行进度">
        <span class="ok" style="width:${percent(progress.succeeded)}"></span>
        <span class="bad" style="width:${percent(progress.failed)}"></span>
        <span class="run" style="width:${percent(progress.running)}"></span>
      </div>
      <div class="progress-legend">
        <span>总数 ${progress.total ?? 0}</span>
        <span style="color:var(--jade)">成功 ${progress.succeeded ?? 0}</span>
        <span style="color:var(--cinnabar)">失败 ${progress.failed ?? 0}</span>
        <span>排队 ${progress.queued ?? 0} · 执行中 ${progress.running ?? 0}</span>
      </div>
    </div>
    <div class="section">
      <div class="section-title">逐行状态</div>
      <div class="run-rows">${rows || empty("没有逐行结果", "执行可能尚未开始")}</div>
    </div>
    <div class="section">
      <div class="section-title">说明</div>
      <div style="font-size:11px;color:var(--ink-3)">技术失败行不写业务结论；等待补料不计入 failed。
        ${run.run_id ? `<div class="mono" style="font-size:10px;color:var(--ink-4);margin-top:4px">run_id=${esc(run.run_id)}</div>` : ""}</div>
    </div>`;
}

/* -------------------------------- 组装 ---------------------------------- */

let toastTimer = null;
function toast(text) {
  document.querySelector(".toast")?.remove();
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = text;
  document.body.appendChild(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 2600);
}

const scrollMemo = new Map();

function render(state) {
  const panes = ["tasks", "items", "card"];
  panes.forEach((key) => {
    const node = document.querySelector(`[data-scroll="${key}"]`);
    if (node) scrollMemo.set(key, node.scrollTop);
  });

  root.innerHTML = `
    ${renderTopbar(state)}
    ${renderBanner(state)}
    ${state.me
      ? `<div class="workspace">
          <section class="pane">
            <div class="pane-head"><span class="pane-title">任务</span>
              <span class="pane-count">${state.tasks.length}${state.tasksCursor ? "+" : ""}</span></div>
            <div class="pane-body" data-scroll="tasks">${renderTasks(state)}</div>
          </section>
          <section class="pane">
            <div class="pane-head"><span class="pane-title">${state.task ? esc(state.task.title) : "商品"}</span>
              <span class="pane-count">${state.items.length} 行</span></div>
            ${renderVersions(state)}
            <div class="pane-body" style="padding:0" data-scroll="items">${renderItems(state)}</div>
          </section>
          <section class="pane">
            <div class="pane-head"><span class="pane-title">商品卡片</span></div>
            <div class="pane-body" data-scroll="card">${renderCard(state)}</div>
          </section>
        </div>`
      : renderGate(state)}
    ${renderDrawer(state)}
  `;

  scrollMemo.forEach((value, key) => {
    const node = document.querySelector(`[data-scroll="${key}"]`);
    if (node) node.scrollTop = value;
  });
}

/* -------------------------------- 事件 ---------------------------------- */

document.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-act]");
  if (!target) return;
  const action = target.dataset.act;

  if (action === "mode") {
    location.search = target.dataset.mode === "fixture" ? "?mode=fixture" : "";
    return;
  }
  if (action === "login") {
    await workspace.login(target.dataset.user, DEMO_PASSWORD);
    return;
  }
  if (action === "logout") {
    await workspace.logout();
    return;
  }
  if (action === "task") {
    await workspace.openTask(target.dataset.id);
    return;
  }
  if (action === "version") {
    await workspace.openVersion(target.dataset.id);
    return;
  }
  if (action === "item") {
    await workspace.selectItem(target.dataset.id);
    return;
  }
  if (action === "run") {
    if (target.dataset.id) await workspace.openRun(target.dataset.id);
    return;
  }
  if (action === "close-run") {
    workspace.closeRun();
    return;
  }
  if (action === "more-tasks") {
    await workspace.loadTasks({ append: true });
    return;
  }
  if (action === "pending") {
    toast(`${target.dataset.label}：写动作尚未实现（501），由后续任务包交付`);
    return;
  }
  if (action === "retry") {
    const scope = target.dataset.scope;
    if (scope === "tasks") await workspace.loadTasks();
    if (scope === "items") await workspace.openVersion(store.get().versionId);
    if (scope === "card") await workspace.selectItem(store.get().itemId);
    if (scope === "run") await workspace.openRun(store.get().runId);
  }
});

store.subscribe(render);
workspace.bootstrap();
