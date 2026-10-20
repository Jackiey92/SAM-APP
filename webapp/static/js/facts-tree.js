/* F-004 P03: read-only FactsTree browser for facts.html.
   F-004 API-002: footnote source lookup — leaf Markdown footnote definition
   lines ([^ref]: ...) become a read-only "来源回查" list; clicking a ref asks
   GET /api/facts/tree/source?leaf_code=<code>&ref=<exact ref> and renders
   original name/location/line range/availability into that item only.
   All server content is rendered via textContent/DOM nodes only — never
   innerHTML — so Markdown bodies stay plain text. Read-only: no editing,
   no company selection; the host injects the company scope server-side. */
(() => {
  const root = document.getElementById('facts-tree');
  if (!root) return;
  const statusEl = document.getElementById('facts-tree-status');
  const navEl = document.getElementById('facts-tree-nav');
  const detailEl = document.getElementById('facts-tree-detail');

  const STATE_LABELS = {
    ready: '已生成',
    empty: '暂无内容',
    not_generated: '未生成',
    unbound: '未绑定模板',
    active: '已发布',
    archived: '已归档',
  };
  const stateLabel = state => (STATE_LABELS[state] !== undefined ? STATE_LABELS[state] : String(state));

  const node = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  };

  const setStatus = (text, tone) => {
    statusEl.textContent = text;
    statusEl.dataset.tone = tone || '';
    statusEl.classList.toggle('facts-tree-error', tone === 'error');
  };

  const get = async url => {
    let response;
    try {
      response = await fetch(url, { headers: { Accept: 'application/json' } });
    } catch (error) {
      throw { kind: 'network' };
    }
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw { kind: 'http', status: response.status, error: body.error, message: body.message };
    }
    return body;
  };

  const request = (which, code) => get(which === 'tree'
    ? '/api/facts/tree'
    : `/api/facts/tree?node=${which}&code=${encodeURIComponent(code)}`);

  const requestSource = (leafCode, ref) => get(
    `/api/facts/tree/source?leaf_code=${encodeURIComponent(leafCode)}`
    + `&ref=${encodeURIComponent(ref)}`);

  const failureText = failure => {
    if (failure.kind === 'network') return '网络异常，事实树读取失败，请稍后重试。';
    if (failure.error === 'facts_tree_disabled') return '事实树功能当前已关闭。';
    if (failure.error === 'facts_tree_unavailable') return '暂无同步数据。';
    const reason = failure.error || `HTTP ${failure.status}`;
    return `事实树读取失败（${reason}），请稍后重试。`;
  };

  const showFailure = failure => {
    setStatus(failureText(failure), 'error');
    navEl.replaceChildren();
    detailEl.replaceChildren(node('p', 'facts-tree-empty', failureText(failure)));
  };

  const textBlock = (title, text, preClass) => {
    const section = node('section', 'facts-tree-section');
    section.append(node('h3', 'facts-tree-section-title', title));
    section.append(node('pre', preClass || 'facts-tree-text', text));
    return section;
  };

  // Standard Markdown footnote definition lines, e.g. "[^a1]: 原件 → 坐标 …".
  // UI extraction only picks candidates to display; the server re-parses and
  // authorizes each ref against its own index.
  const FOOTNOTE_DEF = /^\[\^([^\]\s]+)\]:[ \t]*(.*)$/gm;
  const DOWNLOAD_PREFIX = '/api/knowledge/sources/';

  const extractSources = content => {
    const found = [];
    const seen = new Set();
    if (typeof content !== 'string') return found;
    for (const match of content.matchAll(FOOTNOTE_DEF)) {
      if (seen.has(match[1])) continue;
      seen.add(match[1]);
      found.push({ ref: match[1], text: match[2] });
    }
    return found;
  };

  // Only same-origin document-cabinet paths under a single leading slash are
  // linkable: no protocol-relative or external URLs, no backslashes, no
  // encoded or literal path traversal.
  const safeDownloadHref = url => {
    if (typeof url !== 'string') return null;
    if (!url.startsWith(DOWNLOAD_PREFIX)) return null;
    if (url.includes('\\') || url.includes('..') || url.includes('%')) return null;
    return url;
  };

  const sourceFailureText = failure => {
    if (failure.kind === 'network') return '网络异常，来源回查失败，请稍后重试。';
    const reason = failure.error || `HTTP ${failure.status}`;
    return `来源回查失败（${reason}），请稍后重试。`;
  };

  const renderSourceResult = (container, data) => {
    const source = (data && data.source) || {};
    const original = (data && data.original) || {};
    const rows = [
      ['原件', source.original_name || '（未记录原件名）'],
      ['原文坐标', source.locator || '（未记录坐标）'],
    ];
    // API-002: lines is [start_line, end_line] or null when the citation
    // carries no line range.
    if (Array.isArray(source.lines) && source.lines.length === 2
        && source.lines[0] !== undefined && source.lines[0] !== null
        && source.lines[1] !== undefined && source.lines[1] !== null) {
      rows.push(['行范围', `L${source.lines[0]}–L${source.lines[1]}`]);
    }
    const list = node('dl', 'facts-tree-source-detail');
    for (const [label, value] of rows) {
      list.append(node('dt', '', label));
      list.append(node('dd', '', value));
    }
    const parts = [list];
    if (original.state === 'available') {
      const href = safeDownloadHref(original.download_url);
      if (href) {
        const link = node('a', 'facts-tree-source-download', '打开原件');
        link.setAttribute('href', href);
        link.setAttribute('target', '_blank');
        link.setAttribute('rel', 'noopener');
        parts.push(link);
      } else {
        parts.push(node('p', 'facts-tree-source-state',
          '原件可用，但未提供有效的同源下载地址，无法打开原件。'));
      }
    } else {
      const reason = original.reason ? `：${original.reason}` : '';
      parts.push(node('p', 'facts-tree-source-state', `原件当前不可访问${reason}`));
    }
    container.replaceChildren(...parts);
  };

  const lookupSource = async (container, leafCode, ref) => {
    container.replaceChildren(node('p', 'facts-tree-source-state', '正在回查来源…'));
    try {
      renderSourceResult(container, await requestSource(leafCode, ref));
    } catch (failure) {
      const text = failure && failure.kind
        ? sourceFailureText(failure)
        : '来源回查失败，请稍后重试。';
      container.replaceChildren(node('p', 'facts-tree-source-state facts-tree-error', text));
    }
  };

  const sourcesSection = (leafCode, content) => {
    const sources = extractSources(content);
    if (!sources.length) return null;
    const section = node('section', 'facts-tree-section facts-tree-sources');
    section.append(node('h3', 'facts-tree-section-title', '来源回查'));
    const list = node('ul', 'facts-tree-source-list');
    for (const { ref, text } of sources) {
      const item = node('li', 'facts-tree-source-item');
      const button = node('button', 'facts-tree-source-ref', `[^${ref}]`);
      button.type = 'button';
      button.dataset.ref = ref;
      const result = node('div', 'facts-tree-source-result');
      button.addEventListener('click', () => { void lookupSource(result, leafCode, ref); });
      item.append(button);
      item.append(node('span', 'facts-tree-source-text', text));
      item.append(result);
      list.append(item);
    }
    section.append(list);
    return section;
  };

  const bindingLine = binding => {
    if (!binding) return null;
    const line = node('p', 'facts-tree-binding');
    line.textContent = `模板包 ${binding.template_pack} · 版本 ${binding.pack_version}`
      + ` · 绑定修订 ${binding.binding_revision}`;
    return line;
  };

  const markSelected = code => {
    navEl.querySelectorAll('button[data-code]').forEach(button => {
      const selected = button.dataset.code === code;
      button.classList.toggle('active', selected);
      if (selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
  };

  const showLeaf = data => {
    setStatus(`叶子 ${data.code} · ${stateLabel(data.state)}`, data.state === 'ready' ? 'ok' : '');
    const parts = [];
    const leaf = data.leaf || {};
    const head = node('div', 'facts-tree-leaf-head');
    head.append(node('h2', 'facts-tree-detail-title', leaf.title || data.code));
    const meta = node('p', 'facts-tree-meta');
    meta.textContent = `编号 ${data.code} · 所属枝 ${leaf.trunk || '-'} · 形态 ${leaf.shape || '-'}`
      + ` · 范围 ${leaf.scope || '-'}`;
    head.append(meta);
    if (data.state !== 'ready') {
      head.append(node('p', 'facts-tree-empty', `该叶子${stateLabel(data.state)}，暂无可读正文。`));
      parts.push(head);
    } else {
      const version = node('p', 'facts-tree-meta');
      version.textContent = `版本 v${data.version} · 状态 ${data.leaf_state || '-'}`;
      head.append(version);
      parts.push(head);
      const index = data.index || {};
      const rows = [
        ['最新期间', index.latest_period],
        ['来源文件数', index.source_file_count],
        ['创建时间', index.created_at],
        ['更新时间', index.updated_at],
      ].filter(([, value]) => value !== undefined && value !== null && value !== '');
      if (Array.isArray(index.sources) && index.sources.length) {
        rows.splice(1, 0, ['来源', index.sources.join('、')]);
      }
      if (rows.length) {
        const section = node('section', 'facts-tree-section');
        const list = node('dl', 'facts-tree-index');
        for (const [label, value] of rows) {
          list.append(node('dt', '', label));
          list.append(node('dd', '', value));
        }
        section.append(node('h3', 'facts-tree-section-title', '索引'));
        section.append(list);
        parts.push(section);
      }
      parts.push(textBlock('正文', data.content || '', 'facts-tree-leaf-content'));
      const sources = sourcesSection(data.code, data.content);
      if (sources) parts.push(sources);
    }
    detailEl.replaceChildren(...parts.filter(Boolean));
  };

  const showTrunk = data => {
    setStatus(`树枝 ${data.code} · ${stateLabel(data.state)}`, data.state === 'ready' ? 'ok' : '');
    markSelected(data.code);
    const parts = [];
    parts.push(node('h2', 'facts-tree-detail-title', `树枝 ${data.code}`));
    const bound = bindingLine(data.binding);
    if (bound) parts.push(bound);
    if (data.state !== 'ready') {
      parts.push(node('p', 'facts-tree-empty', `该树枝${stateLabel(data.state)}。`));
    }
    if (data.brief) parts.push(textBlock('简报', data.brief));
    if (data.guide) parts.push(textBlock('导览', data.guide));
    const leaves = Array.isArray(data.leaves) ? data.leaves : [];
    if (leaves.length) {
      const section = node('section', 'facts-tree-section');
      section.append(node('h3', 'facts-tree-section-title', '叶目录'));
      const list = node('ul', 'facts-tree-leaf-list');
      for (const leaf of leaves) {
        const item = node('li', 'facts-tree-leaf-item');
        const button = node('button', 'facts-tree-node facts-tree-leaf facts-tree-leaf-link');
        button.type = 'button';
        button.dataset.code = leaf.code;
        button.textContent = `${leaf.code} · ${leaf.title || ''}`
          + `（${stateLabel(leaf.state)}${leaf.version ? ` · v${leaf.version}` : ''}）`;
        button.addEventListener('click', () => loadLeaf(leaf.code));
        item.append(button);
        list.append(item);
      }
      section.append(list);
      parts.push(section);
    }
    detailEl.replaceChildren(...parts);
  };

  const showTree = data => {
    setStatus(`事实树 · ${stateLabel(data.state)}`, data.state === 'ready' ? 'ok' : '');
    const trunks = Array.isArray(data.trunks) ? data.trunks : [];
    const navButtons = trunks.map(trunk => {
      const button = node('button', 'facts-tree-node facts-tree-trunk-link');
      button.type = 'button';
      button.dataset.code = trunk.code;
      const title = node('span', 'facts-tree-trunk-title',
        `${trunk.directory ? `${trunk.directory} · ` : ''}${trunk.code}`);
      const summary = node('span', 'facts-tree-trunk-state',
        `${stateLabel(trunk.state)} · 叶 ${trunk.generated_leaves}/${trunk.leaves}`);
      button.append(title, summary);
      button.addEventListener('click', () => loadTrunk(trunk.code));
      return button;
    });
    navEl.replaceChildren(...navButtons);
    const parts = [];
    const bound = bindingLine(data.binding);
    if (bound) parts.push(bound);
    if (data.state === 'unbound') {
      parts.push(node('p', 'facts-tree-empty', '本公司尚未绑定事实树模板，暂无可浏览的事实树。'));
    } else if (data.state === 'empty') {
      parts.push(node('p', 'facts-tree-empty', '事实树暂无内容，等待资料阅读与发布。'));
    } else if (data.state === 'not_generated') {
      parts.push(node('p', 'facts-tree-empty', '事实已发布，导览尚未生成，请稍后刷新。'));
    }
    if (data.brief) parts.push(textBlock('简报', data.brief));
    if (data.guide) parts.push(textBlock('导览', data.guide));
    detailEl.replaceChildren(...parts);
  };

  const load = async (which, code, render) => {
    setStatus('正在读取事实树…');
    try {
      render(await request(which, code));
    } catch (failure) {
      showFailure(failure);
    }
  };
  const loadTree = () => load('tree', null, showTree);
  const loadTrunk = code => load('trunk', code, showTrunk);
  const loadLeaf = code => load('leaf', code, showLeaf);

  loadTree();
})();
