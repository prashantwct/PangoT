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

# When a round has already failed, a pair of bearings running within this
# angle of the line joining the two positions is the reason: the animal lies
# roughly between the two teams, each needle points almost at the other, and no
# accuracy makes those lines cross. Four rounds in September 2026 failed this
# way, two with the teams aiming within 3 degrees of each other.
#
# This explains a failure; it does not predict one. Thresholding the angle
# before the second bearing exists does not work — see Advice.
MIN_OFFSET_DEG = 20.0

# The other reason a failed round failed: too little separation for the range.
# MIN_BASELINE_M only catches observers standing together, and six rounds on
# 1 October 2026 were taken from stations 55-85 m apart, aiming at something
# far enough away that none of them could produce a position. Rounds at 98 m
# and 116 m in the same month produced good fixes, so this is not a threshold
# to warn on in advance either — only to explain with.
ADVISORY_SEPARATION_M = 150.0

# Walking less than this is not worth the advice. Moving a distance equal to
# the current baseline turns an on-the-line station into a roughly 45-degree
# offset, so the suggestion is the larger of the two.
MIN_HELPFUL_MOVE_M = 300.0

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


@dataclass(frozen=True)
class Advice:
    """What the field team should do about the geometry they have.

    Produced from the bearings as they stand, so everything in it is measured
    rather than assumed. That matters: the obvious version of this check — warn
    the second team, before they aim, that they are standing on the first
    team's line of sight — cannot be made to work. The crossing angle depends
    on the range to the animal as much as on the angle, and in this project's
    own data the animal sits about as far away as the teams are apart (median
    273 m against a 248 m baseline), where even a 2-degree offset still crosses
    steeply. Thresholding the angle alone warned on 24 rounds that produced a
    position, 8 of them good. Once the second bearing exists there is nothing
    left to assume.
    """

    ok: bool
    code: str  # waiting | same-spot | on-line | too-close | shallow | ok
    message: str
    crossing_deg: float | None = None
    baseline_m: float | None = None
    move_bearing_deg: float | None = None
    move_metres: float | None = None

    @property
    def move_note(self) -> str:
        """"Walk 320 m on 253°", or empty when there is nothing to suggest."""
        if self.move_bearing_deg is None or self.move_metres is None:
            return ""
        return f"Walk about {self.move_metres:.0f} m on {self.move_bearing_deg:.0f}°."


def _acute(a: float, b: float) -> float:
    """Smallest angle between two directions, folded into 0-90 degrees.

    Folding is what makes this about the *line* rather than the direction.
    Standing 180 degrees behind the other team's aim is as useless as standing
    in front of it: either way both bearings run along the same line.
    """
    delta = abs(((a - b + 180.0) % 360.0) - 180.0)
    return min(delta, 180.0 - delta)


def _widest_pair(observations):
    """The two observations furthest apart, with their separation."""
    best = (observations[0], observations[1], 0.0)
    for i, a in enumerate(observations):
        for b in observations[i + 1:]:
            gap = distance_m(a.lat, a.lon, b.lat, b.lon)
            if gap >= best[2]:
                best = (a, b, gap)
    return best


def _move_away_from(a, b, baseline: float):
    """Where to walk to open up a crossing: sideways to the line a-b."""
    across = (bearing_between(a.lat, a.lon, b.lat, b.lon) + 90.0) % 360.0
    return across, max(baseline, MIN_HELPFUL_MOVE_M)


def advise(observations) -> Advice:
    """Say whether this round's geometry can locate the animal, and what to do.

    Called while the team is still standing there, which is the whole point.
    The solver's verdict is correct but arrives after both teams have packed up
    and the animal has moved; the same verdict delivered before the reading is
    filed is something they can act on by walking fifty paces.
    """
    observations = [o for o in observations if o is not None]
    if len(observations) < 2:
        return Advice(
            ok=True,
            code="waiting",
            message="One bearing so far — the second observer's reading completes the round.",
        )

    a, b, baseline = _widest_pair(observations)
    across, metres = _move_away_from(a, b, baseline)

    if baseline < MIN_BASELINE_M:
        return Advice(
            ok=False,
            code="same-spot",
            message=(
                f"All these bearings were taken within {baseline:.0f} m of each other. "
                "Lines that start from the same place cross at your own feet, not at "
                "the animal."
            ),
            baseline_m=baseline,
            move_bearing_deg=(a.bearing_true + 90.0) % 360.0,
            move_metres=MIN_HELPFUL_MOVE_M,
        )

    try:
        fix = solve(observations)
    except TriangulationError as exc:
        # Which of the two failure shapes this is. Both produce a shallow
        # crossing, and the team's remedy differs: one pair has to turn
        # sideways, the other has to spread out.
        on_line = _acute(a.bearing_true, bearing_between(a.lat, a.lon, b.lat, b.lon))
        if on_line < MIN_OFFSET_DEG:
            return Advice(
                ok=False,
                code="on-line",
                message=(
                    f"{exc} Both bearings run within {on_line:.0f}° of the line between "
                    "the two positions, so the animal is roughly between you and the "
                    "lines never cross. Moving further apart along that line will not "
                    "help; one of you has to step off it."
                ),
                baseline_m=baseline,
                move_bearing_deg=across,
                move_metres=metres,
            )
        if baseline < ADVISORY_SEPARATION_M:
            return Advice(
                ok=False,
                code="too-close",
                message=(
                    f"{exc} The two positions are only {baseline:.0f} m apart, which is "
                    "not enough separation for an animal at this range — the lines stay "
                    "nearly parallel."
                ),
                baseline_m=baseline,
                move_bearing_deg=across,
                move_metres=MIN_HELPFUL_MOVE_M,
            )
        return Advice(
            ok=False,
            code="shallow",
            message=str(exc),
            baseline_m=baseline,
            move_bearing_deg=across,
            move_metres=metres,
        )

    if fix.quality == "poor":
        detail = (
            "one bearing points away from the answer, which is what a 180° reading "
            "error looks like — check that the signal gets weaker when you turn the "
            "antenna around"
            if fix.reversed_indices
            else f"they cross at only {fix.crossing_angle_deg:.0f}°, so the position "
            "is smeared along the line of sight"
        )
        return Advice(
            ok=False,
            code="shallow",
            message=f"These bearings do give a position, but {detail}.",
            crossing_deg=fix.crossing_angle_deg,
            baseline_m=baseline,
            move_bearing_deg=across,
            move_metres=metres,
        )

    return Advice(
        ok=True,
        code="ok",
        message=(
            f"{len(observations)} bearings crossing at {fix.crossing_angle_deg:.0f}° "
            f"from {baseline:.0f} m apart — {fix.quality} geometry."
        ),
        crossing_deg=fix.crossing_angle_deg,
        baseline_m=baseline,
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
