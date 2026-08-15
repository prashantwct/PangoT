"""Request payload validation.

The previous ``/sync`` trusted everything: a bearing of 999 or a latitude of
500 went straight into the database, and a single missing key raised a
KeyError that the blanket handler turned into a 500 — discarding the whole
batch because one record was malformed. After a day in the field that is not
an inconvenience, it is lost work.

So: validate per record, accept the good ones, and return the rejected ones
with a reason the field app can actually show someone.
"""
import uuid
from datetime import datetime, timezone

# Namespace for deriving a stable reading_id from a record's own content.
# Fixed forever — changing it would break idempotency for older clients.
_READING_NAMESPACE = uuid.UUID("6f3b1c2e-5a4d-4b9f-8c7a-1d2e3f4a5b6c")

MAX_BATCH = 2000

HEADING_REFS = ("true", "magnetic", "unknown")


class ValidationError(ValueError):
    """A record could not be accepted. The message is shown to the user."""


def _as_float(value, field):
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"{field} is required")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a number, got {value!r}")
    if result != result or result in (float("inf"), float("-inf")):
        raise ValidationError(f"{field} must be a real number")
    return result


def _in_range(value, low, high, field, unit=""):
    if not low <= value <= high:
        raise ValidationError(f"{field} must be between {low} and {high}{unit}, got {value:g}")
    return value


def _as_str(value, field, max_length, required=True, default=""):
    if value is None or value == "":
        if required:
            raise ValidationError(f"{field} is required")
        return default
    text = str(value).strip()
    if required and not text:
        raise ValidationError(f"{field} is required")
    if len(text) > max_length:
        raise ValidationError(f"{field} must be {max_length} characters or fewer, got {len(text)}")
    return text


def _as_timestamp(value):
    if not value:
        raise ValidationError("time is required")
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(f"time must be an ISO 8601 timestamp, got {value!r}")
    # A naive timestamp from a field device is UTC by convention — the client
    # sends toISOString(). Assuming local time here would shift every reading.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def derive_reading_id(record) -> str:
    """A stable ID for a record that arrived without one.

    Field phones cache the app aggressively, so older clients keep uploading
    for a while after a release. Deriving the ID from the record's own content
    means those uploads are still idempotent — a retry produces the same ID and
    collides with the row already stored, instead of duplicating it.
    """
    canonical = "|".join(
        str(record.get(key, ""))
        for key in ("group_id", "pango_id", "observer", "lat", "lon", "bearing", "time")
    )
    return str(uuid.uuid5(_READING_NAMESPACE, canonical))


def validate_reading(record) -> dict:
    """Validate one uploaded bearing. Raises ValidationError with a usable message."""
    if not isinstance(record, dict):
        raise ValidationError("Each reading must be an object")

    lat = _in_range(_as_float(record.get("lat"), "lat"), -90, 90, "lat", "°")
    lon = _in_range(_as_float(record.get("lon"), "lon"), -180, 180, "lon", "°")
    bearing = _in_range(_as_float(record.get("bearing"), "bearing"), 0, 360, "bearing", "°")

    accuracy = record.get("accuracy")
    if accuracy in (None, ""):
        accuracy = None
    else:
        accuracy = _in_range(_as_float(accuracy, "accuracy"), 0, 100_000, "accuracy", " m")

    heading_ref = _as_str(record.get("heading_ref"), "heading_ref", 10, required=False, default="unknown").lower()
    if heading_ref not in HEADING_REFS:
        raise ValidationError(f"heading_ref must be one of {', '.join(HEADING_REFS)}")

    reading_id = _as_str(record.get("reading_id"), "reading_id", 36, required=False)
    if not reading_id:
        reading_id = derive_reading_id(record)

    return {
        "reading_id": reading_id,
        "group_id": _as_str(record.get("group_id"), "group_id", 80),
        "pango_id": _as_str(record.get("pango_id"), "pango_id", 16),
        "observer": _as_str(record.get("observer"), "observer", 16, required=False, default="--"),
        "device_id": _as_str(record.get("device_id"), "device_id", 64, required=False),
        "lat": lat,
        "lon": lon,
        "bearing": bearing % 360.0,
        "heading_ref": heading_ref,
        "gps_accuracy": accuracy,
        "timestamp": _as_timestamp(record.get("time")),
    }


def validate_batch(payload):
    """Validate an upload. Returns ``(accepted, rejected)``.

    Never raises for a bad record — one typo must not cost a whole day's batch.
    """
    if not isinstance(payload, list):
        raise ValidationError("Expected a list of readings")
    if not payload:
        raise ValidationError("No readings in upload")
    if len(payload) > MAX_BATCH:
        raise ValidationError(f"Too many readings in one upload (limit {MAX_BATCH})")

    accepted, rejected = [], []
    for index, record in enumerate(payload):
        try:
            accepted.append(validate_reading(record))
        except ValidationError as exc:
            rejected.append(
                {
                    "index": index,
                    "reading_id": (record or {}).get("reading_id") if isinstance(record, dict) else None,
                    "error": str(exc),
                }
            )
    return accepted, rejected


def validate_animal_id(value) -> str:
    animal_id = _as_str(value, "id", 16)
    if not all(char.isalnum() or char in "-_" for char in animal_id):
        raise ValidationError("Animal ID may only contain letters, numbers, hyphens and underscores")
    return animal_id
