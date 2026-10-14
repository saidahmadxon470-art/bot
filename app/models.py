from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TaskStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"


class MatchingMode(StrEnum):
    EXACT_TEXT = "EXACT_TEXT"
    FLEXIBLE_TEXT = "FLEXIBLE_TEXT"


class SubmissionStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class TransactionType(StrEnum):
    TASK_REWARD = "TASK_REWARD"
    ADMIN_BONUS = "ADMIN_BONUS"
    ADMIN_ADJUSTMENT = "ADMIN_ADJUSTMENT"
    PENALTY = "PENALTY"
    WITHDRAW = "WITHDRAW"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    first_name: Mapped[str] = mapped_column(String(128), default="")
    last_name: Mapped[str] = mapped_column(String(128), default="")
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submissions: Mapped[list["Submission"]] = relationship(back_populates="user")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_active_window", "status", "starts_at", "ends_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    advertisement_text: Mapped[str] = mapped_column(Text)
    advertisement_image_path: Mapped[str | None] = mapped_column(String(500))
    reward_points: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    maximum_participants: Mapped[int | None] = mapped_column(Integer)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    requirements: Mapped[str] = mapped_column(Text, default="")
    matching_mode: Mapped[MatchingMode] = mapped_column(Enum(MatchingMode, name="matching_mode"), default=MatchingMode.EXACT_TEXT)
    similarity_threshold: Mapped[float] = mapped_column(Float, default=0.9)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, name="task_status"), default=TaskStatus.DRAFT, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submissions: Mapped[list["Submission"]] = relationship(back_populates="task")
    assignments: Mapped[list["TaskAssignment"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskAssignment(Base):
    __tablename__ = "task_assignments"

    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    task: Mapped[Task] = relationship(back_populates="assignments")


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        Index("uq_submission_approved_user_task", "task_id", "user_id", unique=True, postgresql_where=text("status = 'APPROVED'")),
        Index("ix_submission_review_queue", "status", "created_at"),
        Index("ix_submission_hash", "sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="RESTRICT"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    image_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    perceptual_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[SubmissionStatus] = mapped_column(Enum(SubmissionStatus, name="submission_status"), default=SubmissionStatus.SUBMITTED, index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    task: Mapped[Task] = relationship(back_populates="submissions")
    user: Mapped[User] = relationship(back_populates="submissions")
    verification: Mapped["VerificationResult | None"] = relationship(back_populates="submission", uselist=False, cascade="all, delete-orphan")


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), unique=True)
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    ai_result: Mapped[str] = mapped_column(Text, default="{}")
    text_similarity: Mapped[float | None] = mapped_column(Float)
    fraud_score: Mapped[int] = mapped_column(Integer, default=0)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    submission: Mapped[Submission] = relationship(back_populates="verification")


class OCRResult(Base):
    __tablename__ = "ocr_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), unique=True)
    text: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(80), default="tesseract")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FraudScore(Base):
    __tablename__ = "fraud_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), unique=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    signals: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyStatistic(Base):
    __tablename__ = "daily_statistics"

    day: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    users: Mapped[int] = mapped_column(Integer, default=0)
    submissions: Mapped[int] = mapped_column(Integer, default=0)
    approved: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    points_awarded: Mapped[int] = mapped_column(BigInteger, default=0)


class PointTransaction(Base):
    __tablename__ = "point_transactions"
    __table_args__ = (Index("uq_reward_per_submission", "submission_id", unique=True, postgresql_where=text("submission_id IS NOT NULL")),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id", ondelete="RESTRICT"))
    amount: Mapped[int] = mapped_column(Integer)
    type: Mapped[TransactionType] = mapped_column(Enum(TransactionType, name="transaction_type"))
    description: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
