/** P19: recipient-scoped inbox. Destroy on logout or any account change. */
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const validMessage = row => row && typeof row.message_id === 'string' && !!row.message_id
  && typeof row.title === 'string' && typeof row.body === 'string' && typeof row.is_read === 'boolean';
const messageTime = value => {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? date.toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '时间待确认';
};

export function renderMessageList(host, state) {
  host.innerHTML = `<section class="p19-inbox" aria-label="站内消息" aria-busy="${state.loading}">
    <header class="p19-heading"><h2>站内消息 <span class="p19-count">${state.unreadCount} 条未读</span></h2>
      <button type="button" data-message-action="refresh" ${state.loading ? 'disabled' : ''}>刷新消息</button></header>
    ${state.error ? `<p class="p19-error" role="alert">${esc(state.error.message || '消息读取失败，请重试。')}</p>` : ''}
    <div class="p19-items">${state.items.map(row => `<article class="p19-message ${row.is_read ? 'is-read' : 'is-unread'}">
      <div class="p19-message-heading"><strong>${esc(row.title)}</strong><span>${row.is_read ? '已读' : '未读'}</span></div>
      <p>${esc(row.body)}</p><small>${esc(messageTime(row.created_at))}</small>
      <div class="p19-message-actions">${row.task_id && row.version_id ? `<button type="button" data-message-action="open" data-message-id="${esc(row.message_id)}">查看关联版本</button>` : ''}
      ${!row.is_read ? `<button type="button" data-message-action="read" data-message-id="${esc(row.message_id)}" ${state.reading.has(row.message_id) ? 'disabled' : ''}>${state.reading.has(row.message_id) ? '正在标记…' : '标为已读'}</button>` : ''}</div>
    </article>`).join('') || (!state.loading && !state.error ? '<p class="p19-empty">暂无站内消息</p>' : '')}</div>
    ${state.loading ? '<p role="status">正在读取消息…</p>' : ''}
    ${state.nextCursor ? `<button type="button" data-message-action="more" ${state.loading ? 'disabled' : ''}>加载更多消息</button>` : ''}
  </section>`;
}

export function createMessageList(host, {client, onOpen = () => {}, onError = () => {}, onCount = () => {}} = {}) {
  if (!host || !client?.listMessages || !client?.markMessageRead) throw new TypeError('需要消息容器和统一消息 client');
  let alive = true, token = 0;
  let state = {items: [], nextCursor: null, unreadCount: 0, loading: false, error: null, reading: new Set()};
  const draw = () => {if (alive) {renderMessageList(host, state); onCount(state.unreadCount);}};
  const report = error => {if (alive) {state = {...state, error}; draw(); onError(error);}};
  async function load(append = false) {
    if (!alive || (append && (state.loading || !state.nextCursor))) return;
    const request = ++token, cursor = append ? state.nextCursor : null;
    state = {...state, loading: true, error: null}; draw();
    try {
      const page = await client.listMessages({limit: 20, cursor});
      if (!alive || request !== token) return;
      if (!Array.isArray(page?.items) || !page.items.every(validMessage)
          || !Number.isSafeInteger(page.unread_count) || page.unread_count < 0
          || (page.next_cursor !== null && typeof page.next_cursor !== 'string')
          || (page.next_cursor && page.next_cursor === cursor)) throw new Error('消息响应格式异常，请刷新重试。');
      const rows = append ? [...state.items, ...page.items] : page.items;
      const seen = new Set();
      const items = rows.filter(row => {if (seen.has(row.message_id)) return false; seen.add(row.message_id); return true;});
      state = {...state, items, unreadCount: page.unread_count, nextCursor: page.next_cursor, loading: false}; draw();
    } catch (error) {
      if (!alive || request !== token) return;
      state = {...state, loading: false}; report(error);
    }
  }
  async function markRead(id) {
    const row = state.items.find(item => item.message_id === id);
    if (!alive || !row || row.is_read || state.reading.has(id)) return;
    state.reading.add(id); state = {...state, error: null}; draw();
    try {
      const result = await client.markMessageRead(id);
      if (!alive) return;
      if (!validMessage(result) || result.message_id !== id || !result.is_read
          || result.task_id !== row.task_id || result.version_id !== row.version_id) throw new Error('已读响应与消息不符，请刷新核对。');
      // A list request started before this write may contain an obsolete unread state.
      token++;
      const current = state.items.find(item => item.message_id === id);
      state = {...state, loading: false, unreadCount: Math.max(0, state.unreadCount - (current && !current.is_read ? 1 : 0)),
        items: state.items.map(item => item.message_id === id ? result : item)};
    } catch (error) {if (alive) report(error);}
    finally {if (alive) {state.reading.delete(id); draw();}}
  }
  async function open(id) {
    const row = state.items.find(item => item.message_id === id);
    if (!alive || !row?.task_id || !row?.version_id) return;
    const target = Object.freeze({...row});
    try {await onOpen(target);} catch (error) {if (alive) report(error);}
  }
  const click = event => {
    const button = event.target.closest?.('[data-message-action]');
    if (!button || button.disabled || !alive) return;
    const action = button.dataset.messageAction;
    if (action === 'refresh') void load();
    if (action === 'more') void load(true);
    if (action === 'read') void markRead(button.dataset.messageId);
    if (action === 'open') void open(button.dataset.messageId);
  };
  host.addEventListener('click', click); draw();
  return {load, markRead, open, getState: () => ({...state, items: [...state.items], reading: new Set(state.reading)}),
    destroy() {alive = false; token++; host.removeEventListener('click', click); host.replaceChildren();}};
}
