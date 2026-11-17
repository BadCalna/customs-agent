/* P08 生产页面（web/pages/p08.js）的行为测试：只读接线、空态、错误分类与旧响应丢弃。
 *
 * 这里不断言源码字符串：模式解析、任务→进度请求、空态文案、错误分类、选择序号都是
 * 从页面模块导入的真实函数；页面装配用假容器 + 假 client 驱动，检查真实渲染结果。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

import {createClient} from '../../web/api.js';
import {
  MODE_FIXTURE,
  MODE_LIVE,
  identityView,
  modeLabel,
  modeNotice,
  mountP08Page,
  noticeHtml,
  parseMode,
  planRunRequest,
  selectionMarkers,
  versionSummary,
} from '../../web/pages/p08.js';

const FIXTURE_PATH = new URL('../../contracts/fixtures/DEMO-SET.json', import.meta.url);
const dataset = JSON.parse(readFileSync(FIXTURE_PATH));
const copy = (value) => structuredClone(value);
const [mockTask, expertTask] = dataset.tasks;
const runOf = (id) => copy(dataset.runs[id]);
const taskWithoutRun = (task) => ({...copy(task), latest_run_id: null, latest_run_kind: null, latest_run_status: null});

/** 与 components.test.mjs 同形的假容器：只记录监听与 innerHTML/textContent/hidden。 */
function host() {
  const listeners = new Map();
  return {
    innerHTML: '', textContent: '', className: '', hidden: false,
    addEventListener(kind, fn) { listeners.set(kind, fn); },
    removeEventListener(kind, fn) { if (listeners.get(kind) === fn) listeners.delete(kind); },
    replaceChildren() { this.innerHTML = ''; },
    contains: () => true,
    listeners,
  };
}

const ALLOWED_READS = ['getRun', 'getTask', 'listTasks', 'me'];
const FORBIDDEN_CALLS = ['uploadFile', 'claimTask', 'login', 'logout', 'getImport', 'listClaimable', 'listItems', 'getItem'];

function fakeClient(options = {}) {
  const {
    tasks = dataset.tasks,
    detail = (id) => tasks.find((task) => task.task_id === id) || null,
    runs = {},
    meResult = {display_name: '演示账号', role_label: '业务员 · FIXTURE 模式'},
    meError = null,
    listError = null,
    getRun = async (id) => runs[id] || null,
  } = options;
  const calls = {me: [], listTasks: [], getTask: [], getRun: []};
  const writes = [];
  const client = {
    async me() { calls.me.push(true); if (meError) throw meError; return meResult; },
    async listTasks(params = {}) {
      calls.listTasks.push(params);
      if (listError) throw listError;
      const rows = params.stage ? tasks.filter((task) => task.stage === params.stage) : tasks;
      return {items: copy(rows), next_cursor: null};
    },
    async getTask(id) {
      calls.getTask.push(id);
      const value = await detail(id);
      if (!value) throw {status: 404, message: '演示数据中没有该任务'};
      return copy(value);
    },
    async getRun(id) {
      calls.getRun.push(id);
      const value = await getRun(id);
      if (!value) throw {status: 404, message: '演示数据中没有该执行'};
      return copy(value);
    },
  };
  for (const name of FORBIDDEN_CALLS) {
    client[name] = async () => { writes.push(name); throw new Error(`页面不应调用 ${name}`); };
  }
  return {client, calls, writes};
}

function mount(options) {
  const nodes = {taskHost: host(), runHost: host(), identityHost: host(),
    noticeHost: host(), summaryHost: host(), modeHost: host()};
  const page = mountP08Page({mode: MODE_FIXTURE, pageSize: 5, ...nodes, ...options});
  return {page, ...nodes};
}

/* ------------------------------ 纯逻辑 ---------------------------------- */

test('模式解析：只有 ?mode=fixture 读契约样例，缺省与未知值都按 live', () => {
  assert.equal(parseMode(''), MODE_LIVE);
  assert.equal(parseMode('?'), MODE_LIVE);
  assert.equal(parseMode('?mode=live'), MODE_LIVE);
  assert.equal(parseMode('?mode=fixture'), MODE_FIXTURE);
  assert.equal(parseMode('?mode=fixture&task=x'), MODE_FIXTURE);
  assert.equal(parseMode('?mode=FIXTURE'), MODE_LIVE);
  assert.equal(parseMode('?mode=demo'), MODE_LIVE);
  assert.equal(parseMode(undefined), MODE_LIVE);
  assert.match(modeLabel(MODE_FIXTURE), /FIXTURE/);
  assert.match(modeLabel(MODE_LIVE), /LIVE/);
  assert.match(modeNotice(MODE_FIXTURE), /DEMO-SET\.json/);
  assert.match(modeNotice(MODE_FIXTURE), /result_mode=mock/);
  assert.match(modeNotice(MODE_LIVE), /\/api\/v1/);
});

