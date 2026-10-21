"""Facts-tree UI script contracts (F-004 P03 slice D, F-004 API-002, 13-A).

The real ``webapp/static/js/facts-tree.js`` runs inside a Node ``vm`` with
minimal DOM doubles: these tests assert event wiring, state/error rendering,
footnote source-lookup interaction, the Decision 13-A fact status/source
block and the no-``company_id`` request contract, not pixels.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "webapp/static/js/facts-tree.js"

HARNESS = r'''
// Execute the real facts-tree.js. Minimal DOM doubles test event wiring, not pixels.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const caseName = process.argv[2];
const script = fs.readFileSync(path.join(process.cwd(), 'webapp/static/js/facts-tree.js'), 'utf8');

const textOf = el => (el.children.length ? el.children.map(textOf).join('') : el._text);

class Element {
  constructor(tag = '', id = '') {
    this.tagName = tag; this.id = id; this._text = ''; this.children = [];
    this.dataset = {}; this.attributes = {}; this.events = {}; this.className = ''; this.type = '';
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, force) {
        const on = force === undefined ? !classes.has(name) : force;
        if (on) classes.add(name); else classes.delete(name);
      },
    };
  }
  get textContent() { return textOf(this); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) { (this.events[name] ||= []).push(handler); }
  dispatch(name) { for (const handler of this.events[name] || []) handler({ preventDefault() {} }); }
  click() { this.dispatch('click'); }
  querySelectorAll(selector) {
    const found = [];
    const walk = el => {
      for (const child of el.children) {
        if (selector === 'button[data-code]' && child.tagName === 'button'
            && child.dataset.code !== undefined) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
}

const elements = new Map(['facts-tree', 'facts-tree-status', 'facts-tree-nav', 'facts-tree-detail']
  .map(id => [id, new Element('div', id)]));
const document = {
  getElementById(id) { assert.ok(elements.has(id), 'unexpected element id: ' + id); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
};
const calls = [];
let fetchImpl = async () => { throw new Error('fetchImpl not installed'); };
const fetch = async (url, options) => { calls.push(url); return fetchImpl(url, options); };
const ok = body => ({ ok: true, status: 200, json: async () => body });
const bad = (status, body) => ({ ok: false, status, json: async () => body });
const context = vm.createContext({ document, fetch, console });
const flush = async () => { for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve)); };

const status = () => elements.get('facts-tree-status').textContent;
const nav = () => elements.get('facts-tree-nav');
const detail = () => elements.get('facts-tree-detail').textContent;
const hasClass = (el, name) => String(el.className).split(' ').includes(name);
const findAll = pred => {
  const out = [];
  const walk = el => { for (const child of el.children) { if (pred(child)) out.push(child); walk(child); } };
  walk(elements.get('facts-tree-detail'));
  return out;
};

const BINDING = { template_pack: 'acc-pack', pack_version: 'v1', binding_revision: 7,
                  pack_digest: 'acc-digest', digest_source: 'store' };
const tree = state => ({
  company_id: 'acc-ui', node: 'tree', code: null, state,
  binding: state === 'unbound' ? null : BINDING,
  brief: state === 'ready' ? '树级简报正文' : null,
  guide: state === 'ready' ? '树级导览正文' : null,
  trunks: state === 'unbound' ? [] : [
    { code: 'T-01', order: 1, directory: '一枝目录', state, leaves: 2,
      generated_leaves: state === 'ready' ? 1 : 0 },
    { code: 'T-02', order: 2, directory: '二枝目录', state: 'not_generated', leaves: 1,
      generated_leaves: 0 },
  ],
});
const TRUNK = {
  company_id: 'acc-ui', node: 'trunk', code: 'T-01', state: 'ready', binding: BINDING,
  brief: '枝级简报正文', guide: '枝级导览正文',
  leaves: [
    { code: 'L-0101', trunk: 'T-01', title: '叶一标题', shape: 'prose', scope: 'company',
      state: 'active', version: 3 },
    { code: 'L-0102', trunk: 'T-01', title: '叶二标题', shape: 'table', scope: 'company',
      state: 'not_generated', version: null },
  ],
};
const LEAF = {
  company_id: 'acc-ui', node: 'leaf', code: 'L-0101', state: 'ready', binding: BINDING,
  leaf: { code: 'L-0101', trunk: 'T-01', title: '叶一标题', shape: 'prose', scope: 'company' },
  uri: 'sam://facts/acc', version: 3, leaf_state: 'active',
  index: { leaf_code: 'L-0101', trunk: 'T-01', title: '叶一标题', company_id: 'acc-ui',
           status: 'active', latest_period: '2026Q3', created_at: '2026-10-01T00:00:00',
           updated_at: '2026-10-19T00:00:00', sources: ['acc-a.pdf'], source_file_count: 1,
           version: 3, owner_skill: 'acc-skill' },
  content: '# 标题\n\n**加粗** 与 <em>标签</em> 都应按原文显示',
};

// F-004 API-002 fixtures: footnote definition lines in the leaf Markdown.
// '[^a1]' is defined twice to prove first-wins dedup; 'c/3' proves exact
// ref URL-encoding.
const SRC_CONTENT = '# 叶三标题\n\n正文引用[^a1]与[^b-2]及[^c/3]。\n\n'
  + '[^a1]: 甲文件.pdf → 第1段 (lines 1-9; block b1; sha abc; facts 1,2)\n'
  + '[^b-2]: 乙文件.pdf → 第2段 (lines 10-20; block b2; sha def; facts 3)\n'
  + '[^a1]: 重复定义应忽略\n'
  + '[^c/3]: 丙文件.pdf → 第3段 (lines 21-30; block b3; sha ghi; facts 4)';
const LEAF_SRC = {
  ...LEAF, code: 'L-0103',
  leaf: { code: 'L-0103', trunk: 'T-01', title: '叶三标题', shape: 'prose', scope: 'company' },
  content: SRC_CONTENT,
};
const LEAF_NO_SRC = { ...LEAF_SRC, content: '# 叶三标题\n\n没有任何脚注定义的正文。' };
const TRUNK_SRC = { ...TRUNK, leaves: [...TRUNK.leaves,
  { code: 'L-0103', trunk: 'T-01', title: '叶三标题', shape: 'prose', scope: 'company',
    state: 'active', version: 1 }] };
// API-002 response shape: source = {ref, file_hash, original_name, locator,
// lines: [start, end] | null}; no source_loc/start_line/end_line fields.
const SOURCE_A = {
  company_id: 'acc-ui', leaf: 'L-0103',
  source: { ref: 'a1', file_hash: 'abc123', original_name: '甲文件.pdf',
            locator: '第1段', lines: [1, 9] },
  original: { state: 'available', download_url: '/api/knowledge/sources/abc123/original' },
};
const SOURCE_B = {
  company_id: 'acc-ui', leaf: 'L-0103',
  source: { ref: 'b-2', file_hash: 'def456', original_name: '乙文件.pdf',
            locator: '第2段', lines: [10, 20] },
  original: { state: 'unavailable', reason: '原件已被移除' },
};
const refOf = url => decodeURIComponent((url.split('ref=')[1] || '').split('&')[0]);

// Decision 13-A fixtures: a ready leaf's formal fact references. Fact 11 is
// claimed with a full source; 12 is verified with no source at all; 13 is
// claimed with only a hash (no original name, page or span).
const LEAF_FACTS = {
  ...LEAF, code: 'L-0104',
  leaf: { code: 'L-0104', trunk: 'T-01', title: '叶四标题', shape: 'prose', scope: 'company' },
  facts: [
    { fact_id: 11, status: 'claimed', entity: '甲公司', attribute: '营业收入',
      period: '2026Q3', value: '1200', value_type: 'number', unit: '万元',
      source: { file_hash: 'abc123', original_name: '三季报.pdf', page: 4, span: '第2段' } },
    { fact_id: 12, status: 'verified', entity: '甲公司', attribute: '员工人数',
      period: '2026Q3', value: '56', value_type: 'number', unit: '人', source: null },
    { fact_id: 13, status: 'claimed', entity: '甲公司', attribute: '毛利率',
      period: '2026Q3', value: '32%', value_type: 'text', unit: null,
      source: { file_hash: 'deadbeefcafe', original_name: null, page: null, span: null } },
  ],
};
const TRUNK_FACTS = { ...TRUNK, leaves: [...TRUNK.leaves,
  { code: 'L-0104', trunk: 'T-01', title: '叶四标题', shape: 'prose', scope: 'company',
    state: 'active', version: 1 }] };

const run = async () => { vm.runInContext(script, context); await flush(); };

const readyFlow = async () => {
  fetchImpl = url => {
    if (url === '/api/facts/tree') return ok(tree('ready'));
    if (url === '/api/facts/tree?node=trunk&code=T-01') return ok(TRUNK);
    if (url === '/api/facts/tree?node=leaf&code=L-0101') return ok(LEAF);
    if (url === '/api/facts/tree?node=leaf&code=L-0102') {
      return ok({ company_id: 'acc-ui', node: 'leaf', code: 'L-0102', state: 'not_generated',
                  binding: BINDING, leaf: { code: 'L-0102', trunk: 'T-01', title: '叶二标题',
                  shape: 'table', scope: 'company' },
                  uri: null, version: null, leaf_state: null, index: null, content: null });
    }
    throw new Error('unexpected url: ' + url);
  };
  await run();
};

// Navigate tree → trunk → the footnote-bearing leaf, with a per-case source
// endpoint stub.
const sourceFlow = async (sourceImpl, leafBody) => {
  fetchImpl = url => {
    if (url === '/api/facts/tree') return ok(tree('ready'));
    if (url === '/api/facts/tree?node=trunk&code=T-01') return ok(TRUNK_SRC);
    if (url === '/api/facts/tree?node=leaf&code=L-0103') return ok(leafBody || LEAF_SRC);
    if (url.startsWith('/api/facts/tree/source?')) return sourceImpl(url);
    throw new Error('unexpected url: ' + url);
  };
  await run();
  nav().children[0].click();
  await flush();
  findAll(el => el.tagName === 'button' && el.dataset.code === 'L-0103')[0].click();
  await flush();
};
// Navigate tree → trunk → the fact-bearing leaf L-0104.
const factsFlow = async leafBody => {
  fetchImpl = url => {
    if (url === '/api/facts/tree') return ok(tree('ready'));
    if (url === '/api/facts/tree?node=trunk&code=T-01') return ok(TRUNK_FACTS);
    if (url === '/api/facts/tree?node=leaf&code=L-0104') return ok(leafBody || LEAF_FACTS);
    throw new Error('unexpected url: ' + url);
  };
  await run();
  nav().children[0].click();
  await flush();
  findAll(el => el.tagName === 'button' && el.dataset.code === 'L-0104')[0].click();
  await flush();
};
const factItems = () => findAll(el => hasClass(el, 'facts-tree-fact'));
const noSourceCall = () => { throw new Error('unexpected source lookup'); };
const sourceResults = () => findAll(el => hasClass(el, 'facts-tree-source-result'));
const clickRef = async ref => {
  findAll(el => el.tagName === 'button' && el.dataset.ref === ref)[0].click();
  await flush();
};

const main = async () => {
  if (caseName === 'tree-ready') {
    await readyFlow();
    assert.match(status(), /已生成/);
    assert.equal(nav().children.length, 2);
    assert.match(nav().children[0].textContent, /一枝目录 · T-01/);
    assert.match(nav().children[0].textContent, /叶 1\/2/);
    assert.match(detail(), /树级简报正文/);
    assert.match(detail(), /acc-pack/);
    nav().children[0].click();
    await flush();
    assert.match(status(), /树枝 T-01 · 已生成/);
    assert.ok(nav().children[0].classList.contains('active'));
    assert.ok(!nav().children[1].classList.contains('active'));
    assert.match(detail(), /枝级简报正文/);
    assert.match(detail(), /L-0101 · 叶一标题（已发布 · v3）/);
    assert.match(detail(), /L-0102 · 叶二标题（未生成）/);
    elements.get('facts-tree-detail').querySelectorAll('button[data-code]')[0].click();
    await flush();
    assert.match(status(), /叶子 L-0101 · 已生成/);
    assert.match(detail(), /# 标题/);
    assert.match(detail(), /<em>标签<\/em> 都应按原文显示/);
    assert.match(detail(), /版本 v3 · 状态 active/);
    assert.match(detail(), /2026Q3/);
    assert.match(detail(), /acc-a\.pdf/);
  } else if (caseName === 'leaf-not-generated') {
    await readyFlow();
    nav().children[0].click();
    await flush();
    elements.get('facts-tree-detail').querySelectorAll('button[data-code]')[1].click();
    await flush();
    assert.match(status(), /叶子 L-0102 · 未生成/);
    assert.match(detail(), /暂无可读正文/);
  } else if (caseName === 'tree-unbound') {
    fetchImpl = () => ok(tree('unbound'));
    await run();
    assert.match(status(), /未绑定模板/);
    assert.match(detail(), /尚未绑定事实树模板/);
    assert.equal(nav().children.length, 0);
  } else if (caseName === 'tree-empty') {
    fetchImpl = () => ok(tree('empty'));
    await run();
    assert.match(status(), /暂无内容/);
    assert.match(detail(), /等待资料阅读与发布/);
  } else if (caseName === 'tree-not-generated') {
    fetchImpl = () => ok(tree('not_generated'));
    await run();
    assert.match(status(), /未生成/);
    assert.match(detail(), /导览尚未生成/);
  } else if (caseName === 'disabled') {
    fetchImpl = () => bad(503, { error: 'facts_tree_disabled', message: 'facts tree is disabled' });
    await run();
    assert.match(status(), /功能当前已关闭/);
  } else if (caseName === 'unavailable') {
    fetchImpl = () => bad(503, { error: 'facts_tree_unavailable' });
    await run();
    assert.match(status(), /暂无同步数据/);
  } else if (caseName === 'http-error') {
    fetchImpl = () => bad(500, { error: 'storage_unavailable',
                                 message: 'db locked <script>alert(1)</script>' });
    await run();
    assert.match(status(), /读取失败（storage_unavailable）/);
    assert.ok(!status().includes('<script>'));
  } else if (caseName === 'network') {
    fetchImpl = async () => { throw new Error('socket hang up'); };
    await run();
    assert.match(status(), /网络异常/);
  } else if (caseName === 'no-company-param') {
    await readyFlow();
    nav().children[0].click();
    await flush();
    elements.get('facts-tree-detail').querySelectorAll('button[data-code]')[0].click();
    await flush();
    assert.deepEqual(calls, ['/api/facts/tree', '/api/facts/tree?node=trunk&code=T-01',
                             '/api/facts/tree?node=leaf&code=L-0101']);
    for (const url of calls) assert.ok(!url.includes('company_id'), url);
  } else if (caseName === 'sources-listed') {
    // Footnote extraction: unique, deduped, original order; no request fired
    // before any click; the section does not touch the leaf body.
    await sourceFlow(noSourceCall);
    assert.match(detail(), /来源回查/);
    assert.match(detail(), /正文引用\[\^a1\]与\[\^b-2\]及\[\^c\/3\]。/);
    const buttons = findAll(el => el.tagName === 'button' && el.dataset.ref !== undefined);
    assert.deepEqual(buttons.map(el => el.dataset.ref), ['a1', 'b-2', 'c/3']);
    assert.match(detail(), /甲文件\.pdf → 第1段/);
    // The duplicate definition stays in the raw body but not in the list.
    const itemTexts = findAll(el => hasClass(el, 'facts-tree-source-item'))
      .map(el => el.textContent);
    assert.equal(itemTexts.length, 3);
    assert.ok(!itemTexts.some(text => text.includes('重复定义应忽略')));
    assert.ok(!calls.some(url => url.includes('/source?')), calls.join(','));
  } else if (caseName === 'leaf-no-sources') {
    await sourceFlow(noSourceCall, LEAF_NO_SRC);
    assert.match(detail(), /没有任何脚注定义的正文/);
    assert.ok(!detail().includes('来源回查'));
    assert.equal(findAll(el => el.dataset.ref !== undefined).length, 0);
  } else if (caseName === 'source-available') {
    const sourceCalls = [];
    await sourceFlow(url => { sourceCalls.push(url); return ok(SOURCE_A); });
    await clickRef('a1');
    assert.deepEqual(sourceCalls, ['/api/facts/tree/source?leaf_code=L-0103&ref=a1']);
    assert.ok(!sourceCalls[0].includes('company_id'));
    const results = sourceResults();
    assert.match(results[0].textContent, /原件/);
    assert.match(results[0].textContent, /甲文件\.pdf/);
    assert.match(results[0].textContent, /第1段/);
    assert.match(results[0].textContent, /L1–L9/);
    const links = findAll(el => el.tagName === 'a');
    assert.equal(links.length, 1);
    assert.equal(links[0].attributes.href, '/api/knowledge/sources/abc123/original');
    assert.match(links[0].textContent, /打开原件/);
    assert.match(detail(), /正文引用\[\^a1\]/);
    assert.match(status(), /叶子 L-0103 · 已生成/);
    // Untouched items stay idle.
    assert.equal(results[1].textContent, '');
  } else if (caseName === 'source-ref-encoding') {
    const sourceCalls = [];
    await sourceFlow(url => { sourceCalls.push(url); return ok(SOURCE_A); });
    await clickRef('c/3');
    assert.deepEqual(sourceCalls, ['/api/facts/tree/source?leaf_code=L-0103&ref=c%2F3']);
  } else if (caseName === 'source-unavailable') {
    await sourceFlow(() => ok(SOURCE_B));
    await clickRef('b-2');
    const results = sourceResults();
    assert.match(results[1].textContent, /乙文件\.pdf/);
    assert.match(results[1].textContent, /原件当前不可访问：原件已被移除/);
    assert.equal(findAll(el => el.tagName === 'a').length, 0);
  } else if (caseName === 'source-no-lines') {
    // A citation without a line range (lines: null) renders no 行范围 row.
    const noLines = { ...SOURCE_B, source: { ...SOURCE_B.source, lines: null } };
    await sourceFlow(() => ok(noLines));
    await clickRef('b-2');
    const results = sourceResults();
    assert.match(results[1].textContent, /乙文件\.pdf/);
    assert.match(results[1].textContent, /第2段/);
    assert.ok(!results[1].textContent.includes('行范围'));
  } else if (caseName === 'source-http-error') {
    await sourceFlow(() => bad(404, { error: 'source_not_found', message: 'no such ref' }));
    await clickRef('a1');
    assert.match(sourceResults()[0].textContent, /来源回查失败（source_not_found）/);
    assert.match(detail(), /正文引用\[\^a1\]/);
    assert.match(status(), /叶子 L-0103 · 已生成/);
  } else if (caseName === 'source-network-error') {
    await sourceFlow(async () => { throw new Error('socket hang up'); });
    await clickRef('a1');
    assert.match(sourceResults()[0].textContent, /网络异常，来源回查失败/);
    assert.match(detail(), /正文引用\[\^a1\]/);
  } else if (caseName === 'source-malicious') {
    // Hostile names/locations stay plain text; off-origin and traversal
    // download URLs never become links.
    const evilA = { ...SOURCE_A,
      source: { ...SOURCE_A.source, original_name: '<img src=x onerror=alert(1)>甲',
                locator: '<b>坐标</b>' },
      original: { state: 'available', download_url: 'https://evil.example/steal' } };
    const evilB = { ...SOURCE_B,
      original: { state: 'available', download_url: '/api/knowledge/sources/../secret' } };
    await sourceFlow(url => (refOf(url) === 'a1' ? ok(evilA) : ok(evilB)));
    await clickRef('a1');
    await clickRef('b-2');
    assert.equal(findAll(el => el.tagName === 'img').length, 0);
    assert.equal(findAll(el => el.tagName === 'b').length, 0);
    assert.equal(findAll(el => el.tagName === 'a').length, 0);
    const results = sourceResults();
    assert.match(results[0].textContent, /<img src=x onerror=alert\(1\)>甲/);
    assert.match(results[0].textContent, /<b>坐标<\/b>/);
    assert.match(results[0].textContent, /无法打开原件/);
    assert.match(results[1].textContent, /无法打开原件/);
  } else if (caseName === 'source-parallel') {
    // Concurrent lookups on different refs land in their own items.
    let resolveA;
    await sourceFlow(url => (refOf(url) === 'a1'
      ? new Promise(resolve => { resolveA = () => resolve(ok(SOURCE_A)); })
      : ok(SOURCE_B)));
    findAll(el => el.dataset.ref === 'a1')[0].click();
    findAll(el => el.dataset.ref === 'b-2')[0].click();
    await flush();
    const results = sourceResults();
    assert.match(results[0].textContent, /正在回查来源/);
    assert.match(results[1].textContent, /乙文件\.pdf/);
    assert.match(results[1].textContent, /原件当前不可访问：原件已被移除/);
    resolveA();
    await flush();
    assert.match(results[0].textContent, /甲文件\.pdf/);
    assert.match(results[0].textContent, /L1–L9/);
    assert.match(results[1].textContent, /乙文件\.pdf/);
  } else if (caseName === 'facts-listed') {
    // 13-A: claimed and verified render as distinct verbatim labels; full,
    // missing and hash-only sources are each stated as-is.
    await factsFlow();
    assert.match(detail(), /事实状态与来源/);
    const items = factItems();
    assert.equal(items.length, 3);
    assert.match(items[0].textContent, /事实 #11/);
    assert.match(items[0].textContent, /已记录，待核验/);
    assert.ok(!items[0].textContent.includes('已核验'));
    assert.match(items[1].textContent, /事实 #12/);
    assert.match(items[1].textContent, /已核验/);
    assert.ok(!items[1].textContent.includes('已记录，待核验'));
    const badges = findAll(el => hasClass(el, 'facts-tree-fact-status'));
    assert.deepEqual(badges.map(el => el.textContent),
      ['已记录，待核验', '已核验', '已记录，待核验']);
    assert.ok(hasClass(badges[0], 'facts-tree-fact-status-claimed'));
    assert.ok(hasClass(badges[1], 'facts-tree-fact-status-verified'));
    // Full source: name, page and span shown.
    assert.match(items[0].textContent, /三季报\.pdf/);
    assert.match(items[0].textContent, /页码4/);
    assert.match(items[0].textContent, /坐标第2段/);
    assert.match(items[0].textContent, /值1200 万元/);
    // Null source: every locator field is stated as 未记录, none fabricated.
    assert.match(items[1].textContent, /来源未记录/);
    assert.match(items[1].textContent, /页码未记录/);
    assert.match(items[1].textContent, /坐标未记录/);
    // Hash-only source: the stable hash stands in for the missing name.
    assert.match(items[2].textContent, /来源哈希 deadbeefcafe/);
    assert.match(items[2].textContent, /页码未记录/);
    assert.match(items[2].textContent, /值32%/);
    // The leaf body stays untouched.
    assert.match(detail(), /# 标题/);
    assert.match(status(), /叶子 L-0104 · 已生成/);
  } else if (caseName === 'facts-empty') {
    // An empty array means the leaf currently cites no formal facts.
    await factsFlow({ ...LEAF_FACTS, facts: [] });
    assert.match(detail(), /事实状态与来源/);
    assert.match(detail(), /该叶当前无正式引用事实/);
    assert.equal(factItems().length, 0);
    assert.match(detail(), /# 标题/);
  } else if (caseName === 'facts-legacy') {
    // A ready response without the facts array (legacy) skips the block.
    const legacy = { ...LEAF_FACTS };
    delete legacy.facts;
    await factsFlow(legacy);
    assert.ok(!detail().includes('事实状态与来源'));
    assert.equal(factItems().length, 0);
    assert.match(detail(), /# 标题/);
  } else if (caseName === 'facts-malicious') {
    // Hostile fields stay plain text; an unknown status renders raw and is
    // never mapped onto a known label.
    const evil = { ...LEAF_FACTS, facts: [
      { fact_id: 21, status: '<b>verified</b>',
        entity: '<img src=x onerror=alert(1)>甲',
        attribute: '营业收入<script>alert(2)</script>', period: '2026Q3',
        value: '<b>1200</b>', value_type: 'text', unit: null,
        source: { file_hash: 'abc', original_name: '<img src=y onerror=alert(3)>报表.pdf',
                  page: '<b>4</b>', span: '<script>alert(4)</script>' } },
    ] };
    await factsFlow(evil);
    assert.equal(findAll(el => el.tagName === 'img').length, 0);
    assert.equal(findAll(el => el.tagName === 'b').length, 0);
    assert.equal(findAll(el => el.tagName === 'script').length, 0);
    const items = factItems();
    assert.equal(items.length, 1);
    assert.match(items[0].textContent, /<img src=x onerror=alert\(1\)>甲/);
    assert.match(items[0].textContent, /<b>verified<\/b>/);
    assert.ok(!items[0].textContent.includes('已核验'));
    assert.ok(!items[0].textContent.includes('已记录，待核验'));
    assert.match(items[0].textContent, /<img src=y onerror=alert\(3\)>报表\.pdf/);
    assert.match(items[0].textContent, /<script>alert\(4\)<\/script>/);
  } else {
    throw new Error('unknown case: ' + caseName);
  }
};

main().catch(error => { console.error(error); process.exit(1); });
'''


@pytest.mark.parametrize("case", [
    "tree-ready", "leaf-not-generated", "tree-unbound", "tree-empty", "tree-not-generated",
    "disabled", "unavailable", "http-error", "network", "no-company-param",
    "sources-listed", "leaf-no-sources", "source-available", "source-ref-encoding",
    "source-unavailable", "source-no-lines", "source-http-error", "source-network-error",
    "source-malicious", "source-parallel",
    "facts-listed", "facts-empty", "facts-legacy", "facts-malicious",
])
def test_facts_tree_ui_script(tmp_path, case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for executable frontend contracts")
    harness = tmp_path / "facts_tree_ui.cjs"
    harness.write_text(HARNESS, encoding="utf-8")
    result = subprocess.run([node, str(harness), case],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_facts_tree_script_stays_text_only_and_host_scoped():
    source = SCRIPT.read_text(encoding="utf-8")
    # Attribute access, not the mere words: comments may describe the ban.
    assert ".innerHTML" not in source
    assert ".outerHTML" not in source
    assert "company_id" not in source
