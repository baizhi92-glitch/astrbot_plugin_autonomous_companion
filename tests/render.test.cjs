// 渲染回归：用假 DOM 跑真实渲染函数，检查"她自己约定的开口时间"在概览、
// 计划页与行动记录里的呈现（到点要显示"已到点"，不能出现"约 0 分钟后"）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE = path.join(__dirname, "..", "pages", "我只在乎你");
const NOW = Math.floor(Date.now() / 1000);
const UMO = "test:FriendMessage:123";

function harness() {
  const store = new Map();
  const node = (id) => ({
    id, innerHTML: "", textContent: "", className: "", hidden: false, value: "",
    dataset: {}, style: {}, disabled: false, checked: false, children: [],
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {}, append() {}, remove() {}, focus() {}, click() {},
    addEventListener() {}, querySelectorAll: () => [], querySelector: () => null,
    contains: () => false, closest: () => null,
  });
  const byId = (id) => {
    if (!store.has(id)) store.set(id, node(id));
    return store.get(id);
  };
  const document = {
    activeElement: null, hidden: false,
    getElementById: byId, querySelectorAll: () => [], querySelector: () => node("tabs"),
    createElement: (tag) => node(tag), addEventListener() {}, body: node("body"),
  };
  const context = vm.createContext({
    document, window: { addEventListener() {} }, console,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0,
    Date, Math, JSON, Promise, String, Number, Object, Array, Set, Map,
  });
  for (const file of ["common.js", "dialogs.js", "plans.js", "app.js"]) {
    vm.runInContext(fs.readFileSync(path.join(PAGE, file), "utf8"), context);
  }
  return { context, byId, run: (code) => vm.runInContext(code, context) };
}

function load(h, state) {
  h.run(`
    data = {
      enabled: true,
      targets: ["${UMO}"],
      sessions: [{ umo: "${UMO}", active: true, running: false, state: ${JSON.stringify(state)} }],
      config: { targets: ["${UMO}"], daily_check_limit: 144, daily_round_limit: 96,
        daily_tool_limit: 60, daily_message_limit: 200, max_rounds: 4,
        adaptive_frequency: true, interval_minutes: 60 }
    };
    rendered.sessions = ""; rendered.activity = ""; rendered.plans = "";
    document.getElementById("plans-target").value = "${UMO}";
    document.getElementById("activity-target").value = "${UMO}";
  `);
}

function baseState(extra) {
  return Object.assign({
    identity: { user_name: "小满" },
    next_due: NOW + 1500,
    future_task: "",
    usage: { day: "2026-11-07", checks: 3, rounds: 1, tools: 0, messages: 0, failures: 0 },
  }, extra);
}

test("settings page groups every option under a described heading", () => {
  const h = harness();
  const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "_conf_schema.json"), "utf8"));
  const config = {};
  for (const [key, spec] of Object.entries(schema)) config[key] = spec.default;
  h.run(`data = { schema: ${JSON.stringify(schema)}, config: ${JSON.stringify(config)}, platforms: [] }; renderSettings();`);
  const html = h.byId("config-fields").innerHTML;
  const blocks = html.match(/<section class="panel">|<details class="panel">/g) || [];
  assert.equal(blocks.length, 12, "twelve settings groups");
  const open = html.match(/<h2>[^<]+<\/h2><p class="hint">[^<]+<\/p>/g) || [];
  const folded = html.match(/<span class="hint">\d+ 项 · 默认收起<\/span><\/summary><div class="panel-body stack"><p class="hint">[^<]+<\/p>/g) || [];
  assert.equal(open.length + folded.length, 12, "every group explains what it is for");
  // 每个配置项都必须能在页面上改到，否则等于藏了一个开关
  const rendered = new Set([...html.matchAll(/data-config="([a-z0-9_]+)"/g)].map(m => m[1]));
  assert.deepEqual(Object.keys(schema).filter(key => !rendered.has(key)), []);
});

