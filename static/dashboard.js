/* PangoT mission control.
 *
 * Every card in here is built with createElement and textContent. The previous
 * version interpolated fix data straight into an innerHTML string that also
 * carried an inline onclick handler, so a note containing an apostrophe broke
 * the Edit button and a crafted note ran as script in the coordinator's
 * session.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const CONFIG = window.PANGOT_CONFIG;

const REFRESH_MS = 30000;
// Typical aim error with a hand-held directional antenna. Used only to draw an
// honest uncertainty ring — it is an estimate, and labelled as one.
const BEARING_ERROR_DEG = 3;

const state = {
  raw: [],
  fixes: [],
  totals: { raw: 0, fixes: 0 },
  selectedId: null,
  hasFitBounds: false,
  undo: null,
  persistentBanner: null,
};

// --- map -------------------------------------------------------------------
//
// The map is treated as optional. If Leaflet or the tile server is
// unreachable, the fix list, filters and CSV exports must still work — a
// coordinator with no map is inconvenienced, one with no data is stuck.

const hasMap = typeof L !== 'undefined';
let map = null;
let bearingLayer = null;
let fixLayer = null;

if (hasMap) {
  map = L.map('map', { zoomControl: true }).setView([20.0, 78.0], 5);

  const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '© OpenStreetMap contributors',
    maxZoom: 19,
  });

  const baseLayers = { 'Street map': osm };

  if (CONFIG.mapboxToken) {
    const satellite = L.tileLayer(
      `https://api.mapbox.com/styles/v1/mapbox/satellite-v9/tiles/{z}/{x}/{y}?access_token=${CONFIG.mapboxToken}`,
      { attribution: '© Mapbox © OpenStreetMap', tileSize: 512, zoomOffset: -1, maxZoom: 20 },
    );
    satellite.addTo(map);
    baseLayers.Satellite = satellite;
  } else {
    osm.addTo(map);
  }
  L.control.layers(baseLayers).addTo(map);

  // Data lives in its own layer groups so redraws never touch the tile layers.
  bearingLayer = L.layerGroup().addTo(map);
  fixLayer = L.layerGroup().addTo(map);
}

// --- helpers ---------------------------------------------------------------

const isValidCoord = (lat, lon) => (
  Number.isFinite(lat) && Number.isFinite(lon)
  && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
);

function localTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString([], {
    day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

const R_EARTH = 6371008.8;
const toRad = (d) => (d * Math.PI) / 180;

function distanceMetres(lat1, lon1, lat2, lon2) {
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2
    + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * R_EARTH * Math.asin(Math.min(1, Math.sqrt(a)));
}

function projectFrom(lat, lon, bearing, metres) {
  const angular = metres / R_EARTH;
  const rLat = toRad(lat);
  const rBrng = toRad(bearing);
  const lat2 = Math.asin(
    Math.sin(rLat) * Math.cos(angular) + Math.cos(rLat) * Math.sin(angular) * Math.cos(rBrng),
  );
  const lon2 = toRad(lon) + Math.atan2(
    Math.sin(rBrng) * Math.sin(angular) * Math.cos(rLat),
    Math.cos(angular) - Math.sin(rLat) * Math.sin(lat2),
  );
  return [(lat2 * 180) / Math.PI, (((lon2 * 180) / Math.PI + 540) % 360) - 180];
}

/** Observers that contributed to a fix, from the raw bearings we hold. */
function observersFor(fix) {
  return state.raw.filter((r) => r.group_id === fix.group_id && r.pango_id === fix.pango_id);
}

/**
 * Estimated uncertainty radius in metres.
 *
 * A 12-pixel dot asserts a precision the data does not have. The ring is a
 * geometric estimate: a few degrees of aim error, spread over the observing
 * range, and amplified when the bearings cross at a shallow angle.
 */
