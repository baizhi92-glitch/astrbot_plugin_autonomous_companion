"use strict";
let workFilter = "all";
let companionOnly = false;
let showArchivedWorks = false;

function isWorkArchived(item, marks) {
  return item.aliases.some(id => marks["id:" + id]) || Boolean(item.content_hash && marks["hash:" + item.content_hash]);
}

const workKinds = { image: "图片", text: "正文", code: "脚本", file: "文件" };
const outboxStates = {
  pending: { label: "待分享", tone: "" },
  sending: { label: "发送中", tone: "warn" },
  sent: { label: "已交给平台", tone: "ok" },
  uncertain: { label: "结果待确认", tone: "warn" },
  failed: { label: "准备失败", tone: "bad" },
  discarded: { label: "已丢弃", tone: "" },
};

function worksSession() {
  return selectedSession("works-target");
}

function uniqueLibrary(works) {
  const groups = new Map();
  for (const item of works) {
    const key = item.kind === "text" && item.content_hash ? `text:${item.content_hash}` : `id:${item.id}`;
    let entry = groups.get(key);
    if (!entry) {
      entry = { ...item, aliases: [item.id], companion_copy: item.producer === "private_companion" };
      groups.set(key, entry);
    } else {
      entry.aliases.push(item.id);
      entry.companion_copy ||= item.producer === "private_companion";
      if (!entry.path && item.path) {
        const aliases = entry.aliases, companion = entry.companion_copy;
        Object.assign(entry, item, { aliases, companion_copy: companion });
      }
    }
  }
  return [...groups.values()];
}

function previewText(content) {
  const text = String(content ?? "");
  return text.length > 800 ? text.slice(0, 800) : text;
}

