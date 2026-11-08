"use strict";
const previewCache = new Map();
const previewQueue = [];
let previewActive = 0;

function queuePreview(job) {
  previewQueue.push(job);
  const run = () => {
    if (previewActive >= 2 || !previewQueue.length) return;
    previewActive += 1;
    Promise.resolve()
      .then(previewQueue.shift())
      .catch(() => {})
      .finally(() => { previewActive -= 1; run(); });
  };
  run();
}

function detailHost(details) {
  return details.querySelector(".work-detail");
}

function mountGallery(details, session, item) {
  let loaded = false;
  const load = () => {
    if (loaded) return;
    loaded = true;
    const host = detailHost(details);
    const meta = document.createElement("p");
    meta.className = "hint";
    meta.textContent = "正在加载图片…";
    host.append(meta);
    queuePreview(async () => {
      try {
        const key = session.umo + ":" + item.id + ":" + (item.modified_at || "");
        let source = previewCache.get(key);
        if (!source) {
          const result = await api("POST", "action", { umo: session.umo, action: "work_image", id: item.id });
          source = result.image;
          previewCache.set(key, source);
          if (previewCache.size > 12) previewCache.delete(previewCache.keys().next().value);
        }
        const image = document.createElement("img");
        image.className = "preview";
        image.alt = item.title;
        image.loading = "lazy";
        image.src = source;
        meta.replaceWith(image);
      } catch (error) {
        meta.textContent = "图片未能加载：" + error.message + "；原服务可能已离线。";
        meta.dataset.tone = "bad";
      }
    });
  };
  details.addEventListener("toggle", () => { if (details.open) load(); });
  if (details.open) load();
}

function mountTextWork(details, session, item) {
  let loaded = false;
  const load = () => {
    if (loaded) return;
    loaded = true;
    const host = detailHost(details);
    const status = document.createElement("p");
    status.className = "hint";
    status.textContent = "正在读取正文…";
    host.append(status);
    queuePreview(async () => {
      try {
        const result = await api("POST", "action", { umo: session.umo, action: "work_read", id: item.id });
        const pre = document.createElement("pre");
        pre.className = "text-preview";
        pre.textContent = result.content || "（空文件）";
        status.replaceWith(pre);
      } catch (error) {
        status.textContent = "读取失败：" + error.message;
        status.dataset.tone = "bad";
      }
    });
  };
  details.addEventListener("toggle", () => { if (details.open) load(); });
}
