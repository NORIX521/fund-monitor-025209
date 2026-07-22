import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const [html, app, css] = await Promise.all([
  readFile(new URL('../../index.html', import.meta.url), 'utf8'),
  readFile(new URL('../../assets/app.js', import.meta.url), 'utf8'),
  readFile(new URL('../../assets/styles.css', import.meta.url), 'utf8'),
]);

test('import preview has an accessible scroll-region name and centralized failures', () => {
  assert.match(html, /class="table-wrap preview-wrap"[^>]+aria-label="可横向滚动的导入预览表格"/);
  assert.ok((app.match(/setImportError\(/g) || []).length >= 3);
});

test('mobile evidence anchors have real 44 by 44px boxes', () => {
  assert.match(css, /\.source-links a, \.news-links a\s*\{[^}]*display:\s*inline-flex/s);
  assert.match(css, /@media \(max-width: 640px\)[\s\S]*\.news-item a, \.source-links a\s*\{\s*min-height:\s*44px/s);
  assert.match(css, /@media \(max-width: 640px\)[\s\S]*\.news-item a, \.source-links a\s*\{[^}]*min-width:\s*44px/s);
});
