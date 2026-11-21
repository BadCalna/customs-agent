// Read-only projection of TaskDetail, ImportDetail and ALL ItemPage rows for one version.
// Never derive a comparison or an approval from the HS strings themselves.
const stages = [
  ["precheck_suggested_hs", "预审建议"], ["business_confirmed_hs", "业务确认"],
  ["submitted_hs", "提交留档"], ["expert_final_hs", "专家终值"],
];
const comparison = new Map([
  ["association_error", ["关联异常", "alert"]], ["supplier_invalid", ["原码异常", "alert"]],
  ["supplier_missing", ["原码缺失", "attention"]], ["inconsistent", ["编码不一致", "attention"]],
  ["needs_confirmation", ["待核对", "attention"]], ["consistent", ["编码一致", "normal"]],
]);
const none = ["暂未有结论", "neutral"], unknown = ["结论未识别", "neutral"], absent = ["结论字段未提供", "neutral"];
const text = value => typeof value === "string" && value.trim() !== "";
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const matches = (entry, taskId, versionId) => entry?.task_id === taskId && entry.version_id === versionId;

export function summarizeReview({task, versionId, items, loading = false, error = null, importJob, run} = {}) {
  if (!task || !versionId) return {state: "hidden"};
  const version = Array.isArray(task.versions) && task.versions.find(v => v?.version_id === versionId);
  const scope = version ? {
    version: Number.isSafeInteger(version.number) && version.number > 0 ? `V${version.number}` : versionId,
    kind: !task.current_version_id ? "选定版本" : task.current_version_id === versionId ? "当前版本" : "历史版本",
    file: text(version.original_filename) ? version.original_filename : "",
  } : {version: "", kind: "选定版本", file: ""};
  const stop = (state, message) => ({state, scope, message});
  if (error) return stop("unavailable", "未能取得完整明细。请刷新任务后重试。");
  if (loading) return stop("loading", "正在读取所选版本的完整明细…");
  if (!version || !text(task.task_id)) return stop("unavailable", "无法核实版本归属，未显示统计。");
  if ([importJob, run].some(obj => obj != null && !matches(obj, task.task_id, versionId)))
    return stop("unavailable", "版本来源不匹配，未显示统计。请刷新核对。");
  if (importJob?.parse_status === "staged") return stop("waiting", "单证尚在解析，商品数量和比较结果待解析后统计。");
  if (importJob?.parse_status === "failed") return stop("unavailable", "单证解析失败，无法统计。请先核对上方的解析信息。");
  if (!Array.isArray(items)) return stop("unavailable", "商品明细未提供，无法统计。");
  const total = items.length, ids = new Set();
  for (const item of items) {
    if (!matches(item, task.task_id, versionId) || !text(item.item_id) || ids.has(item.item_id))
      return stop("unavailable", "明细归属或唯一性无法核实，未显示统计。请刷新核对。");
    ids.add(item.item_id);
  }
  if (importJob?.parse_status === "succeeded" && Number.isSafeInteger(importJob.item_count) && importJob.item_count !== total)
    return stop("unavailable", "解析回执和明细条数不一致，未显示统计。请刷新核对。");
  if (!total) return stop("empty", importJob?.parse_status === "superseded" ? "该版本的解析已被替代，没有可供统计的明细。" : "本次完整明细没有商品，未计算覆盖比例。");

  const coverage = stages.map(([key, label]) => ({key, label, recorded: 0, unknown: 0}));
  const counts = new Map(), examples = [];
  let mock = run?.result_mode === "mock", unverified = false;
  for (const item of items) {
    for (const stage of coverage) {
      const value = item[stage.key];
      if (text(value)) stage.recorded++;
      else if (value !== null && typeof value !== "string") stage.unknown++;
    }
    const status = item.comparison_status;
    const group = comparison.get(status) || (status === null ? none : status === undefined ? absent : unknown);
    counts.set(group, (counts.get(group) || 0) + 1);
    if (examples.length < 3 && ["alert", "attention"].includes(group[1])) {
      const loc = item.source_location;
      examples.push({id: item.item_id, name: text(item.name) ? item.name : item.item_id, label: group[0],
        source: [text(loc?.sheet_name) && loc.sheet_name, Number.isSafeInteger(loc?.row_number) && loc.row_number > 0 && `第 ${loc.row_number} 行`].filter(Boolean).join(" · ")});
    }
    // Without a precheck, the server's conservative result_mode defaults to mock.
    // Do not label untouched items simulated solely because of that default.
    if (text(item.precheck_suggested_hs) || text(status)) {
      if (item.result_mode === "mock") mock = true;
      else if (item.result_mode !== "live") unverified = true;
    }
  }
  const distribution = [...comparison.values(), none, unknown, absent].filter(g => counts.has(g))
    .map(g => ({label: g[0], tone: g[1], count: counts.get(g)}));
  return {state: "ready", scope, total, coverage, distribution, examples, mock, unverified};
}

