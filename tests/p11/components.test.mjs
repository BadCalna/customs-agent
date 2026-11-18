/* P11 处置表单与版本历史的真实行为测试（node --test）。
 *
 * 原 P11 测试只检查源码字符串/赋值位置，抓不到「旧 scope 覆盖当前视图」「构造传 data
 * 却没被读入」「在飞响应清掉新输入」这类行为缺陷。这里用最小 DOM 桩直接 import 两个
 * 组件并驱动真实交互：不引入外部依赖、不起服务器、不读浏览器状态。
 *
 * 覆盖（对应 2026-11-15 审核项 + 2026-11-17 二次对抗审查项）：
 *   1. M-01 跨提交草稿隔离：S1 → S2 不串；切回 S1 恢复它自己的草稿
 *   2. R-05 请求在飞时继续输入：较早的成功响应不得清掉新输入（确认 / 决定两处）
 *   3. R-04 退回决定渲染为「退回问题」；无 final_hs 时不出现「未改」「核查目标一致」
 *   4. M-08a 构造时传 data + 直接 mount 即显示历史（不再空白）
 *   5. M-08b 旧 scope 不能覆盖当前视图作用域
 *   6. M-08c 查看旧 V1 + 任务当前 V2 + 完整任务级历史 → 接受
 *   7. R-06 null / 空串 / 缺失 / 小数 revision 全部拦截且不发请求
 *   8. 409 后刷新失败 → 提示「刷新未完成」，不显示「已刷新」
 *   9. P1 回归：看 V2 时晚到的「只有 V1」任务级快照必须丢弃，视图不变
 *  10. P2 构造期被拒绝的 data 不在 mount 时渲染（pendingData）
 *  11. P2 R-05 清理绑定作用域：S1 的响应不清 S2 的新草稿，也不冒充当前视图结果
 *  12. P2 确认草稿按「任务+版本」保活；决定草稿仍按提交隔离
 *  13. P2 决定级 issues[] 同一商品多条问题全部展示
 *  14. P3 未知 / 缺失 action 仍展示退回问题文本
 *  15. P3 FIX-09 主校验：版本级 V1 数据 + scope 自证 V2 → 丢弃
 *  16. E3 写请求失败时草稿必须保留（确认 + 决定）
 *  17. 409 重新拉取模块的行为：reload reject 必须抛出，resolve 正常返回
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {createReviewActions} from '../../web/components/review-actions.js';
import {createVersionHistory} from '../../web/components/version-history.js';
import {createConflictRefresh} from '../../web/pages/p11-conflict.js';

/* ------------------------------- DOM 桩 ---------------------------------- */

function element(tag = 'div') {
  const listeners = new Map();
  return {
    tagName: tag.toUpperCase(),
    id: '',
    className: '',
    textContent: '',
    innerHTML: '',
    hidden: false,
    dataset: {},
    removed: false,
    focused: false,
    listeners,
    addEventListener(kind, fn) {listeners.set(kind, fn);},
    removeEventListener(kind, fn) {if (listeners.get(kind) === fn) listeners.delete(kind);},
    contains: () => true,
    remove() {this.removed = true;},
    focus() {this.focused = true;},
    /* 组件在 render 后按选择器找拒绝框；桩只在断言不关心的路径返回空。 */
    querySelector: () => null,
    replaceChildren() {},
  };
}

globalThis.document = {
  activeElement: null,
  createElement: (tag) => element(tag),
  getElementById: () => null,
};

/** 与 tests/p08/components.test.mjs 同形的宿：只记录组件挂进来的根节点。 */
function host() {
  return {
    child: null,
    innerHTML: '',
    replaceChildren(node) {this.child = node;},
  };
}

const html = (node) => (node && node.child ? node.child.innerHTML : '');

function dispatchField(root, {name, item = '', value = ''}) {
  const field = {dataset: {raField: name, item}, value};
  const listener = root.listeners.get('input');
  assert.ok(listener, `${name}：组件没有绑定 input 事件`);
  listener({target: {closest: (selector) => (selector === '[data-ra-field]' ? field : null)}});
}

function dispatchClick(root, {act, item = '', value = ''}) {
  const button = {dataset: {raAct: act, item, value}};
  const listener = root.listeners.get('click');
  assert.ok(listener, `${act}：组件没有绑定 click 事件`);
  listener({target: {closest: (selector) => (selector === '[data-ra-act]' ? button : null)}});
}

const flush = async (rounds = 4) => {
  for (let index = 0; index < rounds; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => {resolve = ok; reject = fail;});
  return {promise, resolve, reject};
}

/** 记录调用的假 api；可以用 enqueue 让某个动作挂在 deferred 上模拟“请求在飞”。 */
function recordingApi() {
  const calls = {confirm: [], submit: [], decide: []};
  const queue = {confirm: [], submit: [], decide: []};
  const action = (name) => (payload) => {
    calls[name].push(payload);
    const next = queue[name].shift();
    if (next) return typeof next === 'function' ? next(payload) : next;
    return Promise.resolve({status: 'accepted_for_review', revision: 9, request_id: 'test'});
  };
  return {calls, enqueue(name, value) {queue[name].push(value);}, confirm: action('confirm'), submit: action('submit'), decide: action('decide')};
}

/* ------------------------------ 数据构造 --------------------------------- */

const ITEM = 'item-1';
const SUBMITTED = '1111111111';

function card(overrides = {}) {
  return {
    item_id: ITEM,
    revision: 1,
    facts: {name: '商品A', supplier_hs_raw: '0000000000'},
    source_location: {row_number: 1, sheet_name: 'Sheet1', cells: 'A1'},
    business_confirmed_hs: {value: null, source_id: null},
    precheck_suggested_hs: {value: '2222222222', source_id: null},
    submitted_hs: {value: null, source_id: null},
    expert_final_hs: {value: null, source_id: null},
    allowed_actions: ['read', 'confirmation.write', 'submission.create', 'decision.resolve'],
    ...overrides,
  };
}

