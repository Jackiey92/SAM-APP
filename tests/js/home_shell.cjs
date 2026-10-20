// Execute the real inline JS. Minimal DOM doubles test event wiring, not pixels.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../../prototype/startup-ai-manager.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
const foundation = script.split('/* ===== /SAM foundation ===== */')[0];
const caseName = process.argv[2];
const readonly = caseName.includes('readonly');
const calls = [], alerts = [], timers = new Map();
let timerId = 0;
let fetchImpl = async () => ({ok: true, json: async () => ({rows: [{id: 1}]})});
const window = {
  SAM_CONFIG: {mode: readonly ? 'readonly' : 'full', company_id: 'acc-shell'},
  alert: message => alerts.push(message),
  fetch: async (...args) => {calls.push(args); return fetchImpl(...args);},
  events: {},
  addEventListener(name, handler) {(this.events[name] ||= []).push(handler);},
};

class Element {
  constructor(id = '') {
    this.id = id; this.isConnected = true; this.dataset = {}; this.style = {setProperty() {}};
    this.value = ''; this.textContent = ''; this.innerHTML = '';
    this.children = []; this.events = {}; this.parts = new Map(); this.attributes = {};
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
  addEventListener(name, handler) {(this.events[name] ||= []).push(handler);}
  dispatch(name, extras = {}) {
    for (const handler of this.events[name] || []) handler({target: this, preventDefault() {}, stopPropagation() {}, ...extras});
  }
  click() {this.dispatch('click');}
  focus() {document.activeElement = this;}
  scrollIntoView(options) {this.scrolled = options;}
  querySelectorAll() {return [this.querySelector('button')];}
  closest() {return this;}
  contains(element) {return element === this || element.dataset.importAction != null || element.dataset.knowledgeSource != null || element.dataset.explorerNode != null;}
  setAttribute(name, value) {this.attributes[name] = value;}
  appendChild(child) {this.children.push(child);}
  insertBefore(child) {this.children.unshift(child);}
  querySelector(selector) {
    if (!this.parts.has(selector)) this.parts.set(selector, new Element());
    return this.parts.get(selector);
  }
}
const elements = new Map();
for (const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], new Element(match[1]));
const screens = [...html.matchAll(/class="screen( active)?" id="([^"]+)"/g)].map(match => {
  const element = elements.get(match[2]);
  if (match[1]) element.classList.add('active');
  return element;
});
const suggestion = new Element(); suggestion.dataset.q = '测试建议';
const skill = new Element(); skill.dataset.q = '测试技能';
const knowledgeNav = new Element(); knowledgeNav.dataset.go = 'knowledge';
const driveNav = elements.get('importNavBtn'); driveNav.dataset.go = 'drive';
const document = {
  body: new Element(), hidden: false, events: {},
  addEventListener(name, handler) {(this.events[name] ||= []).push(handler);},
  createElement: () => new Element(),
  getElementById(id) {if (id.startsWith('source-') && !elements.has(id)) return null; assert.ok(elements.has(id), 'missing element: ' + id); return elements.get(id);},
  querySelectorAll(selector) {
    if (selector === '[data-go]') return [knowledgeNav, driveNav];
    if (selector === '.screen') return screens;
    if (selector === '.sugg-card') return [suggestion];
    if (selector === '.skill') return [skill];
    return [];
  },
};
const context = vm.createContext({window, document, console, setInterval() {},
  setTimeout(fn, delay) {assert.equal(delay, 1800); const id = ++timerId; timers.set(id, fn); return id;},
  clearTimeout(id) {timers.delete(id);},
  URLSearchParams, FormData: class {append() {}}, getComputedStyle: () => ({background: ''})});
if (!caseName.startsWith('ui-knowledge-') && !caseName.startsWith('ui-facts-') && !caseName.startsWith('ui-company-title')) vm.runInContext(caseName.startsWith('ui-') ? script : foundation, context);
const SAM = window.SAM;
if (SAM) SAM.demo.probe = [{id: 'demo'}];
const load = options => SAM.data.load('probe', {url: '/api/probe', ...options});
const flush = () => new Promise(resolve => setImmediate(resolve));

const self = {origin: 'declared', entity_type: 'self', status: 'active'};
const selfNamed = {...self, entity_name: 'acc-登记公司'};
const el = id => elements.get(id);
const ok = body => ({ok: true, status: 200, json: async () => body});
const bad = (status, body) => ({ok: false, status, json: async () => body});
const deferred = () => {let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve};};
const tick = async () => {
  const pending = [...timers.entries()];
  for (const [id, fn] of pending) {timers.delete(id); fn();}
  await flush();
};
const action = async (kind, index = 0) => {
  const button = new Element(); button.dataset.importAction = kind; button.dataset.importIndex = String(index);
  el('importFiles').dispatch('click', {target: button}); await flush();
};
const choose = async () => {
  el('importFileInput').files = [{name: '<img src=x onerror=alert(1)>.pdf'}];
  el('importFileInput').dispatch('change'); await flush();
};
const register = async name => {
  el('importCompanyName').value = name; el('importRegistration').dispatch('submit'); await flush();
};
const factsTreeBinding = {template_pack: 'generic', pack_version: 'v1', binding_revision: 2,
  pack_digest: 'acc-digest', digest_source: 'stored'};
