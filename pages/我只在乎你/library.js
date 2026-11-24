"use strict";
let workFilter = "all";
let companionOnly = false;
let showArchivedWorks = false;
let showWorkReferences = false;
const referenceProducers = new Set(["safe_read", "file_hash", "autonomous_read_file", "read_file", "autonomous_list_files", "autonomous_search_files"]);
const workSourceLabels = {
  private_companion: "陪伴作品库", autonomous_save_work: "自主创作", manual: "手动添加",
  run_template: "生成图片", safe_write: "保存文件", actual_output_dispatch: "已投递记录",
  safe_read: "读取文件", autonomous_read_file: "读取文件", read_file: "读取文件",
  file_hash: "校验文件", autonomous_list_files: "列出文件", autonomous_search_files: "检索文件",
};
const availabilityLabels = { local: "本地可用", inline: "正文可用", remote: "仅远程引用", missing: "本地文件缺失", unavailable: "来源不可用" };

function isWorkArchived(item, marks) {
  return item.aliases.some(id => marks["id:" + id]) || Boolean(item.content_hash && marks["hash:" + item.content_hash]);
}

const workKinds = { image: "图片", text: "正文", code: "脚本", file: "文件", audio: "音频", video: "视频" };
const workKindMarks = {
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9" r="1.5"/><path d="m3 17 5-5 4 4 3-3 6 6"/>',
  text: '<path d="M5 3h11l3 3v15H5zM8 9h8M8 13h8M8 17h5"/>',
  code: '<path d="m9 6-6 6 6 6m6-12 6 6-6 6"/>',
  file: '<path d="M5 3h9l5 5v13H5zM14 3v5h5"/>',
  audio: '<path d="M4 10v4m4-8v12m4-15v18m4-15v12m4-8v4"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3z"/>',
};
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

