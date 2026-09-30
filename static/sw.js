// Minimal service worker: makes forest installable (PWA + Android share target).
// Network only - no offline caching, so you never see stale pages.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
