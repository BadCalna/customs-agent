/* =============================================================================
 * web/components/hs-comparison.js —— 编码阶段对照（纯渲染）
 *
 * 口径（D3 第 3 节、D4）：
 * 1. 只读契约字段，不从展示文案反推状态；阶段值 null 且带 source_id 表示
 *    “执行过但未给出建议”，两者都空才是“尚未产生”；
 * 2. 供应商原码来自 facts.supplier_hs_raw，空白原样保留，不用任何建议值补位；
 * 3. 本文件不发请求、不读写存储，也不改业务状态；来源点击只回调页面。
 * ========================================================================== */

/** 五个编码阶段；sourceKind 是契约中该阶段引用的对象类型，仅用于来源说明。 */
export const STAGE_DEFS = Object.freeze([
  { key: "precheck_suggested_hs", label: "预审建议", sourceKind: "run" },
  { key: "business_confirmed_hs", label: "业务员确认", sourceKind: "confirmation" },
  { key: "submitted_hs", label: "提交值", sourceKind: "submission" },
  { key: "expert_suggested_hs", label: "专家建议", sourceKind: "run" },
  { key: "expert_final_hs", label: "专家终值", sourceKind: "decision" },
]);

/** D4 六类比较结论。文案描述结论本身，不描述审批结果。 */
export const COMPARISON_STATUS = Object.freeze({
  association_error: {
    label: "关联异常",
    tone: "cinnabar",
    hint: "商品与任务/版本的关联存在异常（契约结论）。",
  },
  supplier_missing: {
    label: "原码缺失",
    tone: "idle",
    hint: "供应商原码为空（契约结论），页面不会用建议值补位。",
  },
  supplier_invalid: {
    label: "原码异常",
    tone: "cinnabar",
    hint: "供应商原码异常（契约结论），原文仍按原样保留。",
  },
  needs_confirmation: {
    label: "待确认",
    tone: "gold",
    hint: "需要人工确认（契约结论）；待确认不是技术失败。",
  },
  consistent: {
    label: "一致",
    tone: "jade",
    hint: "比较结论为一致（契约结论），不等于审批结果。",
  },
  inconsistent: {
    label: "不一致",
    tone: "gold",
    hint: "比较结论为不一致（契约结论），需人工判断。",
  },
});

export const IMAGE_STATUS = Object.freeze({
  available: {
    label: "有附图",
    tone: "jade",
    hint: "源表存在可读取的附图。",
  },
  absent: {
    label: "无附图",
    tone: "idle",
    hint: "源表没有图片；这是“确实没有”，不是读取失败。",
  },
  read_failed: {
    label: "读图失败",
    tone: "cinnabar",
    hint: "源表有图片但读取失败；不能当作无图，需要补图或重读。",
  },
});

/** 来源类型的中文说明，页面展示来源详情时复用。 */
export const SOURCE_KIND_LABEL = Object.freeze({
  run: "执行（run）",
  confirmation: "确认记录（confirmation）",
  submission: "提交（submission）",
  decision: "专家决定（decision）",
});

const CLEANUP = Symbol.for("p07.hsComparison.cleanup");

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[ch]);
}

/** 统一的徽章片段；tone 仅接受本文件内的常量。 */
export function badgeHtml({ label, tone = "", title = "" }) {
  const titleAttr = title ? ` title="${escapeHtml(title)}"` : "";
  return `<span class="commodity-badge ${tone}"${titleAttr}>${escapeHtml(label)}</span>`;
}

/**
 * 把 StageValue 变成可展示的视图。
 * - 有值：显示原值；
 * - 无值但有 source_id：显示“未给出建议”；
 * - 两者都空：显示“—”。
 */
export function stageValueView(stage) {
  const hasStage = stage !== null && typeof stage === "object";
  const sourceId = hasStage && stage.source_id ? String(stage.source_id) : "";
  const raw = hasStage && stage.value !== null && stage.value !== undefined ? String(stage.value) : "";
  const hasValue = raw.trim() !== "";
  if (hasValue) {
    return { text: raw, hasValue: true, executed: Boolean(sourceId), sourceId, note: "" };
  }
  if (sourceId) {
    return {
      text: "未给出建议",
      hasValue: false,
      executed: true,
      sourceId,
      note: "该阶段有来源引用但未给出值；与“尚未产生该阶段”不同。",
    };
  }
  return { text: "—", hasValue: false, executed: false, sourceId: "", note: "" };
}