test('任务→进度请求：用 latest_run_id 与当前版本组成只读 context，且吻合契约样例的执行记录', () => {
  const plan = planRunRequest(mockTask);
  const run = runOf(mockTask.latest_run_id);
  assert.equal(plan.kind, 'run');
  assert.deepEqual(Object.keys(plan.context).sort(),
    ['isVersionCurrent', 'runId', 'taskId', 'versionId', 'versionNumber']);
  assert.equal(plan.context.taskId, run.task_id);
  assert.equal(plan.context.versionId, run.version_id);
  assert.equal(plan.context.runId, run.run_id);
  assert.equal(plan.context.versionNumber, 2);
  assert.equal(plan.context.isVersionCurrent, true);
});

test('尚无执行记录是空态：不借用别的 run，也不写成失败', () => {
  const plan = planRunRequest(taskWithoutRun(mockTask));
  assert.equal(plan.kind, 'empty');
  assert.equal(plan.context, undefined);
  assert.match(plan.message, /尚无执行记录/);
  assert.match(plan.message, /不代表执行失败/);
  assert.doesNotMatch(plan.message, /异常|错误/);
});

test('详情自称有执行却缺当前版本时停下并说明，不凭猜测读取', () => {
  const plan = planRunRequest({...copy(mockTask), current_version_id: null});
  assert.equal(plan.kind, 'invalid');
  assert.equal(plan.context, undefined);
  assert.match(plan.message, /当前版本编号/);
  for (const bad of [null, {}, {...copy(mockTask), task_id: ''}]) {
    assert.equal(planRunRequest(bad).kind, 'invalid');
  }
});

test('任务摘要按详情字段说明版本与执行记录，取不到版本时直说', () => {
  assert.equal(versionSummary(null), '尚未选择任务');
  assert.match(versionSummary(mockTask), /当前版本 V2/);
  assert.match(versionSummary(mockTask), /契约演示-V2\.xlsx/);
  assert.match(versionSummary(mockTask), /共 2 个版本/);
  assert.match(versionSummary(mockTask), /最新一轮执行/);
  assert.match(versionSummary(taskWithoutRun(mockTask)), /尚无执行记录/);
  assert.match(versionSummary({task_id: 't', versions: []}), /任务尚无当前版本/);
});

test('合成样例标记只来自服务端 result_mode，不推测', () => {
  const mock = selectionMarkers({...copy(mockTask), result_mode: 'mock'});
  assert.equal(mock.mock, true);
  assert.match(mock.title, /result_mode=mock/);
  assert.match(mock.text, /不是真实 AI 审核结论/);
  for (const resultMode of ['live', undefined, null, 'MOCK']) {
    const view = selectionMarkers({...copy(mockTask), result_mode: resultMode});
    assert.equal(view.mock, false);
    assert.equal(view.title, '');
    assert.equal(view.text, '');
  }
});

test('空态与错误提示分属不同 role，外部文本一律转义', () => {
  const empty = noticeHtml({kind: 'empty', message: '该任务尚无执行记录'});
  assert.match(empty, /role="status"/);
  assert.match(empty, /尚无执行记录/);
  const invalid = noticeHtml({kind: 'invalid', message: '<img src=x onerror=alert(1)>'});
  assert.match(invalid, /role="alert"/);
  assert.match(invalid, /&lt;img/);
  assert.doesNotMatch(invalid, /<img/);
  assert.match(noticeHtml({kind: 'info', title: '合成样例', message: '说明'}), /is-info/);
});

test('身份读取错误按 401/403/404/409/501 分类展示，不伪造登录成功', () => {
  const labels = [401, 403, 404, 409, 501].map((status) => identityView({error: {status, message: '服务端消息'}}).label);
  assert.equal(new Set(labels).size, 5);
  for (const label of labels) assert.match(label, /^身份读取失败：/);
  assert.match(identityView({me: null, mode: MODE_LIVE}).label, /未登录/);
  assert.equal(identityView({me: null, mode: MODE_FIXTURE}).kind, 'anonymous');
  assert.match(identityView({me: null, mode: MODE_FIXTURE}).label, /FIXTURE/);
  assert.equal(identityView({me: {display_name: '张三', role_label: '业务员'}}).label, '张三 · 业务员');
  assert.equal(identityView({}) /* 无身份也按未登录，不当成已登录 */ .kind, 'anonymous');
});

/* ------------------------------ 页面装配 -------------------------------- */

