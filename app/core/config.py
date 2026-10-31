from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://bob:bob@localhost:5432/bob"
    cors_origins: list[str] = Field(default_factory=list)
    lablup_api_key: str | None = Field(default=None, repr=False)
    lablup_base_url: str = "https://api.console.lablup.ai/anthropic"
    lablup_model_id: str = "DeepSeek-V4.1-Flash-B300"
    lablup_vision_enabled: bool = False
    ai_timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_image_bytes: int = Field(default=10_485_760, gt=0, le=20_971_520)
    tesseract_cmd: str = "tesseract"
    firebase_project_id: str | None = None
    google_application_credentials: str | None = Field(default=None, repr=False)
    notifications_hour: int = Field(default=20, ge=0, le=23)
    notification_poll_seconds: int = Field(default=60, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
