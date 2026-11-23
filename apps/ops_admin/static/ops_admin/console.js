import {busy, call, errorText, feedback, newKey, requestRef} from "./client.js";

const capabilities = JSON.parse(document.querySelector("#ops-capabilities").textContent);
const operatorId = document.querySelector("#ops-main").dataset.operatorId;
const can = (name) => Boolean(capabilities[name]);
const globalFeedback = document.querySelector("#global-feedback");
const links = Array.from(document.querySelectorAll("[data-section]"));
const titles = {overview: "运行概览", users: "账号目录", groups: "系统组", audit: "审计事件", errors: "异常日志", modules: "模块登记"};
const dialogs = document.querySelector("#ops-dialog");
const dialogTitle = document.querySelector("#dialog-title");
const dialogBody = document.querySelector("#dialog-body");
const dialogFeedback = document.querySelector("#dialog-feedback");
const dismiss = dialogs.querySelector(".dialog-close");
const endpoints = {overview: "/dashboard", users: "/users", groups: "/groups", audit: "/audit", errors: "/errors", modules: "/modules"};
const roleNames = {supplier: "供应商", operator: "业务员", expert: "专家", "": "非业务"};
const dateFormat = new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short", hour12: false});
const labels = {
  active: ["活跃", "good"], disabled: ["已禁用", "warn"], pending_secret: ["待设密", "warn"],
  available: ["可用", "good"], degraded: ["降级", "warn"], unavailable: ["不可用", "error"],
  success: ["成功", "good"], failure: ["失败", "error"], denied: ["已拒绝", "warn"],
  resolved: ["已处理", "good"], open: ["待处理", "warn"], unregistered: ["未登记", "warn"],
};
const views = Object.fromEntries(links.map(({dataset}) => {
  const key = dataset.section;
  const section = document.getElementById(key);
  return [key, {key, section, status: section.querySelector("[data-status]"), results: section.querySelector("[data-results]"),
    next: section.querySelector("[data-next]"), exportButton: section.querySelector("[data-export]"),
    filter: section.querySelector("[data-filter]"), items: [], filters: {}, sequence: 0}];
}));
let selected;
let dialogEpoch = 0;
let dialogBusy = false;
let forgetTicket;
let filterGroupsLoaded = false;

