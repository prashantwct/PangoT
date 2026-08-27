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

function reading(minutes, observer) {
  return { time: new Date(START + minutes * 60000).toISOString(), observer };
}

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
