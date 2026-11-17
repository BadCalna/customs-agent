/* 真正启动浏览器并走 HTTP API。未能启动服务/浏览器时退出非零，不用 fixture 替代。 */
const {chromium}=require("playwright");
const assert=require("node:assert/strict");
const fs=require("node:fs");
const crypto=require("node:crypto");
const BASE=process.env.BASE||"http://127.0.0.1:8006";
const SAMPLE=process.env.P06_SAMPLE||"data/private/p06/synthetic.xlsx";
const REORDER=process.env.P06_REORDER||(process.env.P06_SAMPLE?"":"data/private/p06/synthetic-reordered.xlsx");
const SHOT=process.env.P06_SCREENSHOT||".local/p06-browser.png";
const PASSWORD=process.env.P06_DEMO_PASSWORD||"customs-demo-only";
let browser;
const outputs=[];
const check=(name,condition)=>{assert.ok(condition,name);outputs.push(name);console.log("PASS",name);};
(async()=>{
 browser=await chromium.launch({channel:"chrome",headless:true});
 const contexts=[];
 async function login(user){const context=await browser.newContext();contexts.push(context);const page=await context.newPage();await page.goto(BASE+"/uploads/");await page.locator("#login:not([hidden])").waitFor();await page.locator('[name="username"]').fill(user);await page.locator('[name="password"]').fill(PASSWORD);await page.locator('#login button').click();await page.locator("#identity").filter({hasText:user.startsWith("supplier")?"供应商":"业务员"}).waitFor();return {context,page};}
 const supplier=await login("supplier_a");const page=supplier.page;
 const pageErrors=[];page.on("pageerror",error=>pageErrors.push(String(error)));
 await page.locator("#file").setInputFiles(SAMPLE);
 const uploaded=page.waitForResponse(r=>r.url()===BASE+"/api/v1/tasks"&&r.request().method()==="POST");
 await page.locator("#upload-submit").click();const response=await uploaded;check("真实 multipart 上传 HTTP 200",response.status()===200);const first=await response.json();
 await page.locator("#summary").filter({hasText:`商品 ${first.item_count} 条`}).waitFor();
 check("解析成功且页面明确 mock",first.parse_status==="succeeded"&&first.result_mode==="mock"&&(await page.locator("#summary").innerText()).includes("MOCK"));
 check("待认领且有商品",first.assignment_status==="unclaimed"&&first.item_count>0);
 const original=await supplier.context.request.get(BASE+first.original_download_url);check("原件受控读取字节与样本一致",original.ok()&&crypto.createHash("sha256").update(await original.body()).digest("hex")===crypto.createHash("sha256").update(fs.readFileSync(SAMPLE)).digest("hex"));
 await page.locator("#upload-target").selectOption("current");await page.locator("#file").setInputFiles(SAMPLE);await page.locator("#upload-submit").click();await page.locator("#duplicate:not([hidden])").waitFor();
 check("相同文件显式提示确认",(await page.locator("#notice").innerText()).includes("相同"));
 const newVersion=page.waitForResponse(r=>r.url().endsWith(`/tasks/${first.task_id}/versions`)&&r.status()===200);await page.locator("#confirm-duplicate").click();const second=await (await newVersion).json();check("确认后新增 V2 与独立预审",second.version_number===2&&second.precheck_run_id!==first.precheck_run_id);await page.locator("#summary").filter({hasText:"V2"}).waitFor();
 const a=await login("operator_a"),b=await login("operator_b");
 for(const actor of [a,b]){const denied=await actor.context.request.get(BASE+first.original_download_url);check("未认领业务员原件 404",denied.status()===404);}
 const token=async ctx=>(await ctx.cookies()).find(c=>c.name==="csrftoken").value;
 const claims=await Promise.all([a,b].map(async(actor,index)=>({actor,response:await actor.context.request.post(`${BASE}/api/v1/tasks/${first.task_id}/claim`,{headers:{"X-CSRFToken":await token(actor.context),"Idempotency-Key":`browser-claim-${index}-${first.task_id}`}})})));
 check("两个业务员竞争只有一人成功",claims.filter(x=>x.response.status()===200).length===1&&claims.filter(x=>x.response.status()===409).length===1);
 const winner=claims.find(x=>x.response.status()===200).actor;await winner.page.locator("#refresh").click();await winner.page.locator(`[data-task="${first.task_id}"]`).click();await winner.page.locator("#summary").filter({hasText:"V2"}).waitFor();await winner.page.locator("tr[data-item]").first().click();await winner.page.locator("#card h2").waitFor();check("认领后可查看真实商品卡片",(await winner.page.locator("#card").innerText()).includes("原文"));
 if(fs.existsSync(REORDER)){
  await page.locator("#file").setInputFiles(REORDER);const r=page.waitForResponse(x=>x.url().endsWith(`/tasks/${first.task_id}/versions`)&&x.status()===200);await page.locator("#upload-submit").click();const third=await(await r).json();check("重排新增 V3",third.version_number===3);await page.locator("#summary").filter({hasText:"V3"}).waitFor();
 }
 await page.locator(`[data-version="${first.version_id}"]`).click();await page.locator("#summary").filter({hasText:"V1"}).waitFor();check("V1 原件与结果保留",(await page.locator("#summary").innerText()).includes("下载 V1 原件"));
 check("页面无 JavaScript 异常",pageErrors.length===0);
 await page.screenshot({path:SHOT,fullPage:true});console.log(JSON.stringify({passed:outputs.length,sample:SAMPLE,first_upload:first.item_count,screenshot:SHOT}));
 await browser.close();
})().catch(async error=>{console.error("FAIL",error.message);if(browser)await browser.close();process.exitCode=1;});
