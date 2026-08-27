/* Splitting readings into rounds, on the phone.
 *
 * A port of events.py. Keep the two in step: the rules are stated once, in
 * that file's header, and tests/js/rounds.test.js checks this side against the
 * same cases tests/test_events.py checks the server side against.
 *
 * The field app needs this for the same reason the server does. Its provisional
 * fix was solved from every reading this phone held for the animal in this
 * session — so after a second round it pointed at a blend of two positions and
 * told the observer to walk there.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.PangoRounds = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const GAP_MS = 20 * 60 * 1000;
  const REPEAT_WINDOW_MS = 3 * 60 * 1000;

  const at = (reading) => new Date(reading.time).getTime();

  /** Group readings into rounds, oldest first. */
  function clusterRounds(readings, options) {
    const gap = (options && options.gapMs) || GAP_MS;
    const repeatWindow = (options && options.repeatWindowMs) || REPEAT_WINDOW_MS;

    const ordered = readings.slice().sort((a, b) => at(a) - at(b));
    if (!ordered.length) return [];

    const rounds = [[ordered[0]]];

    for (const reading of ordered.slice(1)) {
      const current = rounds[rounds.length - 1];

      if (at(reading) - at(current[current.length - 1]) > gap) {
        rounds.push([reading]);
        continue;
      }

      if (reading.observer) {
        const earlier = current.filter((r) => r.observer === reading.observer);
        const last = earlier[earlier.length - 1];
        if (last && at(reading) - at(last) > repeatWindow) {
          rounds.push([reading]);
          continue;
        }
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

  return { clusterRounds, latestRound, GAP_MS, REPEAT_WINDOW_MS };
}));
