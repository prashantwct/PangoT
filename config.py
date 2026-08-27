"""Application configuration.

The guiding rule here: in production the app refuses to start rather than
fall back to a known-weak default. A field team finding out the app is down
is recoverable; a shared admin password sitting in a public repo is not.
"""
import os
import secrets

from werkzeug.security import generate_password_hash


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing."""


def _is_production(env) -> bool:
    """Read FLASK_ENV from the supplied environment, not the ambient one.

    Taking it from ``os.environ`` regardless made ``Config(env=...)`` only
    partly injectable: an unrelated FLASK_ENV in the shell silently changed how
    an explicitly-constructed config behaved.
    """
    return env.get("FLASK_ENV", "production").lower() not in ("development", "dev", "testing")


class Config:
    """Reads configuration from the environment and validates it."""

    def __init__(self, env=None, testing=False):
        env = os.environ if env is None else env
        self.testing = testing
        self.production = _is_production(env) and not testing

        missing = []

        # --- Database ---
        self.database_uri = env.get("DATABASE_URL") or "sqlite:///pangolin_data.db"
        # Render and Heroku still hand out the legacy postgres:// scheme, which
        # SQLAlchemy 2.x no longer registers.
        if self.database_uri.startswith("postgres://"):
            self.database_uri = self.database_uri.replace("postgres://", "postgresql://", 1)

        # --- Secret key ---
        self.secret_key = env.get("SECRET_KEY", "").strip()
        if not self.secret_key:
            if self.production:
                missing.append("SECRET_KEY")
            else:
                # Ephemeral: sessions do not survive a restart in dev, which is
                # the correct trade for never shipping a guessable default.
                self.secret_key = secrets.token_urlsafe(48)

        # --- Coordinator login ---
        self.admin_username = env.get("ADMIN_USERNAME", "").strip()
        self.admin_password_hash = env.get("ADMIN_PASSWORD_HASH", "").strip()
        self.password_hash_is_legacy = False

        if not self.admin_password_hash:
            # Transitional: accept a plaintext password so an existing
            # deployment does not go dark mid-season, but hash it on the way in
            # and warn loudly. See README "Migrating the admin password".
            legacy = env.get("ADMIN_PASSWORD", "").strip()
            if legacy:
                self.admin_password_hash = generate_password_hash(legacy)
                self.password_hash_is_legacy = True

        if self.production:
            if not self.admin_username:
                missing.append("ADMIN_USERNAME")
            if not self.admin_password_hash:
                missing.append("ADMIN_PASSWORD_HASH")
        else:
            self.admin_username = self.admin_username or "admin"
            if not self.admin_password_hash:
                self.admin_password_hash = generate_password_hash("dev-only-password")

        # --- Field device token ---
        self.field_token = env.get("FIELD_TOKEN", "").strip()
        if not self.field_token:
            if self.production:
                missing.append("FIELD_TOKEN")
            else:
                self.field_token = "dev-field-token"

        # --- Deployment ---
        # Migrate on boot by default. Render's free tier has no pre-deploy hook
        # and no shell, so this is the only place migrations can reliably run.
        # Set AUTO_MIGRATE=0 to take manual control.
        self.auto_migrate = env.get("AUTO_MIGRATE", "1").strip().lower() not in ("0", "false", "no")

        # A one-off correction, run once at boot. Opt-in, because it rewrites
        # which fixes are current. "dry-run" reports without changing anything.
        # See run_boot_refix in app.py.
        self.refix_on_boot = env.get("REFIX_ON_BOOT", "").strip()
        # Named explicitly rather than keeping the whole environment on this
        # object. Config is not serialised anywhere today, but holding
        # SECRET_KEY, the database password and the admin hash on an attribute
        # is one careless debug endpoint away from being published — and this
        # repository has done that once already.
        self.refix_since = env.get("REFIX_SINCE", "").strip()
        self.refix_until = env.get("REFIX_UNTIL", "").strip()

        # --- Map ---
        self.mapbox_token = env.get("MAPBOX_TOKEN", "").strip()

        # --- Site defaults (used for declination when a reading lacks a position) ---
        self.default_site_lat = _as_float(env.get("DEFAULT_SITE_LAT"), 19.05)
        self.default_site_lon = _as_float(env.get("DEFAULT_SITE_LON"), 73.05)

        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(sorted(missing))
                + ". Copy .env.example to .env and fill it in, or set these in "
                "your host's environment. The app will not start with insecure "
                "defaults in production."
            )

    def as_flask_config(self) -> dict:
        return {
            "SQLALCHEMY_DATABASE_URI": self.database_uri,
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "SQLALCHEMY_ENGINE_OPTIONS": {
                # Managed Postgres drops idle connections; recycle before it does.
                "pool_recycle": 280,
                "pool_pre_ping": True,
            },
            "SECRET_KEY": self.secret_key,
            "TESTING": self.testing,
            # Session cookie hardening. SameSite=Lax still allows the field app
            # and dashboard to work while blocking cross-site form posts.
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": self.production,
            "WTF_CSRF_ENABLED": not self.testing,
            # Coordinators leave the dashboard open for a whole field day; an
            # expiring token would log them out mid-edit for no security gain.
            "WTF_CSRF_TIME_LIMIT": None,
        }


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
