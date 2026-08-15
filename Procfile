# Only used by hosts that support a release phase. Render's free tier does not,
# which is why the real migration runs from gunicorn.conf.py's on_starting hook
# instead — that needs no host feature and no special start command.
release: flask deploy
# Deliberately a plain gunicorn line with nothing extra in it. An earlier
# attempt put `flask deploy &&` in the start command, which crash-looped the
# service on any revision where that command did not exist. gunicorn.conf.py is
# picked up automatically and migrates before the first worker forks.
#
# Threaded workers, not the sync default: /api/stream holds a connection open
# for the life of each dashboard tab, and a sync worker can only serve one
# request at a time. With --threads 8 a couple of workers comfortably carry the
# live streams plus normal traffic. STREAM_MAX_CLIENTS in app.py caps the
# streams so they can never occupy every thread.
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 8 --timeout 120 --access-logfile - --error-logfile -