function operatorData({cardOverrides = {}, taskOverrides = {}, versionOverrides = {}} = {}) {
  return {
    task: {
      task_id: 'task-1',
      title: '演示任务',
      stage: 'awaiting_confirmation',
      revision: 1,
      current_version_id: 'version-1',
      allowed_actions: ['read', 'confirmation.write', 'submission.create'],
      ...taskOverrides,
    },
    version: {version_id: 'version-1', number: 2, ...versionOverrides},
    items: [card(cardOverrides)],
    submission: null,
  };
}

function expertData({submissionId = 'sub-1', submissionRevision = 1, cardOverrides = {}, target = ''} = {}) {
  return {
    task: {
      task_id: 'task-1',
      title: '演示任务',
      stage: 'submitted',
      revision: 2,
      current_version_id: 'version-1',
      allowed_actions: ['read', 'decision.resolve'],
    },
    version: {version_id: 'version-1', number: 2},
    items: [card(cardOverrides)],
    submission: {
      submission_id: submissionId,
      revision: submissionRevision,
      status: 'pending',
      created_at: null,
      created_by: '业务员甲',
      items: [{
        item_id: ITEM,
        name: '商品A',
        source_location: {row_number: 1},
        submitted_hs: SUBMITTED,
        item_revision: 1,
      }],
    },
    expert_check: target ? {target_hs: target, status: 'succeeded'} : null,
  };
}

function versionScopedHistory({versionId = 'version-1', number = 2, marker = 'V2-MARKER', action = 'approve', decisionItems = null} = {}) {
  const items = decisionItems || [{
    item_id: ITEM,
    name: `${marker}-商品`,
    source_location: {row_number: 1},
    previous_hs: SUBMITTED,
    final_hs: SUBMITTED,
    change_reason: '',
  }];
  return {
    task_id: 'task-1',
    version_id: versionId,
    number,
    original_filename: `${marker}.xlsx`,
    submissions: [{
      submission_id: `${marker}-submission`,
      revision: 1,
      status: action === 'return' ? 'returned' : 'approved',
      created_at: '2026-11-15T10:00:00+08:00',
      created_by: '业务员甲',
      items: [{item_id: ITEM, name: `${marker}-商品`, source_location: {row_number: 1}, submitted_hs: SUBMITTED, item_revision: 1}],
      decisions: [{
        decision_id: `${marker}-decision`,
        action,
        return_to: action === 'return' ? 'operator' : '',
        reason: `${marker} 的决定说明`,
        created_at: '2026-11-15T11:00:00+08:00',
        created_by: '专家甲',
        expert_check: {target_hs: SUBMITTED, status: 'succeeded'},
        items,
      }],
    }],
  };
}

function taskHistory({currentVersionId = 'version-2'} = {}) {
  return {
    task_id: 'task-1',
    current_version_id: currentVersionId,
    versions: [
      {
        version_id: 'version-1',
        number: 1,
        original_filename: 'V1-MARKER.xlsx',
        uploaded_at: '2026-11-14T09:00:00+08:00',
        is_current: false,
        approved: true,
        submissions: [{
          submission_id: 'V1-MARKER-submission',
          revision: 1,
          status: 'approved',
          created_by: '业务员甲',
          created_at: '2026-11-14T10:00:00+08:00',
          items: [{item_id: 'item-v1', name: 'V1-MARKER-商品', source_location: {row_number: 1}, submitted_hs: SUBMITTED, item_revision: 1}],
          decisions: [{
            decision_id: 'V1-MARKER-decision',
            action: 'approve',
            reason: 'V1 通过',
            created_by: '专家甲',
            created_at: '2026-11-14T11:00:00+08:00',
            expert_check: {target_hs: SUBMITTED, status: 'succeeded'},
            items: [{item_id: 'item-v1', previous_hs: SUBMITTED, final_hs: SUBMITTED, change_reason: ''}],
          }],
        }],
      },
      {
        version_id: 'version-2',
        number: 2,
        original_filename: 'V2-MARKER.xlsx',
        uploaded_at: '2026-11-15T09:00:00+08:00',
        is_current: true,
        approved: false,
        submissions: [],
      },
    ],
    candidates: [],
  };
}

/* ------------------------------- 用例 ----------------------------------- */

test('M-01 专家草稿按任务+版本+提交隔离：S1 的终值/原因不会带到 S2，切回 S1 恢复', async () => {
  const api = recordingApi();
  const first = expertData({submissionId: 'sub-1'});
  const controller = createReviewActions({api, role: 'expert', data: first});
  const node = host();
  controller.mount(node);

  dispatchField(node.child, {name: 'final-hs', item: ITEM, value: '9999999991'});
  dispatchField(node.child, {name: 'change-reason', item: ITEM, value: 'S1-改码原因'});
  assert.equal(controller.getDraft().decision.rows[ITEM].final_hs, '9999999991');

  // 同一版本、同一商品 id，但换成另一份提交 S2（例如退回后重新提交）。
  controller.update(expertData({submissionId: 'sub-2'}));
  const draftInS2 = controller.getDraft();
  assert.equal(draftInS2.scope, 'task-1::version-1::sub-2');
  assert.equal(draftInS2.decision.rows[ITEM] ? draftInS2.decision.rows[ITEM].final_hs : '', '', 'S2 不应继承 S1 的终值');
  assert.equal(draftInS2.decision.rows[ITEM] ? draftInS2.decision.rows[ITEM].change_reason : '', '', 'S2 不应继承 S1 的改码原因');
  assert.doesNotMatch(html(node), /9999999991/);
  assert.doesNotMatch(html(node), /S1-改码原因/);

  // 在 S2 上提交通过：发送的是 S2 自己的提交值，而不是 S1 的草稿终值。
  dispatchClick(node.child, {act: 'decide'});
  await flush();
  assert.equal(api.calls.decide.length, 1);
  assert.deepEqual(api.calls.decide[0].items, [{item_id: ITEM, final_hs: SUBMITTED, change_reason: ''}]);

  // 切回 S1：恢复 S1 自己的草稿（隔离而不是丢弃）。
  controller.update(first);
  assert.equal(controller.getDraft().scope, 'task-1::version-1::sub-1');
  assert.match(html(node), /9999999991/);
  assert.match(html(node), /S1-改码原因/);
  controller.destroy();
});

