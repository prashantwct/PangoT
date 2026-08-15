"""Least-squares bearing intersection.

Given two or more true-north bearings taken from known positions, find the
point that best satisfies all of them, and — just as importantly — say how much
to trust it.

The quality reporting is deliberate. The previous implementation reported
``Err: 0.00m`` for every two-bearing fix, because with exactly two bearings the
system is square, the residual is always zero, and zero reads as perfect. It is
not: two bearings crossing at a shallow angle produce an enormously elongated
uncertainty region while still fitting both lines exactly. Crossing angle, not
residual, is the number that tells a field team whether to trust a two-bearing
fix, so that is what gets reported.
"""
import math
from dataclasses import dataclass

import numpy as np

from geodesy import bearing_between, distance_m, grid_direction, local_frame

# A ground-based VHF telemetry fix beyond this range is not a long detection,
# it is a bad solve — near-parallel bearings throwing the intersection to
# infinity, or a reversed bearing.
MAX_PLAUSIBLE_RANGE_M = 50_000.0

# Below this the intersection is too grazing for the result to mean anything.
MIN_USABLE_CROSSING_DEG = 10.0

# Below this the bearings are parallel for all practical purposes. Checked
# separately from the grazing case only so the message names the real problem.
PARALLEL_EPS_DEG = 0.5

# Two observers standing together cannot triangulate: every bearing line passes
# through the same point, so the "fix" is just their own position. It happens
# in the field — the pair walks in together and forgets to separate — and it
# produces a confident-looking result at their feet.
MIN_BASELINE_M = 25.0

POOR_CROSSING_DEG = 20.0
FAIR_CROSSING_DEG = 35.0
POOR_RMS_M = 100.0
FAIR_RMS_M = 30.0


class TriangulationError(Exception):
    """Raised when no meaningful fix can be derived from the observations."""


@dataclass(frozen=True)
class Observation:
    lat: float
    lon: float
    bearing_true: float


@dataclass(frozen=True)
class Fix:
    lat: float
    lon: float
    n_bearings: int
    crossing_angle_deg: float
    # None for a two-bearing fix: the system is exactly determined, so a
    # residual of zero carries no information about accuracy.
    rms_error_m: float | None
    max_range_m: float
    quality: str  # good | fair | poor
    reversed_indices: tuple

    def describe(self) -> str:
        """A short human-readable note, stored alongside the fix."""
        parts = [f"{self.n_bearings} bearings", f"cross {self.crossing_angle_deg:.0f}°"]
        if self.rms_error_m is not None:
            parts.append(f"RMS {self.rms_error_m:.0f} m")
        else:
            parts.append("2-line fix, no residual")
        if self.reversed_indices:
            parts.append(f"{len(self.reversed_indices)} bearing(s) point away — check for a 180° error")
        return "; ".join(parts)


def _crossing_angle(directions) -> float:
    """Smallest acute angle between any pair of bearing lines, in degrees.

    The minimum is the right summary: one grazing pair limits the quality of
    the whole solution regardless of how well-crossed the others are.
    """
    smallest = 90.0
    for i in range(len(directions)):
        for j in range(i + 1, len(directions)):
            dot = abs(float(np.dot(directions[i], directions[j])))
            angle = math.degrees(math.acos(min(1.0, dot)))
            smallest = min(smallest, angle)
    return smallest


def _grade(crossing_deg: float, rms_m, reversed_indices) -> str:
    if reversed_indices:
        return "poor"
    if crossing_deg < POOR_CROSSING_DEG:
        return "poor"
    if rms_m is not None and rms_m > POOR_RMS_M:
        return "poor"
    if crossing_deg < FAIR_CROSSING_DEG:
        return "fair"
    if rms_m is not None and rms_m > FAIR_RMS_M:
        return "fair"
    return "good"


