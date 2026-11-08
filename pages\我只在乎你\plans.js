"use strict";
let planDirty = false;
const planDrafts = new Set();

function plansHasUnsaved() { return planDirty; }

function plansSession() {
  return selectedSession("plans-target");
}

// 活跃时间：与后端同一套口径（按小时、工作日与周末分开、样本随时间衰减）。
function describeActivity(entry, settings) {
  const hoursOf = (raw) => {
    const bins = new Array(24).fill(0);
    if (Array.isArray(raw)) {
      for (let hour = 0; hour < 24; hour += 1) {
        const value = Number(raw[hour]);
        bins[hour] = Number.isFinite(value) && value > 0 ? value : 0;
      }
    }
    return bins;
  };
  const source = entry && typeof entry === "object" ? entry : {};
  const weekday = hoursOf(source.weekday);
  const weekend = hoursOf(source.weekend);
  const stamp = Number(source.updated_at || 0);
  const half = Number((settings || {}).activity_half_life_days || 30);
  const elapsed = stamp ? (Date.now() / 1000 - stamp) / 86400 : 0;
  const factor = elapsed > 0 ? Math.pow(0.5, elapsed / Math.max(half, 0.5)) : 1;
  const hours = weekday.map((value, hour) => (value + weekend[hour]) * factor);
  const samples = Number(source.samples || 0);
  const total = hours.reduce((sum, value) => sum + value, 0);
  const note = String(source.note || "");
  const label = samples ? `${samples} 次发言` : "还没有记录";
  if (!(samples >= 6 && total > 0)) {
    const summary = samples
      ? `只有 ${samples} 次发言，还看不出规律。等样本多一点再判断，她不会凭这个猜你在不在。`
      : "还没有足够记录。她不会凭空推测你的作息，也不会拿它当不开口的理由。";
    return { hours, samples, enough: false, label, summary, note };
  }
  const runs = (flags) => {
    const list = flags.reduce((acc, flag, hour) => (flag ? acc.concat(hour) : acc), []);
    if (!list.length) return [];
    if (list.length === 24) return [[0, 23]];
    const groups = [];
    let start = list[0];
    let previous = list[0];
    list.slice(1).forEach((hour) => {
      if (hour === previous + 1) {
        previous = hour;
        return;
      }
      groups.push([start, previous]);
      start = hour;
      previous = hour;
    });
    groups.push([start, previous]);
    if (groups.length > 1 && groups[0][0] === 0 && groups[groups.length - 1][1] === 23) {
      const first = groups.shift();
      const last = groups.pop();
      groups.push([last[0], first[1]]);
    }
    return groups;
  };
  const text = (start, end) => `${String(start).padStart(2, "0")}:00–${String((end + 1) % 24).padStart(2, "0")}:00`;
  const runLength = ([start, end]) => ((end - start + 24) % 24) + 1;
  const runWeight = ([start, end], values) => {
    const span = end >= start ? values.slice(start, end + 1) : values.slice(start).concat(values.slice(0, end + 1));
    return span.reduce((sum, value) => sum + value, 0);
  };
  // 只留最值得说的几段，再按时间顺序摆回去
  const rank = (list, values, limit, byLength) => list.slice().sort((a, b) => {
    const left = byLength ? runLength(a) : runWeight(a, values);
    const right = byLength ? runLength(b) : runWeight(b, values);
    return right - left;
  }).slice(0, limit).sort((a, b) => a[0] - b[0]);
  const average = total / 24;
  const active = rank(runs(hours.map((value) => value >= average * 1.6)), hours, 4, false).map(([start, end]) => text(start, end));
  const quiet = rank(runs(hours.map((value) => value <= average * 0.25)), hours, 2, true).map(([start, end]) => text(start, end));
  const parts = [`常在线 ${active.join("、") || "没有明显高峰"}`];
  if (quiet.length) parts.push(`基本不在 ${quiet.join("、")}`);
  return { hours, samples, enough: true, label, summary: `${parts.join("；")}。`, note };
}


