const CACHE = 'fund-monitor-v2';
const SHELL = [
  './',
  'index.html',
  'assets/styles.css',
  'assets/core.js',
  'assets/app.js',
  'assets/icon.svg',
  'manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) {
      await cache.put(request, response.clone());
      return response;
    }
    const cached = await cache.match(request);
    if (cached) return cached;
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}

self.__fundMonitorTest = { networkFirst };

async function shellFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    return await fetch(request);
  } catch (error) {
    if (request.mode === 'navigate') {
      const fallback = await caches.match('./');
      if (fallback) return fallback;
    }
    throw error;
  }
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const isDataJson = url.origin === self.location.origin && url.pathname.includes('/data/') && url.pathname.endsWith('.json');
  event.respondWith(isDataJson ? networkFirst(event.request) : shellFirst(event.request));
});
