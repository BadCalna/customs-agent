/* =============================================================================
 * web/components/version-history.js —— 只读版本 / 提交 / 专家决定历史（P11）
 *
 * 数据由调用方（页面或 state 层）提供，本组件不发请求、不写任何状态。
 * 视图模型按「版本 → 提交 → 专家决定」三层分组；字段命名与 P02 契约保持一致：
 * version_id / number / is_current、submission_id / revision / status、
 * decision action / return_to / reason、DecisionItem previous_hs / final_hs /
 * change_reason，以及 latest_expert_check.target_hs。
 *
 * FIX-09 的丢弃规则（2026-11-17 二次审核修正后）：
 * - 调用方负责请求令牌；本组件额外做一次身份校验，判定只以「当前视图状态」为准：
 *   版本作用域响应必须带 version_id 且等于 activeVersionId（调用方传入的 scope
 *   不能替换视图作用域，旧 scope 覆盖不了当前视图）；
 * - 任务级响应（带 versions[] 且不带 version_id）先校验 task_id，再要求 versions[]
 *   里存在 activeVersionId：正在查看 V2 时，一份只含 V1 的过期快照会把视图换成 V1，
 *   所以按“无法证明包含当前视图”丢弃。但「正在查看的版本 == 任务当前版本」仍然不是
 *   接受条件——用户看旧 V1、任务当前 V2、响应里两版都有时照常接受；
 * - 版本作用域响应缺少 version_id 时视为“无法证明属于当前版本”，拒绝渲染；
 * - 拒绝时保留上一份已接受的数据，只显示一条“已丢弃”提示，不用 V1 覆盖 V2；
 * - 构造参数 data 先放 pendingData，只有 accept 通过才落库：被拒绝的首次数据不会在
 *   mount 时被渲染，避免“已丢弃”提示与被丢弃内容同时出现的自相矛盾状态。
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
  if (fallbackCard && fallbackCard.name) return fallbackCard.name;
  if (fallbackCard && fallbackCard.facts && fallbackCard.facts.name) return fallbackCard.facts.name;
  return "（未命名商品）";
}

/* --------------------------- 视图模型规范化 ------------------------------ */

