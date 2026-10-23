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
const factsNav = new Element(); factsNav.dataset.go = 'facts';
const driveNav = elements.get('importNavBtn'); driveNav.dataset.go = 'drive';
const overviewNav = new Element(); overviewNav.dataset.go = 'overview';
const mktNav = new Element(); mktNav.dataset.go = 'mkt';
const document = {
  body: new Element(), hidden: false, events: {},
  addEventListener(name, handler) {(this.events[name] ||= []).push(handler);},
  createElement: () => new Element(),
  getElementById(id) {if (id.startsWith('source-') && !elements.has(id)) return null; assert.ok(elements.has(id), 'missing element: ' + id); return elements.get(id);},
  querySelectorAll(selector) {
    if (selector === '[data-go]') return [factsNav, driveNav, overviewNav, mktNav];
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
if (!caseName.startsWith('ui-drive-') && !caseName.startsWith('ui-facts-') && !caseName.startsWith('ui-company-title') && !caseName.startsWith('ui-activity-') && !caseName.startsWith('ui-market-')) vm.runInContext(caseName.startsWith('ui-') ? script : foundation, context);
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
    assert.equal(timers.size, 2); // 上传成功后员工动态小卡重新读取到活动任务，加入有限轮询
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
async function driveCase(name) {
  // 文件柜只读投影保留在企业综合主页壳内；旧知识库平铺事实页已淘汰（见 factsLibraryCase 的拒绝断言）。
  assert.match(html, /<div class="screen" id="drive">/);
  assert.match(html, /class="sys-item import-entry" id="importNavBtn" type="button" data-go="drive">/);
  assert.match(html, /drive:"文件柜"/);
  assert.match(script, /routeKnowledgeLocation\(\);\s*}\)\(\);\s*$/);
  // 新版边界：文件柜只是原始文件入口——无“查看映射”按钮、无映射正文/页坐标浏览、无映射状态缺失提示；
  // 映射文件留在后端存储。旧知识库页面与旧导航同样不得作为新版证据。
  assert.match(html, /文件柜 · 原件/);
  assert.doesNotMatch(html, /查看映射|尚无解析映射|尚无可读取的 L2|原文页码/);
  assert.doesNotMatch(script, /\bkL2\b|\bkProse\b/);
  assert.doesNotMatch(html, /data-go="knowledge"|id="knowledge"/);
  let body = name.includes('empty') ? {} : knowledgeFixture;
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
  if (!name.includes('legacy')) {driveNav.click(); await flush();}
  assert.equal(el('drive').classList.contains('active'), true);
  assert.equal(el('overview').classList.contains('active'), false);
  assert.equal(el('crumb').textContent, '文件柜');
  assert.ok(calls.some(call => call[0] === '/api/knowledge' && (call[1].method || 'GET') === 'GET'));
  if (name.includes('empty') || name.includes('failure')) {
    assert.equal(el('driveFiles').innerHTML, '');
    assert.equal(el('driveEmpty').hidden, false);
    assert.equal(el('driveDataSource').textContent, name.includes('failure') ? '读取失败，请刷新重试' : '暂无可用数据'); return;
  }
  assert.doesNotMatch(el('driveFiles').innerHTML, /acc L0 摘要|acc L1 概览/);
  assert.match(el('driveFiles').innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(el('driveFiles').innerHTML, /<script>|<img/);
  // 接口即使返回映射字段，映射正文/坐标/缺失提示也不进入文件柜 DOM；操作列只有原件入口。
  assert.doesNotMatch(el('driveFiles').innerHTML, /acc 原文 Sheet1!B3|第零页|viking:\/\/acc\/l2|尚无解析映射|查看映射/);
  assert.match(el('driveFiles').innerHTML, /data-knowledge-source="acc-hash">查看原件/);
  // 文件柜不再渲染平铺事实/股权链/工作记忆；这些字段即使由接口返回也不进入文件柜 DOM。
  assert.doesNotMatch(el('driveFiles').innerHTML + el('driveTree').innerHTML, /acc 实体|acc 持有人|acc 目标|acc-event/);
  assert.equal(el('driveDataSource').textContent, '');
  if (name.includes('explorer')) {
    assert.match(el('driveStats').textContent, /1 个文件 · 合计 2.0 KB/);
    assert.match(el('driveFiles').innerHTML, /pdf|acc-upload|2026-10-10/);
    assert.equal(el('importMask').classList.contains('open'), false);
    body = JSON.parse(JSON.stringify(knowledgeFixture));
    body.modules.push({code: 'acc-other', name: '<img src=x>', hidden: false});
    body.evidence.files.push({file_hash: 'acc-other', original_name: 'other.xlsx', category: 'acc-other', label: '<img src=x>', size_bytes: 0});
    el('driveRefresh').click(); await flush();
    assert.match(el('driveStats').textContent, /2 个文件/);
    assert.match(el('driveTree').innerHTML, /&lt;img/);
    assert.doesNotMatch(el('driveTree').innerHTML, /<img/);
    const readCount = calls.length;
    const folder = new Element(); folder.dataset.explorerNode = 'category:1';
    el('driveTree').dispatch('click', {target: folder});
    assert.match(el('driveFiles').innerHTML, /other.xlsx/);
    assert.doesNotMatch(el('driveFiles').innerHTML, /alert\(1\)/);
    assert.match(el('driveStats').textContent, /合计 0 B/);
    assert.equal(calls.length, readCount); // navigation uses existing data, no writes/reads
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
    driveNav.click(); await flush();
    assert.doesNotMatch(el('driveFiles').innerHTML, /尚无解析映射|尚无可读取的 L2|映射/); // 映射状态缺失提示已移除
    assert.doesNotMatch(el('driveFiles').innerHTML, /<td>false<\/td>/); // 平铺事实不再进入文件柜
    const link = new Element(); link.dataset.knowledgeSource = 'acc-hash';
    el('drive').dispatch('click', {target: link});
    // 缺字段的原件仍只打开原件卡片：缺失字段直说“未提供”，不展示映射正文或映射缺失提示。
    assert.match(el('knowledgeSourceBody').innerHTML, /企业原件/);
    assert.match(el('knowledgeSourceBody').innerHTML, /未提供/);
    assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /尚无|L2|映射/); return;
  }
  if (name.includes('warnings')) {
    body = {evidence: {files: [{warnings: ['证据暂不可读']}]}, facts: {cap_table: {warnings: ['回放校验失败']}},
      working_memory: {warnings: ['工作记忆暂不可读']}};
    driveNav.click(); await flush();
    assert.match(el('driveFiles').innerHTML, /证据暂不可读/);
    assert.doesNotMatch(el('driveFiles').innerHTML, /回放校验失败|工作记忆暂不可读/); return;
  }
  if (name.includes('race')) {
    const pending = deferred(); fetchImpl = () => pending.promise;
    driveNav.click(); await flush();
    fetchImpl = () => ok({}); driveNav.click(); await flush();
    pending.resolve(ok(knowledgeFixture)); await flush();
    assert.equal(el('driveFiles').innerHTML, '');
    assert.equal(el('driveEmpty').hidden, false); return;
  }
  if (name.includes('cabinet')) {
    el('driveWizardBtn').click(); await flush();
    await action('result');
    assert.equal(el('importMask').classList.contains('open'), false);
    assert.equal(el('drive').classList.contains('active'), true);
    assert.equal(el('knowledgeSourceMask').classList.contains('open'), true);
    el('driveWizardBtn').click(); await flush();await action('source');
    assert.equal(el('importMask').classList.contains('open'), false);
  } else if (!name.includes('legacy-source')) {
    const link = new Element();link.dataset.knowledgeSource = 'acc-hash';link.dataset.page = '2';link.dataset.locator = 'Sheet1!B3';
    el('drive').dispatch('click', {target: link});
  }
  // 出处/向导回查只进入对应原件：弹窗只展示原件信息与下载入口，不暴露映射正文或页坐标。
  assert.equal(el('knowledgeSourceMask').classList.contains('open'), true);
  assert.match(el('knowledgeSourceBody').innerHTML, /企业原件/);
  assert.match(el('knowledgeSourceBody').innerHTML, /&lt;script&gt;/);
  assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /<script>/);
  assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /Sheet1!B3|acc 原文|第零页|viking:\/\/acc\/l2|原文页码|尚无/);
  if (readonly) {
    assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /download/);
    assert.match(el('knowledgeSourceBody').innerHTML, /回本地查看/);
  } else assert.match(el('knowledgeSourceBody').innerHTML, /\/api\/knowledge\/sources\/acc-hash\/original" download>下载原件/);
  if (name.includes('legacy-source')) assert.equal(elements.get('source-acc-hash').classList.contains('knowledge-selected'), true);
  el('knowledgeSourceMask').dispatch('keydown', {key: 'Escape'});
  assert.equal(el('knowledgeSourceMask').classList.contains('open'), false);
  assert.equal(el('knowledgeSourceMask').attributes['aria-hidden'], 'true');
  const missing = new Element();missing.dataset.knowledgeSource = 'foreign-or-missing';
  el('drive').dispatch('click', {target: missing});
  assert.match(el('knowledgeSourceBody').innerHTML, /当前文件柜中没有该原件/);
  assert.doesNotMatch(el('knowledgeSourceBody').innerHTML, /Sheet1!B3|尚无|映射/);
  el('knowledgeSourceMask').dispatch('click');
  assert.equal(el('knowledgeSourceMask').classList.contains('open'), false);
  assert.equal(calls.some(call => (call[1].method || 'GET') !== 'GET' && call[0] !== '/api/import-guide'), false);
}

