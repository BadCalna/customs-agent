/* =============================================================================
 * web/components/version-history.js —— 只读版本 / 提交 / 专家决定历史（P11）
 *
 * 数据由调用方（页面或 state 层）提供，本组件不发请求、不写任何状态。
 * 视图模型按「版本 → 提交 → 专家决定」三层分组；字段命名与 P02 契约保持一致：
 * version_id / number / is_current、submission_id / revision / status、
 * decision action / return_to / reason、DecisionItem previous_hs / final_hs /
 * change_reason，以及 latest_expert_check.target_hs。
 *
 * FIX-09 的丢弃规则：
 * - 调用方负责请求令牌；本组件额外做一次身份校验——每份历史数据带自己的
 *   task_id / version_id，只有与当前视图的 activeVersionId 一致才接受；
 * - 版本作用域响应缺少 version_id 时视为“无法证明属于当前版本”，拒绝渲染；
 * - 拒绝时保留上一份已接受的数据，只显示一条“已丢弃”提示，不用 V1 覆盖 V2。
 *
 * 跨版本候选只作候选展示：不宣称同一商品身份，不继承确认、提交或审批结论。
 * ========================================================================== */

let instanceSeq = 0;

const SUBMISSION_STATUS = Object.freeze({
  pending: { label: "待专家审核", tone: "gold" },
  approved: { label: "已通过", tone: "jade" },
  returned: { label: "已退回", tone: "cinnabar" },
  superseded: { label: "已被新提交取代", tone: "" },
});

const DECISION_ACTION = Object.freeze({
  approve: { label: "通过", tone: "jade" },
  return: { label: "退回", tone: "cinnabar" },
});

const RETURN_TARGET = Object.freeze({ supplier: "供应商", operator: "业务员" });

const text = (value) => String(value ?? "").trim();

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

const shortId = (value) => (value ? String(value).slice(0, 8) : "");

const versionLabel = (value) => {
  const raw = String(value ?? "");
  if (!raw) return "未限定版本";
  return raw.length > 12 ? `版本 ${shortId(raw)}` : `V${raw}`;
};

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

function rowNumberOf(entry, fallbackCard) {
  if (entry && entry.source_location && entry.source_location.row_number !== undefined) {
    return entry.source_location.row_number;
  }
  if (fallbackCard && fallbackCard.source_location) return fallbackCard.source_location.row_number;
  return "—";
}

function entryName(entry, fallbackCard) {
  if (entry && entry.name) return entry.name;
  if (fallbackCard && fallbackCard.facts && fallbackCard.facts.name) return fallbackCard.facts.name;
  return "（未命名商品）";
}

/* --------------------------- 视图模型规范化 ------------------------------ */

function normalizeDecision(decision) {
  const action = text(decision.action) || "";
  const items = Array.isArray(decision.items) ? decision.items : [];
  return {
    decision_id: decision.decision_id || "",
    submission_id: decision.submission_id || "",
    action,
    return_to: text(decision.return_to),
    reason: text(decision.reason),
    created_at: decision.created_at || "",
    created_by: text(decision.created_by || decision.reviewed_by),
    expert_check: decision.expert_check || null,
    items: items.map((item) => ({
      item_id: item.item_id || "",
      name: item.name || "",
      source_location: item.source_location || null,
      previous_hs: text(item.previous_hs),
      final_hs: text(item.final_hs),
      change_reason: text(item.change_reason),
    })),
  };
}

function normalizeSubmission(submission) {
  const items = Array.isArray(submission.items) ? submission.items : [];
  return {
    submission_id: submission.submission_id || "",
    revision: submission.revision,
    status: text(submission.status),
    created_at: submission.created_at || "",
    created_by: text(submission.created_by),
    expert_check: submission.expert_check || null,
    items: items.map((item) => ({
      item_id: item.item_id || "",
      name: item.name || "",
      source_location: item.source_location || null,
      submitted_hs: text(item.submitted_hs),
      item_revision: item.item_revision,
    })),
    decisions: (Array.isArray(submission.decisions) ? submission.decisions : []).map(normalizeDecision),
  };
}

