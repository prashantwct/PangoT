/* PangoT field app.
 *
 * Runs on a phone in a forest with no signal. Everything here assumes the
 * network is absent by default and arrives occasionally, not the reverse.
 */
'use strict';

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Storage
//
// The queue was previously a JSON string in localStorage. Browsers evict that
// under storage pressure and users clear it whenever someone tells them to
// "clear your cache" — losing a day of fieldwork. IndexedDB plus a persistent
// storage request is markedly harder to lose.
// ---------------------------------------------------------------------------

const DB_NAME = 'pangot';
const DB_VERSION = 1;
const QUEUE_STORE = 'queue';
const META_STORE = 'meta';

let dbPromise = null;

function openDb() {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        db.createObjectStore(QUEUE_STORE, { keyPath: 'reading_id' });
      }
      if (!db.objectStoreNames.contains(META_STORE)) {
        db.createObjectStore(META_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return dbPromise;
}

function tx(store, mode, run) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const transaction = db.transaction(store, mode);
    const request = run(transaction.objectStore(store));
    transaction.oncomplete = () => resolve(request && request.result);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  }));
}

const store = {
  all: () => tx(QUEUE_STORE, 'readonly', (s) => s.getAll()),
  put: (record) => tx(QUEUE_STORE, 'readwrite', (s) => s.put(record)),
  remove: (id) => tx(QUEUE_STORE, 'readwrite', (s) => s.delete(id)),
  get: (id) => tx(QUEUE_STORE, 'readonly', (s) => s.get(id)),

  /** Readings not yet accepted by the server. */
  async pending() {
    return (await store.all()).filter((r) => !r.uploaded);
  },

  /**
   * Mark readings as uploaded rather than deleting them.
   *
   * The field team still wants to see what they recorded this session after it
   * has gone up — deleting on success wiped the session log the moment it
   * became useful.
   */
  markUploaded(ids) {
    const at = new Date().toISOString();
    return tx(QUEUE_STORE, 'readwrite', (s) => {
      ids.forEach((id) => {
        const request = s.get(id);
        request.onsuccess = () => {
          if (request.result) s.put({ ...request.result, uploaded: true, uploadedAt: at, error: null });
        };
      });
    });
  },

  /** Drop uploaded readings older than a week, so the store cannot grow forever. */
  async pruneUploaded(maxAgeDays = 7) {
    const cutoff = Date.now() - maxAgeDays * 86400000;
    const stale = (await store.all()).filter(
      (r) => r.uploaded && r.uploadedAt && new Date(r.uploadedAt).getTime() < cutoff,
    );
    for (const record of stale) await store.remove(record.reading_id);
  },
};

const meta = {
  get: (key) => tx(META_STORE, 'readonly', (s) => s.get(key)),
  set: (key, value) => tx(META_STORE, 'readwrite', (s) => s.put(value, key)),
};

/** Move anything left in localStorage by an older build into IndexedDB. */
async function migrateLegacyStorage() {
  const legacyQueue = localStorage.getItem('sync_queue');
  if (legacyQueue) {
    try {
      for (const item of JSON.parse(legacyQueue)) {
        await store.put({
          reading_id: item.reading_id || crypto.randomUUID(),
          group_id: item.group_id,
          pango_id: item.pango_id,
          observer: item.observer,
          lat: item.lat,
          lon: item.lon,
          bearing: item.bearing,
          heading_ref: item.heading_ref || 'unknown',
          accuracy: item.accuracy || null,
          time: item.time,
        });
      }
      localStorage.removeItem('sync_queue');
    } catch (err) {
      console.error('Could not migrate the old queue', err);
    }
  }

  const legacyInitials = localStorage.getItem('observer_initials');
  if (legacyInitials && !(await meta.get('observer'))) {
    await meta.set('observer', legacyInitials);
    localStorage.removeItem('observer_initials');
  }
}

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------

const state = {
  animals: [],
  selectedAnimal: null,
  session: null,        // { code, startedAt }
  observer: '',
  fieldToken: '',
  position: null,       // { lat, lon, accuracy }
  bearing: null,        // { value, ref }
  accuracyLimit: 25,
  lastFixes: {},        // pango_id -> { lat, lon, quality, at }
  editingId: null,
};

const SESSION_ALPHABET = '23456789ABCDEFGHJKMNPQRSTVWXYZ'; // no 0/1/I/L/O/U

function newSessionCode() {
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, (b) => SESSION_ALPHABET[b % SESSION_ALPHABET.length]).join('');
}

// ---------------------------------------------------------------------------
// Geo helpers — enough to answer "which way do I walk?" without the network
// ---------------------------------------------------------------------------

// Shared with the solver so the whole app measures in one frame — see
// static/triangulate.js.
const { distanceMetres, bearingDegrees } = Triangulate;

/**
 * Solve a fix from readings held on this phone.
 *
 * This is what makes "which way do I walk?" answerable with no signal. It is
 * explicitly PROVISIONAL and labelled that way in the UI: it can only see
 * bearings this phone recorded, and it does not apply magnetic declination
 * (there is no World Magnetic Model on the client). The server's fix, which
 * sees both observers and corrects for declination, replaces it after upload.
 */