test("activity axis ticks sit under their own bar, not at the two ends", () => {
  const h = harness();
  load(h, baseState({
    activity: {
      weekday: Array.from({ length: 24 }, (_, hour) => (hour >= 21 || hour <= 1 ? 6 : 0)),
      weekend: Array.from({ length: 24 }, () => 0),
      samples: 40,
      updated_at: NOW,
    },
  }));
  h.run(`document.getElementById("plans-target").value = "${UMO}"; renderPlans();`);
  const html = h.byId("plans-content").innerHTML;
  const ticks = [...html.matchAll(/<span style="left:([\d.]+)%">(\d\d)<\/span>/g)];
  assert.equal(ticks.length, 5, "five hour ticks");
  assert.deepEqual(ticks.map(m => m[2]), ["00", "06", "12", "18", "23"]);
  // 第一个刻度落在 0 点那根柱子的中心，而不是容器最左边
  assert.equal(ticks[0][1], "2.08");
  assert.equal(ticks[4][1], "97.92");
  const css = fs.readFileSync(path.join(PAGE, "style.css"), "utf8");
  assert.match(css, /\.hours-axis\s*{[^}]*position:\s*relative;/);
  assert.match(css, /\.hours-axis span\s*{[^}]*position:\s*absolute;/);
});

test("overview usage panel separates the night pool, morning report and split delivery", () => {
  const h = harness();
  load(h, baseState({
    morning_report: { day: "2026-11-08", at: NOW - 3600, text: "早上好。昨晚把第三节改完了，工具用了十一次。" },
    security_alerts: ["读到疑似要求忽略此前指令的段落，已只留摘要。"],
  }));
  h.run(`
    Object.assign(data.config, { split_night_budget: true, night_window_start_hour: 23,
      night_window_end_hour: 8, night_budget_percent: 40, morning_report_enabled: true,
      morning_report_hour: 8, proactive_segmented_reply: true, proactive_segment_chars: 60,
      proactive_segment_interval_ms: 800 });
    data.sessions[0].pool = { pool: "night", checks: 6, rounds: 4, tools: 11, messages: 2,
      limit_checks: 57, limit_rounds: 38, limit_tools: 120, limit_messages: 80, today: {} };
    document.getElementById("overview-target").value = "${UMO}";
    renderUsage();
  `);
  const html = h.byId("usage").innerHTML;
  assert.match(html, /额度池/);
  assert.match(html, /夜间 23:00–08:00/);
  assert.match(html, /工具 11\/120/);
  assert.match(html, /晨间记录/);
  assert.match(html, /2026-11-08 .*讲过一段（\d+ 字）/);
  assert.match(html, /早上讲过的那段话/, "the morning words are readable in full");
  assert.match(html, /发送方式/);
  assert.match(html, /分段发送 · 每段约 60 字 · 间隔 800 毫秒/);
  assert.match(html, /读到可疑内容/, "security alerts surface as their own block");
});

test("daytime shows the night quota as an upcoming switch, not a fake pool", () => {
  const h = harness();
  load(h, baseState({}));
  h.run(`
    Object.assign(data.config, { split_night_budget: true, night_window_start_hour: 23,
      night_window_end_hour: 8, night_budget_percent: 40, morning_report_enabled: true,
      morning_report_hour: 8, proactive_segmented_reply: false });
    data.sessions[0].pool = { pool: "day", checks: 2, rounds: 1, tools: 3, messages: 0,
      limit_checks: 144, limit_rounds: 96, limit_tools: 300, limit_messages: 200, today: {} };
    document.getElementById("overview-target").value = "${UMO}";
    renderUsage();
  `);
  const html = h.byId("usage").innerHTML;
  assert.match(html, /白天 · 到 23:00 换成夜间额度（占白天 40%）/);
  assert.match(html, /一次发一整条/);
  assert.match(html, /每天 08:00 之后讲一次，今天还没讲/);
});

test("overview card shows the self-committed opening with relative time", () => {
  const h = harness();
  load(h, baseState({
    next_speak_at: NOW + 660,
    next_speak_reason: "晚饭后想聊聊今天看到的那只猫，顺带问问她今天累不累，别显得像在汇报",
  }));
  const expected = h.run(`soonText(${NOW + 660})`);
  h.run("renderMetrics(sessions()); renderSessions(sessions());");
  const sessions = h.byId("sessions").innerHTML;
  const metrics = h.byId("metrics").innerHTML;
  assert.match(sessions, /她自己约定的开口时间/);
  assert.ok(sessions.includes(expected), `card should show the relative time ${expected}`);
  assert.match(sessions, /…<\/div>/, "long reasons are clipped");
  assert.ok(metrics.includes("约好开口 · " + expected), "metric label carries the next opening");
  assert.match(metrics, /<b>1<\/b>\s*<span>约好开口/);
});

