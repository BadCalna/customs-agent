/* =============================================================================
 * web/components/commodity-card.js —— 商品卡片（纯渲染）
 *
 * 卡片只消费 D3 第 3 节的 ItemCard 字段：身份、源位置、事实、图片、五个编码
 * 阶段、比较结论、问题、证据与专家核查引用。它不发请求、不写存储、不改业务
 * 状态；选择来源、查看执行、点选附图都只以回调交给页面。
 *
 * 展示边界：
 * - 不出现审批结论词，task_stage 与 submission_status 按字段原值显示；
 * - result_mode=mock 必须可见；
 * - FIX-10 的原提交码与专家终值并列，并说明最近核查的目标码，不暗示新终值已核查。
 * ========================================================================== */

import {
  badgeHtml,
  comparisonStatusView,
  escapeHtml as esc,
  imageStatusView,
  renderHsComparison,
  shortId,
} from "./hs-comparison.js";

const CLEANUP = Symbol.for("p07.commodityCard.cleanup");

const EVIDENCE_STATUS = Object.freeze({
  obtained: { label: "已取得依据", tone: "jade" },
  not_found: { label: "未查得", tone: "gold" },
  query_failed: { label: "查询失败", tone: "cinnabar" },
  version_unverified: { label: "版本未核", tone: "gold" },
});

const VERDICT = Object.freeze({
  matched: { label: "相符", tone: "jade" },
  conflict: { label: "冲突", tone: "cinnabar" },
  unknown: { label: "未核实", tone: "gold" },
});

const ASPECT = Object.freeze({
  elements: "申报要素",
  images: "附图",
  tax: "税率",
  regulatory: "监管条件",
});

function safeToken(value) {
  return String(value ?? "card").replace(/[^a-zA-Z0-9_-]/g, "-").slice(0, 48) || "card";
}

function formatValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value === "" ? "（空字符串）" : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function factsSection(card) {
  const facts = card.facts || {};
  const attributes = Object.entries(facts.attributes || {});
  const raw = typeof facts.supplier_hs_raw === "string" ? facts.supplier_hs_raw : "";
  const rawHasValue = raw.trim() !== "";
  const elements = typeof facts.supplier_elements_raw === "string" ? facts.supplier_elements_raw : "";
  const tax = typeof facts.supplier_tax_rate_raw === "string" ? facts.supplier_tax_rate_raw : "";

  return `<section class="commodity-section" aria-labelledby="section-facts">
    <h3 id="section-facts" class="commodity-section-title">申报事实（源表原文，结果不回写）</h3>
    <dl class="commodity-facts">
      <dt>供应商原码</dt>
      <dd data-field="supplier_hs_raw" class="${rawHasValue ? "" : "is-empty"}">
        <code>${rawHasValue ? esc(raw) : "（原值为空白）"}</code>
        ${rawHasValue ? "" : `<span class="commodity-inline-note">按原样保留，不以建议值替代</span>`}
      </dd>
      <dt>申报要素</dt>
      <dd>${elements.trim() ? `<div class="commodity-long-text">${esc(elements)}</div>` : `<span class="commodity-inline-note">（原值为空）</span>`}</dd>
      <dt>供应商退税率</dt>
      <dd class="${tax.trim() ? "" : "is-empty"}">${tax.trim() ? esc(tax) : "（未提供）"}</dd>
      ${attributes.map(([key, value]) => `<dt title="${esc(key)}">${esc(key)}</dt>
        <dd class="commodity-fact-value">${esc(formatValue(value))}</dd>`).join("")}
    </dl>
  </section>`;
}

