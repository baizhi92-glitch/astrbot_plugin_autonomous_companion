const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const page = path.join(__dirname, '../pages/我只在乎你');
const loadLibrary = globals => {
  const context = vm.createContext({ $: () => ({}), ...globals });
  vm.runInContext(fs.readFileSync(path.join(page, 'library.js'), 'utf8'), context);
  return context;
};

test('same text from local and companion is one entry, different revisions remain', () => {
  const result = loadLibrary().uniqueLibrary([
    {id:'companion',kind:'text',title:'Story',content_hash:'same',producer:'private_companion',path:'companion/story.txt',availability:'missing'},
    {id:'local',kind:'text',title:'Story',content_hash:'same',path:'story.txt',availability:'local'},
    {id:'revision',kind:'text',title:'Story',content_hash:'different',availability:'inline'}
  ]);
  assert.equal(result.length, 2);
  assert.equal(result[0].id, 'local');
  assert.equal(result[0].companion_copy, true);
  assert.equal(result[0].aliases.includes('companion'), true);
});

test('series require explicit or valid numbered evidence; generic same-title entries stay separate', () => {
  const { workSeriesIndex, uniqueLibrary } = loadLibrary();
  const results = Object.fromEntries(workSeriesIndex(uniqueLibrary([
    {id:'one',title:'故事 · 第一章《初见》'},
    {id:'ten',title:'故事：第十章 晚安'},
    {id:'two',title:'故事 第２章'},
    {id:'invalid',title:'故事 第十十章'},
    {id:'edition',title:'故事 · 合订终稿',series_title:'故事',compilation:true,chapter_ids:['manuscript']},
    {id:'manuscript',title:'故事'},
    {id:'same-name',title:'故事',producer:'manual'},
    {id:'explicit',title:'不带编号',series_title:'故事'},
    {id:'standalone-a',title:'独立',producer:'autonomous_save_work'},
    {id:'standalone-b',title:'独立',producer:'autonomous_save_work'},
    {id:'plain-a',title:'另一本',series_title:'另一本'},
    {id:'plain-b',title:'另一本',producer:'autonomous_save_work'},
    {id:'manuscript-unlinked',title:'合稿',producer:'autonomous_save_work',manuscript_sections:[1,2]},
    {id:'manuscript-new',title:'合稿',producer:'autonomous_save_work',manuscript_sections:[1,2,3]},
    {id:'manuscript-plain',title:'合稿',producer:'autonomous_save_work'},
    {id:'manuscript-chapter',title:'合稿·第四章 末尾'},
    {id:'manuscript-next',title:'合稿·第五章 开头'},
    {id:'reference',title:'故事·第三章',producer:'safe_read'},
  ].map(row => ({kind:'text', ...row})))));
  assert.deepEqual(JSON.parse(JSON.stringify(results)), {
    one: { title: '故事', order: 1 },
    two: { title: '故事', order: 2 },
    ten: { title: '故事', order: 10 },
    edition: { title: '故事', order: -1 },
    manuscript: { title: '故事', order: 0 },
    explicit: { title: '故事', order: 0 },
    'plain-a': { title: '另一本', order: 0 },
    'manuscript-unlinked': { title: '合稿', order: 0, sections: [1,2] },
    'manuscript-new': { title: '合稿', order: 0, sections: [1,2,3] },
    'manuscript-chapter': { title: '合稿', order: 4 },
    'manuscript-next': { title: '合稿', order: 5 },
  });
});