function node(tag, className = "", text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null && text !== "") element.textContent = String(text);
  return element;
}
function append(parent, children) {
  for (const child of children) {
    if (child != null) parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}
function button(text, listener, className = "utility") {
  const element = node("button", className, text);
  element.type = "button";
  element.addEventListener("click", listener);
  return element;
}
function short(value) {return !value ? "—" : String(value).length > 13 ? `${String(value).slice(0, 9)}…` : String(value);}
function date(value) {
  const timestamp = value ? new Date(value) : null;
  return timestamp && !Number.isNaN(timestamp.valueOf()) ? dateFormat.format(timestamp) : "—";
}
function count(value) {return Number.isFinite(value) ? value.toLocaleString("zh-CN") : "—";}
function role(value) {return Object.hasOwn(roleNames, value) ? roleNames[value] : "—";}
function groupsOf(user) {return Array.isArray(user?.groups) ? user.groups : [];}
function groupNames(groups) {return groups.map((g) => g?.name || (typeof g === "string" ? g : "")).filter(Boolean).join("、") || "—";}
function code(value) {return node("code", "", value || "—");}
function badge(key) {
  const [text, tone] = Object.hasOwn(labels, key) ? labels[key] : ["未知", ""];
  const element = node("span", "badge", text);
  element.dataset.tone = tone;
  return element;
}
function empty(text) {return node("p", "empty-state", text);}
function facts(entries) {
  const dl = node("dl", "fact-list");
  for (const [term, value] of entries) append(dl, [node("dt", "", term), append(node("dd"), [value ?? "—"])]);
  return dl;
}
function idCopy(id) {
  if (!id) return "—";
  const copy = button(short(id), async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error();
      await navigator.clipboard.writeText(String(id));
      feedback(globalFeedback, "完整账号 UUID 已复制。");
    } catch {
      feedback(globalFeedback, `完整账号 UUID：${String(id)}`, "", true);
    }
  }, "copy-short");
  copy.setAttribute("aria-label", `复制账号 UUID ${short(id)}`);
  return copy;
}
function ledger(label, headings, rows) {
  const wrapper = node("div", "table-scroll");
  wrapper.tabIndex = 0;
  wrapper.setAttribute("role", "region");
  wrapper.setAttribute("aria-label", `${label}列表`);
  const table = node("table");
  const header = node("tr");
  for (const label of headings) {
    const cell = node("th", "", label);
    cell.scope = "col";
    header.append(cell);
  }
  const body = node("tbody");
  for (const row of rows) {
    const tr = node("tr");
    for (const value of row) {
      const td = node("td", value instanceof HTMLButtonElement ? "cell-actions" : "");
      append(td, [value]);
      tr.append(td);
    }
    body.append(tr);
  }
  return append(wrapper, [append(table, [append(node("thead"), [header]), body])]);
}
function titleCell(title, subtitle) {
  return append(node("div"), [node("strong", "", title || "—"), subtitle ? node("span", "row-sub", subtitle) : null]);
}
function viewMessage(view, text = "", tone = "", retry) {
  if (!view.status) return;
  view.status.textContent = text;
  view.status.dataset.tone = tone;
  if (retry) view.status.append(button("重试", retry));
}
function readFilters(form) {
  return Object.fromEntries(Array.from(new FormData(form), ([name, value]) => [name, value.trim()]).filter(([, value]) => value));
}
function query(path, filters, cursor) {
  const params = new URLSearchParams(filters);
  if (cursor) params.set("cursor", cursor);
  return `${path}${params.size ? `?${params}` : ""}`;
}
function unauthorized(error) {
  if (error?.status !== 401) return false;
  location.replace("/ops/admin/");
  return true;
}
function route(name, focus = false) {
  const target = Object.hasOwn(views, name) ? name : links[0]?.dataset.section;
  if (!target || target === selected) return;
  selected = target;
  for (const link of links) {
    const on = link.dataset.section === target;
    link.toggleAttribute("aria-current", on);
    if (on) link.setAttribute("aria-current", "page");
    views[link.dataset.section].section.hidden = !on;
  }
  const view = views[target];
  if (focus) view.section.querySelector("h2").focus({preventScroll: true});
  if (!view.loaded) void load(view);
  if (target === "users") void loadGroupFilter();
}
async function load(view, more = false) {
  if (!view?.results || (more && !view.cursor)) return;
  const seq = ++view.sequence;
  const cursor = more ? view.cursor : null;
  const oldItems = more ? view.items : [];
  if (!more && !view.loaded) view.results.replaceChildren();
  view.section.setAttribute("aria-busy", "true");
  if (view.next) view.next.disabled = true;
  if (view.exportButton) view.exportButton.disabled = true;
  viewMessage(view, cursor ? "正在加载下一页…" : "正在读取实时数据…");
  try {
    const data = await call(query(endpoints[view.key], view.filters, cursor));
    if (seq !== view.sequence) return;
    view.data = data;
    view.items = oldItems.concat(Array.isArray(data.items) ? data.items : []);
    view.cursor = data.next_cursor || null;
    view.loaded = true;
    renderView(view);
    const reference = requestRef(data.request_id);
    if (view.key === "overview") {
      viewMessage(view, `${data.status === "partial" ? "部分指标不可用或已降级" : "运行摘要已更新"}${reference ? ` · 请求 ID：${reference}` : ""}`);
    } else {
      viewMessage(view, `已载入 ${view.items.length} 条${view.cursor ? " · 可继续加载" : ""}${reference ? ` · 请求 ID：${reference}` : ""}`);
    }
    if (view.next) view.next.hidden = !view.cursor;
    if (view.exportButton) view.exportButton.disabled = false;
    if (view.key === "groups") void loadCatalog(view);
  } catch (error) {
    if (seq !== view.sequence || unauthorized(error)) return;
    if (!cursor) {
      view.results.replaceChildren(empty(error?.status === 403 ? "当前权限无法查看此工作区。" : "暂时无法加载数据。"));
      if (view.next) view.next.hidden = true;
    }
    viewMessage(view, errorText(error), "error", () => load(view, more));
  } finally {
    if (seq === view.sequence) {
      view.section.removeAttribute("aria-busy");
      if (view.next) view.next.disabled = false;
    }
  }
}
const metricSpecs = [
  ["accounts", "账号总数", (value) => [count(value?.total), `活跃 ${count(value?.active)} · 禁用 ${count(value?.disabled)} · 待设密 ${count(value?.pending_secret)}`]],
  ["failures_24h", "近 24 小时失败事件", (value) => [count(value), "统计失败结果的结构化事件"]],
  ["collector_delay_seconds", "最近一次采集延迟", (value) => [Number.isFinite(value) ? `${count(value)} s` : "—", "结构化事件采集"]],
  ["modules", "共享模块", (value) => [count(value?.active), `停用 ${count(value?.disabled)} · 未登记 ${count(value?.unregistered)}`]],
  ["database", "数据库健康", (value) => [metricText(value), "持久化读写依赖"]],
  ["queue", "队列健康", (value) => [metricText(value), "运行队列与执行器"]],
  ["service_version", "服务版本", (value) => [typeof value === "string" ? value : "—", "当前服务部署标识"]],
];
function metricText(value) {
  const names = {ok: "正常", healthy: "正常", online: "在线", inline: "同步模式", disabled: "未启用", unavailable: "不可用", error: "异常"};
  const key = (typeof value === "object" && value) ? value.status || value.mode : value;
  return typeof key === "string" ? (Object.hasOwn(names, key) ? names[key] : key) : "—";
}
function renderOverview(view) {
  const data = view.data;
  const cards = metricSpecs.filter(([key]) => Object.hasOwn(data.metrics || {}, key)).map(([key, title, display]) => {
    const metric = data.metrics[key] || {};
    const state = ["available", "degraded", "unavailable"].includes(metric.state) ? metric.state : "unavailable";
    const [value, detail] = state === "unavailable" ? ["—", "服务未提供可用数据"] : display(metric.value);
    const reference = requestRef(metric.request_id || data.request_id);
    const card = node("article", "metric");
    card.dataset.state = state;
    return append(card, [
      append(node("div", "metric-head"), [node("h3", "", title), badge(state)]),
      node("strong", "", value), node("p", "muted small", detail),
      append(node("div", "metric-foot"), [node("code", "", reference ? `请求 ${reference}` : "无请求 ID"), state !== "available" ? button("重试", () => load(view)) : null]),
    ]);
  });
  view.results.replaceChildren(...(cards.length ? cards : [empty("当前没有可显示的运行指标。")])) ;
  const operator = document.querySelector("#operator-summary");
  operator.hidden = !data.operator;
  if (data.operator) operator.querySelector("[data-operator]").replaceChildren(facts([
    ["操作者", data.operator.display_name || data.operator.username || "—"],
    ["系统组", groupNames(groupsOf(data.operator))],
    ["显式权限", (Array.isArray(data.operator.permissions) ? data.operator.permissions : []).join("、") || "—"],
  ]));
}
function renderUsers(view) {
  const rows = view.items.map((item) => [
    append(node("div"), [titleCell(item.display_name || item.username, item.display_name ? item.username : ""), idCopy(item.id)]),
    role(item.role), groupNames(groupsOf(item)), badge(item.status),
    titleCell(date(item.last_login), `创建 ${date(item.created_at || item.date_joined)}`), count(item.session_count),
    Array.isArray(item.risk_flags) ? item.risk_flags.join("、") || "—" : "—",
    button("详情", (event) => openUser(item.id, event.currentTarget), "utility plain-action"),
  ]);
  view.results.replaceChildren(rows.length ? ledger("账号", ["账号 / UUID", "业务角色", "系统组", "状态", "最近登录 / 创建", "会话", "风险标记", "操作"], rows) : empty("没有符合条件的账号。"));
}
function renderGroups(view) {
  const rows = view.items.map((item) => [
    titleCell(item.name, item.description),
    Array.isArray(item.permissions) ? String(item.permissions.length) : "—", count(item.member_count),
    titleCell(date(item.last_changed_at), item.last_changed_by),
    button(can("change_group") && (!item.reserved || can("modules")) ? "权限详情 / 编辑" : "权限详情", () => openGroup(item), "utility plain-action"),
  ]);
  view.results.replaceChildren(rows.length ? ledger("系统组", ["系统组", "权限数", "成员数", "最近变更", "操作"], rows) : empty("没有可见系统组。"));
}
function renderAudit(view) {
  const rows = view.items.map((item) => [date(item.occurred_at), titleCell(item.event_type, item.summary_code),
    code(item.actor_id), titleCell(item.target_type, item.target_id), badge(item.outcome), code(item.request_id), code(item.run_id),
    button("详情", () => openAudit(item), "utility plain-action"),
  ]);
  view.results.replaceChildren(rows.length ? ledger("审计事件", ["发生时间", "事件 / 摘要码", "操作者", "目标摘要", "结果", "请求 ID", "运行 ID", "操作"], rows) : empty("当前筛选没有审计事件。"));
}
function renderErrors(view) {
  const rows = view.items.map((item) => [date(item.occurred_at || item.last_seen_at), titleCell(item.error_code, item.message_safe),
    titleCell(item.status_code, `${item.method || ""} ${item.path || ""}`.trim()), count(item.repeat_count), badge(item.resolved_at ? "resolved" : "open"), code(item.request_id),
    button("详情", () => openError(item), "utility plain-action"),
  ]);
  view.results.replaceChildren(rows.length ? ledger("异常日志", ["发生时间", "异常 / 安全摘要", "请求", "重复", "状态", "请求 ID", "操作"], rows) : empty("当前筛选没有异常日志。"));
}
function renderModules(view) {
  const cards = view.items.map((item) => {
    const moduleKey = item.module_key || item.key;
    const status = item.status === "active" ? "active" : item.status === "disabled" ? "disabled" : "unregistered";
    const board = node("article", "sheet module-card");
    return append(board, [
      append(node("div", "metric-head"), [node("h3", "", item.label || moduleKey || "—"), badge(status)]),
      node("p", "section-code", `${moduleKey || "—"} / ${item.area || "—"}`),
      facts([["版本", item.install_version || "—"], ["入口", code(item.path)], ["健康状态", badge(item.health?.state || item.state)],
        ["声明权限", (Array.isArray(item.permissions) ? item.permissions : []).join("、") || "—"], ["最近更新", date(item.updated_at)]]),
      can("modules") && moduleKey && item.path ? append(node("div", "action-line"), [button(status === "active" ? "停用模块" : status === "disabled" ? "启用模块" : "登记模块", () => openModule(moduleKey, status, item.label), status === "active" ? "danger" : "utility")]) : !item.path ? node("p", "muted small", "等待模块安装后登记。") : null,
    ]);
  });
  view.results.replaceChildren(...(cards.length ? cards : [empty("暂无模块登记记录。")])) ;
}
function renderView(view) {
  ({overview: renderOverview, users: renderUsers, groups: renderGroups, audit: renderAudit, errors: renderErrors, modules: renderModules}[view.key])(view);
}

