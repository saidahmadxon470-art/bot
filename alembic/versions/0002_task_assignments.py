"""Add public and user-assigned task visibility."""

from alembic import op
import sqlalchemy as sa

revision = "0002_task_assignments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "is_public" not in task_columns:
        with op.batch_alter_table("tasks") as batch:
            batch.add_column(sa.Column("is_public", sa.Boolean(), server_default=sa.true(), nullable=False))
            batch.create_index("ix_tasks_is_public", ["is_public"])

    if not inspector.has_table("task_assignments"):
        op.create_table(
            "task_assignments",
            sa.Column("task_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("task_id", "user_id"),
        )
        op.create_index("ix_task_assignments_user_id", "task_assignments", ["user_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("task_assignments"):
        op.drop_index("ix_task_assignments_user_id", table_name="task_assignments")
        op.drop_table("task_assignments")
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    if "is_public" in task_columns:
        with op.batch_alter_table("tasks") as batch:
            batch.drop_index("ix_tasks_is_public")
            batch.drop_column("is_public")