"""Общие параметры хранилища и актуальности снимков."""

from typing import Self

from pydantic import AliasChoices, Field, model_validator

from .clickhouse import ClickHouseSettings

_IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_]*$"


class AuctionStorageSettings(ClickHouseSettings):
    """Подключение и интервалы проверки актуальности."""

    history_database: str = Field(
        default="stalzone_history", pattern=_IDENTIFIER,
    )
    snapshots_database: str = Field(
        default="stalzone_snapshots", pattern=_IDENTIFIER,
    )
    scheduler_interval: float = Field(
        default=5, gt=0, le=300,
        validation_alias="STALZONE_ENGINE_SCHEDULER_INTERVAL_SECONDS",
    )
    lots_interval: int = Field(
        default=180, ge=5, le=86400,
        validation_alias="STALZONE_ENGINE_LOTS_SCAN_INTERVAL_SECONDS",
    )
    sync_interval_seconds: int = Field(
        default=60, ge=5, le=86400,
        validation_alias=AliasChoices(
            "STALZONE_ENGINE_HISTORY_SYNC_INTERVAL_SECONDS",
            "STALZONE_HISTORY_SYNC_INTERVAL_SECONDS",
        ),
    )
    @model_validator(mode="after")
    def distinct_databases(self) -> Self:
        """Проверить разделение истории и снимков.

        Returns:
            Проверенные настройки.
        """
        if self.history_database == self.snapshots_database:
            raise ValueError("Укажите разные базы для истории и снимков.")
        return self