function uniqueLibrary(works, marks = {}) {
  const groups = new Map();
  const rank = item => (item.availability === "local" || item.availability === "inline" ? 8 : item.availability === "remote" ? 4 : 0)
    + (referenceProducers.has(item.producer) ? 0 : 2) + (item.status === "finished" ? 1 : 0);
  for (const item of works) {
    const digest = item.kind === "text" ? item.content_hash : item.kind === "image" ? item.image_digest : "";
    let key = digest ? `${item.kind}:${digest}` : `id:${item.id}`;
    // Show identical archived and active images separately so grouping never hides a live copy.
    if (item.kind === "image") key += marks["id:" + item.id] ? ":archived" : ":active";
    let entry = groups.get(key);
    if (!entry) {
      entry = { ...item, aliases: [], records: [], companion_copy: false, reference_only: true };
      groups.set(key, entry);
    } else if (rank(item) > rank(entry)) {
      const { aliases, records, companion_copy, reference_only } = entry;
      entry = { ...item, aliases, records, companion_copy, reference_only };
      groups.set(key, entry);
    }
    entry.aliases.push(item.id);
    entry.records.push(item);
    entry.companion_copy ||= item.producer === "private_companion";
    entry.reference_only &&= referenceProducers.has(item.producer);
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
  const filters = $("work-filters");
  if (typeof stopWorkMedia === "function") stopWorkMedia();
  if (!s) {
    filters.innerHTML = "";
    host.innerHTML = `<section class="works-empty" aria-labelledby="works-start-title">
      <span class="works-kicker">开始使用</span>
      <h3 id="works-start-title">先添加一个陪伴对象</h3>
      <p class="hint">先在「设置」填写要陪伴的 QQ 号，再回到这里选择对象并录入作品。</p>
    </section>`;
    return;
  }
  const archiveMarks = s.state.library_archived || {};
  const library = uniqueLibrary(s.state.work_library || [], archiveMarks);
  const archivedCount = library.filter(item => isWorkArchived(item, archiveMarks)).length;
  const section = library.filter(item => Boolean(isWorkArchived(item, archiveMarks)) === showArchivedWorks);
  const view = section.filter(item => item.reference_only === showWorkReferences && (!companionOnly || item.companion_copy))
    .map(item => ({ ...item, display_kind: item.media_kind || item.kind }));
  const visible = view.filter(item => workFilter === "all" || item.display_kind === workFilter)
    .sort((a, b) => (b.modified_at || 0) - (a.modified_at || 0));
  const outbox = s.state.outbox || [];
  const saved = s.state.saved_works || [];
  const pendingCount = outbox.filter(item => item.status === "pending").length;
  const referenceCount = section.filter(item => item.reference_only).length;
  const shelfTitle = `${showArchivedWorks ? "已移出的" : ""}${showWorkReferences ? "引用资料" : "创作成果"}`;
  const firstRun = !library.length;
  const focusedFilter = document.activeElement?.closest("#work-filters button, #works-management > summary");
  const managementOpen = $("works-management")?.open;

  filters.innerHTML = `
    <section class="works-start" aria-labelledby="works-guide-title">
      <div>
        <span class="works-kicker">作品索引</span>
        <h3 id="works-guide-title">${firstRun ? "把作品收进目录" : "浏览、整理，再分享"}</h3>
        <p class="hint">${firstRun ? "可将陪伴作品同步到当前目录，或添加手头已有的文本。" : "打开条目即可阅读或预览。要分享时，先将作品加入成果箱，再确认发送。"}</p>
      </div>
      <div class="actions" role="group" aria-label="作品快捷操作">
        <button id="sync-companion" ${firstRun ? 'class="btn-primary"' : ""} type="button">同步陪伴作品</button>
        <button id="add-text-work" type="button">添加文本作品</button>
        <button id="jump-works-outbox" class="btn-ghost" type="button">查看成果箱</button>
      </div>
    </section>
    <div class="works-tools">
      <section class="works-filter-box" aria-labelledby="works-filter-title">
        <h3 id="works-filter-title">浏览筛选</h3>
        <div class="works-filter-row" role="group" aria-labelledby="works-shelf-label">
          <span id="works-shelf-label" class="hint">范围</span>
          <div class="actions">
            <button class="chip ${showWorkReferences ? "active" : ""}" id="filter-work-references" type="button" aria-pressed="${showWorkReferences}">引用资料 ${referenceCount}</button>
            <button class="chip ${companionOnly ? "active" : ""}" id="filter-companion" type="button" aria-pressed="${companionOnly}">只看陪伴作品</button>
            <button class="chip ${showArchivedWorks ? "active" : ""}" id="toggle-archived" type="button" aria-pressed="${showArchivedWorks}">已移出 ${archivedCount}</button>
          </div>
        </div>
        <div class="works-filter-row" role="group" aria-labelledby="works-kind-label">
          <span id="works-kind-label" class="hint">类型</span>
          <div class="actions">
            ${[["all", "全部"], ...Object.entries(workKinds)].map(([kind, label]) =>
              `<button class="chip ${workFilter === kind ? "active" : ""}" data-filter="${kind}" type="button" aria-pressed="${workFilter === kind}">${label} ${view.filter(item => kind === "all" || item.display_kind === kind).length}</button>`).join("")}
          </div>
        </div>
      </section>
      <details class="panel works-management" id="works-management" ${managementOpen ? "open" : ""}>
        <summary><span>整理目录</span><span class="hint">补录 · 合订</span></summary>
        <div class="panel-body stack">
          <div class="actions" role="group" aria-label="目录整理操作">
            <button id="rescan-works" type="button">补录文本作品</button>
            <button id="compile-works" type="button">合并已完结章节</button>
          </div>
          <p class="hint">合订前可在正文条目中选章；原章保留，不会自动发送。</p>
        </div>
      </details>
    </div>
    <div class="works-sync hint">
      <p>${escapeHTML(s.state.companion_library_status || "陪伴作品同步：等待首次同步")}</p>
      ${s.state.companion_status ? `<p>${escapeHTML(s.state.companion_status)}</p>` : ""}
    </div>`;

  host.innerHTML = `
    <section class="works-catalogue" aria-labelledby="works-catalogue-title">
      <header class="works-section-head">
        <div><span class="works-kicker">01 / 目录</span><h3 id="works-catalogue-title">${shelfTitle}</h3></div>
        <span class="works-count">${visible.length} 项 · 按最近更新</span>
      </header>
      <div id="work-list">
      ${visible.map(item => {
        const kind = workKinds[item.display_kind] || "作品";
        const source = workSourceLabels[item.producer] || item.producer || "来源未记录";
        const status = item.status === "finished" ? "完稿" : item.status === "drafting" ? "草稿" : "";
        const date = item.modified_at ? new Date(item.modified_at * 1000) : null;
        const usable = ["local", "inline", "remote"].includes(item.availability);
        const isDraft = item.status === "drafting" && item.producer !== "private_companion";
        const id = escapeHTML(item.id);
        return `<details class="work works-entry" data-work-id="${id}">
          <summary>
            <span class="works-mark" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24">${workKindMarks[item.display_kind] || workKindMarks.file}</svg></span>
            <span class="works-entry-copy">
              <span class="works-entry-index">
                <span class="work-kind">${kind}</span>
                ${date && Number.isFinite(date.getTime()) ? `<time class="work-meta" datetime="${date.toISOString()}">${escapeHTML(date.toLocaleDateString())}</time>` : ""}
              </span>
              <span class="work-title">${escapeHTML(item.title || "未命名作品")}</span>
              <span class="works-entry-info">
                <span class="works-source">${escapeHTML(source)}${item.records.length > 1 ? ` · ${item.records.length} 条来源记录` : ""}</span>
                ${status ? `<span class="pill">${status}</span>` : ""}
                ${item.archive_status === "saved" && item.availability === "local" ? '<span class="pill ok">已备份</span>' : ""}
                ${!["local", "inline"].includes(item.availability) ? `<span class="pill warn">${availabilityLabels[item.availability] || "来源不可用"}</span>` : ""}
              </span>
            </span>
            <span class="works-chevron" aria-hidden="true"></span>
          </summary>
          <div class="works-entry-body">
            <div class="work-detail"></div>
            <div>
              <div class="actions" role="group" aria-label="作品操作">
                ${item.kind === "text" ? `<label class="check works-compile-check"><input type="checkbox" data-compile-id="${id}"><span>选择此章合订</span></label>` : ""}
                ${isDraft ? `<button data-finalize-draft="${id}" type="button" class="btn-primary">定稿</button>` : ""}
                ${usable ? `<button data-share="${id}" type="button">加入成果箱</button>` : ""}
                ${item.kind === "image" && item.availability === "remote" ? `<button data-save-image="${id}" type="button">补存本地图片</button>` : ""}
                ${item.availability === "local" ? `<button data-download="${id}" type="button">下载原文件</button>` : ""}
                <button data-organize="${id}" type="button">${showArchivedWorks ? "恢复到作品库" : "移出作品库"}</button>
                ${showArchivedWorks && !(item.kind === "image" && item.aliases.length > 1) ? `<button data-delete-work="${id}" type="button" class="btn-danger">彻底删除</button>` : ""}
                ${item.chapter_ids?.length && !showArchivedWorks ? `<button data-archive-chapters="${id}" type="button">收起已合订章节</button>` : ""}
              </div>
              <div class="works-provenance">
                <p class="works-kicker">来源记录</p>
                <ul>
                  ${item.records.map(row => `<li>
                    <span>${escapeHTML(workSourceLabels[row.producer] || row.producer || "来源未记录")}</span>
                    <span class="pill">${escapeHTML(availabilityLabels[row.availability] || "来源不可用")}</span>
                    ${row.path || row.url ? `<span class="works-path">${escapeHTML(row.path || row.url)}</span>` : ""}
                    ${row.archive_error ? `<span class="works-path">备份失败：${escapeHTML(row.archive_error)}</span>` : ""}
                  </li>`).join("")}
                </ul>
              </div>
            </div>
          </div>
        </details>`;
      }).join("") || `<div class="works-empty">
        <strong>${firstRun ? "目录还是空的" : "当前筛选下没有作品"}</strong>
        <p class="hint">${firstRun ? "从上方同步陪伴作品或添加文本，已有本地文本也可在「整理目录」中补录。" : "试试切换范围，或将类型切换到「全部」。"}</p>
      </div>`}
      </div>
    </section>

    <section class="works-outbox" aria-labelledby="works-outbox-title">
      <header class="works-section-head">
        <div><span class="works-kicker">02 / 分享</span><h3 id="works-outbox-title" tabindex="-1">成果箱</h3></div>
        <span class="works-count">${pendingCount} 待分享</span>
      </header>
      <p class="hint">加入时保存版本快照，只有确认后才发送；结果不确定时不自动重发。</p>
      <div class="list">
        ${outbox.map(item => {
          const state = outboxStates[item.status] || { label: item.status, tone: "" };
          const id = escapeHTML(item.id);
          return `<article class="works-post">
            <div class="works-post-copy">
              <div class="item-title"><h4>${escapeHTML(item.title || "未命名成果")}</h4><span class="pill ${state.tone}">${escapeHTML(state.label)}</span></div>
              ${item.reason ? `<p class="hint">${escapeHTML(item.reason)}</p>` : ""}
              ${item.error ? `<p class="hint" data-tone="bad">${escapeHTML(item.error)}</p>` : ""}
              ${item.content ? `<details class="work works-snapshot"><summary><span>查看保存的内容</span><span class="works-chevron" aria-hidden="true"></span></summary><div class="work-detail"><pre class="text-preview">${escapeHTML(previewText(item.content))}</pre>${String(item.content).length > 800 ? '<p class="hint">仅显示前 800 字符，发送时仍使用完整内容。</p>' : ""}</div></details>` : ""}
            </div>
            <div class="actions">
              ${item.work_id ? `<button data-open-work="${escapeHTML(item.work_id)}" type="button">原作品</button>` : ""}
              ${item.status === "failed" ? `<button data-outbox-retry="${id}" type="button">重新准备</button>` : ""}
              ${item.status === "pending" ? `<button class="btn-primary" data-outbox-send="${id}" type="button">发送</button>` : ""}
              ${!["sending", "discarded"].includes(item.status) ? `<button data-outbox-discard="${id}" type="button">丢弃</button>` : ""}
            </div>
          </article>`;
        }).join("") || '<p class="works-empty hint">还没有待分享成果。打开作品，选择「加入成果箱」。</p>'}
      </div>
    </section>

    ${saved.length ? `<details class="panel works-receipts">
      <summary><span>保存回执</span><span class="hint">${saved.length} 份历史文本</span></summary>
      <div class="panel-body stack">
        ${saved.map(work => `<div><p>${escapeHTML(work.title)} · ${escapeHTML(work.characters)} 字符</p><p class="hint">${escapeHTML(work.path)}</p><p class="hint">陪伴作品库：${work.companion?.saved ? "已同步 · 项目 " + escapeHTML(work.companion.project_id) : "未同步 · " + escapeHTML(work.companion?.reason || "旧回执")}</p></div>`).join("")}
      </div>
    </details>` : ""}
  `;

  const kindButtons = [...document.querySelectorAll("#work-filters [data-filter]")];
  kindButtons.forEach(button => {
    button.onclick = () => { workFilter = button.dataset.filter; renderWorks(); };
  });
  if (focusedFilter?.id) $(focusedFilter.id)?.focus();
  else if (focusedFilter?.dataset.filter) kindButtons.find(button => button.dataset.filter === focusedFilter.dataset.filter)?.focus();
  else if (focusedFilter?.tagName === "SUMMARY") $("works-management")?.querySelector("summary")?.focus();
  $("jump-works-outbox").onclick = () => $("works-outbox-title").focus();
  $("filter-work-references").onclick = () => { showWorkReferences = !showWorkReferences; companionOnly = false; workFilter = "all"; renderWorks(); };
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
    else if (["audio", "video"].includes(item.display_kind)) mountWorkMedia(details, s, item);
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