async function downloadWork(session, item) {
  const result = await api("POST", "action", { umo: session.umo, action: "work_download", id: item.id });
  const bytes = Uint8Array.from(atob(result.base64), c => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = result.filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function renderWorks() {
  const s = worksSession();
  const host = $("works-content");
  if (!s) {
    $("work-filters").innerHTML = "";
    host.innerHTML = '<div class="empty">先添加一个陪伴对象。</div>';
    return;
  }
  const library = uniqueLibrary(s.state.work_library || []);
  const archiveMarks = s.state.library_archived || {};
  const activeLibrary = library.filter(item => !isWorkArchived(item, archiveMarks));
  const visible = library.filter(item =>
    Boolean(isWorkArchived(item, archiveMarks)) === showArchivedWorks &&
    (workFilter === "all" || item.kind === workFilter) &&
    (!companionOnly || item.companion_copy)
  );
  const outbox = s.state.outbox || [];

  $("work-filters").innerHTML =
    [["all", "全部"], ["image", "图片"], ["text", "正文"], ["code", "脚本"], ["file", "文件"]]
      .map(([kind, label]) => `<button class="chip ${workFilter === kind ? "active" : ""}" data-filter="${kind}" type="button">${label} ${activeLibrary.filter(item => kind === "all" || item.kind === kind).length}</button>`)
      .join("") +
    `<span class="grow"></span>
     <button class="chip ${companionOnly ? "active" : ""}" id="filter-companion" type="button">只看陪伴作品</button>
     <button id="sync-companion" type="button">同步陪伴作品</button>
     <button id="rescan-works" type="button">补录文本作品</button>`;
  $("work-filters").insertAdjacentHTML("beforeend", '<button id="compile-works" type="button">合并已完结章节</button>');
  $("work-filters").insertAdjacentHTML("beforeend", `<button id="add-text-work" type="button">添加文本作品</button><button id="toggle-archived" type="button">${showArchivedWorks ? "返回作品库" : "已移出（" + (library.length - activeLibrary.length) + "）"}</button>`);

  host.innerHTML = `
    <p class="hint">${escapeHTML(s.state.companion_library_status || "陪伴作品同步：等待首次同步")}${s.state.companion_status ? " · " + escapeHTML(s.state.companion_status) : ""}</p>
    <div class="stack" id="work-list">
      ${visible.map(item => {
        const kind = workKinds[item.kind] || "作品";
        const source = item.aliases.length > 1 && item.companion_copy ? "本地作品 · 已同步陪伴作品库" : item.producer === "private_companion" ? "我会永远陪着你 · " + (item.status === "finished" ? "已完成" : "创作中") : "自主作品";
        const isDraft = item.status === "drafting" && item.producer !== "private_companion";
        return `<details class="work" data-work-id="${escapeHTML(item.id)}">
          <summary>
            <span class="work-kind">${kind}</span>
            <span class="work-title">${escapeHTML(item.title)}</span>
            <span class="work-meta">${escapeHTML(source)}</span>
            ${isDraft ? '<span class="pill ok">创作中</span>' : ''}
          </summary>
          <div class="work-detail">
            <div class="stack">
              <p class="hint">${escapeHTML(item.url || item.path || "无本地路径")}</p>
              ${item.kind === "image" ? `<p class="hint">${item.path ? "已保存本地图片：" + escapeHTML(item.path) : "仅远程引用，尚未保存本地"}${item.archive_error ? " · 归档失败：" + escapeHTML(item.archive_error) : ""}</p>` : ""}
              <div class="actions">
                ${item.kind === "text" ? `<label class="check"><input type="checkbox" data-compile-id="${escapeHTML(item.id)}">选择此章合订</label>` : ""}
                ${isDraft ? `<button data-finalize-draft="${escapeHTML(item.id)}" type="button" class="btn-small btn-primary">定稿</button>` : ""}
                <button data-share="${escapeHTML(item.id)}" type="button">加入成果箱</button>
                ${item.kind === "image" && !item.path ? `<button data-save-image="${escapeHTML(item.id)}" type="button">补存本地图片</button>` : ""}
                ${item.path ? `<button data-download="${escapeHTML(item.id)}" type="button">下载原文件</button>` : ""}
                <button data-organize="${escapeHTML(item.id)}" type="button">${showArchivedWorks ? "恢复到作品库" : "移出作品库"}</button>
                ${showArchivedWorks ? `<button data-delete-work="${escapeHTML(item.id)}" type="button" class="btn-small btn-danger">彻底删除</button>` : ''}
                ${item.chapter_ids?.length && !showArchivedWorks ? `<button data-archive-chapters="${escapeHTML(item.id)}" type="button">收起已合订章节</button>` : ""}
              </div>
            </div>
          </div>
        </details>`;
      }).join("") || `<div class="empty">${library.length ? "当前筛选下没有作品。" : "还没有发现作品。可补录文本，或等待她完成新的创作。"}</div>`}
    </div>

    <section class="panel" style="margin-top:16px">
      <div class="panel-head">
        <div><h2>成果箱</h2><p class="hint">发送前保存版本快照；结果不确定时不自动重发。</p></div>
        <span class="pill">${outbox.filter(item => item.status === "pending").length} 待分享</span>
      </div>
      <div class="list">
        ${outbox.map(item => {
          const state = outboxStates[item.status] || { label: item.status, tone: "" };
          return `<article class="item">
            <div class="grow">
              <div class="item-title">${escapeHTML(item.title || "未命名成果")}<span class="pill ${state.tone}">${escapeHTML(state.label)}</span></div>
              <div class="item-sub">${escapeHTML(item.reason || "")}</div>
              ${item.error ? `<div class="item-sub">${escapeHTML(item.error)}</div>` : ""}
              ${item.content ? `<details class="work" style="margin-top:8px"><summary><span class="work-kind">内容</span><span class="work-title">查看保存的内容</span></summary><div class="work-detail"><pre class="text-preview">${escapeHTML(previewText(item.content))}</pre>${String(item.content).length > 800 ? '<p class="hint">仅显示前 800 字符，发送时仍使用完整内容。</p>' : ""}</div></details>` : ""}
            </div>
            <div class="actions">
              ${item.work_id ? `<button class="btn-small" data-open-work="${escapeHTML(item.work_id)}" type="button">原作品</button>` : ""}
              ${item.status === "failed" ? `<button class="btn-small" data-outbox-retry="${escapeHTML(item.id)}" type="button">重新准备</button>` : ""}
              ${item.status === "pending" ? `<button class="btn-small btn-primary" data-outbox-send="${escapeHTML(item.id)}" type="button">发送</button>` : ""}
              ${!["sending", "discarded"].includes(item.status) ? `<button class="btn-small" data-outbox-discard="${escapeHTML(item.id)}" type="button">丢弃</button>` : ""}
            </div>
          </article>`;
        }).join("") || '<p class="empty">还没有待分享成果。</p>'}
      </div>
    </section>

    ${(s.state.saved_works || []).length ? `<details class="panel">
      <summary><span>保存回执</span><span class="hint">${(s.state.saved_works || []).length} 份历史文本</span></summary>
      <div class="panel-body stack">
        ${(s.state.saved_works || []).map(work => `<div><p>${escapeHTML(work.title)} · ${escapeHTML(work.characters)} 字符</p><p class="hint">${escapeHTML(work.path)}</p><p class="hint">陪伴作品库：${work.companion?.saved ? "已同步 · 项目 " + escapeHTML(work.companion.project_id) : "未同步 · " + escapeHTML(work.companion?.reason || "旧回执")}</p></div>`).join("")}
      </div>
    </details>` : ""}
  `;

  document.querySelectorAll("[data-filter]").forEach(button => {
    button.onclick = () => { workFilter = button.dataset.filter; renderWorks(); };
  });
  $("filter-companion").onclick = () => { companionOnly = !companionOnly; renderWorks(); };
  $("sync-companion").onclick = () => perform({ umo: s.umo, action: "companion_sync" });
  $("rescan-works").onclick = () => perform({ umo: s.umo, action: "work_rescan" });
  $("toggle-archived").onclick = () => { showArchivedWorks = !showArchivedWorks; renderWorks(); };
  $("add-text-work").onclick = async () => {
    const title = await pagePrompt("作品标题");
    if (!title?.trim()) return;
    const content = await pagePrompt("粘贴完整正文（最多20万字符）");
    if (!content?.trim()) return;
    perform({ umo: s.umo, action: "work_add_text", title, content });
  };
  $("compile-works").onclick = async () => {
    const ids = [...host.querySelectorAll("[data-compile-id]:checked")].map(input => input.dataset.compileId);
    const title = await pagePrompt("整篇作品标题（未勾选章节时，自动查找同标题的第X章/段；合订不代表程序判定故事已经完结）");
    if (!title?.trim()) return;
    if (await pageConfirm("确认这些章节已经完结并整理合订？原章保留，不自动发送；同章有不同版本时请先勾选。")) {
      perform({ umo: s.umo, action: "work_compile", title: title.trim(), ...(ids.length ? { ids } : {}) });
    }
  };

  const cards = [...host.querySelectorAll("[data-work-id]")];
  visible.forEach((item, index) => {
    const details = cards[index];
    if (!details) return;
    if (item.kind === "image") mountGallery(details, s, item);
    else if (item.kind === "text" || item.kind === "code") mountTextWork(details, s, item);
    const share = details.querySelector("[data-share]");
    const saveImage = details.querySelector("[data-save-image]");
    if (saveImage) saveImage.onclick = () => perform({ umo: s.umo, action: "work_archive_image", id: item.id });
    details.querySelector("[data-organize]").onclick = async () => {
      if (!showArchivedWorks && !await pageConfirm("移出此作品及重复副本？可从“已移出”恢复，原文件和陪伴库不删除。")) return;
      perform({ umo: s.umo, action: showArchivedWorks ? "work_restore" : "work_archive", ids: item.aliases });
    };
    const archiveChapters = details.querySelector("[data-archive-chapters]");
    if (archiveChapters) archiveChapters.onclick = async () => {
      if (await pageConfirm("收起这份终稿关联的原章节？保留终稿，章节可在“已移出”恢复。")) perform({ umo: s.umo, action: "work_archive_chapters", id: item.id });
    };
    if (share) share.onclick = () => perform({ umo: s.umo, action: "work_to_outbox", id: item.id });
    const download = details.querySelector("[data-download]");
    if (download) download.onclick = async () => {
      download.disabled = true;
      try { await downloadWork(s, item); }
      catch (error) { notice("下载失败：" + error.message); }
      finally { download.disabled = false; }
    };
    const deleteBtn = details.querySelector("[data-delete-work]");
    if (deleteBtn) deleteBtn.onclick = async () => {
      if (!await pageConfirm(`彻底删除作品 "${item.title}"？此操作不可恢复，原文件保留。`)) return;
      perform({ umo: s.umo, action: "work_delete", id: item.id });
    };
    const finalizeBtn = details.querySelector("[data-finalize-draft]");
    if (finalizeBtn) finalizeBtn.onclick = async () => {
      finalizeBtn.disabled = true;
      try {
        perform({ umo: s.umo, action: "work_finalize_draft", id: item.id });
        notice("已定稿，状态更新为已完成");
      } catch (error) {
        notice("定稿失败：" + error.message);
      } finally {
        finalizeBtn.disabled = false;
      }
    };
  });

  document.querySelectorAll("[data-outbox-send]").forEach(button => {
    button.onclick = async () => {
      if (await pageConfirm("发送这份成果到当前对象？")) perform({ umo: s.umo, action: "outbox", id: button.dataset.outboxSend, operation: "send" });
    };
  });
  document.querySelectorAll("[data-outbox-retry]").forEach(button => {
    button.onclick = () => perform({ umo: s.umo, action: "outbox", id: button.dataset.outboxRetry, operation: "retry" });
  });
  document.querySelectorAll("[data-outbox-discard]").forEach(button => {
    button.onclick = async () => {
      if (await pageConfirm("丢弃这份待分享成果？")) perform({ umo: s.umo, action: "outbox", id: button.dataset.outboxDiscard, operation: "discard" });
    };
  });
  document.querySelectorAll("[data-open-work]").forEach(button => {
    button.onclick = () => {
      const work = visible.find(item => item.aliases.includes(button.dataset.openWork));
      const details = cards.find(card => card.dataset.workId === work?.id);
      if (!details) { notice("原作品不在当前筛选或目录中。"); return; }
      details.open = true;
      details.scrollIntoView({ behavior: "smooth", block: "center" });
    };
  });
}

$("works-target").onchange = () => renderWorks();
