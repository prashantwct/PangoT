/* Tests for compass source arbitration.
 *
 * The bug these exist for: the app listened to `deviceorientationabsolute` and
 * `deviceorientation` at the same time. On Android both fire at ~60 Hz, in two
 * different reference frames, and both were fed into one filter. The needle
 * was dragged between the frames continuously, which the field team reported
 * as constant flicker.
 */
const test = require('node:test');
const assert = require('node:assert');

const C = require('../../static/compass.js');

/** An Android absolute event: alpha referenced to magnetic north. */
function absolute(headingDeg) {
  return { type: 'deviceorientationabsolute', absolute: true, alpha: (360 - headingDeg) % 360 };
}

/** An Android relative event: alpha from an arbitrary zero. */
function relative(headingDeg) {
  return { type: 'deviceorientation', absolute: false, alpha: (360 - headingDeg) % 360 };
}

/** An iOS event: true north, already a heading. */
function ios(headingDeg) {
  return { type: 'deviceorientation', webkitCompassHeading: headingDeg, alpha: 123 };
}

/** Feed n events at 60 Hz starting at t0, returning the clock afterwards. */
function feed(compass, makeEvent, count, t0 = 0) {
  let t = t0;
  for (let i = 0; i < count; i += 1) {
    compass.handle(makeEvent(i), t);
    t += 16;
  }
  return t;
}

// --- classification ---------------------------------------------------------

test('an absolute event is recognised as absolute', () => {
  assert.deepStrictEqual(C.classify(absolute(90)), { source: 'absolute', heading: 90 });
});

test('a plain deviceorientation event is relative, not absolute', () => {
  assert.strictEqual(C.classify(relative(90)).source, 'relative');
});

test('the absolute flag is honoured even on a plain deviceorientation event', () => {
  // Firefox sets it. Trusting only the event name would throw away a good frame.
  const event = { type: 'deviceorientation', absolute: true, alpha: 270 };
  assert.strictEqual(C.classify(event).source, 'absolute');
});

test('iOS webkitCompassHeading wins over alpha and is true north', () => {
  assert.deepStrictEqual(C.classify(ios(42)), { source: 'true', heading: 42 });
});

test('an event with nothing usable in it is rejected', () => {
  assert.strictEqual(C.classify({ type: 'deviceorientation', alpha: null }), null);
  assert.strictEqual(C.classify({ alpha: NaN }), null);
  assert.strictEqual(C.classify(null), null);
});

// --- the reported bug -------------------------------------------------------

test('interleaved absolute and relative streams do not fight', () => {
  // This is the field report. The two frames are 170 degrees apart; blending
  // them puts the needle nowhere near either, and it never settles.
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 120; i += 1) {
    compass.handle(absolute(30), t);
    compass.handle(relative(200), t);
    t += 16;
  }

  assert.strictEqual(compass.source, 'absolute');
  assert.ok(Math.abs(C.shortAngleDiff(30, compass.heading)) < 0.5,
    `heading settled at ${compass.heading}, expected ~30`);

  const { steady, spread } = compass.steadiness(t);
  assert.ok(steady, `expected steady, spread was ${spread}`);
});

test('a relative event is reported as discarded, not silently ignored', () => {
  const compass = new C.Compass();
  compass.handle(absolute(30), 0);

  const result = compass.handle(relative(200), 16);

  assert.strictEqual(result.accepted, false);
  assert.strictEqual(result.reason, 'lower-ranked');
  assert.strictEqual(result.source, 'absolute');
});

test('the discarded frame leaves no trace in the samples', () => {
  const compass = new C.Compass();
  let t = feed(compass, () => absolute(30), 20);
  compass.handle(relative(200), t);

  assert.ok(compass.samples.every((s) => Math.abs(C.shortAngleDiff(30, s.heading)) < 0.001));
});

// --- source ranking ---------------------------------------------------------

test('a better source is adopted mid-stream', () => {
  const compass = new C.Compass();
  const t = feed(compass, () => relative(200), 20);
  assert.strictEqual(compass.source, 'relative');

  const result = compass.handle(absolute(30), t);

  assert.strictEqual(result.accepted, true);
  assert.strictEqual(result.switched, true);
  assert.strictEqual(compass.source, 'absolute');
});

test('switching source discards the old frame entirely', () => {
  // Carrying samples across would reintroduce the blending bug at the moment
  // of the switch — the worst possible time, since that is when the user is
  // watching the needle jump.
  const compass = new C.Compass();
  const t = feed(compass, () => relative(200), 30);
  compass.handle(absolute(30), t);

  assert.strictEqual(compass.samples.length, 1);
  assert.ok(Math.abs(C.shortAngleDiff(30, compass.heading)) < 0.001);
});

test('iOS true north outranks absolute', () => {
  const compass = new C.Compass();
  feed(compass, () => absolute(30), 10);
  compass.handle(ios(42), 200);
  assert.strictEqual(compass.source, 'true');
});

test('a worse source cannot take over while the better one is alive', () => {
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 200; i += 1) {
    compass.handle(absolute(30), t);
    assert.strictEqual(compass.handle(relative(200), t).accepted, false);
    t += 16;
  }
  assert.strictEqual(compass.source, 'absolute');
});

test('a worse source takes over once the better one goes silent', () => {
  // A phone whose absolute sensor drops out must not show a frozen needle
  // for ever while usable events are discarded beside it.
  const compass = new C.Compass();
  feed(compass, () => absolute(30), 20);

  const result = compass.handle(relative(200), 5000);

  assert.strictEqual(result.accepted, true);
  assert.strictEqual(compass.source, 'relative');
  assert.strictEqual(compass.hasNorthReference(), false);
});

