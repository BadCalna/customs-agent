/* Run with Node and Playwright available on NODE_PATH. All API traffic is local and stubbed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "../..");
const app = path.join(root, "apps/agent_debug");
const requests = [];
const findings = ["elements", "images", "tax", "regulatory"].map((aspect) => ({
  aspect, verdict: "unknown", reason: aspect === "images" ? "图片未接入" : "<img src=x onerror=alert(1)>", evidence_ids: aspect === "elements" ? ["hash-1", "hash-2"] : [],
}));
const run = {
  id: "expert-run", stage: "expert_check", mode: "mock", status: "succeeded", business_status: "undetermined",
  content_visible: true, can_rerun: true, target_hs: "0101210010", suggested_hs: null,
  findings, materials: [
    { evidence_id: "hash-1", label: "要素材料", url: "/ops/data/evidence?kind=hs&snapshot_id=test&record_id=test" },
    { evidence_id: "hash-2", label: "不可定位材料", url: "javascript:alert(1)" },
  ], gaps: [{ code: "IMAGE_NOT_CONNECTED", message: "图片未接入，保持待核实" }], outbound_counts: { attempts: 0, responses: 0, parsed: 0 },
};
findings.find((f) => f.aspect === "regulatory").verdict = "matched";
findings.find((f) => f.aspect === "regulatory").reason = "来源核对相符";
run.regulatory = { outcome: "measures_required", verified: true,
  measures: [{ code: "A", category: "inspection", evidence_ids: ["hash-2"] }] };
run.tax_check = { direction: "import", applicability: "not_applicable" };
const server = http.createServer(async (req, res) => {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  const url = new URL(req.url, "http://localhost");
  requests.push({ url: url.pathname + url.search, body: raw ? JSON.parse(raw) : null });
  res.setHeader("Content-Type", "application/json");
  if (url.pathname.startsWith("/static/agent_debug/")) {
    res.setHeader("Content-Type", url.pathname.endsWith(".js") ? "text/javascript" : "text/css");
    res.end(fs.readFileSync(path.join(app, "static/agent_debug", path.basename(url.pathname))));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    let data;
    if (url.pathname.endsWith("/capabilities")) data = {
      stages: ["precheck", "expert_check"], modes: ["mock", "live"], snapshot_ready: true,
      samples: [{ id: "demo", label: "预审样例", stage: "precheck", mode: "mock" },
        { id: "expert-demo", label: "专家样例", stage: "expert_check", mode: "mock" }],
    };
    else if (url.pathname.endsWith("/previews")) data = {
      ticket: "ticket", stage: "expert_check", model_required: false,
      destination: { target: "mock", host: "本地模拟" }, versions: {}, warnings: [],
    };
    else if (url.pathname.endsWith("/steps")) data = { items: [] };
    else if (url.pathname.endsWith("/runs")) data = req.method === "POST" ? run : { items: [run] };
    else data = run;
    res.end(JSON.stringify(data));
    return;
  }
  const template = url.pathname.includes("/runs/") ? (url.pathname.endsWith("/expert-run/") ? "run_detail.html" : "runs.html") : "index.html";
  const html = fs.readFileSync(path.join(app, "templates/agent_debug", template), "utf8")
    .replace(/{% load static %}/g, "").replace(/{% static '([^']+)' %}/g, "/static/$1");
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.end(html);
});

(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  let browser;
  try {
    const options = { headless: true };
    if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE) options.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
    browser = await chromium.launch(options);
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(base + "/ops/debug/");
    await page.waitForSelector(".sample");
    assert.equal(await page.locator("#expertFields").isVisible(), false);
    assert.equal(await page.locator('.sample[data-id="demo"]').isVisible(), true);
    await page.selectOption("#stageSelect", "expert_check");
    assert.equal(await page.locator("#expertFields").isVisible(), true);
    assert.equal(await page.locator('.sample[data-id="demo"]').isVisible(), false);
    await page.fill('[name="name"]', "脱敏商品");
    await page.fill('[name="submitted_hs"]', "0101210010");
    await page.fill('[name="supplier_tax_rate_raw"]', "13%");
    await page.selectOption('[name="direction"]', "export");
    await page.fill('[name="declaration_date"]', "2026-11-25");
    await page.click("#previewBtn");
    await page.waitForSelector("#runBtn");
    const manual = requests.filter((r) => r.url.endsWith("/previews")).at(-1).body;
    assert.equal(manual.stage, "expert_check");
    assert.equal(manual.expert.submitted_hs, "0101210010");
    assert.equal(manual.expert.supplier_tax_rate_raw, "13%");
    assert.equal(manual.query_basis.declaration_date, "2026-11-25");
    assert.match(await page.locator("#previewView").innerText(), /本次不调用模型/);
    await page.click("#runBtn");
    await page.waitForSelector(".finding-card");
    assert.equal(await page.locator(".finding-card").count(), 4);
    assert.equal(await page.locator(".finding-card img").count(), 0);
    assert.equal(await page.locator('.finding-card a[href^="/ops/data/evidence?"]').count(), 1);
    assert.equal(await page.locator('.finding-card a[href^="javascript:"]').count(), 0);
    assert.match(await page.locator("#runView").innerText(), /MOCK · 合成模拟/);
    assert.match(await page.locator("#runView").innerText(), /仅验证调试流程，不构成真实业务核查/);
    assert.match(await page.locator(".result-gaps").innerText(), /IMAGE_NOT_CONNECTED.*图片未接入/);
    assert.match(await page.locator('.finding-card').nth(3).innerText(), /有监管要求/);
    assert.match(await page.locator('.finding-card').nth(3).innerText(), /A.*inspection/);
    assert.match(await page.locator('.finding-card').nth(2).innerText(), /本项出口退税核查不适用/);
    assert.match(await page.locator('.finding-card').nth(3).innerText(), /未提供可定位链接/);
    await page.selectOption("#stageSelect", "precheck");
    assert.equal(await page.locator("#runView").isVisible(), false);
    await page.selectOption("#stageSelect", "expert_check");
    await page.fill('[name="tariff_version"]', "invalid-old-free-input");
    await page.click('.sample[data-id="expert-demo"]');
    assert.equal(await page.locator('[name="submitted_hs"]').isDisabled(), true);
    assert.equal(await page.locator('[name="tariff_version"]').isDisabled(), true);
    assert.equal(await page.locator('[name="declaration_date"]').isDisabled(), true);
    await page.click("#previewBtn");
    await page.waitForSelector("#runBtn");
    const sample = requests.filter((r) => r.url.endsWith("/previews")).at(-1).body;
    assert.equal(sample.sample_id, "expert-demo");
    assert.equal(sample.expert, undefined);
    assert.equal(sample.facts, undefined);
    await page.goto(base + "/ops/debug/?parent_run_id=expert-run");
    await page.waitForSelector(".sample", { state: "attached" });
    assert.equal(await page.locator("#stageSelect").inputValue(), "expert_check");
    assert.equal(await page.locator("#stageSelect").isDisabled(), true);
    assert.equal(await page.locator('[name="tariff_version"]').isDisabled(), true);
    await page.click("#previewBtn");
    await page.waitForSelector("#runBtn");
    const parent = requests.filter((r) => r.url.endsWith("/previews")).at(-1).body;
    assert.deepEqual(parent, { mode: "mock", stage: "expert_check", parent_run_id: "expert-run" });
    await page.goto(base + "/ops/debug/runs/expert-run/");
    await page.waitForSelector(".finding-card");
    assert.equal(await page.locator(".finding-card").count(), 4);
    assert.match(await page.locator("#runDetail").innerText(), /MOCK · 合成模拟/);
    run.regulatory = { outcome: "declared_none", verified: true, measures: [] };
    await page.reload();
    await page.waitForSelector(".finding-card");
    assert.match(await page.locator('.finding-card').nth(3).innerText(), /来源明确无要求/);
    assert.doesNotMatch(await page.locator('.finding-card').nth(3).innerText(), /有监管要求/);
    delete run.regulatory;
    run.tax_check = { direction: "unknown", applicability: "direction_unknown" };
    findings.find((f) => f.aspect === "tax").verdict = "conflict";
    findings.find((f) => f.aspect === "tax").reason = "来源比较冲突";
    await page.reload();
    await page.waitForSelector(".finding-card");
    assert.match(await page.locator('.finding-card').nth(3).innerText(), /待核实/);
    assert.doesNotMatch(await page.locator('.finding-card').nth(3).innerText(), /来源核对相符|来源明确无要求/);
    assert.match(await page.locator('.finding-card').nth(2).innerText(), /方向待确认/);
    assert.doesNotMatch(await page.locator('.finding-card').nth(2).innerText(), /相符|存在冲突|来源比较冲突/);
    run.tax_check = { direction: "export", applicability: "applies", resolved_version: "2026C", comparison: "conflict" };
    await page.reload();
    await page.waitForSelector(".finding-card");
    assert.match(await page.locator('.finding-card').nth(2).innerText(), /存在冲突/);
    delete run.tax_check;
    findings.find((f) => f.aspect === "tax").verdict = "matched";
    findings.find((f) => f.aspect === "tax").reason = "来源核对相符";
    await page.reload();
    await page.waitForSelector(".finding-card");
    assert.match(await page.locator('.finding-card').nth(2).innerText(), /待核实/);
    assert.match(await page.locator('.finding-card').nth(2).innerText(), /旧记录缺贸易方向核验信息.*复制重跑/);
    assert.doesNotMatch(await page.locator('.finding-card').nth(2).innerText(), /相符|存在冲突/);
    run.tax_check = { direction: "export", applicability: "pending" };
    await page.reload();
    await page.waitForSelector(".finding-card");
    assert.match(await page.locator('.finding-card').nth(2).innerText(), /待核实/);
    assert.doesNotMatch(await page.locator('.finding-card').nth(2).innerText(), /相符|存在冲突/);
    run.regulatory = { outcome: "measures_required", verified: false,
      measures: [{ code: "UNVERIFIED", category: "untrusted", evidence_ids: [] }] };
    await page.reload();
    await page.waitForSelector(".finding-card");
    assert.match(await page.locator('.finding-card').nth(3).innerText(), /待核实/);
    assert.doesNotMatch(await page.locator('.finding-card').nth(3).innerText(), /UNVERIFIED|有监管要求/);
    if (process.env.OPS_DEBUG_UI_SCREENSHOT) {
      fs.mkdirSync(path.dirname(process.env.OPS_DEBUG_UI_SCREENSHOT), { recursive: true });
      await page.screenshot({ path: process.env.OPS_DEBUG_UI_SCREENSHOT, fullPage: true });
    }
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(base + "/ops/debug/runs/");
    await page.waitForSelector(".data-table");
    assert.match(await page.locator(".data-table").innerText(), /MOCK · 合成模拟/);
    await page.selectOption("#stage", "expert_check");
    await page.waitForTimeout(50);
    assert.ok(requests.some((r) => r.url.includes("runs?stage=expert_check")));
    assert.equal(errors.length, 0, errors.join("\n"));
    console.log("Expert debug UI: manual input, samples, pinned rerun, four findings, escaped content, filters passed.");
  } finally {
    if (browser) await browser.close();
    server.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
