import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from config import Config
from extensions import db as _db

FIELD_TOKEN = "test-field-token"
COORDINATOR = ("coordinator", "test-password")


@pytest.fixture
def app():
    config = Config(
        env={
            "FLASK_ENV": "testing",
            "DATABASE_URL": "sqlite://",  # in-memory
            "SECRET_KEY": "test-secret-key",
            "ADMIN_USERNAME": COORDINATOR[0],
            "ADMIN_PASSWORD_HASH": generate_password_hash(COORDINATOR[1]),
            "FIELD_TOKEN": FIELD_TOKEN,
        },
        testing=True,
    )
    application = create_app(config)
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def field(client):
    """A client that sends the field-device token on every request."""

    class FieldClient:
        def post(self, path, json):
            return client.post(path, json=json, headers={"X-Field-Token": FIELD_TOKEN})

        def get(self, path):
            return client.get(path, headers={"X-Field-Token": FIELD_TOKEN})

    return FieldClient()


@pytest.fixture
def coordinator(client):
    """A client already signed in to the dashboard."""
    response = client.post(
        "/login",
        data={"username": COORDINATOR[0], "password": COORDINATOR[1]},
        follow_redirects=False,
    )
    assert response.status_code == 302, "login fixture failed"
    return client