function uncertaintyRadius(fix) {
  const observers = observersFor(fix);
  const ranges = observers
    .filter((r) => isValidCoord(r.obs_lat, r.obs_lon))
    .map((r) => distanceMetres(r.obs_lat, r.obs_lon, fix.calc_lat, fix.calc_lon));

  const range = ranges.length ? ranges.reduce((a, b) => a + b, 0) / ranges.length : null;
  const crossing = fix.crossing_angle_deg || 90;
  const geometric = range
    ? (range * Math.tan(toRad(BEARING_ERROR_DEG))) / Math.max(0.15, Math.sin(toRad(crossing)))
    : null;

  const candidates = [fix.rms_error_m, geometric].filter((v) => Number.isFinite(v) && v > 0);
  if (!candidates.length) return null;
  return Math.min(Math.max(...candidates), 5000);
}

const QUALITY_COLOUR = {
  good: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
  fair: getComputedStyle(document.documentElement).getPropertyValue('--warn').trim(),
  poor: getComputedStyle(document.documentElement).getPropertyValue('--danger').trim(),
  unknown: getComputedStyle(document.documentElement).getPropertyValue('--line-firm').trim(),
};

const qualityOf = (fix) => (QUALITY_COLOUR[fix.quality] ? fix.quality : 'unknown');

// --- data ------------------------------------------------------------------

async function loadData({ quiet = false } = {}) {
  if (!quiet) setBanner('info', 'Loading…');

  try {
    const response = await fetch('/api/data?limit=2000', { headers: { Accept: 'application/json' } });

    if (response.status === 401) {
      window.location.href = '/login?next=/dashboard';
      return;
    }
    if (!response.ok) throw new Error(`Server returned ${response.status}`);

    const body = await response.json();
    state.raw = body.raw || [];
    state.fixes = body.fixes || [];
    state.totals = body.totals || { raw: state.raw.length, fixes: state.fixes.length };

    populateFilters();
    render();

    if (body.truncated) {
      setBanner('warn',
        `Showing the most recent ${state.fixes.length} of ${state.totals.fixes} fixes. `
        + 'Narrow the filters to see older records.');
    } else {
      clearBanner();
    }
  } catch (err) {
    console.error(err);
    setBanner('error', 'Could not load the data.', 'Retry', () => loadData());
  }
}

function setBanner(kind, text, actionLabel, onAction) {
  const banner = $('banner');
  banner.className = `banner ${kind}`;
  banner.replaceChildren();

  const message = document.createElement('span');
  message.textContent = text;
  banner.appendChild(message);

  if (actionLabel) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'linkbtn';
    button.style.borderColor = 'currentColor';
    button.textContent = actionLabel;
    button.addEventListener('click', onAction);
    banner.appendChild(button);
  }
  banner.hidden = false;
}

function clearBanner() {
  // A persistent warning (no map library) outlives a successful data load.
  if (state.persistentBanner) {
    setBanner(state.persistentBanner.kind, state.persistentBanner.text);
    return;
  }
  $('banner').hidden = true;
}

// --- filters ---------------------------------------------------------------

function populateFilters() {
  const animals = new Set();
  const observers = new Set();
  state.raw.forEach((r) => {
    if (r.pango_id) animals.add(r.pango_id);
    if (r.observer) observers.add(r.observer);
  });
  state.fixes.forEach((f) => { if (f.pango_id) animals.add(f.pango_id); });

  fillSelect($('filter-animal'), animals);
  fillSelect($('filter-observer'), observers);
}

function fillSelect(select, values) {
  const previous = select.value;
  while (select.options.length > 1) select.remove(1);
  [...values].sort().forEach((value) => select.add(new Option(value, value)));
  if ([...values].includes(previous)) select.value = previous;
}

function filtered() {
  const animal = $('filter-animal').value;
  const observer = $('filter-observer').value;
  const date = $('filter-date').value;
  const quality = $('filter-quality').value;

  const raw = state.raw.filter((r) => (
    (animal === 'all' || r.pango_id === animal)
    && (observer === 'all' || r.observer === observer)
    && (!date || (r.timestamp || '').startsWith(date))
  ));

  const observerGroups = new Set(raw.map((r) => `${r.group_id}|${r.pango_id}`));

  const fixes = state.fixes.filter((f) => (
    (animal === 'all' || f.pango_id === animal)
    && (!date || (f.timestamp || '').startsWith(date))
    && (quality === 'all' || qualityOf(f) === quality)
    && (observer === 'all' || observerGroups.has(`${f.group_id}|${f.pango_id}`))
  ));

  return { raw, fixes };
}

