import { buildIssueUrl, filterAssets, parseImportCsv, parseImportText } from './core.js';

const SAFE_ID = /^[a-z0-9][a-z0-9-]{0,119}$/;
const TYPES = new Set(['stock', 'fund', 'etf', 'lof']);
const STATES = new Set(['优先研究', '持续观察', '等待确认', '风险偏高', '暂不纳入']);
const STATUS_FIELDS = new Set(['provider', 'source_urls', 'attempted_at', 'retrieved_at', 'last_success_at', 'stale', 'error', 'coverage']);
const detailCache = new Map();
let dashboard = null;
let selectedId = '';
let lastImportTrigger = null;

const $ = (id) => document.getElementById(id);
const text = (value, fallback = '—') => {
  const normalized = typeof value === 'string' || typeof value === 'number' ? String(value).trim() : '';
  return normalized ? normalized.slice(0, 500) : fallback;
};
const finite = (value) => Number.isFinite(Number(value)) ? Number(value) : null;
const formatPercent = (value, scale = 1) => {
  const number = finite(value);
  return number == null ? '—' : `${Math.round(number * scale * 100)}%`;
};
const formatDate = (value) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? text(value) : new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(date);
};
const clear = (node) => node.replaceChildren();

function safeUrl(value) {
  try {
    const url = new URL(String(value));
    return ['https:', 'http:'].includes(url.protocol) ? url.toString() : '';
  } catch {
    return '';
  }
}

function setStatus(message, kind = 'neutral') {
  $('appStatus').textContent = message;
  $('appStatus').className = `app-status ${kind}`;
}

function normalizeSummary(raw) {
  if (!raw || typeof raw !== 'object' || !SAFE_ID.test(String(raw.id || ''))) return null;
  const assetType = String(raw.asset_type || '').toLowerCase();
  const state = String(raw.state || '');
  const confidence = finite(raw.confidence);
  if (!TYPES.has(assetType) || !STATES.has(state) || confidence == null || confidence < 0 || confidence > 1) return null;
  return {
    id: String(raw.id),
    code: text(raw.code),
    name: text(raw.name, '未命名资产'),
    asset_type: assetType,
    state,
    confidence,
    stale: raw.stale === true,
  };
}

function normalizeSourceStatus(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('source_status 数据结构无效');
  const normalized = {};
  for (const [key, status] of Object.entries(raw)) {
    if (!status || typeof status !== 'object' || Array.isArray(status)) throw new Error(`source_status.${key} 数据结构无效`);
    const fields = Object.keys(status);
    if (fields.length !== STATUS_FIELDS.size || fields.some((field) => !STATUS_FIELDS.has(field))) throw new Error(`source_status.${key} 字段无效`);
    if (typeof status.provider !== 'string' || !status.provider.trim()) throw new Error(`source_status.${key}.provider 无效`);
    if (!Array.isArray(status.source_urls) || status.source_urls.some((url) => !safeUrl(url))) throw new Error(`source_status.${key}.source_urls 无效`);
    if (typeof status.stale !== 'boolean' || typeof status.error !== 'string' || typeof status.attempted_at !== 'string' || !status.attempted_at) {
      throw new Error(`source_status.${key} 状态字段无效`);
    }
    for (const timestampName of ['attempted_at', 'retrieved_at', 'last_success_at']) {
      const timestamp = status[timestampName];
      if (typeof timestamp !== 'string' || (timestamp && Number.isNaN(Date.parse(timestamp)))) throw new Error(`source_status.${key}.${timestampName} 无效`);
    }
    if (!status.coverage || typeof status.coverage !== 'object' || Array.isArray(status.coverage)) throw new Error(`source_status.${key}.coverage 无效`);
    const coverageFields = Object.keys(status.coverage);
    if (coverageFields.length && (
      coverageFields.length !== 3 || !['covered', 'total', 'pct'].every((field) => coverageFields.includes(field))
      || !Number.isInteger(status.coverage.covered) || !Number.isInteger(status.coverage.total) || status.coverage.total <= 0
      || finite(status.coverage.pct) == null
    )) throw new Error(`source_status.${key}.coverage 无效`);
    normalized[key] = {
      provider: text(status.provider),
      source_urls: status.source_urls.map(safeUrl),
      attempted_at: text(status.attempted_at, ''),
      retrieved_at: text(status.retrieved_at, ''),
      last_success_at: text(status.last_success_at, ''),
      stale: status.stale,
      error: text(status.error, ''),
      coverage: { ...status.coverage },
    };
  }
  if (!normalized.pipeline) throw new Error('source_status.pipeline 缺失');
  return normalized;
}

