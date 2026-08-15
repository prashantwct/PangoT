release: flask db upgrade
# Threaded workers, not the sync default: /api/stream holds a connection open
# for the life of each dashboard tab, and a sync worker can only serve one
# request at a time. With --threads 8 a couple of workers comfortably carry the
# live streams plus normal traffic. STREAM_MAX_CLIENTS in app.py caps the
# streams so they can never occupy every thread.
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --worker-class gthread --threads 8 --timeout 120 --access-logfile - --error-logfile -