function normalizeDecision(decision) {
  const action = text(decision.action) || "";
  const items = Array.isArray(decision.items) ? decision.items : [];
  const looseIssues = Array.isArray(decision.issues) ? decision.issues : [];
  const rows = items.map((item) => ({
    item_id: item.item_id || "",
    name: item.name || "",
    source_location: item.source_location || null,
    previous_hs: text(item.previous_hs),
    final_hs: text(item.final_hs),
    change_reason: text(item.change_reason),
    // 退回明细在处置组件里是 {item_id, issue}；历史数据也可能用 message 命名。
    issue: text(item.issue !== undefined && item.issue !== null ? item.issue : item.message),
  }));

  // 也接受决定级 issues:[{item_id, message}]：按 item_id 合并到对应行，不新造商品身份。
  // 同一商品的多条问题必须累加（用「；」连接，渲染时在问题列完整显示），
  // 不能只留第一条——静默丢掉第二、三条会让专家看到的退回依据不完整（P2）。
  for (const issue of looseIssues) {
    if (!issue || typeof issue !== "object") continue;
    const message = text(issue.message !== undefined && issue.message !== null ? issue.message : issue.issue);
    if (!message) continue;
    const itemId = text(issue.item_id);
    // item_id 为空时不按空串去合并别人的行，避免把问题挂到错误的商品上。
    const target = itemId ? rows.find((row) => row.item_id === itemId) : null;
    if (target) {
      target.issue = target.issue ? `${target.issue}；${message}` : message;
      continue;
    }
    rows.push({
      item_id: itemId,
      name: "",
      source_location: null,
      previous_hs: "",
      final_hs: "",
      change_reason: "",
      issue: message,
    });
  }

  return {
    decision_id: decision.decision_id || "",
    submission_id: decision.submission_id || "",
    action,
    return_to: text(decision.return_to),
    reason: text(decision.reason),
    created_at: decision.created_at || "",
    created_by: text(decision.created_by || decision.reviewed_by),
    expert_check: decision.expert_check || null,
    items: rows,
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
 * @param {object} [options.data] 首次渲染的历史数据；先作为待接受数据，accept 通过后才渲染
 * @param {string} [options.taskId] 当前任务标识
 * @param {string} [options.activeVersionId] 当前视图版本；设置后启用 FIX-09 守卫
 * @param {string} [options.currentVersionId] 任务当前版本，用于“当前/旧版本”标记
 * @returns {{mount: Function, update: Function, accept: Function, destroy: Function, getState: Function}}
 */
export function createVersionHistory(options = {}) {
  const uid = `vh-${++instanceSeq}`;
  let root = null;

  const state = {
    /* 只有 accept 通过的数据才会写进 data；构造参数先放 pendingData（M-08a + P2）。
     * 若首次 accept 被拒，pendingData 会被丢弃，mount 时不会渲染被拒绝的内容。 */
    data: null,
    pendingData: options.data && typeof options.data === "object" ? options.data : null,
    taskId: text(options.taskId),
    activeVersionId: text(options.activeVersionId),
    currentVersionId: text(options.currentVersionId),
    activeVersionLabel: text(options.activeVersionLabel),
    currentVersionLabel: text(options.currentVersionLabel),
    rejection: null,
  };

  function reject(expectedVersion, payloadVersion, reason, payloadNumber, receivedText = "") {
    state.rejection = {
      expected: expectedVersion || "（未设置）",
      expectedLabel: state.activeVersionLabel || (expectedVersion ? `版本 ${shortId(expectedVersion)}` : "（未设置）"),
      received: payloadVersion || "（未标注）",
      receivedLabel: payloadNumber ? `V${payloadNumber}` : payloadVersion ? `版本 ${shortId(payloadVersion)}` : "（未标注）",
      // 任务级响应没有 version_id：用响应里实际带的版本集合说明“收到了什么”。
      receivedText: text(receivedText),
      reason,
    };
  }

  /**
   * 接受一份历史数据。返回 false 表示版本不匹配、已丢弃，当前视图不变。
   *
   * 判定只用当前视图状态（state.taskId / state.activeVersionId）：scope 只是调用方
   * 自证，不能替换视图作用域，否则旧请求的 scope 能绕过 FIX-09 守卫（M-08b）。
   *
   * @param {object} data
   * @param {{taskId?: string, versionId?: string}} [scope] 调用方自证的作用域
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

    const expectedTask = state.taskId;
    const expectedVersion = state.activeVersionId;
    const payloadTask = text(data.task_id);
    const payloadVersion = text(data.version_id);
    const taskScoped = Array.isArray(data.versions);
    const scopeTask = text(scope.taskId);
    const scopeVersion = text(scope.versionId);

    // 调用方自证与当前视图不一致时直接丢弃：这类 scope 只会来自晚到的旧请求。
    if (scopeTask && expectedTask && scopeTask !== expectedTask) {
      reject(expectedVersion, payloadVersion, "调用方作用域任务与当前视图不一致，旧 scope 不能覆盖当前视图（FIX-09）", data.number);
      render();
      return false;
    }
    if (scopeVersion && expectedVersion && scopeVersion !== expectedVersion) {
      reject(expectedVersion, payloadVersion, "调用方作用域版本与当前视图不一致，旧 scope 不能覆盖当前视图（FIX-09）", data.number);
      render();
      return false;
    }
    if (expectedTask && payloadTask && payloadTask !== expectedTask) {
      reject(expectedVersion, payloadVersion, `任务不匹配（响应 ${shortId(payloadTask)}）`, data.number);
      render();
      return false;
    }

    if (!taskScoped) {
      if (expectedVersion && payloadVersion && payloadVersion !== expectedVersion) {
        reject(expectedVersion, payloadVersion, "版本不匹配，晚到的旧响应已丢弃（FIX-09）", data.number);
        render();
        return false;
      }
      if (expectedVersion && !payloadVersion) {
        reject(expectedVersion, payloadVersion, "响应未标注 version_id，无法确认属于当前版本", data.number);
        render();
        return false;
      }
    } else if (expectedVersion) {
      // 任务级历史必须先证明「这份快照包含当前视图版本」：正在看 V2 时收到一份只有 V1
      // 的过期快照（current_version_id="v1"），若接受就会把视图换成 V1，同时页面还宣称
      // V1 晚到响应会被丢弃（P1 回归）。注意这不等于要求「正在查看的版本 == 任务当前
      // 版本」：响应里只要同时含 V1 与 V2 就照常接受（M-08c）。
      const versionIds = (Array.isArray(data.versions) ? data.versions : [])
        .map((entry) => text(entry && entry.version_id))
        .filter(Boolean);
      if (!versionIds.includes(expectedVersion)) {
        const labels = (Array.isArray(data.versions) ? data.versions : [])
          .map((entry) => (entry && entry.number !== undefined && entry.number !== null ? `V${entry.number}` : "未编号版本"))
          .join(" / ");
        reject(
          expectedVersion,
          payloadVersion,
          "任务级快照不含当前视图版本，晚到的旧历史已丢弃（FIX-09）",
          data.number,
          `任务级快照（${labels || "没有版本"}）`,
        );
        render();
        return false;
      }
    }

    state.data = data;
    state.pendingData = null;
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
    const rejection = state.rejection;
    const received = rejection.receivedText
      ? esc(rejection.receivedText)
      : `${esc(rejection.receivedLabel)}${
          rejection.received !== "（未标注）" && rejection.received !== "（空响应）"
            ? `（${esc(shortId(rejection.received))}）`
            : ""
        }`;
    const expected = `${esc(rejection.expectedLabel)}${
      rejection.expected !== "（未设置）" ? `（${esc(shortId(rejection.expected))}）` : ""
    }`;
    return `<div class="vh-rejection" role="alert" tabindex="-1">
      <b>已丢弃版本不匹配的历史响应</b>
      <p>本次响应所属版本为 <span class="mono">${received}</span>，
        当前视图是 <span class="mono">${expected}</span>：${esc(rejection.reason)}。</p>
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
        ? `<p class="vh-current-note">当前版本视图只接受同一 version_id 的版本级响应，或包含本版本的任务级历史；不含本版本的晚到快照会被丢弃，不覆盖本视图（FIX-09）。</p>`
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

  /** 通过：逐行终值表。缺 final_hs 的行不推导「未改」，也不当成等于提交值。 */
  function renderFinalRows(submission, decision) {
    return decision.items.map((item) => {
      // 前值只从本提交快照取；不在其他版本里按 item_id 猜同一商品。
      const localRow = submission ? submission.items.find((row) => row.item_id === item.item_id) : null;
      const previous = item.previous_hs || (localRow ? localRow.submitted_hs : "");
      const final = item.final_hs;
      // 名字不能叫 state：会遮蔽外层组件的 state 对象。
      let finalState = "missing";
      if (final) finalState = !previous ? "unpaired" : final !== previous ? "changed" : "same";
      const stateCell = finalState === "changed"
        ? badge("改码", "gold")
        : finalState === "same"
          ? badge("终值等于提交值", "")
          : finalState === "unpaired"
            ? badge("缺前值，无法比对", "")
            : badge("无终值记录", "");
      const note = finalState === "changed"
        ? (item.change_reason || "（缺少改码原因）")
        : finalState === "same"
          ? "终值等于提交值"
          : finalState === "unpaired"
            ? "缺提交值前值，无法判断是否改码"
            : "该行没有终值记录；不推断是否与提交值相同";
      return `<tr>
        <td class="mono">${esc(rowNumberOf(item, localRow))}</td>
        <td>${esc(entryName(item, localRow))}</td>
        <td class="mono">${esc(previous || "—")}</td>
        <td class="mono">${esc(final || "—")}</td>
        <td>${stateCell}</td>
        <td>${esc(note)}</td>
      </tr>`;
    }).join("");
  }

  /** 退回：逐商品问题表。退回不产生终值，因此不出现「改码 / 未改 / 终值」列。 */
  function renderIssueRows(submission, decision) {
    return decision.items.map((item) => {
      const localRow = submission ? submission.items.find((row) => row.item_id === item.item_id) : null;
      const submitted = item.previous_hs || (localRow ? localRow.submitted_hs : "");
      return `<tr>
        <td class="mono">${esc(rowNumberOf(item, localRow))}</td>
        <td>${esc(entryName(item, localRow))}</td>
        <td class="mono">${esc(submitted || "—")}</td>
        <td>${item.issue ? esc(item.issue) : "（该行未附退回问题）"}</td>
      </tr>`;
    }).join("");
  }

  function renderDecision(version, submission, decision) {
    const action = DECISION_ACTION[decision.action] || { label: decision.action || "动作未提供", tone: "" };
    const isReturn = decision.action === "return";
    // action 是自由字符串（契约没有 enum 约束）：只有 "return" 确定是退回；缺失或其它取值
    // 时不能默认当“通过”，否则退回问题文本会彻底消失（P3）。未知动作按内容判据选表：
    // 有 final_hs 就展示终值表（有 issue 再补问题表），只有 issue 就展示问题表。
    const unknownAction = !isReturn && decision.action !== "approve";
    const hasFinals = decision.items.some((item) => item.final_hs);
    const hasIssues = decision.items.some((item) => item.issue);
    const showFinalTable = !isReturn && (!unknownAction || hasFinals);
    const showIssueTable = isReturn || hasIssues;

    const finalTable = showFinalTable && decision.items.length
      ? `<div class="vh-table-wrap">
            <table class="vh-table">
              <caption class="sr-only">专家逐行终值与改码留痕</caption>
              <thead><tr><th scope="col">行</th><th scope="col">商品</th><th scope="col">提交值</th><th scope="col">专家终值</th><th scope="col">终值状态</th><th scope="col">改码原因</th></tr></thead>
              <tbody>${renderFinalRows(submission, decision)}</tbody>
            </table>
          </div>`
      : "";

    const issueTable = showIssueTable && decision.items.length
      ? `<div class="vh-table-wrap">
            <table class="vh-table">
              <caption class="sr-only">退回决定逐商品问题；退回不产生专家终值</caption>
              <thead><tr><th scope="col">行</th><th scope="col">商品</th><th scope="col">提交值（只读）</th><th scope="col">退回问题</th></tr></thead>
              <tbody>${renderIssueRows(submission, decision)}</tbody>
            </table>
          </div>`
      : "";

    const emptyMessages = [];
    if (isReturn) emptyMessages.push("退回决定没有逐商品问题明细；原样展示，不补写问题内容。");
    else {
      if (showFinalTable) emptyMessages.push("决定没有逐行终值明细；不把提交值当专家终值展示。");
      if (showIssueTable) emptyMessages.push("决定没有逐商品问题明细；原样展示，不补写问题内容。");
    }
    // 未知 action 且既无终值也无问题时也不留白：说明按原样展示，不推断动作含义。
    if (!emptyMessages.length) emptyMessages.push("决定没有逐行终值或逐商品问题明细；原样展示，不推断动作含义。");
    const empty = emptyMessages.map((message) => `<p class="vh-empty">${message}</p>`).join("");

    const expertCheck = decision.expert_check || (submission && submission.expert_check) || null;
    const target = expertCheck && expertCheck.target_hs ? text(expertCheck.target_hs) : "";
    const finals = decision.items.map((item) => item.final_hs).filter(Boolean);
    const changedFinals = finals.filter((final) => target && final !== target);
    let checkText = "";
    let checkWarn = false;
    if (isReturn) {
      checkText = `退回不产生专家终值，核查结论不随退回外推到任何终值（FIX-10）。`;
    } else if (!finals.length) {
      // 没有终值就没有可比对的结论：既不写“一致”，也不写“不同”。
      checkText = "本次决定没有终值记录，无法与核查目标比对；不假设一致。";
    } else if (changedFinals.length) {
      checkWarn = true;
      checkText = `专家终值 <span class="mono">${esc(changedFinals.join(" / "))}</span> 与核查目标不同；旧核查结论不外推到新终值（FIX-10）。`;
    } else {
      checkText = "终值与核查目标一致，但仍以人工决定为准。";
    }

    return `<section class="vh-decision" aria-label="专家决定 ${esc(shortId(decision.decision_id))}">
      <div class="vh-decision__head">
        <div>
          <h5>专家决定 <span class="mono">${esc(shortId(decision.decision_id))}</span></h5>
          <p class="vh-meta">
            ${badge(action.label, action.tone)}
            ${isReturn ? badge(`退回给${RETURN_TARGET[decision.return_to] || decision.return_to || "未标对象"}`, "cinnabar") : ""}
            ${decision.created_at ? `<span>${esc(fmtTime(decision.created_at))}</span>` : ""}
            ${decision.created_by ? `<span>${esc(decision.created_by)}</span>` : ""}
          </p>
        </div>
      </div>
      ${decision.reason ? `<p class="vh-reason"><b>决定说明：</b>${esc(decision.reason)}</p>` : `<p class="vh-empty">决定未填写说明；历史原样显示。</p>`}
      ${isReturn ? `<p class="vh-note">退回决定只记录逐商品问题，不产生专家终值；问题内容原样展示。</p>` : ""}
      ${unknownAction
        ? `<p class="vh-note" role="note">动作取值“${esc(decision.action || "（缺失）")}”不在已知范围（approve / return）内；只按原始内容展示，不推断业务结论。</p>`
        : ""}
      ${finalTable}${issueTable}
      ${finalTable || issueTable ? "" : empty}
      ${target
        ? `<p class="vh-check ${checkWarn ? "vh-check--warn" : ""}" role="note">
            本次专家 AI 核查目标：<span class="mono">${esc(target)}</span>
            ${expertCheck.status ? ` · 状态 ${esc(expertCheck.status)}` : ""}。
            ${checkText}
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
    // 构造参数只作为待接受数据：通过 accept 才落库，被拒绝时不渲染它的内容（P2）。
    if (state.pendingData) {
      const candidate = state.pendingData;
      state.pendingData = null;
      if (accept(candidate, { taskId: state.taskId, versionId: state.activeVersionId })) return controller;
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