test("a due commitment reads 已到点 instead of 约 0 分钟后", () => {
  const h = harness();
  load(h, baseState({ next_speak_at: NOW - 40, next_speak_reason: "想跟她说一句晚安", next_due: NOW - 5 }));
  h.run("renderMetrics(sessions()); renderSessions(sessions());");
  const sessions = h.byId("sessions").innerHTML;
  const metrics = h.byId("metrics").innerHTML;
  assert.match(sessions, /她自己约定的开口时间[\s\S]*已到点/);
  assert.doesNotMatch(sessions, /约 0 分钟后/);
  assert.doesNotMatch(sessions, /（0 分钟后）/);
  assert.match(metrics, /约好开口 · 已到点/);
});

test("no commitment renders no opening line, and the metric reads 约好开口 0", () => {
  const h = harness();
  load(h, baseState({}));
  h.run("renderMetrics(sessions()); renderSessions(sessions());");
  assert.doesNotMatch(h.byId("sessions").innerHTML, /她自己约定的开口时间/);
  assert.match(h.byId("metrics").innerHTML, /<b>0<\/b>\s*<span>约好开口/);
});

test("plans page exposes the opening as a stat and explains the toggle", () => {
  const h = harness();
  load(h, baseState({
    next_speak_at: NOW + 660,
    next_speak_reason: "晚饭后想聊聊",
    rhythm_minutes: 45, rhythm_reason: "她判断今晚可以聊得勤一点", rhythm_scope: "self",
    rhythm_expires_at: NOW + 5400, rounds: 2,
  }));
  h.run("renderPlans();");
  const html = h.byId("plans-content").innerHTML;
  const expected = h.run(`soonText(${NOW + 660})`);
  assert.match(html, /她自己约定的开口<\/span>/);
  assert.ok(html.includes("<b>" + expected + "</b>"), "stat value is the relative time");
  assert.match(html, /可自己约定开口/);
  assert.match(html, /到点只唤醒她一次/);
});

test("a held note shows up on the overview card and the plan page", () => {
  const h = harness();
  load(h, baseState({
    speak_wish: "想问问她面试结果怎么样，但别显得像在催",
    speak_wish_at: NOW - 1800,
    speak_wish_kept: 2,
    rhythm_minutes: 45, rounds: 1,
  }));
  h.run("renderMetrics(sessions()); renderSessions(sessions()); renderPlans();");
  const sessions = h.byId("sessions").innerHTML;
  const plans = h.byId("plans-content").innerHTML;
  assert.match(sessions, /心里存着想说的话/);
  assert.match(sessions, /30 分钟前记下/);
  assert.match(sessions, /已搁置 2 轮/);
  assert.match(plans, /心里存着想说的话/);
  assert.match(plans, /面试结果/);
});

test("no held note renders nothing, and neither does an empty one", () => {
  const h = harness();
  load(h, baseState({ speak_wish: "" }));
  h.run("renderMetrics(sessions()); renderSessions(sessions());");
  assert.doesNotMatch(h.byId("sessions").innerHTML, /心里存着想说的话/);
});

