"""Split bearings into triangulation events

One animal in one session had exactly one fix, solved from every bearing ever
recorded for it. That is correct only when the field team starts a fresh
session for each round of bearings. When they do not — which happened — eight
bearings from four rounds are solved as one system. The animal moved between
rounds, so they intersect nowhere, and least squares returns a point in the
middle of a shape that means nothing.

These columns record which round a reading and a fix belong to, so a session
can hold one fix per round.

Existing rows keep NULL, which reads as "one event per session" and matches how
they were solved. `flask refix` re-solves them into rounds when you are ready;
it is deliberately not done here, because a data migration that runs the
solver would make a schema upgrade depend on the geometry code.

Revision ID: d5b83e91c027
Revises: c4a02f7d16b3
"""
import sqlalchemy as sa
from alembic import op

revision = "d5b83e91c027"
down_revision = "c4a02f7d16b3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("raw_bearings", schema=None) as batch:
        batch.add_column(sa.Column("event_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_raw_bearings_event_started_at", ["event_started_at"])

    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        batch.add_column(sa.Column("event_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_calculated_fixes_event_started_at", ["event_started_at"])
        batch.create_index(
            "ix_calculated_fixes_event", ["group_id", "pango_id", "event_started_at"]
        )


def downgrade():
    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        batch.drop_index("ix_calculated_fixes_event")
        batch.drop_index("ix_calculated_fixes_event_started_at")
        batch.drop_column("event_started_at")

    with op.batch_alter_table("raw_bearings", schema=None) as batch:
        batch.drop_index("ix_raw_bearings_event_started_at")
        batch.drop_column("event_started_at")
