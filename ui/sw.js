const CACHE_NAME = 'squid-pwa-v20261123-134';
const APP_SHELL = [
  '/',
  '/index.html',
  '/style.css?v=20261123-134',
  '/app.js?v=20261123-134',
  '/flow-lang.js?v=20261123-134',
  '/vendor/highlight.min.js',
  '/vendor/atom-one-dark.min.css',
  '/vendor/marked.min.js',
  '/vendor/mermaid.min.js',
  '/vendor/qrcode.min.js',
  '/vendor/xterm.min.js',
  '/vendor/xterm.css',
  '/vendor/addon-fit.min.js',
  '/squid.jpg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(async cache => {
        const status = await fetch('/config/features', { cache: 'no-store' });
        if (status.status !== 401) await cache.addAll(APP_SHELL);
      })
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        .filter((key) => key !== CACHE_NAME)
        .map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  // Keep the installed HTML and its assets on one version. Check for an
  // expired session when online, and keep the installed shell usable offline.
  if (request.mode === 'navigate' && ['/', '/index.html'].includes(url.pathname)) {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      const shell = await cache.match('/');
      try {
        if (shell) {
          const status = await fetch('/config/features', {
            cache: 'no-store', signal: AbortSignal.timeout(1500),
          });
          if (status.status === 401) return Response.redirect(new URL('/instance/pair', url), 303);
          return shell;
        }
        const response = await fetch(request, { signal: AbortSignal.timeout(5000) });
        if (response.ok && !response.redirected) {
          event.waitUntil(cache.addAll(APP_SHELL).catch(() => {}));
        }
        return response;
      } catch {
        return shell || Response.error();
      }
    })());
    return;
  }
  if (request.mode === 'navigate') return;
  if (url.pathname === '/manifest.webmanifest' || url.pathname.startsWith('/icons/')) return;
  if (
    url.pathname.startsWith('/chat')
    || url.pathname.startsWith('/bookmarks')
    || url.pathname.startsWith('/cmd')
    || url.pathname.startsWith('/config')
    || url.pathname.startsWith('/health')
    || url.pathname.startsWith('/history')
    || url.pathname.startsWith('/localfile')
    || url.pathname.startsWith('/search')
    || url.pathname.startsWith('/prompts')
    || url.pathname.startsWith('/processes')
    || url.pathname.startsWith('/queue')
    || url.pathname.startsWith('/watch')
    || url.pathname.startsWith('/quota')
    || url.pathname.startsWith('/stats')
    || url.pathname.startsWith('/topics')
  ) return;

  // Only cache known shell assets. API failures must never receive HTML.
  if (!APP_SHELL.includes(url.pathname + url.search)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) return cached;
    return fetch(request);
  })());
});
