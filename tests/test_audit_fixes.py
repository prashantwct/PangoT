"""The audit script that reads a dashboard export.

It is a reporting tool, so the test is not about wording. It is that the
script runs end to end on a realistic export and names each failure that was
deliberately put in it — in particular that it tells "nobody took a second
bearing" apart from "both bearings came from the same spot", which look
identical in the data and have completely different remedies.
"""
import csv
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from geodesy import bearing_between

sys.path.insert(0, "tools")

START = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
STATION_A = (21.85635, 79.57928)
STATION_B = (21.85596, 79.58008)
TARGET = (21.8585, 79.5796)

HEADER = [
    "reading_id", "group_id", "pango_id", "event_started_at", "observer",
    "device_id", "obs_lat", "obs_lon", "gps_accuracy", "bearing",
    "heading_ref", "declination_deg", "bearing_true", "timestamp",
]


def row(group, pango, who, pos, minutes, bearing=None, accuracy=8, ref="true"):
    value = bearing if bearing is not None else bearing_between(pos[0], pos[1], *TARGET)
    return {
        "reading_id": str(uuid.uuid4()), "group_id": group, "pango_id": pango,
        "event_started_at": "", "observer": who, "device_id": "",
        "obs_lat": pos[0], "obs_lon": pos[1],
        "gps_accuracy": "" if accuracy is None else accuracy,
        "bearing": round(value, 1), "heading_ref": ref, "declination_deg": 0.0,
        "bearing_true": round(value, 1),
        "timestamp": (START + timedelta(minutes=minutes)).isoformat(),
    }


@pytest.fixture
def export(tmp_path):
    rows = [
        # a clean round
        row("S1", "P01", "MK", STATION_A, 0),
        row("S1", "P01", "PD", STATION_B, 1),
        # both bearings from one spot
        row("S4", "P03", "RK", STATION_A, 0),
        row("S4", "P03", "RK", (STATION_A[0] + 0.00005, STATION_A[1]), 1),
        # genuinely a lone bearing, nowhere near the others
        row("S5", "P03", "RK", (STATION_A[0] + 0.02, STATION_A[1] + 0.02), 0),
        # bad GPS, and one with none recorded
        row("S8", "P06", "MK", STATION_A, 0, accuracy=140),
        row("S8", "P06", "PD", STATION_B, 1, accuracy=None),
        # no compass reference
        row("S9", "P07", "MK", STATION_A, 0, ref="unknown"),
        row("S9", "P07", "PD", STATION_B, 1, ref="unknown"),
    ]
    # the same observation stored three times
    duplicate = row("S3", "P02", "BB", STATION_A, 0)
    rows += [duplicate, dict(duplicate, reading_id=str(uuid.uuid4())),
             dict(duplicate, reading_id=str(uuid.uuid4())),
             row("S3", "P02", "BB", STATION_B, 1)]
    # a coordinate typed without its decimal point
    rows.append(dict(row("S10", "P08", "AJ", STATION_A, 0), obs_lon=7957928.0))

    path = tmp_path / "bearings.csv"
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def run(path, capsys, *extra):
    import audit_fixes

    argv = sys.argv
    sys.argv = ["audit_fixes.py", str(path), *extra]
    try:
        assert audit_fixes.main() == 0
    finally:
        sys.argv = argv
    return capsys.readouterr().out


def test_it_runs_and_reports(export, capsys):
    out = run(export, capsys)
    assert "PangoT — bearings and fix audit" in out
    assert "Rounds" in out


def test_an_unusable_coordinate_is_called_out(export, capsys):
    out = run(export, capsys)
    assert "cannot be used at all" in out
    assert "7957928.0" in out


def test_repeated_records_are_counted(export, capsys):
    out = run(export, capsys)
    assert "stored more than once" in out
    assert "3 copies" in out


def test_two_bearings_from_one_spot_is_not_reported_as_a_missing_observer(export, capsys):
    """These look the same in the data and need opposite remedies: one needs a
    second observer, the other needs the two to stand apart."""
    out = run(export, capsys)
    assert "two bearings from the same spot" in out
    assert "only one bearing in the round" in out


def test_gps_and_compass_problems_are_reported(export, capsys):
    out = run(export, capsys)
    assert "±140 m" in out
    assert "do not record which north" in out


def test_a_window_excludes_older_bearings(export, capsys):
    out = run(export, capsys, "--since", "2027-01-01")
    assert "No usable bearings in that window" in out


def test_it_says_when_no_fixes_file_was_given(export, capsys):
    out = run(export, capsys)
    assert "stored fixes were not checked" in out
