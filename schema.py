"""Detect a database whose schema does not match the code.

This exists because of a real incident. The app was deployed without its
migrations having run, so it started cleanly, served pages, accepted uploads —
and then threw on the first query touching a new column. What the field team
saw was an empty animal list and "Sync failed… quote reference cf0f6969". The
cause was knowable from the first request; nothing was looking.

Now it is checked at startup, reported by /healthz, and recognised when a query
fails, so the answer is "run the migrations" rather than an opaque reference.
"""
import logging

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

logger = logging.getLogger(__name__)

# Postgres and SQLite phrase a missing column or table differently. These are
# the fragments that mean "the schema is older than the code".
_STALE_SCHEMA_MARKERS = (
    "no such column",
    "no such table",
    "does not exist",
    "undefinedcolumn",
    "undefinedtable",
    "unknown column",
)

STALE_SCHEMA_MESSAGE = (
    "The server's database has not been upgraded to match this version of the app. "
    "Your readings are safe on this device. Ask the project lead to run the database "
    "migrations, then try again."
)


def looks_like_stale_schema(error: BaseException) -> bool:
    """Is this exception a missing column or table, rather than a real bug?"""
    text = str(error).lower()
    return any(marker in text for marker in _STALE_SCHEMA_MARKERS)


def head_revision(migrations_directory: str = "migrations"):
    """The newest revision the code ships, or None if it cannot be determined."""
    try:
        script = ScriptDirectory(migrations_directory)
        return script.get_current_head()
    except Exception:
        logger.debug("Could not read the migration scripts", exc_info=True)
        return None


def current_revision(engine):
    """The revision the database is stamped at, or None if it is not stamped."""
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except Exception:
        logger.debug("Could not read the database revision", exc_info=True)
        return None


def check(engine, migrations_directory: str = "migrations") -> dict:
    """Compare the database's revision with the code's.

    Returns a dict with ``state`` one of:

    ``ok``          the database is at the newest revision
    ``out-of-date`` the database is stamped, but behind the code
    ``unstamped``   no alembic_version table — either a database created by
                    ``create_all`` (tests) or one that predates migrations and
                    still needs stamping
    ``unknown``     the check itself could not run; never blocks anything
    """
    expected = head_revision(migrations_directory)
    actual = current_revision(engine)

    if expected is None:
        state = "unknown"
    elif actual is None:
        state = "unstamped"
    elif actual == expected:
        state = "ok"
    else:
        state = "out-of-date"

    return {"state": state, "current": actual, "expected": expected}


def log_startup_state(app, engine) -> dict:
    """Report the schema state once at boot, loudly if it is wrong."""
    status = check(engine)

    if status["state"] == "out-of-date":
        app.logger.error(
            "DATABASE SCHEMA IS OUT OF DATE: stamped at %s, this code expects %s. "
            "Uploads and the animal list will fail until you run `flask db upgrade`.",
            status["current"], status["expected"],
        )
    elif status["state"] == "unstamped":
        app.logger.warning(
            "Database has no alembic_version table. If it was created by an older "
            "version of this app, run `flask db stamp 273e298aa774` once and then "
            "`flask db upgrade`. (Expected revision: %s.)",
            status["expected"],
        )

    return status
