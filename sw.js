/* PangoT service worker.
 *
 * This file existed before but was never registered, so the app had no offline
 * capability at all. It is registered from static/app.js now — if you change
 * anything here, verify with a real airplane-mode test: load the app, turn the
 * radio off, force-quit, reopen. That round trip is the acceptance criterion.
 *
 * Two behaviours matter:
 *
 * 1. The app shell is served stale-while-revalidate rather than cache-first.
 *    Cache-first meant a shipped fix could never reach an installed phone
 *    until someone remembered to bump the cache name.
 * 2. Upload and API requests are never cached or replayed from cache. A stale
 *    "sync succeeded" would be worse than no answer at all.
 */

const VERSION = 'v8';
const SHELL_CACHE = `pango-shell-${VERSION}`;
const TILE_CACHE = 'pango-tiles';

const SHELL_ASSETS = [
  '/',
  '/static/app.css',
  '/static/app.js',
  '/static/triangulate.js',
  '/static/compass.js',
  '/static/rounds.js',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/manifest.json',
];

// Anything that changes server state, whose answer must be current, or which
// is behind a sign-in. Serving a cached /dashboard to a signed-out coordinator
// would show them stale data and no way to tell.
const NEVER_CACHE = [
  '/sync', '/api/', '/get_animals', '/add_animal',
  '/login', '/logout', '/healthz', '/dashboard',
  '/download_csv', '/download_fixes',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(async (cache) => {
      // Added individually: one missing asset must not fail the whole install
      // and leave the app with no offline capability.
      await Promise.all(SHELL_ASSETS.map((asset) => cache.add(asset).catch((err) => {
        console.warn('[sw] could not precache', asset, err);
      })));
    }).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key !== SHELL_CACHE && key !== TILE_CACHE)
          .map((key) => caches.delete(key)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  if (url.origin === self.location.origin && NEVER_CACHE.some((path) => url.pathname.startsWith(path))) {
    return; // straight to the network, and fail honestly if it is not there
  }

  // Map tiles: cache as they are viewed, so ground already covered stays
  // available once the team is out of signal.
  if (url.href.includes('tile.openstreetmap.org') || url.href.includes('api.mapbox.com')) {
    event.respondWith(
      caches.open(TILE_CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        const network = fetch(request)
          .then((response) => {
            if (response.ok) cache.put(request, response.clone());
            return response;
          })
          .catch(() => cached);
        return cached || network;
      }),
    );
    return;
  }

  if (url.origin !== self.location.origin) return;

  // App shell: serve from cache immediately, refresh in the background.
  event.respondWith(
    caches.open(SHELL_CACHE).then(async (cache) => {
      const cached = await cache.match(request, { ignoreSearch: true });
      const network = fetch(request)
        .then((response) => {
          if (response.ok) cache.put(request, response.clone());
          return response;
        })
        .catch(() => null);

      if (cached) {
        event.waitUntil(network);
        return cached;
      }

      const response = await network;
      if (response) return response;

      // Offline and never cached — fall back to the app shell for navigations
      // so the user gets the app rather than the browser's error page.
      if (request.mode === 'navigate') {
        const shell = await cache.match('/');
        if (shell) return shell;
      }
      return new Response('Offline', { status: 503, statusText: 'Offline' });
    }),
  );
});