function recodeSection(card, contractNotes) {
  const check = card.latest_expert_check;
  const submitted = card.submitted_hs || {};
  const final = card.expert_final_hs || {};
  const submittedValue = submitted.value ? String(submitted.value) : "";
  const finalValue = final.value ? String(final.value) : "";
  const changed = Boolean(submittedValue && finalValue && submittedValue !== finalValue);
  if (!check && !changed) return "";

  const target = check && check.target_hs ? String(check.target_hs) : "";
  let scope;
  if (check && target && submittedValue && target === submittedValue) {
    scope = `最近一次专家核查（run <code>${esc(shortId(check.run_id, 12))}</code>）的
      <code>target_hs=${esc(target)}</code>，即原提交码。本次核查针对的是原提交码。`;
  } else if (check && target && submittedValue) {
    scope = `最近一次专家核查的 <code>target_hs=${esc(target)}</code> 与
      <code>submitted_hs=${esc(submittedValue)}</code> 不一致；页面如实并列，不推断核查覆盖范围。`;
  } else if (check) {
    scope = `最近一次专家核查的 <code>target_hs=${esc(target || "—")}</code>；卡片没有可对照的原提交码，
      页面不判断核查针对哪一版编码。`;
  } else {
    scope = `契约未返回 latest_expert_check，无法判断改码核查的目标；页面不推断。`;
  }

  const finalNote = changed
    ? (check
      ? `专家终值 <code>${esc(finalValue)}</code> 不属于上述核查目标；契约未提供针对新终值的核查记录，页面不推断它已被核查。`
      : `专家终值 <code>${esc(finalValue)}</code> 未在契约的 latest_expert_check 中体现；页面不推断它是否被核查。`)
    : `专家终值与提交值相同或未形成改码，页面不额外推断核查状态。`;

  const findings = (check && check.findings) || [];
  const findingsHtml = findings.length
    ? `<div class="commodity-findings">
        <h4>分项核查（目标 <code>${esc(target || "—")}</code>）</h4>
        <ul>${findings.map((finding) => {
          const verdict = VERDICT[finding.verdict] || { label: String(finding.verdict), tone: "idle" };
          return `<li>
            ${badgeHtml({ label: verdict.label, tone: verdict.tone, title: "分项结论；未核实不等于无风险" })}
            <span class="commodity-finding-aspect">${esc(ASPECT[finding.aspect] || finding.aspect)}</span>
            <span class="commodity-finding-reason">${esc(finding.reason || "未给出说明")}</span>
          </li>`;
        }).join("")}</ul>
        <p class="commodity-hint">分项结论为“未核实”时只表示未取得核对结果，不等于无风险。</p>
      </div>`
    : "";

  return `<section class="commodity-section commodity-recode" aria-labelledby="section-recode">
    <h3 id="section-recode" class="commodity-section-title">专家改码对照
      ${changed ? `<span class="commodity-recode-flag">原提交码 ≠ 专家终值</span>` : ""}
    </h3>
    <div class="commodity-recode-pair">
      <div class="commodity-recode-box">
        <span class="commodity-recode-label">原提交码（submitted_hs）</span>
        <code class="commodity-recode-value">${esc(submittedValue || "—")}</code>
        <span class="commodity-recode-src">${submitted.source_id ? `来源 ↳ ${esc(shortId(submitted.source_id, 12))}` : "无来源引用"}</span>
      </div>
      <div class="commodity-recode-arrow" aria-hidden="true">→</div>
      <div class="commodity-recode-box is-final">
        <span class="commodity-recode-label">专家终值（expert_final_hs）</span>
        <code class="commodity-recode-value">${esc(finalValue || "—")}</code>
        <span class="commodity-recode-src">${final.source_id ? `来源 ↳ ${esc(shortId(final.source_id, 12))}` : "无来源引用"}</span>
      </div>
    </div>
    <p class="commodity-recode-scope" role="note"><b>核查范围：</b>${scope}</p>
    <p class="commodity-recode-scope is-secondary" role="note">${finalNote}</p>
    ${contractNotes
      ? `<p class="commodity-recode-reason">改码原因：ItemCard v1 未提供该字段，卡片不推断；已在交接 P07 提交契约申请。</p>`
      : ""}
    ${findingsHtml}
  </section>`;
}

