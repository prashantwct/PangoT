"""Per-user coordinator accounts, attribution, and the live-update stream."""
import json

import pytest

from auth import create_user
from extensions import db
from models import CalculatedFix, User

from conftest import COORDINATOR
from test_sync import two_good_bearings


def sign_in(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


# --- bootstrap fallback -----------------------------------------------------


def test_env_admin_works_while_no_accounts_exist(client):
    """A fresh deployment must be reachable before anyone runs `flask users create`."""
    assert User.query.count() == 0
    assert sign_in(client, *COORDINATOR).status_code == 302
    assert client.get("/api/data").status_code == 200


def test_env_admin_stops_working_once_an_account_exists(app, client):
    """Leaving the fallback live would be a permanent second way in."""
    create_user("kavya", "a-long-enough-password", role="admin")

    response = sign_in(client, *COORDINATOR)

    assert response.status_code == 200, "the env fallback should no longer be accepted"
    assert client.get("/api/data").status_code == 401


# --- accounts ---------------------------------------------------------------


def test_a_created_account_can_sign_in(app, client):
    create_user("kavya", "a-long-enough-password", role="coordinator")

    assert sign_in(client, "kavya", "a-long-enough-password").status_code == 302
    assert client.get("/api/data").status_code == 200


def test_a_disabled_account_cannot_sign_in(app, client):
    user = create_user("kavya", "a-long-enough-password")
    user.disabled_at = db.func.now()
    db.session.commit()

    assert sign_in(client, "kavya", "a-long-enough-password").status_code == 200
    assert client.get("/api/data").status_code == 401


def test_the_wrong_password_is_rejected(app, client):
    create_user("kavya", "a-long-enough-password")
    assert sign_in(client, "kavya", "not-the-password").status_code == 200


def test_passwords_are_never_stored_in_the_clear(app):
    create_user("kavya", "a-long-enough-password")
    user = User.query.filter_by(username="kavya").one()
    assert "a-long-enough-password" not in user.password_hash


def test_to_dict_never_serialises_a_password_hash(app):
    from models import to_dict

    create_user("kavya", "a-long-enough-password")
    serialised = to_dict(User.query.filter_by(username="kavya").one())

    assert "password_hash" not in serialised
    assert serialised["username"] == "kavya"


# --- role gating ------------------------------------------------------------


def test_a_coordinator_cannot_manage_accounts(app, client):
    create_user("admin-user", "a-long-enough-password", role="admin")
    create_user("kavya", "a-long-enough-password", role="coordinator")
    sign_in(client, "kavya", "a-long-enough-password")

    assert client.get("/users").status_code == 403
    assert client.post("/api/users", json={
        "username": "sneaky", "password": "a-long-enough-password", "role": "admin",
    }).status_code == 403
    assert User.query.filter_by(username="sneaky").first() is None


def test_an_admin_can_create_an_account(app, client):
    create_user("admin-user", "a-long-enough-password", role="admin")
    sign_in(client, "admin-user", "a-long-enough-password")

    response = client.post("/api/users", json={
        "username": "kavya", "password": "a-long-enough-password", "role": "coordinator",
    })

    assert response.status_code == 200
    assert User.query.filter_by(username="kavya").first() is not None


def test_short_passwords_are_refused(app, client):
    create_user("admin-user", "a-long-enough-password", role="admin")
    sign_in(client, "admin-user", "a-long-enough-password")

    response = client.post("/api/users", json={"username": "kavya", "password": "short"})

    assert response.status_code == 400
    assert "12 characters" in response.get_json()["message"]


def test_duplicate_usernames_are_refused(app, client):
    create_user("admin-user", "a-long-enough-password", role="admin")
    sign_in(client, "admin-user", "a-long-enough-password")

    response = client.post("/api/users", json={
        "username": "admin-user", "password": "a-long-enough-password",
    })

    assert response.status_code == 409


def test_the_last_admin_cannot_be_disabled(app, client):
    """Locking every admin out would need database access to undo."""
    admin = create_user("admin-user", "a-long-enough-password", role="admin")
    other = create_user("kavya", "a-long-enough-password", role="admin")
    sign_in(client, "admin-user", "a-long-enough-password")

    assert client.post(f"/api/users/{other.id}/disable").status_code == 200
    # admin-user is now the only active admin, and is also the signed-in user.
    response = client.post(f"/api/users/{admin.id}/disable")
    assert response.status_code == 400


def test_you_cannot_disable_yourself(app, client):
    admin = create_user("admin-user", "a-long-enough-password", role="admin")
    create_user("other-admin", "a-long-enough-password", role="admin")
    sign_in(client, "admin-user", "a-long-enough-password")

    response = client.post(f"/api/users/{admin.id}/disable")

    assert response.status_code == 400
    assert "your own account" in response.get_json()["message"]


# --- attribution ------------------------------------------------------------


def test_a_deletion_records_who_did_it(app, client, field):
    create_user("kavya", "a-long-enough-password", role="admin")
    field.post("/sync", two_good_bearings())
    fix_id = CalculatedFix.query.filter_by(superseded_at=None).one().id
    sign_in(client, "kavya", "a-long-enough-password")

    client.delete(f"/api/fix/{fix_id}")

    fix = db.session.get(CalculatedFix, fix_id)
    assert fix.deleted_by == "kavya"
    assert fix.deleted_at is not None


def test_an_edit_records_who_did_it_and_when(app, client, field):
    create_user("kavya", "a-long-enough-password", role="admin")
    field.post("/sync", two_good_bearings())
    fix_id = CalculatedFix.query.filter_by(superseded_at=None).one().id
    sign_in(client, "kavya", "a-long-enough-password")

    client.post(f"/api/fix/{fix_id}", json={"note": "checked on foot"})

    fix = db.session.get(CalculatedFix, fix_id)
    assert fix.updated_by == "kavya"
    assert fix.updated_at is not None


def test_restoring_clears_the_deletion_record(app, client, field):
    create_user("kavya", "a-long-enough-password", role="admin")
    field.post("/sync", two_good_bearings())
    fix_id = CalculatedFix.query.filter_by(superseded_at=None).one().id
    sign_in(client, "kavya", "a-long-enough-password")

    client.delete(f"/api/fix/{fix_id}")
    client.post(f"/api/fix/{fix_id}/restore")

    fix = db.session.get(CalculatedFix, fix_id)
    assert fix.deleted_at is None
    assert fix.deleted_by is None


# --- live updates -----------------------------------------------------------


def test_the_stream_requires_a_session(client):
    assert client.get("/api/stream").status_code == 401


def test_the_fingerprint_changes_when_data_changes(app, field):
    from app import _data_fingerprint

    before = _data_fingerprint()
    field.post("/sync", two_good_bearings())
    after = _data_fingerprint()

    assert after != before
    assert after["bearings"] == before["bearings"] + 2


def test_the_fingerprint_changes_when_a_note_is_edited(app, client, field):
    """Counts alone would miss this, which is why fixes carry updated_at."""
    from app import _data_fingerprint

    create_user("kavya", "a-long-enough-password", role="admin")
    field.post("/sync", two_good_bearings())
    fix_id = CalculatedFix.query.filter_by(superseded_at=None).one().id
    sign_in(client, "kavya", "a-long-enough-password")

    before = _data_fingerprint()
    client.post(f"/api/fix/{fix_id}", json={"note": "checked on foot"})
    after = _data_fingerprint()

    assert after != before
    assert after["bearings"] == before["bearings"], "no bearings were added"


def test_the_stream_emits_an_event(app, client, field):
    """A smoke test that the generator produces well-formed SSE frames."""
    import app as app_module

    field.post("/sync", two_good_bearings())
    sign_in(client, *COORDINATOR)

    # Keep the test quick: one poll pass is enough to see the first frame.
    original = app_module.STREAM_MAX_SECONDS
    app_module.STREAM_MAX_SECONDS = 0.1
    try:
        response = client.get("/api/stream")
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        body = response.get_data(as_text=True)
    finally:
        app_module.STREAM_MAX_SECONDS = original

    assert "event: changed" in body
    payload = body.split("data: ", 1)[1].split("\n", 1)[0]
    assert json.loads(payload)["bearings"] == 2


def test_the_stream_caps_concurrent_clients(app, client):
    from app import _streams

    held = []
    try:
        while _streams.acquire():
            held.append(1)
        sign_in(client, *COORDINATOR)
        response = client.get("/api/stream")
        assert response.status_code == 503
        assert response.get_json()["fallback"] == "poll"
    finally:
        for _ in held:
            _streams.release()
