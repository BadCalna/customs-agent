/* =============================================================================
 * web/components/commodity-list.js —— 商品列表（纯渲染）
 *
 * 只读 ItemListEntry（也兼容传入完整的 ItemCard）：
 * - 行内展示源行号、品名、供应商原码、预审建议、比较结论、图片状态与结果模式；
 * - 选中态由 aria-selected + 视觉样式表达，点击只回调 onSelectItem；
 * - 键盘：Tab 进入行，Enter/Space 选中，↑/↓ 移动行焦点，Esc 交给页面处理；
 * - 本文件不发请求、不读写存储、不改业务状态。
 * ========================================================================== */

import {
  badgeHtml,
  comparisonStatusView,
  escapeHtml as esc,
  imageStatusView,
} from "./hs-comparison.js";

const CLEANUP = Symbol.for("p07.commodityList.cleanup");

function stageText(value) {
  if (value !== null && typeof value === "object") return value.value;
  return value;
}

function valueText(value, emptyText = "—") {
  if (value === null || value === undefined) return emptyText;
  const text = String(value);
  return text.trim() === "" ? emptyText : text;
}

/**
 * 兼容 ItemListEntry 与 ItemCard 的字段位置，统一成列表视图模型。
 * 不补造状态：缺字段按空值显示。
 */
export function listItemView(item) {
  const facts = (item && item.facts) || {};
  const name = item && item.name !== undefined ? item.name : facts.name;
  const supplierRaw = item && item.supplier_hs_raw !== undefined
    ? item.supplier_hs_raw
    : facts.supplier_hs_raw;
  const precheck = stageText(item && item.precheck_suggested_hs);
  const final = stageText(item && item.expert_final_hs);
  return {
    itemId: item && item.item_id ? String(item.item_id) : "",
    name: valueText(name, "（未命名商品）"),
    supplierRaw: valueText(supplierRaw, ""),
    precheck: valueText(precheck, "—"),
    expertFinal: valueText(final, "—"),
    rowNumber: item && item.source_location ? item.source_location.row_number : null,
    sheetName: item && item.source_location ? item.source_location.sheet_name : "",
    revision: item ? item.revision : null,
    resultMode: item ? item.result_mode : null,
    comparison: comparisonStatusView(item),
    image: imageStatusView(item && item.image_status),
  };
}

function annotationOf(annotations, itemId) {
  if (!annotations) return null;
  const value = annotations instanceof Map ? annotations.get(itemId) : annotations[itemId];
  if (!value) return null;
  if (typeof value === "string") return { label: value, note: "" };
  return value;
}

function rowHtml(item, selectedItemId, annotations) {
  const view = listItemView(item);
  const selected = view.itemId === selectedItemId;
  const annotation = annotationOf(annotations, view.itemId);
  const rawText = view.supplierRaw || "（空白）";
  const aria = [
    `第 ${view.rowNumber ?? "—"} 行`,
    view.name,
    `供应商原码 ${view.supplierRaw || "空白"}`,
    `预审建议 ${view.precheck}`,
    `专家终值 ${view.expertFinal}`,
    `比较结论 ${view.comparison.label}`,
    `图片 ${view.image.label}`,
    view.resultMode === "mock" ? "MOCK 结果" : `结果模式 ${view.resultMode || "—"}`,
  ].join("；");

  return `<button type="button" class="commodity-row" role="option"
      data-item-id="${esc(view.itemId)}"
      aria-selected="${selected ? "true" : "false"}"
      aria-label="${esc(aria)}"
      title="${esc(`${view.name}｜原码 ${rawText}｜预审 ${view.precheck}｜${view.comparison.hint}`)}">
    <span class="commodity-row-no">${esc(view.rowNumber ?? "—")}</span>
    <span class="commodity-row-main">
      <span class="commodity-row-name">${esc(view.name)}</span>
      <span class="commodity-row-meta">
        ${annotation && annotation.label ? `<span class="commodity-row-tag">${esc(annotation.label)}</span>` : ""}
        <span>${esc(view.sheetName || "—")} · 第 ${esc(view.rowNumber ?? "—")} 行 · rev ${esc(view.revision ?? "—")}</span>
        <span>专家终值 <code>${esc(view.expertFinal)}</code></span>
        ${view.resultMode === "mock" ? badgeHtml({ label: "MOCK", tone: "gold", title: "该行结果来自 mock，不是真实归类结论" }) : ""}
      </span>
    </span>
    <span class="commodity-row-code">
      <span class="commodity-row-code-label">原码</span>
      <code class="${view.supplierRaw ? "" : "is-empty"}">${esc(rawText)}</code>
    </span>
    <span class="commodity-row-code is-precheck">
      <span class="commodity-row-code-label">预审</span>
      <code class="${view.precheck === "—" ? "is-empty" : ""}">${esc(view.precheck)}</code>
    </span>
    <span class="commodity-row-badges">
      ${badgeHtml({ label: view.comparison.label, tone: view.comparison.tone, title: view.comparison.hint })}
      ${badgeHtml({ label: view.image.label, tone: view.image.tone, title: view.image.hint })}
    </span>
  </button>`;
}

