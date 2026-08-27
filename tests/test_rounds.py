"""Several rounds of bearings in one session.

The field report: the team took eight bearings across four rounds without
starting a new session each time. They got one location instead of four.

The animal moves between rounds, so those eight bearings intersect nowhere.
Least squares does not fail on that — it returns the point minimising total
distance to eight lines, which is a place the animal was never at, reported
with the same confidence as a real fix.
"""
import uuid

from extensions import db
from geodesy import bearing_between
from models import CalculatedFix, RawBearing

SITE_LAT, SITE_LON = 19.0500, 73.0500


def at(minutes):
    """An ISO timestamp `minutes` after the start of the night."""
    hour, minute = divmod(minutes, 60)
    return f"2026-08-16T{18 + hour:02d}:{minute:02d}:00.000Z"


def round_of_bearings(target, minutes, group="S1", pango="P01"):
    """Two observers, well separated, shooting `target` at the same moment."""
    lat, lon = target
    positions = [("MK", SITE_LAT - 0.009, SITE_LON - 0.009),
                 ("PD", SITE_LAT - 0.009, SITE_LON + 0.009)]
    return [
        {
            "reading_id": str(uuid.uuid4()),
            "group_id": group,
            "pango_id": pango,
            "observer": observer,
            "lat": obs_lat,
            "lon": obs_lon,
            "bearing": bearing_between(obs_lat, obs_lon, lat, lon),
            "heading_ref": "true",
            "accuracy": 8,
            "time": at(minutes + offset),
        }
        for offset, (observer, obs_lat, obs_lon) in enumerate(positions)
    ]


def third_observer(target, minutes, group="S1", pango="P01"):
    """A third observer, north of the target.

    Not opposite either of the other two: two observers on a straight line
    through the animal give the same bearing line, which the solver refuses as
    parallel — correctly, since it carries no extra information.
    """
    lat, lon = target
    obs_lat, obs_lon = SITE_LAT + 0.012, SITE_LON + 0.002
    return {
        "reading_id": str(uuid.uuid4()),
        "group_id": group,
        "pango_id": pango,
        "observer": "AS",
        "lat": obs_lat,
        "lon": obs_lon,
        "bearing": bearing_between(obs_lat, obs_lon, lat, lon),
        "heading_ref": "true",
        "accuracy": 8,
        "time": at(minutes),
    }


# Four places the animal was, over four rounds three quarters of an hour apart.
NIGHT = [
    ((19.0500, 73.0500), 0),
    ((19.0530, 73.0540), 45),
    ((19.0560, 73.0580), 90),
    ((19.0590, 73.0620), 135),
]


def current_fixes(pango="P01"):
    return (
        CalculatedFix.query.filter_by(pango_id=pango, superseded_at=None, deleted_at=None)
        .order_by(CalculatedFix.event_started_at)
        .all()
    )


# --- the reported failure ---------------------------------------------------


def test_four_rounds_in_one_session_give_four_fixes(app, field):
    """Eight bearings, four rounds, one session code. Four locations."""
    for target, minutes in NIGHT:
        field.post("/sync", round_of_bearings(target, minutes))

    fixes = current_fixes()

    assert RawBearing.query.count() == 8
    assert len(fixes) == 4


def test_each_fix_lands_on_the_animal_not_between_them(app, field):
    """The blended fix was the actual harm: a confident answer, in no real place."""
    for target, minutes in NIGHT:
        field.post("/sync", round_of_bearings(target, minutes))

    fixes = current_fixes()

    for fix, (target, _minutes) in zip(fixes, NIGHT):
        assert abs(fix.calc_lat - target[0]) < 1e-4, f"{fix.calc_lat} vs {target[0]}"
        assert abs(fix.calc_lon - target[1]) < 1e-4, f"{fix.calc_lon} vs {target[1]}"


def test_every_fix_is_built_from_its_own_round_only(app, field):
    for target, minutes in NIGHT:
        field.post("/sync", round_of_bearings(target, minutes))

    assert [fix.n_bearings for fix in current_fixes()] == [2, 2, 2, 2]


def test_readings_are_stamped_with_their_round(app, field):
    """The dashboard needs this to draw each bearing to the fix it made."""
    for target, minutes in NIGHT:
        field.post("/sync", round_of_bearings(target, minutes))

    rounds = {r.event_started_at for r in RawBearing.query.all()}
    assert len(rounds) == 4
    assert all(r.event_started_at is not None for r in RawBearing.query.all())


def test_bearing_rounds_match_their_fix(app, field):
    for target, minutes in NIGHT:
        field.post("/sync", round_of_bearings(target, minutes))

    fix_rounds = {f.event_started_at for f in current_fixes()}
    reading_rounds = {r.event_started_at for r in RawBearing.query.all()}
    assert fix_rounds == reading_rounds


# --- one round still behaves as before --------------------------------------