async function computeLocalFix(animalId) {
  if (!animalId) return null;

  const readings = (await store.all()).filter((r) => (
    r.group_id === state.session.code && r.pango_id === animalId && !r.error
  ));
  if (readings.length < 2) return null;

  try {
    const fix = Triangulate.solve(readings.map((r) => ({
      lat: r.lat,
      lon: r.lon,
      bearingTrue: r.bearing,
    })));
    return { ...fix, source: 'provisional' };
  } catch (err) {
    return { problem: err.message, source: 'provisional' };
  }
}

// ---------------------------------------------------------------------------
// Messages
// ---------------------------------------------------------------------------

const messageTimers = new WeakMap();

function showMessage(element, text, kind = 'info', autoHideMs = 0) {
  element.className = `msg ${kind}`;
  element.textContent = text;
  element.hidden = false;
  clearTimeout(messageTimers.get(element));
  if (autoHideMs) {
    messageTimers.set(element, setTimeout(() => { element.hidden = true; }, autoHideMs));
  }
}

function showMessageHtml(element, html, kind = 'info') {
  element.className = `msg ${kind}`;
  element.replaceChildren(...html);
  element.hidden = false;
  clearTimeout(messageTimers.get(element));
}

function markFieldError(fieldId, message) {
  const input = $(fieldId);
  const wrapper = input.closest('.field') || input.parentElement;
  input.classList.add('invalid');
  wrapper.classList.add('has-error');
  const slot = wrapper.querySelector('.field-error');
  if (slot) slot.textContent = message;
}

function clearFieldErrors() {
  document.querySelectorAll('.field.has-error').forEach((f) => f.classList.remove('has-error'));
  document.querySelectorAll('input.invalid').forEach((i) => i.classList.remove('invalid'));
  $('observer').classList.remove('needs-value');
}

// ---------------------------------------------------------------------------
// Network
// ---------------------------------------------------------------------------

function apiHeaders(extra = {}) {
  return { 'Content-Type': 'application/json', 'X-Field-Token': state.fieldToken, ...extra };
}

