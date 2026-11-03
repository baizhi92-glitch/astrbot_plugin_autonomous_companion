"use strict";
let data = null, bridge = null, busy = false, dirty = false, loadedForm = false, refreshing = false;
let connectionFailed = false;
let activeTab = "overview";
let noticeTimer = 0;
const rendered = {};

function notice(text, tone = "") {
  $("notice-text").textContent = text;
  $("notice").dataset.tone = tone;
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, tone === "bad" ? 20000 : 9000);
}

function tab(name) {
  activeTab = name;
  document.querySelectorAll(".view").forEach(element => { element.hidden = element.id !== name; });
  document.querySelectorAll(".tab").forEach(element => {
    const on = element.dataset.tab === name;
    element.classList.toggle("active", on);
    element.setAttribute("aria-selected", String(on));
  });
  renderView(name);
}

async function api(method, endpoint, body) {
  if (!bridge) throw new Error("请从 AstrBot 左侧栏打开本页面");
  let timer;
  const pending = method === "GET" ? bridge.apiGet("page/" + endpoint) : bridge.apiPost("page/" + endpoint, body);
  try {
    return await Promise.race([pending, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("请求超时；操作可能仍在后台执行，请刷新确认后再试")), endpoint === "preview" ? 135000 : 90000);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

async function refresh() {
  if (busy || refreshing || activePageDialogs) return;
  refreshing = true;
  try {
    data = await api("GET", "snapshot");
    if (data.ui_revision !== "ui-3.0") notice("网页已更新，但后台仍是旧版接口，请重载「我只在乎你」插件后再刷新。", "bad");
    if (!activePageDialogs) render();
    $("updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
    $("live").textContent = data.enabled ? "心跳已启用" : "已暂停";
    $("live").className = "pill " + (data.enabled ? "ok" : "");
    if (connectionFailed) {
      notice("连接已恢复，状态已更新。", "ok");
      connectionFailed = false;
    }
  } catch (error) {
    connectionFailed = true;
    notice("读取失败：" + error.message + "；当前显示上次成功读取的数据。", "bad");
    $("live").textContent = "连接失败";
    $("live").className = "pill bad";
  } finally {
    refreshing = false;
  }
}

function sessions() {
  const all = new Map(data.sessions.map(item => [item.umo, item]));
  data.targets.forEach(umo => { if (!all.has(umo)) all.set(umo, { umo, active: true, running: false, state: {} }); });
  return [...all.values()].filter(item => item.active);
}

function displayName(session) {
  return session.state.identity?.user_name || session.umo.split(":").slice(2).join(":");
}

function selectedSession(id) {
  const select = $(id);
  return select ? sessions().find(session => session.umo === select.value) : undefined;
}

function changed(key, value) {
  const next = JSON.stringify(value);
  if (rendered[key] === next) return false;
  rendered[key] = next;
  return true;
}

function renderMetrics(list) {
  const projects = list.flatMap(item => item.state.projects || []);
  const pending = list.reduce((count, item) => count + (item.state.outbox || []).filter(entry => entry.status === "pending").length, 0);
  const active = list.filter(item => item.running).length;
  const cards = [
    ["👤", "accent", "陪伴对象", list.length],
    ["⚡", active ? "ok" : "", "正在行动", active],
    ["📁", projects.filter(item => item.status === "active").length ? "accent" : "", "持续项目", projects.filter(item => item.status === "active").length],
    ["📤", pending ? "warn" : "", "待分享", pending],
  ];
  $("metrics").innerHTML = cards.map(([icon, tone, label, value]) =>
    `<div class="stat" data-tone="${tone}"><span class="stat-icon">${icon}</span><div><b>${value}</b><span>${label}</span></div></div>`
  ).join("");
}

function renderSessions(list) {
  if (!changed("sessions", [data.enabled, list.map(item => [item.umo, item.running, item.state.next_due, item.state.future_task, item.state.last_error, item.state.pause_until, item.state.last_turn?.status])])) return;
  const unresolved = (data.config.targets || []).length && !list.length;
  $("sessions").innerHTML = list.map(session => {
    const paused = session.state.pause_until > Date.now() / 1000;
    const status = session.running ? ["行动中", "accent"] : data.enabled && !paused ? ["等待心跳", "ok"] : ["已暂停", ""];
    const next = session.state.next_due ? timeText(session.state.next_due) : "尚未安排";
    const inMin = session.state.next_due ? Math.max(0, Math.round((session.state.next_due - Date.now() / 1000) / 60)) : null;
    return `<article class="item">
      <div class="grow">
        <div class="item-title">${escapeHTML(displayName(session))}<span class="pill ${status[1]}">${status[0]}</span></div>
        <div class="item-sub mono">${escapeHTML(session.umo)}</div>
        <div class="item-sub">下次判断 ${next}${inMin !== null ? `（约 ${inMin} 分钟后）` : ""} · ${escapeHTML(session.state.future_task || "没有固定计划，让想法自然发生")}</div>
        ${session.state.last_error ? `<div class="item-sub" style="color:var(--bad)">${escapeHTML(session.state.last_error)}</div>` : ""}
      </div>
      <div class="actions">
        <button class="btn-small" data-action="now" data-umo="${escapeHTML(session.umo)}" ${(!data.enabled || session.running || paused) ? "disabled" : ""} type="button">立即判断</button>
        <button class="btn-small" data-action="stop" data-umo="${escapeHTML(session.umo)}" type="button">结束本轮</button>
        <button class="btn-small" data-open-plan="${escapeHTML(session.umo)}" type="button">计划</button>
      </div>
    </article>`;
  }).join("") || `<div class="empty">${unresolved ? "对象平台尚未识别，请在设置中选择 QQ 平台实例。" : "在设置中填入对象 QQ 号，保存后自动持续运行。"}</div>`;
}

function fillTargets(list) {
  const signature = list.map(item => [item.umo, displayName(item)]);
  if (!changed("targets", signature)) return;
  ["overview-target", "plans-target", "works-target", "activity-target"].forEach(id => {
    const select = $(id);
    if (!select) return;
    const selected = select.value;
    select.innerHTML = list.map(item => `<option value="${escapeHTML(item.umo)}">${escapeHTML(displayName(item))}</option>`).join("");
    if (list.some(item => item.umo === selected)) select.value = selected;
    select.dataset.previous = select.value;
  });
}

function renderUsage() {
  const session = selectedSession("overview-target");
  const host = $("usage");
  if (!session) { host.innerHTML = '<p class="empty">先添加一个陪伴对象。</p>'; return; }
  const usage = session.state.usage || {};
  const config = data.config;
  const pause = session.state.pause_until > Date.now() / 1000;
  const signature = [session.umo, usage, session.state.pause_until, session.state.pause_mode, session.state.available_tool_names, session.state.available_skill_names, config.daily_check_limit, config.daily_round_limit, config.daily_tool_limit, config.daily_message_limit];
  if (!changed("usage", signature)) return;
  host.innerHTML = `
    <p class="hint">${escapeHTML(usage.day || "今日")} · 判断 ${usage.checks || 0}/${config.daily_check_limit || "∞"} · 执行 ${usage.rounds || 0}/${config.daily_round_limit || "∞"} · 工具 ${usage.tools || 0}/${config.daily_tool_limit || "∞"} · 发送尝试 ${usage.messages || 0}/${config.daily_message_limit || "∞"} · 失败 ${usage.failures || 0}</p>
    <p>${pause ? `暂停至 ${timeText(session.state.pause_until)}（${session.state.pause_mode === "messages" ? "仅不发消息" : "全部行动"}）` : "正常运行"}</p>
    <div class="actions">
      <button data-pause="all" type="button">暂停全部 1 小时</button>
      <button data-pause="messages" type="button">仅免打扰 1 小时</button>
      <button data-pause="custom" type="button">自定义</button>
      <button data-pause="resume" type="button">恢复运行</button>
    </div>
    <details class="work"><summary><span class="work-kind">本轮</span><span class="work-title">可用工具与技能</span></summary><div class="work-detail"><div class="stack"><p class="hint">工具：${escapeHTML((session.state.available_tool_names || []).join("、") || "尚无记录")}</p><p class="hint">技能：${escapeHTML((session.state.available_skill_names || []).join("、") || "尚无记录")}</p></div></div></details>
  `;
  host.querySelectorAll("[data-pause]").forEach(button => {
    button.onclick = async () => {
      let mode = button.dataset.pause, minutes = 60;
      if (mode === "resume") { perform({ umo: session.umo, action: "resume" }); return; }
      if (mode === "custom") {
        const value = await pagePrompt("暂停多少分钟？1～10080", "120");
        if (value === null) return;
        minutes = Number(value);
        if (!Number.isInteger(minutes) || minutes < 1 || minutes > 10080) { notice("请输入 1～10080 的整数分钟。", "bad"); return; }
        mode = await pageChoose("选择暂停方式", [{ label: "只暂停消息", value: "messages" }, { label: "暂停全部行动", value: "all" }]);
        if (mode === null) return;
      }
      perform({ umo: session.umo, action: "pause", mode, minutes });
    };
  });
}

function renderActivity() {
  const session = selectedSession("activity-target");
  const host = $("activity-content");
  if (!session) { host.innerHTML = '<div class="empty">先添加一个陪伴对象。</div>'; return; }
  const signature = [session.umo, session.state.events, session.state.journal, session.state.last_turn, session.state.evidence, session.state.stalls, session.state.comfyui_observation, session.state.progress, session.state.template_mcp_status, session.state.memory_status];
  if (!changed("activity", signature)) return;
  const events = session.state.events || [];
  const journal = [...(session.state.journal || [])].reverse().slice(0, 40);
  const turn = session.state.last_turn || {};
  const progress = session.state.progress || {};
  const phases = { preparing: "准备会话", checking: "检查条件", deciding: "选择行动", executing: "执行任务", tool: "调用工具", sending: "提交消息", idle: "等待下一轮" };
  const fields = { trigger: "触发", action: "选择", task: "目标", reason: "依据", summary: "摘要", blocked_reason: "受阻", no_message_reason: "未发消息", error: "错误", verification: "核验" };
  fields.project_update_warning = "项目更新提示";
  fields.continuation_status = "续行安排";
  const verificationLabels = { verified: "已核实", unverified: "未核实", unexecuted: "未执行", observed: "已观察", failed: "失败" };
  host.innerHTML = `
    <section class="panel">
      <div class="panel-head">
        <div><h2>最近一轮</h2><p class="hint">${session.running ? "正在行动 · " + escapeHTML(phases[progress.phase] || "启动中") + (progress.detail ? " · " + escapeHTML(progress.detail) : "") : "等待下一轮"}${progress.run_id ? " · 运行 " + escapeHTML(progress.run_id) : ""}</p></div>
        <span class="pill ${turn.status === "completed" ? "ok" : turn.status === "failed" ? "bad" : ""}">${escapeHTML(turn.status === "completed" ? (turn.continuation_scheduled ? "本轮结束 · 已安排续行" : turn.manual_task_pending ? "本轮结束 · 任务保留" : "本轮结束") : (labels[turn.status] || turn.status || "暂无"))}</span>
      </div>
      <div class="panel-body stack">
        <div class="kv">
          ${Object.entries(fields).filter(([key]) => turn[key] !== undefined && turn[key] !== "").map(([key, label]) => `<div class="kv-item ${["reason", "summary", "error", "blocked_reason"].includes(key) ? "wide" : ""}"><span class="hint">${label}</span><b>${escapeHTML(key === "verification" ? (verificationLabels[turn[key]] || turn[key]) : (labels[turn[key]] || turn[key]))}</b></div>`).join("") || '<p class="hint">还没有完成过一轮判断。</p>'}
        </div>
        ${turn.verification_reason ? `<p class="hint">${escapeHTML(turn.verification_reason)}</p>` : ""}
        ${turn.execution_contract ? `<details class="work"><summary><span class="work-kind">契约</span><span class="work-title">本轮执行步骤</span><span class="work-meta">${(turn.execution_contract.steps || []).length} 步</span></summary><div class="work-detail"><div class="stack">${(turn.execution_contract.steps || []).map(step => `<p class="hint">${escapeHTML(step.goal)} · 工具 ${escapeHTML((step.tools || []).join("、") || "无")} · ${escapeHTML(step.criterion || step.accept || "")}</p>`).join("")}</div></div></details>` : ""}
      </div>
    </section>

    ${events.length ? `<section class="panel"><div class="panel-head"><div><h2>待处理事件</h2></div></div><div class="list">${events.map(item => `<div class="item"><div class="grow"><div class="item-sub">${escapeHTML(item.source)}</div><div>${escapeHTML(item.text)}</div></div></div>`).join("")}</div></section>` : ""}

    <section class="panel">
      <div class="panel-head"><div><h2>行动记录</h2><p class="hint">最近 ${journal.length} 条决策与结果。</p></div></div>
      <div class="list">
        ${journal.map(item => `<div class="item"><div class="grow"><div class="item-title"><span class="pill">${escapeHTML(labels[item.action] || item.action)}</span><span class="hint">${escapeHTML(item.time)}</span></div><pre class="text-preview" style="margin-top:6px;max-height:220px">${escapeHTML(item.summary)}</pre></div></div>`).join("") || '<p class="empty">还没有行动记录。</p>'}
      </div>
    </section>

    <details class="panel">
      <summary><span>事实凭据与重复失败</span><span class="hint">${(session.state.evidence || []).length} 条凭据 · ${Object.keys(session.state.stalls || {}).length} 项暂缓</span></summary>
      <div class="panel-body stack">
        ${(session.state.evidence || []).slice(-10).reverse().map(item => `<p class="hint">${timeText(item.at)} · ${escapeHTML(item.source)} · ${escapeHTML(item.text)}</p>`).join("") || '<p class="hint">暂无凭据。</p>'}
        ${Object.entries(session.state.stalls || {}).map(([key, item]) => `<p class="hint">${escapeHTML(key.split(":")[0])} · 同一失败 ${item.count} 次 · ${escapeHTML(item.reason)}${item.until > Date.now() / 1000 ? " · 暂缓至 " + timeText(item.until) : ""}</p>`).join("")}
        ${session.state.comfyui_observation ? `<p class="hint">ComfyUI ${timeText(session.state.comfyui_observation.checked_at)} · ${escapeHTML(session.state.comfyui_observation.url)} · ${escapeHTML(session.state.comfyui_observation.detail)}</p>` : ""}
        <p class="hint">模板工具连接：${escapeHTML(session.state.template_mcp_status || "尚未检查")}</p>
        <p class="hint">记忆联动：${escapeHTML(session.state.memory_status || "首次行动时检查")}</p>
      </div>
    </details>
  `;
}

const groups = [
  ["陪伴对象与运行",["enabled","targets","platform_id","companion_integration","general_execution_contract","task_completion_message","natural_task_closing","check_comfyui_each_round","comfyui_url","template_mcp_server"]],
  ["每日预算 · 每个对象独立计算",["ignore_budget_while_active","daily_check_limit","daily_round_limit","daily_tool_limit","daily_message_limit"]],
  ["节奏与自主性",["manual_task_ttl_minutes","adaptive_frequency","frequency_min_minutes","frequency_max_minutes","interval_minutes","jitter_minutes","continuation_window_minutes","max_rounds","chat_planning","tool_event_wakeup","extra_prompt"]],
  ["高级 · 工具与上下文",["max_tool_calls","max_agent_steps","turn_timeout_seconds","tool_timeout_seconds","recent_chat_grace_seconds","history_messages","history_chars","memory_timeout_seconds","allowed_tools"],true],
  ["条件查询与作品目录",["condition_query_tools","condition_file_roots","work_library_roots"]]
];
const names = {
  enabled: "持续运行", targets: "对象 QQ 号", platform_id: "QQ 平台实例",
  companion_integration: "适配陪伴插件", general_execution_contract: "执行契约",
  task_completion_message: "完成时交代一句", natural_task_closing: "允许自然收尾",
  check_comfyui_each_round: "每轮检查 ComfyUI", comfyui_url: "ComfyUI 地址",
  template_mcp_server: "模板 MCP 服务", manual_task_ttl_minutes: "手动任务有效期 / 分钟",
  adaptive_frequency: "自主调整间隔", frequency_min_minutes: "最短间隔 / 分钟",
  frequency_max_minutes: "最长间隔 / 分钟", interval_minutes: "基础检查间隔 / 分钟",
  jitter_minutes: "随机浮动 / 分钟", continuation_window_minutes: "短时续行窗口 / 分钟",
  max_rounds: "每个窗口最多回合", chat_planning: "从日常聊天整理计划",
  tool_event_wakeup: "工具返回触发事件", extra_prompt: "额外行为偏好",
  allowed_tools: "限定可用工具", ignore_budget_while_active: "窗口内忽略每日预算",
  daily_check_limit: "每日判断上限", daily_round_limit: "每日执行回合上限",
  daily_tool_limit: "每日工具调用上限", daily_message_limit: "每日发送尝试上限",
  max_tool_calls: "单回合工具上限", max_agent_steps: "单回合模型步骤上限",
  turn_timeout_seconds: "回合超时 / 秒", tool_timeout_seconds: "工具超时 / 秒",
  recent_chat_grace_seconds: "新消息保护 / 秒", history_messages: "上下文消息条数",
  history_chars: "上下文总字符数", memory_timeout_seconds: "记忆读取超时 / 秒",
  condition_query_tools: "只读查询工具", condition_file_roots: "允许检查的目录",
  work_library_roots: "补充作品目录",
};
names.companion_integration="可选适配我会永远陪着你";

function fieldHTML(key) {
  const spec = data.schema[key];
  const value = data.config[key];
  const title = names[key] || spec.description.split(/[（(]/)[0];
  const hint = spec.description === title ? "" : `<span class="hint">${escapeHTML(spec.description)}</span>`;
  if (spec.type === "bool") {
    return `<label class="check wide"><input type="checkbox" data-config="${key}" ${value ? "checked" : ""}><span>${escapeHTML(title)}</span>${hint}</label>`;
  }
  let input;
  if (key === "platform_id") {
    input = `<select data-config="${key}"><option value="">自动识别唯一 OneBot 平台</option>${data.platforms.map(item => `<option value="${escapeHTML(item.id)}" ${value === item.id ? "selected" : ""}>${escapeHTML(item.id)} · ${escapeHTML(item.name)}</option>`).join("")}${value && !data.platforms.some(item => item.id === value) ? `<option selected value="${escapeHTML(value)}">${escapeHTML(value)}（当前未加载）</option>` : ""}</select>`;
  } else if (spec.type === "list" || spec.type === "text") {
    input = `<textarea data-config="${key}" ${spec.type === "text" ? 'maxlength="10000"' : ""} placeholder="${key === "targets" ? "每行一个 QQ 号，例如：123456789" : ""}">${escapeHTML(Array.isArray(value) ? value.join("\n") : value)}</textarea>`;
  } else {
    input = `<input data-config="${key}" type="${spec.type === "int" ? "number" : "text"}" ${spec.type === "int" ? 'min="0" max="100000"' : ""} value="${escapeHTML(value)}">`;
  }
  return `<label class="field ${spec.type === "text" || key === "targets" ? "wide" : ""}"><span>${escapeHTML(title)}</span>${input}${hint}</label>`;
}

function renderSettings() {
  for (const key of ["proactive_segmented_reply", "proactive_segment_chars", "proactive_segment_interval_ms"]) {
    if (!groups[0][1].includes(key)) groups[0][1].push(key);
  }
  if (!groups[0][1].includes("livingmemory_integration")) groups[0][1].push("livingmemory_integration");
  names.livingmemory_integration = "适配 LivingMemory";
  $("config-fields").innerHTML = groups.map(([title, keys, collapsed]) => {
    const body = `<div class="field-grid">${keys.filter(key => data.schema[key]).map(fieldHTML).join("")}</div>`;
    if (collapsed) {
      return `<details class="panel"><summary><span>${title}</span><span class="hint">${keys.length} 项</span></summary><div class="panel-body">${body}</div></details>`;
    }
    return `<section class="panel"><div class="panel-head"><h2>${title}</h2></div><div class="panel-body">${body}</div></section>`;
  }).join("");
  loadedForm = true;
  dirty = false;
}

function renderView(name) {
  if (!data) return;
  const list = sessions();
  if (name === "overview") {
    renderMetrics(list);
    renderSessions(list);
    renderUsage();
  } else if (name === "plans") {
    const session = selectedSession("plans-target");
    if (!session) { renderPlans(); return; }
    const signature = [session.umo, session.state.long_tasks, session.state.future_task, session.state.manual_plan_expires_at, session.state.plan_status, session.state.pending_message,
      session.state.projects, session.state.rhythm_minutes, session.state.rhythm_reason, session.state.rhythm_updated_at,
      session.state.rhythm_scope, session.state.rhythm_expires_at, session.state.rhythm_ignored_reason, session.state.rhythm_history,
      session.state.preferences, session.state.conditions, session.state.condition_observations, session.state.appointments,
      session.state.cancelled_tasks, session.state.execution_checkpoints, session.state.execution_checkpoint,
      session.state.rounds, session.state.next_due, session.state.window_end, session.state.companion_status, session.state.next_task, session.state.continuation_status,
      data.config.max_rounds, data.config.adaptive_frequency, data.config.interval_minutes];
    if (!plansHasUnsaved() && !$("plans-content").contains(document.activeElement) && changed("plans", signature)) renderPlans();
  } else if (name === "works") {
    const session = selectedSession("works-target");
    if (!session) { renderWorks(); return; }
    const signature = [session.umo, session.state.work_library, session.state.library_archived, session.state.saved_works, session.state.outbox, session.state.companion_library_status, session.state.companion_status];
    if (changed("works", signature)) renderWorks();
  } else if (name === "activity") {
    renderActivity();
  } else if (name === "settings") {
    if (!loadedForm) renderSettings();
  }
}

function render() {
  const list = sessions();
  renderMetrics(list);
  renderSessions(list);
  fillTargets(list);
  renderView(activeTab);
  renderUsage();
  if (!loadedForm) renderSettings();
}

async function perform(body) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(button => { button.disabled = true; });
  try {
    const result = await api("POST", "action", body);
    if (body.action === "companion_sync") previewCache.clear();
    if (["plan", "conditions"].includes(body.action)) planDrafts.delete(body.action);
    if (body.action === "cancel_plan") { planDrafts.delete("plan"); if ($("plan-editor")) $("plan-editor").value = ""; }
    planDirty = planDrafts.size > 0;
    rendered.plans = "";
    rendered.activity = "";
    rendered.works = "";
    rendered.sessions = "";
    rendered.usage = "";
    notice(
      result.decision ? "预览：" + JSON.stringify(result.decision)
        : body.action === "companion_sync" ? result.message
        : body.action === "work_rescan" ? "已检查并补录 " + result.indexed + " 份文本文件。"
        : body.action === "now" ? "已安排自主判断，她可以选择行动或沉默。"
        : body.action === "cancel_plan" ? "临时任务已取消，不再恢复。"
        : "已保存",
      "ok"
    );
  } catch (error) {
    notice("操作失败：" + error.message, "bad");
  } finally {
    busy = false;
    document.querySelectorAll("button").forEach(button => { button.disabled = false; });
    await refresh();
  }
}

document.querySelectorAll(".tab").forEach(button => {
  button.onclick = () => tab(button.dataset.tab);
});
document.querySelector(".tabs").addEventListener("keydown", event => {
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
  event.preventDefault();
  const tabs = [...document.querySelectorAll(".tab")];
  const index = tabs.indexOf(document.activeElement);
  if (index < 0) return;
  const next = tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
  next.focus();
  tab(next.dataset.tab);
});
$("refresh").onclick = refresh;
$("notice-close").onclick = () => { $("notice").hidden = true; };
$("open-settings").onclick = () => tab("settings");
$("overview-target").onchange = () => { rendered.usage = ""; renderUsage(); };
$("activity-target").onchange = renderActivity;
$("diagnose").onclick = async () => {
  const button = $("diagnose");
  button.disabled = true;
  try {
    const report = await api("GET", "diagnostics");
    $("diagnostics-result").innerHTML = report.targets.map(row => `<div class="stack"><p class="mono">${escapeHTML(row.target)}</p>${row.checks.map(check => `<div class="row wrap"><span class="pill ${{ ok: "ok", waiting: "warn", error: "bad" }[check.status] || ""}">${{ ok: "正常", waiting: "待确认", error: "异常" }[check.status] || escapeHTML(check.status)}</span><span>${escapeHTML(check.name)}</span><span class="hint">${escapeHTML(check.detail)}</span></div>`).join("")}</div>`).join("") || '<p class="hint">请先在设置添加对象。</p>';
    notice(report.note, "ok");
  } catch (error) {
    notice("自检失败：" + error.message, "bad");
  } finally {
    button.disabled = false;
  }
};

document.addEventListener("click", async event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.openPlan) {
    if (plansHasUnsaved() && !await pageConfirm("当前有未保存的编辑，切换会丢弃，是否继续？")) return;
    tab("plans");
    $("plans-target").value = button.dataset.openPlan;
    $("plans-target").dataset.previous = button.dataset.openPlan;
    renderPlans();
    return;
  }
  if (button.dataset.action) perform({ umo: button.dataset.umo, action: button.dataset.action });
});

$("config-form").addEventListener("input", () => { dirty = true; });
$("config-form").onsubmit = async event => {
  event.preventDefault();
  if (busy) return;
  const config = {};
  document.querySelectorAll("[data-config]").forEach(element => {
    const key = element.dataset.config, type = data.schema[key].type;
    config[key] = type === "bool" ? element.checked
      : type === "int" ? Number(element.value)
      : type === "list" ? element.value.split(/[\n,，]+/).map(value => value.trim()).filter(Boolean)
      : element.value;
  });
  busy = true;
  $("save-settings").disabled = true;
  try {
    await api("POST", "config", config);
    dirty = false;
    loadedForm = false;
    notice("设置已保存，调度会自动采用新配置。", "ok");
  } catch (error) {
    notice("保存失败：" + error.message, "bad");
  } finally {
    busy = false;
    $("save-settings").disabled = false;
    await refresh();
  }
};

window.addEventListener("beforeunload", event => {
  if (dirty || plansHasUnsaved()) { event.preventDefault(); event.returnValue = ""; }
});

(async () => {
  try {
    bridge = window.AstrBotPluginPage;
    if (!bridge) throw new Error("请从 AstrBot 左侧栏打开本页面");
    await bridge.ready();
    await refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  } catch (error) {
    notice(error.message, "bad");
  }
})();
