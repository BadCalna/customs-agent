import {createClient} from "../api.js";
const api = createClient({mode:"live"});
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, x => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[x]));
let me=null, task=null, version=null, epoch=0, requestSequence=0, attempt=null, busy=false;
let taskCursor=null, claimCursor=null;
const claimKeys=new Map();
function notice(text,error=false){$("#notice").textContent=text;$("#notice").className=error?"error":"";}
function fail(error){
  const issues=error.details?.issues;
  notice(`${error.status||"网络异常"} ${error.code||"request_failed"}：${error.message}${issues ? "\n"+issues.map(x=>`${x.sheet||"工作簿"} ${x.cell||""} ${x.row?`第${x.row}行`:""}：${x.code} ${x.message}`).join("\n") : ""}${error.details?.import_id ? `\n导入编号：${error.details.import_id}。重试会复用原版本。`:""}`,true);
}
async function guarded(fn){try{await fn();}catch(e){fail(e);}}
function resetDetail(){task=null;version=null;requestSequence++;attempt=null;$("#duplicate").hidden=true;$("#task-title").textContent="选择一个任务";for(const s of ["#versions","#summary","#report","#items","#card"])$(s).innerHTML="";}
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
  const html=tasks.items.map(t=>`<button data-task="${esc(t.task_id)}">${esc(t.title||t.task_id)}<br><small>V${t.current_version_number||"?"} · ${esc(t.stage)}</small></button>`).join("");
  if(append)$("#tasks").insertAdjacentHTML("beforeend",html);else $("#tasks").innerHTML=html||'<p class="muted">暂无已归属任务</p>';
  if(claims){claimCursor=claims.next_cursor;$("#more-claimable").hidden=!claimCursor;const html=claims.items.map(t=>`<div><small>${esc(t.task_id)}<br>${esc(t.created_at)}</small><br><button data-claim="${esc(t.task_id)}">认领</button></div>`).join("");if(claimsAppend)$("#claimable").insertAdjacentHTML("beforeend",html);else $("#claimable").innerHTML=html||'<p class="muted">暂无待认领任务</p>';}
}
async function openTask(id,preferred){
  resetDetail();const seq=requestSequence,token=epoch;const data=await api.getTask(id);
  if(seq!==requestSequence||token!==epoch)return;task=data;$("#task-title").textContent=data.title;
  $("#versions").innerHTML=data.versions.map(v=>`<button data-version="${esc(v.version_id)}">V${v.number} ${esc(v.original_filename)}</button>`).join("");
  if(data.current_version_id)await openVersion(preferred||data.current_version_id);
}
async function openVersion(id){
  const seq=++requestSequence,token=epoch;version=id;$("#summary").textContent="正在读取解析结果…";$("#items").innerHTML="";$("#card").innerHTML="";$("#report").innerHTML="";
  const pages=[];let cursor=null;do{const page=await api.listItems(id,{limit:100,cursor});if(seq!==requestSequence||token!==epoch)return;pages.push(...page.items);cursor=page.next_cursor;}while(cursor);
  let report=null;try{report=await api.getImport(id);}catch(e){if(e.status!==404)throw e;}
  if(seq!==requestSequence||token!==epoch||version!==id)return;
  const v=task.versions.find(x=>x.version_id===id);
  $("#summary").innerHTML=report?`<b>V${report.version_number} · ${report.assignment_status==="unclaimed"?"待认领":"已认领"} · 解析 ${esc(report.parse_status)}</b><br>商品 ${report.item_count} 条 / 装箱成员 ${report.member_count} 条 / 图片 available ${report.image_counts.available} · absent ${report.image_counts.absent} · read_failed ${report.image_counts.read_failed}<br><a href="${esc(report.original_download_url)}">下载 V${report.version_number} 原件</a> · <b>MOCK</b> 预审：${esc(report.precheck_status)}`:`V${v?.number} · 旧演示版本无真实上传记录`;
  if(report){const issues=[...(report.report.issues||[]),...(report.report.links||[]).flatMap(l=>[...(l.issues||[]),...(l.record?.issues||[])])];$("#report").innerHTML=`${Object.keys(report.error).length?`<pre class="error">${esc(JSON.stringify(report.error,null,2))}</pre>`:""}<details><summary>解析问题与来源（${issues.length}）</summary><pre>${esc(JSON.stringify(issues,null,2))}</pre></details><details><summary>查看本版本完整解析及装箱关联报告</summary><pre>${esc(JSON.stringify(report.report,null,2))}</pre></details>`;}
  $("#items").innerHTML=pages.length?`<table><thead><tr><th>来源</th><th>品名</th><th>供应商原 HS</th><th>图片</th><th>mock 建议</th></tr></thead><tbody>${pages.map(i=>`<tr data-item="${esc(i.item_id)}"><td>${esc(i.source_location.sheet_name)} ${i.source_location.row_number}</td><td>${esc(i.name)}</td><td>${esc(i.supplier_hs_raw)}</td><td>${esc(i.image_status)}</td><td>${esc(i.precheck_suggested_hs||"未给出")}</td></tr>`).join("")}</tbody></table>`:'<p class="muted">本版本暂无商品，检查导入状态和错误。</p>';
}
async function openItem(id){const seq=++requestSequence,token=epoch,v=version;const c=await api.getItem(v,id);if(seq!==requestSequence||token!==epoch||v!==version)return;
  $("#card").innerHTML=`<h2>${esc(c.facts.name)} · ${esc(c.source_location.cells)}</h2><p><b>MOCK</b> · 比较 ${esc(c.comparison_status)} · 图片 ${esc(c.image_status)}</p>${c.images.map(i=>`<p>${esc(i.status)} · ${esc(i.source_cell)} · ${esc(i.error_message)} ${i.download_url?`<a href="${esc(i.download_url)}">下载图片</a><br><img src="${esc(i.download_url)}" alt="${esc(c.facts.name)} 的商品图片">`:""}</p>`).join("")}<details open><summary>原文、来源位置和本版本结果</summary><pre>${esc(JSON.stringify(c,null,2))}</pre></details>`;
}
async function upload(confirmDuplicate=false){
  if(busy)return;const f=$("#file").files[0];if(!f)return;
  const target=$("#upload-target").value;
  if(target==="current"&&!task)throw new Error("请先选择原任务，再从该任务补料");
  const signature=[f.name,f.size,f.lastModified,target,task?.task_id,task?.current_version_id,confirmDuplicate].join("|");
  if(!attempt||attempt.signature!==signature)attempt={signature,key:crypto.randomUUID()};
  busy=true;$("#upload-submit").disabled=true;$("#confirm-duplicate").disabled=true;notice("正在上传、解析并执行 mock 预审，请稍候…");
  const token=epoch;
  try{const result=await api.uploadFile(f,{taskId:target==="current"?task.task_id:null,currentVersionId:task?.current_version_id,key:attempt.key,confirmDuplicate});
    if(token!==epoch)return;attempt=null;$("#duplicate").hidden=true;$("#file").value="";notice(`上传成功：V${result.version_number}，${result.item_count} 条商品，${result.member_count} 条装箱成员。预审为 mock，执行 ${result.precheck_status}。`);await refresh();await openTask(result.task_id,result.version_id);
  }catch(e){if(token!==epoch)return;if(e.code==="duplicate_file"){$("#duplicate").hidden=false;notice(`文件与 V${e.details.version_number} 相同，请确认是否创建新版本。`);}else fail(e);}
  finally{busy=false;$("#upload-submit").disabled=false;$("#confirm-duplicate").disabled=false;}
}
$("#login").addEventListener("submit",e=>{e.preventDefault();guarded(async()=>{const f=new FormData(e.target);me=await api.login(f.get("username"),f.get("password"));identity();notice("已登录");await refresh();});});
$("#logout").onclick=()=>guarded(async()=>{await api.logout();me=null;identity();notice("已退出");});
$("#refresh").onclick=()=>guarded(()=>refresh());
$("#more-tasks").onclick=()=>guarded(()=>refresh(true));
$("#more-claimable").onclick=()=>guarded(()=>refresh(false,true));
$("#upload").addEventListener("submit",e=>{e.preventDefault();guarded(()=>upload());});
$("#confirm-duplicate").onclick=()=>guarded(()=>upload(true));
$("#cancel-duplicate").onclick=()=>{$("#duplicate").hidden=true;attempt=null;notice("已取消创建重复版本");};
$("#file").onchange=()=>{$("#duplicate").hidden=true;attempt=null;};
$("#upload-target").onchange=()=>{$("#duplicate").hidden=true;attempt=null;};
document.addEventListener("click",e=>{const b=e.target.closest("[data-task],[data-version],[data-item],[data-claim]");if(!b)return;guarded(async()=>{if(b.dataset.task)await openTask(b.dataset.task);else if(b.dataset.version)await openVersion(b.dataset.version);else if(b.dataset.item)await openItem(b.dataset.item);else {const id=b.dataset.claim;if(!claimKeys.has(id))claimKeys.set(id,crypto.randomUUID());await api.claimTask(id,claimKeys.get(id));notice("认领成功，已授予该任务访问权限");await refresh();await openTask(id);}});});
guarded(async()=>{me=await api.me();identity();if(me)await refresh();});