function updateOnlineBanner() {
  $('offline-banner').hidden = navigator.onLine;
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

async function loadSession() {
  let session = await meta.get('session');
  if (!session || !session.code) {
    session = { code: newSessionCode(), startedAt: new Date().toISOString() };
    await meta.set('session', session);
  }
  state.session = session;
  renderSession();
}

async function startNewSession() {
  state.session = { code: newSessionCode(), startedAt: new Date().toISOString() };
  await meta.set('session', state.session);
  renderSession();
  renderQueue();
  showMessage($('track-msg'), `New session ${state.session.code}. Share this code with the other observer.`, 'ok', 8000);
}

async function joinSession(code) {
  const clean = (code || '').trim().toUpperCase();
  if (!/^[0-9A-Z]{4,12}$/.test(clean)) {
    return { ok: false, message: 'A session code is 6 characters, like K7M2Q4.' };
  }
  state.session = { code: clean, startedAt: new Date().toISOString(), joined: true };
  await meta.set('session', state.session);
  renderSession();
  renderQueue();
  return { ok: true };
}

function renderSession() {
  const started = new Date(state.session.startedAt);
  $('session-code').textContent = state.session.code;
  $('session-meta').textContent = `${state.session.joined ? 'Joined' : 'Started'} `
    + started.toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// ---------------------------------------------------------------------------
// Animals
// ---------------------------------------------------------------------------

async function loadAnimals() {
  const cached = await meta.get('animals');
  state.animals = Array.isArray(cached) && cached.length ? cached : ['P01', 'P02', 'P03', 'P04'];
  renderAnimals();

  // Nothing to authenticate with yet — the pairing dialog is already open, and
  // a request now would only produce a 401 in the console.
  if (!state.fieldToken) return;

  // Refresh from the server when there is signal; the cache is what the field
  // app actually runs on.
  try {
    const response = await fetch('/get_animals', { headers: apiHeaders() });
    if (response.ok) {
      const animals = await response.json();
      if (Array.isArray(animals) && animals.length) {
        state.animals = animals;
        await meta.set('animals', animals);
        renderAnimals();
      }
    } else if (response.status === 401) {
      showPairingDialog();
    }
  } catch {
    /* offline — the cached list is correct */
  }
}

function renderAnimals() {
  const grid = $('animal-grid');
  grid.replaceChildren(...state.animals.map((id) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pango';
    button.textContent = id;
    button.setAttribute('aria-pressed', String(state.selectedAnimal === id));
    button.addEventListener('click', () => selectAnimal(id));
    return button;
  }));

  const list = $('animal-list');
  list.replaceChildren(...state.animals.map((id) => {
    const row = document.createElement('div');
    row.className = 'queue-item';

    const main = document.createElement('div');
    main.className = 'queue-main';
    const title = document.createElement('div');
    title.className = 'queue-title';
    title.textContent = id;
    main.appendChild(title);

    const actions = document.createElement('div');
    actions.className = 'queue-actions';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn-quiet btn-small';
    remove.textContent = 'Remove';
    remove.addEventListener('click', () => removeAnimal(id));
    actions.appendChild(remove);

    row.append(main, actions);
    return row;
  }));

  if (!state.animals.length) {
    list.replaceChildren(emptyState('No animals yet. Add one above.'));
  }
}

function selectAnimal(id) {
  state.selectedAnimal = id;
  $('selected-animal').textContent = id;
  renderAnimals();
  renderNavigation();
  clearFieldErrors();
}

async function addAnimal() {
  const input = $('new-animal');
  const id = input.value.trim().toUpperCase();
  const messageEl = $('animal-msg');

  if (!id) {
    showMessage(messageEl, 'Type an ID first, for example P17.', 'warn', 5000);
    return;
  }
  if (state.animals.includes(id)) {
    showMessage(messageEl, `${id} is already on the list.`, 'warn', 5000);
    return;
  }

  try {
    const response = await fetch('/add_animal', {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify({ id }),
    });
    const body = await response.json().catch(() => ({}));

    if (response.ok || response.status === 409) {
      // The old version added locally regardless of what the server said, so
      // the two lists silently drifted apart.
      state.animals = [...state.animals, id].sort();
      await meta.set('animals', state.animals);
      renderAnimals();
      input.value = '';
      showMessage(messageEl, body.message || `${id} added.`, 'ok', 4000);
    } else if (response.status === 401) {
      showPairingDialog();
    } else {
      showMessage(messageEl, body.message || 'The server would not accept that ID.', 'error');
    }
  } catch {
    state.animals = [...state.animals, id].sort();
    await meta.set('animals', state.animals);
    renderAnimals();
    input.value = '';
    showMessage(messageEl, `${id} added on this phone. It will reach the server at the next sync.`, 'info', 6000);
  }
}

async function removeAnimal(id) {
  const confirmed = await confirmDialog(
    `Remove ${id}?`,
    'This only hides it on this phone. Readings already recorded for it are kept.',
    'Remove',
  );
  if (!confirmed) return;
  state.animals = state.animals.filter((a) => a !== id);
  if (state.selectedAnimal === id) {
    state.selectedAnimal = null;
    $('selected-animal').textContent = 'none';
  }
  await meta.set('animals', state.animals);
  renderAnimals();
}

// ---------------------------------------------------------------------------
// GPS
//
// The old code called getCurrentPosition with no options, so
// enableHighAccuracy defaulted to false and the browser was free to hand back
// a cached network-derived position — routinely hundreds of metres out, and
// recorded without comment. Every metre of triangulation precision downstream
// of that is fiction.
// ---------------------------------------------------------------------------

let watchId = null;
let watchTimeout = null;

// Stop watching after this long. Long enough to walk to a new spot and take a
// second bearing; short enough that a phone left on Track does not flatten its
// battery before the team gets back to the vehicle.
const WATCH_LIMIT_MS = 5 * 60 * 1000;

function toggleGps() {
  if (watchId !== null) {
    stopGps();
    showMessage($('gps-msg'),
      'Stopped updating. The position above is the last one measured.', 'info', 6000);
    return;
  }
  startGps();
}

function startGps() {
  if (!navigator.geolocation) {
    showMessage($('gps-msg'), 'This device has no location service. Type the coordinates in below.', 'error');
    return;
  }
  if (watchId !== null) return;

  $('gps-btn').textContent = 'Stop updating';
  showMessage($('gps-msg'), 'Locking on to satellites…', 'info');

  clearTimeout(watchTimeout);
  watchTimeout = setTimeout(() => {
    if (watchId === null) return;
    stopGps();
    showMessage($('gps-msg'),
      'Stopped updating to save battery. Tap again if you have moved.', 'info');
  }, WATCH_LIMIT_MS);

  watchId = navigator.geolocation.watchPosition(
    (pos) => {
      const accuracy = Math.round(pos.coords.accuracy);
      state.position = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy };
      $('lat').value = pos.coords.latitude.toFixed(6);
      $('lon').value = pos.coords.longitude.toFixed(6);
      renderAccuracy(accuracy);
      renderNavigation();
    },
    (err) => {
      stopGps();
      const reasons = {
        1: 'Location permission was refused. Turn it on for this site in your browser settings, or type the coordinates in below.',
        2: 'No position available — the sky may be blocked. Move to more open ground or type the coordinates in below.',
        3: 'Timed out waiting for a position. Try again in more open ground.',
      };
      showMessage($('gps-msg'), reasons[err.code] || `Location failed: ${err.message}`, 'error');
    },
    { enableHighAccuracy: true, timeout: 30000, maximumAge: 0 },
  );
}

function stopGps() {
  if (watchId !== null) {
    navigator.geolocation.clearWatch(watchId);
    watchId = null;
  }
  clearTimeout(watchTimeout);
  $('gps-btn').textContent = 'Get my position';
}

function renderAccuracy(accuracy) {
  const limit = state.accuracyLimit;
  const el = $('gps-msg');
  if (accuracy <= limit) {
    showMessage(el, `Position good to ±${accuracy} m. You can stop here or wait for it to tighten.`, 'ok');
  } else if (accuracy <= limit * 4) {
    showMessage(el, `±${accuracy} m and still improving — wait a few seconds. Below ±${limit} m is good enough to save.`, 'warn');
  } else {
    showMessage(el, `±${accuracy} m is too coarse for a fix. Wait, or move to more open ground.`, 'error');
  }
  $('gps-accuracy').textContent = `±${accuracy} m`;
  $('gps-accuracy').className = `chip unit ${accuracy <= limit ? 'good' : accuracy <= limit * 4 ? 'fair' : 'poor'}`;
}

