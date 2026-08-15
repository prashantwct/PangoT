"""Coordinate transforms and magnetic-to-true bearing correction.

Two problems the previous implementation had, both silent:

*Wrong zone.* It hardcoded EPSG:32644 (UTM zone 44N, valid 78-84 degrees E).
Any site outside that band was solved in the wrong projection.

*Grid north is not true north.* A compass bearing is referenced to true (or
magnetic) north, but a projected coordinate system has its own grid north,
and the two diverge by the meridian convergence — up to about 3 degrees at a
UTM zone edge, which is roughly 50 m of error at a 1 km baseline. Because the
error is systematic rather than random, averaging more bearings does not
remove it.

Both are fixed here by projecting into an azimuthal equidistant frame centred
on the observers, and by deriving each bearing's grid direction from a real
geodesic step rather than from ``sin``/``cos`` of the raw angle. That makes
the convergence correction exact and automatic for any projection.
"""
import math
from functools import lru_cache

from pyproj import CRS, Geod, Transformer

WGS84 = Geod(ellps="WGS84")

# Distance stepped along a geodesic when deriving a bearing's grid direction.
# Long enough that floating-point noise in the projection is negligible, short
# enough that the geodesic and its projected image stay collinear.
_DIRECTION_STEP_M = 1000.0


@lru_cache(maxsize=256)
def _transformers(lat0: float, lon0: float):
    """Forward and inverse transformers for a local azimuthal equidistant frame.

    Cached on the rounded centre so repeated solves near one field site reuse
    the same PROJ objects — building them is the expensive part.
    """
    crs = CRS.from_proj4(
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +datum=WGS84 +units=m +no_defs"
    )
    return (
        Transformer.from_crs("EPSG:4326", crs, always_xy=True),
        Transformer.from_crs(crs, "EPSG:4326", always_xy=True),
    )


def local_frame(lat0: float, lon0: float):
    """A projection centred on the field site, as (to_xy, to_latlon) callables.

    Rounded to 3 decimal places (~100 m) so nearby sessions share a cached
    frame. The centring only needs to be approximate — it exists to keep
    distortion small, not to define the answer.
    """
    fwd, inv = _transformers(round(lat0, 3), round(lon0, 3))

    def to_xy(lat, lon):
        x, y = fwd.transform(lon, lat)
        return x, y

    def to_latlon(x, y):
        lon, lat = inv.transform(x, y)
        return lat, lon

    return to_xy, to_latlon


def grid_direction(lat: float, lon: float, bearing_true: float, to_xy) -> tuple:
    """Unit vector, in projected coordinates, of a true-north-referenced bearing.

    Derived by walking a real geodesic from the observer along the bearing and
    projecting both endpoints. This absorbs meridian convergence and projection
    distortion without needing a convergence formula, and stays correct if the
    projection is ever changed.
    """
    lon2, lat2, _ = WGS84.fwd(lon, lat, bearing_true, _DIRECTION_STEP_M)
    x1, y1 = to_xy(lat, lon)
    x2, y2 = to_xy(lat2, lon2)
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    if norm == 0:
        raise ValueError("Degenerate bearing direction")
    return dx / norm, dy / norm


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Geodesic distance in metres."""
    _, _, dist = WGS84.inv(lon1, lat1, lon2, lat2)
    return dist


def bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial true bearing from point 1 to point 2, in degrees clockwise from north."""
    az, _, _ = WGS84.inv(lon1, lat1, lon2, lat2)
    return az % 360.0


# --- Magnetic declination -------------------------------------------------
#
# Device compasses do not agree on their reference. iOS reports
# ``webkitCompassHeading`` relative to TRUE north when location services are
# available; Android's ``deviceorientationabsolute`` reports relative to
# MAGNETIC north. Feeding both into the same solver without correction makes
# two phones standing side by side disagree by the local declination, and the
# solver reads that disagreement as observer error.

_WMM_MIN_YEAR = 2025.0
_WMM_MAX_YEAR = 2029.99

try:  # pragma: no cover - exercised implicitly, guarded for offline installs
    from pygeomag import GeoMag

    _GEOMAG = GeoMag()
except Exception:  # pragma: no cover
    _GEOMAG = None


def _decimal_year(when) -> float:
    start = when.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1)
    return start.year + (when - start).total_seconds() / (end - start).total_seconds()


@lru_cache(maxsize=4096)
def _declination_cached(lat_q: float, lon_q: float, year_q: float):
    if _GEOMAG is None:
        return None
    try:
        result = _GEOMAG.calculate(glat=lat_q, glon=lon_q, alt=0, time=year_q)
        return float(result.d)
    except Exception:
        return None


def declination_deg(lat: float, lon: float, when) -> float | None:
    """Magnetic declination in degrees, east positive. None if unavailable.

    Quantised to a 0.5 degree grid and a quarter year before lookup — the field
    varies slowly enough that this is well inside the model's own uncertainty,
    and it keeps the cache small.
    """
    year = _decimal_year(when)
    # The bundled model is WMM-2025 (valid 2025-2030). Clamp rather than fail:
    # a slightly stale declination is far better than no correction at all, and
    # the annual drift at these latitudes is a small fraction of a degree.
    year = min(max(year, _WMM_MIN_YEAR), _WMM_MAX_YEAR)
    return _declination_cached(round(lat * 2) / 2, round(lon * 2) / 2, round(year * 4) / 4)


def to_true_bearing(bearing: float, heading_ref: str, lat: float, lon: float, when):
    """Convert a device bearing to true north.

    Returns ``(bearing_true, declination_applied, resolved_ref)``.

    An unknown reference is treated as magnetic, which is the safer assumption:
    most Android devices report magnetic, and applying a correction of a degree
    or so where none was needed is a much smaller error than omitting one where
    it was.
    """
    ref = (heading_ref or "unknown").lower()
    if ref == "true":
        return bearing % 360.0, 0.0, "true"

    declination = declination_deg(lat, lon, when)
    if declination is None:
        return bearing % 360.0, 0.0, ref
    return (bearing + declination) % 360.0, declination, ref