test('首屏：读身份与第一页任务；未选中任务前不读执行记录', async () => {
  const {client, calls, writes} = fakeClient();
  const {page, taskHost, runHost, identityHost, noticeHost, modeHost} = mount({client});
  await page.ready;
  assert.deepEqual(calls.getRun, []);
  assert.deepEqual(calls.getTask, []);
  assert.deepEqual(calls.listTasks, [{stage: '', limit: 5, cursor: null}]);
  assert.match(identityHost.textContent, /演示账号/);
  assert.match(modeHost.textContent, /FIXTURE/);
  assert.match(taskHost.innerHTML, /我的任务/);
  assert.match(taskHost.innerHTML, /合成样例/);
  assert.match(runHost.innerHTML, /选择任务后查看运行进度/);
  assert.match(noticeHost.innerHTML, /DEMO-SET\.json/);
  assert.deepEqual(writes, []);
  page.destroy();
});

test('选中任务后才读 run；mock 结果显示合成样例标记，且只调用只读方法', async () => {
  const {client, calls, writes} = fakeClient({runs: dataset.runs});
  const {page, runHost, noticeHost, summaryHost} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  assert.deepEqual(calls.getTask, [mockTask.task_id]);
  assert.deepEqual(calls.getRun, [mockTask.latest_run_id]);
  assert.equal(runHost.hidden, false);
  assert.match(runHost.innerHTML, /合成样例 · 非真实AI结论/);
  assert.match(runHost.innerHTML, /AI预审/);
  assert.match(runHost.innerHTML, /是否通过，以人工审核决定为准/);
  assert.match(noticeHost.innerHTML, /合成样例（result_mode=mock）/);
  assert.match(summaryHost.textContent, /当前版本 V2/);
  assert.deepEqual(writes, []);
  const used = Object.entries(calls).filter(([, log]) => log.length).map(([name]) => name).sort();
  assert.deepEqual(used, ALLOWED_READS);
  page.destroy();
});

test('尚无执行记录：收起进度面板并说明是空态，不读 run、不显示成失败', async () => {
  const task = taskWithoutRun(mockTask);
  const {client, calls} = fakeClient({tasks: [task], runs: dataset.runs});
  const {page, runHost, noticeHost, taskHost} = mount({client});
  await page.ready;
  const before = calls.getRun.length;
  await page.selectTask(task.task_id);
  assert.equal(calls.getRun.length, before);
  assert.equal(runHost.hidden, true);
  assert.match(noticeHost.innerHTML, /尚无执行记录/);
  assert.match(noticeHost.innerHTML, /不代表执行失败/);
  assert.doesNotMatch(noticeHost.innerHTML, /技术失败|暂时无法更新|p08-error/);
  assert.match(taskHost.innerHTML, /尚无执行记录/);
  page.destroy();
});

test('详情不自洽时（有执行但无当前版本）显示既有错误分类，不假装成功', async () => {
  const broken = {...copy(mockTask), latest_run_id: 'run-without-version', current_version_id: null};
  const {client, calls} = fakeClient({tasks: [broken], runs: dataset.runs});
  const {page, runHost, noticeHost} = mount({client});
  await page.ready;
  await page.selectTask(broken.task_id);
  assert.deepEqual(calls.getRun, []);
  assert.equal(runHost.hidden, true);
  assert.match(noticeHost.innerHTML, /无法确认执行归属/);
  assert.match(noticeHost.innerHTML, /role="alert"/);
  page.destroy();
});

test('切换任务时旧响应不得覆盖新选择', async () => {
  const slowRun = runOf(mockTask.latest_run_id);
  const fastRun = runOf(expertTask.latest_run_id);
  const requested = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(mockTask), copy(expertTask)], next_cursor: null}; },
    async getTask(id) { return copy(id === mockTask.task_id ? mockTask : expertTask); },
    async getRun(id) {
      requested.push(id);
      if (id === mockTask.latest_run_id) { await gate; return copy(slowRun); }
      return copy(fastRun);
    },
  };
  const {page, runHost} = mount({client});
  await page.ready;
  const slow = page.selectTask(mockTask.task_id);
  // 先让第一个任务的 getTask 落地（run 请求已发出并被挂起），再切到另一个任务。
  await new Promise((resolve) => setImmediate(resolve));
  await page.selectTask(expertTask.task_id);
  assert.equal(page.getState().selectedId, expertTask.task_id);
  // 慢的旧请求确实已经发出，才会在下面被丢弃。
  assert.deepEqual(requested, [mockTask.latest_run_id, expertTask.latest_run_id]);
  release();
  await slow;
  assert.equal(page.getState().selectedId, expertTask.task_id);
  assert.match(runHost.innerHTML, new RegExp(fastRun.run_id));
  assert.doesNotMatch(runHost.innerHTML, new RegExp(slowRun.run_id));
  page.destroy();
});