function renderPlans() {
  if (activePageDialogs) return;
  planDirty = false;
  planDrafts.clear();
  const s = plansSession();
  const host = $("plans-content");
  if (!s) {
    host.innerHTML = '<div class="empty">先添加一个陪伴对象，任务与项目会保存在这里。</div>';
    return;
  }
  const settings = data.config;
  const projects = s.state.projects || [];
  const notes = Object.entries(s.state.notes || {}).sort((a, b) => {
    const rank = ([, note]) => (note.kind === "preference" ? 2 : note.confirmed ? 1 : 0);
    return rank(b) - rank(a);
  });
  const pendingCount = notes.filter(([, note]) => note.kind !== "preference" && !note.confirmed).length;
  const activityInfo = describeActivity(s.state.activity, settings);
  const activityTop = Math.max(...activityInfo.hours, 0.0001);
  const activityBars = activityInfo.hours.map((value, hour) => {
    const tone = !activityInfo.enough ? "hour-off" : value >= activityTop * 0.5 ? "hour-on" : "";
    return `<span class="hour ${tone}" data-hour="${hour}" title="${String(hour).padStart(2, "0")}:00" style="height:${Math.max(Math.round((value / activityTop) * 46), 2)}px"></span>`;
  }).join("");
  const appointments = s.state.appointments || [];
  const checkpoints = Object.values(s.state.execution_checkpoints || {});
  if (s.state.execution_checkpoint && !checkpoints.length) checkpoints.push(s.state.execution_checkpoint);
  const cancelled = s.state.cancelled_tasks || [];
  const pause = s.state.pause_until > Date.now() / 1000;

  host.innerHTML = `
    <div class="group-head"><h3>她在做什么</h3><p class="hint">长期任务、下次主动想做的事、以及长期目标。</p></div>
    <section class="panel"><div class="panel-head"><div><h2>长期任务</h2><p class="hint">独立执行上下文，后台推进；中断后等待核对，不自动重放。</p></div><button id="add-long-task" type="button">添加长期任务</button></div>
    <div class="panel-body stack">${(s.state.long_tasks || []).map(job => `<details class="work"><summary><span class="work-title">${escapeHTML(job.title)}</span><span class="pill">${escapeHTML(({queued:"排队中",running:"执行中",waiting:"等待核对",paused:"已暂停",failed:"失败",completed:"已完成",cancelled:"已取消"})[job.status] || job.status)}</span></summary><div class="work-detail stack"><p>${escapeHTML(job.goal)}</p><p class="hint">阶段 ${job.steps} · 尝试 ${job.attempts} · ${escapeHTML(job.workspace || "工作区尚未创建")}</p><pre class="text-preview">${escapeHTML(job.result || job.error || "尚无结果")}</pre><details><summary>阶段记录</summary>${(job.log || []).map(entry => `<p>${timeText(entry.at)} · ${escapeHTML(entry.summary)}</p>`).join("")}</details><div class="actions">${(["queued","running"].includes(job.status) ? ["pause","cancel"] : ["paused","waiting","failed"].includes(job.status) ? ["resume","cancel"] : ["delete"]).map(op => `<button data-long-task="${escapeHTML(job.id)}" data-operation="${op}" type="button">${({pause:"暂停",resume:"核对后恢复",cancel:"取消",delete:"移除记录"})[op]}</button>`).join("")}</div></div></details>`).join("") || '<p class="hint">尚无长期任务。可为资料整理、创作或排查任务设定明确目标和完成标准。</p>'}</div></section>
    <section class="panel">
      <div class="panel-head">
        <div><h2>下一次想做的事</h2><p class="hint">她能结合新对话重新判断，也可以由你直接安排。</p></div>
        <span class="pill">${s.state.future_task ? "已有计划" : "自然发生"}</span>
      </div>
      <div class="panel-body stack">
        ${s.state.pending_message ? `
        <div class="item callout">
          <div class="grow">
            <div class="item-title">后台轮想说的话<span class="pill warn">待补发</span></div>
            <div class="item-sub">上次后台工作（notify=false）时写下、还没发出来的话。下次正常主动时自动补发${s.state.pending_message.expires_at ? "，过期时间 " + timeText(s.state.pending_message.expires_at) : ""}。</div>
            <pre class="text-preview" style="margin-top:8px">${escapeHTML(s.state.pending_message.content || "")}</pre>
          </div>
          <div class="actions">
            <button id="discard-pending-message" class="btn-small btn-danger" type="button">丢弃</button>
          </div>
        </div>` : ""}
        <textarea id="plan-editor" maxlength="2000" aria-label="未来计划" placeholder="想要她下次主动时做什么？">${escapeHTML(s.state.future_task || "")}</textarea>
        <div class="row wrap">
          <div class="actions">
            <button id="save-plan" class="btn-primary" type="button">保存计划</button>
            <button id="cancel-plan" type="button">取消临时任务</button>
            <button id="preview-plan" type="button">仅预览判断</button>
          </div>
          <span class="hint grow">${s.state.manual_plan_expires_at ? "临时任务有效至 " + timeText(s.state.manual_plan_expires_at) : "临时安排，保存后按有效期执行"}</span>
        </div>
        <p class="hint">${escapeHTML(s.state.plan_status || "尚未手动保存。暂停、预算及工具条件仍然生效。")}</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div><h2>持续项目</h2><p class="hint">长期目标、进度与下一步，最多 5 个。</p></div>
        <button id="add-project" type="button">添加项目</button>
      </div>
      <div class="panel-body stack">
        ${projects.map(project => `
          <details class="work">
            <summary>
              <span class="work-kind">项目</span>
              <span class="work-title">${escapeHTML(project.title)}</span>
              <span class="pill ${project.status === "active" ? "ok" : ""}">${labels[project.status] || escapeHTML(project.status)}</span>
            </summary>
            <div class="work-detail stack">
              ${[["目标", project.goal], ["实际进展", project.progress], ["下一步", project.next_step], ["作品位置", project.artifact]]
                .filter(([, value]) => value)
                .map(([name, value]) => `<div><span class="hint">${name}</span><p>${escapeHTML(value)}</p></div>`).join("")}
              <p class="hint">最后推进 ${timeText(project.last_progress_at)} · 连续失败 ${project.failure_count || 0}${project.last_error ? " · " + escapeHTML(project.last_error) : ""}</p>
              <p class="hint">最近尝试 ${timeText(project.last_attempt_at)}${project.retry_after > Date.now() / 1000 ? " · 重试冷却至 " + timeText(project.retry_after) : ""}</p>
              ${project.compilation_status ? `<p class="hint">${escapeHTML(project.compilation_status)}</p>` : ""}
              <div class="row wrap">
                <button data-project-edit="${escapeHTML(project.id)}" type="button">编辑项目</button>
                ${project.status === "active" ? `<button data-project-run="${escapeHTML(project.id)}" type="button">安排推进</button>` : ""}
                <label class="inline-field">状态
                  <select data-project="${escapeHTML(project.id)}" aria-label="${escapeHTML(project.title)}的状态">
                    ${["active", "paused", "completed", "cancelled"].map(status => `<option value="${status}" ${project.status === status ? "selected" : ""}>${labels[status]}</option>`).join("")}
                  </select>
                </label>
                <span class="grow"></span>
                <button class="btn-danger" data-project-delete="${escapeHTML(project.id)}" type="button">删除项目</button>
              </div>
              ${(project.history || []).length ? `<details><summary>历史版本（${project.history.length}）</summary>${[...project.history].reverse().map(version => `<details><summary>${timeText(version.at)} · ${escapeHTML(version.title)}</summary><pre class="text-preview">${escapeHTML(JSON.stringify(version, null, 2))}</pre></details>`).join("")}</details>` : ""}
            </div>
          </details>`).join("") || '<p class="empty">还没有持续项目。聊聊故事、画作或共同兴趣，她会从中产生项目。</p>'}
      </div>
    </section>

    <div class="group-head"><h3>她怎么安排自己</h3><p class="hint">基础节奏、自己约定的开口时间、心里存着的话、关于你的记录与待办条件。</p></div>
    <section class="panel">
      <div class="panel-head">
        <div><h2>她的节奏</h2><p class="hint">${settings.adaptive_frequency ? "自主调整已开启" : "固定配置节奏"} · ${settings.self_scheduled_speak === false ? "自己约定开口已关闭" : "可自己约定开口"}</p></div>
        <button id="rhythm-reset" type="button">恢复默认间隔</button>
      </div>
      <div class="panel-body stack">
        <div class="stats" style="margin:0">
          <div class="stat"><b>${escapeHTML(settings.adaptive_frequency ? (s.state.rhythm_minutes ?? settings.interval_minutes) : settings.interval_minutes)}</b><span>当前基础间隔 · 分钟</span></div>
          <div class="stat"><b>${escapeHTML(s.state.rounds || 0)}</b><span>本窗口已执行 · 上限 ${escapeHTML(settings.max_rounds)}</span></div>
          <div class="stat"><b>${escapeHTML(s.state.next_speak_at ? soonText(s.state.next_speak_at) : "未约定")}</b><span>她自己约定的开口</span></div>
        </div>
        <p class="hint">${escapeHTML(s.state.rhythm_reason || "尚未自主调整，使用默认节奏")} · 下次判断 ${timeShort(s.state.next_due)}${s.state.next_due ? "（" + soonText(s.state.next_due) + "）" : ""}</p>
        ${s.state.next_speak_at ? `<p class="hint">她自己约定的开口时间 ${timeShort(s.state.next_speak_at)}${s.state.next_speak_reason ? " · " + escapeHTML(clipText(s.state.next_speak_reason, 60)) : ""} · 到点只唤醒她一次，默认开口说话</p>` : ""}
        ${s.state.speak_wish ? `<p class="hint">心里存着想说的话（${agoText(s.state.speak_wish_at)}记下${s.state.speak_wish_kept ? `，已搁置 ${s.state.speak_wish_kept} 轮` : ""}）：${escapeHTML(clipText(s.state.speak_wish, 80))}</p>` : ""}
        <p class="hint">${s.state.next_task && s.state.window_end > Date.now() / 1000 ? "续行：" + escapeHTML(s.state.next_task) + " · 窗口至 " + timeText(s.state.window_end) : escapeHTML(s.state.continuation_status || "暂无短时续行安排")}</p>
        <p class="hint">${s.state.rhythm_scope === "global" ? "依据：用户明确整体频率偏好" : "依据：她的临时自主节奏"} · 有效至 ${s.state.rhythm_expires_at ? timeShort(s.state.rhythm_expires_at) : "默认节奏"}</p>
        ${s.state.rhythm_ignored_reason ? `<p class="hint">最近忽略的调频：${escapeHTML(s.state.rhythm_ignored_reason)}</p>` : ""}
        ${s.state.speak_ignored_reason ? `<p class="hint">最近忽略的开口约定：${escapeHTML(s.state.speak_ignored_reason)}</p>` : ""}
        ${(s.state.rhythm_history || []).length ? `<details class="work"><summary><span class="work-kind">历史</span><span class="work-title">最近节奏变化</span><span class="work-meta">${(s.state.rhythm_history || []).length} 条</span></summary><div class="work-detail"><div class="stack">${[...(s.state.rhythm_history || [])].reverse().map(item => `<p class="hint">${timeText(item.at)} · ${escapeHTML(item.minutes)} 分钟 · ${escapeHTML(item.reason)}</p>`).join("")}</div></div></details>` : ""}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div><h2>关于你</h2><p class="hint">她记下的关于你的事，分三类：<strong>偏好</strong>会约束她开口的时间、话题与语气，<strong>事实</strong>和<strong>爱好</strong>只用来挑话题。不限条数；标着"未确认"的只是她的猜测，她不会当成你说过的话。人会变，每条都能改。</p></div>
        <div class="actions">
          <span class="pill">共 ${notes.length} 条${pendingCount ? ` · ${pendingCount} 条待确认` : ""}</span>
          <button id="add-note" type="button">添加一条</button>
        </div>
      </div>
      <div class="list">
        ${notes.map(([key, note]) => {
          const kindLabel = note.kind === "preference" ? "偏好" : note.kind === "interest" ? "爱好" : "事实";
          const stale = note.kind === "preference" && !note.pinned && note.updated_at && Date.now() / 1000 - note.updated_at > (settings.preference_stale_days || 90) * 86400;
          return `
          <div class="item">
            <div class="grow">
              <div class="item-title">
                <span class="pill ${note.kind === "fact" ? "" : "accent"}">${kindLabel}</span>
                ${escapeHTML(key)}
                ${note.confirmed ? "" : '<span class="pill warn">未确认</span>'}
                ${note.pinned ? '<span class="pill">固定</span>' : ""}
                ${stale ? '<span class="pill warn">较久未更新</span>' : ""}
                ${note.source === "manual" ? '<span class="pill">手动记录</span>' : ""}
              </div>
              <div>${escapeHTML(note.value)}</div>
              <div class="item-sub">${note.evidence === "用户手动添加" ? "手动添加" : "原话：" + escapeHTML(note.evidence || "")} · 更新 ${note.updated_at ? timeText(note.updated_at) : "未知"}${note.confirmed && note.confirmed_at ? " · 确认于 " + timeText(note.confirmed_at) : ""}${note.previous ? " · 之前是：" + escapeHTML(clipText(note.previous, 40)) : ""}</div>
            </div>
            <div class="actions">
              ${note.confirmed ? "" : `<button data-note-confirm="${escapeHTML(key)}" class="btn-small" type="button">确认</button>`}
              <button data-note-pin="${escapeHTML(key)}" data-pinned="${note.pinned ? "1" : "0"}" class="btn-small" type="button">${note.pinned ? "取消固定" : "固定"}</button>
              <button data-note-edit="${escapeHTML(key)}" class="btn-small" type="button">修改</button>
              <button data-note-delete="${escapeHTML(key)}" class="btn-small btn-danger" type="button">删除</button>
            </div>
          </div>`;
        }).join("") || '<p class="empty">还没有记录。她只有在聊天里真的听到关于你的事才会写下来，不会自己编。</p>'}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div><h2>你的作息</h2><p class="hint">她自己留意的：你一般在哪些时段出现。只看时间，不看内容；越早的样本越轻，作息一变她就跟着变。样本不够时她只承认看不出来。</p></div>
        <div class="actions">
          <span class="pill">${activityInfo.label}</span>
          <button id="reset-activity" type="button">清空统计</button>
        </div>
      </div>
      <div class="panel-body stack">
        <div class="hours" role="img" aria-label="按小时的活跃程度">${activityBars}</div>
        <div class="hours-axis">${[0, 6, 12, 18, 23].map(hour => `<span style="left:${(((hour + 0.5) / 24) * 100).toFixed(2)}%">${String(hour).padStart(2, "0")}</span>`).join("")}</div>
        <p class="hint">${activityInfo.summary}</p>
        <label class="field"><span class="hint">你亲口说过的作息（留空就只看统计）</span>
          <input id="activity-note" type="text" maxlength="160" value="${escapeHTML(activityInfo.note)}" placeholder="例如：我通常凌晨一点才睡，七点起" />
        </label>
        <div class="actions"><button id="save-activity-note" class="btn-primary" type="button">保存备注</button></div>
      </div>
    </section>

    <details class="panel">
      <summary><span>待办条件</span><span class="hint">每轮重新检查 · ${(s.state.condition_observations || []).length} 条观察</span></summary>
      <div class="panel-body stack">
        <p class="hint">支持服务、文件与已批准的只读工具查询；没有结果时保持未知，不重复提交。</p>
        ${(s.state.condition_observations || []).map(item => `<p class="hint mono">${escapeHTML(item.label || item.id)} · ${timeText(item.checked_at)} · ${escapeHTML(item.status)}${item.changed ? " · 有变化" : ""} · ${escapeHTML(typeof item.detail === "string" ? item.detail : JSON.stringify(item.detail))}</p>`).join("")}
        <textarea id="conditions-editor" aria-label="条件描述 JSON">${escapeHTML(JSON.stringify(s.state.conditions || [], null, 2))}</textarea>
        <div class="actions"><button id="save-conditions" class="btn-primary" type="button">保存条件 JSON</button></div>
      </div>
    </details>

    ${appointments.length ? `<section class="panel">
      <div class="panel-head"><div><h2>约定</h2><p class="hint">只记录你明确说过的日期与事件。</p></div></div>
      <div class="list">${appointments.map(item => `
        <div class="item">
          <div class="grow">
            <div class="item-title">${escapeHTML(item.title)}<span class="pill">${({ pending: "待发生", resolved: "已了结", cancelled: "已取消" })[item.status] || escapeHTML(item.status)}</span></div>
            <div class="item-sub">${timeText(item.due_at)}${item.result ? " · " + escapeHTML(item.result) : ""}</div>
          </div>
          ${item.status === "pending" ? `<div class="actions">
            <button data-appointment="${escapeHTML(item.id)}" data-status="resolved" class="btn-small" type="button">标记已了结</button>
            <button data-appointment="${escapeHTML(item.id)}" data-status="cancelled" class="btn-small" type="button">取消约定</button>
          </div>` : ""}
        </div>`).join("")}</div>
    </section>` : ""}

    ${cancelled.length || checkpoints.length ? `<div class="group-head"><h3>历史与恢复</h3><p class="hint">中断留下的断点，以及取消过的计划。</p></div>
    <details class="panel">
      <summary><span>断点与已取消</span><span class="hint">${checkpoints.length} 个断点 · ${cancelled.length} 项取消</span></summary>
      <div class="panel-body stack">
        ${checkpoints.map(item => `<p>${escapeHTML(item.task)} · ${item.pending ? "未完成，保留断点" : "已核验完成"} · ${timeText(item.updated_at)}</p>`).join("")}
        ${cancelled.map(item => `<p class="hint">${timeText(item.at)} · 已取消：${escapeHTML(item.text)}（不会自动恢复）</p>`).join("")}
      </div>
    </details>` : ""}
  `;

  $("plan-editor").oninput = () => { planDrafts.add("plan"); planDirty = true; };
  $("add-long-task").onclick = async () => {
    const title = await pagePrompt("长期任务标题"); if (!title?.trim()) return;
    const goal = await pagePrompt("明确目标、范围和完成标准；后台任务不会自动向对方发送消息"); if (!goal?.trim()) return;
    perform({umo:s.umo,action:"long_task",operation:"create",title,goal});
  };
  host.querySelectorAll("[data-long-task]").forEach(button => button.onclick = async () => {
    if (await pageConfirm("确认执行此操作？恢复前请核对已提交的外部任务，避免重复。")) perform({umo:s.umo,action:"long_task",id:button.dataset.longTask,operation:button.dataset.operation});
  });
  $("save-plan").onclick = () => perform({ action: "plan", umo: s.umo, value: $("plan-editor").value });
  $("cancel-plan").onclick = () => perform({ action: "cancel_plan", umo: s.umo });
  $("preview-plan").onclick = () => perform({ umo: s.umo, action: "preview" });
  const discardPending = $("discard-pending-message");
  if (discardPending) discardPending.onclick = async () => {
    if (await pageConfirm("丢弃这条后台轮暂存的话？丢弃后不会再补发。")) perform({ umo: s.umo, action: "pending_message_discard" });
  };
  $("add-project").onclick = async () => {
    const title = await pagePrompt("项目名称");
    if (!title?.trim()) return;
    const goal = await pagePrompt("希望完成的目标");
    if (goal === null) return;
    perform({ umo: s.umo, action: "project_add", title, goal });
  };
  document.querySelectorAll("[data-project]").forEach(element => {
    element.onchange = () => perform({ action: "project", umo: s.umo, id: element.dataset.project, status: element.value });
  });
  document.querySelectorAll("[data-project-delete]").forEach(element => {
    element.onclick = async () => {
      if (await pageConfirm("删除此项目及其进展记录？")) perform({ umo: s.umo, action: "project_delete", id: element.dataset.projectDelete });
    };
  });
  document.querySelectorAll("[data-project-edit]").forEach(element => {
    element.onclick = async () => {
      const project = projects.find(item => item.id === element.dataset.projectEdit);
      const update = { id: project.id };
      for (const [key, label] of [["title", "项目名称"], ["goal", "目标"], ["progress", "实际进展（最多6000字符）"], ["next_step", "下一步"], ["artifact", "作品路径或引用"]]) {
        const value = await pagePrompt(label, project[key] || "");
        if (value === null) return;
        update[key] = value;
      }
      perform({ umo: s.umo, action: "project_edit", ...update });
    };
  });
  document.querySelectorAll("[data-project-run]").forEach(element => {
    element.onclick = async () => {
      const project = projects.find(item => item.id === element.dataset.projectRun);
      if (await pageConfirm("将该项目安排为临时任务？会替换当前临时计划，随后可在概览立即判断。")) {
        perform({ umo: s.umo, action: "plan", project_id: project.id, value: `推进持续项目“${project.title}”，接续已有进展。下一步：${project.next_step || project.goal || "先明确可执行的一步"}`.slice(0, 2000) });
      }
    };
  });
  const submitNote = async (key0) => {
    const note = (s.state.notes || {})[key0] || {};
    const result = await pageForm(key0 ? "修改这条记录" : "添加一条关于你的记录", [
      { label: "主题（例如 作息、宠物、爱好、工作）", value: key0 || "", maxLength: 80, placeholder: "稳定的短标签，同一件事复用同一个" },
      { label: "内容（越接近原话越好）", value: note.value || "", maxLength: 600 },
      { label: "类型", value: note.kind || "fact", options: [
        { label: "事实：身份、宠物、工作、日程这类", value: "fact" },
        { label: "爱好：喜欢什么、口味、习惯", value: "interest" },
        { label: "偏好：她该怎么对待你（会约束她的说法与时机）", value: "preference" },
      ] },
    ]);
    if (!result) return;
    const [key, value, kind] = result.map(item => String(item || "").trim());
    if (!key || !value) {
      notice("主题和内容都不能为空");
      return;
    }
    perform({ umo: s.umo, action: "note_save", key, value, kind: kind || "fact", replace_key: key0 || "" });
  };
  $("add-note").onclick = () => submitNote("");
  document.querySelectorAll("[data-note-edit]").forEach(element => {
    element.onclick = () => submitNote(element.dataset.noteEdit);
  });
  document.querySelectorAll("[data-note-pin]").forEach(element => {
    element.onclick = () => perform({ umo: s.umo, action: "note_pin", key: element.dataset.notePin, pinned: element.dataset.pinned !== "1" });
  });
  document.querySelectorAll("[data-note-confirm]").forEach(element => {
    element.onclick = () => perform({ umo: s.umo, action: "note_confirm", key: element.dataset.noteConfirm, confirmed: true });
  });
  $("reset-activity").onclick = async () => {
    if (await pageConfirm("清空活跃统计？清空之后她只能从头重新观察，你写的备注会保留。")) perform({ umo: s.umo, action: "activity_reset" });
  };
  $("save-activity-note").onclick = () => perform({ umo: s.umo, action: "activity_note", note: $("activity-note").value.trim() });
  document.querySelectorAll("[data-note-delete]").forEach(element => {
    element.onclick = async () => {
      if (await pageConfirm("删除这条关于你的记录？删掉之后她就不会再按它行事，也不会再记得这件事。")) perform({ umo: s.umo, action: "note_delete", key: element.dataset.noteDelete });
    };
  });
  $("rhythm-reset").onclick = () => perform({ umo: s.umo, action: "rhythm_reset" });
  $("conditions-editor").oninput = () => { planDrafts.add("conditions"); planDirty = true; };
  $("save-conditions").onclick = () => {
    try {
      perform({ umo: s.umo, action: "conditions", value: JSON.parse($("conditions-editor").value) });
    } catch (error) {
      notice("条件 JSON 格式错误：" + error.message);
    }
  };
  document.querySelectorAll("[data-appointment]").forEach(element => {
    element.onclick = () => perform({ umo: s.umo, action: "appointment_status", id: element.dataset.appointment, status: element.dataset.status });
  });
}

$("plans-target").onchange = async () => {
  const select = $("plans-target");
  const next = select.value;
  if (planDirty && !await pageConfirm("当前有未保存的编辑，切换会丢弃，是否继续？")) {
    select.value = select.dataset.previous || next;
    return;
  }
  select.dataset.previous = next;
  renderPlans();
};
