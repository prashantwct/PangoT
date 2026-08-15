# PangoT

Radio-telemetry triangulation for pangolin fieldwork.

Two observers take compass bearings on a collared animal from different
positions. PangoT records those bearings offline on a phone, uploads them when
there is signal, crosses them to calculate the animal's position, and shows the
result on a map.

- `/` — the **field app**. A PWA that installs to a phone's home screen and
  works with no network. This is where bearings are recorded.
- `/dashboard` — **mission control**. Map, filters, fix management, CSV export.
  Needs a coordinator sign-in.

---

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

cp .env.example .env      # then fill it in — see below
.venv/bin/flask db upgrade
.venv/bin/python app.py
```

The app is then on <http://localhost:5000>.

### Filling in `.env`

Every value is required in production; the app refuses to start without them
rather than falling back to a guessable default.

```bash
# A signing key for session cookies and CSRF tokens
python -c "import secrets; print(secrets.token_urlsafe(48))"

# The coordinator password, stored as a hash
python -c "from werkzeug.security import generate_password_hash as h; print(h(input('password: ')))"

# The shared token field phones use to upload
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

### Running the tests

```bash
.venv/bin/python -m pytest
```

---

## How a tracking session works

Triangulation needs at least two bearings on the same animal, taken from
positions well apart, and they must be recorded against the **same session**.

1. One observer opens the field app and taps the session button (`⇄`) →
   **Start a new session**. A six-character code appears in the header, e.g.
   `K7M2Q4`.
2. They read the code out. The second observer taps `⇄` → **Join** and types it.
3. Both now pick the same animal, get a GPS position, take a bearing, and save.
4. Either observer taps **Upload readings** when they have signal. The fix is
   calculated server-side as soon as two bearings for that animal and session
   have arrived.

Both phones show the calculated position afterwards, with a bearing and
distance to walk from wherever the holder is standing — that part works
offline once the fix is known.

### What makes a good fix

| | |
|---|---|
| **Separate well** | Observers must be at least 25 m apart; a few hundred metres is much better. The app refuses a solve from a single spot. |
| **Cross near 90°** | Bearings crossing at a shallow angle give an enormously elongated uncertainty region even when they fit perfectly. Below 20° the fix is graded `poor`; below 10° it is refused. |
| **Wait for GPS** | The app warns above ±25 m accuracy (configurable under Sync → Settings). At ±500 m the calculated position can be out by more than the animal's whole home range. |
| **Three beats two** | With exactly two bearings the system is exactly determined, so there is no residual to report and no way to detect a bad bearing. A third gives you both. |

---

## Deployment

`gunicorn app:app` — unchanged from before.

The app is built with a factory (`create_app`), and `app` is resolved lazily on
attribute access, so `gunicorn app:app`, `gunicorn "app:create_app()"` and
`flask run` all work.

### Required environment variables

| Variable | Notes |
|---|---|
| `SECRET_KEY` | Signs sessions and CSRF tokens. |
| `ADMIN_USERNAME` | Coordinator sign-in. |
| `ADMIN_PASSWORD_HASH` | A Werkzeug hash, not a plaintext password. |
| `FIELD_TOKEN` | Shared secret field phones send with uploads. |
| `DATABASE_URL` | Postgres in production. Defaults to local SQLite. |
| `MAPBOX_TOKEN` | Optional. Without it the dashboard uses OpenStreetMap. |
| `FLASK_ENV` | `development` relaxes the checks above. Anything else is production. |

### Migrating the admin password

`ADMIN_PASSWORD` (plaintext) is still accepted so an existing deployment does
not go dark mid-season, but it logs a warning on every start. To migrate:

```bash
python -c "from werkzeug.security import generate_password_hash as h; print(h(input('password: ')))"
```

Set the result as `ADMIN_PASSWORD_HASH` and delete `ADMIN_PASSWORD`.

### Upgrading a database created before migrations existed

If the database was created by `db.create_all()` it has no `alembic_version`
table. Stamp it at the initial revision once, then upgrade:

```bash
flask db stamp 273e298aa774
flask db upgrade
```

---

## How the maths works

`triangulation.py` is pure and has no Flask or database dependency, so it can be
read and tested on its own (`tests/test_triangulation.py`).

**Projection.** Observations are projected into an azimuthal equidistant frame
centred on the observers, chosen from the data rather than hardcoded.

**Bearing direction.** Each bearing's direction in that frame is derived by
walking a real geodesic from the observer along the bearing and projecting both
endpoints. This absorbs meridian convergence — the difference between true north
and the projection's grid north — exactly, and stays correct if the projection is
ever changed. Treating a true bearing as a grid bearing is worth up to about 3°,
or roughly 50 m at a 1 km baseline, and the error is systematic, so averaging
more bearings does not remove it.

**Declination.** Device compasses disagree about their reference: iOS
`webkitCompassHeading` is true north, Android's absolute `alpha` is magnetic.
Each reading stores which frame it came from, the declination applied (World
Magnetic Model, via `pygeomag`), and both the raw and corrected bearing — so a
model change can be re-applied later without losing the original observation.

**Solve.** Least squares over the bearing lines. Because each direction is a
unit vector, each row's residual is exactly the perpendicular distance from the
solution to that bearing line, in metres.

**Quality.** With more than two bearings, RMS residual. With exactly two, no
residual is reported at all — the system is square, so it is always zero, and
zero reads as "perfect". Crossing angle is reported instead. The solver also
flags bearings whose fix lies *behind* the observer, which is what a 180° error
looks like and is otherwise undetectable.

---

## Project layout

```
app.py             Factory, routes, sync orchestration
config.py          Environment config, validated at startup
models.py          SQLAlchemy models
triangulation.py   The solve — pure, no framework
geodesy.py         Projection, grid convergence, magnetic declination
validation.py      Request payload validation
auth.py            Coordinator sessions and field-device tokens
static/app.js      Field app
static/dashboard.js  Mission control
sw.js              Service worker (offline)
tools/make_icons.py  Regenerates the app icons
tests/             pytest
```

### Data model notes

Two things are deliberate:

- Every raw bearing carries a client-generated `reading_id` with a unique
  index. A phone can retry a failed upload as often as it likes without
  duplicating readings and skewing the solve.
- Fixes are never destroyed. Recalculation stamps the old row `superseded_at`;
  coordinator deletion stamps `deleted_at` and is undoable. The current fix for
  an animal is the one with neither set.

---

## Things to know

- **`.env` and the database must never be committed.** Both were, historically;
  if you are recovering that, rotate every credential and purge them from git
  history — `.gitignore` does not apply to files already tracked.
- **Map tiles are cached as they are viewed.** Panning over your field site
  while you still have signal makes that ground available offline later.
- **Night mode** (the `☾` button) is red-on-black, for tracking after dark
  without destroying anyone's dark adaptation.
- **Leaflet is vendored** in `static/vendor/leaflet/`, not loaded from a CDN, so
  the dashboard still works on a restricted network.
