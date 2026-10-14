"""Remove channel/group targets from community tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0003_remove_ad_targets"
down_revision = "0002_task_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("task_targets"):
        op.drop_table("task_targets")

    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    removed_task_columns = {"target_type", "target_username", "target_telegram_id"} & task_columns
    if removed_task_columns:
        for index in inspector.get_indexes("tasks"):
            indexed_columns = set(index.get("column_names") or [])
            if indexed_columns & removed_task_columns:
                op.drop_index(index["name"], table_name="tasks")
        with op.batch_alter_table("tasks") as batch:
            for column in sorted(removed_task_columns):
                batch.drop_column(column)

    if inspector.has_table("verification_results"):
        verification_columns = {column["name"] for column in inspector.get_columns("verification_results")}
        if "target_match" in verification_columns:
            with op.batch_alter_table("verification_results") as batch:
                batch.drop_column("target_match")

    if bind.dialect.name == "postgresql":
        op.execute("DROP TYPE IF EXISTS target_type")


def downgrade() -> None:
    op.create_table(
        "task_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True, unique=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("bot_access_status", sa.String(40), nullable=False, server_default="UNKNOWN"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("target_type", sa.String(16), nullable=True))
        batch.add_column(sa.Column("target_username", sa.String(64), nullable=True))
        batch.add_column(sa.Column("target_telegram_id", sa.BigInteger(), nullable=True))
    with op.batch_alter_table("verification_results") as batch:
        batch.add_column(sa.Column("target_match", sa.Boolean(), nullable=True))