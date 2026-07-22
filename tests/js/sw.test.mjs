import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../../sw.js', import.meta.url), 'utf8');

function loadWorker({ fetchImpl, cached }) {
  const writes = [];
  const matches = [];
  const cache = {
    match: async (request) => { matches.push(request); return cached; },
    put: async (request, response) => writes.push({ request, response }),
    addAll: async () => {},
  };
  const self = {
    location: { origin: 'https://example.test' },
    clients: { claim() {} },
    skipWaiting() {},
    addEventListener() {},
  };
  const context = { self, fetch: fetchImpl, caches: { open: async () => cache, keys: async () => [], match: async () => cached }, URL, Promise };
  vm.runInNewContext(source, context, { filename: 'sw.js' });
  return { networkFirst: self.__fundMonitorTest.networkFirst, writes, matches };
}

test('network-first stores and returns a successful data response', async () => {
  const network = { ok: true, status: 200, clone: () => ({ copy: true }) };
  const request = { url: 'https://example.test/app/data/dashboard.json?t=123' };
  const { networkFirst, writes } = loadWorker({ fetchImpl: async () => network, cached: { cached: true } });
  assert.equal(await networkFirst(request), network);
  assert.equal(writes.length, 1);
  assert.equal(writes[0].request, 'https://example.test/app/data/dashboard.json');
});

test('cache-busted network rejection falls back to canonical cached data URL', async () => {
  const cached = { ok: true, cached: true };
  const request = { url: 'https://example.test/app/data/dashboard.json?t=456' };
  const { networkFirst, matches } = loadWorker({ fetchImpl: async () => { throw new Error('offline'); }, cached });
  assert.equal(await networkFirst(request), cached);
  assert.deepEqual(matches, ['https://example.test/app/data/dashboard.json']);
});

test('cache-busted 500/404 uses canonical cache and network response when cache is absent', async () => {
  for (const status of [500, 404]) {
    const network = { ok: false, status };
    const cached = { ok: true, cached: status };
    const request = { url: `https://example.test/app/data/asset.json?t=${status}` };
    const withCache = loadWorker({ fetchImpl: async () => network, cached });
    assert.equal(await withCache.networkFirst(request), cached);
    assert.deepEqual(withCache.matches, ['https://example.test/app/data/asset.json']);
    const withoutCache = loadWorker({ fetchImpl: async () => network, cached: undefined });
    assert.equal(await withoutCache.networkFirst(request), network);
    assert.deepEqual(withoutCache.matches, ['https://example.test/app/data/asset.json']);
  }
});
