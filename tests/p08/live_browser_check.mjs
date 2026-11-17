/** Optional real-browser acceptance. Run only against an isolated test service; never stubs APIs/storage. */
import assert from 'node:assert/strict';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import path from 'node:path';

const required=name=>{const value=process.env[name];if(!value)throw Error(`Set ${name}; credentials/files must stay outside Git`);return value;};
const base=required('P08_BASE_URL').replace(/\/$/,'');
const source=path.resolve(required('P08_UPLOAD_FILE'));
const supplier=required('P08_SUPPLIER_USERNAME'),operator=required('P08_OPERATOR_USERNAME');
const supplierPassword=required('P08_SUPPLIER_PASSWORD'),operatorPassword=required('P08_OPERATOR_PASSWORD');
const output=path.resolve(process.env.P08_OUTPUT_DIR||'.local/p08-p20-browser');await mkdir(output,{recursive:true});
const {chromium}=await import(process.env.P08_PLAYWRIGHT_MODULE||'playwright');
const browser=await chromium.launch({headless:process.env.P08_HEADLESS!=='0'});
const report={entry:'/workbench/',page:'live',engine:'mock',executor:'inline',checks:[],unverified:['专业归类质量','真实队列与P13重试'],downloads:[]};
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
async function check(name,run){await run();report.checks.push(name);}
async function login(page,username,password){
 await page.goto(base+'/uploads/');await page.locator('#login input[name=username]').fill(username);
 await page.locator('#login input[name=password]').fill(password);await page.locator('#login button').click();
 await page.locator('#logout').waitFor({state:'visible'});
}
try{
 const supplierContext=await browser.newContext({acceptDownloads:true});const s=await supplierContext.newPage();
 await login(s,supplier,supplierPassword);
 const uploaded=s.waitForResponse(r=>r.url()===base+'/api/v1/tasks'&&r.request().method()==='POST');
 await s.locator('#file').setInputFiles(source);await s.locator('#upload-submit').click();
 const response=await uploaded;assert.equal(response.status(),200);const uploadedTask=await response.json();
 const id=uploadedTask.task_id;
 report.task_id=id;report.version_id=uploadedTask.version_id;report.run_id=uploadedTask.precheck_run_id;
 await check('真实页面上传返回持久化任务',async()=>{assert.ok(id);assert.equal(uploadedTask.result_mode,'mock');});
 const operatorContext=await browser.newContext({acceptDownloads:true});const page=await operatorContext.newPage();await login(page,operator,operatorPassword);
 await page.locator(`[data-claim="${id}"]`).click();await page.locator(`#tasks [data-task="${id}"]`).waitFor();
 const original=page.waitForEvent('download');await page.locator('#summary a').first().click();
 const originalDownload=await original;const originalPath=path.join(output,'original.xlsx');await originalDownload.saveAs(originalPath);
 await check('P05 原件下载哈希与上传一致',async()=>{assert.equal(hash(await readFile(originalPath)),hash(await readFile(source)));});
 await page.goto(base+'/workbench/');await page.locator(`#wb-tasks [data-task="${id}"]`).click();
 await page.locator('#wb-progress .p08-run-rows button[data-item]').first().waitFor();
 await check('任务高亮、真实 Run 与进度',async()=>{
  assert.equal(await page.locator(`#wb-tasks [data-task="${id}"]`).getAttribute('aria-current'),'true');
  assert.ok((await page.locator('#wb-progress').innerText()).includes(report.run_id));
 });
 await page.locator('#wb-progress [data-item]').first().click();
 await check('执行明细定位正式 P07 卡片',async()=>{await page.locator('#wb-card article.commodity-card').waitFor();});
 for(const kind of ['precheck','issues']){
  const pending=page.waitForEvent('download');await page.locator(`[data-export="${kind}"]`).click();
  const download=await pending;const filename=download.suggestedFilename();assert.ok(filename.endsWith('.xlsx'));assert.ok(filename.includes('测试结果'));
  const destination=path.join(output,kind+'-'+filename);await download.saveAs(destination);
  const parse=execFileSync(process.env.P08_PYTHON||'.venv-customs/bin/python',['-c',
   'import sys,zipfile,xml.etree.ElementTree as E; z=zipfile.ZipFile(sys.argv[1]); E.fromstring(z.read("xl/workbook.xml")); assert any(n.startswith("xl/worksheets/") for n in z.namelist()); print("XLSX parsed")',destination],{encoding:'utf8'});
  assert.ok(parse.includes('XLSX parsed'));report.downloads.push({kind,filename,sha256:hash(await readFile(destination))});report.checks.push(kind+' 浏览器下载及 XLSX 解析');
 }
 await page.screenshot({path:path.join(output,'workbench.png'),fullPage:true});
 report.original_sha256=hash(await readFile(originalPath));
 report.unverified.push('快速切换版本/任务的实际网络晚响应','专家阶段及其补料清单','真实失败行禁用说明','实际客户端图片/公式显示');
 await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
 console.log(JSON.stringify({checks:report.checks,unverified:report.unverified,output},null,2));
}finally{await browser.close();}