// --- render ----------------------------------------------------------------

function render() {
  const { raw, fixes } = filtered();

  const valid = fixes.filter((f) => isValidCoord(f.calc_lat, f.calc_lon));
  const invalid = fixes.filter((f) => !isValidCoord(f.calc_lat, f.calc_lon));

  let bounds = null;
  if (hasMap) {
    bearingLayer.clearLayers();
    fixLayer.clearLayers();
    drawBearings(raw, valid);
    bounds = drawFixes(valid);
  }
  renderSidebar(valid, invalid);

  $('status').replaceChildren();
  const status = document.createElement('span');
  status.innerHTML = '';
  status.textContent = `${valid.length} fixes · ${raw.length} bearings`
    + (invalid.length ? ` · ${invalid.length} invalid` : '')
    + (state.totals.fixes > fixes.length ? ` (of ${state.totals.fixes})` : '');
  $('status').appendChild(status);

  // Fit once on first load. Refitting on every filter change or auto-refresh
  // throws away the pan and zoom the coordinator just set.
  if (hasMap && !state.hasFitBounds && bounds && bounds.isValid()) {
    map.fitBounds(bounds, { padding: [60, 60], maxZoom: 15 });
    state.hasFitBounds = true;
  }
}

function drawBearings(raw, fixes) {
  raw.forEach((r) => {
    const lat = Number(r.obs_lat);
    const lon = Number(r.obs_lon);
    if (!isValidCoord(lat, lon)) return;

    const fix = fixes.find((f) => f.group_id === r.group_id && f.pango_id === r.pango_id);
    // Draw the ray to the fix it contributed to, rather than a fixed 2 km that
    // may stop short of the fix or run well past it.
    const length = fix
      ? Math.max(distanceMetres(lat, lon, fix.calc_lat, fix.calc_lon) * 1.15, 300)
      : 2000;

    const bearing = Number(r.bearing_true ?? r.bearing);
    L.polyline([[lat, lon], projectFrom(lat, lon, bearing, length)], {
      color: '#2196a5', weight: 1.5, dashArray: '6,6', opacity: 0.7, interactive: false,
    }).addTo(bearingLayer);

    L.circleMarker([lat, lon], {
      radius: 5, color: '#2196a5', weight: 2, fillColor: '#fff', fillOpacity: 1,
    })
      .bindPopup(observerPopup(r))
      .addTo(bearingLayer);
  });
}

function observerPopup(r) {
  const wrap = document.createElement('div');
  const title = document.createElement('b');
  title.textContent = `Observer ${r.observer || '—'}`;
  wrap.appendChild(title);

  const lines = [
    `${r.pango_id} · ${r.group_id}`,
    `Bearing ${Number(r.bearing).toFixed(1)}° (${r.heading_ref || 'unknown'})`,
    r.declination_deg ? `Declination applied ${Number(r.declination_deg).toFixed(2)}°` : null,
    r.gps_accuracy ? `GPS ±${Math.round(r.gps_accuracy)} m` : 'GPS accuracy not recorded',
    localTime(r.timestamp),
  ].filter(Boolean);

  lines.forEach((line) => {
    wrap.appendChild(document.createElement('br'));
    wrap.appendChild(document.createTextNode(line));
  });
  return wrap;
}

