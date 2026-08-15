# `flask deploy`, not `flask db upgrade`. A database created by an older
# db.create_all() has no alembic_version, and plain `db upgrade` then tries to
# CREATE TABLE over tables that already exist and fails — which is how a live
# field team ended up on an un-migrated database. `deploy` detects that case and
# stamps the baseline first. Safe and idempotent on every deploy.
release: flask deploy
# Threaded workers, not the sync default: /api/stream holds a connection open
# for the life of each dashboard tab, and a sync worker can only serve one
# request at a time. With --threads 8 a couple of workers comfortably carry the
# live streams plus normal traffic. STREAM_MAX_CLIENTS in app.py caps the
# streams so they can never occupy every thread.
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 8 --timeout 120 --access-logfile - --error-logfile -
