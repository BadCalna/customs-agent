/* web/api.js createExport 的契约测试：真实发请求路径、二进制下载、错误体解析、fixture 501。
 * 不替代真实浏览器验收：这里只验证请求/响应处理的分支与字段，不检查视觉与下载落盘。 */
const assert = require("node:assert/strict");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const API = pathToFileURL(path.join(__dirname, "../../web/api.js")).href;

let CSRF = "";
function install(responder) {
  const calls = [];
  // CSRF 值现场生成：api.js 只应回显 cookie 里的令牌，这里不写死任何密钥。
  CSRF = "p20-" + Math.random().toString(36).slice(2);
  globalThis.document = { cookie: `csrftoken=${CSRF}` };
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return responder(url, init);
  };
  return calls;
}

const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

(async () => {
  const { createClient, ApiError } = await import(API);

  // 1) 成功：POST JSON + CSRF，文件名取 RFC 5987 的 filename*，执行与结果来源取响应头。
  let calls = install(async () => new Response(new Uint8Array([80, 75, 3, 4]), {
    status: 200,
    headers: {
      "Content-Type": XLSX,
      "Content-Disposition": "attachment; filename*=utf-8''%E9%A2%84%E5%AE%A1%E7%BB%93%E6%9E%9C-%E6%B5%8B%E8%AF%95%E7%BB%93%E6%9E%9C.xlsx",
      "X-Export-Run": "run-42",
      "X-Result-Mode": "mock",
    },
  }));
  const live = createClient({ mode: "live" });
  const exported = await live.createExport("ver-7", { kind: "precheck", run_id: "run-42" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/versions/ver-7/exports");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.headers["X-CSRFToken"], CSRF);
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { kind: "precheck", run_id: "run-42" });
  assert.equal(exported.filename, "预审结果-测试结果.xlsx");
  assert.equal(exported.runId, "run-42");
  assert.equal(exported.resultMode, "mock");
  assert.equal(exported.contentType, XLSX);
  assert.equal(await exported.blob.arrayBuffer().then((b) => new Uint8Array(b).length), 4);

  // 2) ASCII 文件名（普通 filename=）也要能取到。
  install(async () => new Response(new Uint8Array([80, 75, 3, 4]), {
    status: 200, headers: { "Content-Type": XLSX, "Content-Disposition": 'attachment; filename="issues-export.xlsx"' },
  }));
  assert.equal((await createClient({ mode: "live" }).createExport("v", { kind: "issues", run_id: "r" })).filename,
    "issues-export.xlsx");

  // 3) 非 2xx：JSON 错误体按 ApiError 形状抛出，不返回任何 blob。
  install(async () => new Response(JSON.stringify({
    error: { code: "result_reference_mismatch", message: "执行结果不属于请求的任务和文件版本", details: { run_id: "x" } },
    request_id: "req-9",
  }), { status: 422, headers: { "Content-Type": "application/json" } }));
  await assert.rejects(
    () => createClient({ mode: "live" }).createExport("v", { kind: "precheck", run_id: "x" }),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 422);
      assert.equal(error.code, "result_reference_mismatch");
      assert.equal(error.message, "执行结果不属于请求的任务和文件版本");
      assert.deepEqual(error.details, { run_id: "x" });
      assert.equal(error.requestId, "req-9");
      assert.equal(error.kind, "invalid");
      return true;
    });

  // 4) 200 但没有文件名：报错而不是给一个编造的名字。
  install(async () => new Response(new Uint8Array([80, 75, 3, 4]), { status: 200, headers: { "Content-Type": XLSX } }));
  await assert.rejects(
    () => createClient({ mode: "live" }).createExport("v", { kind: "issues", run_id: "r" }),
    (error) => error.code === "missing_export_filename");

  // 4b) 200 + 空 body：不保存空文件，报错。
  install(async () => new Response(new Uint8Array([]), {
    status: 200, headers: { "Content-Type": XLSX, "Content-Disposition": 'attachment; filename="x.xlsx"' },
  }));
  await assert.rejects(
    () => createClient({ mode: "live" }).createExport("v", { kind: "issues", run_id: "r" }),
    (error) => error instanceof ApiError && error.code === "empty_export_body");

  // 4c) 200 + HTML 错误页冒充 xlsx（非 ZIP 头）：不保存，报错。
  install(async () => new Response("<html>gateway error</html>", {
    status: 200, headers: { "Content-Type": XLSX, "Content-Disposition": 'attachment; filename="x.xlsx"' },
  }));
  await assert.rejects(
    () => createClient({ mode: "live" }).createExport("v", { kind: "issues", run_id: "r" }),
    (error) => error instanceof ApiError && error.code === "unexpected_export_type");

  // 4d) 敌意文件名（路径分隔符 / 上跳 / 非法百分号编码）：不给可下载名，报错。
  for (const disposition of [
    'attachment; filename="../../etc/passwd"',
    'attachment; filename*=\'\'%ZZ.xlsx',
    'attachment; filename="..\\..\\win.ini"',
  ]) {
    install(async () => new Response(new Uint8Array([80, 75, 3, 4]), {
      status: 200, headers: { "Content-Type": XLSX, "Content-Disposition": disposition },
    }));
    await assert.rejects(
      () => createClient({ mode: "live" }).createExport("v", { kind: "issues", run_id: "r" }),
      (error) => error instanceof ApiError && error.code === "missing_export_filename", disposition);
  }

  // 5) fixture 适配器明确拒绝并说明原因，不伪造文件、不发请求。
  calls = install(async () => { throw new Error("fixture 模式不应发请求"); });
  const fixture = createClient({ mode: "fixture" });
  await assert.rejects(
    () => fixture.createExport("v", { kind: "precheck", run_id: "r" }),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 501);
      assert.equal(error.code, "live_required");
      assert.equal(error.kind, "not_implemented");
      return true;
    });
  assert.equal(calls.length, 0);

  console.log("PASS: createExport posts with CSRF, parses filename*/filename, keeps run headers, "
    + "rejects empty/non-ZIP/HTML bodies and hostile filenames, raises ApiError on error/missing filename, "
    + "fixture stays live_required without faking a file");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
