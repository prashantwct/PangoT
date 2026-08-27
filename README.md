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

### Creating a coordinator account

While no accounts exist, the app accepts `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH`
from the environment so a fresh deployment is reachable. Creating the first
account switches that fallback off.

```bash
flask users create kavya --role admin
flask users list
flask users passwd kavya
flask users disable kavya
```

Admins can also manage accounts at `/users`. Coordinators can view and manage
fixes; admins can additionally manage accounts. Deletions and edits are recorded
against the account that made them.

### Running the tests

```bash
.venv/bin/python -m pytest      # Python
node --test 'tests/js/*.test.js'   # the on-device solver
```

CI runs both on every pull request, plus the migrations against a real Postgres
(SQLite's loose typing makes `flask db check` report differences that are not
real).

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

### Migrating the database

**You do not normally run anything.** `gunicorn.conf.py` migrates the database
in gunicorn's master process before the first worker starts, so a deploy brings
its own schema with it. The start command stays a plain `gunicorn app:app …`
with nothing in it that can drift out of step with the deployed code.

Set `AUTO_MIGRATE=0` to turn that off and take manual control. The same logic is
available as a command:

```bash
flask deploy
```

Either way it is safe to run against any database in any state, and handles
three cases:

| State | What it does |
|---|---|
| Empty | Creates the schema from the migrations |
| Already under Alembic | Upgrades to the newest revision |
| Has tables but no `alembic_version` | Stamps the baseline, then upgrades |

That last case is a database created by an older `db.create_all()`. Plain
`flask db upgrade` fails on it — Alembic tries to `CREATE TABLE` over tables
that already exist — and a deploy whose release step fails leaves the app
running new code against the old schema. Uploads then fail with an opaque
reference number and the animal list comes back empty.

If that has already happened, booting the app fixes it in place; no data is
touched. `/healthz` reports the schema state, and returns 503 while it is wrong.

A failed migration never stops the app from starting. It boots on the old
schema, `/healthz` goes degraded, and uploads return a message naming the
problem — which beats a crash-looping host that serves nothing at all.

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

**Which sensor.** A phone can report orientation in more than one frame at once,
and they do not agree. `static/compass.js` picks exactly one and discards the
rest — true north, else magnetic, else nothing usable. See the header of that
file; feeding two frames into one filter is what made the needle unreadable in
the field, and no amount of smoothing fixes it. A phone with only a *relative*
frame knows how far it has turned, not which way it is pointing: the app says so
and refuses to lock a bearing from it, because a bearing wrong by an unknown
constant produces a confident fix in the wrong place.

**Solve.** Least squares over the bearing lines. Because each direction is a
unit vector, each row's residual is exactly the perpendicular distance from the
solution to that bearing line, in metres.

**Quality.** With more than two bearings, RMS residual. With exactly two, no
residual is reported at all — the system is square, so it is always zero, and
zero reads as "perfect". Crossing angle is reported instead. The solver also
flags bearings whose fix lies *behind* the observer, which is what a 180° error
looks like and is otherwise undetectable, and refuses a solve when the observers
are within 25 m of each other (standing together, every bearing line passes
through one point, so the "fix" is just where they are standing).

### The same solve on the phone

`static/triangulate.js` is a port of it, so the field app can answer "which way
do I walk?" with no signal. It is labelled **provisional** in the UI and the
server's answer replaces it after upload, because the phone sees only its own
bearings and has no World Magnetic Model for declination. `tests/js/` checks the
two agree to within a metre — if they drift apart, the field team stops trusting
both.

---

## Project layout

```
app.py             Factory, routes, sync orchestration, live-update stream
config.py          Environment config, validated at startup
models.py          SQLAlchemy models
triangulation.py   The solve — pure, no framework
geodesy.py         Projection, grid convergence, magnetic declination
validation.py      Request payload validation
auth.py            Coordinator sessions and field-device tokens
static/app.js      Field app
static/compass.js  Which orientation sensor to trust, and the smoothing
static/triangulate.js  The solve again, in JS, for offline use
static/dashboard.js  Mission control
sw.js              Service worker (offline)
tools/make_icons.py  Regenerates the app icons
tests/             pytest
tests/js/          node --test
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
- **The map follows new fixes until you take over.** A fix that arrives while
  the dashboard is open is drawn wherever it is, which may be off screen. The
  map re-fits to include it — until you pan or zoom yourself, after which it
  stops moving under you and shows a **Show** notice instead. **Zoom to all**
  hands control back.
- **The dashboard updates live.** `/api/stream` pushes over server-sent events,
  so a new fix appears within a couple of seconds. Each open dashboard holds a
  connection, which is why the Procfile uses `--worker-class gthread`; if the
  stream is unavailable the dashboard falls back to polling on its own.


---

## Keeping secrets out

`.env` and the SQLite database were both committed to this repository once,
while it was public. `.gitignore` does not prevent that — it has no effect on a
file that is already tracked, which is exactly how it happened.

`tools/check_secrets.py` now blocks it. CI runs it on every pull request, and it
can run as a pre-commit hook so the commit never happens at all:

```bash
git config core.hooksPath .githooks     # once per clone
```

It refuses any tracked `.env`, `*.db`, `*.sqlite`, private key or `.pem`, and
flags connection strings with inline passwords, AWS keys, GitHub tokens and
Mapbox secret tokens. Documented examples pointing at `localhost` or
`example.com` are ignored, so it stays quiet unless something is genuinely wrong.

Also worth turning on, free for public repositories: **Settings → Code security
→ Secret scanning** and **Push protection**, which blocks a push containing a
recognised credential before it reaches GitHub.

### If a secret is committed

Deleting the file does not un-publish it — anyone can still fetch it from the
commit it was added in, and forks keep their own copy.

1. **Rotate the credential.** This is the only step that actually revokes
   access; everything else is cleanup.
2. `git rm --cached <file>`, add it to `.gitignore`, commit.
3. Purge it from history and force-push.
4. Ask GitHub Support to drop cached views of the old blobs — a force-push alone
   does not remove them.

### Purging the history

`tools/purge_history.sh` does step 3 for the two files this actually happened to
(`.env` and `pangolin_data.db`):

```bash
pip install git-filter-repo

./tools/purge_history.sh --dry-run                    # rewrite, verify, push nothing
PANGOT_CREDENTIALS_ROTATED=yes ./tools/purge_history.sh
```

It clones a fresh mirror into a temporary directory rather than touching your
checkout, refuses to run until you confirm the credentials are rotated, asks for
a typed confirmation, checks the files are gone from every ref before it pushes
anything, and prints what is left to do afterwards.

Two things to expect. Every commit gets a new SHA, so existing clones must be
re-cloned — pushing from a stale one puts the old history straight back. And the
force-push moves `main`, so a host with auto-deploy will rebuild; the tree is
identical, but the service restarts.

Close or merge open pull requests first. An open PR pins the old commits, which
keeps the blobs reachable regardless.