test('catalogue paginates cards, preserves cross-page selection and opens outbox aliases', () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { focus() {}, scrollIntoView() {} });
    return nodes.get(id);
  };
  const host = node('works-content');
  const outboxButton = { dataset: { openWork: 'chapter-copy' } };
  const mounted = [];
  const parent = { open: false };
  let chapter;
  host.querySelectorAll = selector => selector === '[data-work-id]'
    ? Array.from(host.innerHTML.matchAll(/data-work-id="([^"]+)"/g), ([, id]) => {
      const details = { dataset: { workId: id }, querySelector: s => s === '[data-organize]' ? {} : s === 'summary' ? node('summary') : null,
        closest: () => parent, scrollIntoView() {} };
      if (id === 'chapter') chapter = details;
      return details;
    }) : [];
  const context = loadLibrary({
    $: node, selectedSession: () => context.session, escapeHTML: String,
    document: { querySelectorAll: selector => selector === '[data-open-work]' ? [outboxButton] : [] },
    mountTextWork: (_, __, item) => mounted.push(item.id),
  });
  context.session = { umo: 'test', state: {
    work_library: [
      ...Array.from({ length: 21 }, (_, i) => ({ id: `page-${i}`, kind: 'text', title: `Page ${i}`, modified_at: 100 - i })),
      { id: 'chapter', kind: 'text', title: '故事・第1章', series_title: '故事', availability: 'local', content_hash: 'c' },
      { id: 'chapter-copy', kind: 'text', title: '故事・第1章', content_hash: 'c' },
      { id: 'hidden', kind: 'text', title: '故事 第二章' },
    ],
    library_archived: { 'id:hidden': true },
    outbox: [{ id: 'post', work_id: 'chapter-copy' }],
  } };
  context.renderWorks();
  assert.equal(mounted.length, 20);
  assert.match(host.innerHTML, /第 1 \/ 2 页/);
  host.onchange({ target: { matches: () => true, checked: true, dataset: { compileId: 'page-0' } } });
  outboxButton.onclick();
  assert.match(host.innerHTML, /第 2 \/ 2 页/);
  assert.match(host.innerHTML, /data-work-series="故事"/);
  assert.doesNotMatch(host.innerHTML, /data-work-id="hidden"/);
  assert.equal(chapter.open, true);
  assert.equal(parent.open, true);
  assert.match(node('work-filters').innerHTML, /已选 1 项/);
  node('work-search').value = '故事';
  node('work-search').oninput({ type: 'input' });
  assert.doesNotMatch(host.innerHTML, /作品目录分页/);
  assert.doesNotMatch(host.innerHTML, /data-work-id="page-/);
  node('toggle-archived').onclick();
  assert.match(host.innerHTML, /data-work-id="hidden"/);
  assert.doesNotMatch(host.innerHTML, /data-work-id="chapter"/);
});

test('draft history waits until opened and confirms before restoring', async () => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, { append(...children) { (this.children ||= []).push(...children); } });
    return nodes.get(id);
  };
  const calls = [];
  const actions = [];
  let confirm = false;
  let toggle;
  let mountCount = 0;
  const history = { dataset: {}, querySelector: () => node('history-body'), addEventListener: (_, listener) => { toggle = listener; } };
  const draft = { dataset: { workId: 'draft' }, querySelector: selector => selector === '.works-history' ? history : {} };
  node('works-content').querySelectorAll = selector => selector === '[data-work-id]' ? [draft] : [];
  const context = loadLibrary({
    $: node, escapeHTML: String,
    selectedSession: () => ({ umo: 'test', state: { work_library: [
      { id: 'draft', title: '草稿', kind: 'text', status: 'drafting', producer: 'autonomous_save_work' },
    ] } }),
    document: { querySelectorAll: () => [], createElement: () => ({ append(...children) { this.children = children; } }) },
    mountTextWork: () => { mountCount++; },
    api: async (method, endpoint, body) => { calls.push([method, endpoint, body]); return { versions: [{ id: 'v1', saved_at: 123 }] }; },
    perform: async body => actions.push(body), pageConfirm: async () => confirm,
  });
  context.renderWorks();
  assert.equal(mountCount, 1);
  await toggle();
  assert.equal(calls.length, 0);
  history.open = true;
  await toggle();
  await toggle();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2].action, 'work_versions');
  const restore = node('history-body').children[0].children[1];
  await restore.onclick();
  assert.equal(actions.length, 0);
  confirm = true;
  await restore.onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(actions)), [{ umo: 'test', action: 'work_restore_version', id: 'draft', version_id: 'v1' }]);
});

test('finalize feedback waits for the saved receipt and reports a blocked compilation separately', async () => {
  const messages = [];
  const enabled = { disabled: false };
  const disabled = { disabled: true };
  let resolve, reject;
  const context = vm.createContext({
    busy: false, planDrafts: new Map(), rendered: {}, planDirty: false,
    document: { querySelectorAll: selector => [enabled, disabled].filter(button => selector === 'button' || !button.disabled) },
    api: () => new Promise((yes, no) => { resolve = yes; reject = no; }),
    notice: (...args) => messages.push(args), refresh: async () => {},
  });
  const app = fs.readFileSync(path.join(page, 'app.js'), 'utf8');
  vm.runInContext(app.slice(app.indexOf('async function perform('), app.indexOf('document.querySelectorAll(".tab").forEach(button =>')), context);
  const request = { action: 'work_finalize_draft', id: 'chapter' };
  const ready = context.perform(request);
  assert.deepEqual(messages, []);
  assert.equal(enabled.disabled, true);
  resolve({ saved: true, chapter: true, compilation: { status: 'blocked', reason: '章节有冲突' } });
  await ready;
  assert.deepEqual(messages.pop(), ['章节已定稿；合订未更新：章节有冲突。', 'warn']);
  assert.equal(enabled.disabled, false);
  assert.equal(disabled.disabled, true);
  const failed = context.perform(request);
  reject(new Error('正在执行'));
  await failed;
  assert.deepEqual(messages.pop(), ['定稿失败：正在执行', 'bad']);
  const missingReceipt = context.perform(request);
  resolve({});
  await missingReceipt;
  assert.match(messages.pop()[0], /^定稿失败：未收到保存确认/);
});
