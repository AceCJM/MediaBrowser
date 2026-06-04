// Service Worker for MediaBrowser PWA
const STATIC_CACHE_NAME = 'mediabrowser-static-v3';

// Assets to cache immediately
const STATIC_ASSETS = [
  '/manifest.json',
  '/static/browser/icon.svg',
  '/static/browser/icon-192.png',
  '/static/browser/icon-512.png',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css',
  'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css'
];

// Install event - cache static assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(STATIC_CACHE_NAME)
      .then(cache => cache.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// Activate event - clean up old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== STATIC_CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

function isStaticAssetRequest(url) {
  if (url.origin === self.location.origin) {
    return url.pathname === '/manifest.json' || url.pathname.startsWith('/static/');
  }

  return url.origin === 'https://cdn.jsdelivr.net';
}

// Fetch event - cache only immutable/static assets.
// Dynamic pages and media routes are always network requests.
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  if (!isStaticAssetRequest(url)) return;

  event.respondWith(
    caches.match(event.request)
      .then(cachedResponse => {
        if (cachedResponse) {
          return cachedResponse;
        }

        return fetch(event.request).then(networkResponse => {
          if (!networkResponse.ok || networkResponse.type !== 'basic' && networkResponse.type !== 'cors') {
            return networkResponse;
          }

          const responseClone = networkResponse.clone();
          caches.open(STATIC_CACHE_NAME).then(cache => {
            cache.put(event.request, responseClone);
          });

          return networkResponse;
        });
      })
      .catch(() => new Response('Offline', { status: 503, statusText: 'Offline' }))
  );
});