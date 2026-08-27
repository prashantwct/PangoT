"""Correcting data already collected.

The field team recorded two weeks of bearings before rounds existed, so their
sessions hold at most one fix per animal and the later rounds were never
calculated. The bearings are intact, so re-solving recovers the positions.

This is the path that touches real field data, on a host with no shell, so the
properties that matter are: it can be scoped, it can be rehearsed, it never
deletes, and running it twice is not different from running it once.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from extensions import db
from geodesy import bearing_between
from models import CalculatedFix, RawBearing

SITE_LAT, SITE_LON = 19.0500, 73.0500
OBSERVERS = [("MK", SITE_LAT - 0.009, SITE_LON - 0.009),
             ("PD", SITE_LAT - 0.009, SITE_LON + 0.009)]

# Two weeks back, so the window tests mean something.
TODAY = datetime(2026, 8, 27, tzinfo=timezone.utc)


def record_round(target, when, group="S1", pango="P01"):
    """Write a round of bearings straight to the database, as an old build did:
    no event_started_at, because the column did not exist."""
    for index, (who, olat, olon) in enumerate(OBSERVERS):
        bearing = bearing_between(olat, olon, *target)
        db.session.add(RawBearing(
            reading_id=str(uuid.uuid4()), group_id=group, pango_id=pango, observer=who,
            obs_lat=olat, obs_lon=olon, gps_accuracy=8, bearing=bearing,
            heading_ref="true", declination_deg=0.0, bearing_true=bearing,
            timestamp=when + timedelta(minutes=index), event_started_at=None,
        ))
    db.session.commit()


def a_night(day_offset, group, pango="P01", rounds=4):
    """A night's work: several rounds, animal moving between them."""
    start = TODAY - timedelta(days=day_offset)
    for n in range(rounds):
        record_round(
            (SITE_LAT + 0.003 * n, SITE_LON + 0.004 * n),
            start + timedelta(minutes=45 * n),
            group=group, pango=pango,
        )


def current(pango="P01"):
    return (CalculatedFix.query.filter_by(pango_id=pango, superseded_at=None, deleted_at=None)
            .order_by(CalculatedFix.event_started_at).all())


@pytest.fixture
def two_weeks(app):
    """Three nights inside the last fortnight, one well outside it."""
    a_night(2, "RECENT1")
    a_night(9, "RECENT2")
    a_night(13, "RECENT3")
    a_night(40, "OLD")
    return app


# --- the correction ---------------------------------------------------------


def test_refix_recovers_the_rounds_that_were_never_calculated(two_weeks):
    from app import run_refix

    assert current() == []          # nothing solved yet, as after an old import

    report = run_refix()

    assert report["split"] == 4
    assert len(current()) == 16     # 4 nights x 4 rounds


def test_each_recovered_fix_lands_on_the_animal(two_weeks):
    from app import run_refix

    run_refix()

    for fix in current():
        n = round((fix.calc_lat - SITE_LAT) / 0.003)
        assert abs(fix.calc_lat - (SITE_LAT + 0.003 * n)) < 1e-4
        assert abs(fix.calc_lon - (SITE_LON + 0.004 * n)) < 1e-4


# --- scoping to a window ----------------------------------------------------


def test_a_window_leaves_older_sessions_alone(two_weeks):
    """"Correct the last two weeks" must not quietly rewrite the whole season."""
    from app import run_refix

    run_refix(since=TODAY - timedelta(days=14))

    groups = {fix.group_id for fix in current()}
    assert groups == {"RECENT1", "RECENT2", "RECENT3"}
    assert "OLD" not in groups


def test_a_window_selects_by_bearing_date_not_solve_date(two_weeks):
    from app import run_refix

    report = run_refix(since=TODAY - timedelta(days=10))

    assert report["pairs"] == 2      # the 9-day and 2-day nights
    assert {fix.group_id for fix in current()} == {"RECENT1", "RECENT2"}


def test_a_window_matching_nothing_says_so_rather_than_failing(two_weeks):
    from app import run_refix

    report = run_refix(since=TODAY + timedelta(days=1))

    assert report["pairs"] == 0
    assert "nothing to do" in report["lines"][0]


