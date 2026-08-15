"""Reading IDs, true bearings, and a non-destructive fix lifecycle

Three changes, each backfilled so an existing field database upgrades in place:

* ``raw_bearings.reading_id`` — a unique idempotency key, so a retried sync
  cannot duplicate readings and skew the solve.
* ``raw_bearings.bearing_true`` plus the reference frame and declination that
  produced it, so magnetic and true headings stop being mixed silently.
* ``calculated_fixes`` gains quality columns and a soft lifecycle
  (``superseded_at`` / ``deleted_at``), so recalculation and deletion stop
  destroying data.

Existing bearings are backfilled with ``heading_ref = 'unknown'`` and
``bearing_true = bearing``. That preserves exactly the behaviour they were
recorded under — it does not retroactively claim a correction that was never
applied.

Revision ID: a1c47f2b9e30
Revises: 273e298aa774
"""
import uuid

import sqlalchemy as sa
from alembic import op

revision = "a1c47f2b9e30"
down_revision = "273e298aa774"
branch_labels = None
depends_on = None


# Columns that must be populated for a bearing to be usable. If a legacy row
# has NULLs here the NOT NULL constraint is skipped rather than failing the
# migration or deleting the row — that is the operator's call, not this script's.
REQUIRED_RAW_COLUMNS = ("group_id", "pango_id", "obs_lat", "obs_lon", "bearing", "timestamp")


def _null_counts(conn, table, columns):
    counts = {}
    for column in columns:
        result = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE {column} IS NULL")  # noqa: S608
        ).scalar()
        if result:
            counts[column] = result
    return counts


