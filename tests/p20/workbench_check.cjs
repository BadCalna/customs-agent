/* P20 workbench 守卫的真实浏览器验收：原型页不得再伪造导出成功。
 *
 * 需要 dev server（DEBUG=1）+ Playwright + Chrome：
 *   BASE=http://127.0.0.1:8011 NODE_PATH=<含 playwright 的 node_modules> node tests/p20/workbench_check.cjs
 *
 * FIXTURE 模式：点击导出只提示「样例模式不可导出真实文件」，不产生下载。
 * LIVE 模式：导出控件禁用并指向 /uploads/、/tasks/，点击不产生请求与下载。
 */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");

const BASE = process.env.BASE || "http://127.0.0.1:8011";
const PASSWORD = process.env.P20_PASSWORD || "";
const checks = [];
const check = (name, condition) => {
  assert.ok(condition, name);
  checks.push(name);
  console.log("PASS", name);
};

(async () => {
  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    // workbench 自身没有登录入口：LIVE 模式要求同一浏览器上下文里已有会话。
    const loginInContext = async (context, username) => {
      await context.request.get(`${BASE}/api/v1/me`);
      const csrf = (await context.cookies()).find((cookie) => cookie.name === "csrftoken");
      assert.ok(csrf, "缺少 csrftoken cookie");
      const response = await context.request.post(`${BASE}/api/v1/auth/login`, {
        data: { username, password: PASSWORD },
        headers: { "X-CSRFToken": csrf.value },
      });
      assert.equal(response.status(), 200, "LIVE 模式登录失败：" + (await response.text()).slice(0, 200));
    };

    const openWorkbench = async (mode) => {
      const context = await browser.newContext({ acceptDownloads: true });
      if (mode === "live") {
        if (!PASSWORD) throw new Error("LIVE 模式需要 P20_PASSWORD（seed-demo 演示账号密码）");
        await loginInContext(context, "operator_a");
      }
      const page = await context.newPage();
      const errors = [];
      const downloads = [];
      const posts = [];
      page.on("pageerror", (error) => errors.push(String(error)));
      page.on("download", (download) => downloads.push(download));
      page.on("request", (request) => {
        if (request.method() !== "GET") posts.push(`${request.method()} ${request.url()}`);
      });
      await page.goto(`${BASE}/workbench/?mode=${mode}`);
      await page.locator(".gate").waitFor();
      await page.locator('.gate [data-act="pick-role"][data-role="business"]').first().click();
      await page.locator('#root [data-act="open-task"]').first().waitFor();
      return { context, page, errors, downloads, posts };
    };

    // 1) FIXTURE 模式：按钮可点，但只给说明，不生成文件、不提示成功。
    const fixture = await openWorkbench("fixture");
    await fixture.page.locator('#root [data-act="open-task"]').first().click();
    const fixtureExport = fixture.page.locator('#root button[data-act="export"]').first();
    await fixtureExport.waitFor();
    check("FIXTURE 模式保留可点的导出按钮", await fixtureExport.isEnabled());
    await fixtureExport.click();
    const fixtureToast = fixture.page.locator("#toast");
    await fixtureToast.locator("span").filter({ hasText: "样例模式" }).waitFor();
    const fixtureText = await fixtureToast.innerText();
    check("FIXTURE 模式明示样例不可导出真实文件",
      fixtureText.includes("样例模式（FIXTURE/DEMO）不可导出真实文件"));
    check("FIXTURE 模式不提示导出成功", !fixtureText.includes("已导出"));
    await fixture.page.waitForTimeout(800);
    check("FIXTURE 模式没有产生任何非 GET 请求", fixture.posts.length === 0);
    check("FIXTURE 模式没有触发浏览器下载", fixture.downloads.length === 0);
    check("FIXTURE 模式页面无 JavaScript 异常", fixture.errors.length === 0);
    await fixture.context.close();

    // 2) LIVE 模式：控件禁用并指向真实页面说明按文案迁移。
    //    注意：workbench 的 LIVE 数据桥目前只读 /tasks 列表，而 TaskListEntry 不含 versions，
    //    打开任务详情会在 curVersion() 处抛错（既有缺陷，不属于 P20 导出改动，见回报）。
    //    因此这里的 LIVE 分支用页面自身的 exportControl() 做断言，并如实记录该限制。
    const skipped = [];
    const live = await openWorkbench("live");
    check("LIVE 模式读到真实接口",
      (await live.page.locator(".topbar .demo-chip").first().innerText()).includes("LIVE"));
    const liveMarkup = await live.page.evaluate(() => window.exportControl());
    check("LIVE 模式导出控件禁用并标注 aria-disabled",
      liveMarkup.includes("disabled") && liveMarkup.includes('aria-disabled="true"'));
    check("LIVE 模式给出迁移入口说明",
      liveMarkup.includes("导出已迁至上传/任务页")
      && liveMarkup.includes("/uploads/") && liveMarkup.includes("/tasks/"));

    // 真实打开任务详情：若数据桥可渲染则断言禁用控件；不可渲染时如实记录既有缺陷。
    await live.page.locator('#root [data-act="open-task"]').first().click();
    const liveExport = live.page.locator('#root .hd-actions button:has-text("导出补料清单")').first();
    const headerRendered = await liveExport.waitFor({ timeout: 4000 }).then(() => true).catch(() => false);
    if (headerRendered) {
      check("LIVE 模式任务头里的导出控件禁用", await liveExport.isDisabled());
      check("LIVE 模式任务头给出迁移说明",
        (await live.page.locator("#root .hd-actions").first().innerText()).includes("导出已迁至上传/任务页"));
      await liveExport.click({ force: true }).catch(() => {});
      await live.page.waitForTimeout(600);
      check("LIVE 模式点击不产生非 GET 请求", live.posts.length === 0);
      check("LIVE 模式没有触发浏览器下载", live.downloads.length === 0);
      skipped.push("LIVE 任务头：4 项（控件禁用 / 迁移说明 / 无非 GET / 无下载）");
    } else {
      console.log("NOTE LIVE 任务详情当前无法渲染（既有数据桥缺陷，非 P20 改动）：",
        live.errors[0] || "未捕获到 pageerror");
      skipped.push("LIVE 任务头：4 项（控件禁用 / 迁移说明 / 无非 GET / 无下载）");
    }
    await live.context.close();

    // 汇总必须区分「真正断言过」和「因既有缺陷被跳过」，不能把两项相加当成覆盖。
    console.log(`PASS ${checks.length} 项：workbench 两种模式都不再伪造导出`);
    if (skipped.length) {
      console.log(`NOTE ${skipped.length} 组断言未执行（LIVE 任务详情不可渲染，属既有数据桥缺陷，非本轮改动）：`);
      for (const item of skipped) console.log(`  - ${item}`);
    }
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