function drawFixes(fixes) {
  const points = [];

  fixes.forEach((fix) => {
    const quality = qualityOf(fix);
    const colour = QUALITY_COLOUR[quality];
    const radius = uncertaintyRadius(fix);

    if (radius) {
      L.circle([fix.calc_lat, fix.calc_lon], {
        radius,
        color: colour,
        weight: 1,
        opacity: 0.55,
        fillColor: colour,
        fillOpacity: 0.12,
        interactive: false,
      }).addTo(fixLayer);
    }

    const marker = L.circleMarker([fix.calc_lat, fix.calc_lon], {
      radius: 7, color: '#fff', weight: 2, fillColor: colour, fillOpacity: 1,
    }).addTo(fixLayer);

    marker.bindPopup(fixPopup(fix, radius));
    marker.on('click', () => select(fix.id, false));
    points.push([fix.calc_lat, fix.calc_lon]);
  });

  return points.length ? L.latLngBounds(points) : null;
}

function fixPopup(fix, radius) {
  const wrap = document.createElement('div');
  const title = document.createElement('b');
  title.textContent = fix.pango_id;
  wrap.appendChild(title);

  const lines = [
    `${fix.n_bearings || '?'} bearings · quality ${fix.quality || 'unknown'}`,
    fix.crossing_angle_deg != null ? `Crossing angle ${fix.crossing_angle_deg.toFixed(0)}°` : null,
    fix.rms_error_m != null ? `RMS residual ${fix.rms_error_m.toFixed(0)} m` : 'Two-line fix — no residual',
    radius ? `Estimated uncertainty ±${Math.round(radius)} m` : null,
    localTime(fix.timestamp),
  ].filter(Boolean);

  lines.forEach((line) => {
    wrap.appendChild(document.createElement('br'));
    wrap.appendChild(document.createTextNode(line));
  });
  return wrap;
}

function renderSidebar(valid, invalid) {
  const list = $('fix-list');
  list.replaceChildren();

  if (!valid.length && !invalid.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'No fixes match these filters.';
    list.appendChild(empty);
    return;
  }

  [...valid, ...invalid].forEach((fix) => list.appendChild(fixCard(fix)));
}

function fixCard(fix) {
  const hasCoords = isValidCoord(fix.calc_lat, fix.calc_lon);
  const quality = qualityOf(fix);

  const card = document.createElement('article');
  card.className = `fix q-${quality}`;
  card.setAttribute('aria-current', String(state.selectedId === fix.id));
  card.tabIndex = 0;

  const head = document.createElement('div');
  head.className = 'fix-head';

  const id = document.createElement('span');
  id.className = 'fix-id';
  id.textContent = fix.pango_id;

  const chip = document.createElement('span');
  chip.className = `chip ${quality}`;
  chip.textContent = quality;

  const session = document.createElement('span');
  session.className = 'fix-session';
  session.textContent = fix.group_id;

  head.append(id, chip, session);

  const when = document.createElement('div');
  when.className = 'fix-when';
  when.textContent = localTime(fix.timestamp);

  const coords = document.createElement('div');
  coords.className = 'fix-coords';
  if (hasCoords) {
    const radius = uncertaintyRadius(fix);
    coords.textContent = `${fix.calc_lat.toFixed(5)}, ${fix.calc_lon.toFixed(5)}`
      + (radius ? `  ±${Math.round(radius)} m` : '');
  } else {
    coords.textContent = 'Invalid coordinates — not shown on the map';
    coords.style.color = 'var(--danger)';
  }

  const note = document.createElement('div');
  note.className = 'fix-note';
  note.textContent = fix.note || '';

  card.append(head, when, coords, note);
  card.appendChild(cardActions(fix, hasCoords));

  const activate = () => select(fix.id, hasCoords);
  card.addEventListener('click', (event) => {
    if (event.target.closest('button')) return;
    activate();
  });
  card.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      activate();
    }
  });

  return card;
}

