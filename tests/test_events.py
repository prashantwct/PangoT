"""Splitting a session's bearings into separate triangulation events.

The field report: eight bearings were recorded across four rounds without
starting a new session each time, and the system produced one location instead
of four. The animal moved between rounds, so those eight bearings do not
intersect anywhere — but least squares still returned a point.
"""
from datetime import datetime, timedelta, timezone

import pytest

from events import (
    DEFAULT_GAP_MINUTES,
    cluster_events,
    distinct_observations,
    event_started_at,
)

# Two fixed stations about 130 m apart, as a real team works them.
STATION_A = (21.85635, 79.57928)
STATION_B = (21.85596, 79.58008)

START = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)


class Reading:
    """Only what the clustering needs."""

    def __init__(self, minutes, observer, station=None, bearing=0.0):
        self.timestamp = START + timedelta(minutes=minutes)
        self.observer = observer
        self.obs_lat, self.obs_lon = station if station else (None, None)
        self.bearing_true = bearing

    def __repr__(self):
        return f"<{self.observer} +{(self.timestamp - START).total_seconds() / 60:g}m>"


def seconds(secs, observer, station=None, bearing=0.0):
    return Reading(secs / 60, observer, station, bearing)


def observers(events):
    return [[r.observer for r in event] for event in events]


def offsets(events):
    return [[(r.timestamp - START).total_seconds() / 60 for r in event] for event in events]


# --- the reported failure ---------------------------------------------------


def test_four_rounds_in_one_session_are_four_events():
    """The exact field report: 8 bearings, 4 rounds, no new session started."""
    readings = []
    for round_index in range(4):
        base = round_index * 45      # rounds three quarters of an hour apart
        readings.append(Reading(base, "MK"))
        readings.append(Reading(base + 2, "PD"))

    events = cluster_events(readings)

    assert len(events) == 4
    assert observers(events) == [["MK", "PD"]] * 4


def test_one_round_stays_one_event():
    events = cluster_events([Reading(0, "MK"), Reading(2, "PD")])
    assert len(events) == 1


# --- rule 1: the time gap ---------------------------------------------------


def test_a_long_gap_starts_a_new_event():
    events = cluster_events([Reading(0, "MK"), Reading(DEFAULT_GAP_MINUTES + 1, "PD")])
    assert len(events) == 2


def test_a_gap_inside_the_window_does_not():
    events = cluster_events([Reading(0, "MK"), Reading(DEFAULT_GAP_MINUTES - 1, "PD")])
    assert len(events) == 1


def test_the_gap_is_measured_between_neighbours_not_from_the_start():
    """A round with three observers trickling in stays one event."""
    events = cluster_events([Reading(0, "MK"), Reading(15, "PD"), Reading(30, "AS")])
    assert len(events) == 1


# --- rule 2: a station occupied twice ---------------------------------------


def test_two_rounds_ten_minutes_apart_are_split_by_their_stations():
    """The time gap alone would merge these; the reoccupied stations do not."""
    events = cluster_events([
        Reading(0, "MK", STATION_A), Reading(1, "PD", STATION_B),
        Reading(10, "MK", STATION_A), Reading(11, "PD", STATION_B),
    ])

    assert len(events) == 2
    assert observers(events) == [["MK", "PD"], ["MK", "PD"]]


def test_the_same_login_at_two_stations_is_one_round():
    """Two teams share a login, so the observer name identifies nobody.

    This is why there is no rule about an observer appearing twice: under a
    shared login that is the normal shape of a single round. An earlier rule
    split these after three minutes, which cost real fixes on real data.
    """
    events = cluster_events([
        Reading(0, "BB", STATION_A),
        Reading(9, "BB", STATION_B),      # the other team, nine minutes later
    ])

    assert len(events) == 1


def test_readings_with_no_position_cannot_be_split_inside_the_window():
    """Nothing left to go on but the clock, and the clock says one round."""
    events = cluster_events([Reading(0, "MK"), Reading(1, "MK"), Reading(2, "PD")])
    assert len(events) == 1


# --- ordering and identity --------------------------------------------------


def test_readings_are_sorted_before_clustering():
    """Two phones upload independently, so arrival order means nothing."""
    late = Reading(0, "MK")
    early = Reading(2, "PD")

    assert cluster_events([early, late]) == cluster_events([late, early])
    assert offsets(cluster_events([early, late])) == [[0, 2]]


def test_event_identity_is_its_first_bearing():
    event = [Reading(5, "PD"), Reading(3, "MK")]
    assert event_started_at(event) == START + timedelta(minutes=3)


def test_identity_survives_a_late_bearing_joining_the_event():
    """A third observer uploading afterwards must not look like a new event."""
    first = [Reading(0, "MK"), Reading(2, "PD")]
    later = first + [Reading(4, "AS")]

    assert event_started_at(first) == event_started_at(later)


# --- edges ------------------------------------------------------------------


def test_no_readings_gives_no_events():
    assert cluster_events([]) == []


def test_a_single_reading_is_a_single_event():
    assert len(cluster_events([Reading(0, "MK")])) == 1


def test_thresholds_are_adjustable():
    readings = [Reading(0, "MK"), Reading(30, "PD")]
    assert len(cluster_events(readings, gap_minutes=60)) == 1
    assert len(cluster_events(readings, gap_minutes=10)) == 2


@pytest.mark.parametrize("count", [2, 5, 20])
def test_every_reading_lands_in_exactly_one_event(count):
    readings = [Reading(i * 30, f"OB{i % 3}") for i in range(count)]
    events = cluster_events(readings)
    assert sum(len(e) for e in events) == count