test('P1 update(S2) 不得让固定 S1 请求适配器接收 S2 表单', async () => {
  let owner = 'sub-1';
  const writes = [];
  const api = {
    confirm: async () => ({}), submit: async () => ({}),
    async decide(payload, expectedSubmissionId) {
      if (expectedSubmissionId !== owner) throw new Error('提交快照与请求目标不一致');
      writes.push({owner, payload});
      return {};
    },
  };
  const controller = createReviewActions({api, role: 'expert', data: expertData({submissionId: 'sub-1'})});
  const node = host();
  controller.mount(node);
  controller.update(expertData({submissionId: 'sub-2'}));
  dispatchClick(node.child, {act: 'decide'});
  await flush();
  assert.equal(writes.length, 0, 'S2 表单不能写入旧 S1 owner');

  owner = 'sub-2';
  dispatchClick(node.child, {act: 'decide'});
  await flush();
  assert.equal(writes.length, 1, '匹配的 S2 owner 仍可正常提交');
  assert.equal(writes[0].owner, 'sub-2');
  assert.deepEqual(writes[0].payload.items, [{item_id: ITEM, final_hs: SUBMITTED, change_reason: ''}]);
  controller.destroy();
});

test('M-01 退回草稿同样按提交隔离：S1 的逐商品问题不进入 S2 退回', async () => {
  const api = recordingApi();
  const first = expertData({submissionId: 'sub-1'});
  const controller = createReviewActions({api, role: 'expert', data: first});
  const node = host();
  controller.mount(node);

  dispatchField(node.child, {name: 'decision-mode', item: '', value: 'return'});
  dispatchField(node.child, {name: 'return-to', item: '', value: 'operator'});
  dispatchField(node.child, {name: 'issue', item: ITEM, value: 'S1-材质与规格不一致'});

  controller.update(expertData({submissionId: 'sub-2'}));
  dispatchField(node.child, {name: 'decision-mode', item: '', value: 'return'});
  dispatchField(node.child, {name: 'return-to', item: '', value: 'operator'});
  dispatchField(node.child, {name: 'decision-reason', item: '', value: 'S2 整批退回原因'});
  assert.doesNotMatch(html(node), /S1-材质与规格不一致/);

  dispatchClick(node.child, {act: 'decide'});
  await flush();
  assert.equal(api.calls.decide.length, 0, 'S2 没有自己的逐商品问题，不能借用 S1 的问题退回');
  assert.match(html(node), /至少填写一条逐商品问题/);
  controller.destroy();
});

