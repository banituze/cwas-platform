/* CWAS service worker. Code (CSS/JS) is always fetched fresh and only falls back to the cache offline;
   fonts and images are cache-first; pages already opened under /app can be read offline. */
const V = "cwas-v5";
const HEAVY = /\.(woff2|png|svg|jpg|jpeg|webp|avif|ico)$/;
self.addEventListener("install", e => { e.waitUntil(caches.open(V).then(c => c.add("/offline")).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
const fresh = r => fetch(r).then(n => { if (n.ok) { const c = n.clone(); caches.open(V).then(x => x.put(r, c)); } return n; });
self.addEventListener("fetch", e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin) return;
  if (u.pathname.startsWith("/static/")) {
    e.respondWith(HEAVY.test(u.pathname) ? caches.match(r).then(m => m || fresh(r)) : fresh(r).catch(() => caches.match(r)));
    return;
  }
  if (r.mode === "navigate" && u.pathname.startsWith("/app")) e.respondWith(fresh(r).catch(() => caches.match(r).then(m => m || caches.match("/offline"))));
});