test('列表刷新到新版本后，右侧重新读详情，不把旧版本当“当前版本”', async () => {
  // 另一个标签页上传了 V3：列表刷新会拿到新的 current_version_id / latest_run_id，
  // 但右侧仍是上次选择时的详情。刷新按钮走组件内部 load()，页面必须靠 onLoaded 核对。
  const oldVersion = mockTask.current_version_id;
  const oldRun = runOf(mockTask.latest_run_id);
  const newVersion = 'version-v3';
  const newRun = {...copy(oldRun), run_id: 'run-v3', version_id: newVersion};
  let listRow = {...copy(mockTask)};
  let detailRow = {...copy(mockTask), versions: [{version_id: oldVersion, number: 2}]};
  const calls = {getTask: 0, getRun: []};
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(listRow)], next_cursor: null}; },
    async getTask(id) { calls.getTask += 1; return copy(detailRow); },
    async getRun(id) { calls.getRun.push(id); return copy(id === newRun.run_id ? newRun : oldRun); },
  };
  const {page, runHost, summaryHost} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  assert.match(summaryHost.textContent, /当前版本 V2/);
  assert.deepEqual(calls.getRun, [mockTask.latest_run_id]);

  // 列表刷新到 V3（真实路径：组件刷新按钮 → 内部 load → onLoaded）
  listRow = {...listRow, current_version_id: newVersion, current_version_number: 3, latest_run_id: newRun.run_id};
  detailRow = {...copy(listRow), versions: [{version_id: newVersion, number: 3}]};
  await page.tasks.load();

  assert.equal(calls.getTask, 2, '列表显示新版本后必须重读详情');
  assert.match(summaryHost.textContent, /当前版本 V3/, '摘要不能继续把旧版本说成当前版本');
  assert.deepEqual(calls.getRun, [mockTask.latest_run_id, newRun.run_id], '进度必须改为读取新执行');
  assert.match(runHost.innerHTML, new RegExp(newRun.run_id));
  assert.doesNotMatch(runHost.innerHTML, new RegExp(oldRun.run_id));
  page.destroy();
});

test('用户正在改选 B 时刷新到 A 的新版本：不得把选择抢回 A', async () => {
  // 交错时序：A 已选中 → 用户点 B（B 详情在飞）→ 列表刷新拿到 A 的新版本。
  // 自动核对必须服从用户最新选择，不能再用 A 发起重选把 B 的请求顶掉。
  let releaseB;
  const gateB = new Promise((resolve) => { releaseB = resolve; });
  const versionOfA = 'version-a-v3';
  const runA = runOf(mockTask.latest_run_id);
  const runBV3 = {...copy(runOf(expertTask.latest_run_id))};
  let listRows = [copy(mockTask), copy(expertTask)];
  const calls = {getTask: [], getRun: []};
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: listRows.map(copy), next_cursor: null}; },
    async getTask(id) {
      calls.getTask.push(id);
      if (id === expertTask.task_id) {
        await gateB;
        return copy(expertTask);
      }
      return copy({...mockTask, versions: [{version_id: mockTask.current_version_id, number: 2}]});
    },
    async getRun(id) { calls.getRun.push(id); return copy(id === runA.run_id ? runA : runBV3); },
  };
  const {page, summaryHost} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  assert.match(summaryHost.textContent, /当前版本 V2/);

  const selectingB = page.tasks.selectTask(expertTask.task_id);
  await new Promise((resolve) => setImmediate(resolve));
  // A 在这期间被另一个标签页更新到 V3
  listRows = [{...copy(mockTask), current_version_id: versionOfA, current_version_number: 3,
    latest_run_id: 'run-a-v3'}, copy(expertTask)];
  await page.tasks.load();
  releaseB();
  await selectingB;

  assert.equal(page.tasks.getState().selectedId, expertTask.task_id, '用户选的 B 不能被自动核对抢回 A');
  assert.equal(page.getState().selectedId, expertTask.task_id);
  assert.equal(calls.getTask.filter((id) => id === mockTask.task_id).length, 1,
    'A 不应因为自动核对被再次读取');
  assert.match(summaryHost.textContent, /当前版本 V1/, '右侧应显示用户所选 B 的详情（B 当前 V1），而不是被抢回 A');
  page.destroy();
});