def upgrade():
    conn = op.get_bind()

    # --- raw_bearings: new columns -------------------------------------
    with op.batch_alter_table("raw_bearings", schema=None) as batch:
        batch.add_column(sa.Column("reading_id", sa.String(length=36), nullable=True))
        batch.add_column(sa.Column("device_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("heading_ref", sa.String(length=10), nullable=True))
        batch.add_column(sa.Column("declination_deg", sa.Float(), nullable=True))
        batch.add_column(sa.Column("bearing_true", sa.Float(), nullable=True))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))

    # --- raw_bearings: backfill ----------------------------------------
    # One UUID per existing row. Done row by row rather than in SQL because
    # neither SQLite nor older Postgres offers a portable UUID generator.
    for (row_id,) in conn.execute(sa.text("SELECT id FROM raw_bearings WHERE reading_id IS NULL")):
        conn.execute(
            sa.text("UPDATE raw_bearings SET reading_id = :rid WHERE id = :id"),
            {"rid": str(uuid.uuid4()), "id": row_id},
        )

    conn.execute(
        sa.text(
            """
            UPDATE raw_bearings
               SET heading_ref     = COALESCE(heading_ref, 'unknown'),
                   declination_deg = COALESCE(declination_deg, 0.0),
                   bearing_true    = COALESCE(bearing_true, bearing),
                   created_at      = COALESCE(created_at, timestamp)
            """
        )
    )

    # --- raw_bearings: constraints and widened columns ------------------
    skipped = _null_counts(conn, "raw_bearings", REQUIRED_RAW_COLUMNS)

    with op.batch_alter_table("raw_bearings", schema=None) as batch:
        batch.alter_column("reading_id", existing_type=sa.String(length=36), nullable=False)
        batch.alter_column("bearing_true", existing_type=sa.Float(), nullable=False)
        batch.alter_column(
            "pango_id", existing_type=sa.String(length=10), type_=sa.String(length=16)
        )
        batch.alter_column(
            "observer", existing_type=sa.String(length=10), type_=sa.String(length=16)
        )
        batch.alter_column(
            "timestamp", existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True)
        )
        for column in REQUIRED_RAW_COLUMNS:
            if column not in skipped:
                batch.alter_column(column, nullable=False)

        batch.create_index("ix_raw_bearings_reading_id", ["reading_id"], unique=True)
        batch.create_index("ix_raw_bearings_group_pango", ["group_id", "pango_id"])
        batch.create_index(batch.f("ix_raw_bearings_pango_id"), ["pango_id"])
        batch.create_index(batch.f("ix_raw_bearings_timestamp"), ["timestamp"])

    if skipped:
        print(
            "NOTE: left these raw_bearings columns nullable because existing rows "
            f"contain NULLs: {skipped}. Those readings cannot contribute to a fix. "
            "Clean them up and re-run `alembic upgrade head` to tighten the schema."
        )

    # --- calculated_fixes ----------------------------------------------
    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        batch.add_column(sa.Column("rms_error_m", sa.Float(), nullable=True))
        batch.add_column(sa.Column("crossing_angle_deg", sa.Float(), nullable=True))
        batch.add_column(sa.Column("n_bearings", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("quality", sa.String(length=8), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column(
            "pango_id", existing_type=sa.String(length=10), type_=sa.String(length=16)
        )
        batch.alter_column(
            "timestamp", existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True)
        )
        batch.create_index(batch.f("ix_calculated_fixes_pango_id"), ["pango_id"])
        batch.create_index(batch.f("ix_calculated_fixes_timestamp"), ["timestamp"])

    # Historic fixes predate quality reporting. Marking them 'unknown' rather
    # than guessing a grade keeps the dashboard honest about what it knows.
    conn.execute(
        sa.text(
            """
            UPDATE calculated_fixes
               SET quality    = COALESCE(quality, 'unknown'),
                   n_bearings = COALESCE(n_bearings, 0)
            """
        )
    )

    # --- animals --------------------------------------------------------
    with op.batch_alter_table("animals", schema=None) as batch:
        batch.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column("id", existing_type=sa.String(length=10), type_=sa.String(length=16))
        batch.alter_column(
            "created_at", existing_type=sa.DateTime(), type_=sa.DateTime(timezone=True)
        )


def downgrade():
    with op.batch_alter_table("animals", schema=None) as batch:
        batch.alter_column("id", existing_type=sa.String(length=16), type_=sa.String(length=10))
        batch.alter_column(
            "created_at", existing_type=sa.DateTime(timezone=True), type_=sa.DateTime()
        )
        batch.drop_column("retired_at")

    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        batch.drop_index(batch.f("ix_calculated_fixes_timestamp"))
        batch.drop_index(batch.f("ix_calculated_fixes_pango_id"))
        batch.alter_column(
            "timestamp", existing_type=sa.DateTime(timezone=True), type_=sa.DateTime()
        )
        batch.alter_column(
            "pango_id", existing_type=sa.String(length=16), type_=sa.String(length=10)
        )
        batch.drop_column("deleted_at")
        batch.drop_column("superseded_at")
        batch.drop_column("quality")
        batch.drop_column("n_bearings")
        batch.drop_column("crossing_angle_deg")
        batch.drop_column("rms_error_m")

    with op.batch_alter_table("raw_bearings", schema=None) as batch:
        batch.drop_index(batch.f("ix_raw_bearings_timestamp"))
        batch.drop_index(batch.f("ix_raw_bearings_pango_id"))
        batch.drop_index("ix_raw_bearings_group_pango")
        batch.drop_index("ix_raw_bearings_reading_id")
        batch.alter_column(
            "timestamp", existing_type=sa.DateTime(timezone=True), type_=sa.DateTime(), nullable=True
        )
        batch.alter_column(
            "observer", existing_type=sa.String(length=16), type_=sa.String(length=10)
        )
        batch.alter_column(
            "pango_id", existing_type=sa.String(length=16), type_=sa.String(length=10), nullable=True
        )
        for column in ("group_id", "obs_lat", "obs_lon", "bearing"):
            batch.alter_column(column, nullable=True)
        batch.drop_column("created_at")
        batch.drop_column("bearing_true")
        batch.drop_column("declination_deg")
        batch.drop_column("heading_ref")
        batch.drop_column("device_id")
        batch.drop_column("reading_id")
