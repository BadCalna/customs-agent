const app = document.querySelector('[data-api-base]');
const $ = (selector, root = app) => root.querySelector(selector);
const $$ = (selector, root = app) => [...root.querySelectorAll(selector)];
const list = (value) => Array.isArray(value) ? value : [];
const present = (value) => value !== undefined && value !== null && value !== '';
const display = (value) => present(value) ? String(value) : '未提供';

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false).map((child) =>
    child instanceof Node ? child : String(child)));
  return node;
}
function button(label, action, className = 'text-button') {
  const node = el('button', className, label);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
function url(path, params = {}) {
  const target = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (present(value)) target.searchParams.set(key, value);
  }
  return target;
}
async function get(path, params = {}, signal) {
  const response = await fetch(url(`${app.dataset.apiBase}${path}`, params), {
    signal, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
  });
  const data = await response.json().catch(() => null);
  if (!response.ok || !data || typeof data !== 'object') {
    throw Object.assign(new Error('reference_request_failed'), { status: response.status, data });
  }
  return data;
}
const labels = {
  provided: '已提供', not_provided: '未提供', unsupported: '不支持', input_missing: '输入不可取',
  imported: '已导入', duplicate: '已去重', conflict: '冲突待核', pending: '待核', rejected: '拒绝导入', not_imported: '未导入',
  heading: '品目', chapter_note: '章注', section_note: '类注', other_note: '其他注释',
  active: '有效', expired: '失效', merged: '已合并', unknown: '未知', not_found: '未找到',
  exact_10: '10 位精确', fallback_8: '8 位回退', exact_8: '8 位精确',
  version_unverified: '版本未核实', verified: '已核实', unreviewed: '未经复核', reviewed: '已复核',
};
function stateLabel(value) {
  return labels[value] ? `${labels[value]} · ${value}` : display(value);
}
function tag(value) {
  if (!present(value)) return null;
  const good = ['provided', 'imported', 'active', 'verified'];
  const bad = ['conflict', 'rejected', 'not_imported'];
  return el('span', `tag ${good.includes(value) ? 'good' : bad.includes(value) ? 'bad' : 'warn'}`, stateLabel(value));
}
function message(title, detail, className = 'empty-state', retry) {
  return el('div', className, el('strong', '', title), detail && el('p', '', detail),
    retry && button('重试', retry));
}
function errorMessage(error, retry) {
  const title = {
    400: '查询条件不合法，请核对后重试', 401: '登录已失效，请重新登录',
    403: '没有查看此内容的权限', 404: '指定快照或记录不可访问',
  }[error.status] || '查询失败，请重试或联系数据管理员';
  // Never render API error details or echoed input. Only bounded diagnostic identifiers.
  const diagnostic = [error.data?.error?.code, error.data?.request_id]
    .filter((part) => typeof part === 'string' && /^[a-zA-Z0-9._:-]{1,100}$/.test(part)).join(' / ');
  return message(title, diagnostic, 'error-state', retry);
}
function empty(data) {
  const [title, detail] = {
    not_provided: ['未提供该数据类别', '请查看当前快照的发布信息及来源限制。'],
    input_missing: ['已登记但本机不可取', '当前快照的源输入不可取，不显示先前版本内容。'],
    unsupported: ['当前快照不支持该数据类别', '请查看发布信息，或手动选择其他有权访问的快照。'],
    no_match: ['该快照中没有匹配记录', '请调整筛选条件。结果仅适用于当前快照。'],
  }[data.outcome] || ['本页没有记录', '可以返回上一页或调整筛选条件。'];
  return message(title, `${detail}${data.outcome ? `（${data.outcome}）` : ''}`);
}
function fields(entries) {
  return el('dl', 'detail-grid', ...entries.map(([label, value]) => el('div', 'detail-field',
    el('dt', '', label), el('dd', '', value instanceof Node ? value : display(value)))));
}
function block(title, ...children) {
  return el('section', 'detail-section', el('h3', '', title), ...children);
}
function external(value, label = '打开来源 ↗') {
  if (typeof value !== 'string') return null;
  try {
    const target = new URL(value);
    if (!['https:', 'http:'].includes(target.protocol) || target.username || target.password) return null;
    const link = el('a', 'text-button', label);
    link.href = target.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  } catch { return null; }
}
function sourceInfo(source) {
  if (!source) return null;
  return fields([['来源系统', source.system || source.source_system], ['来源文档', source.document || source.source_id],
    ['发布方', source.publisher], ['来源定位', source.locator], ['采集时间', source.retrieved_at],
    ['来源链接', external(source.url)]]);
}
function evidenceParams(ref, reason) {
  const { kind, snapshot_id, record_id, anchor, label } = ref;
  return { kind, snapshot_id, record_id, anchor, label, reason: kind === 'history' ? reason : undefined };
}
function historyReason() { return $('[name="detail_reason"]')?.value; }
function evidenceLink(ref) {
  if (!ref || !app.dataset.evidenceUrl) return null;
  const link = el('a', 'text-button', '查看证据 ↗');
  const target = () => url(app.dataset.evidenceUrl, evidenceParams(ref, historyReason())).href;
  link.href = target();
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  // The audit reason may change after cards have rendered.
  link.addEventListener('click', () => { link.href = target(); });
  return link;
}
function copyRef(ref) {
  if (!ref || !navigator.clipboard?.writeText) return null;
  return button('复制证据引用', async (event) => {
    const control = event.currentTarget;
    try {
      const { kind, snapshot_id, record_id, anchor, label } = ref;
      await navigator.clipboard.writeText(JSON.stringify({ kind, snapshot_id, record_id, anchor, label }));
      control.textContent = '已复制';
    } catch { control.textContent = '无法访问剪贴板'; }
  });
}
function evidenceActions(ref) {
  return ref ? el('div', 'card-actions', evidenceLink(ref), copyRef(ref)) : null;
}
function sectionNode(section) {
  const part = el('article', 'note-section', el('h4', '', display(section.label)),
    el('p', 'mono', `锚点 ${display(section.anchor)} · ${display(section.text_length)} 字符`),
    el('div', 'verbatim', display(section.text)), evidenceActions(section.evidence_ref));
  return part;
}
function lineage(snapshot) {
  if (!snapshot) return null;
  const statLabels = { record_count: '记录数', element_count: '要素数', code_count: '编码数', tax_count: '退税记录数' };
  const stats = Object.entries(snapshot.statistics || {}).map(([key, value]) => [statLabels[key] || key, value]);
  const manifest = list(snapshot.source_manifest).map((source) => fields([
    ['来源标识', source.source_id], ['来源系统', source.source_system], ['发布方', source.publisher],
    ['采集时间', source.retrieved_at], ['SHA-256', source.sha256], ['字节数', source.bytes], ['来源链接', external(source.url)],
  ]));
  return el('div', 'lineage-content', fields([
    ['快照', snapshot.snapshot_id], ['发布', snapshot.release_id], ['版本', snapshot.version],
    ['数据状态', stateLabel(snapshot.data_status)], ['复核状态', stateLabel(snapshot.review_status)],
    ['发布时刻', snapshot.published_at], ['采集时刻', snapshot.retrieved_at],
    ['生效日期', snapshot.effective_from], ['失效日期', snapshot.effective_to], ['发布人', snapshot.release_actor],
    ['前一发布', snapshot.previous_release_id], ['后一发布', snapshot.next_release_id],
    ['转换器版本', snapshot.converter_version], ['数据契约版本', Object.entries(snapshot.schema_versions || {}).map(([k, v]) => `${k}: ${v}`).join(' / ')],
    ['输入指纹', snapshot.input_fingerprint], ['业务指纹', snapshot.business_fingerprint],
    ['发布指纹', snapshot.release_fingerprint], ['结果指纹', snapshot.outcome_fingerprint],
    ['统计时点', snapshot.statistics_at], ['状态原因', snapshot.reason], ...stats,
  ]), ...manifest);
}
function renderRecord(kind, record, snapshot, evidence) {
  const title = record.title || record.name || record.historical_name || record.record_id;
  const out = el('div', 'detail-record', el('h3', '', display(title)),
    el('div', 'card-meta', tag(record.status), snapshot?.synthetic && el('span', 'tag warn', '合成样例 · 非官方')),
    fields([['记录 ID', record.record_id], ['快照', snapshot?.snapshot_id], ['发布', snapshot?.release_id],
      ['版本', record.version || snapshot?.version], ['来源定位', evidence?.source_locator || record.source_locator], ['原因', record.reason]]));
  if (list(snapshot?.limitations).length) out.append(el('div', 'evidence-box', ...snapshot.limitations.map((text) => el('p', '', text))));
  if (['rejected', 'not_imported'].includes(record.status)) return out;

  if (kind === 'note') {
    out.append(block('注释原文', fields([['层级键', record.tariff_no], ['层级', stateLabel(record.level)],
      ['类型', record.kind], ['章次', record.chapter], ['正文字符数', record.text_length], ['正文 SHA-256', record.text_sha256],
      ['更新时间', record.updated_at]]), ...list(evidence?.section ? [evidence.section] : record.sections).map(sectionNode)),
      block('来源', sourceInfo(record.source), fields([['输入来源标识', record.source_id]])));
  } else if (kind === 'hs') {
    const rate = record.tax_rate;
    out.append(block('HS 目录', fields([['HS 编码', record.code], ['编码长度', record.code?.length], ['状态', stateLabel(record.state)],
      ['合并至', record.merged_to], ['生效日期', record.effective_from], ['失效日期', record.effective_to],
      ['单位代码', list(record.units).map((unit) => unit === null ? 'NULL（来源未提供）' : display(unit)).join(' / ')],
      ['监管代码台账（非监管措施）', record.regulatory_code_ledger], ['监管措施', stateLabel(record.regulatory_measures?.state)],
      ['进口关税', stateLabel(record.import_duty?.state)], ['进口增值税', stateLabel(record.import_vat?.state)],
      ['来源记录数', record.source_count]]), sourceInfo(record.source)),
      block('申报要素', el('p', 'detail-hint', stateLabel(record.element_state)),
        ...(list(record.elements).length ? record.elements.map((item) => el('article', 'note-section',
          el('h4', '', `${item.order} · ${item.label}`), fields([['键', item.key], ['角色', item.role],
            ['必填口径', item.requirement], ['条件', item.condition], ['值类型', item.value_type],
            ['枚举值', list(item.enum_values).join(' / ')]]))) : [el('p', 'detail-hint', '当前快照未提供要素清单，不代表无需申报要素。')]),
        sourceInfo(record.element_source)),
      block('出口退税（非进口税率）', el('p', 'detail-hint', stateLabel(record.tax_state)),
        record.tax_match_warning && el('p', 'notice', record.tax_match_warning),
        rate ? fields([['匹配编码', rate.hs_code], ['匹配层级', stateLabel(rate.match_level)],
          ['原始税率', rate.rate_raw], ['归一税率', rate.rate_percent == null ? null : `${rate.rate_percent}%`],
          ['税率类别', rate.rate_kind], ['税率状态', rate.rate_state], ['退税代码', rate.rebate_code],
          ['核验状态', rate.verification_state], ['生效日期', rate.effective_from], ['失效日期', rate.effective_to],
          ['条件状态', rate.conditions_state], ['附加条件', rate.conditions == null ? '来源未声明（不等于无条件）'
            : rate.conditions.length === 0 ? '来源明确为无附加条件' : JSON.stringify(rate.conditions)]])
          : el('p', 'detail-hint', '无有效退税率记录，不代表税率为 0%。'), sourceInfo(rate?.source)));
    if (list(record.source_variants).length) {
      out.append(block('来源差异（待核）', ...record.source_variants.map((variant) =>
        el('pre', 'verbatim', JSON.stringify(variant, null, 2)))));
    }
  } else {
    out.append(el('div', 'notice', '仅作历史参考，不适用于当前商品的申报或放行。'),
      block('历史事实', fields([['历史 HS', record.historical_hs], ['历史品名', record.historical_name],
        ['原始日期', record.recorded_at], ['数据集 / 修订', `${display(record.dataset_key)} / ${display(record.revision)}`],
        ['规格', record.specification], ['材质', record.material], ['来源系统', record.source_system],
        ['历史结果', record.outcome], ['成功候选', record.success_candidate ? '是（仅历史参考）' : '否（没有明确成功记录）']])),
      block('复核与适用范围', fields([['复核', record.needs_review ? '待复核' : '未标记待复核'],
        ['阻断问题', list(record.blocking_issue_codes).join(' / ')], ['版本状态', stateLabel(record.version_status)],
        ['范围', record.scope], ['生效日期', record.effective_from], ['失效日期', record.effective_to]])),
      block('历史要素', Object.keys(record.elements || {}).length ? fields(Object.entries(record.elements)) : el('p', '', '未提供')));
  }
  if (evidence?.evidence_ref) {
    out.append(block('证据引用', fields([['标签', evidence.evidence_ref.label], ['锚点', evidence.evidence_ref.anchor]]),
      evidenceActions(evidence.evidence_ref)));
  } else { out.append(...[evidenceActions(record.evidence_ref)].filter(Boolean)); }
  return out;
}