test('A 的进度请求在飞时刷新到新版本：落定后补核对，不留旧版本', async () => {
  // 交错时序：A 详情已返回、进度在飞 → 列表刷新到 A 的 V3 → 进度落定。
  // 选中期间的刷新会被防重复逻辑吞掉，必须在选中落定后再核对一次。
  let releaseRun;
  const gateRun = new Promise((resolve) => { releaseRun = resolve; });
  const oldRun = runOf(mockTask.latest_run_id);
  const newVersion = 'version-a-v3';
  const newRun = {...copy(oldRun), run_id: 'run-a-v3', version_id: newVersion};
  let listRow = {...copy(mockTask)};
  let detailRow = {...copy(mockTask), versions: [{version_id: mockTask.current_version_id, number: 2}]};
  const calls = {getRun: [], getTask: 0};
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(listRow)], next_cursor: null}; },
    async getTask() { calls.getTask += 1; return copy(detailRow); },
    async getRun(id) {
      calls.getRun.push(id);
      if (id === oldRun.run_id) { await gateRun; return copy(oldRun); }
      return copy(newRun);
    },
  };
  const {page, runHost, summaryHost} = mount({client});
  await page.ready;
  const selecting = page.selectTask(mockTask.task_id);
  await new Promise((resolve) => setImmediate(resolve));
  // 进度请求已发出并被挂起，此时列表刷新到 V3
  listRow = {...listRow, current_version_id: newVersion, current_version_number: 3, latest_run_id: newRun.run_id};
  detailRow = {...copy(listRow), versions: [{version_id: newVersion, number: 3}]};
  await page.tasks.load();
  assert.match(summaryHost.textContent, /当前版本 V2/, '选中尚未落定时右侧仍是旧详情');
  releaseRun();
  await selecting;
  await new Promise((resolve) => setImmediate(resolve));

  assert.match(summaryHost.textContent, /当前版本 V3/, '落定后必须补核对，不能停在旧版本');
  assert.deepEqual(calls.getRun, [oldRun.run_id, newRun.run_id], '补核对后应改读新执行');
  assert.match(runHost.innerHTML, new RegExp(newRun.run_id));
  page.destroy();
});

test('首次选择时刷新到新版本：页面还没有详情也要登记待核对，落定后补上', async () => {
  // 交错时序：首次点击 A（V2 详情在飞、页面还没有 selectedId/detail）→ 列表刷新到 V3。
  // 若核对在「页面还没有详情」处直接返回，就永远漏掉这次更新。
  let releaseDetail;
  const gate = new Promise((resolve) => { releaseDetail = resolve; });
  const oldVersion = mockTask.current_version_id;
  const newVersion = 'version-first-v3';
  const newRun = {...copy(runOf(mockTask.latest_run_id)), run_id: 'run-first-v3', version_id: newVersion};
  let listRow = {...copy(mockTask)};
  let detailRow = {...copy(mockTask), versions: [{version_id: oldVersion, number: 2}]};
  let detailCalls = 0;
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(listRow)], next_cursor: null}; },
    async getTask() {
      detailCalls += 1;
      // 第一次请求在发出时就取走 V2 快照（模拟「V2 详情在飞」），返回时列表已变成 V3。
      const snapshot = copy(detailRow);
      if (detailCalls === 1) { await gate; }
      return snapshot;
    },
    async getRun(id) { return copy(id === newRun.run_id ? newRun : runOf(mockTask.latest_run_id)); },
  };
  const {page, summaryHost} = mount({client});
  await page.ready;
  const selecting = page.tasks.selectTask(mockTask.task_id);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(page.getState().detail, null, '首屏此时还没有详情');

  // 另一个标签页更新到 V3，列表刷新成功
  listRow = {...listRow, current_version_id: newVersion, current_version_number: 3, latest_run_id: newRun.run_id};
  detailRow = {...copy(listRow), versions: [{version_id: newVersion, number: 3}]};
  await page.tasks.load();
  releaseDetail();
  await selecting;
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(detailCalls, 2, '落定后必须补核对并重读详情');
  assert.match(summaryHost.textContent, /当前版本 V3/, '不能停在首次选择时的 V2');
  page.destroy();
});

test('改选失败后恢复上一个选择，后续刷新仍能核对更新（不留永久待核对）', async () => {
  const oldVersion = mockTask.current_version_id;
  const newVersion = 'version-after-fail-v3';
  const newRun = {...copy(runOf(mockTask.latest_run_id)), run_id: 'run-after-fail-v3', version_id: newVersion};
  let listRow = {...copy(mockTask)};
  let detailRow = {...copy(mockTask), versions: [{version_id: oldVersion, number: 2}]};
  let detailCalls = 0;
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(listRow), copy(expertTask)], next_cursor: null}; },
    async getTask(id) {
      detailCalls += 1;
      if (id === expertTask.task_id) throw {status: 403, message: '当前操作不允许'};
      return copy(detailRow);
    },
    async getRun(id) { return copy(id === newRun.run_id ? newRun : runOf(mockTask.latest_run_id)); },
  };
  const mounted = mount({client});
  await mounted.page.ready;
  await mounted.page.selectTask(mockTask.task_id);
  assert.match(mounted.summaryHost.textContent, /当前版本 V2/);

  // 改选 B 失败：组件回到 A，页面仍显示 A，列表给出错误提示且不再把 B 当已选。
  await mounted.page.tasks.selectTask(expertTask.task_id);
  assert.equal(mounted.page.tasks.getState().selectedId, mockTask.task_id, '失败后应恢复到上一个选择');
  assert.equal(mounted.page.tasks.getState().selectionError?.status, 403);
  assert.equal(mounted.page.getState().selectedId, mockTask.task_id);
  assert.match(mounted.summaryHost.textContent, /当前版本 V2/);

  // 之后列表刷新到 V3：核对必须照常生效（不能因为之前登记过待核对就永久卡住）
  listRow = {...listRow, current_version_id: newVersion, current_version_number: 3, latest_run_id: newRun.run_id};
  detailRow = {...copy(listRow), versions: [{version_id: newVersion, number: 3}]};
  await mounted.page.tasks.load();
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(mounted.summaryHost.textContent, /当前版本 V3/, '失败恢复后仍要能核对到新版本');
  mounted.page.destroy();
});

