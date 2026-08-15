"""Regression tests for a deploy whose migrations never ran.

The incident: new code was deployed against the old schema. The app started
cleanly and served pages, then threw on the first query touching a new column.
The field team saw an empty animal list and "Sync failed… quote reference
cf0f6969" — nothing anywhere named the actual cause.
"""
import sqlite3

import pytest

from extensions import db
from schema import STALE_SCHEMA_MESSAGE, looks_like_stale_schema

from test_sync import two_good_bearings

SQLITE_SUPPORTS_DROP_COLUMN = sqlite3.sqlite_version_info >= (3, 35, 0)


# --- recognising the error --------------------------------------------------


@pytest.mark.parametrize("message", [
    "(sqlite3.OperationalError) no such column: raw_bearings.reading_id",
    "(sqlite3.OperationalError) no such table: users",
    '(psycopg2.errors.UndefinedColumn) column raw_bearings.reading_id does not exist',
    '(psycopg2.errors.UndefinedTable) relation "users" does not exist',
    "(pymysql.err.OperationalError) (1054, \"Unknown column 'reading_id' in 'field list'\")",
])
def test_stale_schema_errors_are_recognised(message):
    assert looks_like_stale_schema(Exception(message))


@pytest.mark.parametrize("message", [
    "(psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint",
    "division by zero",
    "connection refused",
])
def test_ordinary_errors_are_not_mistaken_for_a_stale_schema(message):
    assert not looks_like_stale_schema(Exception(message))


# --- the real thing ---------------------------------------------------------


@pytest.fixture
def stale_schema(app):
    """Remove a column the current code needs, reproducing the old schema."""
    if not SQLITE_SUPPORTS_DROP_COLUMN:
        pytest.skip("SQLite too old to drop a column")
    db.session.remove()
    # SQLite refuses to drop a column an index depends on.
    db.session.execute(db.text("DROP INDEX IF EXISTS ix_raw_bearings_reading_id"))
    db.session.execute(db.text("ALTER TABLE raw_bearings DROP COLUMN reading_id"))
    db.session.commit()
    return app


def test_sync_against_an_old_schema_says_what_is_wrong(stale_schema, field):
    """The message must name the fix, not just hand over a reference number."""
    response = field.post("/sync", two_good_bearings())

    assert response.status_code == 503
    body = response.get_json()
    assert body["message"] == STALE_SCHEMA_MESSAGE
    assert "migrations" in body["message"]
    # Still carries a reference for the logs, and still promises the data is safe.
    assert body["reference"]
    assert "safe on this device" in body["message"]


def test_sync_against_an_old_schema_leaks_no_internals(stale_schema, field):
    body = field.post("/sync", two_good_bearings()).get_json()

    serialised = str(body).lower()
    for leaked in ("sqlite3", "psycopg2", "traceback", "raw_bearings", "select"):
        assert leaked not in serialised


def test_animal_list_against_an_old_schema_says_what_is_wrong(app, field):
    """This is what emptied the animal list on the phones."""
    if not SQLITE_SUPPORTS_DROP_COLUMN:
        pytest.skip("SQLite too old to drop a column")
    db.session.remove()
    db.session.execute(db.text("ALTER TABLE animals DROP COLUMN retired_at"))
    db.session.commit()

    response = field.get("/get_animals")

    assert response.status_code == 503
    assert response.get_json()["message"] == STALE_SCHEMA_MESSAGE


# --- health check -----------------------------------------------------------


def test_an_unstamped_database_is_not_degraded_outside_production(client):
    """A local database built by db.create_all() has no alembic_version.

    Flagging that as degraded would cry wolf on every developer machine, so the
    severity depends on whether this is production.
    """
    response = client.get("/healthz")
    body = response.get_json()

    assert body["schema"]["state"] in ("unstamped", "unknown")
    assert response.status_code == 200
    assert body["status"] == "ok"


def test_an_unstamped_database_is_degraded_in_production():
    """In production it means migrations were never set up at all."""
    from werkzeug.security import generate_password_hash

    from app import create_app
    from config import Config
    from extensions import db as _db

    config = Config(env={
        "FLASK_ENV": "production",
        "DATABASE_URL": "sqlite://",
        "SECRET_KEY": "x" * 40,
        "ADMIN_USERNAME": "coord",
        "ADMIN_PASSWORD_HASH": generate_password_hash("a-long-enough-password"),
        "FIELD_TOKEN": "t" * 24,
    })
    application = create_app(config)
    with application.app_context():
        _db.create_all()
        response = application.test_client().get("/healthz")
        body = response.get_json()

        assert response.status_code == 503
        assert body["status"] == "degraded"
        assert "flask db upgrade" in body["action"]
        _db.session.remove()
        _db.drop_all()


def test_healthz_names_the_expected_revision(client):
    """So an operator can compare it against what the database reports."""
    schema = client.get("/healthz").get_json()["schema"]
    assert "current" in schema and "expected" in schema


# --- the deploy plan --------------------------------------------------------


def test_deploy_adopts_a_database_that_predates_migrations(app):
    """Tables but no alembic_version: stamp the baseline, then upgrade."""
    from schema import BASELINE_REVISION, plan_deploy

    plan = plan_deploy(db.engine)

    assert plan["case"] == "adopt"
    assert plan["action"] == "stamp-then-upgrade"
    assert plan["stamp"] == BASELINE_REVISION


def test_deploy_never_stamps_an_empty_database(app):
    """The dangerous mistake: stamping an empty database would skip the
    migration that creates the tables, leaving a schema with nothing in it."""
    from schema import plan_deploy

    db.session.remove()
    db.drop_all()

    plan = plan_deploy(db.engine)

    assert plan["case"] == "fresh"
    assert plan["action"] == "upgrade"
    assert "stamp" not in plan


def test_deploy_is_a_plain_upgrade_once_stamped(app):
    from schema import plan_deploy

    db.session.execute(db.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    db.session.execute(db.text("INSERT INTO alembic_version VALUES ('a1c47f2b9e30')"))
    db.session.commit()

    plan = plan_deploy(db.engine)

    assert plan["case"] == "stamped"
    assert plan["action"] == "upgrade"
    assert plan["current"] == "a1c47f2b9e30"
