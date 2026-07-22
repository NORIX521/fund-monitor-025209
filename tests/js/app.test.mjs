import test from 'node:test';
import assert from 'node:assert/strict';

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(...values) { values.forEach((value) => this.values.add(value)); }
  remove(...values) { values.forEach((value) => this.values.delete(value)); }
  contains(value) { return this.values.has(value); }
}

class FakeNode {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.classList = new FakeClassList();
    this.dataset = {};
    this.textContent = '';
    this.className = '';
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = [...children]; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
}

const nodes = new Map([
  ['previewRows', new FakeNode('tbody')],
  ['importSummary', new FakeNode('span')],
  ['importMessage', new FakeNode('div')],
  ['createIssueLink', new FakeNode('a')],
]);
globalThis.Node = FakeNode;
globalThis.document = {
  createElement: (tagName) => new FakeNode(tagName),
  getElementById: (id) => nodes.get(id),
};

const { dashboardHealth, normalizeDashboard, renderImportPreview, renderNews, setImportError } = await import('../../assets/app.js');
const { parseImportCsv } = await import('../../assets/core.js');

test('valid CSV followed by import error clears preview and actionable issue state', () => {
  renderImportPreview(parseImportCsv('code,name,asset_type\n600519,贵州茅台,stock'));
  assert.ok(nodes.get('previewRows').children.length > 0);
  assert.ok(nodes.get('createIssueLink').getAttribute('href'));

  setImportError('CSV import requires a code header');
  assert.equal(nodes.get('previewRows').children.length, 0);
  assert.equal(nodes.get('createIssueLink').getAttribute('href'), null);
  assert.equal(nodes.get('createIssueLink').getAttribute('aria-disabled'), 'true');
  assert.equal(nodes.get('createIssueLink').classList.contains('disabled'), true);
});

test('dashboard retains pipeline health and rejects declared count mismatches', () => {
  const raw = {
    generated_at: '2026-07-22T00:00:00+00:00',
    pipeline_version: '4.1',
    asset_count: 0,
    stale_count: 0,
    assets: [],
    source_status: {
      pipeline: {
        provider: 'update_monitor', source_urls: [], attempted_at: '2026-07-22T00:00:00+00:00',
        retrieved_at: '', last_success_at: '', stale: true, error: 'pipeline_refresh_required', coverage: {},
      },
    },
  };
  const dashboard = normalizeDashboard(raw);
  assert.equal(dashboard.source_status.pipeline.stale, true);
  assert.equal(dashboard.source_status.pipeline.error, 'pipeline_refresh_required');
  assert.equal(dashboard.asset_count, 0);
  assert.equal(dashboardHealth(dashboard).kind, 'error');
  assert.match(dashboardHealth(dashboard).message, /pipeline_refresh_required/);

  assert.throws(() => normalizeDashboard({ ...raw, asset_count: 1 }), /asset_count/i);
  assert.throws(() => normalizeDashboard({ ...raw, stale_count: 1 }), /stale_count/i);
  assert.throws(() => normalizeDashboard({
    ...raw,
    source_status: { pipeline: { ...raw.source_status.pipeline, retrieved_at: { unsafe: true } } },
  }), /retrieved_at/i);
  assert.throws(() => normalizeDashboard({
    ...raw,
    source_status: { pipeline: { ...raw.source_status.pipeline, coverage: { covered: 1 } } },
  }), /coverage/i);
});

test('both trend streams expose separate safe article and publisher links', () => {
  for (const region of ['CN', 'INTL']) {
    const root = new FakeNode('div');
    renderNews([{
      title: `${region} trend`,
      article_url: `https://articles.example/${region}`,
      source: `${region} publisher`,
      source_url: `https://publisher.example/${region}`,
      published_at: '2026-07-22T00:00:00+00:00',
      retrieved_at: '2026-07-22T01:00:00+00:00',
    }], root, 'empty');
    const links = root.children[0].children[0].children;
    assert.equal(links.length, 2);
    assert.equal(links[0].href, `https://articles.example/${region}`);
    assert.equal(links[1].href, `https://publisher.example/${region}`);
    assert.match(links[1].textContent, /来源/);
    assert.equal(links[1].rel, 'noreferrer noopener');
  }
  const unsafeRoot = new FakeNode('div');
  renderNews([{ title: 'safe article', article_url: 'https://articles.example/safe', source: 'unsafe publisher', source_url: 'javascript:alert(1)' }], unsafeRoot, 'empty');
  assert.equal(unsafeRoot.children[0].children[0].children.length, 1);
});
