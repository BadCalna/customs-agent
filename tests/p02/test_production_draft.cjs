const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../web/production.js'), 'utf8')
  .replace(/^import .*;\r?\n/m, '')
  .replace('const api = createClient({mode: "live"});', 'const api = globalThis.TEST_API;');
assert.match(source, /const api = globalThis\.TEST_API;/);

function decode(value) {
  return value.replace(/&(amp|lt|gt|quot|#39);/g, (_, entity) =>
    ({amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'"})[entity]);
}

function attributes(source) {
  return Object.fromEntries([...source.matchAll(/([\w-]+)="([^"]*)"/g)]
    .map(([, name, value]) => [name, decode(value)]));
}

function decisionForm(html) {
  const body = html.match(/<form id="decision-form"[^>]*>([\s\S]*?)<\/form>/)?.[1];
  if (!body) return null;
  const controls = new Map();
  for (const [, raw] of body.matchAll(/<input\b([^>]*)>/g)) {
    const attrs = attributes(raw);
    if (attrs.name) controls.set(attrs.name, {value: attrs.value || ''});
  }
  for (const [, raw, content] of body.matchAll(/<textarea\b([^>]*)>([\s\S]*?)<\/textarea>/g)) {
    const attrs = attributes(raw);
    controls.set(attrs.name, {value: decode(content)});
  }
  for (const [, raw, content] of body.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)) {
    const attrs = attributes(raw);
    const options = [...content.matchAll(/<option\b([^>]*)>/g)].map(([, option]) => ({
      value: attributes(option).value, selected: /\bselected\b/.test(option),
    }));
    controls.set(attrs.name, {value: (options.find(option => option.selected) || options[0]).value});
  }
  return {id: 'decision-form', controls};
}

class TestFormData {
  constructor(form) {
    this.fields = new Map([...form.controls].map(([name, control]) => [name, control.value]));
  }
  get(name) { return this.fields.get(name) ?? null; }
  [Symbol.iterator]() { return this.fields[Symbol.iterator](); }
}

async function harness() {
  const elements = new Map();
  const handlers = new Map();
  const timers = [];
  const decisions = [];
  let form = null;
  let runStatus = 'running';
  let submissionId = 'submission-1';
  let taskId = 'task-1';
  let versionId = 'version-1';
  const itemId = 'item-1';
  const element = selector => {
    if (!elements.has(selector)) {
      const node = {hidden: false, textContent: '', className: '', addEventListener() {}, reset() {}};
      Object.defineProperty(node, 'innerHTML', {
        get() { return this.html || ''; },
        set(html) {
          this.html = html;
          if (selector === '#task-detail') form = decisionForm(html);
        },
      });
      elements.set(selector, node);
    }
    return elements.get(selector);
  };
  const api = {
    me: async () => ({id: 'expert-1', username: 'expert', role: 'expert', role_label: 'Expert'}),
    listTasks: async () => ({items: [], next_cursor: null}),
    getTask: async id => ({task_id: id, title: 'Review', current_version_id: versionId,
      current_version_number: 1, stage: 'submitted', result_mode: 'live', latest_run_id: 'run-1',
      versions: [{version_id: versionId, number: 1, original_filename: 'input.xlsx'}],
      supplier: 'Supplier', revision: 1}),
    listItems: async () => ({items: [{item_id: itemId, name: 'Item', source_location: {
      sheet_name: 'Sheet', row_number: 1}, submitted_hs: '1111111111'}], next_cursor: null}),
    taskHistory: async () => ({versions: [{version_id: versionId, submissions: [{
      submission_id: submissionId, status: 'pending', items: [{item_id: itemId, submitted_hs: '1111111111'}],
    }], runs: []}], audit_events: []}),
    getImport: async () => { throw {status: 404}; },
    getRun: async () => ({run_id: 'run-1', task_id: taskId, version_id: versionId,
      kind: 'expert_check', status: runStatus,
      progress: {total: 1, succeeded: Number(runStatus === 'succeeded'), failed: 0,
        running: Number(runStatus === 'running'), queued: 0},
      items: [], retry_allowed: false, retry_item_ids: [], result_mode: 'live'}),
    decideSubmission: async (id, payload, key) => { decisions.push({id, payload, key}); },
  };
  const context = {TEST_API: api, crypto, FormData: TestFormData,
    document: {querySelector: selector => selector === '#decision-form' ? form : element(selector),
      addEventListener: (name, handler) => handlers.set(name, handler)},
    setTimeout: handler => {
      const timer = {handler, canceled: false, fired: false};
      timers.push(timer);
      return timer;
    },
    clearTimeout: timer => { if (timer) timer.canceled = true; },
  };
  vm.createContext(context);
  vm.runInContext(source + '\nglobalThis.hooks = {openTask};', context);
  await new Promise(resolve => setImmediate(resolve));
  await context.hooks.openTask(taskId);
  const currentForm = () => {
    assert.ok(form, 'decision form should be visible');
    return form;
  };
  return {
    currentForm, decisions, api,
    status: () => vm.runInContext('state.run?.status', context),
    set(name, value) { currentForm().controls.get(name).value = value; },
    value(name) { return currentForm().controls.get(name).value; },
    mode(value) {
      handlers.get('click')({target: {dataset: {decisionMode: value}, closest() { return this; }}});
    },
    async poll() {
      const timer = timers.find(entry => !entry.canceled && !entry.fired);
      assert.ok(timer, 'active poll should be scheduled');
      timer.fired = true;
      const completion = timer.handler();
      assert.equal(typeof completion?.then, 'function');
      await completion;
    },
    finish() { runStatus = 'succeeded'; },
    async refresh(submission = submissionId) {
      submissionId = submission;
      await context.hooks.openTask(taskId);
    },
    async switchScope({task = taskId, version = versionId, submission = submissionId}) {
      taskId = task;
      versionId = version;
      submissionId = submission;
      await context.hooks.openTask(taskId);
    },
    async newSession() {
      vm.runInContext('state.me = null; showSession()', context);
      assert.equal(form, null, 'logout should remove the old decision form');
      vm.runInContext('state.me = {id: "expert-2", username: "expert-2", role: "expert", role_label: "Expert"}; showSession()', context);
      await context.hooks.openTask(taskId);
    },
    async submitForm(target) {
      handlers.get('submit')({target, preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
    },
    async submit() {
      const target = currentForm();
      handlers.get('submit')({target, preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
    },
    openTask(id) { return context.hooks.openTask(id); },
  };
}

(async () => {
  const approve = await harness();
  approve.set('final-item-1', '2222222222');
  approve.set('reason-item-1', 'synthetic edit & checked');
  approve.set('reason', 'review note');
  const runningForm = approve.currentForm();
  assert.equal(approve.status(), 'running');
  await approve.poll();
  assert.equal(approve.status(), 'running');
  assert.equal(approve.currentForm(), runningForm, 'running poll should not rebuild the form');
  assert.equal(approve.value('final-item-1'), '2222222222');
  approve.finish();
  await approve.poll();
  assert.notEqual(approve.currentForm(), runningForm, 'completion refresh should rebuild the form');
  assert.equal(approve.value('final-item-1'), '2222222222');
  assert.equal(approve.value('reason-item-1'), 'synthetic edit & checked');
  assert.equal(approve.value('reason'), 'review note');
  await approve.submit();
  assert.equal(approve.decisions[0].id, 'submission-1');
  assert.equal(approve.decisions[0].payload.items[0].final_hs, '2222222222');
  assert.equal(approve.decisions[0].payload.items[0].change_reason, 'synthetic edit & checked');
  assert.equal(approve.decisions[0].payload.reason, 'review note');
  assert.equal(approve.value('final-item-1'), '1111111111', 'successful POST should clear the draft');

  const missingReason = await harness();
  missingReason.set('final-item-1', '9999999999');
  missingReason.finish();
  await missingReason.poll();
  await missingReason.submit();
  assert.equal(missingReason.decisions.length, 0, 'changed code without reason must not POST');
  assert.equal(missingReason.value('final-item-1'), '9999999999');
  missingReason.set('reason-item-1', 'required explanation');
  await missingReason.submit();
  assert.equal(missingReason.decisions[0].payload.items[0].final_hs, '9999999999');
  assert.equal(missingReason.decisions[0].payload.items[0].change_reason, 'required explanation');

  const returned = await harness();
  returned.set('final-item-1', '3333333333');
  returned.set('reason-item-1', 'approve reason');
  returned.mode('return');
  returned.set('return_to', 'supplier');
  returned.set('reason', 'missing documents');
  returned.set('issue', 'invoice mismatch');
  await returned.poll();
  returned.finish();
  await returned.poll();
  assert.equal(returned.value('action'), 'return');
  assert.equal(returned.value('return_to'), 'supplier');
  assert.equal(returned.value('reason'), 'missing documents');
  assert.equal(returned.value('issue'), 'invoice mismatch');
  returned.mode('approve');
  assert.equal(returned.value('final-item-1'), '3333333333');
  assert.equal(returned.value('reason-item-1'), 'approve reason');
  returned.mode('return');
  assert.equal(returned.value('issue'), 'invoice mismatch');
  await returned.submit();
  assert.equal(returned.decisions[0].payload.action, 'return');
  assert.equal(returned.decisions[0].payload.return_to, 'supplier');
  assert.equal(returned.decisions[0].payload.reason, 'missing documents');
  assert.equal(returned.decisions[0].payload.issues[0].message, 'invoice mismatch');

  const scope = await harness();
  scope.set('final-item-1', '4444444444');
  await scope.refresh('submission-2');
  assert.equal(scope.value('final-item-1'), '1111111111', 'new submission must not inherit prior draft');
  await scope.refresh('submission-1');
  assert.equal(scope.value('final-item-1'), '4444444444', 'same submission should recover its draft');
  await scope.switchScope({version: 'version-2'});
  assert.equal(scope.value('final-item-1'), '1111111111', 'new version must not inherit prior draft');
  await scope.switchScope({version: 'version-1', task: 'task-2'});
  assert.equal(scope.value('final-item-1'), '1111111111', 'new task must not inherit prior draft');
  await scope.switchScope({task: 'task-1'});
  assert.equal(scope.value('final-item-1'), '4444444444', 'original task scope should recover its draft');
  await scope.newSession();
  assert.equal(scope.value('final-item-1'), '1111111111', 'new session must not inherit prior draft');

  // P1: a form rendered for S1 must never be rebound to the new S2 submission.
  const stale = await harness();
  const oldSubmissionForm = stale.currentForm();
  await stale.switchScope({submission: 'submission-2'});
  assert.notEqual(stale.currentForm(), oldSubmissionForm);
  await stale.submitForm(oldSubmissionForm);
  assert.equal(stale.decisions.length, 0, 'detached S1 form must not decide current S2');

  // The same protection must apply while the next task is still loading: the old
  // task remains in state briefly, but its write controls are already invalid.
  const loading = await harness();
  const loadingForm = loading.currentForm();
  let releaseTask;
  loading.api.getTask = id => new Promise(resolve => {
    loading.api.getRun = async () => ({run_id: 'run-1', task_id: id, version_id: 'version-1',
      kind: 'expert_check', status: 'succeeded',
      progress: {total: 1, succeeded: 1, failed: 0, running: 0, queued: 0},
      items: [], retry_allowed: false, retry_item_ids: [], result_mode: 'live'});
    releaseTask = () => resolve({task_id: id, title: 'Review B', current_version_id: 'version-1',
      current_version_number: 1, stage: 'submitted', result_mode: 'live', latest_run_id: 'run-1',
      versions: [{version_id: 'version-1', number: 1, original_filename: 'input.xlsx'}],
      supplier: 'Supplier', revision: 1});
  });
  const navigating = loading.openTask('task-2');
  await loading.submitForm(loadingForm);
  assert.equal(loading.decisions.length, 0, 'navigation must invalidate old form before getTask resolves');
  releaseTask();
  await navigating;
  await loading.submit();
  assert.equal(loading.decisions.length, 1, 'freshly bound form remains usable');

  console.log('PASS: running/completed polls, approve/return drafts, scoped refresh and stale-form rejection');
})().catch(error => { console.error(error); process.exitCode = 1; });