// ---------------------------------------------------------------------------
// Compass
//
// The unbounded-angle approach below is preserved from the original: tracking
// renderAngle as a cumulative value that is never reduced mod 360 is what
// stops the rose flickering as it crosses the 0/360 boundary. It is the right
// solution and it is left intact.
//
// What is added: recording WHICH reference frame the heading came from. iOS
// webkitCompassHeading is true north; Android's absolute alpha is magnetic.
// Storing them as the same number is how two phones side by side end up
// disagreeing by the local declination.
// ---------------------------------------------------------------------------

let compassAttached = false;
let sensorHeading = null;
let headingRef = 'unknown';
let renderAngle = null;
let rafId = null;
let lastFrameTime = null;
const DEG_PER_SEC = 120;

function shortAngleDiff(from, to) {
  return ((to - (from % 360) + 540) % 360) - 180;
}

function drawRose(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const size = 220;
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  canvas.style.width = `${size}px`;
  canvas.style.height = `${size}px`;

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const centre = size / 2;
  const radius = centre - 3;
  const styles = getComputedStyle(document.documentElement);
  const ink = styles.getPropertyValue('--ink').trim();
  const ink2 = styles.getPropertyValue('--ink-2').trim();
  const line = styles.getPropertyValue('--line').trim();
  const surface = styles.getPropertyValue('--surface').trim();
  const danger = styles.getPropertyValue('--danger').trim();

  ctx.clearRect(0, 0, size, size);
  ctx.beginPath();
  ctx.arc(centre, centre, radius, 0, Math.PI * 2);
  ctx.fillStyle = surface;
  ctx.fill();
  ctx.strokeStyle = line;
  ctx.lineWidth = 2;
  ctx.stroke();

  for (let deg = 0; deg < 360; deg += 5) {
    const rad = ((deg - 90) * Math.PI) / 180;
    const isMajor = deg % 30 === 0;
    const isMinor = deg % 10 === 0;
    const length = isMajor ? 16 : isMinor ? 10 : 5;
    ctx.beginPath();
    ctx.moveTo(centre + Math.cos(rad) * (radius - 3), centre + Math.sin(rad) * (radius - 3));
    ctx.lineTo(centre + Math.cos(rad) * (radius - 3 - length), centre + Math.sin(rad) * (radius - 3 - length));
    ctx.strokeStyle = isMajor ? ink : isMinor ? ink2 : line;
    ctx.lineWidth = isMajor ? 2.5 : isMinor ? 1.5 : 1;
    ctx.stroke();
  }

  for (let i = 0; i < 12; i += 1) {
    const deg = i * 30;
    const rad = ((deg - 90) * Math.PI) / 180;
    ctx.save();
    ctx.translate(centre + Math.cos(rad) * (radius - 30), centre + Math.sin(rad) * (radius - 30));
    ctx.font = `bold ${deg % 90 === 0 ? 15 : 12}px ${getComputedStyle(document.body).fontFamily}`;
    ctx.fillStyle = deg === 0 ? danger : ink;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(String(deg), 0, 0);
    ctx.restore();
  }
}

function renderLoop(timestamp) {
  if (!compassAttached) return;
  rafId = requestAnimationFrame(renderLoop);
  if (sensorHeading === null) return;

  if (renderAngle === null) {
    renderAngle = sensorHeading;
  } else if (lastFrameTime !== null) {
    const dt = Math.min((timestamp - lastFrameTime) / 1000, 0.05);
    const diff = shortAngleDiff(renderAngle, sensorHeading);
    const maxStep = DEG_PER_SEC * dt;
    // Never reduced mod 360 — that is what prevents the 0/360 flicker.
    renderAngle += Math.max(-maxStep, Math.min(maxStep, diff));
  }
  lastFrameTime = timestamp;

  $('compass-rose').style.transform = `rotate(${-renderAngle}deg)`;
  $('live-bearing').textContent = String(normalise(renderAngle)).padStart(3, '0');
}

const normalise = (angle) => ((Math.round(angle) % 360) + 360) % 360;

function onOrientation(event) {
  let raw = null;
  if (typeof event.webkitCompassHeading === 'number' && isFinite(event.webkitCompassHeading)) {
    raw = event.webkitCompassHeading;
    headingRef = 'true';        // iOS fuses with location, so this is true north
  } else if (event.alpha !== null && isFinite(event.alpha)) {
    raw = (360 - event.alpha) % 360;
    headingRef = event.absolute ? 'magnetic' : 'unknown';
  }
  if (raw === null) return;
  sensorHeading = raw;
  $('compass-error').hidden = true;
  $('heading-ref').textContent = headingRef === 'true'
    ? 'true north'
    : headingRef === 'magnetic' ? 'magnetic north — corrected on upload' : 'reference unknown';
}

async function startCompass() {
  $('compass-start').hidden = true;
  $('compass').hidden = false;
  drawRose($('compass-canvas'));
  sensorHeading = null;
  renderAngle = null;
  lastFrameTime = null;

  const attach = () => {
    window.addEventListener('deviceorientationabsolute', onOrientation, true);
    window.addEventListener('deviceorientation', onOrientation, true);
    compassAttached = true;
    rafId = requestAnimationFrame(renderLoop);
    setTimeout(() => { if (sensorHeading === null) $('compass-error').hidden = false; }, 3000);
  };

  if (typeof DeviceOrientationEvent !== 'undefined'
      && typeof DeviceOrientationEvent.requestPermission === 'function') {
    try {
      const permission = await DeviceOrientationEvent.requestPermission();
      if (permission === 'granted') attach();
      else $('compass-error').hidden = false;
    } catch {
      $('compass-error').hidden = false;
    }
  } else {
    attach();
  }
}

function stopCompass() {
  if (rafId) cancelAnimationFrame(rafId);
  rafId = null;
  if (compassAttached) {
    window.removeEventListener('deviceorientationabsolute', onOrientation, true);
    window.removeEventListener('deviceorientation', onOrientation, true);
    compassAttached = false;
  }
  sensorHeading = null;
  renderAngle = null;
  lastFrameTime = null;
  $('compass-rose').style.transform = 'rotate(0deg)';
  $('compass').hidden = true;
  $('compass-start').hidden = false;
  $('live-bearing').textContent = '---';
}

function lockBearing() {
  if (renderAngle === null) {
    showMessage($('bearing-msg'), 'The compass has not settled yet. Give it a moment.', 'warn', 4000);
    return;
  }
  const locked = normalise(renderAngle);
  $('bearing').value = locked;
  state.bearing = { value: locked, ref: headingRef };
  showMessage($('bearing-msg'), `Locked ${locked}° (${headingRef === 'true' ? 'true' : 'magnetic'}).`, 'ok', 4000);
  clearFieldErrors();
}

// ---------------------------------------------------------------------------
// Saving a reading
// ---------------------------------------------------------------------------

function readForm() {
  clearFieldErrors();
  const problems = [];

  const observer = $('observer').value.trim().toUpperCase();
  if (!observer) {
    $('observer').classList.add('needs-value');
    problems.push('Your initials — top right');
  }

  if (!state.selectedAnimal) problems.push('Which animal — step 1');

  const lat = parseFloat($('lat').value);
  const lon = parseFloat($('lon').value);
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    markFieldError('lat', 'Latitude must be between -90 and 90.');
    problems.push('Your position — step 2');
  } else if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
    markFieldError('lon', 'Longitude must be between -180 and 180.');
    problems.push('Your position — step 2');
  }

  const bearing = parseFloat($('bearing').value);
  if (!Number.isFinite(bearing) || bearing < 0 || bearing > 360) {
    markFieldError('bearing', 'Bearing must be between 0 and 360.');
    problems.push('The bearing — step 3');
  }

  return { observer, lat, lon, bearing, problems };
}

async function saveReading() {
  const { observer, lat, lon, bearing, problems } = readForm();

  if (problems.length) {
    const heading = document.createElement('div');
    heading.textContent = 'Still needed before this can be saved:';
    const list = document.createElement('ul');
    problems.forEach((problem) => {
      const item = document.createElement('li');
      item.textContent = problem;
      list.appendChild(item);
    });
    showMessageHtml($('track-msg'), [heading, list], 'error');
    return;
  }

  const accuracy = state.position && Math.abs(state.position.lat - lat) < 1e-6
    ? state.position.accuracy
    : null;

  if (accuracy !== null && accuracy > state.accuracyLimit) {
    const proceed = await confirmDialog(
      `Position is only good to ±${accuracy} m`,
      `Below ±${state.accuracyLimit} m gives a usable fix. At ±${accuracy} m the calculated `
      + 'position could be out by more than the animal\'s whole home range. Wait for a better '
      + 'lock if you can.',
      'Save anyway',
    );
    if (!proceed) return;
  }

  const record = {
    reading_id: crypto.randomUUID(),
    group_id: state.session.code,
    pango_id: state.selectedAnimal,
    observer,
    lat,
    lon,
    bearing,
    heading_ref: state.bearing && state.bearing.value === bearing ? state.bearing.ref : 'unknown',
    accuracy,
    time: new Date().toISOString(),
  };

  if (state.editingId) {
    await store.remove(state.editingId);
    state.editingId = null;
  }

  await store.put(record);
  await meta.set('observer', observer);

  $('bearing').value = '';
  state.bearing = null;
  showMessage($('bearing-msg'), '', 'info');
  $('bearing-msg').hidden = true;

  const pending = await store.pending();
  showMessage($('track-msg'),
    `Saved ${record.pango_id} at ${bearing.toFixed(0)}°. ${pending.length} reading${pending.length === 1 ? '' : 's'} waiting to upload.`,
    'ok', 6000);

  renderQueue();
  renderNavigation();
}

// ---------------------------------------------------------------------------
// Queue
// ---------------------------------------------------------------------------

function emptyState(text) {
  const div = document.createElement('div');
  div.className = 'empty';
  div.textContent = text;
  return div;
}

