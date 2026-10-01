"""Configuration that decides how the app reaches its database."""


# --- which Postgres driver the URL names -----------------------------------
#
# Found when a rebuilt environment resolved SQLAlchemy 2.1 against a pinned
# psycopg2-binary: a bare postgresql:// URL resolves to psycopg (v3) on 2.1 and
# psycopg2 on 2.0, so the app could no longer open the database at all.


def test_a_legacy_postgres_url_is_pointed_at_the_installed_driver():
    from config import _name_the_postgres_driver

    assert (_name_the_postgres_driver("postgres://user:secret@db.example.com/pangot")
            == "postgresql+psycopg2://user:secret@db.example.com/pangot")


def test_a_bare_postgresql_url_names_the_driver_too():
    """The trap: this one looks fine and breaks on a SQLAlchemy bump."""
    from config import _name_the_postgres_driver

    assert (_name_the_postgres_driver("postgresql://user:secret@db.example.com/pangot")
            == "postgresql+psycopg2://user:secret@db.example.com/pangot")


def test_a_url_that_already_names_a_driver_is_left_alone():
    from config import _name_the_postgres_driver

    for uri in ("postgresql+psycopg2://user:secret@db.example.com/pangot", "postgresql+psycopg://user:secret@db.example.com/pangot"):
        assert _name_the_postgres_driver(uri) == uri


def test_sqlite_is_untouched():
    from config import _name_the_postgres_driver

    assert _name_the_postgres_driver("sqlite:///x.db") == "sqlite:///x.db"
    assert _name_the_postgres_driver("sqlite://") == "sqlite://"


def test_the_driver_a_postgres_url_resolves_to_is_actually_installed():
    """The property that matters, asked of SQLAlchemy rather than assumed.

    import_dbapi() raises if the driver is missing, which is exactly the
    failure this guards: engine creation dying on ModuleNotFoundError.
    """
    from sqlalchemy.engine.url import make_url

    from config import _name_the_postgres_driver

    url = make_url(_name_the_postgres_driver("postgresql://user:secret@db.example.com/pangot"))
    assert url.get_dialect().import_dbapi().__name__ == "psycopg2"
