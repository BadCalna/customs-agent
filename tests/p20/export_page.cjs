/* web/pages/p06.js 导出接线的行为测试：用真实页面源码跑，验证「何时不发请求、请求带什么、
 * 成功后下载什么、失败怎么提示」。不替代真实浏览器点击验收（无 DOM/无浏览器下载）。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../web/pages/p06.js"), "utf8")
  .replace('import {createClient} from "../api.js";', "")
  .replace('const api = createClient({mode:"live"});', "const api = globalThis.TEST_API;");

async function harness(createExport, {importReport = null} = {}) {
  const elements = new Map();
  const calls = [];
  const downloads = [];
  const blobs = [];
  function el(selector) {
    if (!elements.has(selector)) {
      elements.set(selector, {
        hidden: false, disabled: false, value: "", files: [], textContent: "", innerHTML: "", className: "",
        listeners: {}, addEventListener(event, fn) { this.listeners[event] = fn; },
        insertAdjacentHTML(_, text) { this.innerHTML += text; },
      });
    }
    return elements.get(selector);
  }
  const api = {
    me: async () => null, listTasks: async () => ({ items: [], next_cursor: null }),
    listItems: async () => ({ items: [], next_cursor: null }),
    getImport: async () => { if (importReport) return importReport; throw { status: 404 }; },
    getTask: async (id) => ({ task_id: id, title: id, versions: [] }),
    createExport: (...args) => { calls.push(args); return createExport(...args); },
  };
  const context = {
    TEST_API: api,
    document: {
      querySelector: el,
      querySelectorAll: () => [],
      createElement: () => {
        const link = { href: "", download: "", rel: "", clicked: false, removed: false,
          click() { this.clicked = true; }, remove() { this.removed = true; } };
        downloads.push(link);
        return link;
      },
      body: { appendChild() {} },
      addEventListener() {},
    },
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:p20"; }, revokeObjectURL() {} },
    setTimeout: () => 0,
    console,
  };
  vm.createContext(context);
  vm.runInContext(source + "\nglobalThis.hooks={exportResult,renderExports,exportAvailability,identity,openTask,openVersion};", context);
  // 页面模块末尾的 boot 会 await api.me() 再 identity()（推进 epoch、清空详情）。
  // 先让这次启动落定，否则用例里发起的导出会被启动自身的 resetDetail 判定为过期。
  await new Promise((resolve) => setImmediate(resolve));
  // 启动用的 api.me() 在桩里返回 null；用例统一按业务员身份跑，这里在启动之后再落定身份。
  vm.runInContext('me={role:"operator",display_name:"业务员甲"}; identity();', context);
  return { context, el, calls, downloads, blobs };
}

const ok = (overrides = {}) => ({
  blob: { size: 12, type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
  filename: "预审结果-测试结果.xlsx",
  runId: "run-1",
  resultMode: "mock",
  contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ...overrides,
});
const report = (overrides = {}) => ({
  version_id: "ver-1", version_number: 2, precheck_run_id: "run-1", precheck_status: "succeeded",
  result_mode: "mock",
  // openVersion() 渲染摘要时要读这些字段（先备齐，避免用例为了 UI 细节失真）。
  assignment_status: "claimed", parse_status: "succeeded", item_count: 14, member_count: 0,
  image_counts: {available: 0, absent: 0, read_failed: 0},
  original_download_url: "/api/v1/files/ver-1", error: {}, report: {},
  ...overrides,
});

(async () => {
  await new Promise((resolve) => setImmediate(resolve));

  // 1) 还没有解析记录：不发请求，说明缺少真实执行引用。
  const a = await harness(async () => ok());
  a.context.hooks.renderExports(null);
  await a.context.hooks.exportResult("precheck");
  assert.equal(a.calls.length, 0, "缺引用时不得发请求");
  assert.equal(a.el("#exports").hidden, true, "缺引用时不显示导出区");
  assert.match(a.el("#notice").textContent, /缺少真实执行引用/);
  assert.equal(a.el("#notice").className, "error");
  assert.equal(a.downloads.length, 0);

  // 2) 供应商账号：明确说明不能导出审核结果，不发请求。
  const guest = await harness(async () => ok());
  vm.runInContext('me={role:"supplier",display_name:"供应商甲"};', guest.context);
  guest.context.hooks.renderExports(report());
  assert.match(guest.el("#exports").innerHTML, /当前账号不能导出审核结果/);
  await guest.context.hooks.exportResult("precheck");
  assert.equal(guest.calls.length, 0, "无导出权限时不得发请求");

  // 3) 预审未完成：按钮禁用并说明原因，点击也不发请求。
  const b = await harness(async () => ok());
  b.context.hooks.renderExports(report({ precheck_status: "running" }));
  assert.equal(b.el("#exports").hidden, false);
  assert.match(b.el("#exports").innerHTML, /预审尚未完成（running），结果集不可导出/);
  assert.equal((b.el("#exports").innerHTML.match(/disabled/g) || []).length, 2, "两个导出按钮都必须禁用");
  await b.context.hooks.exportResult("issues");
  assert.equal(b.calls.length, 0, "预审未完成不得发请求");

  // 3) 成功：用 version_id + precheck_run_id 组请求，按服务端文件名触发下载并如实提示。
  const c = await harness(async () => ok());
  c.context.hooks.renderExports(report());
  assert.doesNotMatch(c.el("#exports").innerHTML, /disabled/, "预审成功后按钮可用");
  await c.context.hooks.exportResult("precheck");
  assert.equal(c.calls[0][0], "ver-1");
  assert.deepEqual(JSON.parse(JSON.stringify(c.calls[0][1])), { kind: "precheck", run_id: "run-1" });
  assert.equal(c.downloads.length, 1);
  assert.equal(c.downloads[0].href, "blob:p20");
  assert.equal(c.downloads[0].download, "预审结果-测试结果.xlsx");
  assert.equal(c.downloads[0].clicked, true);
  assert.equal(c.downloads[0].removed, true);
  assert.match(c.el("#notice").textContent, /已生成 预审结果-测试结果\.xlsx（执行 run-1 · 结果来源 MOCK）/);
  assert.notEqual(c.el("#notice").className, "error");

  // 4) 服务端拒绝（例如执行已作废）：走错误提示，不下载任何文件。
  const d = await harness(async () => {
    throw Object.assign(new Error("该执行已作废，不能作为有效审核结果导出"), { status: 409, code: "run_superseded", details: {} });
  });
  d.context.hooks.renderExports(report());
  await d.context.hooks.exportResult("issues");
  assert.equal(d.downloads.length, 0, "失败时不得触发下载");
  assert.equal(d.el("#notice").className, "error");
  assert.match(d.el("#notice").textContent, /409 run_superseded/);
  assert.match(d.el("#notice").textContent, /该执行已作废/);
  assert.equal(d.el("#exports").innerHTML.includes("disabled"), false, "失败后按当前状态恢复按钮");

  // 5) 响应引用了别的执行：拒绝保存，说明不一致。
  const e = await harness(async () => ok({ runId: "run-other" }));
  e.context.hooks.renderExports(report());
  await e.context.hooks.exportResult("precheck");
  assert.equal(e.downloads.length, 0);
  assert.match(e.el("#notice").textContent, /export_reference_mismatch/);
  assert.match(e.el("#notice").textContent, /run-other/);

  // 6) 200 但不是 xlsx（例如网关错误页）：不保存、不冒充成功。
  const f = await harness(async () => ok({ contentType: "text/html; charset=utf-8", filename: "gateway.html" }));
  f.context.hooks.renderExports(report());
  await f.context.hooks.exportResult("issues");
  assert.equal(f.downloads.length, 0);
  assert.match(f.el("#notice").textContent, /unexpected_export_type/);

  // 7) 导出在飞时退出登录：响应回来不得下载、不得提示成功/错误（旧上下文整份丢弃）。
  let release7;
  const inflight7 = new Promise((resolve) => { release7 = resolve; });
  const g = await harness(() => inflight7);
  g.context.hooks.renderExports(report());
  const pending7 = g.context.hooks.exportResult("precheck");
  await new Promise((resolve) => setImmediate(resolve));
  vm.runInContext('me=null; identity();', g.context); // 退出登录：推进 epoch 并清空详情
  release7(ok());
  await pending7;
  assert.equal(g.downloads.length, 0, "退出登录后不得下载旧响应");
  assert.doesNotMatch(g.el("#notice").textContent, /已生成/, "不得把旧请求的成功提示写到退出后的页面");
  assert.notEqual(g.el("#notice").className, "error", "也不得把旧请求的错误当成当前错误");
  assert.equal(g.el("#notice").textContent, "", "不留下一直挂着的「正在生成…」");

  // 8) 导出在飞时切到另一个版本：旧版本响应不得覆盖新上下文。
  let release8;
  const inflight8 = new Promise((resolve) => { release8 = resolve; });
  const h = await harness(() => inflight8);
  await h.context.hooks.openTask("task-1"); // 先建立任务上下文，切换版本才有意义
  h.context.hooks.renderExports(report());
  const pending8 = h.context.hooks.exportResult("precheck");
  await new Promise((resolve) => setImmediate(resolve));
  await h.context.hooks.openVersion("ver-2");
  release8(ok());
  await pending8;
  assert.equal(h.downloads.length, 0, "切版本后不得下载旧版本响应");
  assert.doesNotMatch(h.el("#notice").textContent, /已生成/);

  // 9) 反面控制：上下文没变时仍然正常下载（防止「一律丢弃」的假修复）。
  const i = await harness(async () => ok());
  i.context.hooks.renderExports(report());
  await i.context.hooks.exportResult("issues");
  assert.equal(i.downloads.length, 1);

  // 10) A → B → A 重新选择回同一版本同一执行：旧响应也必须失效（严格「重新选择后作废旧响应」）。
  let release10;
  const inflight10 = new Promise((resolve) => { release10 = resolve; });
  const j = await harness(() => inflight10, {importReport: report()});
  await j.context.hooks.openTask("task-1");
  await j.context.hooks.openVersion("ver-1");
  const pending10 = j.context.hooks.exportResult("precheck");
  await new Promise((resolve) => setImmediate(resolve));
  await j.context.hooks.openVersion("ver-2");
  await j.context.hooks.openVersion("ver-1"); // 回到同一版本、同一执行
  release10(ok());
  await pending10;
  assert.equal(j.downloads.length, 0, "重新选择过之后，旧响应不能再下载");

  console.log("PASS: 缺引用/预审未完成不发请求并说明原因；成功按 version_id+run_id 下载服务端文件名；"
    + "服务端错误、执行不一致、非 xlsx 响应都不保存文件并如实提示；"
    + "退出登录/切版本之后到达的旧响应不下载、不提示、不留进行中状态；"
    + "A→B→A 重新选择后旧响应同样失效");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