function normalizeVersion(version) {
  const submissions = (Array.isArray(version.submissions) ? version.submissions : []).map(normalizeSubmission);
  const orphans = [];
  const flatDecisions = Array.isArray(version.decisions) ? version.decisions : [];

  for (const raw of flatDecisions) {
    const decision = normalizeDecision(raw);
    const target = decision.submission_id
      ? submissions.find((submission) => submission.submission_id === decision.submission_id)
      : null;
    if (target) target.decisions.push(decision);
    else orphans.push(decision);
  }

  return {
    version_id: version.version_id || "",
    number: version.number,
    original_filename: text(version.original_filename),
    uploaded_at: version.uploaded_at || "",
    is_current: version.is_current === true,
    approved: version.approved === true,
    submissions,
    orphan_decisions: orphans,
  };
}

function normalizeHistory(data) {
  if (!data || typeof data !== "object") return null;
  if (Array.isArray(data.versions)) {
    return {
      task_id: data.task_id || "",
      current_version_id: data.current_version_id || "",
      versions: data.versions.map(normalizeVersion),
      candidates: Array.isArray(data.candidates) ? data.candidates : [],
    };
  }
  if (data.version_id) {
    return {
      task_id: data.task_id || "",
      current_version_id: data.current_version_id || data.version_id,
      versions: [normalizeVersion(data)],
      candidates: Array.isArray(data.candidates) ? data.candidates : [],
    };
  }
  return {
    task_id: data.task_id || "",
    current_version_id: data.current_version_id || "",
    versions: [],
    candidates: Array.isArray(data.candidates) ? data.candidates : [],
  };
}

function versionApproved(version) {
  if (version.approved) return true;
  return version.submissions.some(
    (submission) =>
      submission.status === "approved" ||
      submission.decisions.some((decision) => decision.action === "approve"),
  );
}

/* ------------------------------ 组件主体 -------------------------------- */

/**
 * 创建只读版本历史。
 *
 * @param {object} options
 * @param {object} [options.data] 首次渲染的历史数据
 * @param {string} [options.taskId] 当前任务标识
 * @param {string} [options.activeVersionId] 当前视图版本；设置后启用 FIX-09 守卫
 * @param {string} [options.currentVersionId] 任务当前版本，用于“当前/旧版本”标记
 * @returns {{mount: Function, update: Function, accept: Function, destroy: Function, getState: Function}}
 */