function cardActions(fix, hasCoords) {
  const actions = document.createElement('div');
  actions.className = 'fix-actions';

  if (hasCoords) {
    const zoom = document.createElement('button');
    zoom.type = 'button';
    zoom.className = 'linkbtn';
    zoom.style.cssText = 'border-color:var(--line-firm);background:var(--surface-2);color:var(--ink)';
    zoom.textContent = 'Zoom to';
    zoom.disabled = !hasMap;
    zoom.addEventListener('click', () => map.flyTo([fix.calc_lat, fix.calc_lon], 16));
    actions.appendChild(zoom);
  }

  const edit = document.createElement('button');
  edit.type = 'button';
  edit.className = 'linkbtn';
  edit.style.cssText = 'border-color:var(--line-firm);background:var(--surface-2);color:var(--ink)';
  edit.textContent = 'Edit';
  edit.addEventListener('click', () => openEditor(fix));

  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'linkbtn';
  remove.style.cssText = 'border-color:var(--danger);background:var(--danger-soft);color:var(--danger)';
  remove.textContent = 'Delete';
  remove.addEventListener('click', () => deleteFix(fix));

  actions.append(edit, remove);
  return actions;
}

function select(id, zoom) {
  state.selectedId = id;
  document.querySelectorAll('.fix').forEach((card) => card.setAttribute('aria-current', 'false'));
  render();
  if (zoom && hasMap) {
    const fix = state.fixes.find((f) => f.id === id);
    if (fix && isValidCoord(fix.calc_lat, fix.calc_lon)) map.flyTo([fix.calc_lat, fix.calc_lon], 16);
  }
}

// --- mutations -------------------------------------------------------------

function apiFetch(url, options = {}) {
  return fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': CONFIG.csrfToken,
      ...(options.headers || {}),
    },
  });
}

async function deleteFix(fix) {
  const response = await apiFetch(`/api/fix/${fix.id}`, { method: 'DELETE' });
  if (!response.ok) {
    setBanner('error', 'Could not delete that fix.');
    return;
  }
  await loadData({ quiet: true });
  showUndo(`Deleted the ${fix.pango_id} fix.`, async () => {
    await apiFetch(`/api/fix/${fix.id}/restore`, { method: 'POST' });
    await loadData({ quiet: true });
  });
}

function showUndo(text, onUndo) {
  const toast = $('toast');
  $('toast-text').textContent = text;
  toast.hidden = false;
  clearTimeout(state.undo);
  state.undo = setTimeout(() => { toast.hidden = true; }, 12000);

  const button = $('toast-undo');
  const handler = async () => {
    button.removeEventListener('click', handler);
    toast.hidden = true;
    clearTimeout(state.undo);
    await onUndo();
  };
  button.addEventListener('click', handler);
}

function openEditor(fix) {
  $('edit-animal').value = fix.pango_id;
  $('edit-note').value = fix.note || '';
  $('edit-error').hidden = true;
  $('edit-dialog').dataset.fixId = fix.id;
  $('edit-dialog').showModal();
}

async function saveEdit() {
  const id = $('edit-dialog').dataset.fixId;
  const response = await apiFetch(`/api/fix/${id}`, {
    method: 'POST',
    body: JSON.stringify({ pango_id: $('edit-animal').value.trim(), note: $('edit-note').value.trim() }),
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    $('edit-error').textContent = body.message || 'Could not save that change.';
    $('edit-error').hidden = false;
    return;
  }
  $('edit-dialog').close();
  loadData({ quiet: true });
}

// --- boot ------------------------------------------------------------------

['filter-animal', 'filter-observer', 'filter-date', 'filter-quality'].forEach((id) => {
  $(id).addEventListener('change', render);
});

$('filter-clear').addEventListener('click', () => {
  $('filter-animal').value = 'all';
  $('filter-observer').value = 'all';
  $('filter-quality').value = 'all';
  $('filter-date').value = '';
  render();
});

$('zoom-all').addEventListener('click', () => {
  state.hasFitBounds = false;
  render();
});

$('refresh').addEventListener('click', () => loadData());
$('edit-save').addEventListener('click', saveEdit);
$('edit-cancel').addEventListener('click', () => $('edit-dialog').close());

if (!hasMap) {
  document.getElementById('map').style.display = 'none';
  state.persistentBanner = {
    kind: 'warn',
    text: 'The map library did not load, so the map is hidden. The fix list, filters and CSV exports still work.',
  };
  setBanner(state.persistentBanner.kind, state.persistentBanner.text);
}

loadData();
setInterval(() => loadData({ quiet: true }), REFRESH_MS);
