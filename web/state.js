/* =============================================================================
 * web/state.js —— 页面状态与请求编排
 *
 * 两条口径：
 * 1. 所有异步结果先校验"请求令牌 + 对象身份"再写回状态。版本切换后晚到的
 *    V1 响应不能覆盖 V2 卡片（D3 第 4 节 FIX-09）。
 * 2. 状态变化通过订阅通知渲染层；渲染层只读状态，不直接调 client。
 * ========================================================================== */

export function createStore(initial) {
  let state = { ...initial };
  const listeners = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...patch };
      for (const listener of listeners) listener(state);
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}

const EMPTY_DETAIL = {
  taskId: null,
  task: null,
  taskError: null,
  versionId: null,
  items: [],
  itemsCursor: null,
  itemsError: null,
  itemId: null,
  card: null,
  cardError: null,
  runId: null,
  run: null,
  runError: null,
};

export function createWorkspace(client) {
  const store = createStore({
    mode: client.mode,
    me: null,
    busy: false,
    authError: null,
    tasks: [],
    tasksCursor: null,
    tasksLoading: false,
    tasksError: null,
    ...EMPTY_DETAIL,
  });

  // 每个请求链一个令牌：新请求发出后，旧响应即使成功也直接丢弃。
  let tasksToken = 0;
  let taskToken = 0;
  let itemsToken = 0;
  let cardToken = 0;
  let runToken = 0;

  function isCurrent(key, value) {
    return store.get()[key] === value;
  }

  async function loadTasks({ append = false } = {}) {
    const token = ++tasksToken;
    store.set({ tasksLoading: true, tasksError: null });
    try {
      const page = await client.listTasks({
        limit: 20,
        cursor: append ? store.get().tasksCursor : undefined,
      });
      if (token !== tasksToken) return;
      store.set({
        tasks: append ? [...store.get().tasks, ...page.items] : page.items,
        tasksCursor: page.next_cursor,
        tasksLoading: false,
      });
    } catch (error) {
      if (token !== tasksToken) return;
      store.set({ tasksError: error, tasksLoading: false });
    }
  }

  async function openTask(taskId) {
    const token = ++taskToken;
    itemsToken += 1; // 任务切换后旧版本请求作废
    cardToken += 1;
    runToken += 1;
    store.set({ ...EMPTY_DETAIL, taskId, taskError: null });
    try {
      const task = await client.getTask(taskId);
      if (token !== taskToken || !isCurrent("taskId", taskId)) return;
      store.set({ task });
      const versionId = task.current_version_id || (task.versions[0] && task.versions[0].version_id) || null;
      if (versionId) await openVersion(versionId);
    } catch (error) {
      if (token !== taskToken || !isCurrent("taskId", taskId)) return;
      store.set({ taskError: error });
    }
  }

  async function openVersion(versionId) {
    const token = ++itemsToken;
    cardToken += 1;
    store.set({ versionId, items: [], itemsCursor: null, itemsError: null, itemId: null, card: null, cardError: null });
    try {
      const page = await client.listItems(versionId, { limit: 100 });
      if (token !== itemsToken || !isCurrent("versionId", versionId)) return;
      store.set({ items: page.items, itemsCursor: page.next_cursor });
    } catch (error) {
      if (token !== itemsToken || !isCurrent("versionId", versionId)) return;
      store.set({ itemsError: error });
    }
  }

  async function selectItem(itemId) {
    const token = ++cardToken;
    const versionId = store.get().versionId;
    store.set({ itemId, card: null, cardError: null });
    try {
      const card = await client.getItem(versionId, itemId);
      if (token !== cardToken || !isCurrent("itemId", itemId)) return;
      if (card.version_id !== store.get().versionId) return; // 双保险：版本已变
      store.set({ card });
    } catch (error) {
      if (token !== cardToken || !isCurrent("itemId", itemId)) return;
      store.set({ cardError: error });
    }
  }

  async function openRun(runId) {
    const token = ++runToken;
    store.set({ runId, run: null, runError: null });
    try {
      const run = await client.getRun(runId);
      if (token !== runToken || !isCurrent("runId", runId)) return;
      store.set({ run });
    } catch (error) {
      if (token !== runToken || !isCurrent("runId", runId)) return;
      store.set({ runError: error });
    }
  }

  function closeRun() {
    runToken += 1;
    store.set({ runId: null, run: null, runError: null });
  }

  async function bootstrap() {
    store.set({ busy: true, authError: null });
    try {
      const me = await client.me();
      store.set({ me, busy: false });
      if (me) await loadTasks();
    } catch (error) {
      store.set({ authError: error, busy: false });
    }
  }

  async function login(username, password) {
    store.set({ busy: true, authError: null });
    try {
      const me = await client.login(username, password);
      store.set({ me, busy: false });
      await loadTasks();
    } catch (error) {
      store.set({ authError: error, busy: false });
    }
  }

  async function logout() {
    try {
      await client.logout();
    } finally {
      tasksToken += 1;
      taskToken += 1;
      itemsToken += 1;
      cardToken += 1;
      runToken += 1;
      store.set({ me: null, tasks: [], tasksCursor: null, tasksError: null, ...EMPTY_DETAIL });
    }
  }

  return { store, loadTasks, openTask, openVersion, selectItem, openRun, closeRun, bootstrap, login, logout };
}
