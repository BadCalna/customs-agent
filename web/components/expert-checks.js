/* =============================================================================
 * web/components/expert-checks.js —— 专家 AI 分项核查展示组件（纯渲染）
 *
 * 只读 D3/契约 v1 的 ExpertCheckRef（run_id, submission_id, target_hs, findings），
 * 渲染核查目标编码 target_hs 与四行分项（要素/附图/退税率/监管条件）的
 * verdict、中文理由、证据条数与可展开定位。
 *
 * 边界约束：
 * - 不发网络请求、不读写浏览器存储、不修改业务状态；
 * - 缺失分项或 unknown 绝不视为相符或无风险；
 * - 不生成审批结论或退回接收人，不推断改码后的新终值已被核查。
 * ========================================================================== */

import { badgeHtml, escapeHtml as esc, shortId } from "./hs-comparison.js";

const CLEANUP = Symbol.for("p18.expertChecks.cleanup");

export const EXPERT_ASPECTS = Object.freeze([
  { key: "elements", label: "申报要素" },
  { key: "images", label: "附图" },
  { key: "tax", label: "退税率" },
  { key: "regulatory", label: "监管条件" },
]);

export const EXPERT_VERDICTS = Object.freeze({
  matched: { label: "相符", tone: "jade", hint: "分项核对相符（仅作参考，不替代人工审批）" },
  conflict: { label: "冲突", tone: "cinnabar", hint: "分项核对存在明确冲突，需专家人工处置" },
  unknown: { label: "未核实", tone: "gold", hint: "未核实不等于无风险，也不计入相符" },
});

const EVIDENCE_STATUS = Object.freeze({
  obtained: { label: "已取得依据", tone: "jade" },
  not_found: { label: "未查得", tone: "gold" },
  query_failed: { label: "查询失败", tone: "cinnabar" },
  version_unverified: { label: "版本未核", tone: "gold" },
});

function nonEmptyText(value) {
  if (value === null || value === undefined) return "";
  const text = String(value).trim();
  return text;
}

function buildEvidenceIndex(evidence) {
  const index = new Map();
  if (!evidence) return index;
  if (Array.isArray(evidence)) {
    for (const entry of evidence) {
      if (entry && nonEmptyText(entry.evidence_id)) {
        index.set(String(entry.evidence_id), entry);
      }
    }
    return index;
  }
  if (typeof evidence === "object") {
    for (const [key, entry] of Object.entries(evidence)) {
      if (entry && typeof entry === "object") {
        const id = nonEmptyText(entry.evidence_id) || key;
        if (id) index.set(id, entry);
      }
    }
  }
  return index;
}

function verdictMeta(verdict) {
  const key = nonEmptyText(verdict);
  if (key && Object.prototype.hasOwnProperty.call(EXPERT_VERDICTS, key)) {
    return { key, ...EXPERT_VERDICTS[key] };
  }
  return {
    key: key || "unknown",
    label: key ? String(key) : "未核实",
    tone: "idle",
    hint: "未知分项结论，按未核实风险对待，不计入相符",
  };
}

function scopeNoticeHtml({ targetHs, submittedHs, expertFinalHs, runId, submissionId }) {
  const parts = [];
  if (targetHs && submittedHs && targetHs === submittedHs) {
    parts.push(
      `核查目标 <code>target_hs=${esc(targetHs)}</code> 与原提交码一致，本次分项结论针对原提交码。`
    );
  } else if (targetHs && submittedHs) {
    parts.push(
      `核查目标 <code>target_hs=${esc(targetHs)}</code> 与当前提交码 <code>${esc(submittedHs)}</code> 不一致；页面如实并列，不扩大核查覆盖范围。`
    );
  } else if (targetHs) {
    parts.push(`本次专家核查针对提交码 <code>target_hs=${esc(targetHs)}</code>。`);
  } else {
    parts.push(`核查记录未标注 <code>target_hs</code>，页面不推断其针对哪一版编码。`);
  }

  if (expertFinalHs && targetHs && expertFinalHs !== targetHs) {
    parts.push(
      `专家终值 <code>${esc(expertFinalHs)}</code> 与核查目标 <code>${esc(targetHs)}</code> 不同；上述分项结论不覆盖改码后的新终值。`
    );
  }

  const meta = [
    `run=<code>${esc(shortId(runId, 12))}</code>`,
    `submission=<code>${esc(shortId(submissionId, 12))}</code>`,
  ].join(" · ");

  return `<div class="expert-checks-scope" role="note">
    <p>${parts.join(" ")}</p>
    <p class="expert-checks-meta">${meta}</p>
  </div>`;
}

