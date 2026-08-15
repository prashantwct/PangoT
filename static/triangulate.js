/* On-device bearing intersection.
 *
 * A JavaScript port of triangulation.py, so the field app can answer "where is
 * the animal, and which way do I walk?" from two bearings without a network.
 *
 * The result is PROVISIONAL. The server's solve remains the record: it uses a
 * proper geodesic library, applies magnetic declination from the World
 * Magnetic Model, and sees bearings contributed by the other observer's phone
 * that this one may not have. The UI labels it as such.
 *
 * Projection: a local equirectangular frame centred on the observers. Over the
 * few kilometres a VHF fix spans, its relative geometry is accurate to well
 * under a metre, and its grid north coincides with true north to about a
 * hundredth of a degree — far inside the few degrees of aim error in any
 * hand-held bearing. The server does not take this shortcut.
 *
 * Thresholds are kept in step with triangulation.py; changing one means
 * changing both, or the phone and the server will disagree about whether a
 * session is solvable.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Triangulate = factory();
}(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const R_EARTH = 6371008.8;            // mean radius, for distance and bearing

  // WGS84, for the local projection.
  const WGS84_A = 6378137.0;
  const WGS84_F = 1 / 298.257223563;
  const WGS84_E2 = WGS84_F * (2 - WGS84_F);

  const MIN_BASELINE_M = 25;
  const PARALLEL_EPS_DEG = 0.5;
  const MIN_USABLE_CROSSING_DEG = 10;
  const MAX_PLAUSIBLE_RANGE_M = 50000;

  const POOR_CROSSING_DEG = 20;
  const FAIR_CROSSING_DEG = 35;
  const POOR_RMS_M = 100;
  const FAIR_RMS_M = 30;

  const toRad = (d) => (d * Math.PI) / 180;
  const toDeg = (r) => (r * 180) / Math.PI;

  class TriangulationError extends Error {
    constructor(message) {
      super(message);
      this.name = 'TriangulationError';
    }
  }

  /** Meridional and normal radii of curvature at a latitude, in metres. */
  function localRadii(latDeg) {
    const sinLat = Math.sin(toRad(latDeg));
    const w = 1 - WGS84_E2 * sinLat * sinLat;
    return {
      meridional: (WGS84_A * (1 - WGS84_E2)) / Math.pow(w, 1.5),
      normal: WGS84_A / Math.sqrt(w),
    };
  }

  /**
   * Local east/north offset between two points, in metres.
   *
   * Uses the same ellipsoidal flat-earth frame as solve(). Sharing one frame
   * matters: a spherical bearing helper feeding an ellipsoidal projection put
   * the recovered position about 6 m out purely from the mismatch. Everything
   * here works at VHF telemetry ranges — a few kilometres — where a local
   * frame is accurate to centimetres.
   */
  function localOffset(lat1, lon1, lat2, lon2) {
    const latMid = (lat1 + lat2) / 2;
    const { meridional, normal } = localRadii(latMid);
    return {
      east: toRad(lon2 - lon1) * normal * Math.cos(toRad(latMid)),
      north: toRad(lat2 - lat1) * meridional,
    };
  }

  function distanceMetres(lat1, lon1, lat2, lon2) {
    const { east, north } = localOffset(lat1, lon1, lat2, lon2);
    return Math.hypot(east, north);
  }

  function bearingDegrees(lat1, lon1, lat2, lon2) {
    const { east, north } = localOffset(lat1, lon1, lat2, lon2);
    return (toDeg(Math.atan2(east, north)) + 360) % 360;
  }

  function grade(crossingDeg, rmsM, reversedCount) {
    if (reversedCount) return 'poor';
    if (crossingDeg < POOR_CROSSING_DEG) return 'poor';
    if (rmsM !== null && rmsM > POOR_RMS_M) return 'poor';
    if (crossingDeg < FAIR_CROSSING_DEG) return 'fair';
    if (rmsM !== null && rmsM > FAIR_RMS_M) return 'fair';
    return 'good';
  }

  /** Smallest acute angle between any pair of bearing lines, in degrees. */
  function crossingAngle(directions) {
    let smallest = 90;
    for (let i = 0; i < directions.length; i += 1) {
      for (let j = i + 1; j < directions.length; j += 1) {
        const dot = Math.abs(directions[i][0] * directions[j][0] + directions[i][1] * directions[j][1]);
        smallest = Math.min(smallest, toDeg(Math.acos(Math.min(1, dot))));
      }
    }
    return smallest;
  }

  /**
   * Intersect bearing lines by least squares.
   *
   * @param {Array<{lat:number, lon:number, bearingTrue:number}>} observations
   * @returns {object} the provisional fix
   * @throws {TriangulationError} when no trustworthy answer exists
   */
  function solve(observations) {
    if (!Array.isArray(observations) || observations.length < 2) {
      throw new TriangulationError('At least two bearings are needed for a fix');
    }

    let baseline = 0;
    for (let i = 0; i < observations.length; i += 1) {
      for (let j = i + 1; j < observations.length; j += 1) {
        baseline = Math.max(baseline, distanceMetres(
          observations[i].lat, observations[i].lon,
          observations[j].lat, observations[j].lon,
        ));
      }
    }
    if (baseline < MIN_BASELINE_M) {
      throw new TriangulationError(
        `All bearings were taken from within ${baseline.toFixed(0)} m of each other. `
        + 'Observers need to be well apart — move at least a few hundred metres and take another bearing.',
      );
    }

    const lat0 = observations.reduce((sum, o) => sum + o.lat, 0) / observations.length;
    const lon0 = observations.reduce((sum, o) => sum + o.lon, 0) / observations.length;
    // Local radii of curvature on the WGS84 ellipsoid rather than one spherical
    // radius. A sphere puts this roughly 6 m away from the server's answer on a
    // 1.4 km fix; matching the ellipsoid brings it under a metre, so the phone
    // and the server do not visibly disagree.
    const { meridional, normal } = localRadii(lat0);
    const metresPerDegLat = (Math.PI / 180) * meridional;
    const metresPerDegLon = (Math.PI / 180) * normal * Math.cos(toRad(lat0));

    const toXY = (lat, lon) => [(lon - lon0) * metresPerDegLon, (lat - lat0) * metresPerDegLat];
    const toLatLon = (x, y) => [lat0 + y / metresPerDegLat, lon0 + x / metresPerDegLon];

    const points = observations.map((o) => toXY(o.lat, o.lon));
    // East is +x and north is +y, so a bearing clockwise from north maps
    // directly onto (sin, cos).
    const directions = observations.map((o) => [
      Math.sin(toRad(o.bearingTrue)),
      Math.cos(toRad(o.bearingTrue)),
    ]);

    const crossing = crossingAngle(directions);
    if (crossing < PARALLEL_EPS_DEG) {
      throw new TriangulationError('Bearings are parallel — they never intersect');
    }
    if (crossing < MIN_USABLE_CROSSING_DEG) {
      throw new TriangulationError(
        `Bearings cross at only ${crossing.toFixed(0)}° — too grazing to locate. `
        + 'Take another bearing from a position well off the current line.',
      );
    }

    // Row i of the system is  dy*x - dx*y = dy*qx - dx*qy.
    // Because d is a unit vector, that row's residual is exactly the
    // perpendicular distance from the solution to bearing line i, in metres.
    const rows = directions.map((d, i) => ({
      a0: d[1],
      a1: -d[0],
      b: d[1] * points[i][0] - d[0] * points[i][1],
    }));

    // Normal equations for a 2x2 system, inverted directly.
    let m00 = 0; let m01 = 0; let m11 = 0; let v0 = 0; let v1 = 0;
    rows.forEach((row) => {
      m00 += row.a0 * row.a0;
      m01 += row.a0 * row.a1;
      m11 += row.a1 * row.a1;
      v0 += row.a0 * row.b;
      v1 += row.a1 * row.b;
    });

    const det = m00 * m11 - m01 * m01;
    if (!Number.isFinite(det) || Math.abs(det) < 1e-12) {
      throw new TriangulationError('Bearings are parallel — they never intersect');
    }

    const x = (m11 * v0 - m01 * v1) / det;
    const y = (-m01 * v0 + m00 * v1) / det;
    if (!Number.isFinite(x) || !Number.isFinite(y)) {
      throw new TriangulationError('Solve produced a non-finite position');
    }

    const [lat, lon] = toLatLon(x, y);
    if (!(lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)) {
      throw new TriangulationError('Solve produced coordinates outside the valid range');
    }

    // A fix behind the observer means that bearing was recorded roughly 180
    // degrees out — easy to do with a directional antenna, and it otherwise
    // produces a perfectly plausible-looking result.
    const reversedIndices = [];
    points.forEach((q, i) => {
      const along = (x - q[0]) * directions[i][0] + (y - q[1]) * directions[i][1];
      if (along <= 0) reversedIndices.push(i);
    });

    const maxRange = Math.max(...observations.map((o) => distanceMetres(o.lat, o.lon, lat, lon)));
    if (maxRange > MAX_PLAUSIBLE_RANGE_M) {
      throw new TriangulationError(
        `Fix lands ${(maxRange / 1000).toFixed(0)} km from the nearest observer, which is beyond `
        + 'plausible detection range. Check the bearings for a reversed or mistyped value.',
      );
    }

    // With exactly two bearings the system is square, so the residual is
    // always zero and reporting it would imply a precision the fix lacks.
    let rms = null;
    if (observations.length > 2) {
      const sumSquares = rows.reduce((sum, row) => {
        const residual = row.a0 * x + row.a1 * y - row.b;
        return sum + residual * residual;
      }, 0);
      rms = Math.sqrt(sumSquares / rows.length);
    }

    return {
      lat,
      lon,
      nBearings: observations.length,
      crossingAngleDeg: crossing,
      rmsErrorM: rms,
      maxRangeM: maxRange,
      quality: grade(crossing, rms, reversedIndices.length),
      reversedIndices,
    };
  }

  return {
    solve,
    distanceMetres,
    bearingDegrees,
    TriangulationError,
    MIN_BASELINE_M,
    MIN_USABLE_CROSSING_DEG,
  };
}));
