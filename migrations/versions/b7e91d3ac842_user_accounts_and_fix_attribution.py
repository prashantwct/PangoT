"""User accounts and fix attribution

Replaces the single shared coordinator login with per-user accounts, and
records who deleted or edited a fix — which one shared credential could never
tell you.

Also adds ``calculated_fixes.updated_at`` so the live-update stream notices an
edited note, not only additions and deletions.

No backfill is needed. While the ``users`` table is empty the app falls back to
the ADMIN_USERNAME environment variable, so an existing deployment keeps
working until the first account is created.

Revision ID: b7e91d3ac842
Revises: a1c47f2b9e30
"""
import sqlalchemy as sa
from alembic import op

revision = "b7e91d3ac842"
down_revision = "a1c47f2b9e30"
branch_labels = None
depends_on = None


def _index_names(conn, table):
    return {index["name"] for index in sa.inspect(conn).get_indexes(table)}


def upgrade():
    conn = op.get_bind()

    # Repair: batch_alter_table in a1c47f2b9e30 rebuilds a table from scratch on
    # SQLite, and it did not carry over the group_id indexes created by the
    # initial migration. group_id is filtered on every recompute, so losing
    # those indexes is a silent performance regression that grows with the
    # dataset. Guarded because a database that never lost them is also valid.
    for table in ("raw_bearings", "calculated_fixes"):
        name = f"ix_{table}_group_id"
        if name not in _index_names(conn, table):
            op.create_index(name, table, ["group_id"], unique=False)

    # The recompute path filters on both together.
    if "ix_calculated_fixes_group_pango" not in _index_names(conn, "calculated_fixes"):
        op.create_index(
            "ix_calculated_fixes_group_pango", "calculated_fixes", ["group_id", "pango_id"]
        )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="coordinator"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("users", schema=None) as batch:
        batch.create_index(batch.f("ix_users_username"), ["username"], unique=True)

    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        batch.add_column(sa.Column("deleted_by", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("updated_by", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table("calculated_fixes", schema=None) as batch:
        batch.drop_column("updated_at")
        batch.drop_column("updated_by")
        batch.drop_column("deleted_by")

    with op.batch_alter_table("users", schema=None) as batch:
        batch.drop_index(batch.f("ix_users_username"))
    op.drop_table("users")

    conn = op.get_bind()
    for table, name in (
        ("calculated_fixes", "ix_calculated_fixes_group_pango"),
        ("calculated_fixes", "ix_calculated_fixes_group_id"),
        ("raw_bearings", "ix_raw_bearings_group_id"),
    ):
        if name in _index_names(conn, table):
            op.drop_index(name, table_name=table)
