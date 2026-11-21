import {createClient} from "../api.js";
import {createRunProgress} from "../components/run-progress.js";
const api = createClient({mode:"live"});
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, x => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[x]));
let me=null, task=null, version=null, epoch=0, requestSequence=0, detailSeq=0, attempt=null, busy=false;
let taskCursor=null, claimCursor=null;
let uploadContext=0, duplicateIntent=null;
let latestReport=null, exporting=false, exportTicket=0;
let importTimer=null, rerunIntent=null;
const refreshedRuns=new Set();
const progress=createRunProgress($("#progress"),{
  client:{getRun:async id=>{const run=await api.getRun(id);if(["succeeded","failed","superseded"].includes(run.status)&&!refreshedRuns.has(id)&&run.task_id===task?.task_id&&run.version_id===version){refreshedRuns.add(id);const token=epoch,seq=detailSeq;setTimeout(()=>{if(token===epoch&&seq===detailSeq&&run.task_id===task?.task_id&&run.version_id===version)guarded(()=>openTask(run.task_id,run.version_id));},0);}return run;}},
  onRetry:intent=>api.retryRun(intent.run_id,intent.item_ids,intent.idempotency_key),
  onSelectItem:entry=>guarded(()=>openItem(entry.item_id))
});
const claimKeys=new Map();
//: 只有这两个导出种类在契约里存在；种类、执行与版本必须同时明确才发请求。
const EXPORTS=[["precheck","导出预审结论（xlsx）"],["issues","导出补料清单（xlsx）"]];
function notice(text,error=false){$("#notice").textContent=text;$("#notice").className=error?"error":"";}
function fail(error){
  const issues=error.details?.issues;
  notice(`${error.status||"网络异常"} ${error.code||"request_failed"}：${error.message}${issues ? "\n"+issues.map(x=>`${x.sheet||"工作簿"} ${x.cell||""} ${x.row?`第${x.row}行`:""}：${x.code} ${x.message}`).join("\n") : ""}${error.details?.import_id ? `\n导入编号：${error.details.import_id}。重试会复用原版本。`:""}`,true);
}
async function guarded(fn){try{await fn();}catch(e){fail(e);}}
function invalidateUploadIntent(){uploadContext++;attempt=null;duplicateIntent=null;$("#duplicate").hidden=true;}
/* --------------------------- 结果导出（LIVE 真实文件） ---------------------------
   导出契约要求结果集必须属于请求版本指定的执行，所以引用只用服务端给的
   version_id + precheck_run_id；引用缺失或预审未完成时只说明原因，不发请求、
   不在前端编造 id，也不在本页兜底生成任何文件。 */
