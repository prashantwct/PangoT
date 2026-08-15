"""Security regression tests.

Authentication was rewritten wholesale here, so these pin down the specific
weaknesses that existed before rather than testing the happy path again.
"""
import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from config import Config, ConfigError
from extensions import db as _db
from models import CalculatedFix

from conftest import COORDINATOR, FIELD_TOKEN
from test_sync import two_good_bearings


# --- configuration refuses weak defaults -----------------------------------


def test_production_refuses_to_start_without_secrets():
    """The old code fell back to 'dev-key-fallback' and 'pango2025'."""
    with pytest.raises(ConfigError) as exc:
        Config(env={"FLASK_ENV": "production"})

    message = str(exc.value)
    for required in ("SECRET_KEY", "ADMIN_USERNAME", "ADMIN_PASSWORD_HASH", "FIELD_TOKEN"):
        assert required in message


def test_production_config_accepts_a_complete_environment():
    config = Config(env={
        "FLASK_ENV": "production",
        "SECRET_KEY": "x" * 40,
        "ADMIN_USERNAME": "coord",
        "ADMIN_PASSWORD_HASH": generate_password_hash("s3cret"),
        "FIELD_TOKEN": "t" * 24,
    })

    assert config.production is True
    flask_config = config.as_flask_config()
    assert flask_config["SESSION_COOKIE_SECURE"] is True
    assert flask_config["SESSION_COOKIE_HTTPONLY"] is True
    assert flask_config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert flask_config["WTF_CSRF_ENABLED"] is True


def test_legacy_plaintext_password_is_hashed_not_stored_raw():
    config = Config(env={
        "FLASK_ENV": "production",
        "SECRET_KEY": "x" * 40,
        "ADMIN_USERNAME": "coord",
        "ADMIN_PASSWORD": "pango2025",
        "FIELD_TOKEN": "t" * 24,
    })

    assert config.password_hash_is_legacy is True
    assert "pango2025" not in config.admin_password_hash


# --- CSRF is enforced on cookie-authenticated mutations --------------------


@pytest.fixture
def csrf_app():
    """An app with CSRF live, which the normal test fixture disables."""
    config = Config(
        env={
            "FLASK_ENV": "testing",
            "DATABASE_URL": "sqlite://",
            "SECRET_KEY": "test-secret-key",
            "ADMIN_USERNAME": COORDINATOR[0],
            "ADMIN_PASSWORD_HASH": generate_password_hash(COORDINATOR[1]),
            "FIELD_TOKEN": FIELD_TOKEN,
        },
        testing=True,
    )
    application = create_app(config)
    application.config["WTF_CSRF_ENABLED"] = True
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


def test_dashboard_mutations_reject_a_request_without_a_csrf_token(csrf_app):
    """The old app marked every mutating route @csrf.exempt while using Basic
    auth, so any site a signed-in coordinator visited could delete fixes."""
    client = csrf_app.test_client()
    client.post(
        "/login",
        data={"username": COORDINATOR[0], "password": COORDINATOR[1]},
        headers={"X-Requested-With": "test"},
    )

    # Seed a fix directly, bypassing the upload path.
    fix = CalculatedFix(group_id="S1", pango_id="P01", calc_lat=19.05, calc_lon=73.05)
    _db.session.add(fix)
    _db.session.commit()

    forged = client.post(f"/api/fix/{fix.id}", json={"pango_id": "P99"})
    assert forged.status_code == 400, "a cross-site POST should be rejected"

    forged_delete = client.delete(f"/api/fix/{fix.id}")
    assert forged_delete.status_code == 400
    assert _db.session.get(CalculatedFix, fix.id).deleted_at is None


# --- field token ------------------------------------------------------------


def test_a_wrong_field_token_is_rejected(client):
    response = client.post(
        "/sync", json=two_good_bearings(), headers={"X-Field-Token": "wrong-token"}
    )
    assert response.status_code == 401


def test_field_token_endpoints_reject_an_empty_token(client):
    for header in ({}, {"X-Field-Token": ""}):
        assert client.post("/sync", json=two_good_bearings(), headers=header).status_code == 401


# --- login ------------------------------------------------------------------


def test_login_rejects_a_wrong_password(client):
    response = client.post("/login", data={"username": COORDINATOR[0], "password": "nope"})
    assert response.status_code == 200          # re-renders the form
    assert client.get("/api/data").status_code == 401


@pytest.mark.parametrize("target", [
    "https://evil.example.com/steal",
    "//evil.example.com/steal",
    "http://evil.example.com",
])
def test_login_will_not_redirect_off_site(client, target):
    response = client.post(
        f"/login?next={target}",
        data={"username": COORDINATOR[0], "password": COORDINATOR[1]},
    )
    assert response.status_code == 302
    assert "evil.example.com" not in response.headers["Location"]


def test_login_honours_a_relative_next(client):
    response = client.post(
        "/login?next=/dashboard",
        data={"username": COORDINATOR[0], "password": COORDINATOR[1]},
    )
    assert response.headers["Location"].endswith("/dashboard")


def test_logout_ends_the_session(coordinator):
    assert coordinator.get("/api/data").status_code == 200
    coordinator.post("/logout")
    assert coordinator.get("/api/data").status_code == 401


# --- no secret leakage ------------------------------------------------------


def test_field_token_is_never_sent_to_the_browser(app, client):
    """The dashboard and field app must not embed the upload token."""
    for path in ("/", "/login"):
        body = client.get(path).get_data(as_text=True)
        assert FIELD_TOKEN not in body


def test_dashboard_does_not_leak_the_admin_hash(coordinator):
    body = coordinator.get("/dashboard").get_data(as_text=True)
    assert "pbkdf2" not in body and "scrypt" not in body


def test_animal_id_rejects_path_traversal_and_quotes(field):
    for bad in ("../../etc/passwd", "P01'; DROP TABLE animals;--", "<script>alert(1)</script>"):
        response = field.post("/add_animal", {"id": bad})
        assert response.status_code == 400, f"accepted {bad!r}"
