import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../../sw.js', import.meta.url), 'utf8');

function loadWorker({ fetchImpl, cached }) {
  const writes = [];
  const cache = {
    match: async () => cached,
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
  return { networkFirst: self.__fundMonitorTest.networkFirst, writes };
}

test('network-first stores and returns a successful data response', async () => {
  const network = { ok: true, status: 200, clone: () => ({ copy: true }) };
  const { networkFirst, writes } = loadWorker({ fetchImpl: async () => network, cached: { cached: true } });
  assert.equal(await networkFirst('request'), network);
  assert.equal(writes.length, 1);
});

test('network-first falls back to cache when fetch rejects', async () => {
  const cached = { ok: true, cached: true };
  const { networkFirst } = loadWorker({ fetchImpl: async () => { throw new Error('offline'); }, cached });
  assert.equal(await networkFirst('request'), cached);
});

test('network-first uses cache for 500/404 and network response when cache is absent', async () => {
  for (const status of [500, 404]) {
    const network = { ok: false, status };
    const cached = { ok: true, cached: status };
    const withCache = loadWorker({ fetchImpl: async () => network, cached });
    assert.equal(await withCache.networkFirst('request'), cached);
    const withoutCache = loadWorker({ fetchImpl: async () => network, cached: undefined });
    assert.equal(await withoutCache.networkFirst('request'), network);
  }
});
