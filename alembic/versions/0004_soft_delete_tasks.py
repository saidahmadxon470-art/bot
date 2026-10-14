"""Allow deleting tasks without removing submission history."""

from alembic import op
import sqlalchemy as sa

revision = "0004_soft_delete_tasks"
down_revision = "0003_remove_ad_targets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "deleted_at" not in task_columns:
        with op.batch_alter_table("tasks") as batch:
            batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "deleted_at" in task_columns:
        with op.batch_alter_table("tasks") as batch:
            batch.drop_column("deleted_at")
