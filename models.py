"""Database models.

Two deliberate choices worth knowing about:

1. Every raw bearing carries a client-generated ``reading_id``. That is what
   makes upload idempotent — a phone can retry a failed sync as often as it
   likes without duplicating readings and skewing the solve.

2. Fixes are never destroyed by recalculation. A superseded fix gets a
   ``superseded_at`` stamp and a deleted one gets ``deleted_at``, so a bad
   solve is always recoverable and the history is auditable.
"""
from datetime import datetime, timezone

from extensions import db


def utcnow() -> datetime:
    """Timezone-aware UTC now. ``datetime.utcnow()`` is deprecated and naive."""
    return datetime.now(timezone.utc)


def as_utc(value):
    """Normalise a datetime read back from the database to aware UTC.

    SQLite discards timezone information, so values written as aware come back
    naive. Postgres with ``timezone=True`` round-trips correctly. This makes
    both behave the same for serialisation.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class RawBearing(db.Model):
    __tablename__ = "raw_bearings"

    id = db.Column(db.Integer, primary_key=True)

    # Client-generated idempotency key. Unique so a retried sync is a no-op.
    reading_id = db.Column(db.String(36), unique=True, index=True, nullable=False)

    group_id = db.Column(db.String(80), index=True, nullable=False)
    pango_id = db.Column(db.String(16), index=True, nullable=False)
    observer = db.Column(db.String(16))
    device_id = db.Column(db.String(64))

    obs_lat = db.Column(db.Float, nullable=False)
    obs_lon = db.Column(db.Float, nullable=False)
    gps_accuracy = db.Column(db.Float)

    # ``bearing`` is what the observer's device reported. ``bearing_true`` is
    # that value corrected to true north, and is the only one the solver uses.
    # Keeping both means a declination model change can be re-applied later.
    bearing = db.Column(db.Float, nullable=False)
    heading_ref = db.Column(db.String(10), default="unknown")
    declination_deg = db.Column(db.Float, default=0.0)
    bearing_true = db.Column(db.Float, nullable=False)

    timestamp = db.Column(db.DateTime(timezone=True), index=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        db.Index("ix_raw_bearings_group_pango", "group_id", "pango_id"),
    )


class CalculatedFix(db.Model):
    __tablename__ = "calculated_fixes"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.String(80), index=True, nullable=False)
    pango_id = db.Column(db.String(16), index=True, nullable=False)

    calc_lat = db.Column(db.Float, nullable=False)
    calc_lon = db.Column(db.Float, nullable=False)

    # None for a two-bearing fix: the system is exactly determined, so the
    # residual is always zero and reporting it would imply a precision that
    # does not exist. Use crossing_angle_deg to judge those instead.
    rms_error_m = db.Column(db.Float)
    crossing_angle_deg = db.Column(db.Float)
    n_bearings = db.Column(db.Integer, default=0)
    quality = db.Column(db.String(8), default="unknown")  # good | fair | poor
    note = db.Column(db.String(255))

    timestamp = db.Column(db.DateTime(timezone=True), default=utcnow, index=True)

    # Soft lifecycle. Superseded = replaced by a newer solve for the same
    # group and animal. Deleted = removed by a coordinator, recoverable.
    superseded_at = db.Column(db.DateTime(timezone=True))
    deleted_at = db.Column(db.DateTime(timezone=True))

    @property
    def is_current(self) -> bool:
        return self.superseded_at is None and self.deleted_at is None


class Animal(db.Model):
    __tablename__ = "animals"

    id = db.Column(db.String(16), primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    # Animals are retired, not deleted — their historical fixes still refer to them.
    retired_at = db.Column(db.DateTime(timezone=True))

    @property
    def is_active(self) -> bool:
        return self.retired_at is None


def to_dict(model) -> dict:
    """Serialise a model to JSON-safe primitives."""
    import math

    data = {}
    for column in model.__table__.columns:
        value = getattr(model, column.name)
        if isinstance(value, datetime):
            data[column.name] = as_utc(value).isoformat()
        elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            # NaN and Infinity are not valid JSON; emitting them produces a
            # payload the browser's JSON.parse rejects outright.
            data[column.name] = None
        else:
            data[column.name] = value
    return data