const factsTreeTrunks = [
  {code: 'acc-trunk-fin', order: 1, directory: '<img src=x onerror=alert(2)> 财务', state: 'ready', leaves: 12, generated_leaves: 12},
  {code: 'acc-trunk-hr', order: 2, directory: 'acc 人事', state: 'not_generated', leaves: 8, generated_leaves: 3},
];
function factsTreeBody(state, extras = {}) {
  if (state === 'unbound') return {company_id: 'acc-shell', binding: null, node: 'tree', code: null, state};
  return {company_id: 'acc-shell', binding: factsTreeBinding, node: 'tree', code: null, state,
    brief: null, guide: null, trunks: factsTreeTrunks, ...extras};
}
async function cabinetCase(name) {
  let entities = name === 'ui-cabinet-registration' ? [{origin: 'model', entity_type: 'self', status: 'suggested'}] : [selfNamed];
  let job = {job_id: 'acc-job', file_hash: 'acc-hash', original_name: 'acc-test.pdf', status: 'queued', stage: 'queued'};
  let upload409 = name === 'ui-cabinet-409';
  let rosterError = name === 'ui-cabinet-registration-error';
  let pollError = false, uploadError = name === 'ui-cabinet-upload-error';
  let pendingPoll = null;
  const pendingList = name === 'ui-cabinet-upload-race' ? deferred() : null;
  const pendingUpload = name === 'ui-cabinet-upload-race' ? deferred() : null;
  fetchImpl = async (url, options) => {
    if (url === '/api/entity-roster') {
      if (options.method === 'POST') {
        const posted = JSON.parse(options.body);
        assert.deepEqual(posted, {entity_name: 'acc-文件柜公司'});
        entities = [{...self, entity_name: posted.entity_name}]; return ok({entity: entities[0]});
      }
      return rosterError ? bad(503, {message: '名册不可用'}) : ok({entities});
    }
    if (url === '/api/parse-jobs') return pendingList ? pendingList.promise : ok({jobs: name === 'ui-cabinet-readonly' ? [job] : []});
    if (url === '/api/import-guide') return ok({guide: {message: '建议', completeness: {}}});
    if (url === '/api/facts/tree') return ok(factsTreeBody('ready', {brief: 'acc 看板摘要'}));
    if (url === '/api/upload') {
      if (pendingUpload) return pendingUpload.promise;
      if (upload409) {upload409 = false; return bad(409, {error: 'company_not_registered', message: '请先登记本公司名称'});}
      if (uploadError) return bad(400, {message: '<拒绝上传>'});
      return ok({...job, original_name: '<img src=x onerror=alert(1)>.pdf', size: 8});
    }
    if (url.endsWith('/cancel')) {job = {...job, status: 'canceled'}; return ok({job});}
    if (url.endsWith('/retry')) {job = {...job, status: 'queued', message: null}; return ok({job});}
    if (url === '/api/parse-jobs/acc-job') {
      if (pendingPoll) return pendingPoll.promise;
      return pollError ? bad(503, {message: '进度服务不可用'}) : ok({job});
    }
    throw new Error('Unexpected URL ' + url);
  };
  await flush(); // 让首页初始化发起的登记读取先落定，再按本用例 mock 打开向导
  el('driveWizardBtn').click(); await flush();
  assert.equal(el('importMask').classList.contains('open'), true);
  assert.equal(el('overview').classList.contains('active'), true);
  if (name === 'ui-cabinet-readonly') {
    assert.equal(el('importRegistration').hidden, true);
    assert.match(el('importFiles').innerHTML, /取消/);
    await action('cancel'); await choose(); await register('acc-文件柜公司');
    assert.equal(calls.some(call => call[1].method === 'POST'), false);
    assert.deepEqual(alerts, Array(3).fill('这一步请在本地操作'));
    assert.equal(el('importMask').classList.contains('open'), true);
    await tick(); assert.equal(timers.size, 1); // reads remain usable
    return;
  }
  if (name === 'ui-cabinet-registration-error') {
    assert.match(el('importRegistrationMessage').textContent, /名册不可用/);
    await choose(); assert.equal(calls.some(call => call[0] === '/api/upload'), false);
    rosterError = false; await register('acc-文件柜公司');
    assert.equal(el('importRegistration').hidden, true);
    assert.match(el('importFiles').innerHTML, /排队中/); return;
  }
  if (name === 'ui-cabinet-registration') {
    assert.equal(el('importRegistration').hidden, false);
    await choose(); assert.equal(calls.some(call => call[0] === '/api/upload'), false);
    await register('  '); assert.match(el('importRegistrationMessage').textContent, /请填写/);
    // A failed POST stays in the same dialog and does not submit pending files.
    const original = fetchImpl;
    fetchImpl = async (url, opts) => url === '/api/entity-roster' && opts.method === 'POST' ? bad(400, {message: '登记被拒绝'}) : original(url, opts);
    await register('acc-文件柜公司'); assert.match(el('importRegistrationMessage').textContent, /登记被拒绝/);
    fetchImpl = original; await register('acc-文件柜公司');
    assert.equal(el('importRegistration').hidden, true);
    assert.equal(el('importMask').classList.contains('open'), true);
    assert.equal(el('overview').classList.contains('active'), true);
    assert.match(el('importFiles').innerHTML, /排队中/); return;
  }
  await choose();
  if (name === 'ui-cabinet-upload-race') {
    pendingList.resolve(ok({jobs: [job]})); await flush();
    assert.equal(timers.size, 1);
    pendingUpload.resolve(ok({...job, size: 8})); await flush();
    assert.equal(timers.size, 1);
    assert.equal((el('importFiles').innerHTML.match(/class="import-file /g) || []).length, 1);
    assert.match(el('importFiles').innerHTML, /排队中/); return;
  }
  if (name === 'ui-cabinet-409') {
    assert.equal(el('importRegistration').hidden, false);
    assert.match(el('importFiles').innerHTML, /等待登记公司/); assert.equal(alerts.length, 0);
    assert.equal(timers.size, 0);
    await register('acc-文件柜公司');
    assert.equal(el('importRegistration').hidden, true);
    assert.equal(calls.filter(call => call[0] === '/api/upload').length, 2);
    assert.equal(el('importMask').classList.contains('open'), true); return;
  }
  if (name === 'ui-cabinet-upload-error') {
    assert.match(el('importFiles').innerHTML, /&lt;拒绝上传&gt;/);
    assert.match(el('importFiles').innerHTML, /重试/); assert.equal(timers.size, 0);
    uploadError = false; await action('retry'); assert.match(el('importFiles').innerHTML, /排队中/); return;
  }
  assert.equal(timers.size, 1);
  assert.match(el('importFiles').innerHTML, /排队中/);
  assert.ok(!el('importFiles').innerHTML.includes('<img'));
  assert.doesNotMatch(el('driveFiles').innerHTML, /<img/);
  if (name === 'ui-cabinet-actions') {
    await action('cancel'); assert.match(el('importFiles').innerHTML, /已取消/); assert.equal(timers.size, 0);
    await action('retry'); assert.match(el('importFiles').innerHTML, /排队中/); assert.equal(timers.size, 1); return;
  }
  if (name === 'ui-cabinet-races') {
    pendingPoll = deferred(); await tick(); assert.equal(timers.size, 0);
    await action('cancel'); assert.match(el('importFiles').innerHTML, /已取消/);
    pendingPoll.resolve(ok({job: {...job, status: 'parsing'}})); await flush();
    assert.match(el('importFiles').innerHTML, /已取消/); assert.equal(timers.size, 0); return;
  }
  if (name === 'ui-cabinet-pause') {
    el('importClose').click(); assert.equal(timers.size, 0);
    el('driveWizardBtn').click(); await flush(); assert.equal(timers.size, 1);
    document.hidden = true; document.events.visibilitychange.forEach(fn => fn()); assert.equal(timers.size, 0);
    document.hidden = false; document.events.visibilitychange.forEach(fn => fn()); assert.equal(timers.size, 1); return;
  }
  if (name === 'ui-cabinet-projection-failed') {
    job = {...job, status: 'failed', error_kind: 'projection_failed', l2_committed: 1,
      message: '事实已入库，但简报/导览重建失败（安全校验）；请重试。'};
    await tick();
    assert.match(el('importFiles').innerHTML, /简报\/导览失败/);
    assert.match(el('importFiles').innerHTML, /事实已入库/);
    assert.match(el('importFiles').innerHTML, /重建简报\/导览/);
    assert.doesNotMatch(el('importFiles').innerHTML, /查看结果|>完成</);
    assert.equal(timers.size, 0);
    await action('retry');
    assert.match(el('importFiles').innerHTML, /排队中/);
    assert.ok(calls.some(call => call[0].endsWith('/retry') && call[1].method === 'POST'));
    return;
  }
  if (name === 'ui-cabinet-errors') {
    pollError = true; await tick(); assert.match(el('importFiles').innerHTML, /进度服务不可用/);
    assert.match(el('importFiles').innerHTML, /刷新状态/); assert.equal(timers.size, 0);
    pollError = false; await action('poll'); assert.equal(timers.size, 1);
    job = {...job, status: 'failed', message: '<后端解析错误>'}; await tick();
    assert.match(el('importFiles').innerHTML, /失败/); assert.match(el('importFiles').innerHTML, /&lt;后端解析错误&gt;/);
    assert.match(el('importFiles').innerHTML, /重试/); assert.equal(timers.size, 0);
    await action('retry'); assert.equal(timers.size, 1); return;
  }
  job = {...job, status: 'parsing', stage: 'consolidating', progress_current: 3, progress_total: 4};
  await tick(); assert.match(el('importFiles').innerHTML, /解析中 · consolidating · 75%/);
  assert.doesNotMatch(el('importFiles').innerHTML, />取消</);
  job = {...job, progress_total: null}; await tick(); assert.doesNotMatch(el('importFiles').innerHTML, /NaN|Infinity|%/);
  job = {...job, status: 'done'}; await tick(); assert.equal(timers.size, 0);
  assert.match(el('importFiles').innerHTML, /完成/);
  assert.match(el('importFiles').innerHTML, /data-import-action="result"/);
  assert.match(el('importFiles').innerHTML, /data-import-action="source"/);
  assert.doesNotMatch(el('importFiles').innerHTML, /target=|href=/);
  assert.match(el('importFiles').innerHTML, /查看结果/); assert.match(el('importFiles').innerHTML, /溯源/);
  // 看板事实树：首次进入读取一次 + 上传任务进入终态后刷新一次
  assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 2);
  const count = calls.length; await tick(); assert.equal(calls.length, count);
}


const knowledgeFixture = {
  modules: [{code: 'acc-category', name: 'acc 自定义分类', hidden: false}],
  evidence: {files: [{file_hash: 'acc-hash', original_name: '<script>alert(1)</script>.pdf', status: 'done',
    category: 'acc-category', label: 'acc 自定义分类', size_bytes: 2048, format: 'pdf', origin_zone: 'acc-upload', uploaded_at: '2026-10-10',
    l0: {content: 'acc L0 摘要'}, l1: {content: 'acc L1 概览'},
    source_ref: {file_hash: 'acc-hash'}, l2: {mapping: 'acc 原文 Sheet1!B3', manifest_uri: 'viking://acc/l2',
      pages: [{page: 0, mapping: '第零页'}, {page: 2, mapping: '<img onerror=alert(1)> Sheet1!B3'}]}}]},
  facts: {groups: [{category: 'acc-category', label: 'acc 自定义分类', facts: [{entity: 'acc 实体', attribute: 'acc 属性', value: 0,
    period: false, source_ref: {file_hash: 'acc-hash', page: 2, locator: 'Sheet1!B3'}}]}],
    cap_table: {snapshot: {total_issued: 0, total_fully_diluted: 0, option_pool: 0,
      holders: {'acc 持有人': {issued: 0, fully_diluted: 0, ownership_issued_pct: 0, ownership_fully_diluted_pct: 0}}, event_ids: ['acc-event']},
      events: [{event_id: 'acc-event', shares_delta: 0, provenance: [{source_ref: {file_hash: 'acc-hash', page: 0}, anchor: {page_no: 0}}]}]}},
  working_memory: {items: [{runtime: {goal: 'acc 目标', state: 'active', expires_at: '2026-10-11',
    open_loops: [{id: 'acc-open', text: 'acc 未闭环'}], working_notes: [{text: 'acc 待固化', confidence: 0, inferred: false}]},
    events: [{type: 'open_loop_add', payload: {id: 'acc-closed', text: 'acc 已闭环'}},
      {type: 'open_loop_remove', payload: {id: 'acc-closed'}}]}]}
};
async function knowledgeCase(name) {
  assert.match(html, /<div class="screen" id="knowledge">/);
  assert.match(html, /<button class="sys-item" type="button" data-go="knowledge">/);
  assert.doesNotMatch(html, /<a class="sys-item" href="\/knowledge">/);
  assert.match(html, /knowledge:"知识库"/);
  assert.match(script, /routeKnowledgeLocation\(\);\s*}\)\(\);\s*$/);
  let body = name.includes('empty') ? {} : knowledgeFixture;
  if (name.endsWith('payload')) body = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  fetchImpl = async url => {
    if (url === '/api/knowledge') {
      if (name.includes('failure')) throw new Error('unavailable');
      return ok(body);
    }
    if (url === '/api/entity-roster') return ok({entities: [selfNamed]});
    if (url === '/api/parse-jobs') return ok({jobs: [{job_id: 'acc-job', file_hash: 'acc-hash', status: 'done'}]});
    if (url === '/api/import-guide') return ok({guide: {completeness: {}}});
    throw new Error('Unexpected URL ' + url);
  };
  elements.set('source-acc-hash', new Element('source-acc-hash'));
  if (name.includes('legacy')) {
    window.location = {pathname: name.includes('source') ? '/knowledge/sources/acc-hash' : '/knowledge',
      hash: name.includes('source') ? '' : '#source-acc-hash', search: '?page=2&locator=Sheet1%21B3'};
  }
  vm.runInContext(script, context); await flush();
  if (!name.includes('legacy')) {knowledgeNav.click(); await flush();}
  const legacy = name.includes('legacy');
  assert.equal(el(legacy ? 'drive' : 'knowledge').classList.contains('active'), true);
  assert.equal(el('overview').classList.contains('active'), false);
  assert.equal(el('crumb').textContent, legacy ? '文件柜' : '知识库');
  assert.ok(calls.some(call => call[0] === '/api/knowledge' && (call[1].method || 'GET') === 'GET'));
  if (name.endsWith('payload')) {
    const first = body.evidence.files[0];
    const link = new Element(); link.dataset.knowledgeSource = first.file_hash;
    el('drive').dispatch('click', {target: link});
    console.log(JSON.stringify({files: el('driveFiles').innerHTML, mapping: el('knowledgeSourceBody').innerHTML, facts: el('knowledgeFacts').innerHTML,
      equity: el('knowledgeEquity').innerHTML, runtime: el('knowledgeRuntime').innerHTML})); return;
  }
  if (name.includes('empty') || name.includes('failure')) {
    assert.equal(el('driveFiles').innerHTML, '');
    assert.equal(el('driveEmpty').hidden, false);
    assert.match(el('knowledgeFacts').innerHTML, /暂无确认事实/);
    assert.match(el('knowledgeEquity').innerHTML, /暂无已存股权事件链/);
    assert.match(el('knowledgeRuntime').innerHTML, /暂无当前公司/);
    assert.equal(el('knowledgeDataSource').textContent, name.includes('failure') ? '读取失败，请刷新重试' : '暂无可用数据'); return;
  }
  assert.doesNotMatch(el('driveFiles').innerHTML, /acc L0 摘要|acc L1 概览/);
  assert.match(el('driveFiles').innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(el('driveFiles').innerHTML, /<script>|<img/);
  assert.match(el('knowledgeFacts').innerHTML, /acc 自定义分类/);
  assert.match(el('knowledgeFacts').innerHTML, /<td>0<\/td><td>false<\/td>/);
  assert.match(el('knowledgeEquity').innerHTML, /已发行总股数：0/);
  assert.match(el('knowledgeEquity').innerHTML, /0%/);
  assert.match(el('knowledgeEquity').innerHTML, /acc-event/);
  for (const text of ['acc 目标', 'acc 未闭环', 'acc 已闭环', 'acc 待固化', '置信度：0', '2026-10-11', '事件时间线'])
    assert.ok(el('knowledgeRuntime').innerHTML.includes(text), text);
  assert.equal(el('knowledgeDataSource').textContent, '');
  assert.match(el('knowledgeRuntime').innerHTML, /<table>[\s\S]*<tbody>[\s\S]*<\/tbody><\/table>/);
  if (name.includes('explorer')) {
    assert.doesNotMatch(html.split('id="knowledge">')[1].split('id="knowledgeSourceMask"')[0], /2A|2B|2C|knowledgeEvidence/);
    assert.match(el('knowledgeTree').innerHTML, /确认事实/);
    assert.match(el('knowledgeTree').innerHTML, /工作记忆/);
    assert.doesNotMatch(el('knowledgeTree').innerHTML, /原始证据/);
    for (const id of ['knowledgeFacts', 'knowledgeRuntime']) assert.doesNotMatch(el(id).innerHTML, /2A|2B|2C/);
    const readCount = calls.length;
    const node = new Element(); node.dataset.explorerNode = 'working';
    el('knowledgeTree').dispatch('click', {target: node});
    assert.equal(el('confirmed-facts').hidden, true);
    assert.equal(el('working-memory').hidden, false);
    assert.equal(el('knowledgePath').textContent, '知识库 / 工作记忆');
    el('knowledgeSelect').value = 'group:0'; el('knowledgeSelect').dispatch('change');
    assert.equal(el('working-memory').hidden, true);
    assert.equal(el('knowledgeEquity').hidden, true);
    assert.match(el('knowledgePath').textContent, /acc 自定义分类/);
    assert.equal(calls.length, readCount); // navigation uses existing data, no writes/reads
    driveNav.click(); await flush();
    assert.equal(el('drive').classList.contains('active'), true);
    assert.equal(el('knowledge').classList.contains('active'), false);
    assert.equal(el('importMask').classList.contains('open'), false);
    assert.equal(el('crumb').textContent, '文件柜');
    assert.match(el('driveStats').textContent, /1 个文件 · 合计 2.0 KB/);
    assert.match(el('driveFiles').innerHTML, /pdf|acc-upload|2026-10-10/);
    body = JSON.parse(JSON.stringify(knowledgeFixture));
    body.modules.push({code: 'acc-other', name: '<img src=x>', hidden: false});
    body.evidence.files.push({file_hash: 'acc-other', original_name: 'other.xlsx', category: 'acc-other', label: '<img src=x>', size_bytes: 0});
    el('driveRefresh').click(); await flush();
    assert.match(el('driveStats').textContent, /2 个文件/);
    assert.match(el('driveTree').innerHTML, /&lt;img/);
    assert.doesNotMatch(el('driveTree').innerHTML, /<img/);
    const folder = new Element(); folder.dataset.explorerNode = 'category:1';
    el('driveTree').dispatch('click', {target: folder});
    assert.match(el('driveFiles').innerHTML, /other.xlsx/);
    assert.doesNotMatch(el('driveFiles').innerHTML, /alert\(1\)/);
    assert.match(el('driveStats').textContent, /合计 0 B/);
    el('driveSelect').value = 'all'; el('driveSelect').dispatch('change');
    const original = new Element(); original.dataset.knowledgeSource = 'acc-hash'; original.dataset.sourceView = 'original';
    el('drive').dispatch('click', {target: original});
    assert.equal(el('knowledgeSourceMask').classList.contains('open'), true);
    assert.match(el('knowledgeSourceBody').innerHTML, /企业原件/);
    if (readonly) {
      assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /download/);
      assert.match(el('knowledgeSourceBody').innerHTML, /回本地查看/);
    } else assert.match(el('knowledgeSourceBody').innerHTML, /\/api\/knowledge\/sources\/acc-hash\/original/);
    el('knowledgeSourceClose').click();
    assert.equal(document.activeElement, original);
    for (const id of ['driveUploadBtn', 'driveWizardBtn', 'driveEmptyUpload']) {
      el(id).click(); await flush();
      assert.equal(el('importMask').classList.contains('open'), true);
      assert.equal(el('drive').classList.contains('active'), true);
      el('importClose').click();
    }
    if (readonly) {
      el('driveWizardBtn').click(); await flush(); await choose();
      assert.ok(alerts.every(message => message === '这一步请在本地操作'));
      assert.ok(alerts.length > 0);
      assert.equal(calls.some(call => (call[1].method || 'GET') !== 'GET'), false);
    }
    body = {}; el('driveRefresh').click(); await flush();
    assert.equal(el('driveEmpty').hidden, false);
    assert.doesNotMatch(el('driveStats').textContent, /合计/);
    assert.equal(el('driveTree').innerHTML.includes('category:'), false);
    return;
  }
  if (name.includes('partial')) {
    body = {evidence: {files: [{file_hash: 'acc-hash'}]}, facts: {groups: [{facts: [{value: false}]}]},
      working_memory: {items: [{runtime: {working_notes: [{}]}}]}};
    knowledgeNav.click(); await flush();
    assert.match(el('driveFiles').innerHTML, /尚无解析映射/);
    const link = new Element(); link.dataset.knowledgeSource = 'acc-hash';
    el('drive').dispatch('click', {target: link});
    assert.match(el('knowledgeSourceBody').innerHTML, /尚无可读取的 L2 原文/);
    assert.match(el('knowledgeFacts').innerHTML, /未存分类/);
    assert.match(el('knowledgeFacts').innerHTML, /<td>false<\/td>/);
    assert.match(el('knowledgeFacts').innerHTML, /原文定位字段未提供/);
    assert.match(el('knowledgeRuntime').innerHTML, /置信度：未提供/); return;
  }
  if (name.includes('warnings')) {
    body = {evidence: {files: [{warnings: ['证据暂不可读']}]}, facts: {cap_table: {warnings: ['回放校验失败']}},
      working_memory: {warnings: ['工作记忆暂不可读']}};
    knowledgeNav.click(); await flush();
    assert.match(el('driveFiles').innerHTML, /证据暂不可读/);
    assert.match(el('knowledgeEquity').innerHTML, /回放校验失败/);
    assert.doesNotMatch(el('knowledgeEquity').innerHTML, /暂无已存股权事件链/);
    assert.match(el('knowledgeRuntime').innerHTML, /工作记忆暂不可读/);
    assert.doesNotMatch(el('knowledgeRuntime').innerHTML, /暂无当前公司/); return;
  }
  if (name.includes('race')) {
    const pending = deferred(); fetchImpl = () => pending.promise;
    knowledgeNav.click(); await flush();
    fetchImpl = () => ok({}); knowledgeNav.click(); await flush();
    pending.resolve(ok(knowledgeFixture)); await flush();
    assert.equal(el('driveFiles').innerHTML, '');
    assert.equal(el('driveEmpty').hidden, false); return;
  }
  if (name.includes('cabinet')) {
    el('driveWizardBtn').click(); await flush();
    await action('result');
    assert.equal(el('importMask').classList.contains('open'), false);
    assert.equal(el('knowledge').classList.contains('active'), true);
    assert.equal(el('knowledgeSourceMask').classList.contains('open'), true);
    el('driveWizardBtn').click(); await flush();await action('source');
    assert.equal(el('importMask').classList.contains('open'), false);
  } else if (!name.includes('legacy-source')) {
    const link = new Element();link.dataset.knowledgeSource = 'acc-hash';link.dataset.page = '2';link.dataset.locator = 'Sheet1!B3';
    el('knowledge').dispatch('click', {target: link});
  }
  assert.equal(el('knowledgeSourceMask').classList.contains('open'), true);
  assert.match(el('knowledgeSourceBody').innerHTML, /Sheet1!B3/);
  assert.match(el('knowledgeSourceBody').innerHTML, /&lt;img/);
  assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /<img/);
  if (name.includes('legacy-source')) assert.match(el('knowledgeSourceBody').innerHTML, /knowledge-selected/);
  el('knowledgeSourceMask').dispatch('keydown', {key: 'Escape'});
  assert.equal(el('knowledgeSourceMask').classList.contains('open'), false);
  assert.equal(el('knowledgeSourceMask').attributes['aria-hidden'], 'true');
  const missing = new Element();missing.dataset.knowledgeSource = 'foreign-or-missing';
  el('knowledge').dispatch('click', {target: missing});
  assert.match(el('knowledgeSourceBody').innerHTML, /尚无可读取的 L2 原文/);
  assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /Sheet1!B3/);
  el('knowledgeSourceMask').dispatch('click');
  assert.equal(el('knowledgeSourceMask').classList.contains('open'), false);
  assert.equal(calls.some(call => (call[1].method || 'GET') !== 'GET' && call[0] !== '/api/import-guide'), false);
}

