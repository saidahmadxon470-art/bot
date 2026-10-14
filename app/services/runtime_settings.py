from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Setting

SETTING_FIELDS: dict[str, type] = {
    "submissions_per_minute": int,
    "submissions_per_day": int,
    "manual_review_confidence": float,
    "high_fraud_policy": str,
    "screenshot_retention_days": int,
}


async def load_runtime_settings(session: AsyncSession) -> Settings:
    base = get_settings()
    rows = (await session.scalars(select(Setting).where(Setting.key.in_(SETTING_FIELDS)))).all()
    updates = {row.key: SETTING_FIELDS[row.key](row.value) for row in rows}
    return base.model_copy(update=updates)