function coverageHtml(m) {
  return m.coverage.map(s => {
    // If a field is omitted the true numerator is unknown. No zero-length bar or percentage.
    const tally = s.unknown ? (s.recorded ? `至少 ${s.recorded} 条` : "未能统计") : `${s.recorded} / ${m.total}`;
    return `<li><div class="ri-line"><span>${s.label}</span><b>${tally}</b></div>${s.unknown
      ? `<small>${s.unknown} 条未提供该字段</small>`
      : `<meter min="0" max="${m.total}" value="${s.recorded}" aria-hidden="true"></meter>`}</li>`;
  }).join("");
}

export function renderReviewInsights(host, data = {}) {
  if (!host) return;
  const m = summarizeReview(data);
  host.hidden = m.state === "hidden";
  if (host.hidden) { host.innerHTML = ""; return; }
  const header = `<header class="ri-heading"><div><h2>版本审核概览</h2><p>${esc([m.scope.kind, m.scope.version, m.scope.file].filter(Boolean).join(" · "))}</p></div>
    ${m.state === "ready" ? `<strong class="ri-total">${m.total.toLocaleString("zh-CN")}<small>条商品</small></strong>` : ""}</header>`;
  if (m.state !== "ready") { host.innerHTML = header + `<p class="ri-state">${m.message}</p>`; return; }
  const warning = m.mock ? "含模拟结果，不可作为正式审核依据。" : m.unverified ? "部分预审结果来源模式未提供，请核实后使用。" : "";
  host.innerHTML = `${header}${warning ? `<p class="ri-warning">${warning}</p>` : ""}
    <div class="ri-grid"><div class="ri-block"><h3>各阶段编码记录</h3><p>有编码的商品数 / 本版本明细数</p>
      <ul class="ri-coverage">${coverageHtml(m)}</ul></div>
      <div class="ri-block"><h3>供应商原码与预审比较</h3><p>每条商品只计一次；编码一致不等于审核通过。</p>
      <ul class="ri-distribution">${m.distribution.map(g => `<li class="ri-${g.tone}"><span>${g.label}</span><b>${g.count} 条</b></li>`).join("")}</ul></div></div>
    ${m.examples.length ? `<div class="ri-follow"><h3>定位需核对的商品</h3><small>按明细顺序，最多列 3 条</small>
      <ul>${m.examples.map(x => `<li><button type="button" data-item="${esc(x.id)}"><span>${esc(x.name)}</span><small>${esc([x.label, x.source].filter(Boolean).join(" · "))}</small></button></li>`).join("")}</ul></div>` : ""}
    <details class="ri-method"><summary>查看统计口径</summary><p>来源为所选版本全部商品明细，不跨版本合并，不受列表筛选影响。各阶段可重复计入同一商品；提交留档只表示有过提交编码，并非当前待审。空编码不计入有值数，未返回字段单独标示。比较直接采用服务端结论，不推算准确率或通过率。</p></details>`;
}
