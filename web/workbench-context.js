/** Formal workbench selection. History is the published source of version/run/snapshot identity. */
import {createStore} from './state.js';

export function runsFor(history, versionId, kind) {
  return (history?.versions?.find(v => v.version_id === versionId)?.runs || []).filter(r => r.kind === kind);
}
export function defaultRun(task, history, versionId, kind) {
  const rows = runsFor(history, versionId, kind);
  if (versionId === task?.current_version_id && task.latest_run_kind === kind && rows.some(r => r.run_id === task.latest_run_id)) return task.latest_run_id;
  const current = rows.filter(r => r.is_current);
  return current.length === 1 ? current[0].run_id : rows.length === 1 ? rows[0].run_id : null;
}
const blank = {taskId: null, task: null, history: null, versionId: null, items: [], itemId: null, card: null,
  kind: 'precheck', runId: null, run: null, progressError: null, decisionId: null, error: null, loading: false};

export function createWorkbenchContext(client) {
  const store = createStore(blank);
  let alive = true, epoch = 0, cardTicket = 0;
  const active = (n) => alive && epoch === n;
  function beginTask(taskId) { epoch++; cardTicket++; store.set({...blank, taskId, loading: !!taskId}); }
  function failTask(taskId, error) {
    if (!alive) return;
    if (taskId && store.get().taskId !== taskId) return;
    epoch++; cardTicket++;
    store.set({...blank, taskId: taskId || null, error: error || new Error('任务读取失败'), loading: false});
  }
  async function acceptTask(task, {preserveItemId = null, preserveVersionId = null} = {}) {
    if (!alive || store.get().taskId !== task.task_id) return;
    const n = epoch;
    store.set({task});
    try {
      const history = await client.getHistory(task.task_id);
      if (!active(n)) return;
      if (history.task_id !== task.task_id) throw new Error('任务历史身份不匹配');
      store.set({history, kind: ['precheck','expert_check'].includes(task.latest_run_kind) ? task.latest_run_kind : 'precheck'});
      if (task.current_version_id) {
        if (!task?.versions?.some(v => v.version_id === task.current_version_id)
            || !history?.versions?.some(v => v.version_id === task.current_version_id)) {
          throw new Error('任务历史缺少当前版本记录');
        }
        const keepItem = preserveVersionId && preserveVersionId === task.current_version_id ? preserveItemId : null;
        await openVersion(task.current_version_id, {preserveItemId: keepItem});
      } else {
        store.set({loading: false});
      }
    } catch (error) { if (active(n)) store.set({error, loading: false}); }
  }
  async function openTask(id, {preserveItem = false, preserveVersionId = null, preserveItemId = null} = {}) {
    const prev = preserveItem ? store.get() : null;
    const keepVersionId = preserveVersionId || (prev && prev.taskId === id ? prev.versionId : null);
    const keepItemId = preserveItemId || (prev && prev.taskId === id ? prev.itemId : null);
    beginTask(id); const n = epoch;
    try { const task = await client.getTask(id); if (active(n)) {
      if (task.task_id !== id) throw new Error('任务身份不匹配');
      await acceptTask(task, {preserveVersionId: keepVersionId, preserveItemId: keepItemId});
    }} catch (error) { if (active(n)) store.set({error, loading: false}); }
  }
  async function openVersion(versionId, {preserveItemId = null} = {}) {
    const {task, history, kind} = store.get();
    if (!alive || !task?.versions?.some(v => v.version_id === versionId) || !history?.versions?.some(v => v.version_id === versionId)) return;
    const n = ++epoch; cardTicket++;
    store.set({versionId, items: [], itemId: null, card: null, runId: defaultRun(task, history, versionId, kind),
      run: null, progressError: null, decisionId: null, error: null, loading: true});
    try {
      const items = [], cursors = new Set(); let cursor = null;
      do {
        const page = await client.listItems(versionId, {limit: 100, cursor});
        if (!active(n)) return;
        if (!Array.isArray(page.items) || page.items.some(i => i.task_id !== task.task_id || i.version_id !== versionId)) throw new Error('商品列表任务或版本身份不匹配');
        items.push(...page.items); cursor = page.next_cursor;
        if (cursor && cursors.has(cursor)) throw new Error('服务端分页游标重复');
        cursors.add(cursor);
      } while (cursor);
      if (new Set(items.map(i => i.item_id)).size !== items.length) throw new Error('商品列表重复');
      const keepItem = preserveItemId && items.some(i => i.item_id === preserveItemId) ? preserveItemId : null;
      store.set({items, itemId: keepItem, loading: false});
      if (keepItem) await selectItem(keepItem);
    } catch (error) { if (active(n)) store.set({error, loading: false}); }
  }
  function chooseRun(runId, kind = store.get().kind) {
    const s = store.get();
    if (!alive || !['precheck','expert_check'].includes(kind)) return;
    if (runId && !runsFor(s.history, s.versionId, kind).some(r => r.run_id === runId)) return;
    cardTicket++;
    store.set({kind, runId: runId || null, run: null, card: null, itemId: null, decisionId: null, progressError: null});
  }
  function chooseKind(kind) { const s = store.get(); chooseRun(defaultRun(s.task, s.history, s.versionId, kind), kind); }
  async function selectItem(itemId, identity = null) {
    const s = store.get(), n = epoch, ticket = ++cardTicket;
    if (!alive || !s.items.some(i => i.item_id === itemId)) return;
    if (identity && (identity.task_id !== s.taskId || identity.version_id !== s.versionId || identity.run_id !== s.runId)) return;
    store.set({itemId, card: null});
    try {
      const card = await client.getItem(s.versionId, itemId);
      if (!active(n) || ticket !== cardTicket) return;
      if (card.task_id !== s.taskId || card.version_id !== s.versionId || card.item_id !== itemId) throw new Error('商品卡片身份不匹配');
      store.set({card, error: null});
    } catch (error) { if (active(n) && ticket === cardTicket) store.set({error}); }
  }
  function acceptProgress(p) {
    const s = store.get(), c = p.context;
    if (!alive || !c || c.taskId !== s.taskId || c.versionId !== s.versionId || c.runId !== s.runId) return;
    if (p.run && p.run.kind !== s.kind) { store.set({run: null, progressError: new Error('执行阶段与所选阶段不符')}); return; }
    store.set({run: p.run, progressError: p.error || (p.loading ? new Error('正在读取执行结果') : null)});
  }
  function chooseDecision(id) {
    const s = store.get(); const sub = s.history?.versions.find(v => v.version_id === s.versionId)?.submissions.find(x => x.submission_id === s.run?.submission_id);
    if (!id || sub?.decisions.some(d => d.decision_id === id)) store.set({decisionId: id || null});
  }
  return {store, beginTask, failTask, acceptTask, openTask, openVersion, chooseKind, chooseRun, selectItem, acceptProgress, chooseDecision,
    destroy() {alive = false; epoch++; cardTicket++;}};
}
