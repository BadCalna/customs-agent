/* web/pages/p06.js 导出接线的行为测试：用真实页面源码跑，验证「何时不发请求、请求带什么、
 * 成功后下载什么、失败怎么提示」。不替代真实浏览器点击验收（无 DOM/无浏览器下载）。 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../../web/pages/p06.js"), "utf8")
  .replace('import {createClient} from "../api.js";', "")
  .replace('const api = createClient({mode:"live"});', "const api = globalThis.TEST_API;");

function harness(createExport) {
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
    getImport: async () => { throw { status: 404 }; },
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
  vm.runInContext(source + "\nglobalThis.hooks={exportResult,renderExports,exportAvailability};", context);
  // 供应商/业务员的权限分支由 exportAvailability 先判，业务页用例统一按业务员身份跑。
  vm.runInContext('me={role:"operator",display_name:"业务员甲"};', context);
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
  result_mode: "mock", ...overrides,
});

(async () => {
  await new Promise((resolve) => setImmediate(resolve));

  // 1) 还没有解析记录：不发请求，说明缺少真实执行引用。
  const a = harness(async () => ok());
  a.context.hooks.renderExports(null);
  await a.context.hooks.exportResult("precheck");
  assert.equal(a.calls.length, 0, "缺引用时不得发请求");
  assert.equal(a.el("#exports").hidden, true, "缺引用时不显示导出区");
  assert.match(a.el("#notice").textContent, /缺少真实执行引用/);
  assert.equal(a.el("#notice").className, "error");
  assert.equal(a.downloads.length, 0);

  // 2) 供应商账号：明确说明不能导出审核结果，不发请求。
  const guest = harness(async () => ok());
  vm.runInContext('me={role:"supplier",display_name:"供应商甲"};', guest.context);
  guest.context.hooks.renderExports(report());
  assert.match(guest.el("#exports").innerHTML, /当前账号不能导出审核结果/);
  await guest.context.hooks.exportResult("precheck");
  assert.equal(guest.calls.length, 0, "无导出权限时不得发请求");

  // 3) 预审未完成：按钮禁用并说明原因，点击也不发请求。
  const b = harness(async () => ok());
  b.context.hooks.renderExports(report({ precheck_status: "running" }));
  assert.equal(b.el("#exports").hidden, false);
  assert.match(b.el("#exports").innerHTML, /预审尚未完成（running），结果集不可导出/);
  assert.equal((b.el("#exports").innerHTML.match(/disabled/g) || []).length, 2, "两个导出按钮都必须禁用");
  await b.context.hooks.exportResult("issues");
  assert.equal(b.calls.length, 0, "预审未完成不得发请求");

  // 3) 成功：用 version_id + precheck_run_id 组请求，按服务端文件名触发下载并如实提示。
  const c = harness(async () => ok());
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
  const d = harness(async () => {
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
  const e = harness(async () => ok({ runId: "run-other" }));
  e.context.hooks.renderExports(report());
  await e.context.hooks.exportResult("precheck");
  assert.equal(e.downloads.length, 0);
  assert.match(e.el("#notice").textContent, /export_reference_mismatch/);
  assert.match(e.el("#notice").textContent, /run-other/);

  // 6) 200 但不是 xlsx（例如网关错误页）：不保存、不冒充成功。
  const f = harness(async () => ok({ contentType: "text/html; charset=utf-8", filename: "gateway.html" }));
  f.context.hooks.renderExports(report());
  await f.context.hooks.exportResult("issues");
  assert.equal(f.downloads.length, 0);
  assert.match(f.el("#notice").textContent, /unexpected_export_type/);

  console.log("PASS: 缺引用/预审未完成不发请求并说明原因；成功按 version_id+run_id 下载服务端文件名；"
    + "服务端错误、执行不一致、非 xlsx 响应都不保存文件并如实提示");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
