const CACHE_NAME = 'pango-v2-static'; // Bump version when you change code
const TILE_CACHE = 'pango-v2-tiles';
const ASSETS = [
  '/',
  '/static/manifest.json', // Ensure this path matches your flask setup, or just use manifest.json if at root
  '/manifest.json',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  '/static/icon.png' // Make sure you actually have an icon or remove this
];

// 1. INSTALL: Cache static app shell (HTML, CSS, JS)
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting(); // Activate new SW immediately
});

// 2. ACTIVATE: Clean up old caches
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME && key !== TILE_CACHE) {
            return caches.delete(key);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// 3. FETCH: The Core Offline Logic
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // A. Handle Map Tiles (OpenStreetMap or Mapbox)
  if (url.href.includes('tile.openstreetmap.org') || url.href.includes('api.mapbox.com/styles')) {
    e.respondWith(
      caches.open(TILE_CACHE).then((cache) => {
        return cache.match(e.request).then((cachedResponse) => {
          // Return cached tile if available, else fetch from net and cache it
          const fetchPromise = fetch(e.request).then((networkResponse) => {
            cache.put(e.request, networkResponse.clone());
            return networkResponse;
          });
          return cachedResponse || fetchPromise;
        });
      })
    );
    return; // Exit, handled
  }

  // B. Handle API Calls (Do not cache /sync or /save)
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/sync')) {
    e.respondWith(fetch(e.request));
    return;
  }

  // C. Handle Static Assets (HTML, JS, CSS) -> Cache First, Fallback to Network
  e.respondWith(
    caches.match(e.request).then((response) => {
      return response || fetch(e.request);
    })
  );
});