"""Integration tests for the upload path.

Most of these are regression tests for ways the previous implementation lost
or corrupted a day's fieldwork.
"""
import uuid

from extensions import db
from geodesy import bearing_between
from models import CalculatedFix, RawBearing

SITE_LAT, SITE_LON = 19.0500, 73.0500
WHEN = "2026-08-15T06:30:00.000Z"


def reading(group="S1", pango="P01", observer="MK", lat=None, lon=None, target=None, **overrides):
    """A well-formed upload record aimed at ``target``."""
    lat = SITE_LAT - 0.009 if lat is None else lat
    lon = SITE_LON - 0.009 if lon is None else lon
    target = target or (SITE_LAT, SITE_LON)
    record = {
        "reading_id": str(uuid.uuid4()),
        "group_id": group,
        "pango_id": pango,
        "observer": observer,
        "lat": lat,
        "lon": lon,
        "bearing": bearing_between(lat, lon, target[0], target[1]),
        "heading_ref": "true",
        "accuracy": 8,
        "time": WHEN,
    }
    record.update(overrides)
    return record


def two_good_bearings(group="S1", pango="P01"):
    return [
        reading(group, pango, "MK", SITE_LAT - 0.009, SITE_LON - 0.009),
        reading(group, pango, "PD", SITE_LAT - 0.009, SITE_LON + 0.009),
    ]


# --- auth ------------------------------------------------------------------


def test_sync_requires_the_field_token(client):
    response = client.post("/sync", json=two_good_bearings())
    assert response.status_code == 401
    assert response.get_json()["code"] == "unpaired"


def test_get_animals_requires_the_field_token(client):
    assert client.get("/get_animals").status_code == 401


def test_dashboard_requires_a_session(client):
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_api_data_requires_a_session(client):
    assert client.get("/api/data").status_code == 401


# --- happy path ------------------------------------------------------------


def test_two_bearings_produce_a_fix(field):
    response = field.post("/sync", two_good_bearings())

    assert response.status_code == 200
    body = response.get_json()
    assert body["stored"] == 2
    assert body["results"][0]["status"] == "fixed"
    assert body["results"][0]["quality"] == "good"

    fixes = CalculatedFix.query.all()
    assert len(fixes) == 1
    assert abs(fixes[0].calc_lat - SITE_LAT) < 0.0001
    assert fixes[0].rms_error_m is None  # two-bearing fix has no residual
    assert fixes[0].crossing_angle_deg > 30


def test_one_bearing_waits_for_the_second_observer(field):
    body = field.post("/sync", [two_good_bearings()[0]]).get_json()

    assert body["results"][0]["status"] == "waiting"
    assert "waiting for the second observer" in body["results"][0]["message"]
    assert CalculatedFix.query.count() == 0


# --- idempotency (F-09) ----------------------------------------------------


def test_resyncing_the_same_batch_does_not_duplicate_readings(field):
    batch = two_good_bearings()

    first = field.post("/sync", batch).get_json()
    second = field.post("/sync", batch).get_json()

    assert first["stored"] == 2 and first["duplicates"] == 0
    assert second["stored"] == 0 and second["duplicates"] == 2
    assert RawBearing.query.count() == 2


def test_resync_leaves_the_fix_unchanged(field):
    """The failure this prevents: duplicated bearings weighting the solve."""
    batch = two_good_bearings()
    field.post("/sync", batch)
    original = CalculatedFix.query.filter_by(superseded_at=None).one()
    original_lat, original_lon = original.calc_lat, original.calc_lon

    field.post("/sync", batch)

    current = CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).one()
    assert (current.calc_lat, current.calc_lon) == (original_lat, original_lon)


def test_readings_without_a_reading_id_are_still_idempotent(field):
    """Older cached clients upload without an ID; the ID is derived from content."""
    batch = [{k: v for k, v in r.items() if k != "reading_id"} for r in two_good_bearings()]

    field.post("/sync", batch)
    field.post("/sync", batch)

    assert RawBearing.query.count() == 2


# --- non-destructive recompute (F-10) --------------------------------------


def test_a_failed_resolve_keeps_the_previous_fix(field):
    """Regression for the delete-then-compute ordering.

    The old code deleted the group's fix before checking whether a replacement
    could be computed, so one unusable bearing wiped a previously good result.
    """
    from app import _recompute

    field.post("/sync", two_good_bearings())
    good_lat = CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).one().calc_lat

    # Make the stored bearings parallel, so the group can no longer be solved.
    for row in RawBearing.query.all():
        row.bearing_true = 45.0
    db.session.commit()

    result = _recompute("S1", "P01")
    db.session.commit()

    assert result["status"] == "failed"
    assert result["kept_previous_fix"] is True
    assert "parallel" in result["message"]

    current = CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).one()
    assert current.calc_lat == good_lat


def test_a_bearing_pointing_away_is_flagged_not_silently_accepted(field):
    """A 180-degree error still intersects — behind the observer."""
    field.post("/sync", two_good_bearings())

    body = field.post(
        "/sync",
        [reading("S1", "P01", "XX", SITE_LAT - 0.009, SITE_LON, bearing=180.0)],
    ).get_json()

    result = body["results"][0]
    assert result["status"] == "fixed"
    assert result["quality"] == "poor"
    assert "180" in result["message"]


def test_recompute_supersedes_rather_than_deletes(field):
    field.post("/sync", two_good_bearings())
    field.post(
        "/sync",
        [reading("S1", "P01", "AB", SITE_LAT + 0.010, SITE_LON + 0.002)],
    )

    assert CalculatedFix.query.count() == 2, "history should be kept"
    assert CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).count() == 1
    current = CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).one()
    assert current.n_bearings == 3
    assert current.rms_error_m is not None