async function factsTreeCase(name) {
  // 公司看板结构契约：主屏不再展示静态样例公司数字，保留既有导航与入口。
  const overviewHtml = html.split('id="overview">')[1].split('id="knowledge">')[0];
  assert.match(overviewHtml, /id="factsBoard"/);
  assert.match(overviewHtml, /id="factsState"/);
  assert.match(overviewHtml, /href="\/facts"/);
  assert.match(overviewHtml, /id="factsUploadBtn"/);
  assert.match(overviewHtml, /hero-ask" data-go="chat"/);
  assert.doesNotMatch(overviewHtml, /企业健康度|214\.00|净利润|现金跑道|在手订单|股权结构|growth-card|data-gkey/);
  if (name === 'ui-facts-ready') {
    fetchImpl = async url => {
      if (url === '/api/entity-roster') return ok({entities: [{...self, entity_name: 'acc-事实树公司'}]});
      assert.equal(url, '/api/facts/tree'); return ok(factsTreeBody('ready', {brief: '# acc 树摘要\n<img src=x onerror=alert(1)> 摘要第二行'}));
    };
    vm.runInContext(script, context); await flush();
    assert.equal(el('overview').classList.contains('active'), true);
    assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 1); // 首次进入事实树只读取一次
    assert.equal(calls.length, 2); // 另有一次同源登记公司名读取
    assert.equal((calls[0][1].method || 'GET'), 'GET');
    assert.equal(calls[0][1].credentials, 'same-origin');
    assert.equal(el('homeCompanyName').textContent, 'acc-事实树公司');
    assert.match(el('factsBoardSub').textContent, /事实树摘要已生成/);
    assert.match(el('factsBoardSub').textContent, /generic · v1/);
    assert.equal(el('factsBrief').hidden, false);
    assert.match(el('factsBrief').textContent, /<img src=x/); // 摘要以纯文本持有，不进入 HTML
    assert.equal(el('factsBrief').innerHTML, '');
    assert.match(el('factsState').textContent, /纯文本展示/);
    assert.equal(el('factsTrunks').hidden, false);
    assert.match(el('factsTrunks').innerHTML, /&lt;img/);
    assert.doesNotMatch(el('factsTrunks').innerHTML, /<img/);
    assert.match(el('factsTrunks').innerHTML, /摘要已生成/);
    assert.match(el('factsTrunks').innerHTML, /摘要待生成/);
    assert.match(el('factsTrunks').innerHTML, /12 \/ 12 叶/);
    assert.match(el('factsTrunks').innerHTML, /3 \/ 8 叶/);
    assert.equal(el('factsRetryBtn').hidden, true);
    return;
  }
  if (name === 'ui-facts-empty') {
    fetchImpl = async () => ok(factsTreeBody('empty'));
    vm.runInContext(script, context); await flush();
    assert.match(el('factsBoardSub').textContent, /暂无事实叶/);
    assert.match(el('factsState').textContent, /还没有已生成的事实叶/);
    assert.match(el('factsState').textContent, /上传企业资料/);
    assert.equal(el('factsBrief').hidden, true);
    assert.equal(el('factsTrunks').hidden, true);
    assert.equal(el('factsRetryBtn').hidden, true);
    assert.doesNotMatch(el('factsState').textContent, /正常|良好|0/); // 空态不显示成 0 或正常经营结论
    return;
  }
  if (name === 'ui-facts-unbound') {
    fetchImpl = async url => {
      if (url === '/api/facts/tree') return ok(factsTreeBody('unbound'));
      if (url === '/api/entity-roster') return ok({entities: [selfNamed]});
      if (url === '/api/parse-jobs') return ok({jobs: []});
      if (url === '/api/import-guide') return ok({guide: {message: '建议', completeness: {}}});
      throw new Error('Unexpected URL ' + url);
    };
    vm.runInContext(script, context); await flush();
    assert.equal(el('factsBoardSub').textContent, '尚未绑定事实树模板');
    assert.match(el('factsState').textContent, /尚未绑定事实树模板/);
    assert.equal(el('factsBrief').hidden, true);
    assert.equal(el('factsTrunks').hidden, true);
    el('factsUploadBtn').click(); await flush(); // 文档柜上传入口保留且可用
    assert.equal(el('importMask').classList.contains('open'), true);
    assert.equal(el('overview').classList.contains('active'), true); // 主页仍是默认主视图
    return;
  }
  if (name === 'ui-facts-not-generated') {
    fetchImpl = async () => ok(factsTreeBody('not_generated'));
    vm.runInContext(script, context); await flush();
    assert.match(el('factsBoardSub').textContent, /树级摘要待生成/);
    assert.match(el('factsState').textContent, /树级摘要尚未生成/);
    assert.equal(el('factsBrief').hidden, true); // 缺失摘要不补造内容
    assert.equal(el('factsTrunks').hidden, false);
    assert.match(el('factsTrunks').innerHTML, /摘要待生成/);
    assert.match(el('factsTrunks').innerHTML, /3 \/ 8 叶/);
    return;
  }
  if (name === 'ui-facts-disabled') {
    fetchImpl = async () => bad(503, {error: 'facts_tree_disabled', message: 'facts tree is disabled'});
    vm.runInContext(script, context); await flush();
    assert.equal(el('factsBoardSub').textContent, '事实树状态暂不可用');
    assert.match(el('factsState').textContent, /未开启事实树功能/);
    assert.match(el('factsState').textContent, /未展示任何替代数据/);
    assert.equal(el('factsRetryBtn').hidden, true); // 功能门关闭不是可重试的网络错误
    assert.equal(el('factsBrief').textContent, '');
    assert.equal(el('factsTrunks').innerHTML, '');
    return;
  }
  if (name === 'ui-facts-scope-missing') {
    fetchImpl = async () => bad(503, {error: 'company_scope_missing', message: '宿主未注入公司范围'});
    vm.runInContext(script, context); await flush();
    assert.match(el('factsState').textContent, /宿主未注入公司范围/);
    assert.equal(el('factsRetryBtn').hidden, false);
    return;
  }
  if (name === 'ui-facts-failure') {
    let fail = true;
    fetchImpl = async () => {if (fail) throw new Error('network down'); return ok(factsTreeBody('ready', {brief: 'acc 摘要'}));};
    vm.runInContext(script, context); await flush();
    assert.match(el('factsState').textContent, /读取失败/);
    assert.match(el('factsState').textContent, /network down/);
    assert.match(el('factsState').textContent, /未回退到演示数据/);
    assert.equal(el('factsRetryBtn').hidden, false);
    assert.equal(el('factsBrief').hidden, true);
    assert.equal(el('factsTrunks').hidden, true);
    fail = false;
    el('factsRetryBtn').click(); await flush();
    assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 2);
    assert.equal(el('factsBrief').hidden, false);
    assert.equal(el('factsBrief').textContent, 'acc 摘要');
    assert.equal(el('factsRetryBtn').hidden, true);
    return;
  }
  if (name === 'ui-facts-upload-refresh') {
    let tree = factsTreeBody('empty');
    let job = {job_id: 'acc-job', file_hash: 'acc-hash', original_name: 'acc-test.pdf', status: 'queued', stage: 'queued'};
    fetchImpl = async url => {
      if (url === '/api/facts/tree') return ok(tree);
      if (url === '/api/entity-roster') return ok({entities: [selfNamed]});
      if (url === '/api/parse-jobs') return ok({jobs: []});
      if (url === '/api/import-guide') return ok({guide: {message: '建议', completeness: {}}});
      if (url === '/api/knowledge') return ok({});
      if (url === '/api/upload') return ok({...job, size: 8});
      if (url === '/api/parse-jobs/acc-job') return ok({job});
      throw new Error('Unexpected URL ' + url);
    };
    vm.runInContext(script, context); await flush();
    assert.match(el('factsState').textContent, /还没有已生成的事实叶/);
    assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 1);
    el('factsUploadBtn').click(); await flush(); // 从看板进入既有上传向导
    assert.equal(el('importMask').classList.contains('open'), true);
    el('importFileInput').files = [{name: 'acc-test.pdf'}];
    el('importFileInput').dispatch('change'); await flush();
    assert.match(el('importFiles').innerHTML, /排队中/);
    tree = factsTreeBody('ready', {brief: 'acc 新摘要'});
    job = {...job, status: 'done'};
    await tick(); // 任务进入终态 → 看板重新读取事实树
    assert.match(el('importFiles').innerHTML, /完成/);
    assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 2);
    assert.equal(el('factsBrief').hidden, false);
    assert.equal(el('factsBrief').textContent, 'acc 新摘要');
    assert.match(el('factsBoardSub').textContent, /事实树摘要已生成/);
    return;
  }
  throw new Error('Unknown contract: ' + name);
}

