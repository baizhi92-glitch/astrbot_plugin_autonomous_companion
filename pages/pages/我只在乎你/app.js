"use strict";
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const timeText = value => value ? new Date(value * 1000).toLocaleString("zh-CN", {hour12:false}) : "尚未安排";
const labels = {active:"进行中",paused:"已暂停",completed:"已完成",cancelled:"已取消",chat:"聊天",create:"创作",tools:"工具行动",silent:"保持沉默",dispatched:"消息已交给平台",dispatch_attempt:"尝试发送",tool_result:"工具返回"};
Object.assign(labels,{heartbeat:"周期心跳",continuation:"短时续行",event:"事件触发",running:"执行中",deferred:"已暂缓",blocked:"条件受阻",failed:"执行失败",tool_notice:"工具状态提示",condition_check:"条件检查",service_check:"服务检查",work_saved:"作品保存回执"});
let data = null, bridge = null, busy = false, dirty = false, planDirty = false, loadedForm = false, refreshing = false;
let connectionFailed=false;
const workspaceDrafts=new Set();
function notice(text) { $("notice").textContent=text; $("notice").hidden=false; }
function tab(name) {
  document.querySelectorAll(".view").forEach(el=>el.hidden=el.id!==name);
  document.querySelectorAll(".tab").forEach(el=>{el.classList.toggle("active",el.dataset.tab===name);el.setAttribute("aria-selected",String(el.dataset.tab===name));});
}
async function api(method, endpoint, body) {
  if (!bridge) throw new Error("请从 AstrBot 左侧栏打开本页面");
  let timer;
  const pending=method === "GET" ? bridge.apiGet("page/"+endpoint) : bridge.apiPost("page/"+endpoint,body);
  try{return await Promise.race([pending,new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error("请求超时；操作可能仍在后台执行，请刷新确认后再试")),endpoint==="preview"?135000:90000);})]);}
  finally{clearTimeout(timer);}
}
async function refresh() {
  if(busy || refreshing || activePageDialogs) return;
  refreshing=true;
  try {
    data=await api("GET","snapshot");
    if(!activePageDialogs)render();
    $("updated").textContent="更新于 "+new Date().toLocaleTimeString("zh-CN");
    $("live").textContent=data.enabled ? "心跳已启用" : "已暂停";
    $("live").classList.toggle("good",data.enabled);
    if(connectionFailed){notice("连接已恢复，状态已更新。");connectionFailed=false;}
  } catch(error) {connectionFailed=true;notice("读取失败："+error.message+"；当前显示的是上次成功读取的数据。");$("live").textContent="连接失败";}
  finally {refreshing=false;}
}
function sessions() {
  const all = new Map(data.sessions.map(s=>[s.umo,s]));
  data.targets.forEach(umo=>{if(!all.has(umo))all.set(umo,{umo,active:true,running:false,state:{}});});
  return [...all.values()].filter(s=>s.active);
}
function render() {
  const list=sessions(), projects=list.flatMap(s=>s.state.projects||[]);
  $("metrics").innerHTML=[["陪伴对象",list.length],["正在行动",list.filter(s=>s.running).length],["持续项目",projects.filter(p=>p.status==="active").length],["待处理事件",list.reduce((n,s)=>n+(s.state.events||[]).length,0)]].map(([label,value])=>`<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
  const unresolved = (data.config.targets||[]).length && !list.length;
  $("sessions").innerHTML=list.map(s=>`<article class="card session"><div class="row"><h3>${escapeHTML(s.state.identity?.user_name||s.umo.split(":").slice(2).join(":"))}</h3><span class="badge">${s.running?"行动中":data.enabled?"等待下一次心跳":"已暂停"}</span></div><div class="origin">${escapeHTML(s.umo)}</div><dl class="details"><dt>下次判断</dt><dd>${timeText(s.state.next_due)}</dd><dt>未来计划</dt><dd>${escapeHTML(s.state.future_task||"还没有计划，让想法自然发生")}</dd><dt>记忆联动</dt><dd>${escapeHTML(s.state.memory_status||"首次行动时检查")}</dd></dl>${s.state.last_error?`<p class="error">${escapeHTML(s.state.last_error)}</p>`:""}<div class="actions"><button data-action="now" data-umo="${escapeHTML(s.umo)}">立即判断</button><button data-action="stop" data-umo="${escapeHTML(s.umo)}">结束本轮</button><button data-workspace="${escapeHTML(s.umo)}">查看计划</button></div></article>`).join("") || `<div class="empty"><strong>${unresolved?"对象平台尚未识别":"从一个陪伴对象开始"}</strong>${unresolved?"请在设置中选择对应的 QQ 平台实例。":"在设置中填入对象 QQ 号，保存后自动持续运行。"}</div>`;
  ["workspace-target","activity-target"].forEach(id=>{
    const selected=$(id).value;
    $(id).innerHTML=list.map(s=>`<option value="${escapeHTML(s.umo)}">${escapeHTML(s.state.identity?.user_name||s.umo.split(":").slice(2).join(":"))}</option>`).join("");
    if(list.some(s=>s.umo===selected))$(id).value=selected;
  });
  // Avoid replacing an editor while the user is typing.
  if(!planDirty && !$("workspace-content").contains(document.activeElement)) renderWorkspace();
  renderActivity();
  if(!loadedForm) renderSettings();
  document.querySelectorAll('[data-action="now"]').forEach(button=>{const s=list.find(s=>s.umo===button.dataset.umo);button.disabled=Boolean(!data.enabled||s?.running||s?.state.pause_until>Date.now()/1000);});
}
function selectedSession(id) { return sessions().find(s=>s.umo===$(id).value); }
function renderWorkspace() {
  if(activePageDialogs)return;
  planDirty=false;
  workspaceDrafts.clear();
  const s=selectedSession("workspace-target");
  $("workspace-target").dataset.previous=$("workspace-target").value;
  if(!s){$("workspace-content").innerHTML='<div class="empty">先添加一个陪伴对象，计划和项目会保存在这里。</div>';return;}
  $("workspace-content").innerHTML=`<article class="card plan"><div class="row"><h3>下一次想做的事</h3><span class="badge">跨小时 · 跨重启</span></div><p>她会结合新对话重新判断，也可以在这里替她记下一件事。</p><textarea id="plan-editor" maxlength="2000" aria-label="未来计划">${escapeHTML(s.state.future_task||"")}</textarea><button class="primary" id="save-plan">保存计划</button></article><div class="section-heading"><h3>持续项目</h3><span class="badge">${(s.state.projects||[]).length} / 5</span></div><div class="project-grid">${(s.state.projects||[]).map(p=>`<article class="card project"><div class="row"><h3>${escapeHTML(p.title)}</h3><span class="badge">${labels[p.status]||p.status}</span></div>${[["目标",p.goal],["实际进展",p.progress],["下一步",p.next_step],["作品位置 / 引用",p.artifact]].filter(([,v])=>v).map(([k,v])=>`<span class="field-title">${k}</span><p>${escapeHTML(v)}</p>`).join("")}<label><span class="field-title">项目状态</span><select data-project="${escapeHTML(p.id)}" aria-label="${escapeHTML(p.title)}的状态">${["active","paused","completed","cancelled"].map(status=>`<option value="${status}" ${p.status===status?"selected":""}>${labels[status]}</option>`).join("")}</select></label></article>`).join("")||'<div class="empty">还没有持续项目。聊聊故事、画作或共同兴趣，她可以从中产生项目。</div>'}</div><article class="card preferences"><h3>她记住的明确偏好</h3>${Object.entries(s.state.preferences||{}).map(([key,p])=>`<div class="preference"><b>${escapeHTML(key)}</b><div>${escapeHTML(p.value)}</div><small>依据：${escapeHTML(p.evidence)}</small></div>`).join("")||'<p>只记录有用户原话依据的偏好，不从沉默推断喜恶。</p>'}</article>`;
  const integration=document.createElement("p");
  integration.textContent="陪伴适配："+(s.state.companion_status||"首次行动时自动检查");
  $("workspace-content").prepend(integration);
  const controls=document.createElement("div");controls.className="actions";
  const add=document.createElement("button");add.textContent="添加持续项目";
  add.onclick=async()=>{const title=await pagePrompt("项目名称");if(!title?.trim())return;const goal=await pagePrompt("希望完成的目标");if(goal===null)return;perform({umo:s.umo,action:"project_add",title,goal});};
  const preview=document.createElement("button");preview.textContent="仅预览判断（调用模型，不执行行动）";
  preview.onclick=()=>perform({umo:s.umo,action:"preview"});
  controls.append(add,preview);$("workspace-content").prepend(controls);
  renderDailyAndOutbox(s);
  document.querySelectorAll("[data-project]").forEach(el=>{
    const p=s.state.projects.find(p=>p.id===el.dataset.project);
    const meta=document.createElement("p");meta.textContent="最后推进："+timeText(p.last_progress_at)+" · 连续失败："+(p.failure_count||0)+(p.last_error?" · "+p.last_error:"");el.closest("article").append(meta);
    const remove=document.createElement("button");remove.textContent="删除项目";remove.onclick=async()=>{if(await pageConfirm("删除此项目及其进展记录？"))perform({umo:s.umo,action:"project_delete",id:p.id});};el.closest("article").append(remove);
  });
  $("plan-editor").oninput=()=>{workspaceDrafts.add("plan");planDirty=true;};
  const planStatus=document.createElement("p");planStatus.textContent="手动任务（明确执行指令）："+(s.state.plan_status||"尚未手动保存")+"。保存后下次主动会直接执行；暂停、预算及工具条件仍生效。可在概览点击立即判断。";$("plan-editor").after(planStatus);
  $("save-plan").onclick=()=>perform({action:"plan",umo:s.umo,value:$("plan-editor").value});
  const cancelPlan=document.createElement("button");cancelPlan.textContent="取消临时主动任务";cancelPlan.onclick=()=>perform({action:"cancel_plan",umo:s.umo});$("save-plan").after(cancelPlan);
  const expiry=document.createElement("p");expiry.textContent=s.state.manual_plan_expires_at?"临时任务有效至："+timeText(s.state.manual_plan_expires_at):"手动任务为临时安排，保存后按设置的有效期执行";cancelPlan.after(expiry);
  document.querySelectorAll("[data-project]").forEach(el=>el.onchange=()=>perform({action:"project",umo:s.umo,id:el.dataset.project,status:el.value}));
}
function renderActivity() {
  const s=selectedSession("activity-target");
  const events=s?.state.events||[], journal=[...(s?.state.journal||[])].reverse();
  $("activity-content").innerHTML=`${events.length?`<article class="card plan"><h3>待处理事件</h3>${events.map(e=>`<p>${escapeHTML(e.source)} · ${escapeHTML(e.text)}</p>`).join("")}</article>`:""}<div class="timeline">${journal.map(item=>`<article class="card"><div class="row"><span class="badge">${labels[item.action]||escapeHTML(item.action)}</span><time>${escapeHTML(item.time)}</time></div><pre>${escapeHTML(item.summary)}</pre></article>`).join("")||'<div class="empty">还没有行动记录。她选择沉默、推进项目或调用工具后，会在这里留下记录。</div>'}</div>`;
  if(s?.state.last_turn){const turn=s.state.last_turn,detail=document.createElement("article");detail.className="card plan";detail.innerHTML='<h3>最近一轮 · 决策详情</h3>';const fields={trigger:"触发来源",status:"执行状态",action:"选择",task:"目标",reason:"简短依据",summary:"结果摘要",blocked_reason:"阻碍原因",no_message_reason:"未发消息原因",error:"错误",deliveries:"发送接口调用数"};for(const [key,title] of Object.entries(fields)){if(turn[key]!==undefined){const p=document.createElement("p");p.textContent=title+"："+String(labels[turn[key]]||turn[key]);detail.append(p);}}$("activity-content").prepend(detail);}
}
const groups=[
  ["陪伴对象与运行",["enabled","targets","platform_id","companion_integration","general_execution_contract","task_completion_message","natural_task_closing","check_comfyui_each_round","comfyui_url","template_mcp_server"]],
  ["节奏与自主性",["manual_task_ttl_minutes","adaptive_frequency","frequency_min_minutes","frequency_max_minutes","interval_minutes","jitter_minutes","continuation_window_minutes","max_rounds","chat_planning","tool_event_wakeup","extra_prompt"]],
  ["高级 · 工具与上下文",["max_tool_calls","max_agent_steps","turn_timeout_seconds","tool_timeout_seconds","recent_chat_grace_seconds","history_messages","history_chars","memory_timeout_seconds","allowed_tools"]],
  ["每日预算 · 每个对象独立计算",["ignore_budget_while_active","daily_check_limit","daily_round_limit","daily_tool_limit","daily_message_limit"]],
  ["通用条件查询",["condition_query_tools","condition_file_roots"]]
];
const names={enabled:"持续运行",targets:"对象 QQ 号",platform_id:"QQ 平台实例",interval_minutes:"基础检查间隔 / 分钟",jitter_minutes:"随机浮动 / 分钟",continuation_window_minutes:"短时续行窗口 / 分钟",max_rounds:"每个窗口最多回合",chat_planning:"从日常聊天整理计划",tool_event_wakeup:"工具返回触发事件",extra_prompt:"额外行为偏好",allowed_tools:"限定可用工具"};
function renderSettings() {
  names.companion_integration="可选适配我会永远陪着你";
  $("config-fields").innerHTML=groups.map(([title,keys],i)=>`<${i===2?"details":"section"} class="card settings-group">${i===2?`<summary>${title}</summary>`:`<h3>${title}</h3>`}<div class="settings-grid">${keys.filter(k=>data.schema[k]).map(key=>{
    const spec=data.schema[key], value=data.config[key], title=names[key]||spec.description.split("（")[0];
    if(spec.type==="bool") return `<label class="control switch"><div><span>${escapeHTML(title)}</span><small>${escapeHTML(spec.description)}</small></div><input type="checkbox" data-config="${key}" ${value?"checked":""}></label>`;
    let input;
    if(key==="platform_id") input=`<select data-config="${key}"><option value="">自动识别唯一 OneBot 平台</option>${data.platforms.map(p=>`<option value="${escapeHTML(p.id)}" ${value===p.id?"selected":""}>${escapeHTML(p.id)} · ${escapeHTML(p.name)}</option>`).join("")}${value&&!data.platforms.some(p=>p.id===value)?`<option selected value="${escapeHTML(value)}">${escapeHTML(value)}（当前未加载）</option>`:""}</select>`;
    else if(spec.type==="list"||spec.type==="text")input=`<textarea data-config="${key}" ${spec.type==="text"?'maxlength="10000"':""} placeholder="${key==="targets"?"每行一个 QQ 号，例如：123456789":""}">${escapeHTML(Array.isArray(value)?value.join("\n"):value)}</textarea>`;
    else input=`<input data-config="${key}" type="${spec.type==="int"?"number":"text"}" ${spec.type==="int"?'min="0" max="100000"':""} value="${escapeHTML(value)}">`;
    return `<label class="control ${spec.type==="text"||key==="targets"?"full":""}"><span>${escapeHTML(title)}</span>${input}<small>${escapeHTML(spec.description)}</small></label>`;
  }).join("")}</div></${i===2?"details":"section"}>`).join("");
  loadedForm=true; dirty=false;
}
async function perform(body) {
  if(busy)return;
  busy=true;
  document.querySelectorAll("button").forEach(b=>b.disabled=true);
  try {const result=await api("POST","action",body);if(["plan","conditions"].includes(body.action))workspaceDrafts.delete(body.action);if(body.action==="cancel_plan"){workspaceDrafts.delete("plan");if($("plan-editor"))$("plan-editor").value="";}planDirty=workspaceDrafts.size>0;notice(result.decision?"预览："+JSON.stringify(result.decision):body.action==="now"?"已安排自主判断，她可以选择行动或沉默。":body.action==="cancel_plan"?"临时任务已取消，不再恢复。":"已保存");}
  catch(error){notice("操作失败："+error.message);}
  finally {busy=false;document.querySelectorAll("button").forEach(b=>b.disabled=false);await refresh();if(!planDirty)renderWorkspace();}
}
$("config-form").addEventListener("input",()=>{dirty=true;});
$("config-form").onsubmit=async event=>{
  event.preventDefault(); if(busy)return;
  const config={};
  document.querySelectorAll("[data-config]").forEach(el=>{const key=el.dataset.config,type=data.schema[key].type;config[key]=type==="bool"?el.checked:type==="int"?Number(el.value):type==="list"?el.value.split(/[\n,，]+/).map(v=>v.trim()).filter(Boolean):el.value;});
  busy=true;$("save-settings").disabled=true;
  try{await api("POST","config",config);dirty=false;loadedForm=false;notice("设置已保存，调度将自动采用新配置。");}
  catch(error){notice("保存失败："+error.message);}
  finally{busy=false;$("save-settings").disabled=false;await refresh();}
};
document.addEventListener("click",async event=>{
  const button=event.target.closest("button");if(!button)return;
  if(button.dataset.tab)tab(button.dataset.tab);
  if(button.dataset.action)perform({umo:button.dataset.umo,action:button.dataset.action});
  if(button.dataset.workspace){if(planDirty&&!await pageConfirm("当前有未保存的编辑，切换会丢弃，是否继续？"))return;tab("workspace");$("workspace-target").value=button.dataset.workspace;renderWorkspace();}
});
$("refresh").onclick=refresh;
$("diagnose").onclick=async()=>{const button=$("diagnose");button.disabled=true;try{const report=await api("GET","diagnostics");$("diagnostics-result").innerHTML=report.targets.map(row=>`<div class="preferences"><b>${escapeHTML(row.target)}</b>${row.checks.map(c=>`<p><span class="badge">${{ok:"正常",waiting:"待确认",error:"异常"}[c.status]||c.status}</span> ${escapeHTML(c.name)} · ${escapeHTML(c.detail)}</p>`).join("")}</div>`).join("")||"请先在设置添加对象。";notice(report.note);}catch(error){notice("自检失败："+error.message);}finally{button.disabled=false;}};
$("open-settings").onclick=()=>tab("settings");
$("workspace-target").onchange=async()=>{if(planDirty&&!await pageConfirm("当前有未保存的编辑，切换会丢弃，是否继续？")){$("workspace-target").value=$("workspace-target").dataset.previous;return;}renderWorkspace();};
$("activity-target").onchange=renderActivity;
window.addEventListener("beforeunload",event=>{if(dirty||planDirty){event.preventDefault();event.returnValue="";}});
(async()=>{try{bridge=window.AstrBotPluginPage;if(!bridge)throw new Error("请从 AstrBot 左侧栏打开自主陪伴页面");await bridge.ready();await refresh();setInterval(()=>{if(!document.hidden)refresh();},15000);}catch(error){notice(error.message);}})();
