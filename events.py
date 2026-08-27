"""Split a session's bearings into separate triangulation events.

A triangulation event is one round of bearings: two or more observers shooting
the same animal at the same time, from different places. Crossing those gives
the animal's position at that moment.

Until now the only grouping was the session code, so every bearing recorded for
an animal in one session was solved together. That is correct only if the field
team starts a fresh session for every round — a manual step, and when it is
missed the failure is silent and severe. Eight bearings from four rounds do not
intersect at a point, because the animal moved between them; least squares
still returns *a* point, somewhere in the middle of a shape that means nothing,
with no indication that four real positions have been merged into one fiction.

So the split is derived from the readings instead of trusted to a button:

1. A gap longer than ``gap_minutes`` starts a new event. Observers coordinate
   by radio and shoot within a couple of minutes of each other; moving to new
   positions takes far longer.

2. An observer returning to a position they have already used in this event
   starts a new one. Teams work fixed stations: one person walks a circuit,
   takes a bearing at each, then walks it again. Coming back to a station means
   the next round has begun, however little time has passed.

3. Otherwise, an observer appearing twice starts a new event only if the two
   readings are more than ``repeat_window_minutes`` apart. Within that window it
   is one person moving between stations inside a single round.

Rule 2 is what real data needed. In an export of 1450 field bearings, rounds
taken back to back — one observer shuttling between two stations, then
repeating the circuit two minutes later — were being merged into rounds of four
bearings that crossed at 5° and produced no fix at all. Splitting on the
returned-to position turns each of those into two fixes crossing at 80°+.
Across that export it recovers 71 positions that time alone could not.

Rules 1 and 3 remain the backstop for teams who do not work fixed stations.

All three prefer splitting. Splitting a real event yields "waiting for the
second observer", which is visible and recoverable; merging two events yields a
confident fix in a place the animal never was.
"""
from datetime import timedelta

from geodesy import distance_m

# Generous enough to absorb clock skew between two phones, short enough that
# walking to a new position always lands in a new event.
DEFAULT_GAP_MINUTES = 20

# One observer moving between stations, still the same round.
DEFAULT_REPEAT_WINDOW_MINUTES = 3

# Back within this distance of a position already used in this round means the
# observer has returned to a station, so the next round has started. Matches
# triangulation.MIN_BASELINE_M: closer than this contributes no new geometry
# anyway, so treating it as the same spot loses nothing.
DEFAULT_SAME_SPOT_M = 25.0


def _at(reading):
    return reading.timestamp


def _position(reading):
    """Where the observer stood, or None if the reading does not say."""
    lat = getattr(reading, "obs_lat", None)
    lon = getattr(reading, "obs_lon", None)
    if lat is None or lon is None:
        return None
    return (lat, lon)


def _same_spot(a, b, tolerance_m):
    here, there = _position(a), _position(b)
    if here is None or there is None:
        return False
    return distance_m(here[0], here[1], there[0], there[1]) <= tolerance_m


def cluster_events(
    readings,
    gap_minutes=DEFAULT_GAP_MINUTES,
    repeat_window_minutes=DEFAULT_REPEAT_WINDOW_MINUTES,
    same_spot_m=DEFAULT_SAME_SPOT_M,
):
    """Group readings into events, oldest first.

    ``readings`` need only carry ``timestamp`` and ``observer``. They are
    sorted here rather than trusted to arrive in order, because two phones
    upload independently.
    """
    ordered = sorted(readings, key=_at)
    if not ordered:
        return []

    gap = timedelta(minutes=gap_minutes)
    repeat_window = timedelta(minutes=repeat_window_minutes)

    events = [[ordered[0]]]

    for reading in ordered[1:]:
        current = events[-1]

        if _at(reading) - _at(current[-1]) > gap:
            events.append([reading])
            continue

        # An observer with no name cannot be matched against, so rules 2 and 3
        # cannot apply. Falling back to rule 1 alone is the safe reading.
        if reading.observer:
            earlier = [r for r in current if r.observer == reading.observer]
            if earlier:
                returned = any(_same_spot(reading, r, same_spot_m) for r in earlier)
                lapsed = _at(reading) - _at(earlier[-1]) > repeat_window
                if returned or lapsed:
                    events.append([reading])
                    continue

        current.append(reading)

    return events


def event_started_at(event):
    """The identity of an event: when its first bearing was taken.

    Stable as later bearings arrive, which is what lets a re-solve recognise
    the fix it already made for this event instead of replacing it.
    """
    return min(_at(r) for r in event)
