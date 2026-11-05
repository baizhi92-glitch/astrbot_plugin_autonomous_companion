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