async function renderQueue() {
  const all = (await store.all()).sort((a, b) => new Date(a.time) - new Date(b.time));
  const pending = all.filter((r) => !r.uploaded);

  $('queue-count').textContent = String(pending.length);
  $('nav-queue-badge').textContent = pending.length ? String(pending.length) : '';
  $('nav-queue-badge').hidden = !pending.length;
  $('sync-btn').disabled = pending.length === 0;

  $('queue-list').replaceChildren(
    pending.length
      ? document.createDocumentFragment()
      : emptyState('Nothing waiting. Every reading has been uploaded.'),
  );
  pending.forEach((record) => $('queue-list').appendChild(queueRow(record, true)));

  // The session log keeps uploaded readings too — it is the team's record of
  // what they did, not a to-do list.
  const thisSession = all.filter((r) => r.group_id === state.session.code);
  $('session-readings').replaceChildren(
    thisSession.length
      ? document.createDocumentFragment()
      : emptyState('No readings in this session yet.'),
  );
  thisSession.forEach((record, index) => {
    $('session-readings').appendChild(queueRow(record, false, index + 1));
  });
}

function queueRow(record, withActions, index) {
  const row = document.createElement('div');
  row.className = 'queue-item';

  const main = document.createElement('div');
  main.className = 'queue-main';

  const title = document.createElement('div');
  title.className = 'queue-title';
  title.textContent = index ? `${index}. ${record.pango_id}` : record.pango_id;
  if (record.group_id !== state.session.code) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = record.group_id;
    chip.style.marginLeft = '8px';
    title.appendChild(chip);
  }

  if (record.uploaded) {
    const uploaded = document.createElement('span');
    uploaded.className = 'chip good';
    uploaded.textContent = 'uploaded';
    uploaded.style.marginLeft = '8px';
    title.appendChild(uploaded);
  }

  const detail = document.createElement('div');
  detail.className = 'queue-detail';
  detail.textContent = `${record.bearing.toFixed(0)}° · ${record.lat.toFixed(5)}, ${record.lon.toFixed(5)}`
    + (record.accuracy ? ` · ±${record.accuracy} m` : '');

  main.append(title, detail);

  if (record.error) {
    const problem = document.createElement('div');
    problem.className = 'queue-detail';
    problem.style.color = 'var(--danger)';
    problem.textContent = `Rejected: ${record.error}`;
    main.appendChild(problem);
  }

  row.appendChild(main);

  if (withActions) {
    const actions = document.createElement('div');
    actions.className = 'queue-actions';

    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'btn btn-quiet btn-small';
    edit.textContent = 'Edit';
    edit.addEventListener('click', () => editReading(record.reading_id));

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'btn btn-danger btn-small';
    remove.textContent = 'Delete';
    remove.addEventListener('click', () => deleteReading(record.reading_id));

    actions.append(edit, remove);
    row.appendChild(actions);
  }

  return row;
}

async function editReading(id) {
  const record = await store.get(id);
  if (!record || record.uploaded) return;

  selectAnimal(record.pango_id);
  $('lat').value = record.lat;
  $('lon').value = record.lon;
  $('bearing').value = record.bearing;
  $('observer').value = record.observer;
  state.editingId = id;
  state.bearing = { value: record.bearing, ref: record.heading_ref };

  switchTab('track');
  showMessage($('track-msg'),
    `Editing the reading for ${record.pango_id}. Change what you need and press Save reading — it replaces the original.`,
    'info');
}

async function deleteReading(id) {
  const record = await store.get(id);
  const confirmed = await confirmDialog(
    'Delete this reading?',
    `${record.pango_id} at ${record.bearing.toFixed(0)}°. It has not been uploaded, so this cannot be undone.`,
    'Delete',
  );
  if (!confirmed) return;
  await store.remove(id);
  if (state.editingId === id) state.editingId = null;
  renderQueue();
}

// ---------------------------------------------------------------------------
// Sync
// ---------------------------------------------------------------------------

async function syncNow() {
  const queue = await store.pending();
  if (!queue.length) return;

  const log = $('sync-msg');
  $('sync-btn').disabled = true;
  showMessage(log, `Uploading ${queue.length} reading${queue.length === 1 ? '' : 's'}…`, 'info');

  let response;
  try {
    response = await fetch('/sync', {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify(queue.map(({ error, uploaded, uploadedAt, ...record }) => record)),
    });
  } catch {
    // The old version left "Syncing…" on screen forever here.
    showMessage(log,
      'No connection to the server. Your readings are safe on this phone — try again when you have signal.',
      'warn');
    $('sync-btn').disabled = false;
    return;
  }

  if (response.status === 401) {
    showPairingDialog();
    showMessage(log, 'This phone is not paired with the server yet.', 'error');
    $('sync-btn').disabled = false;
    return;
  }

  const body = await response.json().catch(() => null);

  if (!response.ok || !body) {
    const reference = body && body.reference ? ` Quote reference ${body.reference} when reporting it.` : '';
    showMessage(log,
      (body && body.message)
        ? `${body.message}${reference}`
        : 'The server could not accept the upload. Your readings are still saved on this phone.',
      'error');
    $('sync-btn').disabled = false;
    return;
  }

  // Mark exactly what the server accepted, rather than clearing the whole
  // queue — anything saved while the request was in flight survives.
  const rejectedIds = new Set((body.rejected || []).map((r) => r.reading_id).filter(Boolean));
  await store.markUploaded((body.accepted_ids || []).filter((id) => !rejectedIds.has(id)));

  for (const rejection of body.rejected || []) {
    if (!rejection.reading_id) continue;
    const record = await store.get(rejection.reading_id);
    if (record) await store.put({ ...record, error: rejection.error });
  }

  for (const result of body.results || []) {
    if (result.status === 'fixed') {
      state.lastFixes[result.pango_id] = {
        lat: result.lat,
        lon: result.lon,
        quality: result.quality,
        at: new Date().toISOString(),
      };
    }
  }
  await meta.set('lastFixes', state.lastFixes);

  renderSyncReport(log, body);
  renderQueue();
  renderNavigation();
  $('sync-btn').disabled = false;
}