test('连续切换后失败：回滚到最后成功显示的任务，晚响应不得改写（B 晚成功）', async () => {
  // A 已显示 → 点 B（在飞）→ 点 C（失败）。回滚必须回到 A，不能回到还在加载的 B；
  // B 的晚响应因 selectionToken 过期被丢弃，之后再刷新 A 的新版本要能恢复核对。
  const oldVersion = mockTask.current_version_id;
  const v3 = 'version-after-abc-v3';
  const newRun = {...copy(runOf(mockTask.latest_run_id)), run_id: 'run-after-abc-v3', version_id: v3};
  let listRow = {...copy(mockTask)};
  let detailRow = {...copy(mockTask), versions: [{version_id: oldVersion, number: 2}]};
  let releaseB;
  const gateB = new Promise((resolve) => { releaseB = resolve; });
  const calls = [];
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(listRow), copy(expertTask), {...copy(expertTask), task_id: 'task-C'}]}; },
    async getTask(id) {
      calls.push(id);
      if (id === expertTask.task_id) { await gateB; return copy(expertTask); }
      if (id === 'task-C') throw {status: 403, message: '当前操作不允许'};
      return copy(detailRow);
    },
    async getRun(id) { return copy(id === newRun.run_id ? newRun : runOf(mockTask.latest_run_id)); },
  };
  const {page, summaryHost} = mount({client});
  await page.ready;
  await page.tasks.selectTask(mockTask.task_id);
  assert.match(summaryHost.textContent, /当前版本 V2/);

  const pendingB = page.tasks.selectTask(expertTask.task_id);
  await new Promise((resolve) => setImmediate(resolve));
  await page.tasks.selectTask('task-C'); // 失败
  assert.equal(page.tasks.getState().selectedId, mockTask.task_id, '失败应回滚到 A，而不是还在加载的 B');
  assert.match(summaryHost.textContent, /当前版本 V2/);

  releaseB();
  await pendingB;
  assert.equal(page.tasks.getState().selectedId, mockTask.task_id, 'B 的晚成功必须被丢弃');
  assert.equal(page.getState().selectedId, mockTask.task_id);
  assert.match(summaryHost.textContent, /当前版本 V2/, '右侧仍是 A');

  // 之后 A 更新到 V3：连续两次刷新都要能恢复核对
  listRow = {...listRow, current_version_id: v3, current_version_number: 3, latest_run_id: newRun.run_id};
  detailRow = {...copy(listRow), versions: [{version_id: v3, number: 3}]};
  await page.tasks.load();
  await page.tasks.load();
  assert.match(summaryHost.textContent, /当前版本 V3/, '失败回滚后仍要能核对到新版本');
  assert.deepEqual(calls, [mockTask.task_id, expertTask.task_id, 'task-C', mockTask.task_id],
    '只有回滚后的一次补核对会重读 A');
  page.destroy();
});

test('连续切换后失败：B 晚失败同样不改写当前选择，之后刷新仍能核对（B 晚失败）', async () => {
  const oldVersion = mockTask.current_version_id;
  const v3 = 'version-after-abf-v3';
  const newRun = {...copy(runOf(mockTask.latest_run_id)), run_id: 'run-after-abf-v3', version_id: v3};
  let listRow = {...copy(mockTask)};
  let detailRow = {...copy(mockTask), versions: [{version_id: oldVersion, number: 2}]};
  let releaseB;
  const gateB = new Promise((resolve) => { releaseB = resolve; });
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(listRow), copy(expertTask), {...copy(expertTask), task_id: 'task-C'}]}; },
    async getTask(id) {
      if (id === expertTask.task_id) { await gateB; throw {status: 404, message: '该任务已不可见'}; }
      if (id === 'task-C') throw {status: 403, message: '当前操作不允许'};
      return copy(detailRow);
    },
    async getRun(id) { return copy(id === newRun.run_id ? newRun : runOf(mockTask.latest_run_id)); },
  };
  const {page, summaryHost} = mount({client});
  await page.ready;
  await page.tasks.selectTask(mockTask.task_id);

  const pendingB = page.tasks.selectTask(expertTask.task_id);
  await new Promise((resolve) => setImmediate(resolve));
  await page.tasks.selectTask('task-C');
  releaseB();
  await pendingB;

  assert.equal(page.tasks.getState().selectedId, mockTask.task_id, 'B 的晚失败也不影响回滚结果');
  assert.equal(page.getState().selectedId, mockTask.task_id);
  listRow = {...listRow, current_version_id: v3, current_version_number: 3, latest_run_id: newRun.run_id};
  detailRow = {...copy(listRow), versions: [{version_id: v3, number: 3}]};
  await page.tasks.load();
  assert.match(summaryHost.textContent, /当前版本 V3/);
  page.destroy();
});

