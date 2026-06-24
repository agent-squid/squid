const CACHE_NAME = 'squid-pwa-v20260624-065';
const APP_SHELL = [
  '/',
  '/index.html',
  '/style.css?v=20260622-022',
  '/app.js?v=20260624-059',
  '/vendor/highlight.min.js',
  '/vendor/atom-one-dark.min.css',
  '/vendor/marked.min.js',
  '/vendor/qrcode.min.js',
  '/squid.jpg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
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
  if (url.pathname === '/manifest.webmanifest' || url.pathname.startsWith('/icons/')) return;
  if (
    url.pathname.startsWith('/chat')
    || url.pathname.startsWith('/cmd')
    || url.pathname.startsWith('/config')
    || url.pathname.startsWith('/health')
    || url.pathname.startsWith('/history')
    || url.pathname.startsWith('/localfile')
    || url.pathname.startsWith('/search')
    || url.pathname.startsWith('/prompts')
    || url.pathname.startsWith('/processes')
    || url.pathname.startsWith('/queue')
    || url.pathname.startsWith('/quota')
    || url.pathname.startsWith('/stats')
    || url.pathname.startsWith('/topics')
  ) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || caches.match('/index.html'))),
  );
});
