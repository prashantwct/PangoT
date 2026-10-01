"""Audit a bearings export, and the fixes calculated from it.

    python tools/audit_fixes.py bearings.csv [fixes.csv] [--since 2026-09-01]

Both files come from the dashboard header: **Bearings CSV** and **Fixes CSV**.
Everything is read-only; nothing is written anywhere.

It answers three questions a coordinator actually has:

  1. What did the field work produce, and how much of it is usable?
  2. Which rounds produced no position, and why?
  3. Do the stored fixes still agree with what the solver makes of the same
     bearings today? A stored fix that no longer reproduces was calculated by
     older code, or from bearings that have since changed.

Reasons are grouped and counted rather than listed one per line: the point is
to show which problem dominates, then give an example to go and look at.
"""
import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from events import cluster_events, distinct_observations, event_started_at  # noqa: E402
from geodesy import bearing_between, distance_m  # noqa: E402
from triangulation import (  # noqa: E402
    MIN_BASELINE_M,
    Observation,
    TriangulationError,
    solve,
)

# A stored fix further than this from a fresh solve of the same bearings was
# not produced by this code. Well inside the uncertainty of any real fix.
FIX_AGREEMENT_M = 1.0


class Reading:
    __slots__ = ("row", "timestamp", "observer", "obs_lat", "obs_lon",
                 "bearing", "bearing_true", "accuracy", "heading_ref", "group", "pango")

    def __init__(self, row):
        self.row = row
        self.timestamp = datetime.fromisoformat(row["timestamp"])
        self.observer = row.get("observer") or None
        self.obs_lat = float(row["obs_lat"])
        self.obs_lon = float(row["obs_lon"])
        self.bearing = float(row["bearing"])
        self.bearing_true = float(row["bearing_true"])
        self.accuracy = float(row["gps_accuracy"]) if row.get("gps_accuracy") else None
        self.heading_ref = row.get("heading_ref") or "unknown"
        self.group = row["group_id"]
        self.pango = row["pango_id"]


def plausible(row):
    """Can this row be used at all?"""
    try:
        lat, lon = float(row["obs_lat"]), float(row["obs_lon"])
        float(row["bearing_true"])
        datetime.fromisoformat(row["timestamp"])
    except (ValueError, TypeError, KeyError):
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def heading(line):
    print()
    print(line)
    print("-" * len(line))


