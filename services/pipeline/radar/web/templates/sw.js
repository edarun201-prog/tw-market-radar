// 台股市場雷達 service worker（App 模式的離線快取）
// 頁面：先連伺服器拿最新的，連不到才用上次看過時存下的版本，再不行就顯示離線頁。
// 樣式與圖示：先用快取（網址帶內容雜湊，改版就是新網址）。API 不快取，一律連伺服器。
const VERSION = '{{ asset_v }}';
const PAGES = 'radar-pages-v1';
const ASSETS = 'radar-assets-' + VERSION;
const PRECACHE = ['/offline', '/static/style.css?v=' + VERSION, '/static/icon.svg', '/static/icon-192.png'];
const MAX_PAGES = 80;

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(ASSETS).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith('radar-assets-') && k !== ASSETS).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

async function remember(request, response) {
  const cache = await caches.open(PAGES);
  await cache.delete(request);            // 重新放進去，排到最後＝最近看過
  await cache.put(request, response);
  const keys = await cache.keys();
  for (const old of keys.slice(0, Math.max(0, keys.length - MAX_PAGES))) await cache.delete(old);
}

async function fromCache(request) {
  const hit = await caches.match(request, { cacheName: PAGES });
  if (!hit) return (await caches.match('/offline')) || new Response('目前沒有連線。', { status: 503, headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
  // 標記「這是存下來的版本」，頁面會顯示離線提示
  const html = (await hit.text()).replace('<html ', '<html data-cached="1" ');
  return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        const html = (response.headers.get('Content-Type') || '').startsWith('text/html');
        if (response.ok && response.type === 'basic' && html && url.pathname !== '/offline') event.waitUntil(remember(request, response.clone()));
        return response;
      } catch (err) {
        return fromCache(request);
      }
    })());
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const hit = await caches.match(request);
      if (hit) return hit;
      const response = await fetch(request);
      if (response.ok) { const c = await caches.open(ASSETS); await c.put(request, response.clone()); }
      return response;
    })());
  }
});