def test_a_single_round_still_gives_one_fix(app, field):
    response = field.post("/sync", round_of_bearings(NIGHT[0][0], 0))

    assert len(current_fixes()) == 1
    assert "fix found" in response.get_json()["messages"][0]


def test_a_third_observer_joining_late_extends_the_round(app, field):
    """Not a new round: one fix, from three bearings."""
    field.post("/sync", round_of_bearings(NIGHT[0][0], 0))

    field.post("/sync", [third_observer(NIGHT[0][0], 4)])

    fixes = current_fixes()
    assert len(fixes) == 1
    assert fixes[0].n_bearings == 3


# --- the properties that must not regress -----------------------------------


def test_re_uploading_the_same_round_does_not_churn_the_fix(app, field):
    """An unchanged round keeps its fix id, so the dashboard sees no new fix."""
    payload = round_of_bearings(NIGHT[0][0], 0)
    field.post("/sync", payload)
    first = current_fixes()[0].id

    field.post("/sync", payload)      # a retried upload

    assert [f.id for f in current_fixes()] == [first]


def test_a_new_round_does_not_supersede_an_earlier_one(app, field):
    """The old behaviour: every upload replaced the session's only fix."""
    field.post("/sync", round_of_bearings(NIGHT[0][0], 0))
    first = current_fixes()[0].id

    field.post("/sync", round_of_bearings(NIGHT[1][0], 45))

    ids = [f.id for f in current_fixes()]
    assert first in ids
    assert len(ids) == 2


def test_a_round_that_cannot_solve_leaves_the_others_alone(app, field):
    field.post("/sync", round_of_bearings(NIGHT[0][0], 0))
    field.post("/sync", round_of_bearings(NIGHT[1][0], 45))

    # A lone bearing in a new round: nothing to cross it with.
    orphan = round_of_bearings(NIGHT[2][0], 90)[0]
    field.post("/sync", [orphan])

    assert len(current_fixes()) == 2


def test_fixes_are_superseded_never_deleted(app, field):
    """Re-solving a round keeps the old row for the audit trail."""
    payload = round_of_bearings(NIGHT[0][0], 0)
    field.post("/sync", payload)

    field.post("/sync", [third_observer(NIGHT[0][0], 3)])

    assert len(current_fixes()) == 1
    assert CalculatedFix.query.count() == 2
    assert CalculatedFix.query.filter(CalculatedFix.superseded_at.isnot(None)).count() == 1


def test_two_animals_in_one_session_stay_separate(app, field):
    field.post("/sync", round_of_bearings(NIGHT[0][0], 0, pango="P01"))
    field.post("/sync", round_of_bearings(NIGHT[2][0], 0, pango="P02"))

    assert len(current_fixes("P01")) == 1
    assert len(current_fixes("P02")) == 1


def test_the_message_names_how_many_rounds_were_solved(app, field):
    for target, minutes in NIGHT[:2]:
        response = field.post("/sync", round_of_bearings(target, minutes))

    message = response.get_json()["messages"][0]
    assert "2 of 2 rounds solved" in message


def test_a_bad_bearing_added_to_a_round_does_not_destroy_its_fix(app, field):
    """Caught by these tests, not by review: the round's own fix was being lost.

    A third observer standing on the line between the other two and the animal
    gives a bearing that carries no new information, and the solve is refused.
    The round's existing fix must survive that.
    """
    field.post("/sync", round_of_bearings(NIGHT[0][0], 0))
    before = current_fixes()[0].id

    # Diametrically opposite PD through the animal: same line, no new information.
    useless = third_observer(NIGHT[0][0], 3)
    useless["lat"] = SITE_LAT + 0.009
    useless["lon"] = SITE_LON - 0.009
    useless["bearing"] = bearing_between(useless["lat"], useless["lon"], *NIGHT[0][0])
    response = field.post("/sync", [useless])

    assert [f.id for f in current_fixes()] == [before]
    assert response.get_json()["results"][0]["kept_previous_fix"] is True


def test_the_exports_identify_which_round_each_row_belongs_to(app, field, coordinator):
    """Four fixes for one animal in one session are otherwise indistinguishable
    in the CSV, and the bearings cannot be joined back to their fix."""
    import csv
    import io

    for target, minutes in NIGHT:
        field.post("/sync", round_of_bearings(target, minutes))

    fixes = list(csv.DictReader(io.StringIO(
        coordinator.get("/download_fixes").get_data(as_text=True))))
    bearings = list(csv.DictReader(io.StringIO(
        coordinator.get("/download_csv").get_data(as_text=True))))

    assert len(fixes) == 4
    assert len({row["event_started_at"] for row in fixes}) == 4
    assert all(row["event_started_at"] for row in bearings)

    # Every bearing joins to exactly one fix on (session, animal, round).
    keys = {(f["group_id"], f["pango_id"], f["event_started_at"]) for f in fixes}
    for row in bearings:
        assert (row["group_id"], row["pango_id"], row["event_started_at"]) in keys