async function allItems(path) {
  let items = [];
  let cursor = null;
  const seen = new Set();
  do {
    const result = await call(query(path, {}, cursor));
    items = items.concat(Array.isArray(result.items) ? result.items : []);
    cursor = result.next_cursor;
    if (cursor) {
      if (seen.has(cursor) || seen.size >= 100) throw new Error();
      seen.add(cursor);
    }
  } while (cursor);
  return items;
}
async function loadGroupFilter() {
  const select = document.querySelector("#filter-groups");
  if (!select || filterGroupsLoaded) return;
  filterGroupsLoaded = true;
  try {
    for (const group of await allItems("/groups")) {
      const opt = node("option", "", group.name);
      opt.value = group.id;
      select.append(opt);
    }
  } catch (error) {
    filterGroupsLoaded = false;
    if (!unauthorized(error)) select.firstElementChild.textContent = "系统组暂不可用";
  }
}
function permissionKey(item) {return item.key || `${item.app_label}.${item.codename}`;}
function moduleOwner(item) {return item.owner_module || item.app_label;}
function permissionList(items) {
  const list = node("ul", "catalog-list");
  for (const item of items) {
    const owner = moduleOwner(item);
    const label = item.risk_level ? ` · ${item.risk_level}` : "";
    list.append(append(node("li"), [node("strong", "", item.label || permissionKey(item)), node("span", "row-sub", `${permissionKey(item)}${label}`), node("span", "row-sub", `${owner === "ops_admin" ? "系统管理" : `${owner} · 由模块声明，系统管理仅分配`}${item.assignable ? "" : " · 不可分配"}`)]));
  }
  return list;
}
async function loadCatalog(view) {
  const shelf = view.section.querySelector("[data-catalog]");
  if (shelf.dataset.loading) return;
  shelf.dataset.loading = "true";
  shelf.replaceChildren(node("p", "muted small", "正在读取目录…"));
  try {
    const items = await allItems("/permissions/whitelist");
    shelf.replaceChildren(items.length ? permissionList(items) : empty("白名单为空。"));
  } catch (error) {
    if (!unauthorized(error)) shelf.replaceChildren(node("p", "muted small", errorText(error)), button("重试", () => loadCatalog(view)));
  } finally {
    delete shelf.dataset.loading;
  }
}
function allowedPath(path) {
  // Links originate from the server allowlist, and the browser still rejects other origins.
  if (typeof path !== "string" || !path.startsWith("/")) return "";
  try {
    const parsed = new URL(path, location.origin);
    if (parsed.origin !== location.origin || parsed.search || parsed.hash) return "";
    if (!/^\/ops\/(debug|data)(\/|$)/.test(parsed.pathname)) return "";
    return parsed.pathname;
  } catch { return ""; }
}
async function sharedNavigation() {
  const sidebar = document.querySelector("#shared-nav");
  try {
    const data = await call("/nav");
    const items = data.items || data.modules || [];
    const anchors = [];
    for (const item of items) {
      const path = allowedPath(item.path);
      if (!path || item.accessible === false || item.can_access === false) continue;
      const anchor = node("a", "", `${item.label || item.module_key} ↗`);
      anchor.href = path;
      anchors.push(anchor);
    }
    sidebar.querySelector("div").replaceChildren(...anchors);
    sidebar.hidden = !anchors.length;
  } catch (error) {
    unauthorized(error);
    sidebar.hidden = true;
  }
}