async function companyTitleCase(name) {
  // 首页标题契约：hero 不再持有静态演示公司名，只显示宿主登记接口确认的名称。
  const hero = html.split('class="hero"')[1].split('id="factsBoard"')[0];
  assert.match(hero, /<div class="h-name" id="homeCompanyName">正在读取公司名称…<\/div>/);
  assert.doesNotMatch(hero, /示例科技/);
  const title = el('homeCompanyName');
  let entities = [], rosterError = name === 'ui-company-title-failure', invalidBody = false;
  if (name === 'ui-company-title') entities = [{...self, entity_name: '<img src=x onerror=alert(1)> 公司'}];
  if (name === 'ui-company-title-unregistered') entities = [
    {origin: 'model', entity_type: 'self', status: 'active', entity_name: 'acc-推断名'},
    {origin: 'declared', entity_type: 'counterparty', status: 'active', entity_name: 'acc-他方'},
    {origin: 'declared', entity_type: 'self', status: 'revoked', entity_name: 'acc-已撤回'},
    {origin: 'declared', entity_type: 'self', status: 'active', entity_name: ''},
    {origin: 'declared', entity_type: 'self', status: 'active', entity_name: '   '},
    {...self, entity_name: 7}];
  fetchImpl = async (url, options) => {
    if (url === '/api/facts/tree') return ok(factsTreeBody('empty'));
    if (url === '/api/entity-roster') {
      if (options.method === 'POST') {
        const posted = JSON.parse(options.body);
        entities = [{...self, entity_name: posted.entity_name}];
        return ok({entity: entities[0]});
      }
      if (rosterError) throw new Error('network down');
      return invalidBody ? ok({company_id: 'acc-shell'}) : ok({entities});
    }
    if (url === '/api/parse-jobs') return ok({jobs: []});
    if (url === '/api/import-guide') return ok({guide: {message: '建议', completeness: {}}});
    throw new Error('Unexpected URL ' + url);
  };
  vm.runInContext(script, context); await flush();
  const reads = () => calls.filter(call => call[0] === '/api/entity-roster' && (call[1].method || 'GET') === 'GET');
  assert.equal(reads().length, 1); // 首次打开即同源读取一次登记接口
  assert.equal(reads()[0][1].credentials, 'same-origin');
  assert.equal(title.innerHTML, ''); // 公司名只经 textContent 更新，不进入 HTML
  if (name === 'ui-company-title') {
    assert.equal(title.textContent, '<img src=x onerror=alert(1)> 公司'); // 恶意字符串按纯文本显示
    return;
  }
  if (name === 'ui-company-title-unregistered') {
    assert.equal(title.textContent, '公司名称未登记'); // 无有效登记记录：origin/entity_type/status/非空名称 任一不符即无效
    el('driveWizardBtn').click(); await flush();
    assert.equal(el('importMask').classList.contains('open'), true);
    assert.equal(el('importRegistration').hidden, false); // 仍走首次登记，而非"修改本公司"
    assert.equal(el('importCompanyLabel').textContent, '首次使用：请登记本公司全称');
    assert.equal(el('importCompanyCurrent').hidden, true);
    assert.equal(reads().length, 2);
    return;
  }
  if (name === 'ui-company-title-failure') {
    assert.equal(title.textContent, '公司名称暂不可用'); // 接口错误
    rosterError = false; invalidBody = true;
    el('driveWizardBtn').click(); await flush(); // 返回结构无效同样按不可用处理
    assert.equal(reads().length, 2);
    assert.equal(title.textContent, '公司名称暂不可用');
    assert.equal(title.innerHTML, '');
    return;
  }
  if (name === 'ui-company-title-register') {
    assert.equal(title.textContent, '公司名称未登记');
    el('driveWizardBtn').click(); await flush();
    assert.equal(el('importMask').classList.contains('open'), true);
    assert.equal(reads().length, 2);
    await register('acc-新登记公司');
    assert.equal(title.textContent, 'acc-新登记公司'); // 登记保存成功后立即同步首页标题
    assert.equal(title.innerHTML, '');
    assert.equal(reads().length, 2); // 保存后不再额外读取
    assert.equal(el('importRegistration').hidden, true);
    return;
  }
  throw new Error('Unknown contract: ' + name);
}