test('P2 确认草稿按「任务+版本」保活：刷新带上 submission 不会整档丢失', () => {
  const api = recordingApi();
  const controller = createReviewActions({api, role: 'operator', data: operatorData()});
  const node = host();
  controller.mount(node);
  assert.equal(controller.getDraft().confirmScope, 'task-1::version-1');

  dispatchField(node.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchField(node.child, {name: 'confirm-reason', item: ITEM, value: '未提交的确认原因'});

  // 刷新时页面补上 submission（真实页面接 P09 后很常见）：决定作用域变了，确认草稿不变。
  controller.update({
    ...operatorData(),
    submission: {submission_id: 'sub-9', revision: 1, status: 'pending', items: []},
  });

  assert.equal(controller.getDraft().confirmScope, 'task-1::version-1', '确认草稿键不能受 submission 影响');
  assert.equal(controller.getDraft().confirm[ITEM].hs, SUBMITTED, '未提交的确认值不能因刷新丢掉');
  assert.equal(controller.getDraft().confirm[ITEM].reason, '未提交的确认原因');
  assert.match(html(node), /value="1111111111"/);
  assert.match(html(node), /未提交的确认原因/);
  controller.destroy();
});

test('P2 决定草稿仍按「任务+版本+提交」隔离（与确认草稿分开换档）', () => {
  const api = recordingApi();
  const controller = createReviewActions({api, role: 'expert', data: expertData({submissionId: 'sub-1'})});
  const node = host();
  controller.mount(node);
  dispatchField(node.child, {name: 'final-hs', item: ITEM, value: '9999999991'});
  assert.equal(controller.getDraft().scope, 'task-1::version-1::sub-1');

  controller.update(expertData({submissionId: 'sub-2'}));
  assert.equal(controller.getDraft().scope, 'task-1::version-1::sub-2');
  assert.equal(controller.getDraft().decision.rows[ITEM].final_hs, '', 'S1 的终值不能带进 S2');
  assert.doesNotMatch(html(node), /9999999991/);

  // 切回 S1 仍恢复它自己的草稿：三段键按提交分档，不是丢弃。
  controller.update(expertData({submissionId: 'sub-1'}));
  assert.match(html(node), /9999999991/);
  controller.destroy();
});

test('R-05 确认表单：较早的成功响应不清掉请求期间的新输入', async () => {
  const api = recordingApi();
  const pending = deferred();
  api.enqueue('confirm', pending.promise);
  const controller = createReviewActions({api, role: 'operator', data: operatorData()});
  const node = host();
  controller.mount(node);

  dispatchField(node.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchClick(node.child, {act: 'confirm', item: ITEM});
  await flush();
  assert.equal(api.calls.confirm.length, 1);
  assert.equal(api.calls.confirm[0].confirmed_hs, SUBMITTED);

  // 请求在飞：用户继续输入下一行的值（还没发送）。
  dispatchField(node.child, {name: 'confirm-hs', item: ITEM, value: '3333333333'});
  dispatchField(node.child, {name: 'confirm-reason', item: ITEM, value: 'B-请求期间填写的原因'});

  pending.resolve({status: 'accepted_for_review', revision: 2});
  await flush();

  assert.match(html(node), /value="3333333333"/, '请求期间的新输入不能被较早的成功响应清掉');
  assert.match(html(node), /value="B-请求期间填写的原因"/);
  assert.equal(controller.getDraft().confirm[ITEM].hs, '3333333333');
  controller.destroy();
});

test('R-05 决定表单：较早的成功响应不清掉请求期间的终值与说明', async () => {
  const api = recordingApi();
  const pending = deferred();
  api.enqueue('decide', pending.promise);
  const controller = createReviewActions({api, role: 'expert', data: expertData()});
  const node = host();
  controller.mount(node);

  dispatchField(node.child, {name: 'final-hs', item: ITEM, value: '2222222222'});
  dispatchField(node.child, {name: 'change-reason', item: ITEM, value: '第一次改码原因'});
  dispatchField(node.child, {name: 'decision-reason', item: '', value: '第一次整批说明'});
  dispatchClick(node.child, {act: 'decide'});
  await flush();
  assert.equal(api.calls.decide.length, 1);
  assert.equal(api.calls.decide[0].items[0].final_hs, '2222222222');

  // 请求在飞：继续改终值、原因与整批说明。
  dispatchField(node.child, {name: 'final-hs', item: ITEM, value: '3333333333'});
  dispatchField(node.child, {name: 'change-reason', item: ITEM, value: '第二次改码原因'});
  dispatchField(node.child, {name: 'decision-reason', item: '', value: '第二次整批说明'});

  pending.resolve({status: 'approved', revision: 3});
  await flush();

  assert.match(html(node), /value="3333333333"/, '请求期间的终值不能被清掉');
  assert.match(html(node), /value="第二次改码原因"/);
  assert.match(html(node), /第二次整批说明/);
  controller.destroy();
});

test('P2 R-05 跨作用域：S1 的响应不清 S2 的草稿，也不冒充当前视图的结果', async () => {
  const api = recordingApi();
  const pending = deferred();
  api.enqueue('decide', pending.promise);
  const controller = createReviewActions({api, role: 'expert', data: expertData({submissionId: 'sub-1'})});
  const node = host();
  controller.mount(node);

  // S1：终值填写与提交值相同（最容易在 S2 里“值相同被误清”的取值）。
  dispatchField(node.child, {name: 'final-hs', item: ITEM, value: SUBMITTED});
  dispatchField(node.child, {name: 'decision-reason', item: '', value: 'S1 整批说明'});
  dispatchClick(node.child, {act: 'decide'});
  await flush();
  assert.equal(api.calls.decide.length, 1);
  assert.equal(controller.getDraft().scope, 'task-1::version-1::sub-1');

  // 请求在飞：用户切到另一份提交 S2，并输入相同的终值与原因。
  controller.update(expertData({submissionId: 'sub-2'}));
  dispatchField(node.child, {name: 'final-hs', item: ITEM, value: SUBMITTED});
  dispatchField(node.child, {name: 'change-reason', item: ITEM, value: 'S2 自己的改码原因'});
  assert.equal(controller.getDraft().scope, 'task-1::version-1::sub-2');

  pending.resolve({status: 'approved', revision: 3});
  await flush();

  const draftInS2 = controller.getDraft();
  assert.equal(draftInS2.scope, 'task-1::version-1::sub-2');
  assert.equal(draftInS2.decision.rows[ITEM].final_hs, SUBMITTED, 'S2 的终值不能被 S1 的响应清掉');
  assert.equal(draftInS2.decision.rows[ITEM].change_reason, 'S2 自己的改码原因');
  assert.match(html(node), /value="1111111111"/);
  assert.match(html(node), /value="S2 自己的改码原因"/);

  // 反馈不能冒充当前视图的结果。
  const text = html(node);
  assert.match(text, /已切换到另一份作用域/, '跨作用域响应要说明它不属于当前视图');
  assert.match(text, /本次没有清理任何输入/);
  assert.doesNotMatch(text, /专家决定：服务端已响应/, '不能把 S1 的成功反馈显示成 S2 的结果');
  controller.destroy();
});

test('E3 写请求失败时草稿必须保留（确认 + 决定）', async () => {
  // 确认：501 失败，输入与原因都要留着。
  const confirmApi = recordingApi();
  confirmApi.enqueue('confirm', () =>
    Promise.reject(Object.assign(new Error('未实现'), {status: 501, code: 'not_implemented', details: {owner: 'P09'}})));
  const confirmController = createReviewActions({api: confirmApi, role: 'operator', data: operatorData()});
  const confirmHost = host();
  confirmController.mount(confirmHost);
  dispatchField(confirmHost.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchField(confirmHost.child, {name: 'confirm-reason', item: ITEM, value: '失败也要保留的原因'});
  dispatchClick(confirmHost.child, {act: 'confirm', item: ITEM});
  await flush();

  assert.equal(confirmApi.calls.confirm.length, 1);
  assert.match(html(confirmHost), /value="1111111111"/, '失败时确认 HS 草稿必须保留');
  assert.match(html(confirmHost), /失败也要保留的原因/, '失败时确认原因必须保留');
  assert.equal(confirmController.getDraft().confirm[ITEM].hs, SUBMITTED);
  assert.equal(confirmController.getDraft().confirm[ITEM].reason, '失败也要保留的原因');
  confirmController.destroy();

  // 决定：409 失败，终值 / 改码原因 / 整批说明都要留着。
  const decideApi = recordingApi();
  decideApi.enqueue('decide', () => Promise.reject(Object.assign(new Error('商品修订已变化'), {status: 409, code: 'conflict'})));
  const decideController = createReviewActions({api: decideApi, role: 'expert', data: expertData()});
  const decideHost = host();
  decideController.mount(decideHost);
  dispatchField(decideHost.child, {name: 'final-hs', item: ITEM, value: '2222222222'});
  dispatchField(decideHost.child, {name: 'change-reason', item: ITEM, value: '失败也要保留的改码原因'});
  dispatchField(decideHost.child, {name: 'decision-reason', item: '', value: '失败也要保留的说明'});
  dispatchClick(decideHost.child, {act: 'decide'});
  await flush();

  assert.equal(decideApi.calls.decide.length, 1);
  const decideDraft = decideController.getDraft().decision;
  assert.equal(decideDraft.rows[ITEM].final_hs, '2222222222', '失败时终值必须保留');
  assert.equal(decideDraft.rows[ITEM].change_reason, '失败也要保留的改码原因');
  assert.equal(decideDraft.reason, '失败也要保留的说明');
  assert.match(html(decideHost), /value="2222222222"/);
  assert.match(html(decideHost), /失败也要保留的改码原因/);
  decideController.destroy();
});

test('R-04 退回决定渲染为「退回问题」，不出现改码/未改/核查目标一致', () => {
  const history = versionScopedHistory({
    marker: 'RETURN-MARKER',
    action: 'return',
    decisionItems: [
      {item_id: ITEM, issue: '材质与规格型号不一致，需补充说明'},
      {item_id: 'item-2'},
    ],
  });
  const controller = createVersionHistory({data: history, taskId: 'task-1', activeVersionId: 'version-1'});
  const node = host();
  controller.mount(node);
  const text = html(node);

  assert.match(text, /退回问题/);
  assert.match(text, /材质与规格型号不一致，需补充说明/);
  assert.doesNotMatch(text, /改码/, '退回没有终值，不能渲染成改码记录');
  assert.doesNotMatch(text, /缺少改码原因/);
  assert.doesNotMatch(text, /未改/);
  assert.doesNotMatch(text, /终值等于提交值/);
  assert.doesNotMatch(text, /核查目标一致/);
  controller.destroy();
});

test('R-04 通过决定缺 final_hs 时不推导「未改」或「核查目标一致」', () => {
  const history = versionScopedHistory({
    marker: 'MISSING-FINAL',
    action: 'approve',
    decisionItems: [{item_id: ITEM, previous_hs: SUBMITTED, final_hs: '', change_reason: ''}],
  });
  const controller = createVersionHistory({data: history, taskId: 'task-1', activeVersionId: 'version-1'});
  const node = host();
  controller.mount(node);
  const text = html(node);

  assert.match(text, /无终值记录/);
  assert.match(text, /无法与核查目标比对/);
  assert.doesNotMatch(text, /未改/, '缺终值不能说成未改');
  assert.doesNotMatch(text, /终值等于提交值/);
  assert.doesNotMatch(text, /核查目标一致/);
  controller.destroy();
});

test('M-08a 构造时传 data + 直接 mount 即显示历史', () => {
  const controller = createVersionHistory({
    data: versionScopedHistory({marker: 'CONSTRUCT-DATA'}),
    taskId: 'task-1',
    activeVersionId: 'version-1',
    currentVersionId: 'version-1',
  });
  const node = host();
  controller.mount(node);
  const text = html(node);

  assert.match(text, /版本 \/ 提交 \/ 专家决定/);
  assert.match(text, /CONSTRUCT-DATA/, '构造参数 data 必须真的被读入并渲染');
  assert.doesNotMatch(text, /尚未提供历史数据/);
  assert.equal(controller.getState().hasData, true);
  controller.destroy();
});

test('M-08b 旧 scope 不能覆盖当前视图作用域', () => {
  const controller = createVersionHistory({
    data: versionScopedHistory({marker: 'VIEW-V2'}),
    taskId: 'task-1',
    activeVersionId: 'version-1',
    currentVersionId: 'version-1',
  });
  const node = host();
  controller.mount(node);
  const before = html(node);
  assert.match(before, /VIEW-V2/);

  const lateV1 = versionScopedHistory({versionId: 'version-0', number: 1, marker: 'LATE-V1'});
  const accepted = controller.accept(lateV1, {taskId: 'task-1', versionId: 'version-0'});

  assert.equal(accepted, false, '旧 scope 不能把 V1 响应算成当前作用域');
  assert.match(html(node), /已丢弃版本不匹配的历史响应/);
  assert.match(html(node), /VIEW-V2/, '当前 V2 视图必须保持不变');
  assert.doesNotMatch(html(node), /LATE-V1 的决定说明/);
  assert.equal(controller.getState().activeVersionId, 'version-1');

  // 调用方自证作用域与视图不一致时也要丢弃（fail-closed）：即使响应体属于当前视图，
  // 带着旧 scope 的请求被视为晚到请求，不能写进当前视图。
  const staleScope = controller.accept(versionScopedHistory({marker: 'SCOPED-V2'}), {
    taskId: 'task-1',
    versionId: 'version-0',
  });
  assert.equal(staleScope, false, '旧 scope 自证与视图不一致时应 fail-closed');
  assert.match(html(node), /VIEW-V2/);
  assert.doesNotMatch(html(node), /SCOPED-V2/);
  controller.destroy();
});

test('M-08c 查看旧 V1 而任务当前是 V2 时，完整任务级历史仍被接受', () => {
  const controller = createVersionHistory({
    taskId: 'task-1',
    activeVersionId: 'version-1',
    currentVersionId: 'version-2',
  });
  const node = host();
  controller.mount(node);
  assert.match(html(node), /尚未提供历史数据/, '构造时没有 data，首次挂载应为空态');

  const accepted = controller.accept(taskHistory({currentVersionId: 'version-2'}));

  assert.equal(accepted, true, '任务级历史只校验任务身份，不要求正在查看的版本等于任务当前版本');
  assert.equal(controller.getState().rejection, null);
  assert.match(html(node), /V1-MARKER/, 'V1 的历史记录应当显示');
  assert.match(html(node), /V2-MARKER/, '任务当前 V2 的分组也应当显示');
  assert.doesNotMatch(html(node), /已丢弃/);
  controller.destroy();
});

test('P1 回归 看 V2 时晚到的「只有 V1」任务级快照必须丢弃，视图不变', () => {
  const controller = createVersionHistory({
    data: taskHistory({currentVersionId: 'version-2'}),
    taskId: 'task-1',
    activeVersionId: 'version-2',
    currentVersionId: 'version-2',
  });
  const node = host();
  controller.mount(node);
  assert.match(html(node), /V2-MARKER/);

  // 过期任务级快照：versions 只有 V1，current_version_id="version-1"。
  const staleV1Only = {
    task_id: 'task-1',
    current_version_id: 'version-1',
    versions: [{
      version_id: 'version-1',
      number: 1,
      original_filename: 'STALE-V1-ONLY.xlsx',
      is_current: true,
      approved: true,
      submissions: [],
    }],
    candidates: [],
  };
  const accepted = controller.accept(staleV1Only);

  assert.equal(accepted, false, '任务级快照不含当前视图版本时必须丢弃');
  const text = html(node);
  assert.match(text, /已丢弃版本不匹配的历史响应/);
  assert.match(text, /任务级快照不含当前视图版本/);
  assert.match(text, /任务级快照（V1）/, '拒绝提示要说明响应里实际带的是哪些版本');
  assert.match(text, /V2-MARKER/, '当前 V2 视图必须保持不变');
  assert.doesNotMatch(text, /STALE-V1-ONLY/, '被拒绝的 V1 内容不能渲染出来');
  assert.equal(controller.getState().activeVersionId, 'version-2');
  assert.equal(controller.getState().rejection.reason.includes('不含当前视图版本'), true);
  controller.destroy();
});

test('P1 回归 真实 DEMO-SET 形状：自测页首屏历史被接受，只有旧版本的快照被丢弃', () => {
  // 自测页用 DEMO-SET.json 装配历史（tasks[].versions + 当前版本）；新守卫若把
  // 首屏数据也拒掉，页面会直接变成「尚未提供历史数据」。这里用真实 fixture 形状锁住。
  const demo = JSON.parse(readFileSync(new URL('../../contracts/fixtures/DEMO-SET.json', import.meta.url), 'utf8'));
  const mainTask = demo.tasks.find((task) => task.title && task.title.includes('主流程')) || demo.tasks[0];
  const currentVersion = mainTask.versions.find((version) => version.is_current) || mainTask.versions[mainTask.versions.length - 1];
  const oldVersion = mainTask.versions.find((version) => version.version_id !== currentVersion.version_id);
  assert.ok(oldVersion, 'fixture 至少要有两个版本才能验证晚到快照');

  const history = {
    task_id: mainTask.task_id,
    current_version_id: currentVersion.version_id,
    versions: mainTask.versions.map((version) => ({
      version_id: version.version_id,
      number: version.number,
      original_filename: version.original_filename,
      is_current: version.version_id === currentVersion.version_id,
      submissions: [],
    })),
    candidates: [],
  };
  const controller = createVersionHistory({
    data: history,
    taskId: mainTask.task_id,
    activeVersionId: currentVersion.version_id,
    currentVersionId: currentVersion.version_id,
  });
  const node = host();
  controller.mount(node);
  assert.equal(controller.getState().hasData, true, '自测页首屏历史必须被接受');
  assert.match(html(node), /版本 \/ 提交 \/ 专家决定/);
  assert.doesNotMatch(html(node), /已丢弃/);

  const onlyOld = {
    task_id: mainTask.task_id,
    current_version_id: oldVersion.version_id,
    versions: [{
      version_id: oldVersion.version_id,
      number: oldVersion.number,
      original_filename: oldVersion.original_filename,
      is_current: true,
      submissions: [],
    }],
    candidates: [],
  };
  assert.equal(controller.accept(onlyOld), false, '只含旧版本的过期快照不能替换当前视图');
  controller.destroy();
});

test('P2 构造期 data 被拒绝后，首次 mount 不得渲染它的内容', () => {
  // 当前视图 V2，构造时却传了一份 V1 的版本级历史：accept 会拒绝。
  const controller = createVersionHistory({
    data: versionScopedHistory({versionId: 'version-0', number: 1, marker: 'REJECTED-CONSTRUCT-V1'}),
    taskId: 'task-1',
    activeVersionId: 'version-1',
    currentVersionId: 'version-1',
  });
  const node = host();
  controller.mount(node);
  const text = html(node);

  assert.doesNotMatch(text, /REJECTED-CONSTRUCT-V1/, '被拒绝的构造期数据不能出现在页面上');
  assert.equal(controller.getState().hasData, false, '被拒绝的数据不能算作已接受数据');
  // 「已丢弃」提示与「上一份已接受数据保持显示」只在真的接受了数据后才成立：
  // 首次挂载没有上一份数据，页面不能同时显示被丢弃内容。
  assert.match(text, /尚未提供历史数据/);
  controller.destroy();
});

test('P3 FIX-09 主校验：版本级 V1 数据 + scope 自证为 V2 → 丢弃且视图不变', () => {
  const controller = createVersionHistory({
    data: versionScopedHistory({marker: 'VIEW-V2-MAIN'}),
    taskId: 'task-1',
    activeVersionId: 'version-1',
    currentVersionId: 'version-1',
  });
  const node = host();
  controller.mount(node);
  assert.match(html(node), /VIEW-V2-MAIN/);

  // 与自测页 #late-v1 相同的调用形状：scope 自证是“当前版本”，但响应体属于 V1。
  const lateV1Body = versionScopedHistory({versionId: 'version-0', number: 1, marker: 'MAIN-GUARD-LATE-V1'});
  const accepted = controller.accept(lateV1Body, {taskId: 'task-1', versionId: 'version-1'});

  assert.equal(accepted, false, 'scope 自证不能把 V1 数据算成当前版本');
  const text = html(node);
  assert.match(text, /版本不匹配/, '拒绝原因应指向 version_id 不匹配');
  assert.match(text, /VIEW-V2-MAIN/, '当前视图必须保持不变');
  assert.doesNotMatch(text, /MAIN-GUARD-LATE-V1/);
  controller.destroy();
});

test('P2 决定级 issues[] 同一商品多条问题全部展示', () => {
  const history = versionScopedHistory({
    marker: 'MULTI-ISSUE',
    action: 'return',
    decisionItems: [{item_id: ITEM, issue: '明细里的第一条问题'}],
  });
  // 决定级问题：同一 item 追加两条，另有一条属于 items[] 里没有的商品。
  history.submissions[0].decisions[0].issues = [
    {item_id: ITEM, message: '第二条问题'},
    {item_id: ITEM, message: '第三条问题'},
    {item_id: 'item-extra', message: '未在明细里的商品问题'},
  ];
  const controller = createVersionHistory({data: history, taskId: 'task-1', activeVersionId: 'version-1'});
  const node = host();
  controller.mount(node);
  const text = html(node);

  assert.match(text, /明细里的第一条问题；第二条问题；第三条问题/, '同一商品的多条问题必须累加，不能只留第一条');
  assert.match(text, /未在明细里的商品问题/, '没有对应明细行的问题也要显示，不能静默丢弃');
  controller.destroy();
});

test('P3 未知 / 缺失 action 仍展示退回问题文本', () => {
  for (const [label, action] of [['未知取值 returned', 'returned'], ['缺失 action', '']]) {
    const history = versionScopedHistory({
      marker: `UNKNOWN-ACTION-${action || 'MISSING'}`,
      action,
      decisionItems: [{item_id: ITEM, issue: `${label} 的问题文本：材质与规格不一致`}],
    });
    const controller = createVersionHistory({data: history, taskId: 'task-1', activeVersionId: 'version-1'});
    const node = host();
    controller.mount(node);
    const text = html(node);

    assert.match(text, /材质与规格不一致/, `${label}：问题文本不能消失`);
    assert.match(text, /不在已知范围/, `${label}：要说明动作取值未知，不推断业务结论`);
    assert.doesNotMatch(text, /改码/, `${label}：没有终值时不能渲染成改码记录`);
    controller.destroy();
  }
});

test('R-06 非整数修订号全部拦截且不发请求（商品/任务/提交三处）', async () => {
  const badValues = [
    ['null', null],
    ['空串', ''],
    ['空白串', '   '],
    ['小数', 1.5],
    ['小数串', '2.5'],
    ['非数字串', 'abc'],
    ['布尔', true],
  ];

  for (const [label, bad] of badValues) {
    // 1) 确认：商品修订号
    const confirmApi = recordingApi();
    const confirmController = createReviewActions({api: confirmApi, role: 'operator', data: operatorData({cardOverrides: {revision: bad}})});
    const confirmHost = host();
    confirmController.mount(confirmHost);
    dispatchField(confirmHost.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
    dispatchClick(confirmHost.child, {act: 'confirm', item: ITEM});
    await flush();
    assert.equal(confirmApi.calls.confirm.length, 0, `确认：商品修订号 ${label} 不应发送请求`);
    assert.match(html(confirmHost), /修订号/, `确认：商品修订号 ${label} 应给出拦截提示`);
    confirmController.destroy();

    // 2) 整批提交：任务修订号
    const submitApi = recordingApi();
    const submitData = operatorData({
      cardOverrides: {business_confirmed_hs: {value: SUBMITTED, source_id: 'conf-1'}},
      taskOverrides: {revision: bad},
    });
    const submitController = createReviewActions({api: submitApi, role: 'operator', data: submitData});
    const submitHost = host();
    submitController.mount(submitHost);
    dispatchClick(submitHost.child, {act: 'submit'});
    await flush();
    assert.equal(submitApi.calls.submit.length, 0, `提交：任务修订号 ${label} 不应发送请求`);
    submitController.destroy();

    // 3) 整批提交：逐行商品修订号
    const itemApi = recordingApi();
    const itemData = operatorData({
      cardOverrides: {business_confirmed_hs: {value: SUBMITTED, source_id: 'conf-1'}, revision: bad},
    });
    const itemController = createReviewActions({api: itemApi, role: 'operator', data: itemData});
    const itemHost = host();
    itemController.mount(itemHost);
    dispatchClick(itemHost.child, {act: 'submit'});
    await flush();
    assert.equal(itemApi.calls.submit.length, 0, `提交：商品修订号 ${label} 不应发送请求`);
    itemController.destroy();

    // 4) 专家决定：提交修订号
    const decideApi = recordingApi();
    const decideController = createReviewActions({api: decideApi, role: 'expert', data: expertData({submissionRevision: bad})});
    const decideHost = host();
    decideController.mount(decideHost);
    dispatchClick(decideHost.child, {act: 'decide'});
    await flush();
    assert.equal(decideApi.calls.decide.length, 0, `决定：提交修订号 ${label} 不应发送请求`);
    assert.match(html(decideHost), /修订号/);
    decideController.destroy();
  }

  // 缺失字段（不是 null，而是字段不存在）同样拦截。
  const missingApi = recordingApi();
  const missingCard = card();
  delete missingCard.revision;
  const missingController = createReviewActions({
    api: missingApi,
    role: 'operator',
    data: {task: operatorData().task, version: operatorData().version, items: [missingCard], submission: null},
  });
  const missingHost = host();
  missingController.mount(missingHost);
  dispatchField(missingHost.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchClick(missingHost.child, {act: 'confirm', item: ITEM});
  await flush();
  assert.equal(missingApi.calls.confirm.length, 0, '确认：缺少 revision 字段不应发送请求');
  missingController.destroy();
});

test('R-06 正例：整数与整数字符串修订号仍会发送（规范化成数字）', async () => {
  const api = recordingApi();
  const controller = createReviewActions({api, role: 'operator', data: operatorData({cardOverrides: {revision: '3'}})});
  const node = host();
  controller.mount(node);
  dispatchField(node.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchClick(node.child, {act: 'confirm', item: ITEM});
  await flush();
  assert.equal(api.calls.confirm.length, 1);
  assert.equal(api.calls.confirm[0].expected_revision, 3);
  controller.destroy();

  const submitApi = recordingApi();
  const submitData = operatorData({cardOverrides: {business_confirmed_hs: {value: SUBMITTED, source_id: 'conf-1'}}});
  const submitController = createReviewActions({api: submitApi, role: 'operator', data: submitData});
  const submitHost = host();
  submitController.mount(submitHost);
  dispatchClick(submitHost.child, {act: 'submit'});
  await flush();
  assert.equal(submitApi.calls.submit.length, 1);
  assert.equal(submitApi.calls.submit[0].revision, 1);
  assert.deepEqual(submitApi.calls.submit[0].items, [{item_id: ITEM, confirmation_id: 'conf-1'}]);
  submitController.destroy();

  const decideApi = recordingApi();
  const decideController = createReviewActions({api: decideApi, role: 'expert', data: expertData({submissionRevision: 4})});
  const decideHost = host();
  decideController.mount(decideHost);
  dispatchClick(decideHost.child, {act: 'decide'});
  await flush();
  assert.equal(decideApi.calls.decide.length, 1);
  decideController.destroy();
});

test('409 后刷新失败：提示刷新未完成，不显示「已刷新」', async () => {
  const api = recordingApi();
  api.enqueue('confirm', () => Promise.reject(Object.assign(new Error('商品修订已变化'), {status: 409, code: 'conflict'})));
  const controller = createReviewActions({
    api,
    role: 'operator',
    data: operatorData(),
    onConflict: async () => {throw new Error('fixture 重新读取失败');},
  });
  const node = host();
  controller.mount(node);
  dispatchField(node.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchClick(node.child, {act: 'confirm', item: ITEM});
  await flush();

  const text = html(node);
  assert.match(text, /内容已被他人修改，请刷新后重试/);
  assert.match(text, /自动刷新未完成：fixture 重新读取失败/);
  assert.doesNotMatch(text, /已刷新/, '刷新失败时不能显示成已刷新');
  controller.destroy();
});

test('409 后刷新成功：仍然提示已刷新（与刷新失败区分）', async () => {
  const api = recordingApi();
  api.enqueue('confirm', () => Promise.reject(Object.assign(new Error('商品修订已变化'), {status: 409, code: 'conflict'})));
  let refreshed = 0;
  const controller = createReviewActions({
    api,
    role: 'operator',
    data: operatorData(),
    onConflict: async () => {refreshed += 1;},
  });
  const node = host();
  controller.mount(node);
  dispatchField(node.child, {name: 'confirm-hs', item: ITEM, value: SUBMITTED});
  dispatchClick(node.child, {act: 'confirm', item: ITEM});
  await flush();

  assert.equal(refreshed, 1);
  assert.match(html(node), /内容已被他人修改，已刷新/);
  controller.destroy();
});

test('409 重新拉取模块：reload reject 时必须继续抛出（不吞成「已刷新」）', async () => {
  const lines = [];
  const conflictRefresh = createConflictRefresh({
    reload: async () => {throw new Error('fixture 重新读取失败');},
    log: (line) => lines.push(line),
  });

  await assert.rejects(() => conflictRefresh('业务员确认/提交'), /fixture 重新读取失败/);
  assert.ok(lines.some((line) => line.includes('收到 409')), '进入 409 分支应有日志');
  assert.ok(lines.some((line) => line.includes('409 后的重新拉取失败')), '失败必须留痕');
});

test('409 重新拉取模块：reload resolve 时正常返回', async () => {
  let reloaded = 0;
  const conflictRefresh = createConflictRefresh({reload: async () => {reloaded += 1;}});
  const result = await conflictRefresh('专家决定');
  assert.equal(result, undefined);
  assert.equal(reloaded, 1);
});

test('409 重新拉取模块：缺少 reload 时直接报错（避免接线成空实现）', () => {
  assert.throws(() => createConflictRefresh({}), /需要 reload 回调/);
  assert.throws(() => createConflictRefresh({reload: 'not-a-function'}), /需要 reload 回调/);
});

test('p11.html 的 409 接线：import 模块并把真正的重读逻辑作为 reload 传入', () => {
  const page = readFileSync(new URL('../../web/pages/p11.html', import.meta.url), 'utf8');
  assert.match(page, /import \{ createConflictRefresh \} from "\.\/p11-conflict\.js";/);
  assert.match(page, /createConflictRefresh\(\{ reload: reloadFixtures, log \}\)/, '页面必须把 reload 交给工厂');

  // 页面侧 reload 必须是真实重读 + 重放，不能是空实现（行为本身由上面的模块用例覆盖）。
  const start = page.indexOf('async function reloadFixtures(');
  assert.ok(start > -1, 'p11.html 缺少 reloadFixtures');
  const end = page.indexOf('\n    }', start);
  assert.ok(end > start, 'reloadFixtures 位置异常');
  const block = page.slice(start, end);
  assert.match(block, /await loadFixtures\(\)/);
  assert.match(block, /currentData = buildData\(\)/);
  assert.match(block, /applyDataToPanels\(\)/);
});