function startDialog(title) {
  if (dialogBusy) return null;
  forgetTicket?.();
  forgetTicket = undefined;
  feedback(dialogFeedback);
  dialogTitle.textContent = title;
  dialogBody.replaceChildren();
  if (!dialogs.open) dialogs.showModal();
  dialogTitle.focus();
  return ++dialogEpoch;
}
function currentDialog(epoch) {return dialogs.open && epoch === dialogEpoch;}
function closeDialog() {if (!dialogBusy) dialogs.close();}
dismiss.addEventListener("click", closeDialog);
dialogs.addEventListener("cancel", (event) => {if (dialogBusy) event.preventDefault();});
dialogs.addEventListener("close", () => {
  if (dialogs.open) return;
  ++dialogEpoch;
  forgetTicket?.();
  forgetTicket = undefined;
  dialogBody.replaceChildren();
  feedback(dialogFeedback);
});
window.addEventListener("pagehide", () => forgetTicket?.());
function textField(label, name, {type = "text", value = "", maxLength = 150, required = false, placeholder = ""} = {}) {
  const input = node(type === "textarea" ? "textarea" : "input");
  if (type !== "textarea") input.type = type;
  Object.assign(input, {name, value: value ?? "", maxLength, required, placeholder});
  return append(node("label"), [label, input]);
}
function selectField(label, name, choices, value = "") {
  const select = node("select");
  select.name = name;
  for (const [key, title] of choices) {
    const option = node("option", "", title);
    option.value = key;
    select.append(option);
  }
  select.value = value;
  return append(node("label"), [label, select]);
}
function check(label, {name = "", value = "", checked = false, disabled = false, required = false} = {}) {
  const input = node("input");
  Object.assign(input, {type: "checkbox", name, value, checked, disabled, required});
  return append(node("label", "check"), [input, label]);
}
function groupField(title, rows, description) {
  return append(node("fieldset"), [node("legend", "", title), description ? node("p", "muted small", description) : null, append(node("div", "choice-list"), rows)]);
}
function operationForm(fields, action, handler, danger = false) {
  const form = node("form", "dialog-form");
  form.method = "post";
  form.autocomplete = "off";
  const submit = node("button", danger ? "danger" : "primary", action);
  submit.type = "submit";
  append(form, [...fields, append(node("div", "action-line"), [submit, button("取消", closeDialog)])]);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!dialogBusy) void handler(form);
  });
  return form;
}
function writer(path, method, done) {
  let signature, key;
  return async (form, body) => {
    const payload = JSON.stringify(body);
    if (payload !== signature) {
      signature = payload;
      key = newKey();
    }
    feedback(dialogFeedback);
    dialogBusy = true;
    busy(form, true);
    dismiss.disabled = true;
    const locked = [...form.querySelectorAll("input,select,textarea,button[type=button]")].filter((item) => !item.disabled);
    for (const field of locked) field.disabled = true;
    try {
      const data = await call(path, {method, body, key});
      dialogBusy = false;
      done(data);
    } catch (error) {
      if (error.code === "account_not_deletable" && views.users?.results) void load(views.users);
      if (!unauthorized(error)) feedback(dialogFeedback, errorText(error), "error", true);
    } finally {
      dialogBusy = false;
      dismiss.disabled = false;
      for (const field of locked) field.disabled = false;
      busy(form, false);
    }
  };
}
function afterWrite(text, view, data) {
  closeDialog();
  const reference = requestRef(data.request_id);
  feedback(globalFeedback, `${text}${reference ? ` · 请求 ID：${reference}` : ""}`);
  if (views[view]?.results) void load(views[view]);
}
function twoSteps({title, confirmTitle, form, describe, submit, danger = false}) {
  dialogBody.replaceChildren(form);
  return (body) => {
    feedback(dialogFeedback);
    dialogTitle.textContent = confirmTitle;
    const final = operationForm([describe(body), check("我已核对变更并确认执行。", {required: true})], "确认执行", (next) => submit(next, body), danger);
    final.querySelector(".action-line").append(button("上一步", () => {
      dialogTitle.textContent = title;
      feedback(dialogFeedback);
      dialogBody.replaceChildren(form);
      dialogTitle.focus();
    }));
    dialogBody.replaceChildren(final);
    dialogTitle.focus();
  };
}
function sectionBlock(title, entries) {
  return append(node("section", "sheet"), [append(node("div", "sheet-head"), [node("h3", "", title)]), append(node("div", "sheet-body"), [facts(entries)])]);
}
function openUser(id) {
  const epoch = startDialog("账号详情");
  if (!epoch) return;
  dialogBody.append(node("p", "muted", "正在读取脱敏账号详情…"));
  call(`/users/${encodeURIComponent(id)}`).then((data) => {
    if (!currentDialog(epoch)) return;
    const user = data.user || data, groups = groupsOf(user);
    const sessions = Array.isArray(user.sessions) ? user.sessions : [];
    const summary = Array.isArray(user.recent_operations) ? user.recent_operations : [];
    dialogTitle.textContent = `账号 / ${user.display_name || user.username || "详情"}`;
    dialogBody.replaceChildren(facts([
      ["用户名", user.username], ["账号 UUID", code(user.id)], ["显示名", user.display_name || "—"],
      ["邮箱（脱敏）", user.email_masked || "—"], ["业务角色", role(user.role)], ["系统组", groupNames(groups)],
      ["账号状态", badge(user.status)], ["最近登录", date(user.last_login)], ["创建时间", date(user.created_at)],
      ["近期登录失败次数", count(user.recent_failed_logins)], ["会话数", count(user.session_count)],
    ]));
    if (groups.some((group) => Array.isArray(group?.permissions))) {
      dialogBody.append(sectionBlock("系统组 · 白名单权限", groups.map((group) => [group.name, (group.permissions || []).join("、") || "—"])));
    }
    if (sessions.length) dialogBody.append(sectionBlock("会话 · 已脱敏", sessions.map((session, i) => [`会话 ${i + 1}`, `有效至 ${date(session.expires_at)}`])));
    if (summary.length) dialogBody.append(sectionBlock("近期操作摘要", summary.map((action) => [action.event_type, [date(action.occurred_at), action.summary_code, requestRef(action.request_id)].filter(Boolean).join(" · ")])));
    const reserved = Array.isArray(user.risk_flags) && user.risk_flags.includes("reserved_account");
    const actions = [];
    if (can("change_user") && !reserved) actions.push(button("编辑账号", () => addOrEditUser(user)));
    if (can("disable_user") && !reserved) {
      if (user.status !== "disabled" && user.id !== operatorId) actions.push(button("禁用账号", () => openReason(user, "disable"), "danger"));
      if (user.status === "disabled") actions.push(button("启用账号", () => openReason(user, "enable")));
    }
    if (!reserved && can("reset_secret") && user.status !== "disabled" && (!user.role || user.id === operatorId || can("change_role"))) actions.push(button(user.status === "pending_secret" ? "签发设密凭证" : "签发重置凭证", () => resetSecret(user)));
    if (!reserved && can("delete_user") && user.id !== operatorId) actions.push(button("删除误建账号", () => openReason(user, "delete"), "danger"));
    if (actions.length) dialogBody.append(append(node("div", "action-line"), actions));
  }).catch((error) => {
    if (!currentDialog(epoch) || unauthorized(error)) return;
    dialogBody.replaceChildren(button("重试读取", () => openUser(id)));
    feedback(dialogFeedback, errorText(error), "error", true);
  });
}
async function addOrEditUser(user) {
  const edit = Boolean(user), title = edit ? "编辑账号" : "新增账号", epoch = startDialog(title);
  if (!epoch) return;
  const currentGroups = groupsOf(user);
  let groups, groupProblem;
  dialogBody.append(node("p", "muted", "正在准备账号表单…"));
  if (can("groups")) {
    try {groups = await allItems("/groups");}
    catch (error) {if (unauthorized(error)) return; groupProblem = true;}
  }
  if (!currentDialog(epoch)) return;
  const fields = [];
  if (!edit) fields.push(textField("用户名", "username", {required: true, placeholder: "账号唯一标识"}));
  fields.push(textField("显示名", "display_name", {maxLength: 64, value: user?.display_name}));
  if (can("change_role") && (!edit || user.id !== operatorId)) fields.push(selectField("业务角色", "role", [["", "非业务"], ...Object.entries(roleNames).filter(([key]) => key)], user?.role || ""));
  else if (edit) fields.push(facts([["业务角色（只读）", role(user.role)]]));
  const byId = new Map([...currentGroups, ...(groups || [])].map((group) => [Number(group.id), group]));
  if (groups) {
    const selected = new Set(currentGroups.map((group) => Number(group.id)));
    fields.push(groupField("系统组", byId.size ? Array.from(byId, ([id, group]) => check(group.name, {
      name: "groups", value: id, checked: selected.has(id),
    })) : [node("p", "muted small", "当前没有可选择的系统组。")], "分配组还须通过服务端权限包含关系检查。"));
  } else fields.push(node("p", "muted small", groupProblem ? "暂时无法读取系统组，保存时不会更改组。" : "此会话无权读取系统组，保存时不会更改组。"));
  if (!edit) {
    fields.push(textField("通知邮箱（可选）", "email", {type: "email", maxLength: 254, placeholder: "仅用于可信邮件交接"}));
    if (can("reset_secret")) fields.push(check("新增成功后立即签发一次性设密凭证。", {name: "issue_secret"}));
    fields.push(node("p", "muted small", "新增账号默认待设密；本人保存密码后才能登录。"));
  }
  const release = writer(`/users${edit ? `/${encodeURIComponent(user.id)}` : ""}`, edit ? "PATCH" : "POST", (data) => {
    if (!edit && handleIssue(data, "账号已新增。")) {
      if (views.users?.results) void load(views.users);
    } else afterWrite(edit ? "账号已更新。" : "账号已新增，状态为待设密。", "users", data);
  });
  const form = operationForm(fields, edit ? "检查变更" : "检查新账号", (form) => {
    const body = {display_name: form.elements.display_name.value.trim()};
    if (!edit) {
      body.username = form.elements.username.value.trim();
      if (!body.username) { feedback(dialogFeedback, "请填写用户名。", "error", true); return; }
      const email = form.elements.email.value.trim();
      if (email) body.email = email;
      if (form.elements.issue_secret?.checked) body.issue_secret = true;
    }
    if (form.elements.role && (edit ? user.role !== form.elements.role.value : form.elements.role.value)) body.role = form.elements.role.value;
    if (groups) {
      const before = currentGroups.map((group) => Number(group.id)).sort((a, b) => a - b);
      const after = Array.from(form.querySelectorAll('[name="groups"]:checked'), (field) => Number(field.value)).sort((a, b) => a - b);
      if (edit ? JSON.stringify(before) !== JSON.stringify(after) : after.length) body.groups = after;
    }
    if (Object.hasOwn(body, "role") || Object.hasOwn(body, "groups")) body.confirm = true;
    verify(body);
  });
  const verify = twoSteps({title, confirmTitle: edit ? "确认账号变更" : "确认新增账号", form, submit: release, describe(body) {
    const pair = (from, to) => edit ? `${from} → ${to}` : to;
    const changes = [["用户名", user?.username || body.username], ["显示名", pair(user?.display_name || "—", body.display_name || "—")]];
    if (Object.hasOwn(body, "role")) changes.push(["业务角色", pair(role(user?.role), role(body.role))]);
    if (body.groups) changes.push(["系统组", pair(groupNames(currentGroups), groupNames(body.groups.map((id) => byId.get(id))))]);
    if (body.issue_secret) changes.push(["设密凭证", "提交后立即签发"]);
    if (body.email) changes.push(["通知邮箱", "已填写，仅用于可信邮件交接"]);
    return append(node("div", "diff"), [node("strong", "", edit ? "变更前 → 变更后" : "请核对新增账号"), facts(changes)]);
  }});
}
function openReason(user, action) {
  const title = {disable: "禁用账号", enable: "启用账号", delete: "删除误建账号"}[action];
  if (!startDialog(title)) return;
  const hint = {
    disable: "禁用会立即撤销此账号已登录的会话。",
    enable: "启用不会自动生成密码或发送设密凭证。",
    delete: "仅误建且无业务关联的账号可删除。此操作不可撤销；有关联时服务端将拒绝删除，并在权限允许时停用该账号。",
  }[action];
  const id = encodeURIComponent(user.id);
  const submit = writer(`/users/${id}${action === "delete" ? "" : `/${action}`}`, action === "delete" ? "DELETE" : "POST", (data) => afterWrite(`${title}操作已完成。`, "users", data));
  let verify;
  const form = operationForm([
    facts([["账号", user.username], ["账号 UUID", code(user.id)]]), node("p", "muted small", hint),
    textField("操作原因", "reason", {type: "textarea", maxLength: 300, required: true, placeholder: "填写原因，勿输入密码或其他敏感信息"}),
  ], action === "delete" ? "检查删除" : `确认${title}`, (form) => {
    const reason = form.elements.reason.value.trim();
    if (!reason) { feedback(dialogFeedback, "请填写操作原因。", "error", true); return; }
    if (action === "delete") verify({reason, confirm: true});
    else void submit(form, {reason});
  }, action !== "enable");
  if (action === "delete") verify = twoSteps({title, confirmTitle: "再次确认删除", form, submit, danger: true,
    describe: (body) => append(node("div", "diff"), [node("strong", "", "删除后不可恢复"), facts([["目标账号", user.username], ["UUID", code(user.id)], ["原因", body.reason]])]),
  });
  else dialogBody.replaceChildren(form);
}
function resetSecret(user) {
  if (!startDialog(user.status === "pending_secret" ? "签发一次性设密" : "签发一次性重置")) return;
  const submit = writer(`/users/${encodeURIComponent(user.id)}/secret/reset`, "POST", (data) => {
    if (!handleIssue(data)) afterWrite("设密请求已受理；重复提交不再返回凭证。", "users", data);
  });
  const form = operationForm([
    facts([["目标账号", user.username], ["账号 UUID", code(user.id)]]),
    node("p", "muted small", "会撤销先前同用途的凭证。符合可信邮件交接条件时只发送链接；否则需要管理员线下交接一次性凭证。"),
    selectField("交接方式", "delivery_mode", [["", "自动选择可信邮件或线下"], ["offline", "线下受控交接"]]),
    check("我已核对目标账号。", {required: true}),
  ], "确认签发", (form) => {
    const delivery = form.elements.delivery_mode.value;
    void submit(form, delivery ? {delivery_mode: delivery} : {});
  });
  dialogBody.replaceChildren(form);
}
function handleIssue(result, intro = "") {
  let ticket = typeof result.ticket === "string" && result.ticket.length <= 512 ? result.ticket : "";
  if (Object.hasOwn(result, "ticket")) delete result.ticket;
  if (!ticket && !result.delivery_mode && !result.expires_at) return false;
  dialogTitle.textContent = "一次性凭证交接";
  const expires = new Date(result.expires_at || "").valueOf(), reference = requestRef(result.request_id);
  const state = result.delivery_mode === "trusted_email" ? "可信邮件已处理，仅内含短期链接。" : ticket ? "线下交接：凭证仅可复制一次，不会在页面显示。请通过受控渠道交给账号本人。" : "此响应未提供新的凭证。已签发凭证不能再次获取。";
  const actions = append(node("div", "action-line"), [button("结束交接", closeDialog)]);
  forgetTicket?.();
  let expireTimer;
  forgetTicket = () => {ticket = ""; clearTimeout(expireTimer);};
  if (ticket) {
    const copy = button("复制一次性链接", async () => {
      if (!ticket || (Number.isFinite(expires) && Date.now() >= expires)) {
        forgetTicket?.();
        copy.disabled = true;
        feedback(dialogFeedback, "凭证不可再复制，请重新签发。", "error", true);
        return;
      }
      if (!navigator.clipboard?.writeText || !window.isSecureContext) {
        feedback(dialogFeedback, "当前浏览器无法安全复制，请在 HTTPS 连接下重新签发。", "error", true);
        return;
      }
      copy.disabled = true;
      try {
        await navigator.clipboard.writeText(`${location.origin}/ops/admin/secret/#ticket=${encodeURIComponent(ticket)}`);
        forgetTicket?.();
        if (copy.isConnected) feedback(dialogFeedback, "已复制一次性链接。交接后请清除剪贴板。", "", true);
      } catch {
        if (ticket) copy.disabled = false;
        if (copy.isConnected) feedback(dialogFeedback, "浏览器未允许复制。允许剪贴板访问后，可再次尝试。", "error", true);
      }
    }, "primary");
    actions.prepend(copy);
    expireTimer = setTimeout(() => {forgetTicket?.(); copy.disabled = true;}, Number.isFinite(expires) ? Math.max(0, expires - Date.now()) : 900_000);
  }
  dialogBody.replaceChildren(node("p", "muted", [intro, state].filter(Boolean).join(" ")), facts([["失效时间", date(result.expires_at)], ["请求 ID", code(reference)]]), actions);
  dialogTitle.focus();
  return true;
}

