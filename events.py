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

2. An observer appearing twice starts a new event, unless the two readings are
   within ``repeat_window_minutes`` of each other — that is one person taking a
   second bearing from a few paces away, which belongs to the same round.

Rule 2 matters because two rounds can be close together in time. Rule 1 matters
because an observer may sit out a round.

Both rules prefer splitting. Splitting a real event yields "waiting for the
second observer", which is visible and recoverable; merging two events yields a
confident fix in a place the animal never was.
"""
from datetime import timedelta

# Generous enough to absorb clock skew between two phones, short enough that
# walking to a new position always lands in a new event.
DEFAULT_GAP_MINUTES = 20

# One observer re-shooting from a few paces away, still the same round.
DEFAULT_REPEAT_WINDOW_MINUTES = 3


def _at(reading):
    return reading.timestamp


def cluster_events(
    readings,
    gap_minutes=DEFAULT_GAP_MINUTES,
    repeat_window_minutes=DEFAULT_REPEAT_WINDOW_MINUTES,
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

        # An observer with no name cannot be matched against, so rule 2 cannot
        # apply. Falling back to rule 1 alone is the safe reading.
        if reading.observer:
            earlier = [r for r in current if r.observer == reading.observer]
            if earlier and _at(reading) - _at(earlier[-1]) > repeat_window:
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
