"""Permanently remove previously hidden tasks and their dependent data."""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

from app.config import get_settings

revision = "0005_hard_delete_tasks"
down_revision = "0004_soft_delete_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    metadata = sa.MetaData()
    tasks = sa.Table("tasks", metadata, autoload_with=connection)
    submissions = sa.Table("submissions", metadata, autoload_with=connection)
    assignments = sa.Table("task_assignments", metadata, autoload_with=connection)
    verification_results = sa.Table("verification_results", metadata, autoload_with=connection)
    ocr_results = sa.Table("ocr_results", metadata, autoload_with=connection)
    fraud_scores = sa.Table("fraud_scores", metadata, autoload_with=connection)
    point_transactions = sa.Table("point_transactions", metadata, autoload_with=connection)
    admin_actions = sa.Table("admin_actions", metadata, autoload_with=connection)

    deleted_task_ids = list(connection.execute(
        sa.select(tasks.c.id).where(tasks.c.deleted_at.is_not(None))
    ).scalars())
    submission_ids = list(connection.execute(
        sa.select(submissions.c.id).where(submissions.c.task_id.in_(deleted_task_ids))
    ).scalars()) if deleted_task_ids else []
    file_paths = set()
    if deleted_task_ids:
        file_paths.update(filter(None, connection.execute(
            sa.select(tasks.c.advertisement_image_path).where(tasks.c.id.in_(deleted_task_ids))
        ).scalars()))
    if submission_ids:
        file_paths.update(filter(None, connection.execute(
            sa.select(submissions.c.image_path).where(submissions.c.id.in_(submission_ids))
        ).scalars()))

    if submission_ids:
        for table in (verification_results, ocr_results, fraud_scores, point_transactions):
            connection.execute(table.delete().where(table.c.submission_id.in_(submission_ids)))
        connection.execute(admin_actions.delete().where(
            admin_actions.c.entity_type == "submission",
            admin_actions.c.entity_id.in_(submission_ids),
        ))
        connection.execute(submissions.delete().where(submissions.c.id.in_(submission_ids)))
    if deleted_task_ids:
        connection.execute(assignments.delete().where(assignments.c.task_id.in_(deleted_task_ids)))
        connection.execute(admin_actions.delete().where(
            admin_actions.c.entity_type == "task",
            admin_actions.c.entity_id.in_(deleted_task_ids),
        ))
        connection.execute(tasks.delete().where(tasks.c.id.in_(deleted_task_ids)))

    upload_root = Path(get_settings().upload_dir).resolve()
    for file_path in file_paths:
        path = Path(file_path).resolve()
        try:
            path.relative_to(upload_root)
        except ValueError:
            continue
        task_reference = connection.scalar(sa.select(tasks.c.id).where(
            tasks.c.advertisement_image_path == str(path)
        ).limit(1))
        submission_reference = connection.scalar(sa.select(submissions.c.id).where(
            submissions.c.image_path == str(path)
        ).limit(1))
        if task_reference is None and submission_reference is None:
            path.unlink(missing_ok=True)

    if "deleted_at" in tasks.c:
        with op.batch_alter_table("tasks") as batch:
            batch.drop_column("deleted_at")


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
