const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE = path.join(__dirname, "..", "pages", "我只在乎你");

// Smoke: index.html contains every element id the scripts bind at load time.
// Ids created inside render templates (plans/works) are bound right after the
// template is injected, so only their presence in the template is checked.
test("index.html declares every load-time bound id", () => {
  const html = fs.readFileSync(path.join(PAGE, "index.html"), "utf8");
  const app = fs.readFileSync(path.join(PAGE, "app.js"), "utf8");
  const dialogs = fs.readFileSync(path.join(PAGE, "dialogs.js"), "utf8");
  const bound = [...new Set([...(app + "\n" + dialogs).matchAll(/\$\("([a-z-]+)"\)/gi)].map(m => m[1]))]
    // plan-editor is created by renderPlans and only touched later in app.js.
    .filter(id => id !== "plan-editor");
  assert.ok(bound.length >= 10, "should find bound ids");
  const missing = bound.filter(id => !html.includes(`id="${id}"`));
  assert.deepEqual(missing, [], "every load-time $('id') must exist in html");
});

test("dynamic render templates declare the ids they bind", () => {
  const html = fs.readFileSync(path.join(PAGE, "index.html"), "utf8");
  const dynamic = ["plan-editor", "add-long-task", "save-plan", "cancel-plan",
    "preview-plan", "add-project", "rhythm-reset", "conditions-editor",
    "save-conditions", "filter-companion", "sync-companion", "rescan-works",
    "toggle-archived", "add-text-work", "compile-works"];
  const templates = ["app.js", "plans.js", "library.js"].map(f =>
    fs.readFileSync(path.join(PAGE, f), "utf8")
  ).join("\n");
  const missing = dynamic.filter(id =>
    !templates.includes(`id="${id}"`) && !html.includes(`id="${id}"`)
  );
  assert.deepEqual(missing, [], "every dynamic id must appear in a template");
});

// Smoke: tabs carry aria-selected and are keyboard-navigable markup.
test("tabs markup exposes aria-selected", () => {
  const html = fs.readFileSync(path.join(PAGE, "index.html"), "utf8");
  const tabs = [...html.matchAll(/<button class="tab[^"]*"([^>]*)>/g)];
  assert.equal(tabs.length, 5);
  tabs.forEach(t => assert.match(t[1], /aria-selected="(?:true|false)"/));
});

// Smoke: every config key in the schema is reachable from some settings group,
// otherwise a working option would be invisible in the page.
test("every schema key belongs to a settings group", () => {
  const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "_conf_schema.json"), "utf8"));
  const js = fs.readFileSync(path.join(PAGE, "app.js"), "utf8");
  const block = js.slice(js.indexOf("const groups = ["), js.indexOf("const names = {"));
  const listed = new Set([...block.matchAll(/"([a-z0-9_]+)"/g)].map(m => m[1]));
  const injectedAtRuntime = [
    "proactive_segmented_reply", "proactive_segment_chars",
    "proactive_segment_interval_ms", "livingmemory_integration",
  ];
  const missing = Object.keys(schema).filter(key => !listed.has(key) && !injectedAtRuntime.includes(key));
  assert.deepEqual(missing, [], "settings groups must cover every schema key");
});

// 主题约束：暖米白纸感 + 墨黑点缀。这些规则靠人眼守不住，交给测试守。
// 排版事故回归：导航项套用了按钮的统一高度会把两行文字压出来互相盖住
test("multi-line controls are not forced to the single-line control height", () => {
  const css = fs.readFileSync(path.join(PAGE, "style.css"), "utf8");
  assert.match(css, /\.tab\s*{[^}]*height:\s*auto;/, "the nav item must size itself");
  assert.match(css, /\.tab\s*{[^}]*white-space:\s*normal;/, "nav hints may wrap");
  // 勾选项必须是"复选框 + 一列文字"，否则标题会被说明挤成竖条
  assert.match(css, /\.check\s*{[^}]*grid-template-columns:\s*15px minmax\(0, 1fr\);/);
  // 折叠块关着的时候内容不许占位压住下一块
  assert.match(css, /details\.panel:not\(\[open\]\) > \*:not\(summary\)[\s\S]{0,120}display:\s*none;/);
  assert.match(css, /details\.work:not\(\[open\]\) > \*:not\(summary\)[\s\S]{0,120}display:\s*none;/);
});

