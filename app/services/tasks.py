from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Submission, SubmissionStatus, Task, TaskAssignment, TaskStatus


def task_is_available(task: Task, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    starts_at = task.starts_at if task.starts_at.tzinfo else task.starts_at.replace(tzinfo=UTC)
    ends_at = task.ends_at if task.ends_at.tzinfo else task.ends_at.replace(tzinfo=UTC)
    return task.deleted_at is None and task.status == TaskStatus.ACTIVE and starts_at <= now < ends_at


async def available_tasks(session: AsyncSession, user_id: int | None = None) -> list[Task]:
    now = datetime.now(UTC)
    visibility = Task.is_public.is_(True)
    if user_id is not None:
        visibility = or_(visibility, Task.assignments.any(TaskAssignment.user_id == user_id))
    tasks = list((await session.scalars(select(Task).where(
        Task.status == TaskStatus.ACTIVE,
        Task.deleted_at.is_(None),
        Task.starts_at <= now,
        Task.ends_at > now,
        visibility,
    ).order_by(Task.ends_at))).all())
    available = []
    for task in tasks:
        if task.maximum_participants is None:
            available.append(task)
            continue
        approved = await session.scalar(select(func.count(Submission.id)).where(Submission.task_id == task.id, Submission.status == SubmissionStatus.APPROVED))
        if (approved or 0) < task.maximum_participants:
            available.append(task)
    return available


async def get_available_task(session: AsyncSession, task_id: int, user_id: int | None = None) -> Task | None:
    task = await session.get(Task, task_id)
    if task is None or not task_is_available(task):
        return None
    if not task.is_public:
        assigned = user_id is not None and await session.scalar(select(TaskAssignment.task_id).where(
            TaskAssignment.task_id == task.id,
            TaskAssignment.user_id == user_id,
        )) is not None
        if not assigned:
            return None
    if task.maximum_participants is not None:
        count = await session.scalar(select(func.count(Submission.id)).where(Submission.task_id == task.id, Submission.status == SubmissionStatus.APPROVED))
        if (count or 0) >= task.maximum_participants:
            return None
    return task
