import json
import hashlib
import mimetypes
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypeAlias

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import SessionLocal, engine, get_session
from app.models import Admin, AdminAction, FraudScore, MatchingMode, OCRResult, PointTransaction, Setting, Submission, SubmissionStatus, Task, TaskAssignment, TaskStatus, TransactionType, User, VerificationResult
from app.security import hash_password, require_admin
from app.services.points import award_submission

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter(dependencies=[Depends(require_admin)])
SessionDep: TypeAlias = Annotated[AsyncSession, Depends(get_session)]
AdminDep: TypeAlias = Annotated[str, Depends(require_admin)]


class TaskCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    advertisement_text: str = Field(min_length=1)
    reward_points: int = Field(gt=0)
    starts_at: datetime
    ends_at: datetime
    maximum_participants: int | None = Field(default=None, gt=0)
    is_public: bool = True
    assigned_user_telegram_id: int | None = Field(default=None, gt=0)
    requirements: str = ""
    matching_mode: MatchingMode = MatchingMode.EXACT_TEXT
    similarity_threshold: float = Field(default=0.9, ge=0, le=1)
    status: TaskStatus = TaskStatus.ACTIVE


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = None
    advertisement_text: str | None = Field(default=None, min_length=1)
    reward_points: int | None = Field(default=None, gt=0)
    is_public: bool | None = None
    assigned_user_telegram_id: int | None = Field(default=None, gt=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    maximum_participants: int | None = Field(default=None, gt=0)
    requirements: str | None = None
    matching_mode: MatchingMode | None = None
    similarity_threshold: float | None = Field(default=None, ge=0, le=1)
    status: TaskStatus | None = None


class RejectBody(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class PointAdjustment(BaseModel):
    amount: int
    description: str = Field(min_length=3, max_length=300)


class UserMessage(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class SettingsUpdate(BaseModel):
    submissions_per_minute: int | None = Field(default=None, ge=1, le=60)
    submissions_per_day: int | None = Field(default=None, ge=1, le=5000)
    manual_review_confidence: float | None = Field(default=None, ge=0, le=1)
    high_fraud_policy: str | None = Field(default=None, pattern="^(manual|reject)$")
    screenshot_retention_days: int | None = Field(default=None, ge=0, le=3650)


class AdminCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern="^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin: AdminDep):
    return templates.TemplateResponse(request, "dashboard.html", {"admin": admin})


@router.get("/api/tasks")
async def list_tasks(session: SessionDep):
    tasks = (await session.scalars(
        select(Task).where(Task.deleted_at.is_(None)).order_by(Task.created_at.desc()).limit(200)
    )).all()
    task_ids = [task.id for task in tasks]
    counts = dict((await session.execute(select(Submission.task_id, func.count(Submission.id)).where(
        Submission.status == SubmissionStatus.APPROVED,
        Submission.task_id.in_(task_ids),
    ).group_by(Submission.task_id))).all()) if tasks else {}
    assignees = dict((await session.execute(
        select(TaskAssignment.task_id, User.telegram_id)
        .join(User, User.id == TaskAssignment.user_id)
        .where(TaskAssignment.task_id.in_(task_ids))
    )).all()) if tasks else {}
    return [{"id": task.id, "title": task.title, "is_public": task.is_public,
             "assigned_user_telegram_id": assignees.get(task.id),
             "reward_points": task.reward_points, "participants": counts.get(task.id, 0),
             "status": task.status.value, "ends_at": task.ends_at.isoformat()}
            for task in tasks]


@router.post("/api/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(data: TaskCreate, session: SessionDep, admin: AdminDep):
    if data.title.startswith("/"):
        raise HTTPException(422, "Task title must be a task name, not a bot command")
    if data.ends_at <= data.starts_at:
        raise HTTPException(422, "ends_at must be after starts_at")
    assigned_user_telegram_id = data.assigned_user_telegram_id
    if not data.is_public and assigned_user_telegram_id is None:
        raise HTTPException(422, "Select a member for a private task")
    values = data.model_dump(exclude={"assigned_user_telegram_id", "is_public"})
    values["is_public"] = assigned_user_telegram_id is None
    assigned_user = None
    if assigned_user_telegram_id is not None:
        assigned_user = await session.scalar(select(User).where(User.telegram_id == assigned_user_telegram_id))
        if assigned_user is None:
            raise HTTPException(404, "Assigned user not found; they must start the bot first")
    task = Task(**values)
    session.add(task)
    await session.flush()
    if assigned_user:
        session.add(TaskAssignment(task_id=task.id, user_id=assigned_user.id))
    session.add(AdminAction(action="CREATE_TASK", entity_type="task", entity_id=task.id, details=task.title))
    recipient_ids = [assigned_user.telegram_id] if assigned_user else list((await session.scalars(
        select(User.telegram_id).where(User.is_blocked.is_(False))
    )).all())
    task_id = task.id
    task_title = task.title
    task_advertisement = task.advertisement_text
    task_reward = task.reward_points
    task_ends_at = task.ends_at
    await session.commit()
    from app.services.notifications import send_task_announcement
    sent_count = await send_task_announcement(
        is_public=assigned_user is None,
        telegram_ids=recipient_ids,
        message=f"{'📢 Yangi topshiriq' if assigned_user is None else '📩 Sizga shaxsiy topshiriq berildi'} "
        f"#{task_id}: {task_title}\n\n📝 {task_advertisement}\n\n"
        f"⭐ Mukofot: {task_reward} ball\n⏰ Tugash vaqti: {task_ends_at:%d-%m-%Y %H:%M UTC}\n\n"
        + (
            "Batafsil ma’lumotni ko‘rish va bajarishni boshlash uchun tugmani bosing."
            if assigned_user is None
            else "Topshiriq “📋 Topshiriqlar” bo‘limingizda ham mavjud. Bajarib bo‘lgach screenshot yuboring."
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Topshiriqni ochish", callback_data=f"task:{task_id}")
        ]]),
    )
    return {
        "id": task_id,
        "status": task.status.value,
        "notified_users": sent_count,
        "available_to_assignee": assigned_user is not None,
    }


@router.get("/api/tasks/{task_id}")
async def task_detail(task_id: int, session: SessionDep):
    task = await session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        raise HTTPException(404, "Task not found")
    assigned_user_telegram_id = await session.scalar(
        select(User.telegram_id)
        .join(TaskAssignment, TaskAssignment.user_id == User.id)
        .where(TaskAssignment.task_id == task.id)
        .limit(1)
    )
    return {"id": task.id, "title": task.title, "description": task.description, "advertisement_text": task.advertisement_text,
            "is_public": task.is_public, "assigned_user_telegram_id": assigned_user_telegram_id,
            "reward_points": task.reward_points,
            "starts_at": task.starts_at.isoformat(), "ends_at": task.ends_at.isoformat(),
            "maximum_participants": task.maximum_participants, "requirements": task.requirements,
            "matching_mode": task.matching_mode.value, "similarity_threshold": task.similarity_threshold,
            "status": task.status.value}


@router.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, data: TaskUpdate, session: SessionDep, admin: AdminDep):
    task = await session.get(Task, task_id, with_for_update=True)
    if task is None or task.deleted_at is not None:
        raise HTTPException(404, "Task not found")
    updates = data.model_dump(exclude_unset=True)
    if updates.get("title") is not None and updates["title"].startswith("/"):
        raise HTTPException(422, "Task title must be a task name, not a bot command")
    is_public = updates.pop("is_public", None)
    assigned_user_telegram_id = updates.pop("assigned_user_telegram_id", None)
    starts_at = updates.get("starts_at", task.starts_at)
    ends_at = updates.get("ends_at", task.ends_at)
    if ends_at <= starts_at:
        raise HTTPException(422, "ends_at must be after starts_at")
    for field, value in updates.items():
        setattr(task, field, value)
    if is_public is True:
        task.is_public = True
        await session.execute(TaskAssignment.__table__.delete().where(TaskAssignment.task_id == task.id))
    elif is_public is False or assigned_user_telegram_id is not None:
        if assigned_user_telegram_id is None:
            raise HTTPException(422, "A Telegram user ID is required for a private task")
        assigned_user = await session.scalar(select(User).where(User.telegram_id == assigned_user_telegram_id))
        if assigned_user is None:
            raise HTTPException(404, "Assigned user not found")
        task.is_public = False
        await session.execute(TaskAssignment.__table__.delete().where(TaskAssignment.task_id == task.id))
        session.add(TaskAssignment(task_id=task.id, user_id=assigned_user.id))
    session.add(AdminAction(action="UPDATE_TASK", entity_type="task", entity_id=task.id, details=admin))
    await session.commit()
    return {"id": task.id, "status": task.status.value}


@router.delete("/api/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, session: SessionDep, admin: AdminDep):
    task = await session.get(Task, task_id, with_for_update=True)
    if task is None or task.deleted_at is not None:
        raise HTTPException(404, "Task not found")
    task.deleted_at = datetime.now(UTC)
    task.status = TaskStatus.PAUSED
    session.add(AdminAction(
        action="DELETE_TASK",
        entity_type="task",
        entity_id=task.id,
        details=f"{admin}: soft-deleted task: {task.title}",
    ))
    await session.commit()


@router.post("/api/tasks/{task_id}/image")
async def upload_task_image(task_id: int, session: SessionDep, file: UploadFile = File(...)):
    task = await session.get(Task, task_id)
    if task is None or task.deleted_at is not None:
        raise HTTPException(404, "Task not found")
    settings = get_settings()
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "Image exceeds configured upload limit")
    try:
        with Image.open(__import__("io").BytesIO(content)) as image:
            image.verify()
            extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image.format)
    except (UnidentifiedImageError, OSError):
        extension = None
    if extension is None:
        raise HTTPException(415, "Upload a valid JPEG, PNG, or WebP image")
    digest = hashlib.sha256(content).hexdigest()
    folder = Path(settings.upload_dir) / "ads"
    folder.mkdir(parents=True, exist_ok=True)
    image_path = folder / f"{digest}{extension}"
    image_path.write_bytes(content)
    task.advertisement_image_path = str(image_path.resolve())
    await session.commit()
    return {"id": task.id, "image_uploaded": True}


