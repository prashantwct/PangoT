const CACHE_NAME = 'pango-v3-static';
const TILE_CACHE = 'pango-v3-tiles';

// Core assets to keep the app running offline
const ASSETS = [
  '/',
  '/manifest.json',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'
];

// 1. INSTALL: Cache static app shell (HTML, CSS, JS)
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting(); // Force this new SW to become active immediately
});

// 2. ACTIVATE: Clean up old caches from previous versions
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          // Delete old caches that don't match the current version
          if (key !== CACHE_NAME && key !== TILE_CACHE) {
            return caches.delete(key);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// 3. FETCH: The Offline Logic
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // A. Dynamic Caching for Map Tiles (Save maps as you view them)
  if (url.href.includes('tile.openstreetmap.org') || url.href.includes('api.mapbox.com/styles')) {
    e.respondWith(
      caches.open(TILE_CACHE).then((cache) => {
        return cache.match(e.request).then((cachedResponse) => {
          // Return cached tile if available, else fetch from network and cache it
          const fetchPromise = fetch(e.request).then((networkResponse) => {
            cache.put(e.request, networkResponse.clone());
            return networkResponse;
          });
          return cachedResponse || fetchPromise;
        });
      })
    );
    return;
  }

  // B. Network Only for API calls (Never cache /sync or /save requests)
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/sync')) {
    e.respondWith(fetch(e.request));
    return;
  }

  // C. Cache First for everything else (HTML, JS, CSS)
  e.respondWith(
    caches.match(e.request).then((response) => {
      return response || fetch(e.request);
    })
  );
});