async function main() {
  if (caseName.startsWith('ui-knowledge-')) {await knowledgeCase(caseName); return;}
  if (caseName.startsWith('ui-facts-')) {await factsTreeCase(caseName); return;}
  if (caseName.startsWith('ui-company-title')) {await companyTitleCase(caseName); return;}
  switch (caseName) {
    case 'api': {
      const result = await load({select: payload => payload.rows});
      assert.equal(result.data[0].id, 1); assert.equal(result.demo, false);
      assert.equal(result.source, 'api'); assert.equal(result.label, '');
      assert.equal(calls[0][0], '/api/probe');
      assert.equal(calls[0][1].credentials, 'same-origin');
      for (const value of [0, false]) {
        fetchImpl = async () => ({ok: true, json: async () => value});
        assert.equal((await load()).data, value);
        assert.equal((await load()).demo, false);
      }
      assert.equal((await load({isEmpty: () => true})).demo, true);
      break;
    }
    case 'empty':
      for (const value of [null, undefined, '', [], {}]) {
        fetchImpl = async () => ({ok: true, json: async () => value});
        const result = await load();
        assert.equal(result.demo, true); assert.equal(result.data, SAM.demo.probe);
        assert.equal(result.label, '演示数据');
      }
      assert.equal((await load({select: () => undefined})).demo, true);
      break;
    case 'failure':
      for (const implementation of [
        async () => {throw new Error('network');},
        async () => ({ok: false, status: 503}),
        async () => ({ok: true, json: async () => {throw new Error('invalid JSON');}}),
      ]) {
        fetchImpl = implementation;
        const result = await load();
        assert.equal(result.demo, true); assert.equal(result.data, SAM.demo.probe);
        assert.ok(result.error);
      }
      await assert.rejects(SAM.data.load('unregistered', {url: '/api/probe'}), /Register/);
      break;
    case 'label': {
      const element = new Element();
      SAM.data.mark(element, {label: '演示数据', source: 'demo'});
      assert.equal(element.textContent, '演示数据'); assert.equal(element.dataset.samSource, 'demo');
      SAM.data.mark(element, {label: '', source: 'api'});
      assert.equal(element.textContent, ''); assert.equal(element.dataset.samSource, 'api');
      break;
    }
    case 'readonly':
      for (const method of ['POST', 'put', 'PATCH', 'DELETE']) {
        await assert.rejects(SAM.api.request('/api/probe', {method}), error => error.code === 'sam_readonly');
      }
      assert.equal(calls.length, 0); assert.equal(alerts.length, 4);
      assert.ok(alerts.every(message => message === '这一步请在本地操作'));
      assert.equal((await load()).source, 'api');
      await SAM.api.request('/api/probe', {method: 'HEAD'});
      assert.equal(calls.length, 2);
      break;
    case 'full':
      assert.equal(SAM.ui.allowWrite(), true);
      await SAM.api.request('/api/upload', {method: 'POST', body: 'test'});
      assert.equal(calls[0][1].body, 'test'); assert.equal(alerts.length, 0);
      await assert.rejects(SAM.api.request('https://other.invalid/api/chat'), /same-origin/);
      assert.equal(calls.length, 1);
      break;
    case 'ui-readonly': {
      const input = elements.get('chatInput'); input.value = '保留草稿';
      elements.get('sendBtn').click();
      input.dispatch('keydown', {key: 'Enter'});
      suggestion.click(); skill.click();
      for (const id of ['importWizardBtn', 'importNavBtn', 'driveUploadBtn', 'driveWizardBtn', 'importRefresh', 'importPick']) elements.get(id).click();
      elements.get('importFileInput').files = [{name: 'acc-test.pdf'}];
      elements.get('importFileInput').dispatch('change');
      elements.get('importDrop').dispatch('drop', {dataTransfer: {files: [{name: 'acc-test.pdf'}]}});
      await flush();
      assert.equal(calls.filter(call => call[1].method === 'POST').length, 0); assert.equal(alerts.length, 8);
      assert.ok(alerts.every(message => message === '这一步请在本地操作'));
      assert.equal(input.value, '保留草稿');
      assert.equal(elements.get('chatInner').children.length, 0);
      assert.equal(elements.get('importMask').classList.contains('open'), true);
      assert.equal(elements.get('drive').classList.contains('active'), true);
      break;
    }
    case 'ui-full':
      fetchImpl = async url => ({ok: true, json: async () => url === '/api/chat' ? {message: '测试回复'} :
        url === '/api/import-guide' ? {guide: {message: '测试引导', next_action: '测试下一步', completeness: {}}} :
        url === '/api/entity-roster' ? {entities: [selfNamed]} : url === '/api/parse-jobs' ? {jobs: []} :
        {job_id: 'job', file_hash: 'hash', status: 'queued', original_name: 'acc-test.pdf', size: 8}});
      assert.equal(elements.get('overview').classList.contains('active'), true);
      assert.equal(calls.length, 2); // 首次进入公司主页：事实树一次 + 登记公司名一次
      assert.equal(calls[0][0], '/api/facts/tree'); assert.equal(calls[1][0], '/api/entity-roster');
      assert.equal((calls[0][1].method || 'GET'), 'GET'); assert.equal(calls[0][1].credentials, 'same-origin');
      assert.equal((calls[1][1].method || 'GET'), 'GET'); assert.equal(calls[1][1].credentials, 'same-origin');
      elements.get('chatInput').value = '测试消息'; elements.get('sendBtn').click();
      await flush();
      assert.equal(calls[2][0], '/api/chat'); assert.equal(calls[2][1].method, 'POST');
      assert.equal(elements.get('chatInner').children[1].querySelector('.bubble').textContent, '测试回复');
      elements.get('driveWizardBtn').click(); await flush();
      assert.equal(elements.get('importMask').classList.contains('open'), true);
      assert.equal(elements.get('importGuideText').textContent, '测试引导');
      elements.get('importFileInput').files = [{name: 'acc-test.pdf'}];
      elements.get('importFileInput').dispatch('change'); await flush();
      assert.deepEqual(calls.map(call => call[0]), ['/api/facts/tree', '/api/entity-roster', '/api/chat', '/api/entity-roster', '/api/parse-jobs', '/api/import-guide', '/api/upload', '/api/knowledge']);
      assert.equal(calls.filter(call => call[0] === '/api/upload').length, 1); assert.equal(alerts.length, 0);
      break;
    case 'ui-cabinet-registration':
    case 'ui-cabinet-409':
    case 'ui-cabinet-lifecycle':
    case 'ui-cabinet-actions':
    case 'ui-cabinet-errors':
    case 'ui-cabinet-projection-failed':
    case 'ui-cabinet-readonly':
    case 'ui-cabinet-pause':
    case 'ui-cabinet-registration-error':
    case 'ui-cabinet-upload-error':
    case 'ui-cabinet-upload-race':
    case 'ui-cabinet-races':
      await cabinetCase(caseName); break;
    default: throw new Error('Unknown contract: ' + caseName);
  }
}
main().catch(error => {console.error(error); process.exitCode = 1;});