async def set_task_status(task_id: int, status_value: TaskStatus, session: AsyncSession, admin: str):
    task = await session.get(Task, task_id, with_for_update=True)
    if task is None or task.deleted_at is not None:
        raise HTTPException(404, "Task not found")
    if status_value == TaskStatus.ACTIVE and task.ends_at <= datetime.now(UTC):
        raise HTTPException(409, "Expired task cannot be activated")
    task.status = status_value
    session.add(AdminAction(action=f"TASK_{status_value.value}", entity_type="task", entity_id=task.id, details=admin))
    await session.commit()
    return {"id": task.id, "status": task.status.value}


@router.post("/api/tasks/{task_id}/activate")
async def activate_task(task_id: int, session: SessionDep, admin: AdminDep):
    return await set_task_status(task_id, TaskStatus.ACTIVE, session, admin)


@router.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: int, session: SessionDep, admin: AdminDep):
    return await set_task_status(task_id, TaskStatus.PAUSED, session, admin)


@router.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: int, session: SessionDep, admin: AdminDep):
    return await set_task_status(task_id, TaskStatus.COMPLETED, session, admin)


@router.get("/api/submissions")
async def list_submissions(session: SessionDep, review: bool = False, status_filter: str | None = None, high_risk: bool = False):
    query = select(Submission).order_by(Submission.created_at.desc()).limit(300)
    if review:
        query = query.where(Submission.status == SubmissionStatus.MANUAL_REVIEW)
    if status_filter:
        try:
            query = query.where(Submission.status == SubmissionStatus(status_filter))
        except ValueError as exc:
            raise HTTPException(422, "Unknown submission status") from exc
    if high_risk:
        query = query.join(FraudScore).where(FraudScore.score >= 70)
    submissions = (await session.scalars(query)).all()
    return [{"id": item.id, "user_id": item.user_id, "task_id": item.task_id, "status": item.status.value,
             "created_at": item.created_at.isoformat(), "reject_reason": item.reject_reason,
             "screenshot_url": f"/api/submissions/{item.id}/screenshot"} for item in submissions]


