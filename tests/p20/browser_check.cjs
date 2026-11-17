/* P20 真实浏览器验收：/uploads/ 页面上用真实模板上传 → 真实预审 → 点按钮真实下载 xlsx。
 *
 * 需要：可访问的 dev server（DEBUG=1）+ Playwright + Chrome + 演示账号密码。
 *   BASE=http://127.0.0.1:8011 NODE_PATH=<含 playwright 的 node_modules> \
 *   P20_PASSWORD=<seed-demo 演示密码> P20_SAMPLE=<真实模板路径> node tests/p20/browser_check.cjs
 *
 * 启动不了服务/浏览器/缺参数时退出非零，不用 fixture 或脚本内伪造替代。
 */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const PASSWORD = process.env.P20_PASSWORD || "";
const SAMPLE = process.env.P20_SAMPLE
  || path.join(__dirname, "../../data/import-basis/2026-11-17/historical-success/H01-20251017-BJXFS-XBY.xlsx");
const OUT = process.env.P20_OUT || "/tmp/p20-browser/downloads";
const checks = [];
const check = (name, condition) => {
  assert.ok(condition, name);
  checks.push(name);
  console.log("PASS", name);
};

function sha256File(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

(async () => {
  if (!PASSWORD) throw new Error("缺少 P20_PASSWORD（seed-demo 演示账号密码）");
  if (!fs.existsSync(SAMPLE)) throw new Error(`样本不存在：${SAMPLE}`);
  fs.mkdirSync(OUT, { recursive: true });
  const sampleSha = sha256File(SAMPLE);
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    const contexts = [];
    const login = async (user) => {
      const context = await browser.newContext({ acceptDownloads: true });
      contexts.push(context);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.goto(`${BASE}/uploads/`);
      await page.locator("#login:not([hidden])").waitFor();
      await page.locator('[name="username"]').fill(user);
      await page.locator('[name="password"]').fill(PASSWORD);
      await page.locator("#login button").click();
      // 等真实身份渲染完成：#refresh 一进页面就存在，不能用来判定登录完成。
      await page.locator("#identity").filter({ hasText: user.startsWith("supplier") ? "供应商" : "业务员" }).waitFor();
      await page.locator("#upload-panel:not([hidden]), #claim-panel:not([hidden])").first().waitFor();
      return { context, page, errors };
    };

    // 1) 供应商真实上传真实模板。
    const supplier = await login("supplier_a");
    await supplier.page.locator("#file").setInputFiles(SAMPLE);
    await supplier.page.locator("#file").waitFor({ state: "attached" });
    const uploaded = supplier.page.waitForResponse(
      (response) => response.url() === `${BASE}/api/v1/tasks` && response.request().method() === "POST");
    await supplier.page.locator("#upload-submit").click();
    const response = await uploaded;
    check("真实 multipart 上传 HTTP 200", response.status() === 200);
    const first = await response.json();
    check("解析成功且为 mock 预审", first.parse_status === "succeeded" && first.result_mode === "mock"
      && first.precheck_status === "succeeded");
    await supplier.page.locator("#summary").filter({ hasText: `商品 ${first.item_count} 条` }).waitFor();

    // 2) 供应商看不到可点击的导出（后端只允许业务员/专家导出），并看到原因。
    const supplierExports = supplier.page.locator("#exports");
    await supplierExports.waitFor();
    check("供应商导出按钮被禁用", await supplier.page.locator('#exports button[data-export="precheck"]').isDisabled());
    check("供应商看到不可导出原因",
      (await supplierExports.innerText()).includes("当前账号不能导出审核结果"));

    // 3) 业务员认领后打开同一版本，导出按钮可用且标注执行引用。
    const operator = await login("operator_a");
    await operator.page.locator("#refresh").click();
    await operator.page.locator(`#claimable [data-claim="${first.task_id}"]`).waitFor();
    const claim = operator.page.waitForResponse(
      (r) => r.url() === `${BASE}/api/v1/tasks/${first.task_id}/claim` && r.request().method() === "POST");
    await operator.page.locator(`#claimable [data-claim="${first.task_id}"]`).click();
    check("业务员认领 HTTP 200", (await claim).status() === 200);
    await operator.page.locator(`[data-task="${first.task_id}"]`).click();
    await operator.page.locator("#summary").filter({ hasText: "下载 V1 原件" }).waitFor();
    const exportsPanel = operator.page.locator("#exports");
    await exportsPanel.waitFor();
    const panelText = await exportsPanel.innerText();
    check("导出区标注本次执行与预审状态",
      panelText.includes(first.precheck_run_id) && panelText.includes("succeeded") && panelText.includes("MOCK"));
    const precheckButton = operator.page.locator('#exports button[data-export="precheck"]');
    const issuesButton = operator.page.locator('#exports button[data-export="issues"]');
    check("预审完成后两个导出按钮可用",
      (await precheckButton.isEnabled()) && (await issuesButton.isEnabled()));

    // 4) 真实下载两种 xlsx：浏览器下载事件 + 文件名来自服务端 Content-Disposition。
    const download = async (locator, expectedName) => {
      const [event] = await Promise.all([operator.page.waitForEvent("download"), locator.click()]);
      const target = path.join(OUT, expectedName);
      await event.saveAs(target);
      check(`下载文件名 ${expectedName}`, path.basename(event.suggestedFilename()) === expectedName);
      const bytes = fs.readFileSync(target);
      check(`${expectedName} 是 xlsx（ZIP 容器）`, bytes.length > 1000 && bytes[0] === 0x50 && bytes[1] === 0x4b);
      return target;
    };
    const precheckFile = await download(precheckButton, "预审结果-测试结果.xlsx");
    check("预审导出后页面提示已生成且带执行号",
      (await operator.page.locator("#notice").innerText()).includes(first.precheck_run_id));
    const issuesFile = await download(issuesButton, "独立补料清单-测试结果.xlsx");
    check("补料清单导出后页面提示已生成",
      (await operator.page.locator("#notice").innerText()).includes("独立补料清单-测试结果.xlsx"));

    // 5) 第三方解析器打开两份导出文件（openpyxl，经独立解释器）。
    const openpyxlPython = process.env.P20_OPENPYXL_PYTHON || "/usr/bin/python3";
    const probe = `
import json, sys, openpyxl
out = {}
for path in sys.argv[1:]:
    book = openpyxl.load_workbook(path, data_only=True)
    sheet = book[book.sheetnames[0]]
    out[path] = {"sheets": book.sheetnames,
                 "first_cell": str(sheet.cell(row=1, column=1).value or "")}
print(json.dumps(out, ensure_ascii=False))
`;
    const parsed = JSON.parse(execFileSync(openpyxlPython, ["-c", probe, precheckFile, issuesFile], { encoding: "utf8" }));
    check("openpyxl 能打开预审导出且页签保真",
      parsed[precheckFile].sheets.includes("申报要素") && parsed[precheckFile].sheets.length === 6);
    check("openpyxl 能打开补料清单且为独立清单页签",
      JSON.stringify(parsed[issuesFile].sheets) === JSON.stringify(["补料清单"])
      && parsed[issuesFile].first_cell.includes("独立补料与核对清单"));

    // 6) 导出前后原件字节不变（服务端仍返回同一原件）。
    const original = await operator.context.request.get(`${BASE}${first.original_download_url}`);
    const originalBody = await original.body();
    check("导出前后原件 sha256 不变",
      original.ok() && crypto.createHash("sha256").update(originalBody).digest("hex") === sampleSha);

    check("供应商页面无 JavaScript 异常", supplier.errors.length === 0);
    check("业务员页面无 JavaScript 异常", operator.errors.length === 0);
    for (const context of contexts) await context.close();
    console.log(`PASS ${checks.length} 项；导出文件在 ${OUT}`);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