# --- grouping by animal (F-14) ---------------------------------------------


def test_two_animals_in_one_session_are_solved_separately(field):
    batch = two_good_bearings("S1", "P01") + two_good_bearings("S1", "P07")

    field.post("/sync", batch)

    fixes = CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).all()
    assert {f.pango_id for f in fixes} == {"P01", "P07"}
    assert all(f.n_bearings == 2 for f in fixes)


def test_a_second_animals_bearings_do_not_pollute_the_first(field):
    """The old solver grouped on group_id alone and intersected everything in it."""
    elsewhere = (SITE_LAT + 0.05, SITE_LON + 0.05)
    batch = two_good_bearings("S1", "P01") + [
        reading("S1", "P07", "MK", SITE_LAT - 0.009, SITE_LON - 0.009, target=elsewhere),
        reading("S1", "P07", "PD", SITE_LAT - 0.009, SITE_LON + 0.009, target=elsewhere),
    ]

    field.post("/sync", batch)

    p01 = CalculatedFix.query.filter_by(pango_id="P01", superseded_at=None).one()
    assert abs(p01.calc_lat - SITE_LAT) < 0.0005
    assert abs(p01.calc_lon - SITE_LON) < 0.0005


# --- validation (F-18) -----------------------------------------------------


def test_one_bad_record_does_not_reject_the_whole_batch(field):
    batch = two_good_bearings()
    batch.append(reading("S1", "P02", bearing=999))

    body = field.post("/sync", batch).get_json()

    assert body["stored"] == 2
    assert len(body["rejected"]) == 1
    assert "bearing must be between 0 and 360" in body["rejected"][0]["error"]
    assert RawBearing.query.count() == 2


def test_out_of_range_coordinates_are_rejected_with_a_readable_reason(field):
    body = field.post("/sync", [reading(lat=500)]).get_json()

    assert body["stored"] == 0
    assert "lat must be between -90 and 90" in body["rejected"][0]["error"]


def test_a_missing_field_names_itself(field):
    record = reading()
    del record["bearing"]

    body = field.post("/sync", [record]).get_json()

    assert "bearing is required" in body["rejected"][0]["error"]


def test_empty_upload_is_a_clean_400(field):
    response = field.post("/sync", [])
    assert response.status_code == 400
    assert "No readings" in response.get_json()["message"]


# --- errors do not leak internals (F-19) -----------------------------------


def test_errors_do_not_leak_database_internals(field):
    response = field.post("/sync", [{"group_id": "S1"}])
    body = response.get_json()

    serialised = str(body)
    for leaked in ("sqlalchemy", "psycopg2", "Traceback", "SELECT", "raw_bearings"):
        assert leaked.lower() not in serialised.lower()


# --- fix lifecycle ---------------------------------------------------------


def test_deleting_a_fix_is_reversible(field, coordinator):
    field.post("/sync", two_good_bearings())
    fix_id = CalculatedFix.query.filter_by(superseded_at=None).one().id

    assert coordinator.delete(f"/api/fix/{fix_id}").status_code == 200
    assert db.session.get(CalculatedFix, fix_id).deleted_at is not None
    assert coordinator.get("/api/data").get_json()["totals"]["fixes"] == 0

    assert coordinator.post(f"/api/fix/{fix_id}/restore").status_code == 200
    assert db.session.get(CalculatedFix, fix_id).deleted_at is None
    assert coordinator.get("/api/data").get_json()["totals"]["fixes"] == 1


def test_updating_a_fix_validates_the_animal_id(field, coordinator):
    field.post("/sync", two_good_bearings())
    fix_id = CalculatedFix.query.filter_by(superseded_at=None).one().id

    bad = coordinator.post(f"/api/fix/{fix_id}", json={"pango_id": "P01'; DROP--"})
    assert bad.status_code == 400

    good = coordinator.post(f"/api/fix/{fix_id}", json={"pango_id": "P42", "note": "recheck"})
    assert good.status_code == 200
    assert db.session.get(CalculatedFix, fix_id).pango_id == "P42"


# --- animals ---------------------------------------------------------------


def test_animal_list_seeds_then_accepts_additions(field):
    seeded = field.get("/get_animals").get_json()
    assert "P01" in seeded

    assert field.post("/add_animal", {"id": "P99"}).status_code == 200
    assert field.post("/add_animal", {"id": "P99"}).status_code == 409
    assert "P99" in field.get("/get_animals").get_json()


def test_animal_id_is_validated(field):
    response = field.post("/add_animal", {"id": "../../etc/passwd"})
    assert response.status_code == 400
    assert "letters, numbers" in response.get_json()["message"]


def test_retired_animals_drop_off_the_field_list(field, coordinator):
    field.post("/add_animal", {"id": "P77"})
    assert coordinator.post("/api/animals/P77/retire").status_code == 200
    assert "P77" not in field.get("/get_animals").get_json()


# --- exports and health ----------------------------------------------------


def test_csv_exports_include_the_new_columns(field, coordinator):
    field.post("/sync", two_good_bearings())

    raw_csv = coordinator.get("/download_csv").get_data(as_text=True)
    assert "bearing_true" in raw_csv and "heading_ref" in raw_csv

    fixes_csv = coordinator.get("/download_fixes").get_data(as_text=True)
    assert "crossing_angle_deg" in fixes_csv and "quality" in fixes_csv


def test_health_check(client):
    assert client.get("/healthz").get_json()["status"] == "ok"


def test_api_data_paginates(field, coordinator):
    field.post("/sync", two_good_bearings())

    body = coordinator.get("/api/data?limit=1").get_json()

    assert len(body["raw"]) == 1
    assert body["totals"]["raw"] == 2
    assert body["truncated"] is True