@router.get("/api/submissions/{submission_id}")
async def submission_detail(submission_id: int, session: SessionDep):
    item = await session.get(Submission, submission_id)
    if item is None:
        raise HTTPException(404, "Submission not found")
    result = await session.scalar(select(VerificationResult).where(VerificationResult.submission_id == item.id))
    ocr = await session.scalar(select(OCRResult).where(OCRResult.submission_id == item.id))
    fraud = await session.scalar(select(FraudScore).where(FraudScore.submission_id == item.id))
    task = await session.get(Task, item.task_id)
    return {"id": item.id, "user_id": item.user_id, "task_id": item.task_id, "status": item.status.value,
            "advertisement": task.advertisement_text if task else None, "screenshot_url": f"/api/submissions/{item.id}/screenshot",
            "ocr_text": ocr.text if ocr else result.ocr_text if result else "",
            "ai_result": json.loads(result.ai_result) if result else {},
            "similarity": result.text_similarity if result else None,
            "fraud_score": fraud.score if fraud else result.fraud_score if result else None,
            "reason": result.decision_reason if result else item.reject_reason}


@router.get("/api/submissions/{submission_id}/screenshot")
async def get_screenshot(submission_id: int, session: SessionDep):
    item = await session.get(Submission, submission_id)
    if item is None or not Path(item.image_path).is_file():
        raise HTTPException(404, "Screenshot not found")
    media_type = mimetypes.guess_type(item.image_path)[0] or "application/octet-stream"
    return FileResponse(item.image_path, media_type=media_type, filename=f"submission-{item.id}{Path(item.image_path).suffix}")


