/* Tests for the on-device geometry advisory.
 *
 * The phone has to reach the same verdict as the server, because the phone is
 * the one that can act on it — it warns the team while they are still standing
 * at the station. These cases mirror tests/test_advice.py one for one, against
 * the same real coordinates, so the two implementations cannot drift apart
 * without a test going red on one side.
 */
const test = require('node:test');
const assert = require('node:assert');

const T = require('../../static/triangulate.js');

const o = (lat, lon, bearingTrue) => ({ lat, lon, bearingTrue });

// --- the two reported shapes ------------------------------------------------

test('teams aiming at each other is named as such', () => {
  // B2BGYY / P15, 1 October 2026. 314 m apart, each within 3° of the other.
  const advice = T.advise([o(21.880167, 79.579881, 340.1), o(21.882879, 79.578988, 161.1)]);
  assert.strictEqual(advice.code, 'on-line');
  assert.strictEqual(advice.ok, false);
  assert.match(advice.message, /between you/);
  // Moving further apart along the line is the intuitive thing and makes it
  // worse, so the message has to rule it out explicitly.
  assert.match(advice.message, /will not help/);
});

test('stations too close together is named separately', () => {
  // NS2QQA / Leopard, 1 October 2026. 60 m apart, both aiming north.
  const advice = T.advise([o(22.460568, 78.419074, 2.2), o(22.460622, 78.419650, 1.2)]);
  assert.strictEqual(advice.code, 'too-close');
  assert.match(advice.message, /60 m apart/);
});

test('the two shapes are not confused with each other', () => {
  const onLine = T.advise([o(21.880167, 79.579881, 340.1), o(21.882879, 79.578988, 161.1)]);
  const tooClose = T.advise([o(22.460568, 78.419074, 2.2), o(22.460622, 78.419650, 1.2)]);
  assert.notStrictEqual(onLine.code, tooClose.code);
});

test('bearings from one spot are caught before the solve', () => {
  const advice = T.advise([o(21.88, 79.57, 10), o(21.880001, 79.570001, 100)]);
  assert.strictEqual(advice.code, 'same-spot');
  assert.match(advice.message, /your own feet/);
});

// --- what it must stay quiet about -----------------------------------------

test('good geometry is not warned about', () => {
  // CVDKDF / P15: the same two stations as B2BGYY, 48 minutes earlier, aiming
  // 22° off the line. Crossed at 43°.
  const advice = T.advise([o(21.882879, 79.578988, 185.1), o(21.880167, 79.579881, 322.1)]);
  assert.strictEqual(advice.ok, true);
  assert.strictEqual(advice.code, 'ok');
  assert.strictEqual(advice.moveBearingDeg, null);
});

test('a single bearing waits rather than complaining', () => {
  assert.strictEqual(T.advise([o(21.88, 79.57, 10)]).code, 'waiting');
});

test('no bearings at all is not an error', () => {
  assert.strictEqual(T.advise([]).code, 'waiting');
  assert.strictEqual(T.advise(null).code, 'waiting');
  assert.strictEqual(T.advise([null]).code, 'waiting');
});

test('a close pair that still crosses well is left alone', () => {
  // 98 m and 116 m baselines produced good fixes in September, which is why
  // separation alone is not something to warn on.
  const advice = T.advise([o(21.86, 79.575, 30), o(21.86, 79.576, 330)]);
  assert.strictEqual(advice.ok, true, advice.message);
});

// --- the move suggestion ----------------------------------------------------

test('the move is across the line joining the stations, not along it', () => {
  const a = o(21.880167, 79.579881, 340.1);
  const b = o(21.882879, 79.578988, 161.1);
  const advice = T.advise([a, b]);
  const along = T.bearingDegrees(a.lat, a.lon, b.lat, b.lon);
  assert.ok(Math.abs(T.acuteBetween(advice.moveBearingDeg, along) - 90) < 0.5);
});

test('the move is far enough to matter and reads as an instruction', () => {
  const advice = T.advise([o(21.880167, 79.579881, 340.1), o(21.882879, 79.578988, 161.1)]);
  assert.ok(advice.moveMetres >= T.MIN_HELPFUL_MOVE_M);
  assert.match(T.moveNote(advice), /^Walk about \d+ m on \d+°\.$/);
});

test('a round that is fine suggests no walk', () => {
  const advice = T.advise([o(21.882879, 79.578988, 185.1), o(21.880167, 79.579881, 322.1)]);
  assert.strictEqual(T.moveNote(advice), '');
  assert.strictEqual(T.moveNote(null), '');
});

// --- a position that solves but should not be trusted ----------------------

test('a reversed bearing is called out by name', () => {
  // SGPDEH / P15, 1 October. It solves, and the fix lands behind an observer.
  const advice = T.advise([o(21.882879, 79.578988, 161.1), o(21.880107, 79.579881, 322.1)]);
  assert.strictEqual(advice.code, 'shallow');
  assert.match(advice.message, /180°/);
  assert.match(advice.message, /turn the antenna around/);
});

test('a shallow crossing that still solves says what is wrong with it', () => {
  // 2XTW9B / P12, 6 September: crossed at 12° with both bearings pointing the
  // right way, so the complaint must be about the crossing, not a 180° error.
  const advice = T.advise([o(21.85610, 79.58088, 239.1), o(21.85646, 79.57929, 227.1)]);
  assert.strictEqual(advice.code, 'shallow');
  assert.match(advice.message, /smeared/);
  assert.doesNotMatch(advice.message, /180°/);
});

// --- agreement with the server ---------------------------------------------

test('the thresholds match the ones triangulation.py uses', () => {
  // Drifting apart would mean the phone warns about a round the server accepts,
  // or stays quiet about one it rejects.
  assert.strictEqual(T.MIN_BASELINE_M, 25);
  assert.strictEqual(T.MIN_USABLE_CROSSING_DEG, 10);
  assert.strictEqual(T.MIN_OFFSET_DEG, 20);
  assert.strictEqual(T.ADVISORY_SEPARATION_M, 150);
  assert.strictEqual(T.MIN_HELPFUL_MOVE_M, 300);
});

// --- the helper -------------------------------------------------------------

test('acuteBetween folds a reciprocal onto zero', () => {
  assert.ok(T.acuteBetween(10, 190) < 1e-9);
  assert.ok(T.acuteBetween(10, 10) < 1e-9);
  assert.ok(Math.abs(T.acuteBetween(0, 90) - 90) < 1e-9);
  assert.ok(Math.abs(T.acuteBetween(350, 10) - 20) < 1e-9);
});
