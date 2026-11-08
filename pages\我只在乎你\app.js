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

const metricIcons = {
  // 单色线性图标：只用 currentColor 描边，不填色、不带 emoji
  people: '<circle cx="9" cy="6.6" r="2.6"/><path d="M3.8 15c.5-2.6 2.6-4 5.2-4s4.7 1.4 5.2 4"/>',
  pulse: '<path d="M2.2 9h3l1.8-4 2.6 8 1.7-4h3.5"/>',
  clock: '<circle cx="9" cy="9" r="6.2"/><path d="M9 5.6V9l2.4 1.6"/>',
  folder: '<path d="M2.4 6.2a1.4 1.4 0 0 1 1.4-1.4h3l1.4 1.8h5a1.4 1.4 0 0 1 1.4 1.4v5.6a1.4 1.4 0 0 1-1.4 1.4H3.8a1.4 1.4 0 0 1-1.4-1.4z"/>',
  outbox: '<path d="M3 11.2V14a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-2.8"/><path d="M9 11V3.4M6.2 6.2 9 3.4l2.8 2.8"/>',
};

function metricIcon(name) {
  return `<svg class="icon" viewBox="0 0 18 18" aria-hidden="true" focusable="false">${metricIcons[name]}</svg>`;
}

function renderMetrics(list) {
  const projects = list.flatMap(item => item.state.projects || []);
  const pending = list.reduce((count, item) => count + (item.state.outbox || []).filter(entry => entry.status === "pending").length, 0);
  const active = list.filter(item => item.running).length;
  const speakList = list.filter(item => item.state.next_speak_at);
  const speakAt = speakList.map(item => item.state.next_speak_at).sort((a, b) => a - b)[0] || 0;
  const cards = [
    ["people", "accent", "陪伴对象", list.length],
    ["pulse", active ? "ok" : "", "正在行动", active],
    ["clock", speakList.length ? "accent" : "", speakAt ? `约好开口 · ${soonText(speakAt)}` : "约好开口", speakList.length],
    ["folder", projects.filter(item => item.status === "active").length ? "accent" : "", "持续项目", projects.filter(item => item.status === "active").length],
    ["outbox", pending ? "warn" : "", "待分享", pending],
  ];
  $("metrics").innerHTML = cards.map(([icon, tone, label, value]) =>
    `<div class="stat" data-tone="${tone}"><span class="stat-icon">${metricIcon(icon)}</span><div><b>${value}</b><span>${label}</span></div></div>`
  ).join("");
}

