from functools import lru_cache

from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    database_url: str = Field(default="postgresql+psycopg://bob:bob@localhost:5432/bob", repr=False)
    database_pool_mode: Literal["pooled", "null"] = "pooled"
    cors_origins: list[str] = Field(default_factory=list)
    lablup_api_key: str | None = Field(
        default=None, repr=False, validation_alias=AliasChoices("lablup_api_key", "Lablup")
    )
    lablup_base_url: str = "https://api.console.lablup.ai/anthropic"
    lablup_model_id: str = "DeepSeek-V4.1-Flash-B300"
    lablup_vision_enabled: bool = False
    ai_timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_image_bytes: int = Field(default=10_485_760, gt=0, le=20_971_520)
    tesseract_cmd: str = "tesseract"
    ocr_backend: Literal["cli", "python"] = "cli"
    tessdata_dir: str = "vendor/tessdata"
    firebase_project_id: str | None = None
    google_application_credentials: str | None = Field(default=None, repr=False)
    firebase_service_account_json: str | None = Field(default=None, repr=False)
    cron_secret: str | None = Field(default=None, repr=False, min_length=32)
    notifications_hour: int = Field(default=20, ge=0, le=23)
    notification_poll_seconds: int = Field(default=60, ge=1)

    @field_validator("database_url")
    @classmethod
    def use_psycopg3(cls, value: str) -> str:
        # Cloud providers supply a standard PostgreSQL URL; this project uses psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