function renderSyncReport(element, body) {
  const parts = [];

  const summary = document.createElement('div');
  summary.textContent = `Uploaded ${body.stored} reading${body.stored === 1 ? '' : 's'}`
    + (body.duplicates ? `, ${body.duplicates} already on the server` : '')
    + (body.rejected && body.rejected.length ? `, ${body.rejected.length} rejected` : '')
    + '.';
  parts.push(summary);

  if (body.results && body.results.length) {
    const list = document.createElement('ul');
    body.results.forEach((result) => {
      const item = document.createElement('li');
      item.textContent = result.message;
      list.appendChild(item);
    });
    parts.push(list);
  }

  if (body.rejected && body.rejected.length) {
    const list = document.createElement('ul');
    body.rejected.forEach((rejection) => {
      const item = document.createElement('li');
      item.textContent = rejection.error;
      list.appendChild(item);
    });
    parts.push(list);
  }

  const anyPoor = (body.results || []).some((r) => r.quality === 'poor');
  const anyRejected = body.rejected && body.rejected.length;
  showMessageHtml(element, parts, anyRejected ? 'error' : anyPoor ? 'warn' : 'ok');
}

// ---------------------------------------------------------------------------
// Navigate to the last known fix
//
// The question a field team actually has is "which way do I walk?". The old
// app collected the data to answer it and then sent it away — the fix appeared
// only on the dashboard, which needs signal and a login.
// ---------------------------------------------------------------------------

async function renderNavigation() {
  const card = $('nav-card');
  const animal = state.selectedAnimal;

  if (!animal) {
    card.hidden = true;
    return;
  }

  // A server fix is authoritative; fall back to whatever this phone can work
  // out on its own, which is the case that matters in the field.
  const confirmed = state.lastFixes[animal];
  const fix = confirmed || await computeLocalFix(animal);

  if (!fix) {
    card.hidden = true;
    return;
  }

  if (fix.problem) {
    $('nav-title').textContent = `${animal} — cannot place yet`;
    $('nav-distance').textContent = '—';
    $('nav-bearing').textContent = fix.problem;
    $('nav-needle').style.transform = 'rotate(0deg)';
    $('nav-quality').textContent = 'no fix';
    $('nav-quality').className = 'chip poor';
    $('nav-source').textContent = '';
    card.hidden = false;
    return;
  }

  $('nav-title').textContent = `${animal} — calculated position`;
  $('nav-quality').textContent = fix.quality;
  $('nav-quality').className = `chip ${fix.quality}`;
  $('nav-source').textContent = confirmed
    ? 'Confirmed by the server'
    : 'Provisional — from this phone only, not yet uploaded';

  if (!state.position) {
    $('nav-distance').textContent = `${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)}`;
    $('nav-distance').style.fontSize = '1.1rem';
    $('nav-bearing').textContent = 'Get your position to see which way to walk';
    $('nav-needle').style.transform = 'rotate(0deg)';
    card.hidden = false;
    return;
  }

  const range = distanceMetres(state.position.lat, state.position.lon, fix.lat, fix.lon);
  const bearing = bearingDegrees(state.position.lat, state.position.lon, fix.lat, fix.lon);

  $('nav-distance').style.fontSize = '';
  $('nav-distance').textContent = range < 1000
    ? `${Math.round(range)} m`
    : `${(range / 1000).toFixed(2)} km`;
  $('nav-bearing').textContent = `Head ${Math.round(bearing)}° from where you are standing`;
  $('nav-needle').style.transform = `rotate(${bearing}deg)`;
  card.hidden = false;
}

// ---------------------------------------------------------------------------
// Dialogs — replacing alert/confirm/prompt, which are unstyled, block the
// whole page and are dismissible by accident with one thumb.
// ---------------------------------------------------------------------------

function confirmDialog(title, body, confirmLabel) {
  return new Promise((resolve) => {
    const dialog = $('confirm-dialog');
    $('confirm-title').textContent = title;
    $('confirm-body').textContent = body;
    $('confirm-ok').textContent = confirmLabel;

    const finish = (answer) => {
      dialog.close();
      $('confirm-ok').removeEventListener('click', onOk);
      $('confirm-cancel').removeEventListener('click', onCancel);
      resolve(answer);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);

    $('confirm-ok').addEventListener('click', onOk);
    $('confirm-cancel').addEventListener('click', onCancel);
    dialog.showModal();
  });
}

function showPairingDialog() {
  $('pair-dialog').showModal();
}

// ---------------------------------------------------------------------------
// Tabs, night mode, boot
// ---------------------------------------------------------------------------

