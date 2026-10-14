from datetime import UTC, datetime, timedelta
from pathlib import Path

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import FraudScore, OCRResult, Submission, SubmissionStatus, Task, TaskStatus
from app.services.runtime_settings import load_runtime_settings
from app.services.verification import save_verification_result, verify_submission

settings = get_settings()


def final_submission_status(_decision_status: str) -> SubmissionStatus:
    return SubmissionStatus.MANUAL_REVIEW


async def verify_submission_job(ctx: dict, submission_id: int) -> None:
    async with SessionLocal() as session:
        submission = await session.scalar(select(Submission).options(
            selectinload(Submission.user), selectinload(Submission.verification)
        ).where(Submission.id == submission_id).with_for_update())
        if submission is None or submission.status not in (SubmissionStatus.SUBMITTED, SubmissionStatus.PROCESSING):
            return
        submission.status = SubmissionStatus.PROCESSING
        task = await session.get(Task, submission.task_id)
        if task is None:
            submission.status = SubmissionStatus.MANUAL_REVIEW
            submission.reject_reason = "Task yozuvi topilmadi"
            await session.commit()
            return
        try:
            runtime_settings = await load_runtime_settings(session)
            decision = await verify_submission(session, submission, task, runtime_settings)
            final_status = final_submission_status(decision.status)
            submission.status = final_status
            submission.reject_reason = None
            submission.reviewed_at = datetime.now(UTC) if final_status == SubmissionStatus.REJECTED else None
            session.add(save_verification_result(submission.id, decision))
            session.add(OCRResult(submission_id=submission.id, text=decision.ocr_text))
            session.add(FraudScore(
                submission_id=submission.id,
                score=decision.fraud_score,
                signals=decision.reason,
            ))
            await session.commit()
        except Exception:
            await session.rollback()
            submission = await session.scalar(select(Submission).options(
                selectinload(Submission.user)
            ).where(Submission.id == submission_id))
            if submission:
                submission.status = SubmissionStatus.MANUAL_REVIEW
                submission.reject_reason = "Avtomatik tekshiruv yakunlanmadi"
                await session.commit()


async def maintenance_job(ctx: dict) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        runtime_settings = await load_runtime_settings(session)
        expired = list((await session.scalars(select(Task).where(
            Task.status == TaskStatus.ACTIVE,
            Task.ends_at <= now,
        ))).all())
        for task in expired:
            task.status = TaskStatus.EXPIRED

        capped_tasks = list((await session.scalars(select(Task).where(
            Task.status == TaskStatus.ACTIVE,
            Task.maximum_participants.is_not(None),
        ))).all())
        for task in capped_tasks:
            approved_count = await session.scalar(select(func.count(Submission.id)).where(
                Submission.task_id == task.id,
                Submission.status == SubmissionStatus.APPROVED,
            ))
            if (approved_count or 0) >= task.maximum_participants:
                task.status = TaskStatus.COMPLETED

        retention_days = runtime_settings.screenshot_retention_days
        if retention_days > 0:
            cutoff = now - timedelta(days=retention_days)
            old_submissions = list((await session.scalars(select(Submission).where(
                Submission.created_at < cutoff,
                Submission.image_path != "",
            ))).all())
            for submission in old_submissions:
                Path(submission.image_path).unlink(missing_ok=True)
                submission.image_path = ""
        await session.commit()


async def expire_tasks_job(ctx: dict) -> None:
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        expired = list((await session.scalars(select(Task).where(
            Task.status == TaskStatus.ACTIVE,
            Task.ends_at <= now,
        ))).all())
        for task in expired:
            task.status = TaskStatus.EXPIRED
        if expired:
            await session.commit()


async def startup(ctx: dict) -> None:
    ctx["settings"] = settings


async def shutdown(ctx: dict) -> None:
    await engine.dispose()


class WorkerSettings:
    functions = [verify_submission_job]
    cron_jobs = [
        cron(expire_tasks_job, minute=set(range(60)), unique=True),
        cron(maintenance_job, hour=3, minute=17, unique=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = 3
    job_timeout = 180