// --- what the reading means -------------------------------------------------

test('only absolute and true frames count as a north reference', () => {
  const compass = new C.Compass();

  feed(compass, () => relative(10), 5);
  assert.strictEqual(compass.hasNorthReference(), false);
  assert.strictEqual(compass.headingRef(), 'unknown');

  compass.handle(absolute(10), 1000);
  assert.strictEqual(compass.hasNorthReference(), true);
  assert.strictEqual(compass.headingRef(), 'magnetic');

  compass.handle(ios(10), 1100);
  assert.strictEqual(compass.headingRef(), 'true');
});

// --- smoothing --------------------------------------------------------------

test('noise below the deadband does not move the published heading', () => {
  const compass = new C.Compass();
  feed(compass, () => absolute(90), 60);
  const settled = compass.heading;

  let t = 960;
  for (let i = 0; i < 60; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 0.15 : -0.15)), t);
    t += 16;
  }

  assert.strictEqual(compass.heading, settled);
});

test('the filter does not invert across the 0/360 seam', () => {
  // Averaging 359 and 1 arithmetically gives 180 — the needle points exactly
  // backwards. This is the failure the vector filter exists to prevent.
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 120; i += 1) {
    compass.handle(absolute(i % 2 ? 359 : 1), t);
    t += 16;
  }

  assert.ok(Math.abs(C.shortAngleDiff(0, compass.heading)) < 2,
    `heading was ${compass.heading}, expected ~0`);
});

test('a real turn is followed', () => {
  const compass = new C.Compass();
  feed(compass, () => absolute(0), 60);
  let t = 960;
  for (let i = 0; i < 120; i += 1) {
    compass.handle(absolute(90), t);
    t += 16;
  }
  assert.ok(Math.abs(C.shortAngleDiff(90, compass.heading)) < 1,
    `heading was ${compass.heading}, expected ~90`);
});

// --- steadiness -------------------------------------------------------------

test('steadiness is not reported until there are enough samples', () => {
  const compass = new C.Compass();
  compass.handle(absolute(90), 0);
  const { ready, steady } = compass.steadiness(0);
  assert.strictEqual(ready, false);
  assert.strictEqual(steady, false);
});

test('a still phone reads steady', () => {
  const compass = new C.Compass();
  const t = feed(compass, () => absolute(90), 60);
  assert.strictEqual(compass.steadiness(t).steady, true);
});

test('a swinging phone reads moving', () => {
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 60; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 20 : -20)), t);
    t += 16;
  }
  assert.strictEqual(compass.steadiness(t).steady, false);
});

test('the steady label does not oscillate at the threshold', () => {
  // Without hysteresis a spread hovering on the boundary flips the chip
  // between "steady" and "moving" every frame, which is its own flicker.
  const compass = new C.Compass({ steadyEnterDeg: 3, steadyExitDeg: 4.5 });
  let t = 0;
  for (let i = 0; i < 40; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 1.5 : -1.5)), t);
    t += 16;
  }
  assert.strictEqual(compass.steadiness(t).steady, true, 'should latch steady at spread ~1.5');

  // Nudge the spread just past the enter threshold but below the exit one.
  const states = [];
  for (let i = 0; i < 40; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 1.8 : -1.8)), t);
    t += 16;
    states.push(compass.steadiness(t).steady);
  }
  assert.ok(states.every((s) => s === true), 'stayed steady inside the hysteresis band');
});

test('a large excursion still breaks the steady latch', () => {
  const compass = new C.Compass();
  let t = feed(compass, () => absolute(90), 40);
  assert.strictEqual(compass.steadiness(t).steady, true);

  for (let i = 0; i < 40; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 30 : -30)), t);
    t += 16;
  }
  assert.strictEqual(compass.steadiness(t).steady, false);
});

test('samples older than the window are dropped', () => {
  const compass = new C.Compass({ sampleWindowMs: 1000 });
  feed(compass, () => absolute(90), 30);
  compass.handle(absolute(90), 9000);
  assert.strictEqual(compass.samples.length, 1);
});

// --- locking ----------------------------------------------------------------

test('locking averages the window rather than taking an instant', () => {
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 60; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 2 : -2)), t);
    t += 16;
  }

  const locked = compass.lock(t);

  assert.strictEqual(locked.heading, 90);
  assert.ok(locked.count > 8);
  assert.strictEqual(locked.source, 'absolute');
});

test('locking averages correctly across the seam', () => {
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 60; i += 1) {
    compass.handle(absolute(i % 2 ? 358 : 2), t);
    t += 16;
  }
  assert.strictEqual(compass.lock(t).heading, 0);
});

test('locking with no samples returns nothing rather than a wrong number', () => {
  assert.strictEqual(new C.Compass().lock(0), null);
});

test('a lock taken while moving says so', () => {
  const compass = new C.Compass();
  let t = 0;
  for (let i = 0; i < 60; i += 1) {
    compass.handle(absolute(90 + (i % 2 ? 25 : -25)), t);
    t += 16;
  }
  assert.strictEqual(compass.lock(t).steady, false);
});

// --- lifecycle --------------------------------------------------------------

test('reset clears the chosen source so a new session can pick again', () => {
  const compass = new C.Compass();
  feed(compass, () => absolute(30), 20);
  compass.reset();

  assert.strictEqual(compass.source, null);
  assert.strictEqual(compass.heading, null);
  assert.strictEqual(compass.samples.length, 0);

  compass.handle(relative(200), 0);
  assert.strictEqual(compass.source, 'relative');
});