def solve(observations) -> Fix:
    """Intersect bearing lines by least squares.

    Raises TriangulationError when the observations cannot produce a
    trustworthy answer, rather than returning a plausible-looking wrong one.
    """
    observations = list(observations)
    if len(observations) < 2:
        raise TriangulationError("At least two bearings are needed for a fix")

    baseline = max(
        distance_m(a.lat, a.lon, b.lat, b.lon)
        for i, a in enumerate(observations)
        for b in observations[i + 1:]
    )
    if baseline < MIN_BASELINE_M:
        raise TriangulationError(
            f"All bearings were taken from within {baseline:.0f} m of each other. "
            "Observers need to be well apart — move at least a few hundred metres "
            "and take another bearing."
        )

    lat0 = sum(o.lat for o in observations) / len(observations)
    lon0 = sum(o.lon for o in observations) / len(observations)
    to_xy, to_latlon = local_frame(lat0, lon0)

    points, directions = [], []
    for obs in observations:
        try:
            direction = grid_direction(obs.lat, obs.lon, obs.bearing_true, to_xy)
        except ValueError as exc:
            raise TriangulationError(str(exc)) from exc
        points.append(to_xy(obs.lat, obs.lon))
        directions.append(np.array(direction))

    # A point p lies on the line through q with unit direction d when
    # (p - q) x d == 0, i.e.  dy*x - dx*y = dy*qx - dx*qy.
    # Because d is a unit vector, the residual of that row is exactly the
    # perpendicular distance from p to the line, in metres.
    A = np.array([[d[1], -d[0]] for d in directions])
    B = np.array([d[1] * q[0] - d[0] * q[1] for d, q in zip(directions, points)])

    # Judge the geometry before solving. A near-singular system still returns
    # numbers, and those numbers look like a fix.
    crossing = _crossing_angle(directions)
    if crossing < PARALLEL_EPS_DEG:
        raise TriangulationError("Bearings are parallel — they never intersect")
    if crossing < MIN_USABLE_CROSSING_DEG:
        raise TriangulationError(
            f"Bearings cross at only {crossing:.0f}° — too grazing to locate. "
            "Take another bearing from a position well off the current line."
        )

    solution, _, rank, _ = np.linalg.lstsq(A, B, rcond=None)
    if rank < 2:
        raise TriangulationError("Bearings are parallel — they never intersect")

    x, y = float(solution[0]), float(solution[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise TriangulationError("Solve produced a non-finite position")

    lat, lon = to_latlon(x, y)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise TriangulationError("Solve produced coordinates outside the valid range")

    # A fix behind the observer means that bearing was recorded roughly 180
    # degrees out — an easy mistake with a directional antenna, and one that
    # otherwise produces a perfectly plausible-looking result.
    reversed_indices = tuple(
        i
        for i, (q, d) in enumerate(zip(points, directions))
        if (x - q[0]) * d[0] + (y - q[1]) * d[1] <= 0
    )

    max_range = max(distance_m(o.lat, o.lon, lat, lon) for o in observations)
    if max_range > MAX_PLAUSIBLE_RANGE_M:
        raise TriangulationError(
            f"Fix lands {max_range / 1000:.0f} km from the nearest observer, which is "
            "beyond plausible detection range. Check the bearings for a reversed or "
            "mistyped value."
        )

    rms = None
    if len(observations) > 2:
        residuals = A @ np.array([x, y]) - B
        rms = float(np.sqrt(np.mean(residuals**2)))

    return Fix(
        lat=lat,
        lon=lon,
        n_bearings=len(observations),
        crossing_angle_deg=crossing,
        rms_error_m=rms,
        max_range_m=max_range,
        quality=_grade(crossing, rms, reversed_indices),
        reversed_indices=reversed_indices,
    )


def bearing_and_range(from_lat, from_lon, to_lat, to_lon):
    """True bearing and distance from one point to another.

    Used by the field app to answer the only question that matters on the
    ground: which way do I walk, and how far?
    """
    return (
        bearing_between(from_lat, from_lon, to_lat, to_lon),
        distance_m(from_lat, from_lon, to_lat, to_lon),
    )
