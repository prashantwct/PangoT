"""Gunicorn configuration.

Picked up automatically by `gunicorn app:app` when run from the project root,
so the deployment's start command stays plain and has nothing in it that can
drift out of step with the deployed code.

The point of this file is `on_starting`: it runs once in the master process
before any worker forks, which is the one place a migration can run exactly
once without a lock dance between workers — and without needing a pre-deploy
hook, which Render's free tier does not have.

The same slot carries one-off data corrections, opt-in by environment
variable, for the same reason: on a host with no shell there is nowhere else
to run them. See run_boot_refix in app.py.
"""


def on_starting(server):
    """Bring the database up to date before the first worker starts."""
    try:
        from app import create_app, run_boot_refix
        from extensions import db
        from schema import run_auto_migration
    except Exception:
        server.log.exception("Could not load the app to migrate; starting anyway.")
        return

    try:
        app = create_app()
    except Exception:
        # Bad configuration surfaces when the workers try to boot, with a
        # clearer message than anything this hook could produce.
        server.log.exception("Could not build the app to migrate; starting anyway.")
        return

    with app.app_context():
        if app.config["PANGOT"].auto_migrate:
            run_auto_migration(app, db, logger_=server.log)
        else:
            server.log.info("AUTO_MIGRATE is off; skipping the migration check.")

        # After the migration, never before: the correction needs the columns
        # the migration adds. It is a no-op unless REFIX_ON_BOOT is set.
        run_boot_refix(app, db, logger_=server.log)