function startEvidence() {
  const queries = new URLSearchParams(location.search);
  const params = Object.fromEntries(['kind', 'snapshot_id', 'record_id', 'anchor', 'label', 'reason']
    .map((name) => [name, queries.get(name)]));
  const content = $('#evidence-content');
  const back = $('#evidence-back');
  back.href = url(back.pathname, { tab: params.kind, snapshot_id: params.snapshot_id }).href;
  async function load() {
    content.replaceChildren(el('div', 'loading-line', '正在解析证据引用…'));
    try {
      const data = await get('/evidence', params);
      content.replaceChildren(renderRecord(params.kind, data.record, data.snapshot, data));
    } catch (error) { content.replaceChildren(errorMessage(error, load)); }
  }
  load();
}

function startBrowser() {
  const tabs = $$('[data-view]');
  const states = new Map(tabs.map((tab) => [tab.dataset.view, { snapshotId: '', page: 1, filters: {}, initialized: false }]));
  const params = new URLSearchParams(location.search);
  const picker = $('#snapshot-select');
  const notice = $('#global-feedback');
  const dialog = $('#record-dialog');
  const dialogContent = $('#dialog-content');
  let view = states.has(params.get('tab')) ? params.get('tab') : tabs[0].dataset.view;
  let snapshots = [];
  let ready = false;
  let queryController, detailController;
  const recordAccess = (kind) => kind !== 'history' || app.dataset.canHistoryDetail === 'true';
  const filterForm = (kind) => $(`[data-filter="${kind}"]`);
  const slot = (kind) => $(`#${kind}-results`);
  const updateAddress = () => history.replaceState(null, '', url(location.pathname, {
    tab: view, snapshot_id: states.get(view).snapshotId,
  }));
  const getFilters = (kind) => Object.fromEntries([...new FormData(filterForm(kind))]
    .filter(([key, value]) => key !== 'detail_reason' && value.trim()).map(([key, value]) => [key, value.trim()]));
  const currentSnapshots = () => snapshots.filter((snapshot) => snapshot.kind === view);

  function snapshotMetadata(snapshot) {
    const panel = $('#snapshot-meta');
    const details = $('#snapshot-lineage');
    panel.replaceChildren();
    details.hidden = !snapshot;
    details.open = false;
    $('#snapshot-lineage-content').replaceChildren(...[lineage(snapshot)].filter(Boolean));
    if (!snapshot) return;
    panel.append(el('div', 'card-meta', tag(snapshot.data_status), tag(snapshot.review_status),
      snapshot.synthetic && el('span', 'tag warn', '合成样例 · 非官方')),
    el('div', 'mono', `${snapshot.snapshot_id} / ${snapshot.release_id}`),
    el('div', '', `版本 ${display(snapshot.version)} · 发布 ${display(snapshot.published_at)}`));
    for (const limitation of list(snapshot.limitations)) panel.append(el('div', 'snapshot-limitation', limitation));
  }
  function syncPicker() {
    const state = states.get(view);
    const choices = currentSnapshots();
    if (!state.initialized && ready) {
      state.initialized = true;
      // A pinned URL must never silently resolve to a newer generation.
      state.snapshotId = state.snapshotId || choices.find((item) => item.is_active_pointer)?.snapshot_id || choices[0]?.snapshot_id || '';
    }
    const options = choices.map((item) => new Option(
      `${item.version} · ${item.snapshot_id}${item.is_active_pointer ? '（发布指针）' : ''}`, item.snapshot_id));
    if (!options.length || !choices.some((item) => item.snapshot_id === state.snapshotId)) {
      options.unshift(new Option(!ready ? '正在读取快照…' : state.snapshotId ? '指定快照不可访问' : '无可用快照', state.snapshotId));
    }
    picker.replaceChildren(...options);
    picker.value = state.snapshotId;
    picker.disabled = !choices.length;
    snapshotMetadata(choices.find((item) => item.snapshot_id === state.snapshotId));
  }
  function show(kind) {
    view = kind;
    for (const tab of tabs) {
      const active = tab.dataset.view === kind;
      tab.classList.toggle('is-active', active);
      if (active) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    }
    for (const panel of $$('[data-view-panel]')) panel.hidden = panel.dataset.viewPanel !== kind;
    syncPicker();
    if (ready) { updateAddress(); load(); }
  }
  function tree(rows, state) {
    const root = $('#note-tree');
    root.replaceChildren();
    if (!rows.length) { root.append(el('p', 'loading-line', '没有可用的目录节点')); return; }
    const byParent = new Map();
    for (const row of rows) {
      if (!byParent.has(row.parent_key)) byParent.set(row.parent_key, []);
      byParent.get(row.parent_key).push(row);
    }
    const built = new Set();
    function branch(row, depth) {
      if (built.has(row.tariff_no)) return;
      built.add(row.tariff_no);
      const active = row.tariff_no === state.filters.key;
      const node = button('', () => {
        const input = $('[name="key"]', filterForm('note'));
        input.value = active ? '' : row.tariff_no;
        filterForm('note').requestSubmit();
      }, `tree-item depth-${Math.min(depth, 3)}${active ? ' is-selected' : ''}`);
      node.setAttribute('aria-pressed', String(active));
      node.append(el('small', '', row.tariff_no), display(row.title));
      root.append(node);
      for (const child of byParent.get(row.tariff_no) || []) branch(child, depth + 1);
    }
    for (const row of byParent.get(null) || []) branch(row, 0);
    for (const row of rows) branch(row, 0);
  }
  function card(kind, record, data) {
    const unavailable = ['rejected', 'not_imported'].includes(record.status);
    const title = kind === 'note' ? record.title : kind === 'hs' ? record.name : record.historical_name;
    const code = kind === 'note' ? record.tariff_no : kind === 'hs' ? record.code : record.historical_hs;
    const headline = el('div', 'result-card-head', el('h3', '', unavailable ? '记录不可用' : display(title)),
      el('span', 'code', code || record.record_id));
    const result = el('article', 'result-card', headline, el('div', 'card-meta', tag(record.status)));
    if (record.reason) result.append(el('p', '', record.reason));
    if (unavailable) return result;
    const bits = kind === 'note' ? [stateLabel(record.level), `章次 ${display(record.chapter)}`,
      `${display(record.section_count)} 段 · ${display(record.text_length)} 字符`]
      : kind === 'hs' ? [stateLabel(record.state), `要素：${stateLabel(record.element_state)}`,
        `退税：${record.rate_percent == null ? stateLabel(record.tax_state) : `${record.rate_percent}%`}`,
        record.match_level && stateLabel(record.match_level)]
        : ['仅供参考', display(record.recorded_at), stateLabel(record.version_status),
          record.needs_review ? '待复核' : null, record.success_candidate ? '明确成功候选' : '非成功候选'];
    result.append(el('p', '', bits.filter(Boolean).join(' / ')),
      el('p', 'record-locator', `${data.snapshot.version} · ${record.source_system ? `${record.source_system} · ` : ''}${display(record.source_locator)}`));
    if (kind === 'history') {
      const issues = list(record.blocking_issue_codes);
      if (issues.length) result.append(el('p', 'detail-hint', `阻断问题：${issues.join(' / ')}`));
    }
    if (recordAccess(kind)) result.append(el('div', 'card-actions',
      button('查看详情 →', () => openRecord(kind, record, data.snapshot_id)), evidenceLink(record.evidence_ref)));
    return result;
  }
  async function openRecord(kind, record, snapshotId) {
    const revision = kind === 'history' ? record.revision : undefined;
    const options = { snapshot_id: snapshotId, revision, reason: kind === 'history' ? historyReason() : undefined };
    const path = `${kind === 'history' ? '/history' : `/catalog/${kind}`}/${encodeURIComponent(record.record_id)}`;
    if (!dialog.open) dialog.showModal();
    detailController?.abort();
    const controller = new AbortController();
    detailController = controller;
    dialogContent.replaceChildren(el('div', 'loading-line', '正在读取详情…'));
    try {
      const data = await get(path, options, controller.signal);
      if (detailController === controller && dialog.open) dialogContent.replaceChildren(renderRecord(kind, data.record, data.snapshot));
    } catch (error) {
      if (!controller.signal.aborted && dialog.open) dialogContent.replaceChildren(errorMessage(error, () => openRecord(kind, record, snapshotId)));
    }
  }
  function dataSets(items, state) {
    const input = $('[name="dataset_key"]');
    input.replaceChildren(new Option('全部可见数据集', ''), ...items.map((item) => new Option(
      `${item.dataset_key} · ${item.record_count} 条${item.synthetic ? ' · 合成样例' : ''}`, item.dataset_key)));
    input.value = state.filters.dataset_key || '';
  }
  async function load() {
    queryController?.abort();
    const controller = new AbortController();
    queryController = controller;
    const kind = view;
    const state = states.get(kind);
    const results = slot(kind);
    const summary = $(`[data-summary="${kind}"]`);
    const pager = $(`[data-pager="${kind}"]`);
    results.replaceChildren();
    pager.replaceChildren();
    summary.textContent = '';
    if (kind === 'note') $('#note-tree').replaceChildren();
    if (!currentSnapshots().some((snapshot) => snapshot.snapshot_id === state.snapshotId)) {
      results.append(message(state.snapshotId ? '指定快照不可访问' : '当前没有可访问的快照',
        '可选择其他有权访问的快照。系统不会自动切换到最新版。'));
      return;
    }
    results.append(el('div', 'loading-line', '正在查询快照…'));
    results.setAttribute('aria-busy', 'true');
    try {
      const data = await get(kind === 'history' ? '/history' : '/catalog', {
        ...state.filters, tab: kind === 'history' ? undefined : kind,
        snapshot_id: state.snapshotId, page: state.page,
      }, controller.signal);
      if (controller.signal.aborted) return;
      if (kind === 'note') tree(list(data.tree), state);
      if (kind === 'history') dataSets(list(data.datasets), state);
      const items = list(data.items);
      const mode = data.query_mode === 'prefix' ? '前缀结果 · ' : data.query_mode === 'exact' ? '精确结果 · ' : '';
      summary.textContent = `${mode}${data.total} 条记录 · 第 ${data.page} 页 · ${data.snapshot.version}`;
      results.replaceChildren(...(items.length ? items.map((item) => card(kind, item, data)) : [empty(data)]));
      const changePage = (page) => { state.page = page; load(); };
      if (data.page > 1) pager.append(button('← 上一页', () => changePage(data.page - 1)));
      if (data.has_next) pager.append(button('下一页 →', () => changePage(data.page + 1)));
    } catch (error) {
      if (!controller.signal.aborted) results.replaceChildren(errorMessage(error, load));
    } finally {
      // An aborted request must not clear a newer request's busy state.
      if (queryController === controller || view !== kind) results.removeAttribute('aria-busy');
    }
  }
  async function loadSnapshots() {
    notice.hidden = true;
    try {
      const data = await get('/snapshots');
      snapshots = list(data.items);
      ready = true;
      show(view);
    } catch (error) {
      picker.replaceChildren(new Option('快照读取失败', ''));
      notice.replaceChildren(errorMessage(error, loadSnapshots));
      notice.hidden = false;
    }
  }
  states.get(view).snapshotId = params.get('snapshot_id') || '';
  for (const tab of tabs) tab.addEventListener('click', () => show(tab.dataset.view));
  for (const control of $$('[data-refresh]')) control.addEventListener('click', () => ready ? load() : loadSnapshots());
  for (const form of $$('[data-filter]')) form.addEventListener('submit', (event) => {
    event.preventDefault();
    const state = states.get(form.dataset.filter);
    state.filters = getFilters(form.dataset.filter);
    state.page = 1;
    if (ready) load();
  });
  picker.addEventListener('change', () => {
    const state = states.get(view);
    if (picker.value === state.snapshotId) return;
    if (currentSnapshots().some((item) => item.snapshot_id === state.snapshotId) &&
        !window.confirm('切换快照将离开当前版本并重新查询。是否继续？')) {
      picker.value = state.snapshotId;
      return;
    }
    state.snapshotId = picker.value;
    state.page = 1;
    if (view === 'history') {
      delete state.filters.dataset_key;
      dataSets([], state);
    }
    syncPicker();
    updateAddress();
    load();
  });
  dialog.addEventListener('close', () => { detailController?.abort(); dialogContent.replaceChildren(); });
  show(view);
  loadSnapshots();
}

if (app.hasAttribute('data-ref-evidence')) startEvidence();
else startBrowser();
