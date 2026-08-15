"""Enforce NOT NULL on the calculated_fixes columns the model requires

a1c47f2b9e30 tightened raw_bearings but left calculated_fixes alone, so the
models declared group_id, pango_id, calc_lat and calc_lon as ``nullable=False``
while the database still accepted NULLs in all four. A fix row with no
coordinates is meaningless — the dashboard already has to special-case it — and
the database should be the thing that refuses it.

Caught by ``flask db check`` against Postgres in CI.

As in a1c47f2b9e30, a column is left alone if existing rows would violate the
constraint. Deleting a coordinator's field data to satisfy a schema change is
their decision, not this script's; the counts are printed so they can act.

Revision ID: c4a02f7d16b3
Revises: b7e91d3ac842
"""
import sqlalchemy as sa
from alembic import op

revision = "c4a02f7d16b3"
down_revision = "b7e91d3ac842"
branch_labels = None
depends_on = None

REQUIRED_COLUMNS = ("group_id", "pango_id", "calc_lat", "calc_lon")


def upgrade():
    conn = op.get_bind()

    blocked = {}
    for column in REQUIRED_COLUMNS:
        count = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM calculated_fixes WHERE {column} IS NULL")  # noqa: S608
        ).scalar()
        if count:
            blocked[column] = count

    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        for column in REQUIRED_COLUMNS:
            if column not in blocked:
                batch.alter_column(column, nullable=False)

    if blocked:
        print(
            "NOTE: left these calculated_fixes columns nullable because existing "
            f"rows contain NULLs: {blocked}. Those fixes cannot be placed on the "
            "map. Delete or repair them, then re-run `flask db upgrade`."
        )


def downgrade():
    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        for column in REQUIRED_COLUMNS:
            batch.alter_column(column, nullable=True)
