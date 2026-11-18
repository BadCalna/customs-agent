/* Execute the actual production page in a VM with deterministic DOM/API/timers.
 * Like P06's page harness and P08/P11's node:test suites, this exercises behavior,
 * not source-string assertions. It does not claim real-browser coverage.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

const source = readFileSync(new URL('../../web/production.js', import.meta.url), 'utf8')
  .replace(/^import .*;\r?\n/gm, '');
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((ok, fail) => {resolve = ok; reject = fail;});
  return {promise, resolve, reject};
};
const task = (id, overrides = {}) => ({
  task_id: id, title: `Task ${id}`, current_version_id: `V${id}`, latest_run_id: `R${id}`,
  stage: 'submitted', revision: 1, result_mode: 'live', supplier: 'supplier',
  versions: [{version_id: `V${id}`, number: 1, original_filename: `${id}.xlsx`}],
  ...overrides,
});
const run = (id, overrides = {}) => ({
  run_id: `R${id}`, task_id: id, version_id: `V${id}`, status: 'failed', kind: 'precheck',
  result_mode: 'live', retry_allowed: true, retry_item_ids: [`item-${id}`],
  progress: {total: 1, succeeded: 0, failed: 1, running: 0, queued: 0}, items: [],
  ...overrides,
});
const history = (id, submissions = [], versions = []) => ({
  task_id: id, audit_events: [],
  versions: [{version_id: `V${id}`, submissions, confirmations: [], runs: [], ...versions[0]}, ...versions.slice(1)],
});

async function harness(overrides = {}) {
  const elements = new Map(), listeners = new Map(), timers = new Map();
  const calls = {getTask: [], getRun: [], retry: [], decide: []};
  let timerId = 0;
  const element = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      hidden: false, innerHTML: '', textContent: '', className: '', listeners: new Map(),
      addEventListener(name, fn) {this.listeners.set(name, fn);},
    });
    return elements.get(selector);
  };
  const api = {
    me: async () => null,
    listTasks: async () => ({items: [], next_cursor: null}),
    listItems: async () => ({items: [], next_cursor: null}),
    getImport: async () => {throw {status: 404};},
    getTask: async id => {calls.getTask.push(id); return task(id);},
    taskHistory: async id => history(id),
    getRun: async id => {calls.getRun.push(id); return run(id.slice(1));},
    retryRun: async (...args) => {calls.retry.push(args);},
    decideSubmission: async (...args) => {calls.decide.push(args);},
    ...overrides,
  };
  // Only FormData's browser-form extraction is stubbed; page control flow is real.
  class TestFormData extends Map {
    constructor(form) {super(Object.entries(form.fields || {}));}
  }
  const context = vm.createContext({
    createClient: () => api,
    document: {
      querySelector: selector => selector === '#decision-form' ? null : element(selector),
      addEventListener: (name, fn) => listeners.set(name, fn),
    },
    FormData: TestFormData, crypto: {randomUUID}, console,
    setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
    clearTimeout(id) {timers.delete(id);},
  });
  vm.runInContext(`${source}\nglobalThis.hooks = {state, openTask, openVersion, decide};`, context);
  await flush();
  const hooks = context.hooks;
  hooks.state.me = {role: 'expert', username: 'expert'};
  return {
    ...hooks, api, calls, element, timers,
    async click(dataset) {
      listeners.get('click')({target: {closest: () => ({dataset})}});
      await flush();
    },
    async tick() {
      const callbacks = [...timers.values()];
      timers.clear();
      await Promise.all(callbacks.map(fn => fn()));
    },
  };
}

function bindDecisionForm(h, form) {
  const [taskId, versionId, submissionId] = JSON.parse(h.state.renderedDecisionScope);
  return {...form, fields: {...form.fields, scope_task_id: taskId,
    scope_version_id: versionId, scope_submission_id: submissionId}};
}

for (const action of ['return', 'approve']) {
  test(`${action}: same submission network retry reuses key; new submission with identical payload gets a new key`, async () => {
    let submission = {submission_id: 'S1', status: 'pending', items: [{item_id: 'item-A', submitted_hs: '1234567890'}]};
    let failOnce = true;
    const calls = [];
    const h = await harness({
      taskHistory: async () => history('A', [submission]),
      async decideSubmission(id, payload, key) {
        calls.push({id, payload, key});
        if (failOnce) {failOnce = false; throw new Error('offline');}
        submission = {...submission, status: action === 'return' ? 'returned' : 'approved'};
      },
    });
    const form = {fields: action === 'return'
      ? {action, return_to: 'operator', reason: '补充资料', issue: '用途不清'}
      : {action, reason: '同意', 'final-item-A': '1234567890', 'reason-item-A': ''}};
    await h.openTask('A');
    const boundForm = bindDecisionForm(h, form);
    await assert.rejects(h.decide(boundForm), /offline/);
    await h.decide(boundForm);
    assert.equal(calls[0].id, 'S1');
    assert.equal(calls[0].key, calls[1].key, 'network retry must retain the original idempotency key');
    submission = {...submission, submission_id: 'S2', status: 'pending'};
    await h.openTask('A');
    await h.decide(bindDecisionForm(h, form));
    assert.equal(calls[2].id, 'S2');
    assert.equal(JSON.stringify(calls[0].payload), JSON.stringify(calls[2].payload));
    assert.notEqual(calls[0].key, calls[2].key, 'new submission is a new decision intent, even on the same version');
  });
}

test('late task A getRun cannot overwrite B state, rendered retry, or the actual retry endpoint', async () => {
  const old = deferred();
  const h = await harness();
  const getRun = h.api.getRun;
  h.api.getRun = id => id === 'RA' ? old.promise : getRun(id);
  const openingA = h.openTask('A');
  await flush(); // A has reached getRun, not just getTask.
  assert.equal(h.state.task.task_id, 'A');
  await h.openTask('B');
  const renderedB = h.element('#run-detail').innerHTML;
  old.resolve(run('A'));
  await openingA;
  assert.equal(h.state.task.task_id, 'B');
  assert.equal(h.state.versionId, 'VB');
  assert.equal(h.state.run.run_id, 'RB');
  assert.equal(h.element('#run-detail').innerHTML, renderedB);
  await h.click({action: 'retry'});
  assert.equal(h.calls.retry.length, 1);
  assert.equal(h.calls.retry[0][0], 'RB');
  assert.deepEqual(Array.from(h.calls.retry[0][1]), ['item-B']);
  assert.equal(h.calls.getTask.at(-1), 'B');
});

test('starting navigation clears the old retry before the next getTask returns', async () => {
  const next = deferred();
  const h = await harness();
  await h.openTask('A');
  assert.match(h.element('#run-detail').innerHTML, /data-action="retry"/);
  h.api.getTask = id => id === 'B' ? next.promise : task(id);
  const openingB = h.openTask('B');
  assert.equal(h.state.run, null);
  assert.doesNotMatch(h.element('#run-detail').innerHTML, /data-action="retry"/);
  await h.click({action: 'retry'}); // stale/detached button or queued click
  assert.equal(h.calls.retry.length, 0);
  next.resolve(task('B'));
  await openingB;
  assert.equal(h.state.run.run_id, 'RB');
});

test('late older-version getRun cannot overwrite the current version on the same task', async () => {
  const old = deferred();
  const h = await harness({
    getTask: async () => task('A', {versions: [
      {version_id: 'VA', number: 2}, {version_id: 'VA-old', number: 1},
    ]}),
    taskHistory: async () => history('A', [], [{},
      {version_id: 'VA-old', submissions: [], runs: [{run_id: 'RA-old'}]},
    ]),
    getRun: id => id === 'RA-old' ? old.promise : Promise.resolve(run('A')),
  });
  const older = h.openTask('A', 'VA-old');
  await flush();
  await h.openVersion('VA');
  old.resolve(run('A', {run_id: 'RA-old', version_id: 'VA-old'}));
  await older;
  assert.equal(h.state.versionId, 'VA');
  assert.equal(h.state.run.run_id, 'RA');
  await h.click({action: 'retry'});
  assert.equal(h.calls.retry[0][0], 'RA');
});

for (const mismatch of [{task_id: 'B'}, {version_id: 'VB'}, {run_id: 'RB'}]) {
  test(`getRun with mismatched ${Object.keys(mismatch)[0]} is not published or retryable`, async () => {
    const h = await harness({getRun: async () => run('A', mismatch)});
    await assert.rejects(h.openTask('A'), /运行结果归属不匹配/);
    assert.equal(h.state.run, null);
    assert.doesNotMatch(h.element('#run-detail').innerHTML, /data-action="retry"/);
    await h.click({action: 'retry'});
    assert.equal(h.calls.retry.length, 0);
  });
}

test('navigation rejects task responses and version selections outside the requested scope', async () => {
  const wrongTask = await harness({getTask: async () => task('B')});
  await assert.rejects(wrongTask.openTask('A'), /任务响应归属不匹配/);
  assert.equal(wrongTask.state.task, null);
  assert.equal(wrongTask.state.run, null);
  const h = await harness();
  await h.openTask('A');
  await assert.rejects(h.openVersion('VB'), /版本不属于当前任务/);
  assert.equal(h.state.run, null);
  assert.deepEqual(h.calls.getRun, ['RA']);
});

test('retry click independently rejects a run that does not belong to the current task/version/run', async () => {
  for (const mismatch of [{task_id: 'B'}, {version_id: 'VB'}, {run_id: 'RB'}, {retry_allowed: false}, {retry_item_ids: []}]) {
    const h = await harness();
    await h.openTask('A');
    h.state.run = run('A', mismatch);
    await h.click({action: 'retry'});
    assert.equal(h.calls.retry.length, 0, JSON.stringify(mismatch));
  }
});

test('a late polling response is discarded after navigating to another task', async () => {
  const old = deferred();
  const h = await harness({getRun: async id => run(id.slice(1), id === 'RA' ? {status: 'running'} : {})});
  await h.openTask('A');
  assert.equal(h.timers.size, 1);
  h.api.getRun = id => id === 'RA' ? old.promise : Promise.resolve(run('B'));
  const polling = h.tick();
  await h.openTask('B');
  old.resolve(run('A'));
  await polling;
  assert.equal(h.state.task.task_id, 'B');
  assert.equal(h.state.run.run_id, 'RB');
  assert.equal(h.timers.size, 0);
});

test('polling never publishes a response belonging to another task/version', async () => {
  const h = await harness({getRun: async () => run('A', {status: 'running'})});
  await h.openTask('A');
  h.api.getRun = async () => run('B');
  await h.tick();
  assert.equal(h.state.run.run_id, 'RA');
  assert.match(h.element('#notice').textContent, /运行结果归属不匹配/);
  assert.equal(h.timers.size, 0);
});
