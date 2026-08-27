/* Rounds, on the phone.
 *
 * These mirror tests/test_events.py case for case. If the two implementations
 * drift, the phone's provisional fix and the server's fix are solved from
 * different sets of bearings and disagree — which is how the field team stops
 * trusting either.
 */
const test = require('node:test');
const assert = require('node:assert');

const R = require('../../static/rounds.js');

const START = Date.parse('2026-08-16T18:00:00.000Z');

// Two fixed stations about 130 m apart, as a real team works them.
const STATION_A = { lat: 21.85635, lon: 79.57928 };
const STATION_B = { lat: 21.85596, lon: 79.58008 };

function reading(minutes, observer, station) {
  return {
    time: new Date(START + minutes * 60000).toISOString(),
    observer,
    lat: station ? station.lat : null,
    lon: station ? station.lon : null,
  };
}

const seconds = (secs, observer, station) => reading(secs / 60, observer, station);

const observers = (rounds) => rounds.map((r) => r.map((x) => x.observer));

// --- the reported failure ---------------------------------------------------

test('four rounds in one session are four rounds', () => {
  const readings = [];
  for (let i = 0; i < 4; i += 1) {
    readings.push(reading(i * 45, 'MK'));
    readings.push(reading(i * 45 + 2, 'PD'));
  }

  const rounds = R.clusterRounds(readings);

  assert.strictEqual(rounds.length, 4);
  assert.deepStrictEqual(observers(rounds), [['MK', 'PD'], ['MK', 'PD'], ['MK', 'PD'], ['MK', 'PD']]);
});

test('one round stays one round', () => {
  assert.strictEqual(R.clusterRounds([reading(0, 'MK'), reading(2, 'PD')]).length, 1);
});

// --- rule 1: the time gap ---------------------------------------------------

test('a long gap starts a new round', () => {
  assert.strictEqual(R.clusterRounds([reading(0, 'MK'), reading(21, 'PD')]).length, 2);
});

test('a gap inside the window does not', () => {
  assert.strictEqual(R.clusterRounds([reading(0, 'MK'), reading(19, 'PD')]).length, 1);
});

test('the gap is measured between neighbours, not from the start', () => {
  const rounds = R.clusterRounds([reading(0, 'MK'), reading(15, 'PD'), reading(30, 'AS')]);
  assert.strictEqual(rounds.length, 1);
});

// --- rule 2: an observer appearing twice ------------------------------------

test('the same observer twice starts a new round', () => {
  const rounds = R.clusterRounds([
    reading(0, 'MK'), reading(1, 'PD'), reading(10, 'MK'), reading(11, 'PD'),
  ]);
  assert.strictEqual(rounds.length, 2);
  assert.deepStrictEqual(observers(rounds), [['MK', 'PD'], ['MK', 'PD']]);
});

test('an observer reshooting immediately stays in the same round', () => {
  const rounds = R.clusterRounds([reading(0, 'MK'), reading(1, 'MK'), reading(2, 'PD')]);
  assert.strictEqual(rounds.length, 1);
  assert.deepStrictEqual(observers(rounds), [['MK', 'MK', 'PD']]);
});

test('an unnamed observer falls back to the time rule', () => {
  const rounds = R.clusterRounds([reading(0, null), reading(5, null), reading(9, null)]);
  assert.strictEqual(rounds.length, 1);
});

// --- ordering and edges -----------------------------------------------------

test('readings are sorted before clustering', () => {
  const late = reading(0, 'MK');
  const early = reading(2, 'PD');
  assert.deepStrictEqual(observers(R.clusterRounds([early, late])), [['MK', 'PD']]);
});

test('no readings gives no rounds', () => {
  assert.deepStrictEqual(R.clusterRounds([]), []);
});

test('a single reading is a single round', () => {
  assert.strictEqual(R.clusterRounds([reading(0, 'MK')]).length, 1);
});

test('thresholds are adjustable', () => {
  const readings = [reading(0, 'MK'), reading(30, 'PD')];
  assert.strictEqual(R.clusterRounds(readings, { gapMs: 60 * 60000 }).length, 1);
  assert.strictEqual(R.clusterRounds(readings, { gapMs: 10 * 60000 }).length, 2);
});

