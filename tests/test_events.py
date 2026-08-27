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
    event_started_at,
)

START = datetime(2026, 8, 16, 18, 0, tzinfo=timezone.utc)


class Reading:
    """Only what the clustering needs."""

    def __init__(self, minutes, observer):
        self.timestamp = START + timedelta(minutes=minutes)
        self.observer = observer

    def __repr__(self):
        return f"<{self.observer} +{(self.timestamp - START).total_seconds() / 60:g}m>"


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


# --- rule 2: an observer appearing twice ------------------------------------


def test_the_same_observer_twice_starts_a_new_event():
    """Two rounds only ten minutes apart — the time gap alone would merge them."""
    events = cluster_events([
        Reading(0, "MK"), Reading(1, "PD"),
        Reading(10, "MK"), Reading(11, "PD"),
    ])

    assert len(events) == 2
    assert observers(events) == [["MK", "PD"], ["MK", "PD"]]


def test_an_observer_reshooting_immediately_stays_in_the_same_event():
    """One person stepping a few paces and taking a second bearing."""
    events = cluster_events([Reading(0, "MK"), Reading(1, "MK"), Reading(2, "PD")])
    assert len(events) == 1
    assert observers(events) == [["MK", "MK", "PD"]]


def test_an_unnamed_observer_falls_back_to_the_time_rule():
    """Rule 2 cannot apply without a name; it must not split on that alone."""
    events = cluster_events([Reading(0, None), Reading(5, None), Reading(9, None)])
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
