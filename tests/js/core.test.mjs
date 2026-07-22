import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildIssueUrl,
  escapeHtml,
  filterAssets,
  parseImportCsv,
  parseImportText,
} from '../../assets/core.js';

test('mixed paste becomes a normalized safe GitHub issue payload', () => {
  const assets = parseImportText('600519,贵州茅台,stock\n025209\t半导体基金\tfund');
  const url = new URL(buildIssueUrl(assets));

  assert.equal(assets.length, 2);
  assert.deepEqual(assets.map(({ code, asset_type, market }) => ({ code, asset_type, market })), [
    { code: '600519.SH', asset_type: 'stock', market: 'CN' },
    { code: '025209', asset_type: 'fund', market: 'CN' },
  ]);
  assert.equal(url.origin + url.pathname, 'https://github.com/NORIX521/fund-monitor-025209/issues/new');
  assert.equal(url.searchParams.get('title'), '[watchlist-import] 2 assets');
  assert.equal(
    url.searchParams.get('body'),
    `请确认以下批量导入。\n\n<!-- WATCHLIST_IMPORT_V1\n${JSON.stringify({ version: 1, mode: 'merge', assets })}\n-->`,
  );
});

test('paste rejects more than fifty rows', () => {
  const rows = Array.from({ length: 51 }, (_, index) => `A${index},Asset ${index},stock`).join('\n');
  assert.throws(() => parseImportText(rows), /at most 50/i);
});

test('paste reports duplicate and invalid rows without including them', () => {
  const assets = parseImportText([
    '600519,贵州茅台,stock',
    '600519.SH,重复项,stock',
    'bad code,危险代码,stock',
    '025209,基金,fund',
    '00700.HK,腾讯,stock',
  ].join('\n'));

  assert.deepEqual(assets.map((asset) => asset.code), ['600519.SH', '025209', '00700.HK']);
  assert.equal(assets.duplicates.length, 1);
  assert.equal(assets.duplicates[0].row, 2);
  assert.equal(assets.invalid.length, 1);
  assert.equal(assets.invalid[0].row, 3);
  assert.match(assets.invalid[0].reason, /invalid code/i);
});

test('CSV parser supports quoted fields and bilingual headers', () => {
  const assets = parseImportCsv([
    '代码,名称,类型,板块,备注',
    '600519,"贵州,茅台",股票,消费,"长期,观察"',
    '025209,半导体基金,基金,半导体,',
  ].join('\n'));

  assert.equal(assets.length, 2);
  assert.equal(assets[0].name, '贵州,茅台');
  assert.equal(assets[0].note, '长期,观察');
  assert.equal(assets[1].asset_type, 'fund');
});

test('CSV parser preserves quoted CRLF records and logical source rows', () => {
  const assets = parseImportCsv([
    'code,name,asset_type,note',
    '',
    '600519,"贵州\r\n茅台",stock,"第一行\r\n第二行"',
    '025209,半导体基金,fund,',
  ].join('\r\n'));

  assert.equal(assets.length, 2);
  assert.equal(assets[0].name, '贵州茅台');
  assert.equal(assets[0].note, '第一行第二行');
  assert.equal(assets[0].sourceRow, 3);
  assert.equal(assets[1].sourceRow, 6);
  assert.equal(Object.prototype.propertyIsEnumerable.call(assets[0], 'sourceRow'), false);
  assert.doesNotMatch(JSON.stringify(assets), /sourceRow/);
});

test('CSV parser requires a code header and reports malformed rows', () => {
  assert.throws(() => parseImportCsv('name,type\nApple,stock'), /code header/i);
  const assets = parseImportCsv('code,name,asset_type\n600519,茅台,stock\n"unterminated');
  assert.equal(assets.length, 1);
  assert.equal(assets.invalid.length, 1);
});

test('escapeHtml neutralizes imported markup', () => {
  assert.equal(
    escapeHtml(`<img src=x onerror="alert('x')"> &`),
    '&lt;img src=x onerror=&quot;alert(&#39;x&#39;)&quot;&gt; &amp;',
  );
});

test('filterAssets applies query, type, state, and stale filters', () => {
  const assets = [
    { code: '600519.SH', name: '贵州茅台', asset_type: 'stock', state: '优先研究', stale: false },
    { code: '025209', name: '半导体基金', asset_type: 'fund', state: '等待确认', stale: true },
    { code: 'AAPL', name: 'Apple', asset_type: 'stock', state: '持续观察', stale: false },
  ];

  assert.deepEqual(filterAssets(assets, { query: 'apple' }).map((asset) => asset.code), ['AAPL']);
  assert.deepEqual(filterAssets(assets, { assetType: 'stock', recommendation: '优先研究' }).map((asset) => asset.code), ['600519.SH']);
  assert.deepEqual(filterAssets(assets, '等待确认').map((asset) => asset.code), ['025209']);
  assert.deepEqual(filterAssets(assets, { status: '持续观察' }).map((asset) => asset.code), ['AAPL']);
  assert.deepEqual(filterAssets(assets, { freshness: 'stale' }).map((asset) => asset.code), ['025209']);
  assert.equal(filterAssets(assets, { freshness: 'fresh' }).length, 2);
});

test('unsafe codes and unsupported asset types are rejected before issue creation', () => {
  const unsafe = parseImportText('600519<script>,X,stock\n600519,债券,bond');
  assert.equal(unsafe.length, 0);
  assert.equal(unsafe.invalid.length, 2);
  assert.throws(() => buildIssueUrl([{ code: '600519<script>', asset_type: 'stock' }]), /invalid code/i);
  assert.throws(() => buildIssueUrl([{ code: '600519', asset_type: 'bond' }]), /unsupported asset type/i);
});
