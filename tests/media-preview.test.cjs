const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE = path.join(__dirname, "..", "pages", "我只在乎你");
const TOKEN = "01234567-0123-4567-8910-0123456789ab";
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(request = async () => ({ token: TOKEN })) {
  class Element {
    constructor(tag) {
      this.tag = tag;
      this.children = [];
      this.listeners = {};
      this.isConnected = true;
    }
    append(child) { child.parent = this; this.children.push(child); }
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
    setAttribute(key, value) { this[key] = value; }
    removeAttribute(key) { delete this[key]; }
    addEventListener(event, handler) { this.listeners[event] = handler; }
    pause() { this.paused = true; }
    load() { this.reset = true; }
  }
  const calls = [];
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, new Element(id));
    return nodes.get(id);
  };
  const context = vm.createContext({
    $: node, selectedSession: () => context.session,
    escapeHTML: value => String(value),
    document: { createElement: tag => new Element(tag), querySelectorAll: () => [] },
    api: (...args) => { calls.push(args); return request(...args); },
  });
  for (const file of ["gallery.js", "library.js"]) {
    vm.runInContext(fs.readFileSync(path.join(PAGE, file), "utf8"), context);
  }
  const mount = (id, availability, media_kind, umo = "session-one") => {
    const details = new Element("details");
    const detail = new Element("div");
    details.querySelector = () => detail;
    context.mountWorkMedia(details, { umo }, { id, availability, media_kind, media_preview: "ready", title: id });
    return { details, detail, status: detail.children[0], button: detail.children[1] };
  };
  return { calls, context, mount, node };
}

test("old generic media types render as audio/video, with separate absent and backup states", () => {
  const { context, node } = harness();
  node("works-content").querySelectorAll = () => [];
  node("work-filters").insertAdjacentHTML = () => {};
  context.session = { umo: "session-one", state: { work_library: [
    { id: "mp3", title: "song.mp3", kind: "file", media_kind: "audio", availability: "local", archive_status: "saved" },
    { id: "mp4", title: "film.mp4", kind: "file", media_kind: "video", availability: "missing" },
  ] } };
  context.renderWorks();
  assert.match(node("work-filters").innerHTML, /data-filter="audio"[^>]*>音频 1/);
  assert.match(node("work-filters").innerHTML, /data-filter="video"[^>]*>视频 1/);
  const html = node("works-content").innerHTML;
  assert.match(html, /data-work-id="mp3"[\s\S]*?已备份/);
  assert.match(html, /data-work-id="mp4"[\s\S]*?本地文件缺失/);
  assert.doesNotMatch(html, /data-download="mp4"/);
});

test("media waits for explicit action, streams with native controls, and stops on replacement or collapse", async () => {
  const { calls, mount } = harness();
  const missing = mount("film", "missing", "video");
  assert.match(missing.status.textContent, /缺失/);
  assert.equal(missing.button, undefined);

  const audio = mount("song", "local", "audio");
  const video = mount("film", "local", "video", "session-two");
  audio.details.open = true;
  assert.equal(calls.length, 0);
  audio.button.onclick();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ["POST", "action", { umo: "session-one", action: "work_media", id: "song" }]);
  const player = audio.detail.children[2];
  assert.equal(player.tag, "audio");
  assert.equal(player.controls, true);
  assert.equal(player.preload, "none");
  assert.equal(Boolean(player.autoplay), false);
  assert.equal(player.src, "/api/v1/files/tokens/" + TOKEN);

  video.details.open = true;
  video.button.onclick();
  await flush();
  assert.equal(calls[1][2].umo, "session-two");
  assert.equal(player.paused, true);
  assert.equal(player.reset, true);
  assert.equal(player.src, undefined);
  assert.equal(audio.detail.children.length, 2);
  assert.equal(video.detail.children[2].tag, "video");
  video.details.open = false;
  video.details.listeners.toggle();
  assert.equal(video.detail.children.length, 2);
});

test("a collapsed activation is discarded before mounting; arbitrary source URLs are rejected", async () => {
  const pending = [];
  const { mount } = harness(() => new Promise(resolve => pending.push(resolve)));
  const audio = mount("song", "local", "audio");
  audio.details.open = true;
  audio.button.onclick();
  await flush();
  audio.details.open = false;
  audio.details.listeners.toggle();
  audio.details.open = true;
  audio.button.onclick();
  await flush();
  pending[0]({ token: TOKEN });
  await flush();
  assert.equal(audio.detail.children.length, 2);
  pending[1]({ token: "https://example.invalid/audio.mp3" });
  await flush();
  assert.equal(audio.detail.children.length, 2);
  assert.match(audio.status.textContent, /无法加载媒体/);
});