function renderSessions(list) {
  if (!changed("sessions", [data.enabled, list.map(item => [item.umo, item.running, item.state.next_due, item.state.next_speak_at, item.state.next_speak_reason, item.state.speak_wish, item.state.speak_wish_at, item.state.speak_wish_kept, item.state.future_task, item.state.last_error, item.state.pause_until, item.state.last_turn?.status])])) return;
  const unresolved = (data.config.targets || []).length && !list.length;
  $("sessions").innerHTML = list.map(session => {
    const paused = session.state.pause_until > Date.now() / 1000;
    const status = session.running ? ["行动中", "accent"] : data.enabled && !paused ? ["等待心跳", "ok"] : ["已暂停", ""];
    const next = session.state.next_due ? timeShort(session.state.next_due) : "尚未安排";
    const nextState = soonText(session.state.next_due);
    const speakAt = session.state.next_speak_at;
    return `<article class="item">
      <div class="grow">
        <div class="item-title">${escapeHTML(displayName(session))}<span class="pill ${status[1]}">${status[0]}</span></div>
        <div class="item-sub mono">${escapeHTML(session.umo)}</div>
        <div class="item-sub">下次判断 ${next}${nextState ? `（${nextState}）` : ""} · ${escapeHTML(session.state.future_task || "没有固定计划，让想法自然发生")}</div>
        ${speakAt ? `<div class="item-sub">她自己约定的开口时间 ${timeShort(speakAt)} · ${soonText(speakAt)}${session.state.next_speak_reason ? " · " + escapeHTML(clipText(session.state.next_speak_reason, 32)) : ""}</div>` : ""}
        ${session.state.speak_wish ? `<div class="item-sub">心里存着想说的话（${agoText(session.state.speak_wish_at)}记下${session.state.speak_wish_kept ? `，已搁置 ${session.state.speak_wish_kept} 轮` : ""}）· ${escapeHTML(clipText(session.state.speak_wish, 40))}</div>` : ""}
        ${session.state.last_error ? `<div class="item-sub error-text">${escapeHTML(session.state.last_error)}</div>` : ""}
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
  const pool = session.pool || {};
  const nightMode = pool.pool === "night";
  const nightStart = String(config.night_window_start_hour ?? 23).padStart(2, "0");
  const nightEnd = String(config.night_window_end_hour ?? 8).padStart(2, "0");
  const nightPercent = config.night_budget_percent ?? 0;
  const poolLine = nightMode
    ? `夜间 ${nightStart}:00–${nightEnd}:00 · 判断 ${pool.checks || 0}/${pool.limit_checks || 0} · 执行 ${pool.rounds || 0}/${pool.limit_rounds || 0} · 工具 ${pool.tools || 0}/${pool.limit_tools || 0} · 发送 ${pool.messages || 0}/${pool.limit_messages || 0}`
    : `白天 · 到 ${nightStart}:00 换成夜间额度（占白天 ${nightPercent}%）`;
  const morning = session.state.morning_report || {};
  const morningLine = !config.morning_report_enabled
    ? "未开启"
    : morning.day
      ? `${morning.day} ${timeShort(morning.at)} 讲过一段（${String(morning.text || "").length} 字）`
      : `每天 ${String(config.morning_report_hour ?? 8).padStart(2, "0")}:00 之后讲一次，今天还没讲`;
  const segmentLine = config.proactive_segmented_reply
    ? `分段发送 · 每段约 ${config.proactive_segment_chars ?? 0} 字 · 间隔 ${config.proactive_segment_interval_ms ?? 0} 毫秒`
    : "一次发一整条";
  const alerts = (session.state.security_alerts || []).filter(text => String(text || "").trim());
  const signature = [session.umo, usage, pool, morning, alerts, session.state.pause_until, session.state.pause_mode, session.state.available_tool_names, session.state.available_skill_names, config.daily_check_limit, config.daily_round_limit, config.daily_tool_limit, config.daily_message_limit, config.split_night_budget, config.night_window_start_hour, config.night_window_end_hour, config.night_budget_percent, config.morning_report_enabled, config.morning_report_hour, config.proactive_segmented_reply, config.proactive_segment_chars, config.proactive_segment_interval_ms];
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
    <div class="kv">
      <div class="kv-item"><span class="hint">额度池</span><b>${poolLine}</b></div>
      <div class="kv-item"><span class="hint">晨间记录</span><b>${morningLine}</b></div>
      <div class="kv-item"><span class="hint">发送方式</span><b>${segmentLine}</b></div>
    </div>
    ${morning.text ? `<details class="work"><summary><span class="work-kind">晨间</span><span class="work-title">${escapeHTML(morning.day || "")} 早上讲过的那段话</span><span class="work-meta">${String(morning.text).length} 字</span></summary><div class="work-detail"><pre class="text-preview">${escapeHTML(morning.text)}</pre></div></details>` : ""}
    ${alerts.length ? `<div class="callout"><p class="hint">读到可疑内容，原文只留了摘要：</p>${alerts.slice(-3).map(text => `<p class="hint">${escapeHTML(clipText(String(text), 90))}</p>`).join("")}</div>` : ""}
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
        ${journal.map(item => `<div class="item"><div class="grow"><div class="item-title"><span class="pill ${/^(speak|wish)_/.test(String(item.action || "")) ? "accent" : ""}">${escapeHTML(labels[item.action] || item.action)}</span><span class="hint">${escapeHTML(item.time)}</span></div><pre class="text-preview" style="margin-top:8px;max-height:220px">${escapeHTML(item.summary)}</pre></div></div>`).join("") || '<p class="empty">还没有行动记录。</p>'}
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
  ["对象与运行",["enabled","targets","platform_id"],"谁在被陪伴、总开关在哪。填好 QQ 号保存后即持续运行。"],
  ["陪伴与人格",["companion_integration","livingmemory_integration","expose_plugin_hooks","proactive_segmented_reply","proactive_segment_chars","proactive_segment_interval_ms","general_execution_contract","task_completion_message","natural_task_closing","extra_prompt"],"和别的插件怎么配合、一句话要不要拆成几条发、做完事要不要交代一句、额外的语气要求。"],
  ["节奏与自主性",["manual_task_ttl_minutes","adaptive_frequency","frequency_min_minutes","frequency_max_minutes","interval_minutes","jitter_minutes","continuation_window_minutes","max_rounds","tool_event_wakeup"],"多久判断一次、能不能自己调频率、一次主动能连续做多少事。"],
  ["自己约定的开口",["self_scheduled_speak","next_speak_min_minutes","next_speak_max_minutes","speak_wish_ttl_hours"],"她自己约下次开口的时间（到点专门唤醒一次），以及心里存着的话保留多久。"],
  ["夜间与晨间",["split_night_budget","night_window_start_hour","night_window_end_hour","night_budget_percent","morning_report_enabled","morning_report_hour","morning_report_min_gap_hours","morning_report_max_chars","morning_report_timeout_seconds"],"夜里单独算一份额度，免得半夜把白天的预算花光；早上醒来先用她自己的话讲一遍昨晚发生了什么。"],
  ["记忆与整理",["chat_planning","preference_stale_days","activity_half_life_days","action_digest_chars","external_text_chars"],"从日常聊天整理计划与关于你的记录；读进来的外部文本最多塞多少字。"],
  ["每日预算 · 每个对象独立",["ignore_budget_while_active","daily_check_limit","daily_round_limit","daily_tool_limit","daily_message_limit"],"保护阈值：判断、回合、工具、发送分开计数，用完就停，不跨天补。夜里的那份见「夜间与晨间」。"],
  ["工具、超时与上下文",["max_tool_calls","max_agent_steps","turn_timeout_seconds","tool_timeout_seconds","recent_chat_grace_seconds","history_messages","history_chars","memory_timeout_seconds","allowed_tools","check_comfyui_each_round","comfyui_url","template_mcp_server"],"单轮工具上限、超时与上下文窗口。没有特殊需要不必改。",true],
  ["文件与命令",["file_tools_enabled","file_tools_root","file_tools_extra_read_roots","file_tools_snapshot_before_write","allow_shell_commands","shell_command_timeout"],"让她读你本机的文件、必要时执行命令。改文件前先留快照；命令默认关闭，想开再开。",true],
  ["条件查询与作品目录",["condition_query_tools","condition_file_roots","work_library_roots"],"允许她只读检查的服务与目录、补充的作品路径。"],
  ["高级 · 发送守卫",["state_claim_guard","tool_work_message","delivery_tools","action_memory_write","pending_message_ttl_hours"],"发送前自检、工具回执与后台轮暂存策略。一般不用改。",true],
  ["高级 · DSH 集成",["dsh_enabled","dsh_command","dsh_home","dsh_working_dir","dsh_timeout","dsh_snapshot_before_task","dsh_snapshot_keep","dsh_max_result_chars","dsh_max_concurrency","dsh_api_key","dsh_base_url"],"接入本机 DSH/DeepSeek 工具链时才需要；不接就保持关闭。",true],
];
const names = {
  enabled: "持续运行", targets: "对象 QQ 号", platform_id: "QQ 平台实例",
  companion_integration: "适配陪伴插件", livingmemory_integration: "适配 LivingMemory",
  expose_plugin_hooks: "对外暴露插件钩子",
  proactive_segmented_reply: "她的消息分段发送",
  proactive_segment_chars: "每段最多 / 字",
  proactive_segment_interval_ms: "两段之间间隔 / 毫秒",
  general_execution_contract: "执行契约",
  task_completion_message: "完成时交代一句", natural_task_closing: "允许自然收尾",
  check_comfyui_each_round: "每轮检查 ComfyUI", comfyui_url: "ComfyUI 地址",
  template_mcp_server: "模板 MCP 服务", manual_task_ttl_minutes: "手动任务有效期 / 分钟",
  adaptive_frequency: "自主调整间隔", frequency_min_minutes: "最短间隔 / 分钟",
  frequency_max_minutes: "最长间隔 / 分钟", interval_minutes: "基础检查间隔 / 分钟",
  self_scheduled_speak: "自己约定开口时间", next_speak_min_minutes: "开口约定最短 / 分钟",
  next_speak_max_minutes: "开口约定最长 / 分钟",
  speak_wish_ttl_hours: "想说的话保留 / 小时",
  split_night_budget: "夜里单独算一份额度",
  night_window_start_hour: "夜里从几点算起",
  night_window_end_hour: "夜里到几点结束",
  night_budget_percent: "夜间额度占白天 / 百分比",
  morning_report_enabled: "早上先讲一遍昨晚",
  morning_report_hour: "几点算早上",
  morning_report_min_gap_hours: "两次至少隔 / 小时",
  morning_report_max_chars: "这段最多 / 字",
  morning_report_timeout_seconds: "生成超时 / 秒",
  preference_stale_days: "偏好多久算过时 / 天",
  activity_half_life_days: "活跃统计多久淡化一半 / 天",
  action_digest_chars: "做事摘要最多 / 字",
  external_text_chars: "外部文本最多 / 字",
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
  file_tools_enabled: "允许她读写文件",
  file_tools_root: "文件工具根目录",
  file_tools_extra_read_roots: "额外可读目录",
  file_tools_snapshot_before_write: "改文件前先留快照",
  allow_shell_commands: "允许执行命令",
  shell_command_timeout: "命令超时 / 秒",
  dsh_snapshot_before_task: "接任务前留快照",
  dsh_snapshot_keep: "保留几份快照",
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
  $("config-fields").innerHTML = groups.map(([title, keys, lead, collapsed]) => {
    const body = `<div class="field-grid">${keys.filter(key => data.schema[key]).map(fieldHTML).join("")}</div>`;
    const sub = lead ? `<p class="hint">${escapeHTML(lead)}</p>` : "";
    if (collapsed) {
      return `<details class="panel"><summary><span>${title}</span><span class="hint">${keys.length} 项 · 默认收起</span></summary><div class="panel-body stack">${sub}${body}</div></details>`;
    }
    return `<section class="panel"><div class="panel-head"><div><h2>${title}</h2>${sub}</div></div>${body}</section>`;
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
      session.state.notes, session.state.activity, session.state.conditions, session.state.condition_observations, session.state.appointments,
      session.state.cancelled_tasks, session.state.execution_checkpoints, session.state.execution_checkpoint,
      session.state.rounds, session.state.next_due, session.state.next_speak_at, session.state.next_speak_reason, session.state.speak_ignored_reason, session.state.speak_wish, session.state.speak_wish_at, session.state.speak_wish_kept, session.state.window_end, session.state.companion_status, session.state.next_task, session.state.continuation_status,
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
