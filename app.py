"""PangoT — pangolin radio-telemetry triangulation.

Two surfaces:

* ``/``          the field app. Runs offline on a phone, queues bearings
                 locally, uploads when there is signal.
* ``/dashboard`` mission control. Map, filters, fix management, CSV export.

Run locally with ``flask run``; in production with ``gunicorn app:app``.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    flash,
    stream_with_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from auth import (
    authenticate,
    create_user,
    current_coordinator,
    log_in,
    log_out,
    requires_admin,
    requires_coordinator,
    requires_field_token,
    set_password,
)
from config import Config
from extensions import csrf, db, migrate
from events import cluster_events, event_started_at
from geodesy import to_true_bearing
from models import Animal, CalculatedFix, RawBearing, User, as_utc, to_dict, utcnow
from schema import (
    STALE_SCHEMA_MESSAGE,
    check as check_schema,
    log_startup_state,
    looks_like_stale_schema,
    plan_deploy,
)
from triangulation import Observation, TriangulationError, solve
from validation import ValidationError, validate_animal_id, validate_batch

DEFAULT_ANIMAL_IDS = [f"P{i:02d}" for i in range(1, 17)]

# Live-update stream tuning. Each open stream occupies a worker thread, so the
# cap matters more than the interval.
STREAM_POLL_SECONDS = 2.0
STREAM_MAX_SECONDS = 300      # EventSource reconnects on its own afterwards
STREAM_MAX_CLIENTS = 8
API_PAGE_LIMIT = 1000
API_MAX_LIMIT = 5000


class _StreamLimiter:
    """Caps concurrent live-update streams.

    Each stream holds a worker thread until it closes, so without a cap a
    handful of stale browser tabs could occupy every thread and take the
    dashboard down for everyone.
    """

    def __init__(self, limit):
        self._lock = threading.Lock()
        self._limit = limit
        self._count = 0

    def acquire(self) -> bool:
        with self._lock:
            if self._count >= self._limit:
                return False
            self._count += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)


_streams = _StreamLimiter(STREAM_MAX_CLIENTS)


def _data_fingerprint() -> dict:
    """A cheap summary that changes whenever the dashboard's view would.

    Counts alone would miss an edited note, which is why CalculatedFix carries
    an ``updated_at``.
    """
    def as_iso(value):
        return value.isoformat() if value is not None else None

    return {
        "bearings": db.session.query(func.count(RawBearing.id)).scalar() or 0,
        "fixes": db.session.query(func.count(CalculatedFix.id)).scalar() or 0,
        "latest_bearing": as_iso(db.session.query(func.max(RawBearing.created_at)).scalar()),
        "latest_fix": as_iso(db.session.query(func.max(CalculatedFix.timestamp)).scalar()),
        "latest_edit": as_iso(db.session.query(func.max(CalculatedFix.updated_at)).scalar()),
        "latest_delete": as_iso(db.session.query(func.max(CalculatedFix.deleted_at)).scalar()),
    }


def create_app(config: Config | None = None) -> Flask:
    app = Flask(__name__)

    from dotenv import load_dotenv

    load_dotenv()

    config = config or Config()
    app.config.update(config.as_flask_config())
    app.config["PANGOT"] = config

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if config.password_hash_is_legacy:
        app.logger.warning(
            "ADMIN_PASSWORD is set as plaintext. Set ADMIN_PASSWORD_HASH instead "
            "and remove ADMIN_PASSWORD — see README 'Migrating the admin password'."
        )
    if not config.mapbox_token:
        app.logger.info("MAPBOX_TOKEN not set; the dashboard will use OpenStreetMap tiles.")

    # Say so at boot if the database is older than the code. Silence here is
    # what turned a missing migration into an unexplained field failure.
    if not config.testing:
        with app.app_context():
            try:
                app.config["PANGOT_SCHEMA"] = log_startup_state(app, db.engine)
            except Exception:
                app.logger.warning("Could not determine the database schema state", exc_info=True)

    _register_routes(app)
    _register_error_handlers(app)
    _register_cli(app)
    return app


# --- helpers --------------------------------------------------------------


def _fail(app, exc, message, status=500):
    """Log the real error, return one the user can quote back to you.

    Exception text from SQLAlchemy carries table names, driver versions and
    sometimes fragments of the connection string. A field worker cannot act on
    a stack trace anyway; a short reference they can read out over the radio is
    more useful to everyone.
    """
    reference = uuid.uuid4().hex[:8]
    app.logger.exception("[%s] %s", reference, message)
    return jsonify({"status": "error", "message": message, "reference": reference}), status


def _current_fixes_query():
    return CalculatedFix.query.filter(
        CalculatedFix.superseded_at.is_(None),
        CalculatedFix.deleted_at.is_(None),
    )


def _build_reading(reading_id, record):
    """Turn a validated record into a RawBearing, correcting the bearing to true north."""
    bearing_true, declination, resolved_ref = to_true_bearing(
        record["bearing"],
        record["heading_ref"],
        record["lat"],
        record["lon"],
        record["timestamp"],
    )
    return RawBearing(
        reading_id=reading_id,
        group_id=record["group_id"],
        pango_id=record["pango_id"],
        observer=record["observer"],
        device_id=record["device_id"] or None,
        obs_lat=record["lat"],
        obs_lon=record["lon"],
        gps_accuracy=record["gps_accuracy"],
        bearing=record["bearing"],
        heading_ref=resolved_ref,
        declination_deg=declination,
        bearing_true=bearing_true,
        timestamp=record["timestamp"],
    )


def _store_readings(accepted):
    """Insert readings idempotently. Returns ``(stored, duplicates)``.

    The client generates a ``reading_id`` per reading, so a sync that was
    committed but whose response was lost — the normal failure mode of patchy
    field connectivity — can be retried safely. The second upload collides on
    the unique index and is counted as a duplicate rather than inserted again
    and silently skewing the solve.
    """
    incoming = {record["reading_id"]: record for record in accepted}
    if not incoming:
        return 0, 0

    existing = {
        row.reading_id
        for row in RawBearing.query.filter(RawBearing.reading_id.in_(list(incoming))).all()
    }
    fresh = {rid: rec for rid, rec in incoming.items() if rid not in existing}

    db.session.add_all(_build_reading(rid, rec) for rid, rec in fresh.items())
    try:
        db.session.commit()
        stored = len(fresh)
    except IntegrityError:
        # Another device uploaded an overlapping batch between the SELECT above
        # and this commit. Retry one at a time so a single collision does not
        # cost the rest of the batch.
        db.session.rollback()
        stored = 0
        for reading_id, record in fresh.items():
            if RawBearing.query.filter_by(reading_id=reading_id).first():
                continue
            db.session.add(_build_reading(reading_id, record))
            try:
                db.session.commit()
                stored += 1
            except IntegrityError:
                db.session.rollback()

    return stored, len(incoming) - stored


def _solve_event(group_id, pango_id, readings):
    """Solve one round of bearings. Returns a per-event result dict."""
    started_at = event_started_at(readings)

    if len(readings) < 2:
        return {
            "event_started_at": started_at,
            "status": "waiting",
            "n_bearings": len(readings),
            "message": (
                f"{pango_id}: {len(readings)} of 2 bearings"
                " — waiting for the second observer"
            ),
        }

    try:
        fix = solve([Observation(r.obs_lat, r.obs_lon, r.bearing_true) for r in readings])
    except TriangulationError as exc:
        return {
            "event_started_at": started_at,
            "status": "failed",
            "n_bearings": len(readings),
            "message": f"{pango_id}: {exc}",
        }

    return {
        "event_started_at": started_at,
        "status": "fixed",
        "fix": fix,
        "n_bearings": len(readings),
    }


def _recompute(group_id, pango_id):
    """Re-solve one animal within one session. Returns a message for the field app.

    Bearings are split into rounds first — see events.cluster_events. Solving a
    whole session together is right only if the team started a fresh session for
    every round, and when they did not, eight bearings from four rounds were
    merged into one fix in a place the animal never was. Each round now gets its
    own fix.

    Nothing is destroyed. A fix whose round has been re-solved is superseded,
    not deleted, and a round that now fails to solve leaves its previous fix
    standing — appending one bad bearing must not cost a fix that was correct.
    """
    readings = (
        RawBearing.query.filter_by(group_id=group_id, pango_id=pango_id)
        .order_by(RawBearing.timestamp)
        .all()
    )

    if not readings:
        return {
            "group_id": group_id,
            "pango_id": pango_id,
            "status": "waiting",
            "events": [],
            "message": f"{pango_id}: no bearings yet",
        }

    events = cluster_events(readings)

    # Stamp each reading with its round, so the dashboard can draw a bearing
    # ray to the fix it actually contributed to.
    for event in events:
        started_at = event_started_at(event)
        for reading in event:
            reading.event_started_at = started_at

    current = {
        as_utc(fix.event_started_at): fix
        for fix in _current_fixes_query()
        .filter_by(group_id=group_id, pango_id=pango_id)
        .all()
    }

    results = [_solve_event(group_id, pango_id, event) for event in events]
    solved_at = utcnow()
    fixed = 0

    for result in results:
        started_at = result["event_started_at"]
        # Claim this round's existing fix whatever the outcome. A round that
        # cannot solve keeps the fix it already had: appending one bad bearing
        # must not cost a fix that was correct, and leaving it in `current`
        # would let the sweep below supersede it.
        previous = current.pop(as_utc(started_at), None)

        if result["status"] != "fixed":
            continue

        fix = result["fix"]

        # An unchanged round is left alone. Superseding and reinserting an
        # identical fix on every upload would churn its id, and the dashboard
        # reads a new id as a new fix.
        if previous is not None and _same_fix(previous, fix):
            continue

        if previous is not None:
            previous.superseded_at = solved_at

        db.session.add(
            CalculatedFix(
                group_id=group_id,
                pango_id=pango_id,
                event_started_at=started_at,
                calc_lat=fix.lat,
                calc_lon=fix.lon,
                rms_error_m=fix.rms_error_m,
                crossing_angle_deg=fix.crossing_angle_deg,
                n_bearings=fix.n_bearings,
                quality=fix.quality,
                note=fix.describe(),
                timestamp=solved_at,
            )
        )
        fixed += 1

    # Anything left in `current` belongs to a round that no longer exists —
    # only reachable if readings were removed. Its fix no longer describes
    # anything, so it is superseded rather than left to mislead.
    for orphan in current.values():
        orphan.superseded_at = solved_at

    return _recompute_summary(group_id, pango_id, results, fixed)


def _same_fix(row, fix) -> bool:
    """Would re-solving this round produce the row we already have?"""
    return (
        row.n_bearings == fix.n_bearings
        and _close(row.calc_lat, fix.lat)
        and _close(row.calc_lon, fix.lon)
    )


def _close(a, b, tolerance=1e-9) -> bool:
    if a is None or b is None:
        return a is b
    return abs(a - b) <= tolerance


def _recompute_summary(group_id, pango_id, results, fixed):
    """One message for the field app, covering every round in the session."""
    solved = [r for r in results if r["status"] == "fixed"]
    waiting = [r for r in results if r["status"] == "waiting"]
    failed = [r for r in results if r["status"] == "failed"]

    if solved:
        newest = solved[-1]["fix"]
        if len(results) == 1:
            message = f"{pango_id}: fix found ({newest.quality}) — {newest.describe()}"
        else:
            message = (
                f"{pango_id}: {len(solved)} of {len(results)} rounds solved"
                f" — latest {newest.quality}, {newest.describe()}"
            )
        return {
            "group_id": group_id,
            "pango_id": pango_id,
            "status": "fixed",
            "quality": newest.quality,
            "lat": newest.lat,
            "lon": newest.lon,
            "crossing_angle_deg": round(newest.crossing_angle_deg, 1),
            "rms_error_m": (
                round(newest.rms_error_m, 1) if newest.rms_error_m is not None else None
            ),
            "events": len(results),
            "events_solved": len(solved),
            "events_updated": fixed,
            "message": message,
        }

    detail = (failed or waiting)[-1]["message"]
    return {
        "group_id": group_id,
        "pango_id": pango_id,
        "status": "failed" if failed else "waiting",
        "events": len(results),
        "events_solved": 0,
        "kept_previous_fix": bool(
            _current_fixes_query().filter_by(group_id=group_id, pango_id=pango_id).first()
        ),
        "message": detail,
    }


# --- routes ---------------------------------------------------------------


def _register_routes(app):  # noqa: C901 - route table, flat by nature
    config = app.config["PANGOT"]

    @app.route("/")
    def home():
        return render_template("index.html")

    @app.route("/healthz")
    def healthz():
        try:
            db.session.execute(db.text("SELECT 1"))
        except Exception:
            app.logger.exception("Health check failed")
            return jsonify({"status": "degraded", "database": "unreachable"}), 503

        schema_status = check_schema(db.engine)

        # "out-of-date" is always wrong: the code expects columns the database
        # does not have. "unstamped" is only wrong in production — a local
        # database built by db.create_all() has no alembic_version and is fine.
        degraded = schema_status["state"] == "out-of-date" or (
            schema_status["state"] == "unstamped" and config.production
        )

        if degraded:
            # Reported as degraded so a deploy that skipped its migrations shows
            # up in monitoring rather than only when a field team tries to sync.
            return (
                jsonify({
                    "status": "degraded",
                    "database": "reachable",
                    "schema": schema_status,
                    "action": "Run `flask db upgrade` (stamp first if the database predates migrations).",
                }),
                503,
            )

        return jsonify({"status": "ok", "schema": schema_status})

    @app.route("/manifest.json")
    def manifest():
        return send_from_directory(".", "manifest.json", mimetype="application/manifest+json")

    @app.route("/sw.js")
    def service_worker():
        response = send_from_directory(".", "sw.js", mimetype="application/javascript")
        # Never let the browser serve a stale worker — it is what ships every
        # future update to installed devices.
        response.headers["Cache-Control"] = "no-cache"
        return response

    # --- auth ---

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            identity = authenticate(username, password)
            if identity:
                log_in(identity)
                destination = request.args.get("next", "")
                # Only ever redirect within this site.
                if not destination.startswith("/") or destination.startswith("//"):
                    destination = url_for("dashboard")
                return redirect(destination)
            app.logger.warning("Failed dashboard login for %r", username[:32])
            flash("That username and password did not match.")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        log_out()
        return redirect(url_for("home"))

    # --- field app API ---
    #
    # These are authenticated by the field token header rather than a cookie.
    # A custom header cannot be set by a cross-origin form post, so CSRF does
    # not apply to them; exempting them here is safe in a way that exempting
    # the cookie-authenticated dashboard routes never was.

    @app.route("/get_animals")
    @requires_field_token
    def get_animals():
        try:
            animals = Animal.query.filter(Animal.retired_at.is_(None)).order_by(Animal.id).all()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            if looks_like_stale_schema(exc):
                return _fail(app, exc, STALE_SCHEMA_MESSAGE, status=503)
            return _fail(app, exc, "Could not load the animal list.")

        if not animals:
            db.session.add_all(Animal(id=animal_id) for animal_id in DEFAULT_ANIMAL_IDS)
            db.session.commit()
            return jsonify(DEFAULT_ANIMAL_IDS)

        return jsonify([animal.id for animal in animals])

    @app.route("/add_animal", methods=["POST"])
    @csrf.exempt
    @requires_field_token
    def add_animal():
        try:
            animal_id = validate_animal_id((request.get_json(silent=True) or {}).get("id"))
        except ValidationError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        existing = db.session.get(Animal, animal_id)
        if existing:
            if existing.retired_at is None:
                return jsonify({"status": "exists", "message": f"{animal_id} is already on the list"}), 409
            existing.retired_at = None
            db.session.commit()
            return jsonify({"status": "added", "id": animal_id, "message": f"{animal_id} brought back"})

        try:
            db.session.add(Animal(id=animal_id))
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"status": "exists", "message": f"{animal_id} is already on the list"}), 409
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            return _fail(app, exc, "Could not add that animal. Try again.")

        return jsonify({"status": "added", "id": animal_id, "message": f"{animal_id} added"})

    @app.route("/api/animals/<animal_id>/retire", methods=["POST"])
    @requires_coordinator
    def retire_animal(animal_id):
        animal = db.session.get(Animal, animal_id)
        if not animal:
            return jsonify({"status": "error", "message": "No such animal"}), 404
        animal.retired_at = utcnow()
        db.session.commit()
        return jsonify({"status": "retired", "id": animal_id})

    @app.route("/sync", methods=["POST"])
    @csrf.exempt
    @requires_field_token
    def sync_data():
        try:
            accepted, rejected = validate_batch(request.get_json(silent=True))
        except ValidationError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        try:
            stored, duplicates = _store_readings(accepted)

            pairs = sorted({(r["group_id"], r["pango_id"]) for r in accepted})
            results = [_recompute(group_id, pango_id) for group_id, pango_id in pairs]
            db.session.commit()
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            if looks_like_stale_schema(exc):
                # A missing column is an operations problem with a known fix,
                # not a mystery. Say which one it is.
                return _fail(app, exc, STALE_SCHEMA_MESSAGE, status=503)
            return _fail(app, exc, "Sync failed. Your readings are still saved on this device.")

        return jsonify(
            {
                "status": "success",
                "stored": stored,
                "duplicates": duplicates,
                # Echoed back so the client can clear exactly what the server
                # accepted, rather than dropping the whole queue.
                "accepted_ids": [r["reading_id"] for r in accepted],
                "rejected": rejected,
                "results": results,
                "messages": [r["message"] for r in results],
            }
        )

    # --- dashboard ---

    @app.route("/dashboard")
    @requires_coordinator
    def dashboard():
        return render_template("dashboard.html", mapbox_token=config.mapbox_token)

    @app.route("/api/data")
    @requires_coordinator
    def api_data():
        try:
            limit = min(int(request.args.get("limit", API_PAGE_LIMIT)), API_MAX_LIMIT)
            offset = max(int(request.args.get("offset", 0)), 0)
        except ValueError:
            return jsonify({"status": "error", "message": "limit and offset must be integers"}), 400

        include_deleted = request.args.get("include_deleted") == "1"

        fixes_query = CalculatedFix.query.filter(CalculatedFix.superseded_at.is_(None))
        if not include_deleted:
            fixes_query = fixes_query.filter(CalculatedFix.deleted_at.is_(None))

        raw_total = RawBearing.query.count()
        fix_total = fixes_query.count()

        raw = (
            RawBearing.query.order_by(RawBearing.timestamp.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        fixes = (
            fixes_query.order_by(CalculatedFix.timestamp.desc()).limit(limit).offset(offset).all()
        )

        return jsonify(
            {
                "raw": [to_dict(r) for r in raw],
                "fixes": [to_dict(f) for f in fixes],
                "totals": {"raw": raw_total, "fixes": fix_total},
                "page": {"limit": limit, "offset": offset},
                "truncated": raw_total > offset + len(raw) or fix_total > offset + len(fixes),
            }
        )

    @app.route("/api/fix/<int:fix_id>", methods=["DELETE"])
    @requires_coordinator
    def delete_fix(fix_id):
        fix = db.session.get(CalculatedFix, fix_id)
        if not fix or fix.deleted_at is not None:
            return jsonify({"status": "error", "message": "That fix is not there any more"}), 404
        # Soft delete: this is irreplaceable field data, and the only guard in
        # front of it is one click.
        fix.deleted_at = utcnow()
        fix.deleted_by = current_coordinator()
        db.session.commit()
        app.logger.info("Fix %s deleted by %s", fix_id, current_coordinator())
        return jsonify({"status": "deleted", "id": fix_id})

    @app.route("/api/fix/<int:fix_id>/restore", methods=["POST"])
    @requires_coordinator
    def restore_fix(fix_id):
        fix = db.session.get(CalculatedFix, fix_id)
        if not fix:
            return jsonify({"status": "error", "message": "No such fix"}), 404
        fix.deleted_at = None
        fix.deleted_by = None
        db.session.commit()
        app.logger.info("Fix %s restored by %s", fix_id, current_coordinator())
        return jsonify({"status": "restored", "id": fix_id})

    @app.route("/api/fix/<int:fix_id>", methods=["POST"])
    @requires_coordinator
    def update_fix(fix_id):
        fix = db.session.get(CalculatedFix, fix_id)
        if not fix or fix.deleted_at is not None:
            return jsonify({"status": "error", "message": "That fix is not there any more"}), 404

        payload = request.get_json(silent=True) or {}
        try:
            if "pango_id" in payload:
                fix.pango_id = validate_animal_id(payload["pango_id"])
            if "note" in payload:
                note = str(payload["note"] or "").strip()
                if len(note) > 255:
                    return jsonify({"status": "error", "message": "Note must be 255 characters or fewer"}), 400
                fix.note = note
        except ValidationError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400

        fix.updated_by = current_coordinator()
        fix.updated_at = utcnow()
        db.session.commit()
        return jsonify({"status": "updated", "id": fix_id})

    # --- account management ---

    @app.route("/users")
    @requires_admin
    def users_page():
        return render_template("users.html", users=User.query.order_by(User.username).all())

    @app.route("/api/users", methods=["POST"])
    @requires_admin
    def create_user_route():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        role = str(payload.get("role", "coordinator"))

        if not username or len(username) > 64:
            return jsonify({"status": "error", "message": "Username is required, 64 characters or fewer"}), 400
        if len(password) < 12:
            # Coordinators hold every pangolin location the project has.
            return jsonify({"status": "error", "message": "Password must be at least 12 characters"}), 400
        if role not in ("admin", "coordinator"):
            return jsonify({"status": "error", "message": "Role must be admin or coordinator"}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({"status": "error", "message": f"{username} already has an account"}), 409

        create_user(username, password, role)
        app.logger.info("Account %r created by %s", username, current_coordinator())
        return jsonify({"status": "created", "username": username})

    @app.route("/api/users/<int:user_id>/disable", methods=["POST"])
    @requires_admin
    def disable_user_route(user_id):
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "No such account"}), 404
        if user.username == current_coordinator():
            return jsonify({"status": "error", "message": "You cannot disable your own account"}), 400
        if user.is_admin and User.query.filter_by(role="admin", disabled_at=None).count() <= 1:
            return jsonify({"status": "error", "message": "That is the last admin account"}), 400

        user.disabled_at = utcnow()
        db.session.commit()
        app.logger.info("Account %r disabled by %s", user.username, current_coordinator())
        return jsonify({"status": "disabled", "username": user.username})

    @app.route("/api/users/<int:user_id>/enable", methods=["POST"])
    @requires_admin
    def enable_user_route(user_id):
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "No such account"}), 404
        user.disabled_at = None
        db.session.commit()
        return jsonify({"status": "enabled", "username": user.username})

    # --- live updates ---
    #
    # Replaces the dashboard's 30-second poll. The server still checks the
    # database on a short interval, but it does so once for all viewers and
    # pushes only when something actually changed, so a coordinator sees a new
    # fix within a couple of seconds instead of up to thirty.
    #
    # Each stream holds a worker thread for its lifetime, hence the cap and the
    # gthread worker class in the Procfile. The client falls back to polling if
    # this is unavailable, so losing it degrades rather than breaks.

    @app.route("/api/stream")
    @requires_coordinator
    def stream():
        if not _streams.acquire():
            return (
                jsonify({"status": "error", "message": "Too many live connections", "fallback": "poll"}),
                503,
            )

        @stream_with_context
        def events():
            try:
                last = None
                deadline = time.monotonic() + STREAM_MAX_SECONDS
                while time.monotonic() < deadline:
                    # Drop the session each pass so the next read sees committed
                    # work from other workers rather than a cached snapshot.
                    db.session.remove()
                    current = _data_fingerprint()
                    if current != last:
                        last = current
                        yield f"event: changed\ndata: {json.dumps(current)}\n\n"
                    else:
                        yield ": keepalive\n\n"
                    time.sleep(STREAM_POLL_SECONDS)
                yield "event: reconnect\ndata: {}\n\n"
            finally:
                _streams.release()

        return Response(
            events(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Stops nginx buffering the stream into uselessness.
                "X-Accel-Buffering": "no",
            },
        )

    # --- exports ---

    @app.route("/download_csv")
    @requires_coordinator
    def download_csv():
        rows = RawBearing.query.order_by(RawBearing.timestamp).all()
        header = [
            "reading_id", "group_id", "pango_id", "event_started_at", "observer",
            "device_id", "obs_lat", "obs_lon", "gps_accuracy", "bearing",
            "heading_ref", "declination_deg", "bearing_true", "timestamp",
        ]
        return _csv_response(rows, header, "pangolin_raw_bearings.csv")

    @app.route("/download_fixes")
    @requires_coordinator
    def download_fixes():
        rows = _current_fixes_query().order_by(CalculatedFix.timestamp).all()
        # event_started_at identifies the round. Without it a session with
        # several rounds on one animal exports as rows that look like
        # duplicates, and the bearings CSV cannot be joined back to its fix.
        header = [
            "group_id", "pango_id", "event_started_at", "calc_lat", "calc_lon",
            "n_bearings", "crossing_angle_deg", "rms_error_m", "quality", "note",
            "timestamp",
        ]
        return _csv_response(rows, header, "pangolin_fixes.csv")


def _csv_response(rows, header, filename):
    import csv
    import io

    from models import as_utc

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        record = []
        for field in header:
            value = getattr(row, field, None)
            if isinstance(value, datetime):
                value = as_utc(value).isoformat()
            record.append(value)
        writer.writerow(record)

    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_day(value, end_of_day=False):
    """A YYYY-MM-DD date as an aware UTC datetime."""
    if not value:
        return None
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a date in YYYY-MM-DD form") from exc
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.replace(tzinfo=timezone.utc)


def run_refix(since=None, until=None, dry_run=False):
    """Re-solve sessions into rounds. Returns a report; raises nothing routine.

    `since` and `until` select which sessions to touch, by whether they hold a
    bearing in that window. Each selected session is then re-solved from *all*
    of its bearings, not just those inside the window: clustering has to see the
    complete history or a round straddling the boundary would be split in two.
    """
    selector = db.session.query(RawBearing.group_id, RawBearing.pango_id).distinct()
    if since is not None:
        selector = selector.filter(RawBearing.timestamp >= since)
    if until is not None:
        selector = selector.filter(RawBearing.timestamp <= until)

    pairs = selector.order_by(RawBearing.group_id, RawBearing.pango_id).all()

    window = ""
    if since or until:
        window = (
            f" with a bearing between {since.date() if since else 'the start'}"
            f" and {until.date() if until else 'now'}"
        )

    if not pairs:
        return {
            "pairs": 0, "split": 0, "before": 0, "after": 0, "dry_run": dry_run,
            "lines": [f"No sessions found{window} — nothing to do."],
        }

    before = _current_fixes_query().count()
    lines = []
    split = 0

    for group_id, pango_id in pairs:
        readings = (
            RawBearing.query.filter_by(group_id=group_id, pango_id=pango_id)
            .order_by(RawBearing.timestamp)
            .all()
        )
        rounds = cluster_events(readings)
        if len(rounds) > 1:
            split += 1
            lines.append(
                f"  {group_id} / {pango_id}: {len(readings)} bearings -> {len(rounds)} rounds"
            )
        if not dry_run:
            _recompute(group_id, pango_id)

    if dry_run:
        db.session.rollback()
        lines.append(
            f"\nDry run — nothing changed. {split} of {len(pairs)} session/animal"
            f" pairs{window} would be split into rounds."
        )
        return {
            "pairs": len(pairs), "split": split, "before": before, "after": before,
            "dry_run": True, "lines": lines,
        }

    db.session.commit()
    after = _current_fixes_query().count()
    lines.append(
        f"\nRe-solved {len(pairs)} session/animal pairs{window};"
        f" {split} were split into rounds."
        f"\nCurrent fixes: {before} -> {after}."
    )
    return {
        "pairs": len(pairs), "split": split, "before": before, "after": after,
        "dry_run": False, "lines": lines,
    }


def run_boot_refix(app, db_, logger_):
    """Run `refix` once at boot, when REFIX_ON_BOOT asks for it.

    Render's free tier has no shell and no pre-deploy hook, so this is the only
    way to run a one-off correction there. It is deliberately opt-in by
    environment variable, reports what it did to the log, and never stops the
    app from starting — a correction that fails is not a reason to serve
    nothing.

        REFIX_ON_BOOT=dry-run   report only
        REFIX_ON_BOOT=1         apply
        REFIX_SINCE=2026-08-13  optional window, by bearing date
        REFIX_UNTIL=2026-08-27

    Re-running it is harmless: a round whose fix is already correct is left
    alone. Remove the variable once the log shows what you wanted.
    """
    setting = (app.config["PANGOT"].refix_on_boot or "").strip().lower()
    if setting in ("", "0", "false", "no"):
        return None

    dry_run = setting in ("dry-run", "dry_run", "dryrun", "report")
    env = app.config["PANGOT"].env

    try:
        report = run_refix(
            since=_parse_day(env.get("REFIX_SINCE")),
            until=_parse_day(env.get("REFIX_UNTIL"), end_of_day=True),
            dry_run=dry_run,
        )
    except Exception:
        db_.session.rollback()
        logger_.exception("REFIX_ON_BOOT failed; starting anyway with data unchanged.")
        return None

    logger_.info("REFIX_ON_BOOT (%s):", "dry run" if dry_run else "applying")
    for line in report["lines"]:
        for part in line.splitlines():
            if part.strip():
                logger_.info("  %s", part)
    if not dry_run:
        logger_.info("Remove REFIX_ON_BOOT from the environment now that it has run.")
    return report


def _register_cli(app):
    """`flask users ...` — account management without needing the web UI."""
    import click

    @app.cli.command("refix")
    @click.option("--dry-run", is_flag=True, help="Report what would change, change nothing.")
    @click.option("--since", help="Only sessions with a bearing on or after this date (YYYY-MM-DD).")
    @click.option("--until", help="Only sessions with a bearing on or before this date (YYYY-MM-DD).")
    def refix_command(dry_run, since, until):
        """Re-solve sessions, splitting their bearings into rounds.

        Sessions recorded before rounds existed hold at most one fix per animal,
        built from every bearing for it. Where the team took several rounds
        without starting a new session, the later ones were never calculated.
        The bearings are all still there, so re-solving recovers them.

        Superseded fixes are left in place, as always. Nothing is deleted, and
        running it twice changes nothing the second time.
        """
        try:
            report = run_refix(
                since=_parse_day(since),
                until=_parse_day(until, end_of_day=True),
                dry_run=dry_run,
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc

        for line in report["lines"]:
            click.echo(line)

    @app.cli.command("deploy")
    def deploy_command():
        """Bring the database up to date, whatever state it is in.

        Safe to run on every deploy. Replaces the manual `flask db stamp ...`
        then `flask db upgrade` dance, which is easy to forget — and forgetting
        it is what put a live field team on an un-migrated database, where
        uploads failed with an opaque reference number.
        """
        from flask_migrate import stamp, upgrade

        plan = plan_deploy(db.engine)

        if plan["case"] == "adopt":
            click.echo(
                "Database has tables but no alembic_version — adopting it at "
                f"baseline {plan['stamp']} before upgrading."
            )
            stamp(revision=plan["stamp"])
        elif plan["case"] == "fresh":
            click.echo("Empty database — creating the schema from the migrations.")
        else:
            click.echo(f"Database is at {plan['current']}.")

        upgrade()

        status = check_schema(db.engine)
        if status["state"] == "ok":
            click.echo(f"Schema is up to date at {status['current']}.")
        else:
            raise click.ClickException(
                f"Schema is still {status['state']} after upgrading "
                f"(at {status['current']}, expected {status['expected']})."
            )

    @app.cli.group()
    def users():
        """Manage coordinator accounts."""

    @users.command("create")
    @click.argument("username")
    @click.option("--role", type=click.Choice(["admin", "coordinator"]), default="coordinator")
    @click.password_option(help="Minimum 12 characters.")
    def users_create(username, role, password):
        if len(password) < 12:
            raise click.ClickException("Password must be at least 12 characters")
        if User.query.filter_by(username=username).first():
            raise click.ClickException(f"{username} already has an account")
        create_user(username, password, role)
        click.echo(f"Created {role} account {username!r}.")

    @users.command("list")
    def users_list():
        rows = User.query.order_by(User.username).all()
        if not rows:
            click.echo("No accounts yet. The ADMIN_USERNAME fallback is still active — "
                       "create one with `flask users create <name> --role admin` to disable it.")
            return
        for user in rows:
            state = "disabled" if user.disabled_at else "active"
            last = user.last_login_at.strftime("%Y-%m-%d") if user.last_login_at else "never"
            click.echo(f"{user.username:<24} {user.role:<12} {state:<9} last login {last}")

    @users.command("passwd")
    @click.argument("username")
    @click.password_option(help="Minimum 12 characters.")
    def users_passwd(username, password):
        if len(password) < 12:
            raise click.ClickException("Password must be at least 12 characters")
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"No account named {username!r}")
        set_password(user, password)
        click.echo(f"Password updated for {username!r}.")

    @users.command("disable")
    @click.argument("username")
    def users_disable(username):
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"No account named {username!r}")
        if user.is_admin and User.query.filter_by(role="admin", disabled_at=None).count() <= 1:
            raise click.ClickException("That is the last active admin account")
        user.disabled_at = utcnow()
        db.session.commit()
        click.echo(f"Disabled {username!r}.")

    @users.command("enable")
    @click.argument("username")
    def users_enable(username):
        user = User.query.filter_by(username=username).first()
        if not user:
            raise click.ClickException(f"No account named {username!r}")
        user.disabled_at = None
        db.session.commit()
        click.echo(f"Enabled {username!r}.")


def _register_error_handlers(app):
    def wants_json():
        return request.path.startswith(("/api/", "/sync", "/add_animal", "/get_animals"))

    @app.errorhandler(400)
    def bad_request(error):
        if wants_json():
            return jsonify({"status": "error", "message": "That request could not be read"}), 400
        return error

    @app.errorhandler(404)
    def not_found(error):
        if wants_json():
            return jsonify({"status": "error", "message": "Not found"}), 404
        return error

    @app.errorhandler(500)
    def server_error(error):
        reference = uuid.uuid4().hex[:8]
        app.logger.exception("[%s] Unhandled error", reference)
        if wants_json():
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Something went wrong on the server.",
                        "reference": reference,
                    }
                ),
                500,
            )
        return error


_app = None


def __getattr__(name):
    """Build the app on first access to ``app``.

    ``gunicorn app:app`` and ``flask run`` both resolve the name by attribute
    lookup, so they keep working unchanged. Building it lazily means importing
    ``create_app`` — from tests, or from a migration — does not require the full
    production configuration to be present.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    application = create_app()
    with application.app_context():
        db.create_all()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
