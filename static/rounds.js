/* Splitting readings into rounds, on the phone.
 *
 * A port of events.py. Keep the two in step: the rules are stated once, in
 * that file's header — including why there is no rule about the same observer
 * appearing twice — and tests/js/rounds.test.js checks this side against the
 * same cases tests/test_events.py checks the server side against.
 *
 * The field app needs this for the same reason the server does. Its provisional
 * fix was solved from every reading this phone held for the animal in this
 * session — so after a second round it pointed at a blend of two positions and
 * told the observer to walk there.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./triangulate.js'));
  } else {
    root.PangoRounds = factory(root.Triangulate);
  }
}(typeof self !== 'undefined' ? self : this, function (Triangulate) {
  'use strict';

  const GAP_MS = 20 * 60 * 1000;
  // Matches events.DEFAULT_SAME_SPOT_M and triangulation.MIN_BASELINE_M.
  const SAME_SPOT_M = 25;

  const at = (reading) => new Date(reading.time).getTime();

  /** Metres between two readings, or null if either has no position. */
  function apart(a, b) {
    if (a.lat == null || a.lon == null || b.lat == null || b.lon == null) return null;
    return Triangulate.distanceMetres(a.lat, a.lon, b.lat, b.lon);
  }

  function sameSpot(a, b, tolerance) {
    const d = apart(a, b);
    return d !== null && d <= tolerance;
  }

  /** Group readings into rounds, oldest first. */
  function clusterRounds(readings, options) {
    const gap = (options && options.gapMs) || GAP_MS;
    const sameSpotM = (options && options.sameSpotM) || SAME_SPOT_M;

    const ordered = readings.slice().sort((a, b) => at(a) - at(b));
    if (!ordered.length) return [];

    const rounds = [[ordered[0]]];

    for (const reading of ordered.slice(1)) {
      const current = rounds[rounds.length - 1];

      if (at(reading) - at(current[current.length - 1]) > gap) {
        rounds.push([reading]);
        continue;
      }

      // A station occupied twice means the next round has begun, however
      // little time has passed. Deliberately not filtered by observer: two
      // teams share a login, so the name says nothing about who stood where.
      if (current.some((r) => sameSpot(reading, r, sameSpotM))) {
        rounds.push([reading]);
        continue;
      }

      current.push(reading);
    }

    return rounds;
  }

  /** The round in progress — the only one worth walking towards. */
  function latestRound(readings) {
    const rounds = clusterRounds(readings);
    return rounds.length ? rounds[rounds.length - 1] : [];
  }

  return { clusterRounds, latestRound, GAP_MS, SAME_SPOT_M };
}));