export function normalizeDashboard(raw) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.assets)) throw new Error('总览数据结构无效');
  const assets = raw.assets.map(normalizeSummary).filter(Boolean);
  if (!Number.isInteger(raw.asset_count) || raw.asset_count < 0 || raw.asset_count !== raw.assets.length || assets.length !== raw.assets.length) {
    throw new Error(`asset_count 与有效资产行不一致：声明 ${raw.asset_count}，原始 ${raw.assets.length}，有效 ${assets.length}`);
  }
  const normalizedStaleCount = assets.filter((asset) => asset.stale).length;
  if (!Number.isInteger(raw.stale_count) || raw.stale_count < 0 || raw.stale_count !== normalizedStaleCount) {
    throw new Error(`stale_count 与资产状态不一致：声明 ${raw.stale_count}，有效 ${normalizedStaleCount}`);
  }
  return {
    generated_at: text(raw.generated_at, ''),
    pipeline_version: text(raw.pipeline_version, '—'),
    source_status: normalizeSourceStatus(raw.source_status),
    stale_count: raw.stale_count,
    asset_count: raw.asset_count,
    assets,
  };
}

export function dashboardHealth(value) {
  const pipeline = value?.source_status?.pipeline;
  if (pipeline?.error) return { kind: 'error', label: '流水线异常', message: `流水线异常：${pipeline.error}` };
  if (pipeline?.stale) return { kind: 'warning', label: '流水线过期', message: '流水线数据已过期，请核对来源与最后成功时间。' };
  if (value?.assets?.some((asset) => asset.stale)) return { kind: 'warning', label: '含过期数据', message: `已载入 ${value.assets.length} 项资产，其中 ${value.stale_count} 项需重新确认。` };
  return value?.assets?.length
    ? { kind: 'success', label: '数据当前', message: `已载入 ${value.assets.length} 项研究资产。` }
    : { kind: 'neutral', label: '等待资产', message: '数据已载入，研究队列当前为空。' };
}