test('列表刷新但任务未变时，不重复读详情（避免每次刷新都闪一下右侧）', async () => {
  let getTaskCount = 0;
  const client = {
    async me() { return {display_name: '演示账号'}; },
    async listTasks() { return {items: [copy(mockTask)], next_cursor: null}; },
    async getTask(id) { getTaskCount += 1; return copy(mockTask); },
    async getRun(id) { return copy(runOf(id)); },
  };
  const {page} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  const before = getTaskCount;
  await page.tasks.load();
  assert.equal(getTaskCount, before, '版本与执行都没变时不应重新读详情');
  page.destroy();
});

test('执行记录身份不符时停止更新并报错，不渲染成已完成', async () => {
  const mismatched = {...runOf(expertTask.latest_run_id), run_id: mockTask.latest_run_id};
  const {client} = fakeClient({runs: {[mockTask.latest_run_id]: mismatched}});
  const {page, runHost} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  assert.match(runHost.innerHTML, /不属于所选任务或版本/);
  assert.match(runHost.innerHTML, /role="alert"/);
  assert.doesNotMatch(runHost.innerHTML, /已结束|执行完成/);
  page.destroy();
});

test('列表 401/403/501 走组件既有分类文案，不显示成空数据', async () => {
  const expected = {401: /登录已失效/, 403: /当前操作不允许/, 501: /该功能尚未接通/};
  for (const [status, pattern] of Object.entries(expected)) {
    const {client} = fakeClient({listError: {status: Number(status), message: '服务端拒绝'}});
    const {page, taskHost} = mount({client});
    await page.ready;
    assert.match(taskHost.innerHTML, pattern);
    assert.doesNotMatch(taskHost.innerHTML, /暂无可见任务/);
    page.destroy();
  }
});

test('身份读取失败时显示分类标题与请求标识，仍按只读继续读任务', async () => {
  const {client, calls} = fakeClient({
    runs: dataset.runs,
    meError: {status: 409, message: '会话已更新，请刷新', requestId: '<req-1>'},
  });
  const {page, identityHost, noticeHost, taskHost} = mount({client});
  await page.ready;
  assert.match(identityHost.textContent, /身份读取失败：内容已更新，请刷新/);
  assert.match(noticeHost.innerHTML, /内容已更新，请刷新/);
  assert.match(noticeHost.innerHTML, /请求标识/);
  assert.match(noticeHost.innerHTML, /&lt;req-1&gt;/);
  assert.equal(calls.listTasks.length, 1);
  assert.match(taskHost.innerHTML, /我的任务/);
  page.destroy();
});

test('只读边界：不接重试、不发写请求，失败行只给出未接通提示', async () => {
  const {client, calls, writes} = fakeClient({runs: dataset.runs});
  const {page, runHost} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  assert.match(runHost.innerHTML, /技术失败 1 项/);
  assert.match(runHost.innerHTML, /重试入口待后台接通，当前未发送重试请求/);
  assert.match(runHost.innerHTML, /data-action="retry" disabled/);
  assert.deepEqual(writes, []);
  const failed = runOf(mockTask.latest_run_id).items.filter((item) => item.status === 'failed');
  assert.ok(failed.length > 0);
  assert.deepEqual(calls.getRun, [mockTask.latest_run_id]);
  page.destroy();
});

test('destroy 之后恢复为未选状态、移除监听，不再渲染', async () => {
  const {client} = fakeClient({runs: dataset.runs});
  const {page, taskHost, runHost, noticeHost} = mount({client});
  await page.ready;
  await page.selectTask(mockTask.task_id);
  page.destroy();
  assert.equal(taskHost.listeners.size, 0);
  assert.equal(runHost.listeners.size, 0);
  assert.equal(taskHost.innerHTML, '');
  assert.equal(runHost.innerHTML, '');
  assert.equal(noticeHost.innerHTML, '');
  assert.equal(runHost.hidden, true);
  assert.equal(page.getState().alive, false);
});