function renderEvidenceItem(eid, entry, runId, hasCallback) {
  const safeId = esc(eid);
  const trigger = hasCallback
    ? `<button type="button" class="expert-check-ev-btn" data-evidence-id="${safeId}">定位详情</button>`
    : "";
  if (!entry) {
    return `<li class="expert-check-ev-item" data-evidence-ref="${safeId}">
      <div class="expert-check-ev-head">
        <code>${safeId}</code>
        ${trigger}
      </div>
      <span class="expert-check-ev-loc">引用自 ExpertCheckRef.evidence_ids；完整 document / locator 随执行明细 <code>${esc(shortId(runId, 12))}</code> 返回。</span>
    </li>`;
  }
  const evStatus = EVIDENCE_STATUS[entry.status] || { label: entry.status || "—", tone: "idle" };
  const doc = nonEmptyText(entry.document);
  const loc = nonEmptyText(entry.locator);
  const locatorLine = doc && loc ? `${doc} · ${loc}` : doc || loc || "未提供定位字段";
  const windowParts = [entry.effective_from, entry.effective_to].filter(Boolean).map(String);
  const windowText = windowParts.length ? `有效期 ${windowParts.join(" ~ ")}` : "";
  const extra = [entry.retrieved_at ? `取得 ${entry.retrieved_at}` : "", windowText, entry.note]
    .filter(Boolean)
    .map(String)
    .join(" · ");
  return `<li class="expert-check-ev-item" data-evidence-ref="${safeId}">
    <div class="expert-check-ev-head">
      ${badgeHtml({ label: evStatus.label, tone: evStatus.tone })}
      <code class="expert-check-ev-type">${esc(entry.source_type || "—")}</code>
      <span class="expert-check-ev-title">${esc(entry.title || safeId)}</span>
      <code>${safeId}</code>
      ${trigger}
    </div>
    <div class="expert-check-ev-loc">定位：${esc(locatorLine)}</div>
    ${extra ? `<div class="expert-check-ev-extra">${esc(extra)}</div>` : ""}
  </li>`;
}

function normalizeRows(findings) {
  const rows = Array.isArray(findings) ? findings : [];
  const byAspect = new Map();
  const extras = [];
  for (const item of rows) {
    if (!item || typeof item !== "object") continue;
    const aspect = nonEmptyText(item.aspect);
    if (!aspect) continue;
    const isKnown = EXPERT_ASPECTS.some((def) => def.key === aspect);
    if (isKnown && !byAspect.has(aspect)) {
      byAspect.set(aspect, item);
    } else {
      extras.push(item);
    }
  }
  const ordered = EXPERT_ASPECTS.map((def) => {
    const found = byAspect.get(def.key);
    if (found) {
      return { aspect: def.key, label: def.label, finding: found, synthesizedMissing: false };
    }
    return {
      aspect: def.key,
      label: def.label,
      finding: {
        aspect: def.key,
        verdict: "unknown",
        reason: "契约未返回该分项核查结论，保持未核实（不视为相符或无风险）",
        evidence_ids: [],
      },
      synthesizedMissing: true,
    };
  });
  for (const extra of extras) {
    ordered.push({
      aspect: extra.aspect,
      label: String(extra.aspect),
      finding: extra,
      synthesizedMissing: false,
    });
  }
  return ordered;
}

function renderFindingRow({ aspect, label, finding, synthesizedMissing }, { evidenceIndex, runId, hasEvidenceCallback }) {
  const v = verdictMeta(finding.verdict);
  const reason = nonEmptyText(finding.reason) || "未给出核查理由说明";
  const rawIds = Array.isArray(finding.evidence_ids) ? finding.evidence_ids.map(String).filter(Boolean) : [];
  const evidenceIds = [...new Set(rawIds)];
  const count = evidenceIds.length;
  const detailsBody = count
    ? `<ul class="expert-check-ev-list">${evidenceIds
        .map((eid) => renderEvidenceItem(eid, evidenceIndex.get(eid), runId, hasEvidenceCallback))
        .join("")}</ul>`
    : `<p class="expert-check-ev-empty">本分项未附可定位依据（查无、未配置或未提供时不生成虚构条款）。</p>`;

  return `<li class="expert-check-row is-${esc(v.key)}${synthesizedMissing ? " is-missing-aspect" : ""}" data-aspect="${esc(aspect)}" data-verdict="${esc(v.key)}">
    <div class="expert-check-main">
      <span class="expert-check-aspect">${esc(label)}</span>
      ${badgeHtml({ label: v.label, tone: v.tone, title: v.hint })}
      <span class="expert-check-count" data-role="evidence-count">证据 ${count} 条</span>
    </div>
    <p class="expert-check-reason">${esc(reason)}</p>
    <details class="expert-check-evidence">
      <summary>${count ? `展开定位（${count} 条依据）` : "展开定位（0 条依据）"}</summary>
      ${detailsBody}
    </details>
  </li>`;
}

