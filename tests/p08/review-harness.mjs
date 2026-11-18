/** Minimal DOM for actual P11 events; does not stand in for browser acceptance. */
export function element() {
  return {innerHTML:'',dataset:{},listeners:new Map(),removed:false,
    addEventListener(k,v){this.listeners.set(k,v);},removeEventListener(k){this.listeners.delete(k);},
    contains:()=>true,querySelector:()=>null,remove(){this.removed=true;},focus(){}};
}
export function installDocument(){globalThis.document={activeElement:null,createElement:element,getElementById:()=>null};}
export function host(){return {child:null,dataset:{},innerHTML:'',replaceChildren(node){this.child=node||null;this.innerHTML='';}};}
export function field(root,name,value,item='') {
  const input={dataset:{raField:name,item},value};
  root.listeners.get('input')({target:{closest:selector=>selector==='[data-ra-field]'?input:null}});
}
export function click(root,act,item='') {
  const button={dataset:{raAct:act,item}};
  root.listeners.get('click')({target:{closest:selector=>selector==='[data-ra-act]'?button:null}});
}
export async function flush(){for(let i=0;i<3;i++)await new Promise(r=>setImmediate(r));}
export function expertState() {
  const rows=['first','second','third'].map((id,i)=>({task_id:'T',version_id:'V',item_id:id,name:['合成甲','合成乙','合成丙'][i],
    revision:i+1,source_location:{sheet_name:'申报要素',row_number:[8,15,22][i],cells:`A${[8,15,22][i]}:J${[8,15,22][i]}`}}));
  return {taskId:'T',versionId:'V',task:{task_id:'T',current_version_id:'V',versions:[{version_id:'V',number:1}],revision:2,allowed_actions:['read','decision.resolve']},
    items:rows,loading:false,card:null,itemId:null,runId:'R',run:{run_id:'R',kind:'expert_check',submission_id:'S',status:'succeeded',is_current:true},
    history:{versions:[{version_id:'V',submissions:[{submission_id:'S',revision:2,status:'pending',items:[
      {item_id:'third',submitted_hs:'3333333333'}, {item_id:'first',submitted_hs:'1111111111'}, {item_id:'second',submitted_hs:'2222222222'}]}]}]}};
}