async function loadDashboard(force = false) {
  if (force) detailCache.clear();
  $('refreshButton').disabled = true;
  $('refreshButton').textContent = '读取中…';
  setStatus('正在读取研究数据…');
  try {
    const url = new URL('data/dashboard.json', document.baseURI);
    if (force) url.searchParams.set('t', Date.now());
    const response = await fetch(url, { cache: force ? 'no-store' : 'default' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    dashboard = normalizeDashboard(await response.json());
    renderOverview();
    renderAssets();
    const health = dashboardHealth(dashboard);
    setStatus(health.message, health.kind);
    if (selectedId && dashboard.assets.some((asset) => asset.id === selectedId)) selectAsset(selectedId);
    else if (dashboard.assets.length) selectAsset(dashboard.assets[0].id);
    else resetDetail();
  } catch (error) {
    dashboard = {
      generated_at: '', pipeline_version: '—', stale_count: 0, asset_count: 0, assets: [],
      source_status: { pipeline: { provider: 'browser', source_urls: [], attempted_at: new Date().toISOString(), retrieved_at: '', last_success_at: '', stale: true, error: 'dashboard_load_failed', coverage: {} } },
    };
    renderOverview();
    renderAssets('无法读取总览数据。请检查网络后重试；若离线，需先成功访问一次以建立缓存。');
    resetDetail('总览读取失败，暂时无法选择资产。');
    setStatus(`数据读取失败：${error instanceof Error ? error.message : '未知错误'}`, 'error');
  } finally {
    $('refreshButton').disabled = false;
    $('refreshButton').textContent = '刷新数据';
  }
}

function renderOverview() {
  const assets = dashboard?.assets || [];
  const confidence = assets.length ? assets.reduce((sum, asset) => sum + asset.confidence, 0) / assets.length : null;
  $('assetCount').textContent = String(dashboard?.asset_count ?? assets.length);
  $('averageConfidence').textContent = confidence == null ? '—' : formatPercent(confidence, 1);
  $('priorityCount').textContent = String(assets.filter((asset) => asset.state === '优先研究').length);
  $('staleCount').textContent = String(dashboard?.stale_count ?? assets.filter((asset) => asset.stale).length);
  $('generatedAt').textContent = `更新时间 ${formatDate(dashboard?.generated_at)}`;
  $('pipelineVersion').textContent = `Pipeline ${dashboard?.pipeline_version || '—'}`;
  const health = dashboardHealth(dashboard);
  $('freshnessBadge').textContent = health.label;
  const badgeKind = health.kind === 'success' ? 'positive' : health.kind === 'error' ? 'negative' : health.kind;
  $('freshnessBadge').className = `status-chip ${badgeKind}`;
}

function currentFilters() {
  return {
    query: $('searchInput').value,
    assetType: $('typeFilter').value,
    recommendation: $('stateFilter').value,
    freshness: $('freshnessFilter').value,
  };
}

function makeCell(label, value, className = '') {
  const cell = document.createElement('td');
  cell.dataset.label = label;
  if (className) cell.className = className;
  if (value instanceof Node) cell.append(value);
  else cell.textContent = value;
  return cell;
}

function stateClass(state) {
  if (state === '优先研究') return 'positive';
  if (state === '风险偏高' || state === '暂不纳入') return 'negative';
  if (state === '等待确认') return 'warning';
  return 'neutral';
}

function renderAssets(errorMessage = '') {
  const body = $('assetRows');
  clear(body);
  const filtered = filterAssets(dashboard?.assets || [], currentFilters());
  $('resultCount').textContent = `${filtered.length} 项`;
  $('assetEmpty').hidden = filtered.length > 0;
  $('assetEmptyText').textContent = errorMessage || ((dashboard?.assets || []).length ? '没有匹配当前筛选条件的资产。' : '通过批量导入中心建立第一批研究资产。');
  for (const asset of filtered) {
    const row = document.createElement('tr');
    if (asset.id === selectedId) row.classList.add('selected');

    const identity = document.createElement('div');
    identity.className = 'asset-identity';
    const name = document.createElement('strong');
    name.textContent = asset.name;
    const code = document.createElement('span');
    code.textContent = asset.code;
    identity.append(name, code);

    const state = document.createElement('span');
    state.className = `status-chip ${stateClass(asset.state)}`;
    state.textContent = asset.state;
    const stale = document.createElement('span');
    stale.className = `status-chip ${asset.stale ? 'warning' : 'positive'}`;
    stale.textContent = asset.stale ? '需确认' : '当前';
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'row-action';
    open.textContent = asset.id === selectedId ? '当前' : '查看';
    open.setAttribute('aria-label', `查看 ${asset.name} ${asset.code} 研究详情`);
    if (asset.id === selectedId) open.setAttribute('aria-current', 'true');
    open.addEventListener('click', () => selectAsset(asset.id));

    row.append(
      makeCell('资产', identity),
      makeCell('类型', asset.asset_type.toUpperCase()),
      makeCell('研究状态', state),
      makeCell('置信度', formatPercent(asset.confidence, 1), 'numeric'),
      makeCell('数据', stale),
      makeCell('操作', open, 'action-cell'),
    );
    body.append(row);
  }
}

function resetDetail(message = '从资产列表选择一项，查看模型、证据和风险失效条件。') {
  selectedId = '';
  $('detailContent').hidden = true;
  $('detailPlaceholder').hidden = false;
  $('detailTitle').textContent = '选择一项资产';
  $('detailPlaceholder').querySelector('p').textContent = message;
  $('assetDetail').setAttribute('aria-busy', 'false');
  renderNews([], $('domesticNews'), '选择资产后加载。');
  renderNews([], $('internationalNews'), '选择资产后加载。');
}

async function selectAsset(id) {
  if (!SAFE_ID.test(id) || !dashboard?.assets.some((asset) => asset.id === id)) return;
  selectedId = id;
  renderAssets();
  $('assetDetail').setAttribute('aria-busy', 'true');
  $('detailContent').hidden = true;
  $('detailPlaceholder').hidden = false;
  $('detailTitle').textContent = '正在加载研究详情…';
  $('detailPlaceholder').querySelector('p').textContent = '正在读取单资产证据文件。';
  try {
    let detail = detailCache.get(id);
    if (!detail) {
      const response = await fetch(new URL(`data/assets/${encodeURIComponent(id)}.json`, document.baseURI));
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      detail = await response.json();
      if (!detail?.asset || detail.asset.id !== id || !SAFE_ID.test(String(detail.asset.id))) throw new Error('单资产数据结构无效');
      detailCache.set(id, detail);
    }
    if (selectedId !== id) return;
    renderDetail(detail);
  } catch (error) {
    if (selectedId !== id) return;
    $('detailTitle').textContent = '研究详情不可用';
    $('detailPlaceholder').querySelector('p').textContent = `无法读取该资产的详情：${error instanceof Error ? error.message : '未知错误'}。总览摘要仍可使用。`;
    renderNews([], $('domesticNews'), '详情读取失败，未展示未经验证的趋势。');
    renderNews([], $('internationalNews'), '详情读取失败，未展示未经验证的趋势。');
  } finally {
    if (selectedId === id) $('assetDetail').setAttribute('aria-busy', 'false');
  }
}

function addListItems(node, values, fallback) {
  clear(node);
  const items = Array.isArray(values) ? values.filter((value) => typeof value === 'string' && value.trim()) : [];
  for (const value of items) {
    const item = document.createElement('li');
    item.textContent = text(value);
    node.append(item);
  }
  if (!items.length) {
    const item = document.createElement('li');
    item.className = 'muted';
    item.textContent = fallback;
    node.append(item);
  }
}

function renderDetail(detail) {
  const asset = detail.asset || {};
  const score = detail.score || {};
  const recommendation = detail.recommendation || {};
  const coverage = score.coverage || {};
  const confidence = Math.max(0, Math.min(1, finite(recommendation.confidence ?? score.confidence) ?? 0));
  const overall = finite(score.overall);
  const isStock = asset.asset_type === 'stock';

  $('detailPlaceholder').hidden = true;
  $('detailContent').hidden = false;
  $('detailCode').textContent = text(asset.code);
  $('detailName').textContent = text(asset.name, '未命名资产');
  $('detailMeta').textContent = [text(asset.asset_type, '').toUpperCase(), text(asset.market, ''), text(asset.sector, '')].filter(Boolean).join(' · ') || '资产信息不完整';
  $('detailState').textContent = STATES.has(recommendation.state) ? recommendation.state : '等待确认';
  $('detailState').className = `status-chip ${stateClass(recommendation.state)}`;
  $('overallScore').textContent = overall == null ? '—' : String(Math.round(overall));
  $('modelLabel').textContent = isStock ? `UZI 股票复核模型 · ${text(detail.uzi?.version ?? detail.uzi?.model_version, '版本未提供')}` : `基金研究模型 · 持仓 UZI 覆盖 ${formatPercent(coverage.holding_uzi_pct, 0.01)}`;
  $('confidenceValue').textContent = formatPercent(confidence, 1);
  $('confidenceBar').style.width = `${confidence * 100}%`;
  const weightCoverage = finite(coverage.weight_pct);
  $('coverageLabel').textContent = weightCoverage == null ? '证据覆盖未提供' : `证据覆盖 ${formatPercent(weightCoverage, 0.01)}`;

  clear($('componentList'));
  const components = score.components && typeof score.components === 'object' ? Object.entries(score.components) : [];
  for (const [key, value] of components) {
    const number = finite(value);
    if (number == null) continue;
    const item = document.createElement('div');
    const label = document.createElement('span');
    label.textContent = key.replaceAll('_', ' ');
    const bar = document.createElement('div');
    bar.className = 'mini-bar';
    const fill = document.createElement('span');
    fill.style.width = `${Math.max(0, Math.min(100, number))}%`;
    bar.append(fill);
    const scoreValue = document.createElement('strong');
    scoreValue.textContent = String(Math.round(number));
    item.append(label, bar, scoreValue);
    $('componentList').append(item);
  }
  if (!components.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '本周期没有足够的分项证据。';
    $('componentList').append(empty);
  }

  addListItems($('reasonList'), recommendation.reasons, '未提供可追溯的研究依据。');
  const risk = recommendation.risk || {};
  const risks = [...(Array.isArray(risk.hard_flags) ? risk.hard_flags : []), ...(Array.isArray(risk.warnings) ? risk.warnings : []), ...(Array.isArray(risk.hard_failures) ? risk.hard_failures : []), ...(Array.isArray(score.risk_flags) ? score.risk_flags : [])];
  addListItems($('riskList'), [...new Set(risks)], '当前记录未列出明确风险；这不等于没有风险。');
  addListItems($('invalidationList'), recommendation.invalidation_rules, '失效条件尚未提供，结论需谨慎使用。');
  renderSourceStatus(detail.source_status || {});
  renderNews(detail.news?.CN || [], $('domesticNews'), '本周期未获得可靠的国内趋势更新。');
  renderNews(detail.news?.INTL || [], $('internationalNews'), '本周期未获得可靠的国际趋势更新。');
}

function renderSourceStatus(statuses) {
  const root = $('sourceStatusList');
  clear(root);
  const entries = statuses && typeof statuses === 'object' ? Object.entries(statuses) : [];
  for (const [name, status] of entries) {
    if (!status || typeof status !== 'object') continue;
    const item = document.createElement('div');
    item.className = 'source-row';
    const label = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = `${name.replaceAll('_', ' ')} · ${text(status.provider, '来源未提供')}`;
    const timestamp = document.createElement('small');
    timestamp.textContent = `最后成功 ${formatDate(status.last_success_at || status.retrieved_at)}${status.error ? ` · ${text(status.error)}` : ''}`;
    label.append(title, timestamp);
    const badge = document.createElement('span');
    badge.className = `status-chip ${status.stale ? 'warning' : 'positive'}`;
    badge.textContent = status.stale ? '需确认' : '当前';
    item.append(label, badge);
    const urls = Array.isArray(status.source_urls) ? status.source_urls.map(safeUrl).filter(Boolean) : [];
    if (urls.length) {
      const links = document.createElement('div');
      links.className = 'source-links';
      urls.slice(0, 3).forEach((url, index) => {
        const link = document.createElement('a');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noreferrer noopener';
        link.textContent = `来源 ${index + 1}`;
        links.append(link);
      });
      item.append(links);
    }
    root.append(item);
  }
  if (!root.childElementCount) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '没有可审计的数据来源状态。';
    root.append(empty);
  }
}

export function renderNews(items, root, fallback) {
  clear(root);
  const valid = Array.isArray(items) ? items.filter((item) => item && typeof item === 'object' && safeUrl(item.article_url || item.url)) : [];
  for (const item of valid) {
    const article = document.createElement('article');
    article.className = 'news-item';
    const links = document.createElement('div');
    links.className = 'news-links';
    const link = document.createElement('a');
    link.href = safeUrl(item.article_url || item.url);
    link.target = '_blank';
    link.rel = 'noreferrer noopener';
    link.textContent = text(item.title, '未命名来源记录');
    links.append(link);
    const publisherUrl = safeUrl(item.source_url);
    if (publisherUrl) {
      const publisher = document.createElement('a');
      publisher.href = publisherUrl;
      publisher.target = '_blank';
      publisher.rel = 'noreferrer noopener';
      publisher.className = 'publisher-link';
      publisher.textContent = `来源：${text(item.source, '发布方')}`;
      links.append(publisher);
    }
    const meta = document.createElement('p');
    meta.textContent = `${text(item.source, '来源未注明')} · 发布 ${formatDate(item.published_at)} · 抓取 ${formatDate(item.retrieved_at)}`;
    article.append(links, meta);
    root.append(article);
  }
  if (!valid.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-copy';
    empty.textContent = fallback;
    root.append(empty);
  }
}

function openImport(event) {
  lastImportTrigger = event?.currentTarget || $('importButton');
  if (!$('importDialog').open) $('importDialog').showModal();
  requestAnimationFrame(() => $('importText').focus());
}

function resetImport() {
  $('importText').value = '';
  $('csvFile').value = '';
  clear($('previewRows'));
  $('importSummary').textContent = '尚未解析';
  $('importSummary').className = 'status-chip neutral';
  $('importMessage').textContent = '输入内容后即可预览。';
  $('createIssueLink').removeAttribute('href');
  $('createIssueLink').classList.add('disabled');
  $('createIssueLink').setAttribute('aria-disabled', 'true');
}

function disableIssueLink() {
  const link = $('createIssueLink');
  link.removeAttribute('href');
  link.classList.add('disabled');
  link.setAttribute('aria-disabled', 'true');
}

export function setImportError(message, title = '解析失败') {
  clear($('previewRows'));
  $('importSummary').textContent = title;
  $('importSummary').className = 'status-chip negative';
  $('importMessage').textContent = text(message, '无法解析导入内容');
  disableIssueLink();
}

function appendPreviewRow(row, code, type, status, kind) {
  const tr = document.createElement('tr');
  const badge = document.createElement('span');
  badge.className = `status-chip ${kind}`;
  badge.textContent = status;
  tr.append(makeCell('行', String(row)), makeCell('代码 / 名称', code), makeCell('类型', type), makeCell('状态', badge));
  $('previewRows').append(tr);
}

export function renderImportPreview(assets) {
  clear($('previewRows'));
  assets.forEach((asset, index) => appendPreviewRow(asset.sourceRow ?? index + 1, `${asset.code} · ${asset.name || '未命名'}`, asset.asset_type.toUpperCase(), '可导入', 'positive'));
  assets.duplicates.forEach((item) => appendPreviewRow(item.row, item.code, '—', '重复，已忽略', 'warning'));
  assets.invalid.forEach((item) => appendPreviewRow(item.row, `${text(item.input, '无有效内容')} · ${text(item.reason)}`, '—', '无效，已拒绝', 'negative'));
  const issues = assets.duplicates.length + assets.invalid.length;
  $('importSummary').textContent = `${assets.length} 可导入 · ${issues} 需处理`;
  $('importSummary').className = `status-chip ${assets.length && !issues ? 'positive' : assets.length ? 'warning' : 'negative'}`;
  $('importMessage').textContent = assets.length ? '预览仅保留规范化字段。提交后由仓库工作流再次鉴权和校验。' : '没有可导入资产；请修正代码或资产类型。';
  const link = $('createIssueLink');
  if (assets.length) {
    link.setAttribute('href', buildIssueUrl(assets));
    link.target = '_blank';
    link.rel = 'noreferrer noopener';
    link.classList.remove('disabled');
    link.setAttribute('aria-disabled', 'false');
  } else {
    disableIssueLink();
  }
}

function parsePastedImport() {
  const value = $('importText').value;
  if (!value.trim()) return resetImport();
  try {
    renderImportPreview(parseImportText(value));
  } catch (error) {
    setImportError(error instanceof Error ? error.message : '无法解析导入内容');
  }
}

async function parseCsvFile() {
  const [file] = $('csvFile').files;
  if (!file) return;
  try {
    const content = await file.text();
    renderImportPreview(parseImportCsv(content));
    $('importText').value = '';
  } catch (error) {
    setImportError(error instanceof Error ? error.message : '无法读取 CSV 文件', 'CSV 解析失败');
  }
}

function initializeApp() {
  $('refreshButton').addEventListener('click', () => loadDashboard(true));
  $('importButton').addEventListener('click', openImport);
  $('emptyImportButton').addEventListener('click', openImport);
  $('closeImportButton').addEventListener('click', () => $('importDialog').close());
  $('clearImportButton').addEventListener('click', resetImport);
  $('importText').addEventListener('input', parsePastedImport);
  $('csvFile').addEventListener('change', parseCsvFile);
  $('createIssueLink').addEventListener('click', (event) => { if (event.currentTarget.getAttribute('aria-disabled') === 'true') event.preventDefault(); });
  $('importDialog').addEventListener('close', () => lastImportTrigger?.focus());
  $('filterForm').addEventListener('input', () => renderAssets());
  $('filterForm').addEventListener('submit', (event) => event.preventDefault());
  if ('serviceWorker' in navigator) navigator.serviceWorker.register(new URL('sw.js', document.baseURI)).catch(() => {});
  loadDashboard();
}

if (typeof document !== 'undefined' && $('refreshButton')) initializeApp();