export function comparisonStatusView(item) {
  const status = item && item.comparison_status;
  if (status && COMPARISON_STATUS[status]) {
    return { status, ...COMPARISON_STATUS[status] };
  }
  if (status) {
    return {
      status,
      label: String(status),
      tone: "idle",
      hint: "契约未登记的枚举值，按原值显示，不推断含义。",
    };
  }
  if (item && item.latest_run_id) {
    return {
      status: null,
      label: "未得出结论",
      tone: "gold",
      hint: "comparison_status 为空且存在执行引用；页面不推断是待确认还是技术失败。",
    };
  }
  return {
    status: null,
    label: "未执行",
    tone: "idle",
    hint: "comparison_status 为空且没有执行引用。",
  };
}

export function imageStatusView(status) {
  if (status && IMAGE_STATUS[status]) return { status, ...IMAGE_STATUS[status] };
  if (status) {
    return {
      status,
      label: String(status),
      tone: "idle",
      hint: "契约未登记的图片状态，按原值显示。",
    };
  }
  return { status: null, label: "图片状态未提供", tone: "idle", hint: "契约 image_status 为空。" };
}

export function comparisonBadgeHtml(item, { compact = false } = {}) {
  const view = comparisonStatusView(item);
  const label = compact && view.label === "未得出结论" ? "未出结论" : view.label;
  return badgeHtml({ label, tone: view.tone, title: view.hint });
}

export function shortId(value, length = 8) {
  const text = String(value ?? "");
  if (!text) return "—";
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function stageRowHtml(card, def) {
  const view = stageValueView(card[def.key]);
  const source = view.sourceId
    ? `<button type="button" class="hs-source" data-stage-source="${escapeHtml(def.key)}"
        aria-label="查看${escapeHtml(def.label)}的来源引用 ${escapeHtml(shortId(view.sourceId))}"
        title="source_id=${escapeHtml(view.sourceId)}">↳ ${escapeHtml(shortId(view.sourceId))}</button>`
    : `<span class="hs-source hs-source-empty">无来源引用</span>`;
  const valueClass = view.hasValue ? "hs-value" : "hs-value hs-value-empty";
  const note = view.note ? `<span class="hs-note">${escapeHtml(view.note)}</span>` : "";
  return `<div class="hs-row" data-stage="${escapeHtml(def.key)}">
    <span class="hs-label">${escapeHtml(def.label)}</span>
    <span class="${valueClass}">${escapeHtml(view.text)}</span>
    ${source}
    ${note}
  </div>`;
}

/**
 * 在一个容器里渲染“供应商原码 + 五阶段”对照。
 * @param {HTMLElement} el
 * @param {{card?: object, onSelectSource?: (source: object) => void}} options
 */
export function renderHsComparison(el, { card = null, onSelectSource } = {}) {
  if (!el) return;
  if (el[CLEANUP]) {
    el[CLEANUP]();
    el[CLEANUP] = null;
  }
  if (!card) {
    el.innerHTML = `<p class="hs-empty">未选择商品，无法显示编码阶段对照。</p>`;
    return;
  }

  const facts = card.facts || {};
  const raw = typeof facts.supplier_hs_raw === "string" ? facts.supplier_hs_raw : "";
  const rawHasValue = raw.trim() !== "";
  const rawText = rawHasValue ? raw : "（原值为空白）";
  const rawNote = rawHasValue
    ? "源表原值，保留空白与异常字符；任何阶段结果都不回写这里。"
    : "源表原值为空白，按原样保留；不以预审建议或人工值替代。";

  el.innerHTML = `<div class="hs-compare" role="group" aria-label="编码阶段对照">
    <div class="hs-row hs-row-supplier" data-stage="supplier_hs_raw">
      <span class="hs-label">供应商原码</span>
      <span class="hs-value ${rawHasValue ? "" : "hs-value-empty"}">${escapeHtml(rawText)}</span>
      <span class="hs-source hs-source-static">源表原值</span>
      <span class="hs-note">${escapeHtml(rawNote)}</span>
    </div>
    ${STAGE_DEFS.map((def) => stageRowHtml(card, def)).join("")}
  </div>
  <p class="hs-rule">空值规则：阶段值为 <code>null</code> 但带 <code>source_id</code> → “未给出建议”；
    值与来源都为空 → “—”。页面不把空值替换成别的阶段值。</p>`;

  const handler = (event) => {
    const button = event.target.closest("[data-stage-source]");
    if (!button || !el.contains(button)) return;
    const stageKey = button.dataset.stageSource;
    const def = STAGE_DEFS.find((entry) => entry.key === stageKey);
    if (!def) return;
    const view = stageValueView(card[stageKey]);
    onSelectSource?.({
      stageKey,
      label: def.label,
      sourceKind: def.sourceKind,
      sourceId: view.sourceId,
      text: view.text,
      trigger: button,
    });
  };
  el.addEventListener("click", handler);
  el[CLEANUP] = () => el.removeEventListener("click", handler);
}
