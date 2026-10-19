// forest service worker: makes the app installable, starts fast, and says "offline" instead of a browser error.
// Never caches API data: only the static libraries and the (data-free) page shells.
const VERSION = 'forest-v2';
const SHELLS = ['/', '/tasks', '/admin', '/clip'];
const SKIP = ['/api/', '/mcp', '/git/', '/auth/', '/oauth/', '/upload/', '/.well-known/', '/healthz', '/sw.js'];

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k !== VERSION) await caches.delete(k);
  await self.clients.claim();
})()));

const OFFLINE = `<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">
<title>forest - offline</title><body style="font:14px Menlo,Consolas,monospace;padding:30px;text-align:center">
<p>forest is offline.</p><p style="color:#666">The server can't be reached right now.</p>
<p><button onclick="location.reload()" style="font:inherit;padding:8px 16px">retry</button></p></body>`;

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin || SKIP.some(p => url.pathname.startsWith(p))) return;

  // static assets: serve from cache instantly, refresh in the background
  if (url.pathname.startsWith('/lib/') || url.pathname.startsWith('/icons/') ||
      url.pathname === '/icon.svg' || url.pathname === '/manifest.webmanifest') {
    e.respondWith((async () => {
      const cache = await caches.open(VERSION);
      const hit = await cache.match(req);
      const fresh = fetch(req).then(r => { if (r.ok) cache.put(req, r.clone()); return r; }).catch(() => hit);
      return hit || fresh;
    })());
    return;
  }

  // page shells: network first (always current), cached copy or offline page when unreachable
  if (req.mode === 'navigate' && SHELLS.includes(url.pathname)) {
    e.respondWith((async () => {
      const cache = await caches.open(VERSION);
      try {
        const r = await fetch(req);
        if (r.ok && !r.redirected) cache.put(url.pathname, r.clone());   // never cache the login redirect
        return r;
      } catch (err) {
        return (await cache.match(url.pathname)) ||
          new Response(OFFLINE, { headers: { 'Content-Type': 'text/html; charset=utf-8' } });
      }
    })());
  }
});
