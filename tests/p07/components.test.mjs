import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {renderCommodityCard} from '../../web/components/commodity-card.js';

function host() {
  const listeners = new Map();
  const stageHost = {innerHTML: '', addEventListener() {}, removeEventListener() {}, contains: () => true};
  return {
    innerHTML: '',
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name, fn) { if (listeners.get(name) === fn) listeners.delete(name); },
    contains: () => true,
    querySelector(selector) { return selector === '[data-role="hs-comparison"]' ? stageHost : null; },
    listeners,
  };
}

function card(overrides = {}) {
  return {
    item_id: 'item-p07-test', task_id: 'task-1', version_id: 'version-1', revision: 1,
    source_location: {sheet_name: 'Sheet1', row_number: 2, cells: 'A2'},
    facts: {name: '测试商品', supplier_hs_raw: '8544422900', supplier_elements_raw: '', supplier_tax_rate_raw: '', attributes: {}},
    images: [], image_status: 'absent',
    precheck_suggested_hs: {value: null, source_id: null},
    business_confirmed_hs: {value: null, source_id: null},
    submitted_hs: {value: '8544422900', source_id: 'submission-1'},
    expert_suggested_hs: {value: null, source_id: null},
    expert_final_hs: {value: '8536901900', source_id: 'decision-1'},
    comparison_status: 'inconsistent', issues: [], evidence_status: 'obtained', confidence: null,
    latest_run_id: 'run-1', latest_expert_check: {target_hs: '8544422900', run_id: 'run-1', findings: []},
    result_mode: 'mock', task_stage: null, submission_status: null, allowed_actions: [],
    ...overrides,
  };
}

function render(cardValue, options = {}) {
  const node = host();
  renderCommodityCard(node, {card: cardValue, ...options});
  return node.innerHTML;
}

test('R-01: target_hs equal to expert final is described as covered, not as outside target', () => {
  const html = render(card({latest_expert_check: {target_hs: '8536901900', run_id: 'run-1', findings: []}}));
  assert.match(html, /专家终值 <code>8536901900<\/code> 与最近一次核查目标/);
  assert.doesNotMatch(html, /不属于上述核查目标/);
});

test('R-01: target_hs equal to submitted code keeps outside-target warning after recode', () => {
  const html = render(card());
  assert.match(html, /专家终值 <code>8536901900<\/code> 不属于上述核查目标/);
});

test('R-01: unknown target does not infer coverage', () => {
  const html = render(card({latest_expert_check: null}));
  assert.match(html, /未在契约的 latest_expert_check 中体现；页面不推断它是否被核查/);
});

test('R-01: target_hs neither submitted nor final is described as unexplained, not as covered', () => {
  const html = render(card({latest_expert_check: {target_hs: '8500000000', run_id: 'run-1', findings: []}}));
  assert.match(html, /专家终值 <code>8536901900<\/code> 与最近一次核查目标 <code>target_hs=8500000000<\/code> 不同；契约未说明核查覆盖范围，页面不推断。/);
  assert.doesNotMatch(html, /不属于上述核查目标/);
  assert.doesNotMatch(html, /与最近一次核查目标[\s\S]{0,80}一致；页面仅据契约字段展示/);
});

test('R-03: changed code always shows missing reason with default options', () => {
  const html = render(card());
  assert.match(html, /改码原因：未提供/);
});

test('R-03: contractNotes only adds developer note, never gates the missing-reason line', () => {
  const html = render(card(), {contractNotes: true});
  assert.match(html, /改码原因：未提供/);
  assert.match(html, /契约说明：ItemCard v1 未提供改码原因字段/);
  // 同一句不能既当业务缺口又当开发说明重复出现两次。
  assert.equal(html.match(/改码原因：未提供/g).length, 1);
});

test('R-03: unchanged code shows no missing-reason claim', () => {
  const html = render(card({expert_final_hs: {value: '8544422900', source_id: 'decision-1'}}));
  assert.doesNotMatch(html, /改码原因：未提供/);
});

test('R-01/R-03: findings reason is never used as the recode reason', () => {
  const html = render(card({
    latest_expert_check: {
      target_hs: '8544422900', run_id: 'run-1',
      findings: [{aspect: 'elements', verdict: 'conflict', reason: '要素里写了“专用”但未给依据'}],
    },
  }));
  assert.match(html, /改码原因：未提供/);
  assert.doesNotMatch(html, /改码原因：要素里写了/);
});

test('R-02: null comparison with a run is labeled as undetermined, never technical failure', () => {
  const page = readFileSync(new URL('../../web/pages/p07.html', import.meta.url), 'utf8');
  assert.match(page, /比较结论为空（未得出结论）/);
  // 演示页不得再把“比较结论为空”写成技术失败：整页不出现该判定词。
  assert.doesNotMatch(page, /技术失败/);
  assert.match(page, /comparison_status === null && card\.latest_run_id/);
});

test('R-01/P07-1: missing submitted code never claims "same as submitted"', () => {
  const html = render(card({
    submitted_hs: {value: null, source_id: null},
    latest_expert_check: {target_hs: '8536901900', run_id: 'run-1', findings: []},
  }));
  assert.doesNotMatch(html, /相同或未形成改码/);
  assert.match(html, /卡片没有原提交码记录；专家终值 <code>8536901900<\/code> 无法与提交值比较/);
  assert.doesNotMatch(html, /改码原因：未提供/);
});

test('R-01/P07-1: numeric zero final code counts as a value, not as missing', () => {
  const html = render(card({expert_final_hs: {value: 0, source_id: 'decision-1'}}));
  assert.match(html, /专家终值 <code>0<\/code>/);
  assert.match(html, /改码原因：未提供/);
  assert.doesNotMatch(html, /缺少专家终值/);
});

test('R-01/P07-1: missing final code is stated as missing, not as unchanged', () => {
  const html = render(card({
    expert_final_hs: {value: null, source_id: null},
    latest_expert_check: {target_hs: '8544422900', run_id: 'run-1', findings: []},
  }));
  assert.match(html, /卡片没有专家终值记录；无法判断是否改码/);
  assert.doesNotMatch(html, /相同或未形成改码/);
});

test('R-01/P07-2: recode scope never claims a missing submitted code exists', () => {
  const html = render(card({
    submitted_hs: {value: null, source_id: null},
    latest_expert_check: {target_hs: '8536901900', run_id: 'run-1', findings: []},
  }));
  assert.match(html, /卡片没有可对照的原提交码/);
  // 同区块上方没有原提交码，才允许说「没有可对照的原提交码」；这里提交码确实缺失，不算矛盾。
  assert.match(html, /原提交码（submitted_hs）<\/span>\s*<code class="commodity-recode-value">—<\/code>/);
});

test('R-01/P07-2: check without target is described as unlabeled, not as "no submitted code"', () => {
  const html = render(card({
    latest_expert_check: {target_hs: null, run_id: 'run-1', findings: []},
  }));
  assert.match(html, /最近一次专家核查记录未标注 <code>target_hs<\/code>/);
  assert.doesNotMatch(html, /卡片没有可对照的原提交码/);
});

test('P07-1: both codes missing is reported as unpaired, not inferred', () => {
  const html = render(card({
    submitted_hs: {value: null, source_id: null},
    expert_final_hs: {value: null, source_id: null},
    latest_expert_check: {target_hs: null, run_id: 'run-1', findings: []},
  }));
  assert.match(html, /卡片既没有原提交码也没有专家终值/);
  assert.match(html, /对照不完整：原提交码与专家终值都缺失/);
});