@router.post("/api/submissions/{submission_id}/approve")
async def approve_submission(submission_id: int, session: SessionDep, admin: AdminDep):
    try:
        changed = await award_submission(session, submission_id)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc)) from exc
    item = await session.get(Submission, submission_id)
    user = await session.get(User, item.user_id) if item else None
    task = await session.get(Task, item.task_id) if item else None
    session.add(AdminAction(action="APPROVE_SUBMISSION", entity_type="submission", entity_id=submission_id, details=admin))
    await session.commit()
    if changed and item and user and task:
        from app.services.notifications import send_user_message
        await send_user_message(user.telegram_id, f"✅ Topshirig'ingiz tasdiqlandi!\n\n🎯 Task #{task.id}\n⭐ +{task.reward_points} ball\n💰 Balansingiz: {user.balance} ⭐")
    return {"id": submission_id, "approved": changed, "status": item.status.value if item else "APPROVED"}


@router.post("/api/submissions/{submission_id}/reject")
async def reject_submission(submission_id: int, body: RejectBody, session: SessionDep, admin: AdminDep):
    item = await session.scalar(select(Submission).where(Submission.id == submission_id).with_for_update())
    if item is None:
        raise HTTPException(404, "Submission not found")
    if item.status == SubmissionStatus.APPROVED:
        raise HTTPException(409, "Approved submission cannot be rejected")
    item.status = SubmissionStatus.REJECTED
    item.reject_reason = body.reason
    item.reviewed_at = datetime.now(UTC)
    session.add(AdminAction(action="REJECT_SUBMISSION", entity_type="submission", entity_id=item.id, details=f"{admin}: {body.reason}"))
    user = await session.get(User, item.user_id)
    task = await session.get(Task, item.task_id)
    await session.commit()
    if user and task:
        from app.services.notifications import send_user_message
        await send_user_message(
            user.telegram_id,
            f"❌ Topshirig'ingiz rad etildi.\n\n🎯 Task #{task.id}\nSabab: {body.reason}",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔁 Qayta urinish", callback_data=f"task:{task.id}")
            ]]),
        )
    return {"id": item.id, "status": item.status.value}


@router.get("/api/users")
async def list_users(session: SessionDep, query: str | None = None, blocked: bool | None = None):
    statement = select(User).order_by(User.created_at.desc()).limit(300)
    if query:
        pattern = f"%{query}%"
        statement = statement.where(
            User.username.ilike(pattern)
            | User.first_name.ilike(pattern)
            | User.last_name.ilike(pattern)
            | cast(User.telegram_id, String).ilike(pattern)
        )
    if blocked is not None:
        statement = statement.where(User.is_blocked == blocked)
    users = (await session.scalars(statement)).all()
    return [{"id": user.id, "telegram_id": user.telegram_id, "username": user.username,
             "name": f"{user.first_name} {user.last_name}".strip(), "balance": user.balance,
             "blocked": user.is_blocked, "created_at": user.created_at.isoformat()} for user in users]