async function factsTreeCase(name) {
  // 公司看板结构契约：主屏不再展示静态样例公司数字，保留既有导航与入口。
  const overviewHtml = html.split('id="overview">')[1].split('id="facts">')[0];
  assert.match(overviewHtml, /id="factsBoard"/);
  assert.match(overviewHtml, /id="factsState"/);
  assert.match(overviewHtml, /id="factsTreeLink" type="button" data-go="facts"/); // 事实库入口在壳内切页，不跳出外壳
  assert.doesNotMatch(overviewHtml, /href="\/facts"/);
  assert.match(overviewHtml, /id="factsUploadBtn"/);
  assert.match(overviewHtml, /hero-ask" data-go="chat"/);
  assert.doesNotMatch(overviewHtml, /企业健康度|214\.00|净利润|现金跑道|在手订单|股权结构|growth-card|data-gkey/);
  if (name === 'ui-facts-ready') {
    fetchImpl = async url => {
      if (url === '/api/entity-roster') return ok({entities: [{...self, entity_name: 'acc-事实树公司'}]});
      if (url === '/api/parse-jobs') return ok({jobs: []});
      assert.equal(url, '/api/facts/tree'); return ok(factsTreeBody('ready', {brief: '# acc 树摘要\n<img src=x onerror=alert(1)> 摘要第二行'}));
    };
    vm.runInContext(script, context); await flush();
    assert.equal(el('overview').classList.contains('active'), true);
    assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 1); // 首次进入事实树只读取一次
    assert.equal(calls.length, 3); // 另有同源登记公司名与员工动态任务各一次读取
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

async function factsLibraryCase(name) {
  // 事实库入口与页面：同一永久主页壳内切页，复用 webapp/static/js/facts-tree.js 完整只读
  // 渲染（树/枝/叶、正文/索引与脚注来源回查由该渲染器承担，行为契约见 tests/test_facts_tree_ui.py）。
  assert.match(html, /<button class="sys-item" type="button" data-go="facts">/);
  assert.ok(html.indexOf('data-go="facts"') < html.indexOf('id="importNavBtn"'));
  assert.match(html, /facts:"事实库"/);
  assert.doesNotMatch(html, /<a class="sys-item" href="\/facts">/); // 侧栏入口不跳出主页壳
  assert.match(html, /<div class="screen" id="facts">/);
  const factsHtml = html.split('<div class="screen" id="facts">')[1].split('id="knowledgeSourceMask"')[0];
  for (const marker of ['id="facts-tree"', 'id="facts-tree-status"', 'id="facts-tree-nav"', 'id="facts-tree-detail"'])
    assert.ok(factsHtml.includes(marker), marker);
  assert.match(html, /<script src="\/static\/js\/facts-tree\.js"><\/script>/);
  // 拒绝旧知识库作为新版事实库证据：旧导航、旧页面与按来源平铺的旧事实/股权链/工作记忆区块均已移除。
  assert.doesNotMatch(html, /data-go="knowledge"/);
  assert.doesNotMatch(html, /id="knowledge"/);
  for (const legacy of ['id="knowledgeFacts"', 'id="knowledgeEquity"', 'id="knowledgeRuntime"',
    'id="confirmed-facts"', 'id="working-memory"', 'id="knowledgeTree"', 'id="knowledgeSelect"'])
    assert.ok(!html.includes(legacy), legacy);
  assert.doesNotMatch(script, /renderExplorerTree|knowledgeSelection|knowledgeFacts|knowledgeEquity|knowledgeRuntime/);
  fetchImpl = async url => {
    if (url === '/api/facts/tree') return ok(factsTreeBody('ready', {brief: 'acc 摘要'}));
    if (url === '/api/entity-roster') return ok({entities: [selfNamed]});
    if (url === '/api/knowledge') return ok({});
    throw new Error('Unexpected URL ' + url);
  };
  if (name === 'ui-facts-library-legacy') {
    // 旧 /knowledge 平铺页链接落到壳内事实库，不再打开旧页面。
    window.location = {pathname: '/knowledge', hash: '', search: ''};
  }
  vm.runInContext(script, context); await flush();
  if (name === 'ui-facts-library-legacy') {
    assert.equal(el('facts').classList.contains('active'), true);
    assert.equal(el('overview').classList.contains('active'), false);
    assert.equal(el('crumb').textContent, '事实库');
    assert.equal(calls.some(call => call[0] === '/api/knowledge'), false);
    return;
  }
  // 默认首页仍是公司看板；首次进入只读事实树状态与登记公司名，不读旧平铺事实。
  assert.match(html, /<div class="screen active" id="overview">/);
  assert.match(html, /<div class="crumb" id="crumb">企业综合<\/div>/);
  assert.equal(el('overview').classList.contains('active'), true);
  assert.equal(el('facts').classList.contains('active'), false);
  assert.equal(calls.filter(call => call[0] === '/api/facts/tree').length, 1);
  assert.equal(calls.some(call => call[0] === '/api/knowledge'), false);
  factsNav.click(); await flush();
  assert.equal(el('facts').classList.contains('active'), true);
  assert.equal(el('overview').classList.contains('active'), false);
  assert.equal(el('crumb').textContent, '事实库');
  assert.equal(el('topbar').style.display, 'flex'); // 顶栏保留，仍为同一主页壳
  assert.equal(calls.some(call => call[0] === '/api/knowledge'), false); // 事实库切页不读取旧平铺事实接口
  driveNav.click(); await flush();
  assert.equal(el('drive').classList.contains('active'), true);
  assert.equal(el('facts').classList.contains('active'), false);
  assert.equal(el('crumb').textContent, '文件柜');
  assert.equal(calls.filter(call => call[0] === '/api/knowledge').length, 1); // 文件柜保留既有只读投影
  factsNav.click(); await flush();
  assert.equal(el('facts').classList.contains('active'), true);
  assert.equal(el('drive').classList.contains('active'), false);
  assert.equal(el('crumb').textContent, '事实库');
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

async function activityCase(name) {
  // 员工动态小卡结构契约：卡片嵌在公司综合主页，只读真实持久任务状态，静态壳不含预置任务或样例进度。
  const overviewHtml = html.split('id="overview">')[1].split('id="facts">')[0];
  assert.match(overviewHtml, /<div class="card" id="activityBoard">/);
  assert.match(overviewHtml, /<span class="sub" id="activitySub">正在读取任务状态…<\/span>/);
  assert.match(overviewHtml, /<button class="drive-btn" id="activityRetryBtn" type="button" hidden>重试<\/button>/);
  assert.match(overviewHtml, /<button class="drive-btn" id="activityDetailBtn" type="button">任务详情<\/button>/);
  assert.match(overviewHtml, /<div class="activity-list" id="activityList" aria-live="polite"><\/div>/);
  assert.ok(overviewHtml.indexOf('id="activityBoard"') > overviewHtml.indexOf('id="factsBoard"'));
  const activityHtml = overviewHtml.split('id="activityBoard">')[1].split('id="marketNotice"')[0];
  assert.doesNotMatch(activityHtml, /排队中|解析中|已完成|需处理/); // 壳内不预置任何任务状态样例
  const xssName = '<img src=x onerror=alert(1)>.pdf';
  const listCalls = () => calls.filter(call => call[0] === '/api/parse-jobs');
  const assertNoCompanyParam = () => {
    assert.ok(calls.every(call => !String(call[0]).includes('company_id')), 'no request may carry company_id');
    assert.ok(listCalls().every(call => ((call[1] || {}).method || 'GET') === 'GET'), 'activity card reads must be GET');
  };
  let jobs = [];
  if (name === 'ui-activity-active') jobs = [
    {job_id: 'acc-job-1', file_hash: 'acc-h1', original_name: xssName, status: 'queued', stage: 'queued'},
    {job_id: 'acc-job-2', file_hash: 'acc-h2', original_name: 'acc-年报.pdf', status: 'parsing', stage: 'parsing', progress_current: 2, progress_total: 5}];
  if (name === 'ui-activity-terminal') jobs = [
    {job_id: 'acc-j1', file_hash: 'acc-h1', original_name: 'acc-失败.pdf', status: 'failed', stage: 'failed', message: '<后端解析错误>', finished_at: '2026-10-22T09:00:00+00:00'},
    {job_id: 'acc-j2', file_hash: 'acc-h2', original_name: 'acc-待确认.pdf', status: 'needs_human', stage: 'needs_human'},
    {job_id: 'acc-j3', file_hash: 'acc-h3', original_name: 'acc-部分.pdf', status: 'partial', stage: 'partial'},
    {job_id: 'acc-j4', file_hash: 'acc-h4', original_name: 'acc-完成.pdf', status: 'done', stage: 'done', finished_at: '2026-10-22T08:00:00+00:00'},
    {job_id: 'acc-j5', file_hash: 'acc-h5', original_name: 'acc-取消.pdf', status: 'canceled', stage: 'canceled'}];
  if (name === 'ui-activity-recent') jobs = [
    {job_id: 'acc-j1', file_hash: 'acc-h1', status: 'parsing'},
    {job_id: 'acc-j2', file_hash: 'acc-h2', original_name: 'acc-完成.pdf', status: 'done'},
    {job_id: 'acc-j3', file_hash: 'acc-h3', original_name: 'acc-取消.pdf', status: 'canceled'}];
  if (name === 'ui-activity-detail') jobs = [
    {job_id: 'acc-j1', file_hash: 'acc-h1', original_name: 'acc-完成.pdf', status: 'done', stage: 'done'},
    {job_id: 'acc-j2', file_hash: 'acc-h2', original_name: 'acc-取消.pdf', status: 'canceled', stage: 'canceled'},
    {job_id: 'acc-j3', file_hash: 'acc-h3', original_name: 'acc-失败.pdf', status: 'failed', stage: 'failed', message: 'acc-失败原因'}];
  if (name === 'ui-activity-limit') jobs = [
    {job_id: 'acc-j1', file_hash: 'acc-h1', original_name: 'acc-排队.pdf', status: 'queued', stage: 'queued'},
    {job_id: 'acc-j2', file_hash: 'acc-h2', original_name: 'acc-解析.pdf', status: 'parsing', stage: 'parsing', progress_current: 1, progress_total: 4},
    {job_id: 'acc-j3', file_hash: 'acc-h3', original_name: 'acc-失败.pdf', status: 'failed', stage: 'failed'},
    {job_id: 'acc-j4', file_hash: 'acc-h4', original_name: 'acc-部分.pdf', status: 'partial', stage: 'partial'},
    {job_id: 'acc-j5', file_hash: 'acc-h5', original_name: 'acc-完成.pdf', status: 'done', stage: 'done'},
    {job_id: 'acc-j6', file_hash: 'acc-h6', original_name: 'acc-取消.pdf', status: 'canceled', stage: 'canceled'}];
  if (name === 'ui-activity-done-order') jobs = [
    {job_id: 'acc-j1', file_hash: 'acc-h1', original_name: 'acc-处理中.pdf', status: 'parsing', stage: 'parsing'},
    {job_id: 'acc-j2', file_hash: 'acc-h2', original_name: 'acc-待处理.pdf', status: 'failed', stage: 'failed'},
    {job_id: 'acc-j3', file_hash: 'acc-h3', original_name: 'acc-较早完成.pdf', status: 'done', stage: 'done', finished_at: '2026-10-22T06:00:00+00:00'},
    {job_id: 'acc-j4', file_hash: 'acc-h4', original_name: 'acc-较晚完成.pdf', status: 'done', stage: 'done', finished_at: '2026-10-22T07:30:00+00:00'},
    {job_id: 'acc-j5', file_hash: 'acc-h5', original_name: 'acc-取消.pdf', status: 'canceled', stage: 'canceled'}];
  if (name === 'ui-activity-readonly') jobs = [
    {job_id: 'acc-j1', file_hash: 'acc-h1', original_name: 'acc-只读.pdf', status: 'queued', stage: 'queued'}];
  let failMode = name === 'ui-activity-error' ? 'invalid' : null;
  fetchImpl = async url => {
    if (url === '/api/parse-jobs') {
      if (failMode === 'invalid') return ok({company_id: 'acc-shell'});
      if (failMode === 'down') throw new Error('network down');
      return ok({jobs});
    }
    if (url === '/api/entity-roster') return ok({entities: [selfNamed]});
    if (url === '/api/facts/tree') return ok(factsTreeBody('empty'));
    if (url === '/api/knowledge') return ok({});
    if (url === '/api/import-guide') return ok({guide: {message: '建议', completeness: {}}});
    throw new Error('Unexpected URL ' + url);
  };
  vm.runInContext(script, context); await flush();
  assert.equal(el('overview').classList.contains('active'), true);
  if (name === 'ui-activity-empty') {
    assert.equal(el('activitySub').textContent, '当前没有上传任务');
    assert.match(el('activityList').innerHTML, /暂无处理中的资料任务/);
    assert.equal(el('activityRetryBtn').hidden, true);
    assert.equal(timers.size, 0); // 没有活动任务时不轮询
    assertNoCompanyParam();
    return;
  }
  if (name === 'ui-activity-error') {
    assert.equal(el('activitySub').textContent, '任务状态暂不可用');
    assert.match(el('activityList').innerHTML, /任务状态返回无效/);
    assert.match(el('activityList').innerHTML, /未回退到演示数据/);
    assert.equal(el('activityRetryBtn').hidden, false);
    failMode = 'down'; el('activityRetryBtn').click(); await flush();
    assert.match(el('activityList').innerHTML, /network down/);
    assert.equal(el('activityRetryBtn').hidden, false);
    failMode = null; jobs = [{job_id: 'acc-j1', file_hash: 'acc-h1', original_name: 'acc-任务.pdf', status: 'done'}];
    el('activityRetryBtn').click(); await flush();
    assert.equal(el('activitySub').textContent, '1 项最近记录');
    assert.match(el('activityList').innerHTML, /acc-任务\.pdf/);
    assert.match(el('activityList').innerHTML, /已完成/);
    assert.equal(el('activityRetryBtn').hidden, true);
    assert.equal(listCalls().length, 3);
    assertNoCompanyParam();
    return;
  }
  if (name === 'ui-activity-active') {
    assert.equal(el('activitySub').textContent, '2 项处理中');
    assert.match(el('activityList').innerHTML, /排队中/);
    assert.match(el('activityList').innerHTML, /解析中 · parsing · 40%/);
    assert.match(el('activityList').innerHTML, /&lt;img/);
    assert.doesNotMatch(el('activityList').innerHTML, /<img/);
    assert.equal(listCalls().length, 1);
    assertNoCompanyParam();
    assert.equal(timers.size, 1); // 存在活动任务时在主页可见期间有限轮询
    await tick();
    assert.equal(listCalls().length, 2);
    assert.equal(timers.size, 1);
    // 离开公司综合主页：停止轮询；期间恢复可见也不读取
    driveNav.click(); await flush();
    assert.equal(el('overview').classList.contains('active'), false);
    assert.equal(timers.size, 0);
    document.hidden = true; document.events.visibilitychange.forEach(fn => fn());
    document.hidden = false; document.events.visibilitychange.forEach(fn => fn()); await flush();
    assert.equal(listCalls().length, 2);
    // 切回公司综合主页：重新读取持久状态并恢复轮询
    overviewNav.click(); await flush();
    assert.equal(el('overview').classList.contains('active'), true);
    assert.equal(listCalls().length, 3);
    assert.equal(timers.size, 1);
    // 页面隐藏：停止轮询；重新可见：重新读取持久状态
    document.hidden = true; document.events.visibilitychange.forEach(fn => fn());
    assert.equal(timers.size, 0);
    document.hidden = false; document.events.visibilitychange.forEach(fn => fn()); await flush();
    assert.equal(listCalls().length, 4);
    assert.equal(timers.size, 1);
    // 全部进入终态后停止轮询，最近记录如实展示
    jobs = jobs.map(job => ({...job, status: 'done', stage: 'done'}));
    await tick();
    assert.equal(el('activitySub').textContent, '2 项最近记录');
    assert.match(el('activityList').innerHTML, /已完成/);
    assert.equal(timers.size, 0);
    return;
  }
  if (name === 'ui-activity-terminal') {
    assert.equal(el('activitySub').textContent, '3 项需处理 · 2 项最近记录');
    assert.match(el('activityList').innerHTML, /失败 · 需处理/);
    assert.match(el('activityList').innerHTML, /待人工确认 · 需处理/);
    assert.match(el('activityList').innerHTML, /&lt;后端解析错误&gt;/);
    assert.doesNotMatch(el('activityList').innerHTML, /<后端解析错误>/);
    // 存在多条需处理任务时，真实最近完成项仍保底显示一条，不被全部挤出代表行
    assert.match(el('activityList').innerHTML, /acc-完成\.pdf<\/span><span class="status">已完成 · /);
    assert.equal((el('activityList').innerHTML.match(/class="activity-item /g) || []).length, 3);
    assert.match(el('activityList').innerHTML, /另有 2 项任务/); // 其余需处理/已取消计入摘要但超出代表项数量
    assert.doesNotMatch(el('activityList').innerHTML, /acc-部分\.pdf|acc-取消\.pdf/);
    assert.equal(timers.size, 0); // 无活动任务不轮询
    assertNoCompanyParam();
    return;
  }
  if (name === 'ui-activity-recent') {
    assert.equal(el('activitySub').textContent, '1 项处理中 · 2 项最近记录');
    // 缺字段不补造：无文件名、无阶段、无进度、无完成时间
    assert.match(el('activityList').innerHTML, /未提供文件名<\/span><span class="status">解析中<\/span>/);
    assert.match(el('activityList').innerHTML, /acc-完成\.pdf<\/span><span class="status">已完成<\/span>/);
    assert.match(el('activityList').innerHTML, /acc-取消\.pdf<\/span><span class="status">已取消<\/span>/);
    assert.doesNotMatch(el('activityList').innerHTML, /NaN|undefined|%/);
    assert.equal(timers.size, 1); // 仍有活动任务，继续有限轮询
    await tick();
    assert.doesNotMatch(el('activityList').innerHTML, /NaN|undefined|%/);
    assertNoCompanyParam();
    return;
  }
  if (name === 'ui-activity-limit') {
    assert.equal(el('activitySub').textContent, '2 项处理中 · 2 项需处理 · 2 项最近记录');
    // 行数有限时三类代表项各保留一条：活跃、需处理与真实最近完成
    assert.equal((el('activityList').innerHTML.match(/class="activity-item /g) || []).length, 3);
    assert.match(el('activityList').innerHTML, /acc-排队\.pdf/);
    assert.match(el('activityList').innerHTML, /acc-失败\.pdf/);
    assert.match(el('activityList').innerHTML, /acc-完成\.pdf<\/span><span class="status">已完成<\/span>/);
    assert.doesNotMatch(el('activityList').innerHTML, /acc-解析\.pdf|acc-部分\.pdf|acc-取消\.pdf/);
    assert.match(el('activityList').innerHTML, /另有 3 项任务/); // 超出代表行的数量准确提示
    assertNoCompanyParam();
    return;
  }
  if (name === 'ui-activity-done-order') {
    assert.equal(el('activitySub').textContent, '1 项处理中 · 1 项需处理 · 3 项最近记录');
    // 两条完成记录都有可解析 finished_at 时，最近完成代表项取真实最新一条而非API顺序
    assert.match(el('activityList').innerHTML, /acc-较晚完成\.pdf/);
    assert.doesNotMatch(el('activityList').innerHTML, /acc-较早完成\.pdf|acc-取消\.pdf/);
    assert.match(el('activityList').innerHTML, /另有 2 项任务/);
    assert.equal(timers.size, 1); // 仍有活动任务，继续有限轮询
    // 完成记录都缺有效时间时不造时间，沿用API顺序取第一条
    jobs = jobs.map(job => job.status === 'done' ? {...job, finished_at: undefined} : job);
    await tick();
    assert.match(el('activityList').innerHTML, /acc-较早完成\.pdf<\/span><span class="status">已完成<\/span>/);
    assert.doesNotMatch(el('activityList').innerHTML, /acc-较晚完成\.pdf|NaN|undefined/);
    assert.equal(timers.size, 1);
    assertNoCompanyParam();
    return;
  }
  if (name === 'ui-activity-detail' || name === 'ui-activity-readonly') {
    assert.match(el('activityList').innerHTML, name === 'ui-activity-detail' ? /另有 0 项任务|acc-完成\.pdf/ : /acc-只读\.pdf/);
    el('activityDetailBtn').click(); await flush();
    // 详情入口复用既有上传/文件柜任务弹窗，不新建任务面板
    assert.equal(el('importMask').classList.contains('open'), true);
    assert.equal(el('overview').classList.contains('active'), true);
    assert.match(el('importFiles').innerHTML, /acc-(取消|只读)\.pdf/);
    if (name === 'ui-activity-detail') {
      assert.match(el('importFiles').innerHTML, /已取消/);
      assert.match(el('importFiles').innerHTML, /失败/);
      assert.match(el('importFiles').innerHTML, /acc-失败原因/);
    } else {
      assert.match(el('importFiles').innerHTML, /排队中/);
      assert.equal(timers.size > 0, true); // 只读模式下活动任务仍可读轮询
      await tick();
    }
    // 小卡自身只 GET /api/parse-jobs 且不带 company_id；向导既有行为（如完整模式刷新引导）不在小卡契约内
    assertNoCompanyParam();
    el('importClose').click(); await flush();
    assert.equal(el('importMask').classList.contains('open'), false);
    return;
  }
  throw new Error('Unknown contract: ' + name);
}

async function marketCase(name) {
  // 市场动态未接入契约：默认首页与市场页只展示未接入状态，不残留本卡涉及的静态内容、
  // 设置与详情入口；默认看板结构（事实树卡/员工动态卡/上传入口）与市场导航/切页保留。
  const overviewHtml = html.split('id="overview">')[1].split('id="facts">')[0];
  assert.match(overviewHtml, /<div class="card" id="marketNotice">/);
  assert.match(overviewHtml, /市场动态策略尚未接入/);
  assert.match(overviewHtml, /不展示新闻、政策、研报或市场数字/);
  assert.ok(overviewHtml.indexOf('id="marketNotice"') > overviewHtml.indexOf('id="activityBoard"'));
  assert.doesNotMatch(overviewHtml, /news-card|newsTrack|newsViewport|newsFilters|newsSetBtn/);
  assert.doesNotMatch(overviewHtml, /按关注来源实时汇总|每日按所选来源|点击查看原文/);
  const mktHtml = html.split('<div class="screen" id="mkt">')[1].split('<div class="screen deferred-screen" id="fin">')[0];
  assert.match(mktHtml, /<div class="card" id="mktNotice">/);
  assert.match(mktHtml, /市场动态策略尚未接入/);
  assert.match(mktHtml, /不展示新闻、政策、研报或市场数字示例/);
  assert.doesNotMatch(mktHtml, /insight|mkt-tab|mkt-pane|data-pane|src-line|src-tag/);
  assert.doesNotMatch(mktHtml, /国产化率|竞争对手对比|竞争壁垒|近期重点政策|行业研报观点|行业动态|技术前沿/);
  assert.doesNotMatch(mktHtml, /中金|中信|新华社|财联社|Nature Materials|arXiv|工信部|企查查/);
  for (const kept of ['id="factsBoard"', 'id="activityBoard"', 'id="importWizardBtn"', 'hero-ask" data-go="chat"'])
    assert.ok(html.includes(kept), kept); // 默认看板结构保留
  // 市场导航与壳内切页入口保留：侧栏三个子项仍指向 mkt 屏，面包屑标题仍在路由表
  assert.equal((html.match(/data-go="mkt"/g) || []).length, 3);
  assert.match(script, /mkt:"市场"/);
  // 消息关注设置/详情弹窗与其脚本一并移除，不残留可打开入口
  for (const gone of ['id="newsSetMask"', 'id="newsDetailMask"', 'id="newsSetBtn"', 'id="ndMeta"', 'nset-mask', 'nd-mask'])
    assert.ok(!html.includes(gone), gone);
  assert.doesNotMatch(script, /var NEWS=|renderNews|renderFilters|newsSetSave|ndAskBtn|openDetail|TYPE_LABEL|LV_LABEL|mkt-tab/);
  fetchImpl = async url => {
    if (url === '/api/entity-roster') return ok({entities: [selfNamed]});
    if (url === '/api/parse-jobs') return ok({jobs: []});
    if (url === '/api/facts/tree') return ok(factsTreeBody('empty'));
    throw new Error('Unexpected URL ' + url);
  };
  vm.runInContext(script, context); await flush(); // 移除旧 DOM 后初始化无缺失元素错误
  assert.equal(el('overview').classList.contains('active'), true);
  assert.equal(el('activitySub').textContent, '当前没有上传任务'); // 员工动态卡读取正常完成
  assert.deepEqual([...new Set(calls.map(call => call[0]))].sort(),
    ['/api/entity-roster', '/api/facts/tree', '/api/parse-jobs']); // 看板只有既有读取，无任何市场数据请求
  mktNav.click(); await flush();
  assert.equal(el('mkt').classList.contains('active'), true);
  assert.equal(el('overview').classList.contains('active'), false);
  assert.equal(el('crumb').textContent, '市场');
  overviewNav.click(); await flush();
  assert.equal(el('overview').classList.contains('active'), true);
  assert.equal(el('mkt').classList.contains('active'), false);
}

async function main() {
  if (caseName.startsWith('ui-drive-')) {await driveCase(caseName); return;}
  if (caseName.startsWith('ui-facts-library')) {await factsLibraryCase(caseName); return;}
  if (caseName.startsWith('ui-facts-')) {await factsTreeCase(caseName); return;}
  if (caseName.startsWith('ui-company-title')) {await companyTitleCase(caseName); return;}
  if (caseName.startsWith('ui-activity-')) {await activityCase(caseName); return;}
  if (caseName.startsWith('ui-market-')) {await marketCase(caseName); return;}
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
      assert.equal(calls.length, 3); // 首次进入公司主页：事实树一次 + 登记公司名一次 + 员工动态任务一次
      assert.equal(calls[0][0], '/api/facts/tree'); assert.equal(calls[1][0], '/api/entity-roster'); assert.equal(calls[2][0], '/api/parse-jobs');
      assert.equal((calls[0][1].method || 'GET'), 'GET'); assert.equal(calls[0][1].credentials, 'same-origin');
      assert.equal((calls[1][1].method || 'GET'), 'GET'); assert.equal(calls[1][1].credentials, 'same-origin');
      assert.equal((calls[2][1].method || 'GET'), 'GET'); assert.equal(calls[2][1].credentials, 'same-origin');
      elements.get('chatInput').value = '测试消息'; elements.get('sendBtn').click();
      await flush();
      assert.equal(calls[3][0], '/api/chat'); assert.equal(calls[3][1].method, 'POST');
      assert.equal(elements.get('chatInner').children[1].querySelector('.bubble').textContent, '测试回复');
      elements.get('driveWizardBtn').click(); await flush();
      assert.equal(elements.get('importMask').classList.contains('open'), true);
      assert.equal(elements.get('importGuideText').textContent, '测试引导');
      elements.get('importFileInput').files = [{name: 'acc-test.pdf'}];
      elements.get('importFileInput').dispatch('change'); await flush();
      assert.deepEqual(calls.map(call => call[0]), ['/api/facts/tree', '/api/entity-roster', '/api/parse-jobs', '/api/chat', '/api/entity-roster', '/api/parse-jobs', '/api/import-guide', '/api/upload', '/api/knowledge', '/api/parse-jobs']);
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