function imageTileHtml(image, index) {
  const status = imageStatusView(image.status);
  const url = image.download_url ? String(image.download_url) : "";
  const label = `附图 ${index + 1}，${status.label}，来源 ${image.source_sheet || "—"} ${image.source_cell || "—"}`;
  let thumb;
  if (url) {
    thumb = `<span class="commodity-image-thumb"><img src="${esc(url)}" alt="${esc(label)}" loading="lazy"></span>`;
  } else if (image.status === "read_failed") {
    thumb = `<span class="commodity-image-thumb is-failed">读取失败</span>`;
  } else if (image.status === "absent") {
    thumb = `<span class="commodity-image-thumb is-absent">无图</span>`;
  } else {
    thumb = `<span class="commodity-image-thumb">下载未提供</span>`;
  }
  const detail = image.status === "read_failed"
    ? `<span class="commodity-image-error">${esc(image.error_message || "图片存在但读取失败；不能当作无图")}</span>`
    : image.status === "absent"
      ? `<span class="commodity-image-note">该条明细为无图；与读取失败不同</span>`
      : `<span class="commodity-image-note">${url ? "" : "受控下载由 P05 提供，当前 download_url=null"}</span>`;
  return `<button type="button" class="commodity-image-tile ${image.status === "read_failed" ? "is-failed" : ""} ${image.status === "absent" ? "is-absent" : ""}"
      data-image-id="${esc(image.image_id)}"
      aria-label="${esc(label)}"
      title="${esc(`image_id=${image.image_id}${image.dispimg_id ? ` · ${image.dispimg_id}` : ""}`)}">
    ${thumb}
    <span class="commodity-image-status">${esc(status.label)} <code>${esc(image.status || "—")}</code></span>
    <span class="commodity-image-loc">${esc(image.source_sheet || "—")} · ${esc(image.source_cell || "—")}</span>
    ${detail}
  </button>`;
}

function imagesSection(card) {
  const overall = imageStatusView(card.image_status);
  const images = Array.isArray(card.images) ? card.images : [];
  let empty = "";
  if (!images.length) {
    if (card.image_status === "absent") {
      empty = `<p class="commodity-image-empty" data-field="image_status">无附图：源表没有图片（<code>image_status=absent</code>），不是读取失败。</p>`;
    } else if (card.image_status === "read_failed") {
      empty = `<p class="commodity-image-empty is-failed" data-field="image_status"><code>image_status=read_failed</code>，但 <code>images</code> 明细为空；按字段呈现，不推断为无图。</p>`;
    } else if (card.image_status === "available") {
      empty = `<p class="commodity-image-empty"><code>image_status=available</code>，但 <code>images</code> 明细为空；无法给出源单元格。</p>`;
    } else {
      empty = `<p class="commodity-image-empty">契约未提供图片状态。</p>`;
    }
  }
  return `<section class="commodity-section" aria-labelledby="section-images">
    <h3 id="section-images" class="commodity-section-title">附图
      ${badgeHtml({ label: overall.label, tone: overall.tone, title: overall.hint })}
      <code class="commodity-section-code">image_status=${esc(card.image_status || "—")}</code>
    </h3>
    <p class="commodity-hint">${esc(overall.hint)}</p>
    ${empty}
    ${images.length ? `<div class="commodity-images" role="list" aria-label="商品附图（${images.length}）">
      ${images.map((image, index) => `<div role="listitem">${imageTileHtml(image, index)}</div>`).join("")}
    </div>` : ""}
  </section>`;
}

function issuesSection(card) {
  const issues = Array.isArray(card.issues) ? card.issues : [];
  if (!issues.length) {
    return `<section class="commodity-section" aria-labelledby="section-issues">
      <h3 id="section-issues" class="commodity-section-title">问题（0）</h3>
      <p class="commodity-hint"><code>issues</code> 为空只表示契约未返回问题，不代表没有业务问题。</p>
    </section>`;
  }
  return `<section class="commodity-section" aria-labelledby="section-issues">
    <h3 id="section-issues" class="commodity-section-title">问题（${issues.length}）</h3>
    <div class="commodity-issues">
      ${issues.map((issue) => `<div class="commodity-issue">
        <div class="commodity-issue-head"><code>${esc(issue.code)}</code><span>${esc(issue.message)}</span></div>
        <div class="commodity-issue-meta">
          ${issue.field ? `<span>字段 <code>${esc(issue.field)}</code></span>` : ""}
          ${issue.missing ? `<span>缺少 ${esc(issue.missing)}</span>` : ""}
          ${issue.suggested_role ? `<span>建议由 ${esc(issue.suggested_role)} 补充</span>` : ""}
        </div>
      </div>`).join("")}
    </div>
  </section>`;
}