# --- rule 2: returning to a station you have already used -------------------
#
# These come from a real export. One observer walks between two fixed stations,
# takes a bearing at each about 40 seconds apart, then walks the circuit again
# two minutes later. The time rule alone merged the two circuits into a round of
# four bearings that crossed at 5° and produced no fix at all.


def test_returning_to_a_station_starts_a_new_round():
    events = cluster_events([
        seconds(0, "BB", STATION_A),      # circuit one
        seconds(36, "BB", STATION_B),
        seconds(154, "BB", STATION_B),    # back at B: circuit two has begun
        seconds(199, "BB", STATION_A),
    ])

    assert len(events) == 2
    assert offsets(events) == [[0, 0.6], [154 / 60, 199 / 60]]


def test_moving_between_stations_stays_in_one_round():
    """The same observer twice, seconds apart, at genuinely different places."""
    events = cluster_events([seconds(0, "BB", STATION_A), seconds(36, "BB", STATION_B)])

    assert len(events) == 1


def test_the_station_rule_does_not_need_the_time_rule():
    """Two circuits back to back are split however little time has passed."""
    events = cluster_events([
        seconds(0, "RK", STATION_A),
        seconds(30, "RK", STATION_B),
        seconds(45, "RK", STATION_A),     # 15 s later, but back at A
    ])

    assert len(events) == 2


def test_a_few_paces_from_a_used_station_counts_as_the_same_station():
    """GPS never repeats a position exactly; 25 m of slack matches the solver,
    which refuses a baseline shorter than that as carrying no geometry."""
    nearby = (STATION_A[0] + 0.00009, STATION_A[1])      # about 10 m north

    events = cluster_events([
        seconds(0, "BB", STATION_A),
        seconds(36, "BB", STATION_B),
        seconds(60, "BB", nearby),
    ])

    assert len(events) == 2


def test_a_genuinely_new_position_does_not_split_the_round():
    far = (STATION_A[0] + 0.0045, STATION_A[1])          # about 500 m north

    events = cluster_events([
        seconds(0, "BB", STATION_A),
        seconds(36, "BB", STATION_B),
        seconds(60, "BB", far),
    ])

    assert len(events) == 1


def test_readings_without_a_position_fall_back_to_the_time_rule():
    events = cluster_events([Reading(0, "BB"), Reading(1, "BB"), Reading(2, "PD")])
    assert len(events) == 1


def test_two_observers_at_their_own_stations_are_one_round():
    """The ordinary case: each observer has a station and neither returns."""
    events = cluster_events([seconds(0, "MK", STATION_A), seconds(40, "PD", STATION_B)])
    assert len(events) == 1


def test_the_station_tolerance_is_adjustable():
    close = (STATION_A[0] + 0.0009, STATION_A[1])        # about 100 m

    readings = [seconds(0, "BB", STATION_A), seconds(36, "BB", STATION_B),
                seconds(60, "BB", close)]

    assert len(cluster_events(readings, same_spot_m=25)) == 1
    assert len(cluster_events(readings, same_spot_m=150)) == 2


# --- the same observation stored more than once -----------------------------
#
# A phone can save one observation several times, each copy with its own
# reading_id, so idempotent upload never sees them as duplicates. 184 rows of
# a real 1450-row export were copies; two observations appeared sixteen times.


def test_a_repeated_record_is_not_a_station_being_reoccupied():
    """Without this, every copy became a round of its own."""
    events = cluster_events([
        seconds(0, "BB", STATION_A),
        seconds(0, "BB", STATION_A),      # the same observation, stored again
        seconds(0, "BB", STATION_A),
        seconds(36, "BB", STATION_B),
    ])

    assert len(events) == 1
    assert len(events[0]) == 4


def test_a_station_reoccupied_later_still_splits():
    """The rule needs time to have moved on, and here it has."""
    events = cluster_events([
        seconds(0, "BB", STATION_A),
        seconds(0, "BB", STATION_A),
        seconds(36, "BB", STATION_B),
        seconds(154, "BB", STATION_B),
    ])

    assert len(events) == 2


def test_round_identities_are_unique_within_a_session():
    """Two rounds sharing a start time made a re-solve add a second fix for the
    same round instead of recognising the one it already had."""
    readings = [
        seconds(0, "BB", STATION_A), seconds(0, "BB", STATION_A),
        seconds(36, "BB", STATION_B), seconds(36, "BB", STATION_B),
        seconds(154, "BB", STATION_B), seconds(199, "BB", STATION_A),
    ]

    starts = [event_started_at(e) for e in cluster_events(readings)]

    assert len(starts) == len(set(starts))
    assert starts == sorted(starts)


def test_distinct_observations_collapses_copies():
    a = seconds(0, "BB", STATION_A, bearing=170.0)
    same = seconds(0, "BB", STATION_A, bearing=170.0)
    other_bearing = seconds(0, "BB", STATION_A, bearing=171.0)
    other_place = seconds(0, "BB", STATION_B, bearing=170.0)
    later = seconds(36, "BB", STATION_A, bearing=170.0)

    kept = distinct_observations([a, same, other_bearing, other_place, later])

    assert len(kept) == 4
    assert same not in kept


def test_distinct_observations_keeps_everything_when_nothing_repeats():
    readings = [seconds(0, "MK", STATION_A, 10.0), seconds(36, "PD", STATION_B, 200.0)]
    assert distinct_observations(readings) == readings


def test_distinct_observations_is_ordered_oldest_first():
    late = seconds(36, "PD", STATION_B, 200.0)
    early = seconds(0, "MK", STATION_A, 10.0)
    assert distinct_observations([late, early]) == [early, late]