async function openGroup(group) {
  const title = `系统组 / ${group.name}`, epoch = startDialog(title);
  if (!epoch) return;
  const current = new Set(Array.isArray(group.permissions) ? group.permissions : []);
  const summary = facts([["组名", group.name], ["说明", group.description || "—"], ["成员数", count(group.member_count)],
    ["最近变更", date(group.last_changed_at)], ["变更人", code(group.last_changed_by)], ["系统保留", group.reserved ? "是" : "否"]]);
  dialogBody.append(summary, node("p", "muted", "正在读取白名单…"));
  let catalog;
  try {catalog = await allItems("/permissions/whitelist");}
  catch (error) {
    if (!currentDialog(epoch) || unauthorized(error)) return;
    dialogBody.replaceChildren(summary, button("重试读取", () => openGroup(group)));
    feedback(dialogFeedback, errorText(error), "error", true);
    return;
  }
  if (!currentDialog(epoch)) return;
  if (!can("change_group") || (group.reserved && !can("modules"))) {
    const assigned = catalog.filter((item) => current.has(permissionKey(item)));
    dialogBody.replaceChildren(summary, assigned.length ? permissionList(assigned) : empty("没有已分配的白名单权限。"));
    return;
  }
  const assigned = new Set(catalog.filter((item) => item.assignable).map(permissionKey));
  const options = catalog.map((item) => {
    const key = permissionKey(item), owner = moduleOwner(item);
    const caption = append(node("span"), [node("strong", "", item.label || key), node("span", "row-sub", `${key} · ${owner === "ops_admin" ? "系统管理" : `${owner}（模块声明）`} · ${item.risk_level === "high" ? "高风险" : "标准"}${item.assignable ? "" : " · 不可分配"}`)]);
    return check(caption, {name: "permissions", value: key, checked: current.has(key), disabled: !item.assignable});
  });
  const submit = writer(`/groups/${encodeURIComponent(group.id)}`, "PATCH", (data) => afterWrite("组权限已更新。", "groups", data));
  const form = operationForm([summary, groupField("白名单权限", options, "仅能分配白名单项；实际授权范围由服务端复核。")], "检查权限变更", (form) => {
    const permissions = Array.from(form.querySelectorAll('[name="permissions"]:checked:not(:disabled)'), (field) => field.value).sort();
    const previous = Array.from(current).filter((key) => assigned.has(key)).sort();
    if (JSON.stringify(previous) === JSON.stringify(permissions)) {
      feedback(dialogFeedback, "权限没有变化。", "", true);
      return;
    }
    verify({permissions, confirm: true});
  });
  const verify = twoSteps({title, form, confirmTitle: "确认组权限变更", submit, describe({permissions}) {
    const next = new Set(permissions);
    const adding = permissions.filter((key) => !current.has(key));
    const removing = Array.from(current).filter((key) => assigned.has(key) && !next.has(key));
    return append(node("div", "diff"), [node("strong", "", "组成员的访问权限将同步变更"), facts([
      ["组名", group.name], ["将新增", adding.join("、") || "无"], ["将移除", removing.join("、") || "无"],
    ])]);
  }});
}
function auditFacts(item) {
  return facts([["事件 ID", code(item.event_id)], ["发生时间", date(item.occurred_at)], ["操作者", code(item.actor_id)],
    ["请求 ID", code(item.request_id)], ["目标类型", item.target_type], ["目标摘要", code(item.target_id)],
    ["结果", badge(item.outcome)], ["摘要码", code(item.summary_code)], ["关联运行 ID", code(item.run_id)], ["来源模块", code(item.module || item.source)]]);
}
async function openAudit(item) {
  const epoch = startDialog(`审计 / ${item.event_type || "事件"}`);
  if (!epoch) return;
  dialogBody.append(auditFacts(item));
  if (!item.event_id) return;
  try {
    const detail = await call(`/audit/${encodeURIComponent(item.event_id)}`);
    if (!currentDialog(epoch)) return;
    const record = detail.item || detail;
    dialogBody.replaceChildren(auditFacts({...record, request_id: record.event_request_id || item.request_id}));
    if (record.details && typeof record.details === "object" && Object.keys(record.details).length) {
      dialogBody.append(append(node("section", "sheet"), [append(node("div", "sheet-head"), [node("h3", "", "脱敏详情")]), append(node("div", "sheet-body"), [node("pre", "safe-excerpt", JSON.stringify(record.details, null, 2))])]));
    } else dialogBody.append(node("p", "muted small", "没有可查看的脱敏详情。"));
  } catch (error) {
    if (!currentDialog(epoch) || unauthorized(error)) return;
    // Detail auth may be stricter than list visibility. Do not retain a stale projection on denial.
    if (error.status === 403 || error.status === 404) dialogBody.replaceChildren(empty("此事件详情不可查看。"));
    else dialogBody.append(button("重试读取详情", () => openAudit(item)));
    feedback(dialogFeedback, errorText(error), "error", true);
  }
}
function openError(item) {
  if (!startDialog(`异常 / ${item.error_code || "日志"}`)) return;
  dialogBody.append(facts([
    ["异常 ID", code(item.id)], ["请求 ID", code(item.request_id)], ["异常类型", code(item.exception_class)],
    ["HTTP 状态码", item.status_code], ["请求方法", item.method], ["请求路径", code(item.path)],
    ["安全摘要", item.message_safe], ["堆栈指纹", code(item.stack_hash)], ["服务版本", item.service_version],
    ["操作者", code(item.actor_id)], ["重复计数", count(item.repeat_count)], ["首次出现", date(item.first_seen_at)],
    ["最近出现", date(item.last_seen_at)], ["处理状态", badge(item.resolved_at ? "resolved" : "open")],
    ["处理时间", date(item.resolved_at)],
  ]));
  if (can("resolve_error") && !item.resolved_at && item.id) dialogBody.append(append(node("div", "action-line"), [button("标记已处理", () => resolveError(item), "primary")]));
}
function resolveError(item) {
  if (!startDialog("确认处理异常")) return;
  const submit = writer(`/errors/${encodeURIComponent(item.id)}/resolve`, "POST", (data) => afterWrite("异常已标记为处理完成。", "errors", data));
  dialogBody.append(operationForm([
    facts([["异常代码", item.error_code], ["请求 ID", code(item.request_id)]]),
    node("p", "muted small", "标记只更新处理状态，记录不会被清除。"),
    textField("处理说明", "reason", {type: "textarea", required: true, maxLength: 300, placeholder: "请勿附带敏感数据"}),
  ], "确认已处理", (form) => {
    const reason = form.elements.reason.value.trim();
    if (!reason) {feedback(dialogFeedback, "请填写处理说明。", "error", true); return;}
    void submit(form, {reason});
  }));
}
let exporting = false;
const exportRetryKeys = new Map();
async function exportCurrent(view) {
  if (exporting || !view.loaded || view.exportButton.disabled) return;
  exporting = true;
  view.exportButton.disabled = true;
  feedback(globalFeedback);
  const body = {kind: view.key, filters: {...view.filters}};
  const signature = JSON.stringify(body);
  const previous = exportRetryKeys.get(signature);
  const key = previous || newKey();
  try {
    const response = await call("/audit/export", {method: "POST", key, body, file: true});
    exportRetryKeys.delete(signature);
    const rid = requestRef(response.requestId || response.request_id);
    if (response.blob) {
      const url = URL.createObjectURL(response.blob);
      const link = node("a");
      link.href = url;
      link.download = `ops-${view.key}${rid ? `-${rid.slice(0, 12)}` : ""}.csv`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1_000);
      feedback(globalFeedback, `受控 CSV 已生成。请遵循资料保管要求。${rid ? ` · 请求 ID：${rid}` : ""}`);
    } else feedback(globalFeedback, `导出已受理。${response.run_id ? ` · 运行 ID：${response.run_id}` : ""}${rid ? ` · 请求 ID：${rid}` : ""}`);
  } catch (error) {
    if (!error.status && exportRetryKeys.size < 10) exportRetryKeys.set(signature, key);
    else exportRetryKeys.delete(signature);
    if (!unauthorized(error)) feedback(globalFeedback, errorText(error, "导出失败，请稍后重试。"), "error", true);
  } finally {
    exporting = false;
    view.exportButton.disabled = view.section.hasAttribute("aria-busy");
  }
}
function openModule(key, current, label) {
  const status = current === "active" ? "disabled" : "active";
  const title = current === "unregistered" ? "登记模块" : status === "active" ? "启用模块" : "停用模块";
  if (!startDialog(title)) return;
  const submit = writer(`/modules/${encodeURIComponent(key)}`, current === "unregistered" ? "POST" : "PATCH", (data) => {
    afterWrite(`${title}操作已完成。`, "modules", data);
    if (views.overview) views.overview.loaded = false;
    void sharedNavigation();
  });
  dialogBody.append(operationForm([
    facts([["模块", label || key], ["模块键", code(key)], ["当前状态", badge(current)], ["变更后", badge(status)]]),
    node("p", "muted small", "此操作只控制已安装模块的入口，不删除业务数据，也不改变模块业务权限。"),
    check("我已确认此模块的运行影响。", {required: true}),
  ], `确认${title}`, (form) => submit(form, {status}), status === "disabled"));
}