@router.get("/api/users/{user_id}")
async def user_detail(user_id: int, session: SessionDep):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    submissions = list((await session.scalars(select(Submission).where(Submission.user_id == user.id))).all())
    transactions = list((await session.scalars(select(PointTransaction).where(PointTransaction.user_id == user.id).order_by(PointTransaction.created_at.desc()).limit(30))).all())
    counts = {item.value: sum(submission.status == item for submission in submissions) for item in SubmissionStatus}
    return {"id": user.id, "telegram_id": user.telegram_id, "username": user.username,
            "name": f"{user.first_name} {user.last_name}".strip(), "balance": user.balance,
            "total_submissions": len(submissions), "approved": counts["APPROVED"],
            "rejected": counts["REJECTED"], "pending": counts["SUBMITTED"] + counts["PROCESSING"] + counts["MANUAL_REVIEW"],
            "approval_rate": counts["APPROVED"] / len(submissions) if submissions else 0,
            "points_history": [{"amount": entry.amount, "type": entry.type.value, "description": entry.description,
                                "created_at": entry.created_at.isoformat()} for entry in transactions]}


@router.post("/api/users/{user_id}/points")
async def adjust_user_points(user_id: int, body: PointAdjustment, session: SessionDep, admin: AdminDep):
    if body.amount == 0:
        raise HTTPException(422, "Point adjustment cannot be zero")
    user = await session.get(User, user_id, with_for_update=True)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.balance + body.amount < 0:
        raise HTTPException(409, "Point balance cannot become negative")
    user.balance += body.amount
    session.add(PointTransaction(user_id=user.id, amount=body.amount, type=TransactionType.ADMIN_ADJUSTMENT,
                                 description=body.description))
    session.add(AdminAction(action="ADJUST_POINTS", entity_type="user", entity_id=user.id,
                            details=f"{admin}: {body.amount} {body.description}"))
    await session.commit()
    return {"id": user.id, "balance": user.balance}


@router.post("/api/users/{user_id}/message")
async def message_user(user_id: int, body: UserMessage, session: SessionDep, admin: AdminDep):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    from app.services.notifications import send_user_message
    sent = await send_user_message(user.telegram_id, body.message)
    session.add(AdminAction(action="MESSAGE_USER", entity_type="user", entity_id=user.id, details=admin))
    await session.commit()
    return {"id": user.id, "sent": sent}


@router.post("/api/users/{user_id}/block")
async def block_user(user_id: int, session: SessionDep, admin: AdminDep):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.is_blocked = True
    session.add(AdminAction(action="BLOCK_USER", entity_type="user", entity_id=user.id, details=admin))
    await session.commit()
    return {"id": user.id, "blocked": True}


@router.post("/api/users/{user_id}/unblock")
async def unblock_user(user_id: int, session: SessionDep, admin: AdminDep):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    user.is_blocked = False
    session.add(AdminAction(action="UNBLOCK_USER", entity_type="user", entity_id=user.id, details=admin))
    await session.commit()
    return {"id": user.id, "blocked": False}


@router.get("/api/leaderboard")
async def get_leaderboard(session: SessionDep, period: str = "all"):
    if period not in {"today", "7d", "30d", "all"}:
        raise HTTPException(422, "period must be today, 7d, 30d, or all")
    query = select(User.username, User.first_name, func.sum(PointTransaction.amount).label("points")).join(PointTransaction).group_by(User.id).order_by(func.sum(PointTransaction.amount).desc()).limit(100)
    if period == "today":
        query = query.where(PointTransaction.created_at >= datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0))
    elif period != "all":
        from datetime import timedelta
        delta = {"7d": timedelta(days=7), "30d": timedelta(days=30)}[period]
        query = query.where(PointTransaction.created_at >= datetime.now(UTC) - delta)
    rows = (await session.execute(query)).all()
    return [{"rank": index, "username": row.username, "name": row.first_name, "points": row.points or 0} for index, row in enumerate(rows, 1)]


