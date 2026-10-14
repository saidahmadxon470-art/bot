from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PointTransaction, Submission, SubmissionStatus, Task, TaskStatus, TransactionType, User


async def award_submission(session: AsyncSession, submission_id: int) -> bool:
    submission = await session.scalar(
        select(Submission).where(Submission.id == submission_id).with_for_update()
    )
    if submission is None:
        raise ValueError("Submission not found")
    if submission.status == SubmissionStatus.APPROVED:
        return False
    if submission.status not in (SubmissionStatus.PROCESSING, SubmissionStatus.MANUAL_REVIEW, SubmissionStatus.SUBMITTED):
        raise ValueError("Rejected submissions cannot be approved")

    task = await session.scalar(select(Task).where(Task.id == submission.task_id).with_for_update())
    user = await session.scalar(select(User).where(User.id == submission.user_id).with_for_update())
    if task is None or user is None:
        raise ValueError("Submission task or user is missing")
    if task.maximum_participants is not None:
        approved_count = await session.scalar(select(func.count(Submission.id)).where(
            Submission.task_id == task.id,
            Submission.status == SubmissionStatus.APPROVED,
        ))
        if (approved_count or 0) >= task.maximum_participants:
            task.status = TaskStatus.COMPLETED
            raise ValueError("Task participant limit has been reached")

    submission.status = SubmissionStatus.APPROVED
    submission.reviewed_at = datetime.now(UTC)
    user.balance += task.reward_points
    session.add(PointTransaction(
        user_id=user.id,
        submission_id=submission.id,
        amount=task.reward_points,
        type=TransactionType.TASK_REWARD,
        description=f"Task #{task.id} reward",
    ))
    if task.maximum_participants is not None and (approved_count or 0) + 1 >= task.maximum_participants:
        task.status = TaskStatus.COMPLETED
    await session.flush()
    return True
