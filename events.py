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

2. A reading from a station already used in this event, *later* than the
   reading already there, starts a new one. Two teams stand at two fixed
   stations and shoot together; when a station is occupied again, the next
   round has begun, however little time has passed.

   "Later" matters. A phone can record one observation several times — 184 rows
   in an export of 1450 were a repeat of an existing time, place and bearing,
   two of them sixteen times over — each with its own reading_id, so idempotent
   upload never saw them as duplicates. Without the requirement that time move
   on, each copy looked like the station being reoccupied and became a round of
   its own. It also makes round starts strictly increasing, so no two rounds
   share an identity; see event_started_at.

Rule 2 is what real data needed. In an export of 1450 field bearings, rounds
taken back to back — both stations shooting, then both shooting again two
minutes later — were merged into rounds of four bearings that crossed at 5° and
produced no fix at all. Splitting on the reoccupied station turns each of those
into two fixes crossing at 80°+.

WHY THERE IS NO RULE ABOUT THE SAME OBSERVER TWICE

There was one: an observer appearing twice more than three minutes apart began
a new event. It rested on the observer field naming a person, and it does not.
Teams share a login — two teams standing at two stations record under the same
initials — so "the same observer twice" is the *normal* shape of a single
round, not evidence of a second one. On that export the rule split 18 rounds
whose two teams were more than three minutes apart in shooting, costing 6
fixes and leaving 27 bearings stranded alone in rounds of one.

Rule 2 covers what that rule was meant to catch, and covers it on evidence the
app actually has. Position is recorded per reading and is never shared.

Both rules prefer splitting. Splitting a real event yields "waiting for the
second observer", which is visible and recoverable; merging two events yields a
confident fix in a place the animal never was.
"""
from datetime import timedelta

from geodesy import distance_m

# Generous enough to absorb clock skew between two phones, short enough that
# walking to a new position always lands in a new event.
DEFAULT_GAP_MINUTES = 20

# A reading within this distance of one already in the round is from the same
# station, so the station has been occupied twice and a new round has started.
# Matches triangulation.MIN_BASELINE_M: closer than this contributes no new
# geometry anyway, so treating it as the same station loses nothing.
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


def _reoccupied(reading, earlier, tolerance_m):
    """Is this reading a station being used again, rather than a repeat record?"""
    return any(
        _at(reading) > _at(r) and _same_spot(reading, r, tolerance_m)
        for r in earlier
    )


def distinct_observations(readings):
    """One entry per physical observation, oldest first.

    The same observation can be stored several times, each with its own
    reading_id. Handing all of them to the solver weights that bearing line
    once per copy, and a line repeated is a line crossing itself at 0°, which
    the solver refuses outright. Every copy is kept in the database; they are
    collapsed only where the geometry is worked out.
    """
    seen = set()
    out = []
    for reading in sorted(readings, key=_at):
        position = _position(reading)
        fingerprint = (
            _at(reading),
            None if position is None else (round(position[0], 7), round(position[1], 7)),
            round(getattr(reading, "bearing_true", None) or 0.0, 6),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        out.append(reading)
    return out


def cluster_events(
    readings,
    gap_minutes=DEFAULT_GAP_MINUTES,
    same_spot_m=DEFAULT_SAME_SPOT_M,
):
    """Group readings into events, oldest first.

    ``readings`` need only carry ``timestamp`` and a position (``obs_lat`` /
    ``obs_lon``). They are sorted here rather than trusted to arrive in order,
    because two phones upload independently.
    """
    ordered = sorted(readings, key=_at)
    if not ordered:
        return []

    gap = timedelta(minutes=gap_minutes)

    events = [[ordered[0]]]

    for reading in ordered[1:]:
        current = events[-1]

        if _at(reading) - _at(current[-1]) > gap:
            events.append([reading])
            continue

        # Deliberately not filtered by observer. Two teams share a login, so
        # the name says nothing about who stood where; the station does.
        if _reoccupied(reading, current, same_spot_m):
            events.append([reading])
            continue

        current.append(reading)

    return events


def event_started_at(event):
    """The identity of an event: when its first bearing was taken.

    Stable as later bearings arrive, which is what lets a re-solve recognise
    the fix it already made for this event instead of replacing it. Unique
    within a session and animal, because a round only ever begins at a reading
    strictly later than everything in the round before it.
    """
    return min(_at(r) for r in event)
