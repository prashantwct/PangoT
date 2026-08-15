"""Generate realistic demo data for development.

Rewritten to go through the ORM rather than a hardcoded ``sqlite3.connect``,
so it works against whatever DATABASE_URL is configured — the previous version
could not run against the deployed Postgres at all.

    python seed_data.py            # 25 sessions, 2 observers each
    python seed_data.py --sessions 60 --clear

Never run this against production.
"""
import argparse
import random
import uuid

from app import create_app
from extensions import db
from geodesy import WGS84, bearing_between
from models import Animal, CalculatedFix, RawBearing, utcnow
from triangulation import Observation, TriangulationError, solve

SITE_LAT, SITE_LON = 19.0500, 73.0500
ANIMALS = [f"P{i:02d}" for i in range(1, 9)]
OBSERVERS = ["MK", "PD", "RA", "SN"]

# A hand-held antenna is good to a few degrees at best.
AIM_ERROR_DEG = 4.0
GPS_ACCURACY_M = (4, 30)


OBSERVER_RANGE_M = (300, 1200)
# A competent field team spreads out deliberately rather than standing where
# they happen to be, so seeded observers are placed at well-separated azimuths
# around the animal. Purely random placement puts them near-collinear often
# enough that most sessions would produce no fix at all.
MIN_AZIMUTH_SEPARATION_DEG = 45


def offset(lat, lon, max_deg=0.02):
    return lat + random.uniform(-max_deg, max_deg), lon + random.uniform(-max_deg, max_deg)


def observer_azimuths(count):
    """Azimuths around the animal, at least MIN_AZIMUTH_SEPARATION_DEG apart."""
    start = random.uniform(0, 360)
    spread = random.uniform(MIN_AZIMUTH_SEPARATION_DEG, 360 / count)
    return [(start + i * max(spread, MIN_AZIMUTH_SEPARATION_DEG)) % 360 for i in range(count)]


def seed(sessions, clear):
    app = create_app()
    with app.app_context():
        if clear:
            print("Clearing existing bearings and fixes…")
            CalculatedFix.query.delete()
            RawBearing.query.delete()
            db.session.commit()

        for animal_id in ANIMALS:
            if not db.session.get(Animal, animal_id):
                db.session.add(Animal(id=animal_id))
        db.session.commit()

        created = 0
        for index in range(1, sessions + 1):
            animal = random.choice(ANIMALS)
            true_lat, true_lon = offset(SITE_LAT, SITE_LON)
            group_id = f"SEED{index:03d}"

            observations = []
            for azimuth in observer_azimuths(random.choice([2, 2, 3])):
                obs_lon, obs_lat, _ = WGS84.fwd(
                    true_lon, true_lat, azimuth, random.uniform(*OBSERVER_RANGE_M)
                )
                true_bearing = bearing_between(obs_lat, obs_lon, true_lat, true_lon)
                measured = (true_bearing + random.gauss(0, AIM_ERROR_DEG)) % 360

                db.session.add(
                    RawBearing(
                        reading_id=str(uuid.uuid4()),
                        group_id=group_id,
                        pango_id=animal,
                        observer=random.choice(OBSERVERS),
                        obs_lat=obs_lat,
                        obs_lon=obs_lon,
                        gps_accuracy=random.uniform(*GPS_ACCURACY_M),
                        bearing=measured,
                        heading_ref="true",
                        declination_deg=0.0,
                        bearing_true=measured,
                        timestamp=utcnow(),
                    )
                )
                observations.append(Observation(obs_lat, obs_lon, measured))

            try:
                fix = solve(observations)
            except TriangulationError as exc:
                print(f"  {group_id}: no fix ({exc})")
                continue

            db.session.add(
                CalculatedFix(
                    group_id=group_id,
                    pango_id=animal,
                    calc_lat=fix.lat,
                    calc_lon=fix.lon,
                    rms_error_m=fix.rms_error_m,
                    crossing_angle_deg=fix.crossing_angle_deg,
                    n_bearings=fix.n_bearings,
                    quality=fix.quality,
                    note=fix.describe(),
                )
            )
            created += 1

        db.session.commit()
        print(f"Seeded {created} fixes from {sessions} sessions.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=25)
    parser.add_argument("--clear", action="store_true", help="delete existing bearings and fixes first")
    seed(**vars(parser.parse_args()))
