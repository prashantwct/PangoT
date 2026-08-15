/* Tests for the on-device solver.
 *
 * The important property is not just that it works, but that it AGREES with
 * the server. If the phone shows a fix and the server then computes a
 * materially different one, the field team stops trusting both.
 */
const test = require('node:test');
const assert = require('node:assert');

const T = require('../../static/triangulate.js');

const SITE_LAT = 19.05;
const SITE_LON = 73.05;

function lookingAt(obsLat, obsLon, targetLat, targetLon, errorDeg = 0) {
  return {
    lat: obsLat,
    lon: obsLon,
    bearingTrue: (T.bearingDegrees(obsLat, obsLon, targetLat, targetLon) + errorDeg + 360) % 360,
  };
}

test('two bearings recover the true position', () => {
  const fix = T.solve([
    lookingAt(SITE_LAT - 0.009, SITE_LON - 0.009, SITE_LAT, SITE_LON),
    lookingAt(SITE_LAT - 0.009, SITE_LON + 0.009, SITE_LAT, SITE_LON),
  ]);

  assert.ok(T.distanceMetres(fix.lat, fix.lon, SITE_LAT, SITE_LON) < 2,
    `recovered position was ${T.distanceMetres(fix.lat, fix.lon, SITE_LAT, SITE_LON).toFixed(1)} m out`);
  assert.strictEqual(fix.quality, 'good');
  assert.strictEqual(fix.nBearings, 2);
});

test('a two-bearing fix reports no residual', () => {
  const fix = T.solve([
    lookingAt(SITE_LAT - 0.009, SITE_LON - 0.009, SITE_LAT, SITE_LON),
    lookingAt(SITE_LAT - 0.009, SITE_LON + 0.009, SITE_LAT, SITE_LON),
  ]);
  assert.strictEqual(fix.rmsErrorM, null);
});

test('accuracy holds well away from the old hardcoded UTM zone', () => {
  for (const lon of [68, 73, 78, 88]) {
    const fix = T.solve([
      lookingAt(19.05 - 0.009, lon - 0.009, 19.05, lon),
      lookingAt(19.05 - 0.009, lon + 0.009, 19.05, lon),
    ]);
    const error = T.distanceMetres(fix.lat, fix.lon, 19.05, lon);
    assert.ok(error < 2, `${error.toFixed(1)} m error at ${lon}E`);
  }
});

test('three noisy bearings report a residual in metres', () => {
  const fix = T.solve([
    lookingAt(SITE_LAT - 0.009, SITE_LON - 0.009, SITE_LAT, SITE_LON, +2),
    lookingAt(SITE_LAT - 0.009, SITE_LON + 0.009, SITE_LAT, SITE_LON, -2),
    lookingAt(SITE_LAT + 0.010, SITE_LON, SITE_LAT, SITE_LON, +1.5),
  ]);

  assert.ok(fix.rmsErrorM > 0);
  assert.ok(fix.rmsErrorM < 100, `residual ${fix.rmsErrorM} looks like the wrong units`);
  assert.ok(T.distanceMetres(fix.lat, fix.lon, SITE_LAT, SITE_LON) < 100);
});

test('bearings from the same spot are refused', () => {
  assert.throws(
    () => T.solve([
      { lat: SITE_LAT, lon: SITE_LON, bearingTrue: 47 },
      { lat: SITE_LAT + 0.00002, lon: SITE_LON, bearingTrue: 313 },
    ]),
    /within .* of each other/,
  );
});

test('parallel bearings are refused', () => {
  assert.throws(
    () => T.solve([
      { lat: SITE_LAT, lon: SITE_LON, bearingTrue: 45 },
      { lat: SITE_LAT + 0.01, lon: SITE_LON + 0.01, bearingTrue: 45 },
    ]),
    /parallel/,
  );
});

test('grazing bearings are refused rather than guessed', () => {
  assert.throws(
    () => T.solve([
      lookingAt(SITE_LAT, SITE_LON - 0.0005, SITE_LAT + 0.2, SITE_LON),
      lookingAt(SITE_LAT, SITE_LON + 0.0005, SITE_LAT + 0.2, SITE_LON),
    ]),
    /grazing|parallel|plausible/,
  );
});

test('a reversed bearing is flagged', () => {
  const a = lookingAt(SITE_LAT - 0.009, SITE_LON - 0.009, SITE_LAT, SITE_LON);
  const b = lookingAt(SITE_LAT - 0.009, SITE_LON + 0.009, SITE_LAT, SITE_LON);

  const fix = T.solve([
    { lat: a.lat, lon: a.lon, bearingTrue: (a.bearingTrue + 180) % 360 },
    { lat: b.lat, lon: b.lon, bearingTrue: (b.bearingTrue + 180) % 360 },
  ]);

  assert.deepStrictEqual(fix.reversedIndices, [0, 1]);
  assert.strictEqual(fix.quality, 'poor');
});

test('a single bearing is refused', () => {
  assert.throws(() => T.solve([{ lat: SITE_LAT, lon: SITE_LON, bearingTrue: 90 }]), /two bearings/);
});

test('crossing angle reports the worst pair', () => {
  const a = lookingAt(SITE_LAT - 0.012, SITE_LON, SITE_LAT, SITE_LON);
  const b = lookingAt(SITE_LAT - 0.012, SITE_LON + 0.004, SITE_LAT, SITE_LON);
  const c = lookingAt(SITE_LAT, SITE_LON - 0.012, SITE_LAT, SITE_LON);

  const fix = T.solve([a, b, c]);

  assert.ok(fix.crossingAngleDeg < 20, `expected the poor A/B pair, got ${fix.crossingAngleDeg}`);
  assert.strictEqual(fix.quality, 'poor');
});

test('agrees with the server solver to within a metre', () => {
  // Expected values come from triangulation.solve() in Python, which uses
  // pyproj on the WGS84 ellipsoid. If the two implementations drift apart the
  // phone and the server will show different positions for the same bearings,
  // and the field team stops trusting either.
  //
  // Tolerance is 1 m. Real fix uncertainty is tens of metres, so this is well
  // inside the noise — but it must stay a rounding difference, not a bug.
  const cases = [
    {
      name: 'field site, 73E',
      observations: [
        { lat: 19.041, lon: 73.041, bearingTrue: 43 },
        { lat: 19.041, lon: 73.059, bearingTrue: 317 },
      ],
      python: { lat: 19.050177463528936, lon: 73.05 },
    },
    {
      name: 'southern hemisphere',
      observations: [
        { lat: -8.5, lon: 115.2, bearingTrue: 10 },
        { lat: -8.49, lon: 115.21, bearingTrue: 280 },
      ],
      python: { lat: -8.488598940074267, lon: 115.20201926819026 },
    },
  ];

  for (const { name, observations, python } of cases) {
    const fix = T.solve(observations);
    const apart = T.distanceMetres(fix.lat, fix.lon, python.lat, python.lon);
    assert.ok(apart < 1.0, `${name}: ${apart.toFixed(2)} m from the server's answer`);
  }
});