def tally(title, counter, total=None, examples=None):
    if not counter:
        return
    heading(title)
    for reason, n in counter.most_common():
        share = f"  ({n / total:.0%})" if total else ""
        print(f"  {n:4}  {reason}{share}")
        if examples and reason in examples:
            print(f"        e.g. {examples[reason]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bearings", help="Bearings CSV from the dashboard")
    parser.add_argument("fixes", nargs="?", help="Fixes CSV from the dashboard")
    parser.add_argument("--since", help="Only bearings on or after this date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, help="Only the last N days of bearings")
    args = parser.parse_args()

    rows = list(csv.DictReader(open(args.bearings, newline="")))
    if not rows:
        print("The bearings file is empty.")
        return 1

    usable = [r for r in rows if plausible(r)]
    unusable = [r for r in rows if not plausible(r)]

    latest = max(datetime.fromisoformat(r["timestamp"]) for r in usable)
    since = None
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif args.days:
        since = latest - timedelta(days=args.days)

    window = [r for r in usable if since is None
              or datetime.fromisoformat(r["timestamp"]) >= since]

    print("=" * 72)
    print("PangoT — bearings and fix audit")
    print("=" * 72)
    print(f"  file              : {args.bearings}")
    print(f"  rows              : {len(rows)}")
    print(f"  window            : {since.date() if since else 'everything'}"
          f" to {latest.date()}")
    print(f"  bearings in window: {len(window)}")

    if unusable:
        heading("Rows that cannot be used at all")
        print(f"  {len(unusable)} row(s) have a coordinate or timestamp that is not valid.")
        for r in unusable[:4]:
            print(f"    {r.get('timestamp', '?')[:19]}  {r.get('group_id')}/{r.get('pango_id')}"
                  f"  obs_lat={r.get('obs_lat')!r} obs_lon={r.get('obs_lon')!r}")
        print("  A latitude or longitude written without its decimal point looks like")
        print("  this. Current validation rejects them, so these predate it.")

    if not window:
        print("\nNo usable bearings in that window.")
        return 0

    audit_bearings(window)
    rounds_by_pair = audit_rounds(window)
    if args.fixes:
        audit_stored_fixes(args.fixes, rounds_by_pair, since)
    else:
        print()
        print("No fixes file given, so stored fixes were not checked. Pass the")
        print("Fixes CSV as a second argument to compare them against a fresh solve.")
    return 0


def audit_bearings(window):
    readings = [Reading(r) for r in window]

    heading("The bearings themselves")
    print(f"  sessions          : {len({r.group for r in readings})}")
    print(f"  animals           : {len({r.pango for r in readings})}")
    print(f"  observers recorded: {len({r.observer for r in readings if r.observer})}")

    refs = Counter(r.heading_ref for r in readings)
    print(f"  compass reference : " + ", ".join(f"{k} {v}" for k, v in refs.most_common()))
    if refs.get("unknown"):
        share = refs["unknown"] / len(readings)
        print(f"      {share:.0%} of bearings do not record which north they were read")
        print("      against. Declination cannot be verified for those.")

    # Duplicate records: same moment, place and bearing, different reading_id.
    seen = Counter((r.group, r.pango, r.timestamp, round(r.obs_lat, 7),
                    round(r.obs_lon, 7), round(r.bearing, 4)) for r in readings)
    repeats = {k: n for k, n in seen.items() if n > 1}
    if repeats:
        extra = sum(repeats.values()) - len(repeats)
        heading("The same observation stored more than once")
        print(f"  {extra} redundant row(s) across {len(repeats)} observation(s)"
              f"  ({extra / len(readings):.0%} of the window)")
        worst = max(repeats.items(), key=lambda kv: kv[1])
        print(f"  most repeated: {worst[1]} copies of"
              f" {worst[0][0]}/{worst[0][1]} at {worst[0][2].strftime('%d %b %H:%M:%S')}")
        print("  Each copy has its own reading_id, so the upload cannot tell them")
        print("  apart. They are ignored when solving, but the phone is making them.")

    missing = [r for r in readings if r.accuracy is None]
    poor = [r for r in readings if r.accuracy is not None and r.accuracy > 25]
    if missing or poor:
        heading("GPS accuracy")
        if missing:
            print(f"  {len(missing)} bearing(s) record no GPS accuracy"
                  f"  ({len(missing) / len(readings):.0%})")
        if poor:
            worst = max(poor, key=lambda r: r.accuracy)
            print(f"  {len(poor)} bearing(s) worse than ±25 m, worst ±{worst.accuracy:.0f} m")
            print("  A position uncertain by ±X m moves the calculated fix by about")
            print("  the same amount, before any bearing error.")


def audit_rounds(window):
    by_pair = defaultdict(list)
    for row in window:
        by_pair[(row["group_id"], row["pango_id"])].append(Reading(row))

    failures = Counter()
    examples = {}
    quality = Counter()
    crossings = []
    reversed_bearings = 0
    solved = 0
    total_rounds = 0
    rounds_by_pair = {}

    for pair, readings in by_pair.items():
        rounds = cluster_events(readings)
        rounds_by_pair[pair] = rounds
        for index, event in enumerate(rounds):
            total_rounds += 1
            observations = distinct_observations(event)
            label = (f"{pair[0]}/{pair[1]} at "
                     f"{event_started_at(event).strftime('%d %b %H:%M')}")

            if len(observations) < 2:
                # "One bearing" is the symptom. Usually the cause is that the
                # round was split off because its station had already been
                # used, which means two bearings were taken from one place —
                # and two bearings from one place cannot cross. Say that
                # instead, because the two have different remedies: one is a
                # missing second observer, the other is both observers
                # standing together.
                neighbours = []
                if index:
                    neighbours += rounds[index - 1]
                if index + 1 < len(rounds):
                    neighbours += rounds[index + 1]
                shared_station = any(
                    distance_m(observations[0].obs_lat, observations[0].obs_lon,
                               other.obs_lat, other.obs_lon) <= MIN_BASELINE_M
                    for other in neighbours
                )
                reason = ("two bearings from the same spot — split apart,"
                          " neither can cross" if shared_station
                          else "only one bearing in the round")
                failures[reason] += 1
                examples.setdefault(reason, label)
                continue

            spread = max(
                distance_m(a.obs_lat, a.obs_lon, b.obs_lat, b.obs_lon)
                for a in observations for b in observations
            )
            try:
                fix = solve([Observation(r.obs_lat, r.obs_lon, r.bearing_true)
                             for r in observations])
            except TriangulationError as exc:
                reason = str(exc).split("—")[0].split(".")[0].strip()
                if spread < MIN_BASELINE_M:
                    reason = (f"all bearings from within {MIN_BASELINE_M:.0f} m"
                              " — nothing to cross")
                failures[reason] += 1
                examples.setdefault(reason, label)
                continue

            solved += 1
            quality[fix.quality] += 1
            crossings.append(fix.crossing_angle_deg)
            if "point away" in fix.describe():
                reversed_bearings += 1

    heading("Rounds")
    print(f"  rounds in the window : {total_rounds}")
    print(f"  produced a position  : {solved}  ({solved / total_rounds:.0%})")
    print(f"  produced nothing     : {total_rounds - solved}")

    tally("Why a round produced no position", failures,
          total=total_rounds - solved, examples=examples)

    if solved:
        heading("Quality of the positions that were produced")
        for grade in ("good", "fair", "poor", "unknown"):
            if quality.get(grade):
                print(f"  {quality[grade]:4}  {grade}  ({quality[grade] / solved:.0%})")
        crossings.sort()
        mid = crossings[len(crossings) // 2]
        print(f"  crossing angle: worst {crossings[0]:.0f}°,"
              f" median {mid:.0f}°, best {crossings[-1]:.0f}°")
        shallow = sum(1 for c in crossings if c < 30)
        if shallow:
            print(f"  {shallow} position(s) cross under 30°, where a 3° aim error")
            print("  stretches the uncertainty along the line of sight.")
        if reversed_bearings:
            print(f"  {reversed_bearings} position(s) have a bearing pointing AWAY from")
            print("  the answer — what a 180° reading error looks like.")

    return rounds_by_pair


def audit_stored_fixes(path, rounds_by_pair, since):
    rows = list(csv.DictReader(open(path, newline="")))
    heading("Stored fixes against a fresh solve")
    print(f"  file  : {path}")
    print(f"  fixes : {len(rows)}")

    # Index what the solver makes of each round now.
    fresh = {}
    for pair, rounds in rounds_by_pair.items():
        for event in rounds:
            observations = distinct_observations(event)
            if len(observations) < 2:
                continue
            try:
                fix = solve([Observation(r.obs_lat, r.obs_lon, r.bearing_true)
                             for r in observations])
            except TriangulationError:
                continue
            fresh[(pair[0], pair[1], event_started_at(event).isoformat()[:19])] = fix

    agree = disagree = unmatched = outside = 0
    worst = None
    for row in rows:
        try:
            lat, lon = float(row["calc_lat"]), float(row["calc_lon"])
        except (ValueError, TypeError, KeyError):
            continue
        stamp = (row.get("event_started_at") or "")[:19]
        if since is not None and stamp:
            try:
                if datetime.fromisoformat(row["event_started_at"]) < since:
                    outside += 1
                    continue
            except ValueError:
                pass
        key = (row["group_id"], row["pango_id"], stamp)
        match = fresh.get(key)
        if match is None:
            unmatched += 1
            continue
        moved = distance_m(lat, lon, match.lat, match.lon)
        if moved <= FIX_AGREEMENT_M:
            agree += 1
        else:
            disagree += 1
            if worst is None or moved > worst[0]:
                worst = (moved, key, (lat, lon), (match.lat, match.lon))

    print(f"  reproduce exactly        : {agree}")
    print(f"  disagree with a fresh solve: {disagree}")
    print(f"  no matching round        : {unmatched}")
    if outside:
        print(f"  outside the window       : {outside}")

    if unmatched:
        print()
        print("  A fix with no matching round is one of:")
        print("    - its round is outside the bearings window you exported;")
        print("    - it has no event_started_at, so it predates rounds and was")
        print("      solved from every bearing in its session at once;")
        print("    - its bearings have been edited or removed since.")
    if disagree and worst:
        moved, key, stored, now = worst
        print()
        print(f"  Largest disagreement: {moved:.0f} m")
        print(f"    {key[0]}/{key[1]} round {key[2]}")
        print(f"    stored {stored[0]:.6f},{stored[1]:.6f}"
              f"   fresh {now[0]:.6f},{now[1]:.6f}")
        print("  Recalculate rounds on the dashboard rewrites these from the bearings.")


if __name__ == "__main__":
    sys.exit(main())
