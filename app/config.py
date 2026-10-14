from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = SecretStr("")
    database_url: str = "postgresql+asyncpg://bot:change-me@localhost:5432/taskbot"
    redis_url: str = "redis://localhost:6379/0"
    admin_ids: str = ""
    admin_secret: SecretStr = SecretStr("")
    admin_username: str = "admin"
    admin_password_hash: str = ""
    ai_api_key: SecretStr = SecretStr("")
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4o-mini"
    ocr_language: str = "eng+rus+uzb"
    upload_dir: str = "./data/uploads"
    max_upload_bytes: int = 10 * 1024 * 1024
    submissions_per_minute: int = 5
    submissions_per_day: int = 50
    manual_review_confidence: float = Field(default=0.78, ge=0, le=1)
    high_fraud_policy: str = "manual"
    screenshot_retention_days: int = 30
    default_task_reward_points: int = Field(default=100, ge=1)
    default_task_duration_hours: int = Field(default=24, ge=1, le=720)
    app_env: str = "development"
    local_mode: bool = Field(default=False, validation_alias="BOT_LOCAL_MODE")

    @property
    def admin_id_set(self) -> set[int]:
        return {int(value.strip()) for value in self.admin_ids.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
