// forest service worker: makes the app installable, starts fast, and says "offline" instead of a browser error.
// Never caches API data: only the static libraries and the (data-free) page shells.
const VERSION = 'forest-v5';
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
      url.pathname.endsWith('.svg') || url.pathname.endsWith('.webmanifest')) {
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

// ── notifications (Web Push from the forest server; payload is end-to-end encrypted) ──
self.addEventListener('push', e => {
  let m = {};
  try { m = e.data ? e.data.json() : {}; } catch (err) { m = { title: 'forest', body: e.data ? e.data.text() : '' }; }
  const url = m.url || '/';
  const tasks = new URL(url, location.origin).pathname.startsWith('/tasks');
  e.waitUntil(self.registration.showNotification(m.title || 'forest', {
    body: m.body || '',
    icon: tasks ? '/icons/tasks-192.png' : '/icons/icon-192.png',
    badge: '/icons/badge-96.png',
    tag: m.tag || undefined,            // same tag replaces the older one (e.g. today's digest)
    renotify: !!m.tag,
    // reminders: buzz, and on desktop stay up until dismissed; digests arrive quietly
    requireInteraction: !!m.urgent,
    vibrate: m.urgent ? [200, 100, 200] : undefined,
    silent: false,
    data: { url, actions: m.actions || [] },
    actions: (m.actions || []).slice(0, 2).map(a => ({ action: a.action, title: a.title })),
  }));
});

self.addEventListener('notificationclick', e => {
  const n = e.notification, d = n.data || {};
  n.close();
  const act = (d.actions || []).find(a => a.action === e.action);
  if (act) {   // done / snooze: signed one-time link, no login needed, no window opened
    e.waitUntil(fetch(act.url, { method: 'POST' }).then(r => r.ok ? null :
      self.registration.showNotification('forest: action failed', { body: 'Open the task to change it.', data: { url: d.url } })));
    return;
  }
  e.waitUntil((async () => {
    const target = new URL(d.url || '/', location.origin);
    const same = target.origin === location.origin ? target.href : new URL(target.pathname + target.hash, location.origin).href;
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const app = target.pathname.startsWith('/tasks') ? '/tasks' : '/';
    const w = wins.find(c => new URL(c.url).pathname === app);
    if (w) { await w.focus(); return w.navigate(same).catch(() => {}); }
    return self.clients.openWindow(same);
  })());
});
