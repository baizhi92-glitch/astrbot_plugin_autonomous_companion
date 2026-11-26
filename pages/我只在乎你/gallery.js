"use strict";
const previewCache = new Map();
const previewQueue = [];
let previewActive = 0;
let stopWorkMedia = null;

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

function mountWorkMedia(details, session, item) {
  const host = detailHost(details);
  const status = document.createElement("p");
  status.className = "hint";
  status.setAttribute("role", "status");
  host.append(status);
  if (item.availability !== "local" || item.media_preview !== "ready") {
    status.textContent = item.availability !== "local" ? "本地媒体文件缺失或不可用，无法预览。"
      : item.media_preview === "too_large" ? "媒体超过 256 MB 预览上限。"
      : item.media_preview === "empty" ? "媒体文件为空，无法预览。" : "此媒体格式不支持网页预览。";
    return;
  }

  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "加载" + (item.media_kind === "audio" ? "音频" : "视频") + "播放器";
  host.append(button);
  let player = null;
  let objectURL = null;
  let activation = 0;
  const stop = () => {
    ++activation;
    if (stopWorkMedia === stop) stopWorkMedia = null;
    if (player) {
      player.pause();
      player.removeAttribute("src");
      player.load();
      player.remove();
      player = null;
    }
    if (objectURL) { URL.revokeObjectURL(objectURL); objectURL = null; }
    status.textContent = "";
    button.hidden = false;
  };
  button.onclick = () => {
    if (!details.open || !details.isConnected || stopWorkMedia === stop) return;
    if (stopWorkMedia) stopWorkMedia();
    stopWorkMedia = stop;
    button.hidden = true;
    status.textContent = "正在准备播放器…";
    const requested = ++activation;
    queuePreview(async () => {
      if (requested !== activation || !details.open || !details.isConnected) return;
      try {
        const { token } = await api("POST", "action", { umo: session.umo, action: "work_media", id: item.id });
        if (requested !== activation || !details.open || !details.isConnected) return;
        if (typeof token !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(token)) throw new Error("未收到有效的媒体凭据");
        player = document.createElement(item.media_kind === "audio" ? "audio" : "video");
        player.className = "preview";
        player.controls = true;
        player.preload = "metadata";
        player.playsInline = true;
        player.setAttribute("aria-label", item.title || button.textContent);
        let fallbackUsed = false;
        const cannotPlay = () => {
          if (requested !== activation) return;
          stop();
          status.textContent = "预览播放失败。若原文件仍可下载，可以用本地播放器打开。";
        };
        player.addEventListener("error", async () => {
          if (requested !== activation) return;
          const mime = /\.wav$/i.test(item.path || item.title || "") ? "audio/wav"
            : /\.mp3$/i.test(item.path || item.title || "") ? "audio/mpeg" : "";
          if (fallbackUsed || item.media_kind !== "audio" || !mime || !(item.bytes > 0 && item.bytes <= 10 * 1024 * 1024)) {
            cannotPlay();
            return;
          }
          fallbackUsed = true;
          status.textContent = "串流失败，正在尝试读取本地音频…";
          try {
            const result = await api("POST", "action", { umo: session.umo, action: "work_download", id: item.id });
            if (requested !== activation || !details.open || !details.isConnected) return;
            if (typeof result.base64 !== "string" || result.base64.length > Math.ceil(10 * 1024 * 1024 / 3) * 4) throw new Error("音频超过备用预览限制");
            const bytes = Uint8Array.from(atob(result.base64), char => char.charCodeAt(0));
            objectURL = URL.createObjectURL(new Blob([bytes], { type: mime }));
            player.src = objectURL;
          } catch {
            cannotPlay();
          }
        });
        player.addEventListener("loadedmetadata", () => {
          if (requested === activation) status.textContent = "";
        });
        player.src = "/api/v1/files/tokens/" + token;
        status.textContent = "";
        host.append(player);
      } catch (error) {
        if (requested !== activation) return;
        stop();
        status.textContent = "无法加载媒体：" + error.message;
      }
    });
  };
  details.addEventListener("toggle", () => {
    if (!details.open && stopWorkMedia === stop) stop();
  });
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
