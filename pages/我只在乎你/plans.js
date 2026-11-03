"use strict";
let planDirty = false;
const planDrafts = new Set();

function plansHasUnsaved() { return planDirty; }

function plansSession() {
  return selectedSession("plans-target");
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
  const prefs = Object.entries(s.state.preferences || {});
  const appointments = s.state.appointments || [];
  const checkpoints = Object.values(s.state.execution_checkpoints || {});
  if (s.state.execution_checkpoint && !checkpoints.length) checkpoints.push(s.state.execution_checkpoint);
  const cancelled = s.state.cancelled_tasks || [];
  const pause = s.state.pause_until > Date.now() / 1000;

  host.innerHTML = `
    <section class="panel"><div class="panel-head"><div><h2>长期任务</h2><p class="hint">独立执行上下文，后台推进；中断后等待核对，不自动重放。</p></div><button id="add-long-task" type="button">添加长期任务</button></div>
    <div class="panel-body stack">${(s.state.long_tasks || []).map(job => `<details class="work"><summary><span class="work-title">${escapeHTML(job.title)}</span><span class="pill">${escapeHTML(({queued:"排队中",running:"执行中",waiting:"等待核对",paused:"已暂停",failed:"失败",completed:"已完成",cancelled:"已取消"})[job.status] || job.status)}</span></summary><div class="work-detail stack"><p>${escapeHTML(job.goal)}</p><p class="hint">阶段 ${job.steps} · 尝试 ${job.attempts} · ${escapeHTML(job.workspace || "工作区尚未创建")}</p><pre class="text-preview">${escapeHTML(job.result || job.error || "尚无结果")}</pre><details><summary>阶段记录</summary>${(job.log || []).map(entry => `<p>${timeText(entry.at)} · ${escapeHTML(entry.summary)}</p>`).join("")}</details><div class="actions">${(["queued","running"].includes(job.status) ? ["pause","cancel"] : ["paused","waiting","failed"].includes(job.status) ? ["resume","cancel"] : ["delete"]).map(op => `<button data-long-task="${escapeHTML(job.id)}" data-operation="${op}" type="button">${({pause:"暂停",resume:"核对后恢复",cancel:"取消",delete:"移除记录"})[op]}</button>`).join("")}</div></div></details>`).join("") || '<p class="hint">尚无长期任务。可为资料整理、创作或排查任务设定明确目标和完成标准。</p>'}</div></section>
    <section class="panel">
      <div class="panel-head">
        <div><h2>下一次想做的事</h2><p class="hint">她能结合新对话重新判断，也可以由你直接安排。</p></div>
        <span class="pill">${s.state.future_task ? "已有计划" : "自然发生"}</span>
      </div>
      <div class="panel-body stack">
        ${s.state.pending_message ? `
        <div class="item" style="border:1px solid var(--warn);border-radius:10px;padding:10px 12px">
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

    <section class="panel">
      <div class="panel-head">
        <div><h2>她的节奏</h2><p class="hint">${settings.adaptive_frequency ? "自主调整已开启" : "固定配置节奏"}</p></div>
        <button id="rhythm-reset" type="button">恢复默认间隔</button>
      </div>
      <div class="panel-body stack">
        <div class="stats" style="margin:0">
          <div class="stat"><b>${escapeHTML(settings.adaptive_frequency ? (s.state.rhythm_minutes ?? settings.interval_minutes) : settings.interval_minutes)}</b><span>当前基础间隔 · 分钟</span></div>
          <div class="stat"><b>${escapeHTML(s.state.rounds || 0)}</b><span>本窗口已执行 · 上限 ${escapeHTML(settings.max_rounds)}</span></div>
        </div>
        <p class="hint">${escapeHTML(s.state.rhythm_reason || "尚未自主调整，使用默认节奏")} · 下次判断 ${timeText(s.state.next_due)}</p>
        <p class="hint">${s.state.next_task && s.state.window_end > Date.now() / 1000 ? "续行：" + escapeHTML(s.state.next_task) + " · 窗口至 " + timeText(s.state.window_end) : escapeHTML(s.state.continuation_status || "暂无短时续行安排")}</p>
        <p class="hint">${s.state.rhythm_scope === "global" ? "依据：用户明确整体频率偏好" : "依据：她的临时自主节奏"} · 有效至 ${s.state.rhythm_expires_at ? timeText(s.state.rhythm_expires_at) : "默认节奏"}</p>
        ${s.state.rhythm_ignored_reason ? `<p class="hint">最近忽略的调频：${escapeHTML(s.state.rhythm_ignored_reason)}</p>` : ""}
        ${(s.state.rhythm_history || []).length ? `<details class="work"><summary><span class="work-kind">历史</span><span class="work-title">最近节奏变化</span><span class="work-meta">${(s.state.rhythm_history || []).length} 条</span></summary><div class="work-detail"><div class="stack">${[...(s.state.rhythm_history || [])].reverse().map(item => `<p class="hint">${timeText(item.at)} · ${escapeHTML(item.minutes)} 分钟 · ${escapeHTML(item.reason)}</p>`).join("")}</div></div></details>` : ""}
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <div><h2>明确偏好</h2><p class="hint">只记录有原话依据的偏好，最多 12 项。</p></div>
        <span class="pill">${prefs.length} / 12</span>
      </div>
      <div class="list">
        ${prefs.map(([key, pref]) => `
          <div class="item">
            <div class="grow">
              <div class="item-title">${pref.pinned ? '<span class="pill accent">固定</span>' : ""}${escapeHTML(key)}</div>
              <div>${escapeHTML(pref.value)}</div>
              <div class="item-sub">原话：${escapeHTML(pref.evidence)} · 更新 ${pref.updated_at ? timeText(pref.updated_at) : "未知"}</div>
            </div>
            <div class="actions">
              <button data-pref-pin="${escapeHTML(key)}" data-pinned="${pref.pinned ? "1" : "0"}" class="btn-small" type="button">${pref.pinned ? "取消固定" : "固定"}</button>
              <button data-pref-delete="${escapeHTML(key)}" class="btn-small btn-danger" type="button">删除</button>
            </div>
          </div>`).join("") || '<p class="empty">还没有记录偏好。她不会从沉默推断喜恶。</p>'}
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

    ${cancelled.length || checkpoints.length ? `<details class="panel">
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
  document.querySelectorAll("[data-pref-pin]").forEach(element => {
    element.onclick = () => perform({ umo: s.umo, action: "preference_pin", key: element.dataset.prefPin, pinned: element.dataset.pinned !== "1" });
  });
  document.querySelectorAll("[data-pref-delete]").forEach(element => {
    element.onclick = async () => {
      if (await pageConfirm("删除这项偏好？之后新的明确表达仍可重新记录。")) perform({ umo: s.umo, action: "preference_delete", key: element.dataset.prefDelete });
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