/* --------------------- fixture 模式端到端（真实 adapter） ------------------ */

test('fixture 模式端到端：真实 web/api.js adapter + 页面 + 组件能加载 DEMO-SET', async () => {
  const realFetch = globalThis.fetch;
  const fixtureText = readFileSync(FIXTURE_PATH, 'utf8');
  const requested = [];
  // 只替换 fetch 的取数来源；pageOf / getTask / getRun / me 都走 web/api.js 的真实代码。
  globalThis.fetch = async (url) => {
    requested.push(String(url));
    return {ok: true, status: 200, text: async () => fixtureText};
  };
  try {
    const client = createClient({mode: MODE_FIXTURE});
    assert.equal(client.mode, 'fixture');
    const {page, taskHost, runHost, noticeHost, identityHost} = mount({client});
    await page.ready;
    assert.deepEqual(requested, ['/demo/fixtures/DEMO-SET.json']);
    assert.match(identityHost.textContent, /FIXTURE/);
    assert.match(taskHost.innerHTML, /契约演示-主流程/);
    assert.match(taskHost.innerHTML, /契约演示-专家改码/);
    assert.match(noticeHost.innerHTML, /DEMO-SET\.json/);
    await page.selectTask(mockTask.task_id);
    assert.match(runHost.innerHTML, /AI预审/);
    assert.match(runHost.innerHTML, /合成样例 · 非真实AI结论/);
    assert.match(runHost.innerHTML, /是否通过，以人工审核决定为准/);
    page.destroy();
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ------------------ 真实入口：按 p08.html 挂载点执行 bootstrap ---------------- */

test('真实入口装配：用 p08.html 的挂载点运行页面 bootstrap（DOM shim + fixture adapter）', async () => {
  const html = readFileSync(new URL('../../web/pages/p08.html', import.meta.url), 'utf8');
  const declared = new Set([...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]));
  const fixtureText = readFileSync(FIXTURE_PATH, 'utf8');
  const nodes = new Map();
  const documentShim = {
    getElementById(id) {
      if (!declared.has(id)) return null; // 页面引用了 HTML 里不存在的挂载点就会拿不到容器
      if (!nodes.has(id)) nodes.set(id, host());
      return nodes.get(id);
    },
  };
  const pageEvents = new Map();
  const previous = {
    document: globalThis.document,
    location: globalThis.location,
    fetch: globalThis.fetch,
    addEventListener: globalThis.addEventListener,
  };
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  globalThis.document = documentShim;
  globalThis.location = {search: '?mode=fixture'};
  globalThis.addEventListener = (kind, fn) => pageEvents.set(kind, fn);
  globalThis.fetch = async () => ({ok: true, status: 200, text: async () => fixtureText});
  try {
    const until = async (predicate, label) => {
      for (let tick = 0; tick < 60; tick += 1) {
        if (predicate()) return;
        await settle();
      }
      assert.fail(`等待超时：${label}`);
    };
    await import('../../web/pages/p08.js?bootstrap-test');
    // 等首屏落地：身份 + 第一页任务（bootstrap 的 ready 不在全局暴露）。
    await until(() => /契约演示-主流程/.test(nodes.get('p08-tasks')?.innerHTML || ''), '任务列表首屏');
    assert.equal(declared.has('p08-tasks'), true);
    assert.match(nodes.get('p08-mode').textContent, /FIXTURE/);
    assert.match(nodes.get('p08-identity').textContent, /FIXTURE/);
    assert.match(nodes.get('p08-tasks').innerHTML, /契约演示-主流程/);
    assert.match(nodes.get('p08-notice').innerHTML, /DEMO-SET\.json/);
    assert.match(nodes.get('p08-run').innerHTML, /选择任务后查看运行进度/);

    // 通过页面真实监听器模拟一次点击：任务→getTask→getRun→进度渲染。
    nodes.get('p08-tasks').listeners.get('click')({
      target: {closest: () => ({dataset: {task: mockTask.task_id}, disabled: false})},
    });
    await until(() => /AI预审/.test(nodes.get('p08-run').innerHTML || ''), '执行进度');
    assert.match(nodes.get('p08-run').innerHTML, /AI预审/);
    assert.match(nodes.get('p08-run').innerHTML, /合成样例 · 非真实AI结论/);
    assert.match(nodes.get('p08-summary').textContent, /当前版本 V2/);
    assert.match(nodes.get('p08-notice').innerHTML, /合成样例（result_mode=mock）/);
    assert.equal(nodes.get('p08-run').hidden, false);
    assert.equal(typeof pageEvents.get('pagehide'), 'function');
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name];
      else globalThis[name] = value;
    }
  }
});
