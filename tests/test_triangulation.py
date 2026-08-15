import math

import pytest

from geodesy import bearing_between, distance_m
from triangulation import (
    MAX_PLAUSIBLE_RANGE_M,
    Observation,
    TriangulationError,
    solve,
)

# A real field site, in UTM zone 43N. The previous implementation hardcoded
# zone 44N, so this location exercises exactly the case it got wrong.
SITE_LAT, SITE_LON = 19.0500, 73.0500


def line_angle(bearing_a, bearing_b):
    """Acute angle between two bearing lines, in degrees.

    Folded twice: once because bearings wrap at 360, and once because a bearing
    line is a line rather than a ray — 10 and 190 degrees describe the same line.
    """
    delta = abs(bearing_a - bearing_b) % 180.0
    return min(delta, 180.0 - delta)


def observer_looking_at(obs_lat, obs_lon, target_lat, target_lon, error_deg=0.0):
    """An observation whose bearing points at the target, optionally mis-aimed."""
    return Observation(
        lat=obs_lat,
        lon=obs_lon,
        bearing_true=(bearing_between(obs_lat, obs_lon, target_lat, target_lon) + error_deg) % 360,
    )


def test_two_bearings_recover_the_true_position():
    animal_lat, animal_lon = SITE_LAT, SITE_LON
    observations = [
        observer_looking_at(SITE_LAT - 0.009, SITE_LON - 0.009, animal_lat, animal_lon),
        observer_looking_at(SITE_LAT - 0.009, SITE_LON + 0.009, animal_lat, animal_lon),
    ]

    fix = solve(observations)

    assert distance_m(fix.lat, fix.lon, animal_lat, animal_lon) < 1.0
    assert fix.n_bearings == 2
    assert fix.quality == "good"


def test_two_bearing_fix_reports_no_residual():
    """A square system always fits exactly; reporting 0.00 m implies precision
    the fix does not have. The residual must be absent, not zero."""
    observations = [
        observer_looking_at(SITE_LAT - 0.009, SITE_LON - 0.009, SITE_LAT, SITE_LON),
        observer_looking_at(SITE_LAT - 0.009, SITE_LON + 0.009, SITE_LAT, SITE_LON),
    ]

    fix = solve(observations)

    assert fix.rms_error_m is None
    assert "no residual" in fix.describe()


def test_accuracy_holds_far_from_the_old_hardcoded_utm_zone():
    """Regression for the hardcoded EPSG:32644.

    Zone 44N is centred on 81 E. At 73 E the grid convergence is several
    degrees, which the old solver silently absorbed as position error. A local
    frame plus geodesic-derived directions must stay accurate anywhere.
    """
    for lon in (68.0, 73.0, 78.0, 88.0):
        animal_lat, animal_lon = 19.05, lon
        observations = [
            observer_looking_at(animal_lat - 0.009, animal_lon - 0.009, animal_lat, animal_lon),
            observer_looking_at(animal_lat - 0.009, animal_lon + 0.009, animal_lat, animal_lon),
        ]

        fix = solve(observations)

        error = distance_m(fix.lat, fix.lon, animal_lat, animal_lon)
        assert error < 1.0, f"{error:.1f} m error at {lon} E"


def test_three_noisy_bearings_report_a_real_residual():
    animal_lat, animal_lon = SITE_LAT, SITE_LON
    observations = [
        observer_looking_at(SITE_LAT - 0.009, SITE_LON - 0.009, animal_lat, animal_lon, error_deg=+2.0),
        observer_looking_at(SITE_LAT - 0.009, SITE_LON + 0.009, animal_lat, animal_lon, error_deg=-2.0),
        observer_looking_at(SITE_LAT + 0.010, SITE_LON, animal_lat, animal_lon, error_deg=+1.5),
    ]

    fix = solve(observations)

    assert fix.n_bearings == 3
    assert fix.rms_error_m is not None
    assert fix.rms_error_m > 0
    # Two degrees of aim error over roughly a kilometre is tens of metres, not
    # hundreds — if this blows up the residual is being computed in wrong units.
    assert fix.rms_error_m < 100
    assert distance_m(fix.lat, fix.lon, animal_lat, animal_lon) < 100


def cross_track_m(obs, point_lat, point_lon):
    """Perpendicular distance from a point to an observation's bearing line.

    Computed independently of the solver, from spherical geometry, so it can
    check the solver's residual rather than restate it.
    """
    R = 6371008.8
    d13 = distance_m(obs.lat, obs.lon, point_lat, point_lon)
    theta13 = math.radians(bearing_between(obs.lat, obs.lon, point_lat, point_lon))
    theta12 = math.radians(obs.bearing_true)
    return abs(math.asin(math.sin(d13 / R) * math.sin(theta13 - theta12)) * R)


def test_residual_is_the_perpendicular_distance_in_metres():
    """The reported RMS must be real metres on the ground.

    Checked against an independent cross-track calculation — if the residual
    were ever left in raw projected units, or as the squared value numpy
    returns, this would diverge immediately.
    """
    animal_lat, animal_lon = SITE_LAT, SITE_LON
    observations = [
        observer_looking_at(SITE_LAT - 0.009, SITE_LON - 0.009, animal_lat, animal_lon, error_deg=+1.0),
        observer_looking_at(SITE_LAT - 0.009, SITE_LON + 0.009, animal_lat, animal_lon, error_deg=-1.0),
        observer_looking_at(SITE_LAT + 0.010, SITE_LON, animal_lat, animal_lon, error_deg=+1.5),
    ]

    fix = solve(observations)

    expected = math.sqrt(
        sum(cross_track_m(o, fix.lat, fix.lon) ** 2 for o in observations) / len(observations)
    )
    assert fix.rms_error_m == pytest.approx(expected, abs=0.5)
    # Sanity on the absolute scale: a degree or so of aim error over ~1.4 km is
    # tens of metres of cross-track offset.
    assert 1.0 < fix.rms_error_m < 60.0