test('every reading lands in exactly one round', () => {
  const readings = [];
  for (let i = 0; i < 20; i += 1) readings.push(reading(i * 30, `OB${i % 3}`));
  const total = R.clusterRounds(readings).reduce((n, r) => n + r.length, 0);
  assert.strictEqual(total, 20);
});

// --- what the phone actually asks for ---------------------------------------

test('the latest round is the one to walk towards', () => {
  const rounds = R.latestRound([
    reading(0, 'MK'), reading(2, 'PD'),
    reading(45, 'MK'), reading(47, 'PD'),
  ]);
  assert.strictEqual(rounds.length, 2);
  assert.deepStrictEqual(rounds.map((r) => r.observer), ['MK', 'PD']);
  assert.strictEqual(rounds[0].time, new Date(START + 45 * 60000).toISOString());
});

test('with nothing recorded the latest round is empty', () => {
  assert.deepStrictEqual(R.latestRound([]), []);
});

// --- returning to a station you have already used ---------------------------
//
// From a real export: one observer walks between two fixed stations, takes a
// bearing at each about 40 seconds apart, then walks the circuit again two
// minutes later. Time alone merged the circuits into one round of four
// bearings that crossed at 5° and produced no fix.

test('returning to a station starts a new round', () => {
  const rounds = R.clusterRounds([
    seconds(0, 'BB', STATION_A),
    seconds(36, 'BB', STATION_B),
    seconds(154, 'BB', STATION_B),
    seconds(199, 'BB', STATION_A),
  ]);

  assert.strictEqual(rounds.length, 2);
  assert.deepStrictEqual(rounds.map((r) => r.length), [2, 2]);
});

test('moving between stations stays in one round', () => {
  const rounds = R.clusterRounds([seconds(0, 'BB', STATION_A), seconds(36, 'BB', STATION_B)]);
  assert.strictEqual(rounds.length, 1);
});

test('the station rule does not need the time rule', () => {
  const rounds = R.clusterRounds([
    seconds(0, 'RK', STATION_A),
    seconds(30, 'RK', STATION_B),
    seconds(45, 'RK', STATION_A),
  ]);
  assert.strictEqual(rounds.length, 2);
});

test('a few paces from a used station counts as the same station', () => {
  const nearby = { lat: STATION_A.lat + 0.00009, lon: STATION_A.lon };   // ~10 m
  const rounds = R.clusterRounds([
    seconds(0, 'BB', STATION_A), seconds(36, 'BB', STATION_B), seconds(60, 'BB', nearby),
  ]);
  assert.strictEqual(rounds.length, 2);
});

test('a genuinely new position does not split the round', () => {
  const far = { lat: STATION_A.lat + 0.0045, lon: STATION_A.lon };       // ~500 m
  const rounds = R.clusterRounds([
    seconds(0, 'BB', STATION_A), seconds(36, 'BB', STATION_B), seconds(60, 'BB', far),
  ]);
  assert.strictEqual(rounds.length, 1);
});

test('readings without a position fall back to the time rule', () => {
  const rounds = R.clusterRounds([reading(0, 'BB'), reading(1, 'BB'), reading(2, 'PD')]);
  assert.strictEqual(rounds.length, 1);
});

test('two observers at their own stations are one round', () => {
  const rounds = R.clusterRounds([seconds(0, 'MK', STATION_A), seconds(40, 'PD', STATION_B)]);
  assert.strictEqual(rounds.length, 1);
});

test('the station tolerance is adjustable', () => {
  const close = { lat: STATION_A.lat + 0.0009, lon: STATION_A.lon };     // ~100 m
  const readings = [
    seconds(0, 'BB', STATION_A), seconds(36, 'BB', STATION_B), seconds(60, 'BB', close),
  ];
  assert.strictEqual(R.clusterRounds(readings, { sameSpotM: 25 }).length, 1);
  assert.strictEqual(R.clusterRounds(readings, { sameSpotM: 150 }).length, 2);
});