def test_a_selected_session_is_solved_from_all_its_bearings(app):
    """A round straddling the window edge must not be cut in half."""
    from app import run_refix

    start = TODAY - timedelta(days=14)
    record_round((SITE_LAT, SITE_LON), start - timedelta(minutes=1), group="EDGE")
    record_round((SITE_LAT + 0.003, SITE_LON + 0.004), start + timedelta(hours=2), group="EDGE")

    run_refix(since=start)

    # Both rounds solved, including the one that begins before the window.
    assert len(current()) == 2


# --- rehearsing it ----------------------------------------------------------


def test_a_dry_run_changes_nothing(two_weeks):
    from app import run_refix

    report = run_refix(dry_run=True)

    assert report["split"] == 4
    assert report["dry_run"] is True
    assert current() == []
    assert CalculatedFix.query.count() == 0


def test_a_dry_run_reports_what_it_would_split(two_weeks):
    from app import run_refix

    lines = "\n".join(run_refix(dry_run=True)["lines"])

    assert "8 bearings -> 4 rounds" in lines
    assert "nothing changed" in lines


# --- safety -----------------------------------------------------------------


def test_running_it_twice_changes_nothing_the_second_time(two_weeks):
    from app import run_refix

    run_refix()
    ids = sorted(fix.id for fix in current())

    second = run_refix()

    assert sorted(fix.id for fix in current()) == ids
    assert second["before"] == second["after"]


def test_refix_never_deletes_a_bearing(two_weeks):
    from app import run_refix

    before = RawBearing.query.count()
    run_refix()
    assert RawBearing.query.count() == before


def test_an_existing_fix_is_superseded_not_deleted(two_weeks):
    """The blended fix a session already had stays in the record."""
    from app import run_refix

    readings = RawBearing.query.filter_by(group_id="RECENT1").all()
    db.session.add(CalculatedFix(
        group_id="RECENT1", pango_id="P01", calc_lat=SITE_LAT, calc_lon=SITE_LON,
        n_bearings=len(readings), quality="poor", note="legacy blend",
        event_started_at=None,
    ))
    db.session.commit()

    run_refix()

    stale = CalculatedFix.query.filter_by(note="legacy blend").one()
    assert stale.superseded_at is not None
    assert stale.calc_lat == SITE_LAT


# --- the boot switch --------------------------------------------------------


def test_the_boot_switch_is_off_unless_asked_for(two_weeks):
    from app import run_boot_refix

    assert run_boot_refix(two_weeks, db, logger_=_NullLog()) is None
    assert current() == []


@pytest.mark.parametrize("setting", ["", "0", "false", "no"])
def test_the_boot_switch_stays_off_for_falsey_settings(two_weeks, setting):
    from app import run_boot_refix

    two_weeks.config["PANGOT"].refix_on_boot = setting
    assert run_boot_refix(two_weeks, db, logger_=_NullLog()) is None


def test_the_boot_switch_can_rehearse(two_weeks):
    from app import run_boot_refix

    two_weeks.config["PANGOT"].refix_on_boot = "dry-run"

    report = run_boot_refix(two_weeks, db, logger_=_NullLog())

    assert report["dry_run"] is True
    assert current() == []


def test_the_boot_switch_applies_the_window_from_the_environment(two_weeks):
    from app import run_boot_refix

    config = two_weeks.config["PANGOT"]
    config.refix_on_boot = "1"
    config.refix_since = (TODAY - timedelta(days=14)).strftime("%Y-%m-%d")

    run_boot_refix(two_weeks, db, logger_=_NullLog())

    assert "OLD" not in {fix.group_id for fix in current()}


def test_a_broken_date_does_not_stop_the_app_booting(two_weeks):
    """The whole point of running it at boot is that it cannot take the app down."""
    from app import run_boot_refix

    config = two_weeks.config["PANGOT"]
    config.refix_on_boot = "1"
    config.refix_since = "the fourteenth"

    assert run_boot_refix(two_weeks, db, logger_=_NullLog()) is None
    assert current() == []          # and nothing half-applied


class _NullLog:
    def info(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass
