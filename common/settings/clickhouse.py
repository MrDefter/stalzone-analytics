"""Настройки локального хранилища истории аукциона."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class ClickHouseSettings(BaseSettings):
    """Подключение и интервалы синхронизации истории аукциона."""

    model_config = SettingsConfigDict(
        env_prefix="STALZONE_CLICKHOUSE_", env_file=_ENV_FILE,
        env_file_encoding="utf-8-sig", extra="ignore", frozen=True,
        hide_input_in_errors=True, populate_by_name=True,
    )

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8123, ge=1, le=65535)
    database: str = Field(
        default="stalzone", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    user: str = Field(default="stalzone", min_length=1)
    password: SecretStr = SecretStr("")
    ttl_days: int = Field(
        default=30, ge=1, le=30, validation_alias="STALZONE_HISTORY_TTL_DAYS",
    )
    sync_interval_seconds: int = Field(
        default=60, ge=5, le=3600,
        validation_alias="STALZONE_HISTORY_SYNC_INTERVAL_SECONDS",
    )
    overlap_seconds: int = Field(
        default=120, ge=1, le=3600,
        validation_alias="STALZONE_HISTORY_OVERLAP_SECONDS",
    )
    chunk_timeout_seconds: int = Field(
        default=30, ge=5, le=300,
        validation_alias="STALZONE_HISTORY_CHUNK_TIMEOUT_SECONDS",
    )