function exportAvailability(report){
  // 后端只允许该任务的业务员/专家导出审核结果；供应商账号在这里就被说明拦住。
  if(!me||(me.role!=="operator"&&me.role!=="expert"))return {ready:false,reason:"当前账号不能导出审核结果（仅该任务的业务员或专家可导出）"};
  if(!report)return {ready:false,reason:"该版本没有上传解析记录，缺少真实执行引用，不能导出"};
  if(!report.precheck_run_id)return {ready:false,reason:"该解析记录没有预审执行编号，不能导出"};
  if(report.precheck_status!=="succeeded")return {ready:false,reason:`预审尚未完成（${String(report.precheck_status)}），结果集不可导出`};
  return {ready:true,reason:""};
}
function hideExports(){latestReport=null;exporting=false;exportTicket++;const box=$("#exports");if(!box)return;box.innerHTML="";box.hidden=true;}
function sourceLabel(mode){return mode === "live" ? "LIVE" : mode === "mock" ? "MOCK" : "未提供或未知";}
function renderExports(report){
  const box=$("#exports");if(!box)return;
  latestReport=report||null;
  if(!report){hideExports();return;}
  const state=exportAvailability(report),source=sourceLabel(report.result_mode);
  const note=state.ready
    ?`结果来源 ${source}。${report.result_mode === "mock" ? "模拟结果不能用于申报。" : "使用前须人工核对。"}导出不退回、不发送通知。`
    :state.reason;
  box.hidden=false;
  box.innerHTML=`<h2>结果导出</h2>
    <p class="muted">V${esc(report.version_number)} · 执行 ${esc(report.precheck_run_id||"（缺少执行编号）")} · 预审 ${esc(report.precheck_status)} · 结果来源 ${esc(source)}</p>
    <div class="heading" style="justify-content:flex-start">${EXPORTS.map(([kind,label])=>
      `<button data-export="${kind}" ${state.ready&&!exporting?"":"disabled"} title="${esc(state.ready?"":"导出不可用："+state.reason)}">${label}</button>`).join("")}</div>
    <p class="muted">${esc(note)}</p>`;
}
function setExportButtonsDisabled(disabled){
  for(const button of document.querySelectorAll("[data-export]"))button.disabled=disabled;
}
async function exportResult(kind){
  const report=latestReport,state=exportAvailability(report);
  if(!state.ready){notice(state.reason,true);return;}
  if(exporting)return;
  const exportRun=++exportTicket;
  exporting=true;setExportButtonsDisabled(true);
  const versionId=report.version_id,runId=report.precheck_run_id;
  // 导出是跨 await 的动作：记下发起时的登录身份代次与详情版本。
  // 响应回来时若身份/版本/引用已变（退出、切任务、切版本、重新选择），整份结果直接丢弃——
  // 不下载、不提示成功、也不把旧请求的错误落到新上下文里。
  const token=epoch,seq=detailSeq,requestVersion=version;
  // 退出登录、切任务/版本都会调用 hideExports()（latestReport 置空）并推进 epoch，
  // 因此「上下文仍有效」= 身份代次不变 + 当前版本变量没被换掉 + 页面上仍是同一份报告。
  const isCurrentExport=()=>token===epoch&&seq===detailSeq&&version===requestVersion
    &&Boolean(latestReport)&&latestReport.version_id===versionId
    &&latestReport.precheck_run_id===runId;
  const pendingText=`正在生成${kind==="precheck"?"预审结论":"补料清单"}…`;
  notice(pendingText);
  try{
    const result=await api.createExport(versionId,{kind,run_id:runId});
    if(!isCurrentExport())return;
    // 服务端回的执行必须就是本页引用的执行；不一致时报错，不把别的结果冒充成本次导出。
    if(result.runId!==runId)throw Object.assign(new Error(`导出引用的执行 ${result.runId||"（缺失）"} 与本页执行 ${runId} 不一致，已拒绝保存`),{status:409,code:"export_reference_mismatch",details:{}});
    if(!String(result.contentType||"").includes("spreadsheet"))throw Object.assign(new Error(`导出响应不是 xlsx（${result.contentType||"无 Content-Type"}），不保存`),{status:0,code:"unexpected_export_type",details:{}});
    const url=URL.createObjectURL(result.blob);
    const link=document.createElement("a");link.href=url;link.download=result.filename;link.rel="noopener";
    document.body.appendChild(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(url),3000);
    notice(`已生成 ${result.filename}（执行 ${result.runId} · 结果来源 ${sourceLabel(result.resultMode)}）`);
  }catch(error){
    if(!isCurrentExport())return;
    fail(error);
  }finally{
    if(exportRun===exportTicket){
      exporting=false;
      setExportButtonsDisabled(!exportAvailability(latestReport).ready);
    }
    // 上下文已变时不要留下「正在生成…」这种会一直挂着的进行中提示。
    if(!isCurrentExport()&&$("#notice").textContent===pendingText)$("#notice").textContent="";
  }
}
//: 详情上下文代次：每次清空详情或重新选择版本都递增。导出的旧响应按它判失效，
//: 这样「A→B→A 回到同一版本同一执行」也不会再被当成同一次选择。
function resetDetail(){clearTimeout(importTimer);importTimer=null;progress.open(null);rerunIntent=null;$("#rerun").hidden=true;task=null;version=null;latestReport=null;requestSequence++;detailSeq++;invalidateUploadIntent();$("#task-title").textContent="选择一个任务";for(const s of ["#versions","#summary","#report","#items","#card"])$(s).innerHTML="";hideExports();}
function identity(){
  epoch++;resetDetail();attempt=null;$("#duplicate").hidden=true;$("#file").value="";
  $("#identity").textContent=me?`${me.display_name||me.username} · ${me.role_label}`:"未登录";
  $("#login").hidden=!!me;$("#logout").hidden=!me;$("#upload-panel").hidden=me?.role!=="supplier";$("#claim-panel").hidden=me?.role!=="operator";
  $("#tasks").innerHTML="";$("#claimable").innerHTML="";
}
async function refresh(append=false,claimsAppend=false){
  if(!me)return;const token=epoch;
  const [tasks,claims]=await Promise.all([api.listTasks({limit:30,cursor:append?taskCursor:null}),me.role==="operator"?api.listClaimable({limit:30,cursor:claimsAppend?claimCursor:null}):null]);
  if(token!==epoch)return;
  taskCursor=tasks.next_cursor;$("#more-tasks").hidden=!taskCursor;
  const html=tasks.items.map(t=>`<button data-task="${esc(t.task_id)}">${esc(t.title||t.task_id)}<br><small>V${esc(t.current_version_number||"?")} · ${esc(t.stage)}</small></button>`).join("");
  if(append)$("#tasks").insertAdjacentHTML("beforeend",html);else $("#tasks").innerHTML=html||'<p class="muted">暂无已归属任务</p>';
  if(claims){claimCursor=claims.next_cursor;$("#more-claimable").hidden=!claimCursor;const html=claims.items.map(t=>`<div><small>${esc(t.task_id)}<br>${esc(t.created_at)}</small><br><button data-claim="${esc(t.task_id)}">认领</button></div>`).join("");if(claimsAppend)$("#claimable").insertAdjacentHTML("beforeend",html);else $("#claimable").innerHTML=html||'<p class="muted">暂无待认领任务</p>';}
}
async function openTask(id,preferred){
  resetDetail();const seq=requestSequence,token=epoch;const data=await api.getTask(id);
  if(seq!==requestSequence||token!==epoch)return;task=data;$("#task-title").textContent=data.title;
  $("#versions").innerHTML=data.versions.map(v=>`<button data-version="${esc(v.version_id)}">V${esc(v.number)} ${esc(v.original_filename)}</button>`).join("");
  if(data.current_version_id)await openVersion(preferred||data.current_version_id);
}
async function openVersion(id){
  clearTimeout(importTimer);importTimer=null;progress.open(null);rerunIntent=null;$("#rerun").hidden=true;
  detailSeq++;
  invalidateUploadIntent();
  // 切版本时先撤掉上一版本的导出区：读取期间不能留着旧版本的导出按钮让人误点。
  hideExports();
  const seq=++requestSequence,token=epoch;version=id;$("#summary").textContent="正在读取解析结果…";$("#items").innerHTML="";$("#card").innerHTML="";$("#report").innerHTML="";
  const pages=[];let cursor=null;do{const page=await api.listItems(id,{limit:100,cursor});if(seq!==requestSequence||token!==epoch)return;pages.push(...page.items);cursor=page.next_cursor;}while(cursor);
  let report=null;try{report=await api.getImport(id);}catch(e){if(e.status!==404)throw e;}
  if(seq!==requestSequence||token!==epoch||version!==id)return;
  const v=task.versions.find(x=>x.version_id===id);
  $("#summary").innerHTML=report?`<b>V${esc(report.version_number)} · ${report.assignment_status==="unclaimed"?"待认领":"已认领"} · 解析 ${esc(report.parse_status)}</b><br>商品 ${esc(report.item_count)} 条 / 装箱成员 ${esc(report.member_count)} 条 / 图片 available ${esc(report.image_counts.available)} · absent ${esc(report.image_counts.absent)} · read_failed ${esc(report.image_counts.read_failed)}<br><a href="${esc(report.original_download_url)}">下载 V${esc(report.version_number)} 原件</a> · <b>${sourceLabel(report.result_mode)}</b> 预审：${esc(report.precheck_status)}`:`V${esc(v?.number)} · 本版本无可用上传解析记录`;
  if(report){const issues=[...(report.report.issues||[]),...(report.report.links||[]).flatMap(l=>[...(l.issues||[]),...(l.record?.issues||[])])];$("#report").innerHTML=`${Object.keys(report.error).length?`<pre class="error">${esc(JSON.stringify(report.error,null,2))}</pre>`:""}<details><summary>解析问题与来源（${issues.length}）</summary><pre>${esc(JSON.stringify(issues,null,2))}</pre></details><details><summary>查看本版本完整解析及装箱关联报告</summary><pre>${esc(JSON.stringify(report.report,null,2))}</pre></details>`;}
  renderExports(report);
  $("#items").innerHTML=pages.length?`<table><thead><tr><th>来源</th><th>品名</th><th>供应商原 HS</th><th>图片</th><th>预审建议</th></tr></thead><tbody>${pages.map(i=>`<tr data-item="${esc(i.item_id)}"><td>${esc(i.source_location.sheet_name)} ${esc(i.source_location.row_number)}</td><td>${esc(i.name)}</td><td>${esc(i.supplier_hs_raw)}</td><td>${esc(i.image_status)}</td><td>${esc(i.precheck_suggested_hs||"未给出")}</td></tr>`).join("")}</tbody></table>`:'<p class="muted">本版本暂无商品，检查导入状态和错误。</p>';
  if(report?.parse_status==="staged"){importTimer=setTimeout(()=>{if(seq===requestSequence&&token===epoch&&version===id)guarded(()=>openVersion(id));},1000);return;}
  $("#rerun").hidden=!(v?.is_current&&task.precheck_allowed===true);
  const runId=v?.is_current&&task.latest_run_id?task.latest_run_id:report?.precheck_run_id;
  if(runId)void progress.open({taskId:task.task_id,versionId:id,runId,isVersionCurrent:!!v?.is_current,versionNumber:v?.number});
}
async function openItem(id){const seq=++requestSequence,token=epoch,v=version;const c=await api.getItem(v,id);if(seq!==requestSequence||token!==epoch||v!==version)return;
  $("#card").innerHTML=`<h2>${esc(c.facts.name)} · ${esc(c.source_location.cells)}</h2><p><b>${sourceLabel(c.result_mode)}</b> · 比较 ${esc(c.comparison_status)} · 图片 ${esc(c.image_status)}</p>${c.images.map(i=>`<p>${esc(i.status)} · ${esc(i.source_cell)} · ${esc(i.error_message)} ${i.download_url?`<a href="${esc(i.download_url)}">下载图片</a><br><img src="${esc(i.download_url)}" alt="${esc(c.facts.name)} 的商品图片">`:""}</p>`).join("")}<details open><summary>原文、来源位置和本版本结果</summary><pre>${esc(JSON.stringify(c,null,2))}</pre></details>`;
}
async function upload(confirmDuplicate=false){
  if(busy)return;
  let intent;
  if(confirmDuplicate){
    // 确认引用原上传意图，不重新读取当前任务或文件。
    intent=duplicateIntent;
    if(!intent||intent.context!==uploadContext||intent.epoch!==epoch)return;
  }else{
    const file=$("#file").files[0];if(!file)return;
    const target=$("#upload-target").value;
    if(target==="current"&&!task)throw new Error("请先选择原任务，再从该任务补料");
    const taskId=target==="current"?task.task_id:null;
    const currentVersionId=target==="current"?task.current_version_id:null;
    const signature=[file.name,file.size,file.lastModified,target,taskId,currentVersionId].join("|");
    if(!attempt||attempt.signature!==signature)attempt={signature,key:crypto.randomUUID()};
    intent=Object.freeze({file,taskId,currentVersionId,key:attempt.key,confirmDuplicate:false,context:uploadContext,epoch});
  }
  const isCurrent=()=>intent.context===uploadContext&&intent.epoch===epoch;
  busy=true;$("#upload-submit").disabled=true;$("#confirm-duplicate").disabled=true;
  notice("正在保存文件并登记处理任务，请稍候…");
  try{
    const result=await api.uploadFile(intent.file,{taskId:intent.taskId,currentVersionId:intent.currentVersionId,key:intent.key,confirmDuplicate:intent.confirmDuplicate});
    if(!isCurrent())return;
    attempt=null;duplicateIntent=null;$("#duplicate").hidden=true;$("#file").value="";
    notice(result.parse_status==="staged"?`上传已受理：V${result.version_number}。后台正在解析和预审，进度会自动更新。`:`上传成功：V${result.version_number}，${result.item_count} 条商品，${result.member_count} 条装箱成员。预审来源 ${sourceLabel(result.result_mode)}，执行 ${result.precheck_status}。`);
    await refresh();if(isCurrent())await openTask(result.task_id,result.version_id);
  }catch(error){
    if(!isCurrent())return;
    if(error.code==="duplicate_file"){
      duplicateIntent=Object.freeze({...intent,key:crypto.randomUUID(),confirmDuplicate:true});
      $("#duplicate").hidden=false;
      notice(`文件与 V${error.details.version_number} 相同，请确认是否创建新版本。`);
    }else fail(error);
  }finally{
    busy=false;$("#upload-submit").disabled=false;$("#confirm-duplicate").disabled=false;
  }
}
$("#login").addEventListener("submit",e=>{e.preventDefault();guarded(async()=>{const f=new FormData(e.target);me=await api.login(f.get("username"),f.get("password"));identity();notice("已登录");await refresh();const requested=new URL(location.href).searchParams.get("task");if(requested)await openTask(requested);});});
$("#logout").onclick=()=>guarded(async()=>{await api.logout();me=null;identity();notice("已退出");});
$("#refresh").onclick=()=>guarded(async()=>{await refresh();if(task)await openTask(task.task_id,version);});
$("#rerun").onclick=()=>guarded(async()=>{if(!task?.precheck_allowed||busy)return;const tid=task.task_id,vid=version;rerunIntent||={key:crypto.randomUUID(),revision:task.revision};$("#rerun").disabled=true;try{await api.startPrecheck(vid,{revision:rerunIntent.revision},rerunIntent.key);rerunIntent=null;notice("已登记新一轮预审，前次结果保留为历史。");await openTask(tid,vid);}finally{$("#rerun").disabled=false;}});
$("#more-tasks").onclick=()=>guarded(()=>refresh(true));
$("#more-claimable").onclick=()=>guarded(()=>refresh(false,true));
$("#upload").addEventListener("submit",e=>{e.preventDefault();guarded(()=>upload());});
$("#confirm-duplicate").onclick=()=>guarded(()=>upload(true));
$("#cancel-duplicate").onclick=()=>{invalidateUploadIntent();notice("已取消创建重复版本");};
$("#file").onchange=invalidateUploadIntent;
$("#upload-target").onchange=invalidateUploadIntent;
document.addEventListener("click",e=>{const b=e.target.closest("[data-task],[data-version],[data-item],[data-claim],[data-export]");if(!b)return;guarded(async()=>{if(b.dataset.export){await exportResult(b.dataset.export);}else if(b.dataset.task)await openTask(b.dataset.task);else if(b.dataset.version)await openVersion(b.dataset.version);else if(b.dataset.item)await openItem(b.dataset.item);else {const id=b.dataset.claim;if(!claimKeys.has(id))claimKeys.set(id,crypto.randomUUID());await api.claimTask(id,claimKeys.get(id));notice("认领成功，已授予该任务访问权限");await refresh();await openTask(id);}});});
guarded(async()=>{me=await api.me();identity();if(me){await refresh();const requested=new URL(location.href).searchParams.get("task");if(requested)await openTask(requested);}});