function handleHash(focus = false) {route(location.hash.slice(1), focus);}
window.addEventListener("hashchange", () => handleHash(true));
for (const link of links) {
  link.addEventListener("click", (event) => {
    if (event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    event.preventDefault();
    if (location.hash !== link.hash) history.pushState(null, "", link.hash);
    handleHash(true);
  });
}
for (const view of Object.values(views)) {
  view.section.querySelector("[data-refresh]")?.addEventListener("click", () => load(view));
  view.next?.addEventListener("click", () => load(view, true));
  view.exportButton?.addEventListener("click", () => exportCurrent(view));
  view.filter?.addEventListener("submit", (event) => {
    event.preventDefault();
    view.filters = readFilters(view.filter);
    void load(view);
  });
  view.filter?.addEventListener("reset", () => {
    view.filters = {};
    void load(view);
  });
}
document.querySelector("#add-user")?.addEventListener("click", () => addOrEditUser());
const logout = document.querySelector("#ops-logout");
logout.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (logout.getAttribute("aria-busy") === "true") return;
  busy(logout, true);
  forgetTicket?.();
  try {
    await call("@logout", {method: "POST"});
    location.replace("/ops/admin/");
  } catch (error) {
    if (!unauthorized(error)) feedback(globalFeedback, errorText(error), "error", true);
  } finally {busy(logout, false);}
});
logout.hidden = false;
handleHash();
void sharedNavigation();