@router.get("/api/statistics")
async def statistics(session: SessionDep):
    total_users = await session.scalar(select(func.count(User.id))) or 0
    active_tasks = await session.scalar(select(func.count(Task.id)).where(Task.status == TaskStatus.ACTIVE)) or 0
    total_submissions = await session.scalar(select(func.count(Submission.id))) or 0
    counts = {status.value: await session.scalar(select(func.count(Submission.id)).where(Submission.status == status)) or 0 for status in SubmissionStatus}
    points = await session.scalar(select(func.coalesce(func.sum(PointTransaction.amount), 0))) or 0
    return {"users": total_users, "active_tasks": active_tasks, "submissions": total_submissions,
            "approved": counts["APPROVED"], "rejected": counts["REJECTED"],
            "manual_review": counts["MANUAL_REVIEW"], "processing": counts["PROCESSING"] + counts["SUBMITTED"],
            "points_distributed": points}


@router.get("/api/settings")
async def get_admin_settings(session: SessionDep, admin: AdminDep):
    settings = get_settings()
    rows = (await session.scalars(select(Setting))).all()
    values = {row.key: row.value for row in rows}
    return {"submissions_per_minute": int(values.get("submissions_per_minute", settings.submissions_per_minute)),
            "submissions_per_day": int(values.get("submissions_per_day", settings.submissions_per_day)),
            "manual_review_confidence": float(values.get("manual_review_confidence", settings.manual_review_confidence)),
            "high_fraud_policy": values.get("high_fraud_policy", settings.high_fraud_policy),
            "screenshot_retention_days": int(values.get("screenshot_retention_days", settings.screenshot_retention_days))}


@router.patch("/api/settings")
async def update_admin_settings(data: SettingsUpdate, session: SessionDep, admin: AdminDep):
    updates = data.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in updates.items():
        setting = await session.get(Setting, key)
        if setting is None:
            session.add(Setting(key=key, value=str(value)))
        else:
            setting.value = str(value)
    session.add(AdminAction(action="UPDATE_SETTINGS", entity_type="settings", details=f"{admin}: {json.dumps(updates)}"))
    await session.commit()
    return await get_admin_settings(session, admin)


@router.get("/api/admins")
async def list_admins(session: SessionDep, admin: AdminDep):
    admins = (await session.scalars(select(Admin).order_by(Admin.username))).all()
    return [{"id": item.id, "username": item.username, "is_active": item.is_active,
             "created_at": item.created_at.isoformat(),
             "last_login_at": item.last_login_at.isoformat() if item.last_login_at else None}
            for item in admins]


@router.post("/api/admins", status_code=status.HTTP_201_CREATED)
async def create_admin(data: AdminCreate, session: SessionDep, admin: AdminDep):
    if not get_settings().admin_secret.get_secret_value():
        raise HTTPException(503, "ADMIN_SECRET must be configured before adding admin accounts")
    if await session.scalar(select(Admin.id).where(Admin.username == data.username)):
        raise HTTPException(409, "Admin username already exists")
    record = Admin(username=data.username, password_hash=hash_password(
        data.password, get_settings().admin_secret.get_secret_value()
    ))
    session.add(record)
    session.add(AdminAction(action="CREATE_ADMIN", entity_type="admin", details=f"{admin}: {data.username}"))
    await session.commit()
    return {"id": record.id, "username": record.username, "is_active": record.is_active}


@router.patch("/api/admins/{admin_id}")
async def set_admin_status(admin_id: int, enabled: bool, session: SessionDep, admin: AdminDep):
    record = await session.get(Admin, admin_id)
    if record is None:
        raise HTTPException(404, "Admin not found")
    if record.username == admin and not enabled:
        raise HTTPException(409, "You cannot disable your own account")
    record.is_active = enabled
    session.add(AdminAction(action="SET_ADMIN_STATUS", entity_type="admin", entity_id=record.id,
                            details=f"{admin}: enabled={enabled}"))
    await session.commit()
    return {"id": record.id, "is_active": record.is_active}


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        await engine.dispose()

    application = FastAPI(title="Task Verification Admin", docs_url="/api/docs", lifespan=lifespan)
    application.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    application.include_router(router)

    @application.get("/health")
    async def health():
        async with SessionLocal() as session:
            await session.execute(select(1))
        return {"status": "ok"}

    return application


app = create_app()