def test_grazing_bearings_are_refused_rather_than_guessed():
    animal_lat, animal_lon = SITE_LAT + 0.2, SITE_LON
    observations = [
        observer_looking_at(SITE_LAT, SITE_LON - 0.0005, animal_lat, animal_lon),
        observer_looking_at(SITE_LAT, SITE_LON + 0.0005, animal_lat, animal_lon),
    ]

    with pytest.raises(TriangulationError, match="grazing|parallel|plausible"):
        solve(observations)


def test_parallel_bearings_raise():
    observations = [
        Observation(lat=SITE_LAT, lon=SITE_LON, bearing_true=45.0),
        Observation(lat=SITE_LAT + 0.01, lon=SITE_LON + 0.01, bearing_true=45.0),
    ]

    with pytest.raises(TriangulationError, match="parallel"):
        solve(observations)


def test_reversed_bearing_is_flagged():
    """A 180-degree error still intersects, just behind the observers."""
    animal_lat, animal_lon = SITE_LAT, SITE_LON
    a = observer_looking_at(SITE_LAT - 0.009, SITE_LON - 0.009, animal_lat, animal_lon)
    b = observer_looking_at(SITE_LAT - 0.009, SITE_LON + 0.009, animal_lat, animal_lon)
    flipped = [
        Observation(a.lat, a.lon, (a.bearing_true + 180) % 360),
        Observation(b.lat, b.lon, (b.bearing_true + 180) % 360),
    ]

    fix = solve(flipped)

    assert fix.reversed_indices == (0, 1)
    assert fix.quality == "poor"
    assert "180" in fix.describe()


def test_absurdly_distant_fix_is_refused():
    observations = [
        Observation(lat=SITE_LAT, lon=SITE_LON, bearing_true=0.5),
        Observation(lat=SITE_LAT, lon=SITE_LON + 0.02, bearing_true=359.5),
    ]

    with pytest.raises(TriangulationError, match="plausible|grazing"):
        solve(observations)


def test_bearings_from_the_same_spot_are_refused():
    """Both observers standing together cannot triangulate.

    Every line passes through their shared position, so the solve returns that
    position — a confident-looking fix at the observers' own feet.
    """
    observations = [
        Observation(lat=SITE_LAT, lon=SITE_LON, bearing_true=47.0),
        Observation(lat=SITE_LAT + 0.00002, lon=SITE_LON, bearing_true=313.0),
    ]

    with pytest.raises(TriangulationError, match="within .* of each other"):
        solve(observations)


def test_a_short_but_usable_baseline_is_accepted():
    """A ~300 m baseline on a ~330 m range is tight but perfectly workable."""
    animal_lat, animal_lon = SITE_LAT + 0.003, SITE_LON
    observations = [
        observer_looking_at(SITE_LAT, SITE_LON - 0.0015, animal_lat, animal_lon),
        observer_looking_at(SITE_LAT, SITE_LON + 0.0015, animal_lat, animal_lon),
    ]

    fix = solve(observations)

    assert fix.crossing_angle_deg > 35
    assert distance_m(fix.lat, fix.lon, animal_lat, animal_lon) < 5.0


def test_single_bearing_is_refused():
    with pytest.raises(TriangulationError, match="at least two|At least two"):
        solve([Observation(lat=SITE_LAT, lon=SITE_LON, bearing_true=90.0)])


def test_crossing_angle_reports_the_worst_pair_not_the_average():
    """One poorly-crossed pair caps the reported quality even when others are good.

    Observers A and B sit close together south of the animal, so their bearings
    differ by under 20 degrees. C sits to the west and crosses both of them near
    perpendicular. Averaging would call this good; the worst pair is what limits
    the fix, so that is what must be reported.
    """
    animal_lat, animal_lon = SITE_LAT, SITE_LON
    a = observer_looking_at(SITE_LAT - 0.012, SITE_LON, animal_lat, animal_lon)
    b = observer_looking_at(SITE_LAT - 0.012, SITE_LON + 0.004, animal_lat, animal_lon)
    c = observer_looking_at(SITE_LAT, SITE_LON - 0.012, animal_lat, animal_lon)

    ab_angle = line_angle(a.bearing_true, b.bearing_true)
    ac_angle = line_angle(a.bearing_true, c.bearing_true)
    assert ab_angle < 20, "test geometry: A and B should be poorly crossed"
    assert ac_angle > 60, "test geometry: A and C should be well crossed"

    fix = solve([a, b, c])

    assert fix.crossing_angle_deg == pytest.approx(ab_angle, abs=1.0)
    assert fix.quality == "poor"


def test_perpendicular_bearings_grade_good():
    animal_lat, animal_lon = SITE_LAT, SITE_LON
    observations = [
        observer_looking_at(SITE_LAT - 0.01, SITE_LON, animal_lat, animal_lon),
        observer_looking_at(SITE_LAT, SITE_LON - 0.01, animal_lat, animal_lon),
    ]

    fix = solve(observations)

    assert fix.crossing_angle_deg > 80
    assert fix.quality == "good"
    assert not fix.reversed_indices