function evidenceDetails(card) {
  const entries = Array.isArray(card.evidence) ? card.evidence : [];
  if (!entries.length) return `<p class="commodity-hint">依据明细为空。</p>`;
  return `<details class="commodity-evidence">
    <summary>依据明细（${entries.length}）</summary>
    <ul>${entries.map((entry) => {
      const status = EVIDENCE_STATUS[entry.status] || { label: entry.status || "—", tone: "idle" };
      const detail = [entry.document, entry.locator, entry.note].filter(Boolean).map(String).join(" · ");
      return `<li>
        ${badgeHtml({ label: status.label, tone: status.tone })}
        <code class="commodity-evidence-type">${esc(entry.source_type || "—")}</code>
        <span>${esc(entry.title || "（未命名依据）")}</span>
        ${detail ? `<span class="commodity-evidence-detail">${esc(detail)}</span>` : ""}
      </li>`;
    }).join("")}</ul>
  </details>`;
}

function resultsSection(card, onOpenRun) {
  const evidence = EVIDENCE_STATUS[card.evidence_status] || {
    label: card.evidence_status || "未产生",
    tone: "idle",
  };
  const confidence = card.confidence === null || card.confidence === undefined
    ? "置信度 —（未给出）"
    : `置信度 ${card.confidence}`;
  const run = card.latest_run_id
    ? (onOpenRun
      ? `<button type="button" class="commodity-run-link" data-run-id="${esc(card.latest_run_id)}"
          aria-label="查看最近执行的引用 ${esc(shortId(card.latest_run_id, 12))}">执行 ↳ <code>${esc(shortId(card.latest_run_id, 12))}</code></button>`
      : `<span class="commodity-metric">最近执行 <code>${esc(shortId(card.latest_run_id, 12))}</code></span>`)
    : `<span class="commodity-metric">无执行引用</span>`;
  return `<section class="commodity-section" aria-labelledby="section-results">
    <h3 id="section-results" class="commodity-section-title">结果与依据</h3>
    <div class="commodity-metrics">
      ${badgeHtml({ label: `证据：${evidence.label}`, tone: evidence.tone })}
      ${badgeHtml({ label: confidence, tone: card.confidence === null || card.confidence === undefined ? "idle" : "teal" })}
      ${run}
    </div>
    <p class="commodity-hint">
      ${card.result_mode === "mock"
        ? "置信度是 mock 固定值，未经校准，不是准确率；证据为合成样例。"
        : "置信度来自当前结果集；未经业务确认前不作为申报依据。"}
    </p>
    ${evidenceDetails(card)}
  </section>`;
}

function footerSection(card) {
  const actions = Array.isArray(card.allowed_actions) ? card.allowed_actions : [];
  return `<footer class="commodity-footer">
    <span>allowed_actions（只展示，服务端每次重新校验）</span>
    <code>${actions.length ? actions.map(esc).join(" · ") : "—"}</code>
    <span class="commodity-refs">task_id=<code>${esc(shortId(card.task_id, 12))}</code>
      · version_id=<code>${esc(shortId(card.version_id, 12))}</code></span>
  </footer>`;
}

/**
 * 渲染完整商品卡片。
 * @param {HTMLElement} el
 * @param {{
 *   card?: object|null,
 *   onSelectSource?: (source: object) => void,
 *   onSelectImage?: (image: object) => void,
 *   onOpenRun?: (runId: string) => void,
 *   contractNotes?: boolean
 * }} options
 */