/**
 * 渲染商品列表。
 * @param {HTMLElement} el
 * @param {{
 *   items?: object[],
 *   selectedItemId?: string|null,
 *   onSelectItem?: (itemId: string, item: object) => void,
 *   onEscape?: () => void,
 *   annotations?: Map<string, {label: string, note?: string}>|Object,
 *   ariaLabel?: string,
 *   emptyTitle?: string,
 *   emptyHint?: string
 * }} options
 */
export function renderCommodityList(el, options = {}) {
  if (!el) return;
  if (el[CLEANUP]) {
    el[CLEANUP]();
    el[CLEANUP] = null;
  }

  const {
    items = [],
    selectedItemId = null,
    onSelectItem,
    onEscape,
    annotations = null,
    ariaLabel = "商品列表",
    emptyTitle = "没有商品行",
    emptyHint = "当前版本没有可显示的商品，或尚未加载。",
  } = options;

  const active = document.activeElement;
  const hadFocus = Boolean(active && el.contains(active));
  const activeId = hadFocus && active.closest("[data-item-id]")
    ? active.closest("[data-item-id]").dataset.itemId
    : null;

  if (!items.length) {
    el.innerHTML = `<div class="commodity-list-empty" role="status">
      <b>${esc(emptyTitle)}</b>
      <span>${esc(emptyHint)}</span>
    </div>`;
    return;
  }

  el.innerHTML = `<div class="commodity-list" role="listbox" aria-label="${esc(ariaLabel)}">
    ${items.map((item) => rowHtml(item, selectedItemId, annotations)).join("")}
  </div>`;

  const handler = (event) => {
    const row = event.target.closest("[data-item-id]");
    if (!row || !el.contains(row)) return;
    const itemId = row.dataset.itemId;
    const item = items.find((entry) => String(entry.item_id) === itemId);
    if (item) onSelectItem?.(itemId, item);
  };
  el.addEventListener("click", handler);

  const keyHandler = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onEscape?.();
      return;
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const rows = Array.from(el.querySelectorAll(".commodity-row"));
    const index = rows.indexOf(document.activeElement);
    if (index === -1) return;
    event.preventDefault();
    const delta = event.key === "ArrowDown" ? 1 : -1;
    rows[(index + delta + rows.length) % rows.length]?.focus();
  };
  el.addEventListener("keydown", keyHandler);

  el[CLEANUP] = () => {
    el.removeEventListener("click", handler);
    el.removeEventListener("keydown", keyHandler);
  };

  // 重渲染不丢键盘焦点：优先回到原行，其次回到选中行。
  if (hadFocus) {
    const rows = Array.from(el.querySelectorAll(".commodity-row"));
    const target = rows.find((row) => row.dataset.itemId === activeId)
      || rows.find((row) => row.getAttribute("aria-selected") === "true");
    target?.focus();
  }
}

/** 页面在 Esc 清除选中后，可把焦点交回列表第一行。 */
export function focusFirstCommodity(el) {
  el?.querySelector(".commodity-row")?.focus();
}