test("theme stays paper-light: no gradients, no shadows, single 8px radius scale", () => {
  const css = fs.readFileSync(path.join(PAGE, "style.css"), "utf8");
  for (const [name, value] of Object.entries({
    "--bg": "#faf7f1", "--surface": "#ffffff", "--elevated": "#f2ede4", "--border": "#e8e2d6",
    "--text": "#2b2b2e", "--text-muted": "#6b6b72", "--accent": "#a7988a", "--ink": "#232326",
  })) {
    assert.match(css, new RegExp(`${name}:\\s*${value};`, "i"), `${name} must keep its paper value`);
  }
  assert.doesNotMatch(css, /gradient\(/, "no gradients allowed");
  assert.doesNotMatch(css, /box-shadow\s*:/, "depth comes from 1px borders, not shadows");
  assert.match(css, /--radius:\s*8px;/);
  const radii = [...css.matchAll(/border-radius:\s*([^;]+);/g)].map(m => m[1].trim());
  assert.ok(radii.length > 10);
  assert.deepEqual([...new Set(radii)], ["var(--radius)"], "only the 8px radius token is allowed");
  // 脚本里不许自带圆角或用已删除的旧色板变量
  const scripts = ["app.js", "plans.js", "library.js", "dialogs.js", "gallery.js"]
    .map(file => fs.readFileSync(path.join(PAGE, file), "utf8")).join("\n");
  assert.doesNotMatch(scripts, /style="[^"]*border-radius/, "radius must come from the stylesheet");
  assert.doesNotMatch(scripts, /var\(--(?:bad|warn|cream|dim|line|shadow)/, "scripts must not use removed tokens");
});

// 图标必须是单色线性 SVG：emoji 会带颜色，破坏"无彩色强调色"的约束。
test("metrics use monochrome line icons instead of emoji", () => {
  const app = fs.readFileSync(path.join(PAGE, "app.js"), "utf8");
  assert.match(app, /metricIcons/);
  assert.match(app, /viewBox="0 0 18 18"/);
  const emoji = app.match(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]/gu);
  assert.equal(emoji, null, "no emoji in the page scripts");
  const css = fs.readFileSync(path.join(PAGE, "style.css"), "utf8");
  assert.match(css, /\.icon\s*{[^}]*fill:\s*none;/);
  assert.match(css, /\.icon\s*{[^}]*stroke:\s*currentColor;/);
});

// Smoke: dialog css classes exist so the restyled overlay is styled.
test("dialog overlay uses stylesheet classes", () => {
  const js = fs.readFileSync(path.join(PAGE, "dialogs.js"), "utf8");
  assert.match(js, /className="dialog-overlay"/);
  assert.match(js, /className="panel dialog-box"/);
  const css = fs.readFileSync(path.join(PAGE, "style.css"), "utf8");
  assert.match(css, /\.dialog-overlay/);
  assert.match(css, /\.dialog-box/);
});

// Smoke: metrics cards render icons + tone wiring.
test("metric cards include icon and tone hooks", () => {
  const js = fs.readFileSync(path.join(PAGE, "app.js"), "utf8");
  assert.match(js, /stat-icon/);
  assert.match(js, /data-tone=/);
});

// Smoke: the shared relative-time helper drives the "下次开口" wording so the
// overview and the plan page never disagree, and a due commitment reads 已到点
// instead of "约 0 分钟后".
test("relative time helper drives the speak commitment wording", () => {
  const context = vm.createContext({ Date });
  vm.runInContext(fs.readFileSync(path.join(PAGE, "common.js"), "utf8"), context);
  const now = Math.floor(Date.now() / 1000);
  const out = vm.runInContext(`({
    empty: soonText(0),
    due: soonText(${now - 30}),
    ten: soonText(${now + 600}),
    hour: soonText(${now + 3600}),
    twoDays: soonText(${now + 172800}),
    clipLong: clipText("abcdef", 3),
    clipShort: clipText("abc", 3),
    clipNull: clipText(null, 3),
    short: timeShort(1800000000),
  })`, context);
  assert.equal(out.empty, "");
  assert.equal(out.due, "已到点");
  assert.equal(out.ten, "10 分钟后");
  assert.equal(out.hour, "1 小时后");
  assert.equal(out.twoDays, "2 天后");
  assert.equal(out.clipLong, "abc…");
  assert.equal(out.clipShort, "abc");
  assert.equal(out.clipNull, "");
  assert.equal(out.short, new Date(1800000000000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }));
  const app = fs.readFileSync(path.join(PAGE, "app.js"), "utf8");
  const plans = fs.readFileSync(path.join(PAGE, "plans.js"), "utf8");
  assert.match(app, /约好开口 · \$\{soonText\(speakAt\)\}/);
  assert.match(app, /她自己约定的开口时间 \$\{timeShort\(speakAt\)\} · \$\{soonText\(speakAt\)\}/);
  assert.match(plans, /她自己约定的开口时间 \$\{timeShort\(s\.state\.next_speak_at\)\}/);
  assert.match(plans, /soonText\(s\.state\.next_speak_at\)/);
});
