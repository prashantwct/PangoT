"""Tests for the field geometry advisory.

The failure these exist for: in September 2026 the field teams lost 12 rounds
of two bearings to geometry that could never have produced a position. Nine of
them were one of two shapes — the animal lying between the two teams, so both
needles pointed at each other, or the teams standing 55-85 m apart while aiming
at something far away. Both are obvious on the ground and invisible from it:
the solver's refusal only reached anyone the next day, from the dashboard.

The coordinates below are the real ones, so a change that stops catching what
happened in the field fails here.
"""
import triangulation as T
from triangulation import Observation as O


def codes(*observations):
    return T.advise(list(observations)).code


# --- the two reported shapes ------------------------------------------------

def test_teams_aiming_at_each_other_is_named_as_such():
    # B2BGYY / P15, 1 October 2026 16:09. 314 m apart, aims 340 and 161 — each
    # team pointing within 3 degrees of the other. Crossed at 1 degree.
    advice = T.advise([
        O(21.880167, 79.579881, 340.1),
        O(21.882879, 79.578988, 161.1),
    ])
    assert advice.code == "on-line"
    assert not advice.ok
    assert "between you" in advice.message
    # The remedy has to say "not along the line", because moving further apart
    # along it is the intuitive thing to do and makes it worse.
    assert "will not help" in advice.message


def test_stations_too_close_is_named_separately():
    # NS2QQA / Leopard, 1 October 2026. 60 m apart, both aiming north.
    advice = T.advise([
        O(22.460568, 78.419074, 2.2),
        O(22.460622, 78.419650, 1.2),
    ])
    assert advice.code == "too-close"
    assert "60 m apart" in advice.message


def test_the_two_shapes_are_not_confused():
    # Both fail at a 1 degree crossing, so only the explanation distinguishes
    # them — and the two remedies are different: turn sideways, or spread out.
    on_line = T.advise([O(21.880167, 79.579881, 340.1), O(21.882879, 79.578988, 161.1)])
    too_close = T.advise([O(22.460568, 78.419074, 2.2), O(22.460622, 78.419650, 1.2)])
    assert on_line.code != too_close.code


def test_bearings_from_one_spot_are_caught_before_the_solve():
    # Two bearings from the same place cross at a fine angle, at the observers'
    # own feet. The crossing angle cannot see this; the baseline can.
    advice = T.advise([O(21.88, 79.57, 10.0), O(21.880001, 79.570001, 100.0)])
    assert advice.code == "same-spot"
    assert "your own feet" in advice.message


# --- what it must stay quiet about -----------------------------------------

def test_good_geometry_is_not_warned_about():
    # CVDKDF / P15, taken from the same two stations as B2BGYY 48 minutes
    # earlier, but aiming 22 degrees off the line. Crossed at 43 degrees.
    advice = T.advise([
        O(21.882879, 79.578988, 185.1),
        O(21.880167, 79.579881, 322.1),
    ])
    assert advice.ok
    assert advice.code == "ok"
    assert advice.move_bearing_deg is None


def test_a_single_bearing_waits_rather_than_complaining():
    advice = T.advise([O(21.88, 79.57, 10.0)])
    assert advice.ok
    assert advice.code == "waiting"


def test_no_bearings_at_all_is_not_an_error():
    assert T.advise([]).code == "waiting"
    assert T.advise([None]).code == "waiting"


def test_a_close_pair_that_worked_is_left_alone():
    # 98 m apart and 116 m apart produced good fixes in September, which is why
    # separation is not a threshold to warn on in advance. Only a round that
    # actually failed gets the "too close" explanation.
    advice = T.advise([
        O(21.8600, 79.5750, 30.0),
        O(21.8600, 79.5760, 330.0),
    ])
    assert advice.ok, advice.message


# --- the move suggestion ----------------------------------------------------

def test_the_move_is_across_the_line_not_along_it():
    a, b = O(21.880167, 79.579881, 340.1), O(21.882879, 79.578988, 161.1)
    advice = T.advise([a, b])

    along = T.bearing_between(a.lat, a.lon, b.lat, b.lon)
    # Perpendicular to the line joining the two stations, which is the move
    # that opens the crossing angle fastest.
    assert abs(T._acute(advice.move_bearing_deg, along) - 90.0) < 0.5


def test_the_move_is_far_enough_to_matter():
    advice = T.advise([O(21.880167, 79.579881, 340.1), O(21.882879, 79.578988, 161.1)])
    assert advice.move_metres >= T.MIN_HELPFUL_MOVE_M
    assert "Walk about" in advice.move_note
    assert "°" in advice.move_note


def test_a_round_that_is_fine_suggests_no_walk():
    advice = T.advise([O(21.882879, 79.578988, 185.1), O(21.880167, 79.579881, 322.1)])
    assert advice.move_note == ""


# --- a position that solves but should not be trusted ----------------------

def test_a_reversed_bearing_is_called_out_by_name():
    # SGPDEH / P15, 1 October. It does solve — and the fix lands behind one of
    # the observers, which is what reading the back lobe of the antenna looks
    # like. Saying "poor quality" would not tell anyone what to do about it.
    advice = T.advise([
        O(21.882879, 79.578988, 161.1),
        O(21.880107, 79.579881, 322.1),
    ])
    assert advice.code == "shallow"
    assert "180°" in advice.message
    assert "turn the antenna around" in advice.message


def test_a_shallow_crossing_that_still_solves_says_what_is_wrong_with_it():
    # 2XTW9B / P12, 6 September: crossed at 12 degrees with both bearings
    # pointing the right way. A position exists, but it is a long thin ellipse
    # the map draws as a dot, so the message has to be about the crossing and
    # not about a 180 degree error.
    advice = T.advise([
        O(21.85610, 79.58088, 239.1),
        O(21.85646, 79.57929, 227.1),
    ])
    assert not advice.ok
    assert advice.code == "shallow"
    assert "smeared" in advice.message
    assert "180°" not in advice.message
    assert advice.crossing_deg is not None and advice.crossing_deg < T.POOR_CROSSING_DEG


# --- the helper -------------------------------------------------------------

def test_acute_folds_a_reciprocal_onto_zero():
    # Standing behind the other team's aim is as useless as standing in front
    # of it, so 180 degrees has to fold onto 0.
    assert T._acute(10.0, 190.0) < 1e-9
    assert T._acute(10.0, 10.0) < 1e-9
    assert abs(T._acute(0.0, 90.0) - 90.0) < 1e-9
    assert abs(T._acute(350.0, 10.0) - 20.0) < 1e-9


def test_the_widest_pair_is_the_one_that_sets_the_baseline():
    # Three observers: the advice has to judge the best separation available,
    # not whichever pair happens to come first.
    a, b, c = O(21.88, 79.57, 10.0), O(21.8801, 79.5701, 20.0), O(21.90, 79.59, 200.0)
    _, _, baseline = T._widest_pair([a, b, c])
    assert baseline > 2000