/**
 * 纯渲染 ExpertCheckRef（target_hs + 四行分项：verdict / 中文理由 / 证据条数 / 可展开定位）。
 * @param {HTMLElement} el
 * @param {{
 *   expertCheck?: object|null,
 *   check?: object|null,
 *   submittedHs?: string|null,
 *   expertFinalHs?: string|null,
 *   evidence?: Array<object>|object|null,
 *   resultMode?: string|null,
 *   onOpenRun?: (runId: string) => void,
 *   onSelectEvidence?: (payload: object) => void,
 * }} options
 */
export function renderExpertChecks(el, options = {}) {
  if (!el) return;
  if (el[CLEANUP]) {
    el[CLEANUP]();
    el[CLEANUP] = null;
  }

  const check = options.expertCheck ?? options.check ?? null;
  if (!check || typeof check !== "object") {
    el.innerHTML = `<section class="expert-checks expert-checks-empty" role="status" aria-label="专家分项核查">
      <b>暂无专家分项核查记录</b>
      <span>契约未返回 latest_expert_check；页面不推断提交码是否核清。</span>
    </section>`;
    return;
  }

  const runId = nonEmptyText(check.run_id);
  const submissionId = nonEmptyText(check.submission_id);
  const targetHs = nonEmptyText(check.target_hs);
  const submittedHs = nonEmptyText(options.submittedHs);
  const expertFinalHs = nonEmptyText(options.expertFinalHs);
  const evidenceIndex = buildEvidenceIndex(options.evidence);
  const rows = normalizeRows(check.findings);
  const { onOpenRun, onSelectEvidence, resultMode } = options;

  const runAction = runId && onOpenRun
    ? `<button type="button" class="expert-checks-run-btn" data-run-id="${esc(runId)}">查看执行 <code>${esc(shortId(runId, 12))}</code></button>`
    : "";
  const modeBadge = resultMode === "mock"
    ? badgeHtml({ label: "MOCK 核查", tone: "gold", title: "模拟引擎或合成样本输出，不构成真实依据" })
    : "";

  el.innerHTML = `<section class="expert-checks" aria-label="专家分项核查" data-target-hs="${esc(targetHs)}">
    <header class="expert-checks-head">
      <div class="expert-checks-title-row">
        <h3>专家分项核查</h3>
        <code class="expert-checks-target" data-role="target-hs">${esc(targetHs || "—")}</code>
        ${modeBadge}
      </div>
      ${runAction}
    </header>
    ${scopeNoticeHtml({ targetHs, submittedHs, expertFinalHs, runId, submissionId })}
    <ul class="expert-checks-list">
      ${rows.map((row) => renderFindingRow(row, {
        evidenceIndex,
        runId,
        hasEvidenceCallback: typeof onSelectEvidence === "function",
      })).join("")}
    </ul>
    <p class="expert-checks-footnote">口径说明：分项结论为“未核实”只表示未取得可用核对结果，不计入相符、不构成无风险；AI 分项意见不替代人工审批或退回决定。</p>
  </section>`;

  const handler = (event) => {
    const runBtn = event.target.closest("[data-run-id]");
    if (runBtn && el.contains(runBtn)) {
      onOpenRun?.(runBtn.dataset.runId);
      return;
    }
    const evBtn = event.target.closest("[data-evidence-id]");
    if (evBtn && el.contains(evBtn)) {
      const evidenceId = evBtn.dataset.evidenceId;
      const entry = evidenceIndex.get(evidenceId) || null;
      const rowNode = evBtn.closest("[data-aspect]");
      onSelectEvidence?.({
        evidenceId,
        aspect: rowNode?.dataset.aspect || "",
        runId,
        submissionId,
        targetHs,
        entry: entry ? { ...entry } : null,
      });
    }
  };
  el.addEventListener("click", handler);
  el[CLEANUP] = () => el.removeEventListener("click", handler);
}