function switchTab(name) {
  document.querySelectorAll('.page').forEach((page) => {
    page.classList.toggle('active', page.id === `tab-${name}`);
  });
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.setAttribute('aria-selected', String(item.dataset.tab === name));
  });
}

async function setNightMode(on) {
  document.documentElement.dataset.mode = on ? 'night' : 'day';
  localStorage.setItem('night_mode', on ? '1' : '0');
  $('night-toggle').setAttribute('aria-pressed', String(on));
  $('night-toggle').textContent = on ? '☀' : '☾';
  $('night-toggle').setAttribute('aria-label', on ? 'Switch to day mode' : 'Switch to night mode');
  if (compassAttached) drawRose($('compass-canvas'));
}

function registerServiceWorker() {
  // The previous build shipped a complete service worker and never registered
  // it, so the app had no offline capability at all.
  if (!('serviceWorker' in navigator)) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch((err) => {
      console.error('Service worker registration failed', err);
    });
  });
}

async function boot() {
  registerServiceWorker();

  if (navigator.storage && navigator.storage.persist) {
    // Ask the browser not to evict a day of fieldwork under storage pressure.
    navigator.storage.persist().catch(() => {});
  }

  await migrateLegacyStorage();
  await store.pruneUploaded().catch(() => {});

  state.fieldToken = (await meta.get('fieldToken')) || '';
  state.observer = (await meta.get('observer')) || '';
  state.lastFixes = (await meta.get('lastFixes')) || {};
  state.accuracyLimit = (await meta.get('accuracyLimit')) || 25;
  $('observer').value = state.observer;
  $('accuracy-limit').value = state.accuracyLimit;

  await loadSession();
  await loadAnimals();
  await renderQueue();

  setNightMode(localStorage.getItem('night_mode') === '1');
  updateOnlineBanner();

  if (!state.fieldToken) showPairingDialog();

  wireEvents();
}

function wireEvents() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.addEventListener('click', () => switchTab(item.dataset.tab));
  });

  $('night-toggle').addEventListener('click', () => {
    setNightMode(document.documentElement.dataset.mode !== 'night');
  });

  $('observer').addEventListener('change', async () => {
    state.observer = $('observer').value.trim().toUpperCase();
    $('observer').value = state.observer;
    await meta.set('observer', state.observer);
    clearFieldErrors();
  });

  $('session-btn').addEventListener('click', () => $('session-dialog').showModal());
  $('session-close').addEventListener('click', () => $('session-dialog').close());

  $('session-new').addEventListener('click', async () => {
    const confirmed = await confirmDialog(
      'Start a new session?',
      'Only do this for a new animal or a new location. Readings already saved keep their old session and still upload normally.',
      'Start new',
    );
    if (!confirmed) return;
    $('session-dialog').close();
    await startNewSession();
  });

  $('session-join').addEventListener('click', async () => {
    const result = await joinSession($('session-join-code').value);
    if (result.ok) {
      $('session-dialog').close();
      $('session-join-code').value = '';
      showMessage($('track-msg'), `Joined session ${state.session.code}.`, 'ok', 6000);
    } else {
      showMessage($('session-join-msg'), result.message, 'error', 6000);
    }
  });

  $('session-share').addEventListener('click', async () => {
    const text = `PangoT session ${state.session.code}`;
    try {
      if (navigator.share) await navigator.share({ text });
      else {
        await navigator.clipboard.writeText(state.session.code);
        showMessage($('session-join-msg'), 'Session code copied.', 'ok', 4000);
      }
    } catch { /* the user dismissed the share sheet */ }
  });

  $('gps-btn').addEventListener('click', toggleGps);

  $('compass-start').addEventListener('click', startCompass);
  $('compass-stop').addEventListener('click', stopCompass);
  $('compass-lock').addEventListener('click', lockBearing);

  $('bearing').addEventListener('input', () => {
    state.bearing = null;   // typed by hand, so the reference frame is unknown
    clearFieldErrors();
  });

  $('save-btn').addEventListener('click', saveReading);
  $('sync-btn').addEventListener('click', syncNow);
  $('add-animal-btn').addEventListener('click', addAnimal);
  $('new-animal').addEventListener('keydown', (e) => { if (e.key === 'Enter') addAnimal(); });

  $('accuracy-limit').addEventListener('change', async () => {
    const value = Math.max(5, Math.min(500, parseInt($('accuracy-limit').value, 10) || 25));
    state.accuracyLimit = value;
    $('accuracy-limit').value = value;
    await meta.set('accuracyLimit', value);
  });

  $('pair-save').addEventListener('click', async () => {
    const token = $('pair-token').value.trim();
    if (!token) return;
    state.fieldToken = token;
    await meta.set('fieldToken', token);
    $('pair-dialog').close();
    loadAnimals();
  });

  window.addEventListener('online', () => { updateOnlineBanner(); });
  window.addEventListener('offline', updateOnlineBanner);
}

boot().catch((err) => {
  console.error(err);
  document.body.insertAdjacentHTML(
    'afterbegin',
    '<div class="msg error" style="margin:14px">The app could not start on this device. '
    + 'Close and reopen it; if that does not help, report the problem to the project lead.</div>',
  );
});