export function renderCommodityCard(el, options = {}) {
  if (!el) return;
  if (el[CLEANUP]) {
    el[CLEANUP]();
    el[CLEANUP] = null;
  }

  const { card = null, onSelectSource, onSelectImage, onOpenRun, contractNotes = false } = options;
  if (!card) {
    el.innerHTML = `<div class="commodity-empty" role="status">
      <b>选择一件商品</b>
      <span>左侧列表选中后，这里显示事实、五阶段编码、图片状态、问题与专家核查引用。</span>
    </div>`;
    return;
  }

  const facts = card.facts || {};
  const name = facts.name || "（未命名商品）";
  const location = card.source_location || {};
  const token = safeToken(card.item_id);
  const titleId = `commodity-title-${token}`;
  const comparison = comparisonStatusView(card);
  const image = imageStatusView(card.image_status);
  const isMock = card.result_mode === "mock";
  const modeBadge = badgeHtml({
    label: isMock ? "MOCK 结果" : (card.result_mode === "live" ? "LIVE 结果" : `result_mode=${card.result_mode || "—"}`),
    tone: isMock ? "gold" : (card.result_mode === "live" ? "jade" : "idle"),
    title: isMock ? "固定样例/模拟引擎输出，不是真实归类结论" : "结果模式来自契约字段 result_mode",
  });
  const taskStage = card.task_stage === null || card.task_stage === undefined ? "—" : String(card.task_stage);
  const submissionStatus = card.submission_status === null || card.submission_status === undefined
    ? "—"
    : String(card.submission_status);

  el.innerHTML = `<article class="commodity-card" role="article" aria-labelledby="${titleId}" data-item-id="${esc(card.item_id)}">
    ${isMock ? `<div class="commodity-mock-banner" role="note">
      <b>MOCK 结果</b>
      <span>固定样例/模拟引擎输出，不是真实归类结论，不构成申报依据。</span>
    </div>` : ""}
    <header class="commodity-head">
      <div class="commodity-head-main">
        <h2 id="${titleId}" class="commodity-title" title="${esc(name)}">${esc(name)}</h2>
        <p class="commodity-loc">
          ${esc(location.sheet_name || "—")} · 第 ${esc(location.row_number ?? "—")} 行
          · <code>${esc(location.cells || "—")}</code> · rev ${esc(card.revision ?? "—")}
        </p>
        <div class="commodity-field-row">
          <span class="commodity-field"><span>task_stage</span><code>${esc(taskStage)}</code></span>
          <span class="commodity-field"><span>submission_status</span><code>${esc(submissionStatus)}</code></span>
          <span class="commodity-field"><span>result_mode</span><code>${esc(card.result_mode || "—")}</code></span>
        </div>
        <p class="commodity-hint">阶段与提交状态按字段原值显示，页面不翻译成审批结论。</p>
      </div>
      <div class="commodity-badges">
        ${modeBadge}
        ${badgeHtml({ label: comparison.label, tone: comparison.tone, title: comparison.hint })}
        ${badgeHtml({ label: image.label, tone: image.tone, title: image.hint })}
      </div>
    </header>
    ${factsSection(card)}
    <section class="commodity-section" aria-labelledby="section-stages">
      <h3 id="section-stages" class="commodity-section-title">编码阶段对照
        <span class="commodity-section-code">五阶段 + 供应商原码</span>
      </h3>
      <div data-role="hs-comparison"></div>
    </section>
    ${recodeSection(card, contractNotes)}
    ${issuesSection(card)}
    ${imagesSection(card)}
    ${resultsSection(card, onOpenRun)}
    ${footerSection(card)}
  </article>`;

  renderHsComparison(el.querySelector('[data-role="hs-comparison"]'), { card, onSelectSource });

  const handler = (event) => {
    const imageButton = event.target.closest("[data-image-id]");
    if (imageButton && el.contains(imageButton)) {
      const imageId = imageButton.dataset.imageId;
      const selected = (card.images || []).find((entry) => entry.image_id === imageId);
      if (selected) onSelectImage?.({ ...selected });
      return;
    }
    const runButton = event.target.closest("[data-run-id]");
    if (runButton && el.contains(runButton)) {
      onOpenRun?.(runButton.dataset.runId);
    }
  };
  el.addEventListener("click", handler);
  el[CLEANUP] = () => el.removeEventListener("click", handler);
}