test("note panel labels the kind of every row and keeps staleness and history", () => {
  const h = harness();
  load(h, baseState({
    notes: {
      作息: { value: "晚上十一点后别发消息", kind: "preference", confirmed: true, evidence: "十一点以后别理我", updated_at: NOW - 200 * 86400 },
      语气: { value: "别用感叹号", kind: "preference", confirmed: true, evidence: "别用感叹号", updated_at: NOW, pinned: true, previous: "多用感叹号" },
      宠物: { value: "养了一只叫团子的橘猫", kind: "fact", confirmed: true, evidence: "我家团子", updated_at: NOW, confirmed_at: NOW },
      音乐: { value: "喜欢 city pop", kind: "interest", confirmed: true, evidence: "最近在循环", updated_at: NOW },
    },
    rhythm_minutes: 45, rounds: 1,
  }));
  h.run("renderPlans();");
  const html = h.byId("plans-content").innerHTML;
  assert.match(html, /关于你/);
  assert.match(html, /id="add-note"/);
  assert.equal((html.match(/class="pill[^"]*">偏好</g) || []).length, 2, "preferences are labelled");
  assert.equal((html.match(/class="pill[^"]*">事实</g) || []).length, 1, "facts are labelled");
  assert.equal((html.match(/class="pill[^"]*">爱好</g) || []).length, 1, "interests are labelled");
  assert.equal((html.match(/较久未更新/g) || []).length, 1, "only the stale unpinned preference is flagged");
  assert.match(html, /之前是：多用感叹号/);
  assert.match(html, /固定/);
  assert.match(html, /data-note-pin="语气"/);
  assert.match(html, /data-note-edit="宠物"/);
  assert.match(html, /data-note-delete="宠物"/);
});

test("note panel separates confirmed rows from guesses and explains itself when empty", () => {
  const h = harness();
  load(h, baseState({
    notes: {
      工作: { value: "可能在做设计", kind: "fact", confirmed: false, evidence: "又要改稿", updated_at: NOW },
      昵称: { value: "叫我小林", kind: "fact", confirmed: true, evidence: "用户手动添加", updated_at: NOW, source: "manual" },
    },
    rhythm_minutes: 45, rounds: 1,
  }));
  h.run("renderPlans();");
  const html = h.byId("plans-content").innerHTML;
  assert.match(html, /共 2 条/);
  assert.match(html, /1 条待确认/);
  assert.equal((html.match(/class="pill warn">未确认</g) || []).length, 1, "only the guess is flagged");
  assert.match(html, /data-note-confirm="工作"/);
  assert.doesNotMatch(html, /data-note-confirm="昵称"/, "confirmed rows need no confirm button");
  assert.match(html, /手动添加/);

  const empty = harness();
  load(empty, baseState({ notes: {}, rhythm_minutes: 45, rounds: 1 }));
  empty.run("renderPlans();");
  const emptyHtml = empty.byId("plans-content").innerHTML;
  assert.match(emptyHtml, /共 0 条/);
  assert.match(emptyHtml, /不会自己编/);
});

test("journal pills mark speak events with the accent style", () => {
  const h = harness();
  load(h, baseState({
    journal: [
      { time: "02:20:00", action: "speak_due", summary: "已到自己约定的开口时间：想聊聊" },
      { time: "02:19:30", action: "wish_kept", summary: "心里记下一句想说的话：想问问她" },
      { time: "02:19:00", action: "silent", summary: "这轮不打扰" },
    ],
    last_turn: { status: "completed", trigger: "speak", action: "chat", finished_at: NOW },
  }));
  h.run("renderActivity();");
  const html = h.byId("activity-content").innerHTML;
  assert.match(html, /<span class="pill accent">到点开口<\/span>/);
  assert.match(html, /<span class="pill accent">记下想说的话<\/span>/);
  assert.match(html, /自己约定的开口/, "the round trigger is labelled");
  assert.doesNotMatch(html, /<span class="pill accent">保持沉默<\/span>/);
});

test("activity panel draws one bar per hour and refuses to guess from few samples", () => {
  const h = harness();
  const weekday = new Array(24).fill(0);
  [8, 12, 13, 21, 22].forEach((hour) => { weekday[hour] = 4; });
  load(h, baseState({
    activity: { weekday, weekend: new Array(24).fill(0), samples: 20, updated_at: NOW },
    rhythm_minutes: 45, rounds: 1,
  }));
  h.run("renderPlans();");
  const html = h.byId("plans-content").innerHTML;
  assert.match(html, /你的作息/);
  assert.equal((html.match(/data-hour="/g) || []).length, 24, "one bar per hour");
  assert.match(html, /20 次发言/);
  assert.match(html, /常在线/);
  assert.match(html, /id="activity-note"/);
  assert.match(html, /id="reset-activity"/);

  const empty = harness();
  load(empty, baseState({
    activity: { weekday: new Array(24).fill(0), weekend: new Array(24).fill(0), samples: 2, updated_at: NOW },
    rhythm_minutes: 45, rounds: 1,
  }));
  empty.run("renderPlans();");
  const emptyHtml = empty.byId("plans-content").innerHTML;
  assert.match(emptyHtml, /还看不出规律/);
  assert.doesNotMatch(emptyHtml, /常在线/);
});