export function createVersionHistory(options = {}) {
  const uid = `vh-${++instanceSeq}`;
  let root = null;

  const state = {
    data: null,
    taskId: text(options.taskId),
    activeVersionId: text(options.activeVersionId),
    currentVersionId: text(options.currentVersionId),
    activeVersionLabel: text(options.activeVersionLabel),
    currentVersionLabel: text(options.currentVersionLabel),
    rejection: null,
  };

  function reject(expectedVersion, payloadVersion, reason, payloadNumber) {
    state.rejection = {
      expected: expectedVersion || "（未设置）",
      expectedLabel: state.activeVersionLabel || (expectedVersion ? `版本 ${shortId(expectedVersion)}` : "（未设置）"),
      received: payloadVersion || "（未标注）",
      receivedLabel: payloadNumber ? `V${payloadNumber}` : payloadVersion ? `版本 ${shortId(payloadVersion)}` : "（未标注）",
      reason,
    };
  }

  /**
   * 接受一份历史数据。返回 false 表示版本不匹配、已丢弃，当前视图不变。
   * @param {object} data
   * @param {{taskId?: string, versionId?: string}} [scope]
   */
  function accept(data, scope = {}) {
    if (!data || typeof data !== "object") {
      state.rejection = {
        expected: state.activeVersionId || "（未设置）",
        expectedLabel: state.activeVersionLabel || "（未设置）",
        received: "（空响应）",
        receivedLabel: "（空响应）",
        reason: "空响应",
      };
      render();
      return false;
    }

    const expectedTask = text(scope.taskId || state.taskId);
    const expectedVersion = text(scope.versionId || state.activeVersionId);
    const payloadTask = text(data.task_id);
    const payloadVersion = text(data.version_id);
    const taskScoped = Array.isArray(data.versions);

    if (expectedTask && payloadTask && payloadTask !== expectedTask) {
      reject(expectedVersion, payloadVersion, `任务不匹配（响应 ${shortId(payloadTask)}）`, data.number);
      render();
      return false;
    }
    if (expectedVersion && payloadVersion && payloadVersion !== expectedVersion) {
      reject(expectedVersion, payloadVersion, "版本不匹配，晚到的旧响应已丢弃（FIX-09）", data.number);
      render();
      return false;
    }
    if (expectedVersion && taskScoped && data.current_version_id && data.current_version_id !== expectedVersion) {
      reject(
        expectedVersion,
        data.current_version_id,
        "任务级历史快照的 current_version_id 与当前视图不一致，晚到快照已丢弃（FIX-09）",
        null,
      );
      render();
      return false;
    }
    if (expectedVersion && !payloadVersion && !taskScoped) {
      reject(expectedVersion, payloadVersion, "响应未标注 version_id，无法确认属于当前版本", data.number);
      render();
      return false;
    }

    state.data = data;
    state.rejection = null;
    render();
    return true;
  }

  function update({ data, taskId, activeVersionId, currentVersionId, activeVersionLabel, currentVersionLabel } = {}) {
    if (taskId !== undefined) state.taskId = text(taskId);
    if (activeVersionId !== undefined) state.activeVersionId = text(activeVersionId);
    if (currentVersionId !== undefined) state.currentVersionId = text(currentVersionId);
    if (activeVersionLabel !== undefined) state.activeVersionLabel = text(activeVersionLabel);
    if (currentVersionLabel !== undefined) state.currentVersionLabel = text(currentVersionLabel);
    if (data !== undefined) {
      accept(data, { taskId: state.taskId, versionId: state.activeVersionId });
    } else {
      render();
    }
    return controller;
  }

  /* ------------------------------- 渲染 --------------------------------- */

  function renderRejection() {
    if (!state.rejection) return "";
    return `<div class="vh-rejection" role="alert" tabindex="-1">
      <b>已丢弃版本不匹配的历史响应</b>
      <p>本次响应所属版本为 <span class="mono">${esc(state.rejection.receivedLabel)}${state.rejection.received !== "（未标注）" && state.rejection.received !== "（空响应）" ? `（${esc(shortId(state.rejection.received))}）` : ""}</span>，
        当前视图是 <span class="mono">${esc(state.rejection.expectedLabel)}${state.rejection.expected !== "（未设置）" ? `（${esc(shortId(state.rejection.expected))}）` : ""}</span>：${esc(state.rejection.reason)}。</p>
      <p>上一份已接受的历史保持显示，不会被晚到的旧版本覆盖。</p>
    </div>`;
  }

  function renderHeader(history) {
    const versionCount = history ? history.versions.length : 0;
    const submissionCount = history
      ? history.versions.reduce((total, version) => total + version.submissions.length, 0)
      : 0;
    const decisionCount = history
      ? history.versions.reduce(
          (total, version) =>
            total +
            version.submissions.reduce((sum, submission) => sum + submission.decisions.length, 0) +
            version.orphan_decisions.length,
          0,
        )
      : 0;
    const dataTag = history
      ? history.versions.length === 1 && history.versions[0].version_id
        ? `version_id=${shortId(history.versions[0].version_id)}`
        : `versions=${history.versions.map((version) => `V${version.number ?? "?"}`).join(" / ") || "—"}`
      : "未提供数据";

    return `<header class="vh-head">
      <div>
        <div class="vh-kicker">只读历史 · 不做编辑或回退</div>
        <h2 class="vh-title">版本 / 提交 / 专家决定</h2>
        <p class="vh-meta mono">
          <span>task ${esc(shortId(history && history.task_id))}</span>
          <span>${esc(dataTag)}</span>
          <span>当前视图 ${esc(state.activeVersionLabel || versionLabel(state.activeVersionId))}</span>
        </p>
      </div>
      <div class="vh-badges">
        ${badge("只读", "")}
        ${state.currentVersionId ? badge(`当前版本 ${state.currentVersionLabel || shortId(state.currentVersionId)}`, "teal") : ""}
        <span class="vh-count">${versionCount} 个版本 · ${submissionCount} 次提交 · ${decisionCount} 个决定</span>
      </div>
    </header>`;
  }

  function renderVersion(version) {
    const isCurrent = version.is_current || (state.currentVersionId && version.version_id === state.currentVersionId);
    const approved = versionApproved(version);
    const headingId = `${uid}-version-${esc(version.version_id || "unknown")}`;
    const label = `V${version.number ?? "?"}`;
    const flags = [
      isCurrent ? badge("当前版本", "teal") : badge("旧版本", ""),
      !isCurrent && approved ? badge("旧版本已通过", "jade") : "",
      isCurrent && approved ? badge("当前版本已通过", "jade") : "",
    ].filter(Boolean).join("");

    return `<section class="vh-version" data-version-id="${esc(version.version_id)}" aria-labelledby="${headingId}">
      <div class="vh-version__head">
        <div>
          <h3 id="${headingId}">${esc(label)} <span class="vh-filename">${esc(version.original_filename || "（未提供文件名）")}</span></h3>
          <p class="vh-meta mono">
            version ${esc(shortId(version.version_id))}
            ${version.uploaded_at ? ` · 上传 ${esc(fmtTime(version.uploaded_at))}` : ""}
          </p>
        </div>
        <div class="vh-badges">${flags}</div>
      </div>
      ${!isCurrent && approved
        ? `<p class="vh-caution" role="note">该版本的“已通过”只属于该版本；不授予当前版本，也不沿用提交或决定（R06/B07）。</p>`
        : ""}
      ${isCurrent
        ? `<p class="vh-current-note">当前版本视图只接受同一 version_id 的历史响应；V1 晚到响应会被丢弃，不覆盖本视图（FIX-09）。</p>`
        : ""}
      <div class="vh-submissions">
        ${version.submissions.length
          ? version.submissions.map((submission) => renderSubmission(version, submission)).join("")
          : `<p class="vh-empty">该版本没有提交记录；没有提交就不产生专家决定。</p>`}
      </div>
      ${version.orphan_decisions.length
        ? `<div class="vh-orphans">
            <h4>未关联到提交的决定</h4>
            <p class="vh-caution" role="note">历史数据把决定放在版本级且没有匹配的 submission_id；原样展示，不猜它属于哪次提交。</p>
            ${version.orphan_decisions.map((decision) => renderDecision(version, null, decision)).join("")}
          </div>`
        : ""}
    </section>`;
  }

  function renderSubmission(version, submission) {
    const status = SUBMISSION_STATUS[submission.status] || { label: submission.status || "状态未提供", tone: "" };
    const headingId = `${uid}-submission-${esc(submission.submission_id || "unknown")}`;
    const rows = submission.items.map((item) => `<tr>
      <td class="mono">${esc(rowNumberOf(item, null))}</td>
      <td>${esc(entryName(item, null))}</td>
      <td class="mono">${esc(item.submitted_hs || "—")}</td>
      <td class="mono">${esc(item.item_revision ?? "—")}</td>
    </tr>`).join("");

    return `<article class="vh-submission" aria-labelledby="${headingId}">
      <div class="vh-submission__head">
        <div>
          <h4 id="${headingId}">提交 <span class="mono">${esc(shortId(submission.submission_id))}</span></h4>
          <p class="vh-meta">
            ${badge(status.label, status.tone)}
            <span class="mono">revision ${esc(submission.revision ?? "—")}</span>
            ${submission.created_at ? ` · ${esc(fmtTime(submission.created_at))}` : ""}
            ${submission.created_by ? ` · ${esc(submission.created_by)}` : ""}
          </p>
        </div>
      </div>
      ${submission.items.length
        ? `<div class="vh-table-wrap">
            <table class="vh-table">
              <caption class="sr-only">该次提交的逐行快照，提交值不可变</caption>
              <thead><tr><th scope="col">行</th><th scope="col">商品</th><th scope="col">提交 HS</th><th scope="col">商品修订</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>`
        : `<p class="vh-empty">提交没有逐行明细；历史原样显示，不推断提交值。</p>`}
      <div class="vh-decisions">
        ${submission.decisions.length
          ? submission.decisions.map((decision) => renderDecision(version, submission, decision)).join("")
          : `<p class="vh-empty">尚无专家决定。</p>`}
      </div>
    </article>`;
  }

  function renderDecision(version, submission, decision) {
    const action = DECISION_ACTION[decision.action] || { label: decision.action || "动作未提供", tone: "" };
    const rows = decision.items.map((item) => {
      // 前值只从本提交快照取；不在其他版本里按 item_id 猜同一商品。
      const localRow = submission ? submission.items.find((row) => row.item_id === item.item_id) : null;
      const previous = item.previous_hs || (localRow ? localRow.submitted_hs : "");
      const changed = Boolean(previous) && item.final_hs !== previous;
      return `<tr>
        <td class="mono">${esc(rowNumberOf(item, null))}</td>
        <td>${esc(entryName(item, null))}</td>
        <td class="mono">${esc(previous || "—")}</td>
        <td class="mono">${esc(item.final_hs || "—")}</td>
        <td>${changed ? badge("改码", "gold") : badge("未改", "")}</td>
        <td>${esc(changed ? item.change_reason || "（缺少改码原因）" : "终值等于提交值")}</td>
      </tr>`;
    }).join("");

    const expertCheck = decision.expert_check || (submission && submission.expert_check) || null;
    const target = expertCheck && expertCheck.target_hs ? text(expertCheck.target_hs) : "";
    const changedFinals = decision.items
      .map((item) => item.final_hs)
      .filter((final) => final && target && final !== target);

    return `<section class="vh-decision" aria-label="专家决定 ${esc(shortId(decision.decision_id))}">
      <div class="vh-decision__head">
        <div>
          <h5>专家决定 <span class="mono">${esc(shortId(decision.decision_id))}</span></h5>
          <p class="vh-meta">
            ${badge(action.label, action.tone)}
            ${decision.action === "return" ? badge(`退回给${RETURN_TARGET[decision.return_to] || decision.return_to || "未标对象"}`, "cinnabar") : ""}
            ${decision.created_at ? `<span>${esc(fmtTime(decision.created_at))}</span>` : ""}
            ${decision.created_by ? `<span>${esc(decision.created_by)}</span>` : ""}
          </p>
        </div>
      </div>
      ${decision.reason ? `<p class="vh-reason"><b>决定说明：</b>${esc(decision.reason)}</p>` : `<p class="vh-empty">决定未填写说明；历史原样显示。</p>`}
      ${decision.items.length
        ? `<div class="vh-table-wrap">
            <table class="vh-table">
              <caption class="sr-only">专家逐行终值与改码留痕</caption>
              <thead><tr><th scope="col">行</th><th scope="col">商品</th><th scope="col">提交值</th><th scope="col">专家终值</th><th scope="col">是否改码</th><th scope="col">改码原因</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>`
        : `<p class="vh-empty">决定没有逐行终值明细；不把提交值当专家终值展示。</p>`}
      ${target
        ? `<p class="vh-check ${changedFinals.length ? "vh-check--warn" : ""}" role="note">
            本次专家 AI 核查目标：<span class="mono">${esc(target)}</span>
            ${expertCheck.status ? ` · 状态 ${esc(expertCheck.status)}` : ""}。
            ${changedFinals.length
              ? `专家终值 <span class="mono">${esc(changedFinals.join(" / "))}</span> 与核查目标不同；旧核查结论不外推到新终值（FIX-10）。`
              : "终值与核查目标一致，但仍以人工决定为准。"}
          </p>`
        : `<p class="vh-empty">没有专家核查目标记录；不假设 AI 已核查该终值。</p>`}
    </section>`;
  }

  function renderCandidates(history) {
    if (!history || !history.candidates.length) return "";
    const headingId = `${uid}-candidates`;
    return `<section class="vh-candidates" aria-labelledby="${headingId}">
      <h3 id="${headingId}">跨版本候选对照</h3>
      <p class="vh-warning" role="note">
        仅作候选提示：不表示同一商品、不建立稳定商品身份，也不继承确认、提交或审批结论。
        跨版本商品 ID 不同，核对时请以原文件行号与商品 ID 为准。
      </p>
      <ul class="vh-candidate-list">
        ${history.candidates.map((pair) => {
          const left = pair.left || {};
          const right = pair.right || {};
          return `<li>
            <div class="vh-candidate__side">
              <span class="vh-candidate__ver mono">V${esc(left.version_number ?? "?")}</span>
              <span class="mono">第 ${esc(left.row_number ?? "—")} 行</span>
              <span>${esc(left.name || "（未命名商品）")}</span>
              <span class="mono">${esc(left.hs || "—")}</span>
            </div>
            <span class="vh-candidate__arrow" aria-hidden="true">↔</span>
            <div class="vh-candidate__side">
              <span class="vh-candidate__ver mono">V${esc(right.version_number ?? "?")}</span>
              <span class="mono">第 ${esc(right.row_number ?? "—")} 行</span>
              <span>${esc(right.name || "（未命名商品）")}</span>
              <span class="mono">${esc(right.hs || "—")}</span>
            </div>
            <span class="vh-candidate__basis">候选依据：${esc(pair.basis || "未提供")}</span>
          </li>`;
        }).join("")}
      </ul>
    </section>`;
  }

  function render() {
    if (!root) return;
    const history = normalizeHistory(state.data);
    const versions = history ? history.versions : [];
    root.innerHTML = `<div class="vh-stack" data-history-version="${esc(state.data && state.data.version_id ? state.data.version_id : "")}">
      ${renderHeader(history)}
      ${renderRejection()}
      ${versions.length
        ? versions.map(renderVersion).join("")
        : `<div class="vh-empty" role="status">
            <b>尚未提供历史数据</b>
            <p>本组件是只读视图，不会自行请求历史端点；由页面或 state 层传入已接受的数据。</p>
          </div>`}
      ${renderCandidates(history)}
      <p class="vh-footnote">历史只读：确认、提交、决定和逐行终值落库后不可变；本组件不提供编辑、重做或回退入口。</p>
    </div>`;
    if (state.rejection) {
      const node = root.querySelector(".vh-rejection");
      if (node && typeof node.focus === "function") {
        try {
          node.focus({ preventScroll: true });
        } catch {
          node.focus();
        }
      }
    }
  }

  function mount(container) {
    if (!container || typeof container.replaceChildren !== "function") {
      throw new Error("createVersionHistory().mount 需要一个 DOM 容器");
    }
    if (root) destroy();
    root = document.createElement("div");
    root.className = "vh-root";
    container.replaceChildren(root);
    if (state.data) {
      const accepted = accept(state.data, { taskId: state.taskId, versionId: state.activeVersionId });
      if (accepted) return controller;
    }
    render();
    return controller;
  }

  function destroy() {
    if (!root) return;
    root.remove();
    root = null;
  }

  function getState() {
    return {
      taskId: state.taskId,
      activeVersionId: state.activeVersionId,
      currentVersionId: state.currentVersionId,
      activeVersionLabel: state.activeVersionLabel,
      currentVersionLabel: state.currentVersionLabel,
      rejection: state.rejection ? { ...state.rejection } : null,
      hasData: Boolean(state.data),
    };
  }

  const controller = { mount, update, accept, destroy, getState };
  return controller;
}
