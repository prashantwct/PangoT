"""The dashboard control that runs the correction.

Render's free tier has no shell, so this is how the correction is actually
triggered on the deployment holding the data. It rewrites which fixes are
current across many sessions at once, so the gates around it matter as much as
the arithmetic: admin only, CSRF enforced, and a preview that cannot write.
"""
import uuid

import pytest

from extensions import db
from geodesy import bearing_between
from models import CalculatedFix, RawBearing

SITE_LAT, SITE_LON = 19.0500, 73.0500
OBSERVERS = [("MK", SITE_LAT - 0.009, SITE_LON - 0.009),
             ("PD", SITE_LAT - 0.009, SITE_LON + 0.009)]


def at(minutes):
    hour, minute = divmod(minutes, 60)
    return f"2026-08-16T{18 + hour:02d}:{minute:02d}:00.000Z"


def a_round(target, minutes, group="S1", pango="P01"):
    return [
        {
            "reading_id": str(uuid.uuid4()), "group_id": group, "pango_id": pango,
            "observer": who, "lat": lat, "lon": lon,
            "bearing": bearing_between(lat, lon, *target),
            "heading_ref": "true", "accuracy": 8, "time": at(minutes + i),
        }
        for i, (who, lat, lon) in enumerate(OBSERVERS)
    ]


NIGHT = [((19.0500, 73.0500), 0), ((19.0530, 73.0540), 45),
         ((19.0560, 73.0580), 90), ((19.0590, 73.0620), 135)]


def current_fixes():
    return CalculatedFix.query.filter_by(superseded_at=None, deleted_at=None).all()


@pytest.fixture
def admin(coordinator):
    """The environment fallback signs in as an admin (see auth.identify)."""
    return coordinator


@pytest.fixture
def plain_coordinator(app, client):
    """A real account with the coordinator role, not admin.

    Creating any account switches the environment fallback off, so this client
    is the only way in once the fixture has run.
    """
    from auth import create_user

    create_user("watcher", "watcher-password-1", role="coordinator")
    response = client.post(
        "/login",
        data={"username": "watcher", "password": "watcher-password-1"},
        follow_redirects=False,
    )
    assert response.status_code == 302, "coordinator login failed"
    return client


@pytest.fixture
def a_night_of_bearings(app, field):
    """Bearings recorded as an older build would have: no rounds."""
    for target, minutes in NIGHT:
        field.post("/sync", a_round(target, minutes))
    for reading in RawBearing.query.all():
        reading.event_started_at = None
    for fix in CalculatedFix.query.all():
        db.session.delete(fix)
    db.session.commit()
    return app


# --- who may run it ---------------------------------------------------------


def test_signed_out_callers_are_refused(client, a_night_of_bearings):
    response = client.post("/api/refix", json={})

    assert response.status_code == 401
    assert current_fixes() == []


def test_a_coordinator_without_admin_is_refused(a_night_of_bearings, plain_coordinator):
    response = plain_coordinator.post("/api/refix", json={})

    assert response.status_code == 403
    assert "admin" in response.get_json()["message"].lower()
    assert current_fixes() == []


def test_csrf_is_enforced(app, a_night_of_bearings):
    """The dashboard sends X-CSRFToken; a cross-site form post must not work."""
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        client = app.test_client()
        client.post("/login", data={"username": "coordinator", "password": "test-password"})
        response = client.post("/api/refix", json={"apply": True})
        assert response.status_code in (400, 401, 403)
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert current_fixes() == []


# --- preview ----------------------------------------------------------------


def test_preview_is_the_default_and_writes_nothing(admin, a_night_of_bearings):
    response = admin.post("/api/refix", json={})
    body = response.get_json()

    assert response.status_code == 200
    assert body["applied"] is False
    assert body["dry_run"] is True
    assert body["split"] == 1
    assert current_fixes() == []


def test_preview_reports_what_it_would_do(admin, a_night_of_bearings):
    body = admin.post("/api/refix", json={}).get_json()

    joined = "\n".join(body["lines"])
    assert "8 bearings -> 4 rounds" in joined
    assert "Nothing has changed" in joined
    assert (body["before"], body["after"]) == (0, 4)


def test_preview_after_applying_offers_nothing_to_apply(admin, a_night_of_bearings):
    """What drives the Apply button: the delta, not the grouping."""
    admin.post("/api/refix", json={"apply": True})

    body = admin.post("/api/refix", json={}).get_json()

    assert body["before"] == body["after"]
    assert "already solved by round" in "\n".join(body["lines"])


# --- applying ---------------------------------------------------------------


def test_applying_writes_the_rounds(admin, a_night_of_bearings):
    body = admin.post("/api/refix", json={"apply": True}).get_json()

    assert body["applied"] is True
    assert body["before"] == 0
    assert body["after"] == 4
    assert len(current_fixes()) == 4


def test_applying_twice_changes_nothing_the_second_time(admin, a_night_of_bearings):
    admin.post("/api/refix", json={"apply": True})
    ids = sorted(f.id for f in current_fixes())

    second = admin.post("/api/refix", json={"apply": True}).get_json()

    assert sorted(f.id for f in current_fixes()) == ids
    assert second["before"] == second["after"]


def test_the_window_is_honoured(admin, a_night_of_bearings):
    body = admin.post("/api/refix", json={"since": "2026-09-01"}).get_json()

    assert body["pairs"] == 0
    assert "nothing to do" in body["lines"][0]


def test_a_bad_date_is_refused_without_touching_anything(admin, a_night_of_bearings):
    response = admin.post("/api/refix", json={"since": "last tuesday", "apply": True})

    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.get_json()["message"]
    assert current_fixes() == []


def test_raw_bearings_are_never_touched(admin, a_night_of_bearings):
    before = RawBearing.query.count()

    admin.post("/api/refix", json={"apply": True})

    assert RawBearing.query.count() == before
