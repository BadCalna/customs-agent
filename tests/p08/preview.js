import {createClient, ApiError} from '/demo/assets/api.js';
import {createTaskList} from '/demo/assets/components/task-list.js';
import {createRunProgress} from '/demo/assets/components/run-progress.js';

const client = createClient({mode:'fixture'});
const data = await (await fetch('/demo/fixtures/DEMO-SET.json')).json();
const scenario = document.querySelector('#scenario');
const versions = document.querySelector('#version');
const title = document.querySelector('#task-title');
const selection = document.querySelector('#selection');
let selectedTask = null;
const progress = createRunProgress(document.querySelector('#progress'), {
  client: {async getRun(id) {
    if (scenario.value === 'network') throw new ApiError(503,'network','连接暂时不可用，请点击重新连接',{},'preview-network');
    const value = structuredClone(await client.getRun(id));
    if (scenario.value === 'expert-no-issues' && value.kind === 'expert_check') {
      value.items = value.items.map(item => ({...item,issues:[]}));
    }
    if (scenario.value === 'running') {
      value.status = 'running'; value.finished_at = null;
      value.items = value.items.map(item => ({...item,status:'queued',comparison_status:null,issues:[],error_message:''}));
      value.progress = {total:value.items.length,queued:value.items.length,running:0,succeeded:0,failed:0};
    }
    return value;
  }},
  retryAllowed: true, // Only the preview simulates authorization; production defaults to false.
  async onRetry() {
    if(scenario.value === 'conflict') throw new ApiError(409,'conflict','当前修订已变化，请刷新后处理',{},'preview-conflict');
    throw new ApiError(501,'not_implemented','后台重试由P13接入，演练未创建新执行',{},'preview-501');
  },
  onSelectItem(value) {selection.textContent = `已选择版本 ${value.version_id} 的商品 ${value.item_id}，接入商品卡片后在此定位。`;},
});
async function openVersion() {
  if (!selectedTask) return;
  const version = selectedTask.versions.find(v=>v.version_id===versions.value);
  const runs = Object.values(data.runs).filter(r=>r.task_id===selectedTask.task_id && r.version_id===versions.value);
  const run = runs.find(r=>r.run_id===selectedTask.latest_run_id) || runs.find(r=>r.is_current) || runs[0];
  await progress.open({taskId:selectedTask.task_id,versionId:versions.value,runId:run?.run_id || null,
    versionNumber:version?.number,isVersionCurrent:versions.value===selectedTask.current_version_id});
}
const tasks = createTaskList(document.querySelector('#tasks'),{client,pageSize:1,
  async onSelectTask(task) {
    selectedTask=task;title.textContent=task.title;
    versions.replaceChildren(...task.versions.map(version=>{
      const option=document.createElement('option');option.value=version.version_id;
      option.textContent=`V${version.number}${version.is_current?' · 当前版本':' · 历史版本'}`;return option;
    }));
    versions.value=task.current_version_id;versions.disabled=false;await openVersion();
  },
});
versions.addEventListener('change',()=>{void openVersion();});
scenario.addEventListener('change',()=>{void openVersion();});
await tasks.load();
if (tasks.getState().items.length) await tasks.selectTask(tasks.getState().items[0].task_id);
window.addEventListener('pagehide',()=>{tasks.destroy();progress.destroy();},{once:true});
