// P01 手测页无头浏览器实测（Playwright + 系统 Chrome）。
//
// 前置：Node 与 Playwright（无需下载浏览器，使用本机 Chrome）。
// 运行示例（NODE_PATH 指向任意已安装 playwright 的 node_modules）：
//   BASE=http://127.0.0.1:8010 //   NODE_PATH="$(find ~/.npm/_npx -maxdepth 4 -name playwright -type d | head -1 | xargs dirname)" \
//   node tests/p01/browser_check.js
//
// 注意：让 dev server 指向包含 apps/accounts/static 的代码；新增静态目录后需重启服务。

const { chromium } = require("playwright");
const fs = require("fs");

const BASE = process.env.BASE || "http://127.0.0.1:8000";
const PAGE = `${BASE}/static/accounts/p01_check.html`;
const seed = fs.readFileSync(
  require("path").join(__dirname, "../../apps/accounts/management/commands/seed_demo.py"),
  "utf8",
);
const password = seed.match(/DEMO_PASSWORD = "(.*)"/)[1];

const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : "  ← " + detail}`);
}

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  const page = await browser.newPage();
  const jsErrors = [];
  page.on("pageerror", (error) => jsErrors.push(String(error)));

  await page.goto(PAGE);
  await page.waitForLoadState("networkidle");
  await page.waitForSelector(".entry .badge");

  const entries = () => page.locator(".entry");
  const entryText = async (index) => (await entries().nth(index).innerText()).replace(/\s+/g, " ");

  // 1. 打开页面：匿名查身份 401
  let text = await entryText(0);
  check("打开页面即匿名 401", text.includes("401") && text.includes("unauthorized"), text);
  check("身份卡片显示未登录", (await page.locator("#identity").innerText()).includes("未登录"),
    await page.locator("#identity").innerText());

  // 2. 登录 200
  await page.fill("#password", password);
  await page.click("#btn-login");
  await page.waitForFunction((n) => document.querySelectorAll(".entry").length > n, 1);
  text = await entryText(0);
  check("登录返回 200 且显示业务员角色", text.includes("200") && text.includes("业务员") && text.includes("operator_a"), text);

  // 3. 查当前身份 200
  await page.click("#btn-me");
  await page.waitForFunction((n) => document.querySelectorAll(".entry").length > n, 2);
  text = await entryText(0);
  check("登录后查身份返回 200", text.includes("200") && text.includes("业务员甲"), text);

  // 4. 退出 200
  await page.click("#btn-logout");
  await page.waitForFunction((n) => document.querySelectorAll(".entry").length > n, 3);
  text = await entryText(0);
  check("退出返回 200", text.includes("200") && text.includes("已退出会话"), text);

  // 5. 再查身份 401
  await page.click("#btn-me");
  await page.waitForFunction((n) => document.querySelectorAll(".entry").length > n, 4);
  text = await entryText(0);
  check("退出后查身份 401", text.includes("401") && text.includes("unauthorized"), text);
  check("身份卡片回到未登录", (await page.locator("#identity").innerText()).includes("未登录"),
    await page.locator("#identity").innerText());

  check("页面无 JS 运行时错误", jsErrors.length === 0, jsErrors.join(" | "));

  const shot = process.env.SHOT || "/tmp/p01_check.png";
  await page.screenshot({ path: shot, fullPage: true });
  await browser.close();

  const passed = results.filter(Boolean).length;
  console.log(`\n浏览器实测：${passed}/${results.length} 项通过，截图 ${process.env.SHOT || "/tmp/p01_check.png"}`);
  process.exit(passed === results.length ? 0 : 1);
})().catch((error) => {
  console.error("自动验收失败：", error);
  process.exit(2);
});
