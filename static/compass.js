/* Compass source arbitration and smoothing.
 *
 * WHY THIS IS A SEPARATE FILE
 *
 * The field app listened to two orientation events at once:
 *
 *     window.addEventListener('deviceorientationabsolute', onOrientation, true);
 *     window.addEventListener('deviceorientation', onOrientation, true);
 *
 * On Android Chrome both fire, at roughly 60 Hz each, and they do not mean the
 * same thing. `deviceorientationabsolute` reports alpha against magnetic north.
 * Plain `deviceorientation` reports alpha from the relative rotation vector,
 * whose zero is wherever the device happened to be when the sensor started —
 * an arbitrary offset that also drifts.
 *
 * Both streams were fed into one filter. The needle was therefore being pulled
 * between two different reference frames a hundred times a second, which is
 * what the field team saw as constant flicker. No amount of smoothing fixes
 * that: the average of two frames is not a bearing in either of them.
 *
 * So this module picks ONE source and refuses the others. It is separate from
 * app.js because app.js runs on load and cannot be imported by a test, and this
 * logic is worth testing directly — it has already been got wrong once.
 *
 * Sources, best first:
 *
 *   true      iOS webkitCompassHeading. Fused with location, so true north.
 *   absolute  Magnetic north. Needs declination applied server-side.
 *   relative  No north reference at all. Usable as "how far have I turned",
 *             useless as a bearing. Accepted only when nothing better exists,
 *             and flagged so the UI can say so.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.PangoCompass = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SOURCE_RANK = { relative: 0, absolute: 1, true: 2 };

  const DEFAULTS = {
    // Per sensor event. Phones emit at roughly 60 Hz, so 0.12 settles in about
    // a fifth of a second: fast enough to follow a real turn, slow enough that
    // magnetometer noise does not twitch the needle.
    filterAlpha: 0.12,
    // Movement below this does not move the published heading.
    deadbandDeg: 0.4,
    sampleWindowMs: 1200,
    // Hysteresis. A single threshold makes the steady/moving chip oscillate
    // when the spread sits right on it, which reads as a flickering label even
    // though the compass itself is fine.
    steadyEnterDeg: 3.0,
    steadyExitDeg: 4.5,
    minSamples: 8,
    // If the chosen source stops emitting for this long, allow a worse one.
    // Without it, a phone whose absolute sensor drops out shows a frozen
    // needle for ever while relative events are discarded beside it.
    staleSourceMs: 2000,
  };

  function normalise(deg) {
    return ((deg % 360) + 360) % 360;
  }

  /** Signed difference from -> to, in (-180, 180]. */
  function shortAngleDiff(from, to) {
    return ((to - (from % 360) + 540) % 360) - 180;
  }

  /** Circular mean, in degrees. Averaging angles directly is wrong across the
   *  0/360 seam: 359 and 1 average to 180, pointing the needle backwards. */
  function circularMean(headings) {
    let x = 0;
    let y = 0;
    for (const h of headings) {
      x += Math.sin((h * Math.PI) / 180);
      y += Math.cos((h * Math.PI) / 180);
    }
    return normalise((Math.atan2(x, y) * 180) / Math.PI);
  }

  /** Largest deviation from the circular mean, in degrees. */
  function circularSpread(headings) {
    if (headings.length < 2) return Infinity;
    const mean = circularMean(headings);
    return Math.max(...headings.map((h) => Math.abs(shortAngleDiff(mean, h))));
  }

  /** Which frame is this event in, and what heading does it carry? */
  function classify(event) {
    if (!event) return null;

    if (typeof event.webkitCompassHeading === 'number'
        && isFinite(event.webkitCompassHeading)) {
      return { source: 'true', heading: normalise(event.webkitCompassHeading) };
    }

    if (typeof event.alpha === 'number' && isFinite(event.alpha)) {
      // `deviceorientationabsolute` is absolute by definition; some browsers
      // also set the flag on plain `deviceorientation`.
      const absolute = event.absolute === true
        || event.type === 'deviceorientationabsolute';
      return {
        source: absolute ? 'absolute' : 'relative',
        heading: normalise(360 - event.alpha),
      };
    }

    return null;
  }

  function Compass(options) {
    this.options = Object.assign({}, DEFAULTS, options || {});
    this.reset();
  }

  Compass.prototype.reset = function reset() {
    this.source = null;
    this.lastAcceptedAt = null;
    this._clearFilter();
  };

  Compass.prototype._clearFilter = function clearFilter() {
    this.samples = [];
    this.filterX = null;
    this.filterY = null;
    this.heading = null;
    this.wasSteady = false;
  };

  /** Should an event from `source` be used, given what we are already on? */
  Compass.prototype._arbitrate = function arbitrate(source, now) {
    if (this.source === null) return { accept: true, switched: true };
    if (source === this.source) return { accept: true, switched: false };

    if (SOURCE_RANK[source] > SOURCE_RANK[this.source]) {
      // Something better turned up. Worth the reset — the old frame's samples
      // are in different units, so blending them would reintroduce the bug.
      return { accept: true, switched: true };
    }

    const silence = this.lastAcceptedAt === null
      ? Infinity
      : now - this.lastAcceptedAt;
    if (silence >= this.options.staleSourceMs) {
      return { accept: true, switched: true, reason: 'stale' };
    }

    return { accept: false, reason: 'lower-ranked' };
  };

  Compass.prototype._prune = function prune(now) {
    const cutoff = now - this.options.sampleWindowMs;
    if (this.samples.length && this.samples[0].at < cutoff) {
      this.samples = this.samples.filter((sample) => sample.at >= cutoff);
    }
    return this.samples;
  };

  Compass.prototype._absorb = function absorb(heading, now) {
    this.samples.push({ heading, at: now });
    this._prune(now);

    // Filter as a vector, for the same seam reason as circularMean.
    const rad = (heading * Math.PI) / 180;
    const sx = Math.sin(rad);
    const sy = Math.cos(rad);
    if (this.filterX === null) {
      this.filterX = sx;
      this.filterY = sy;
    } else {
      this.filterX += this.options.filterAlpha * (sx - this.filterX);
      this.filterY += this.options.filterAlpha * (sy - this.filterY);
    }

    const filtered = normalise((Math.atan2(this.filterX, this.filterY) * 180) / Math.PI);
    if (this.heading === null
        || Math.abs(shortAngleDiff(this.heading, filtered)) >= this.options.deadbandDeg) {
      this.heading = filtered;
    }
  };

  /**
   * Feed one orientation event.
   * Returns { accepted, source, heading, switched } — `accepted: false` means
   * the event was from a frame we are not using and was discarded.
   */
  Compass.prototype.handle = function handle(event, now) {
    const reading = classify(event);
    if (!reading) return { accepted: false, reason: 'unreadable' };

    const decision = this._arbitrate(reading.source, now);
    if (!decision.accept) {
      return { accepted: false, reason: decision.reason, source: this.source };
    }
    if (decision.switched) this._clearFilter();

    this.source = reading.source;
    this.lastAcceptedAt = now;
    this._absorb(reading.heading, now);

    return {
      accepted: true,
      switched: Boolean(decision.switched),
      source: this.source,
      heading: this.heading,
    };
  };

  /** Is the compass sitting still enough to take a reading from? */
  Compass.prototype.steadiness = function steadiness(now) {
    const window = this._prune(now);
    if (window.length < this.options.minSamples) {
      this.wasSteady = false;
      return { ready: false, steady: false, spread: null };
    }

    const spread = circularSpread(window.map((sample) => sample.heading));
    const threshold = this.wasSteady
      ? this.options.steadyExitDeg
      : this.options.steadyEnterDeg;
    this.wasSteady = spread <= threshold;

    return { ready: true, steady: this.wasSteady, spread };
  };

  /** The value to record: the average over the window, not whatever instant
   *  the thumb landed on. The press itself often nudges the phone. */
  Compass.prototype.lock = function lock(now) {
    const window = this._prune(now);
    if (!window.length) return null;

    const { steady, spread } = this.steadiness(now);
    return {
      heading: Math.round(circularMean(window.map((s) => s.heading))) % 360,
      count: window.length,
      steady,
      spread,
      source: this.source,
    };
  };

  /** False means the readings are a rotation, not a bearing. */
  Compass.prototype.hasNorthReference = function hasNorthReference() {
    return this.source === 'true' || this.source === 'absolute';
  };

  /** How the reading should be stored, matching the server's heading_ref. */
  Compass.prototype.headingRef = function headingRef() {
    if (this.source === 'true') return 'true';
    if (this.source === 'absolute') return 'magnetic';
    return 'unknown';
  };

  return {
    Compass,
    classify,
    circularMean,
    circularSpread,
    shortAngleDiff,
    normalise,
    SOURCE_RANK,
    DEFAULTS,
  };
}));
