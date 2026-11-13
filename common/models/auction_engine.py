"""Модели хранимых версий и управляющих команд ядра."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from common.models.auction_strategy import StrategySettings


class MonitoringUpdate(BaseModel):
    """Параметры запроса на изменение мониторинга."""

    monitoring: bool | None = None
    interval: int | None = Field(default=None, ge=5, le=86400)
    priority: int | None = Field(default=None, ge=0, le=10)


class ScannerCommand(MonitoringUpdate):
    """Запрос на сканирование, расчёт или изменение мониторинга."""

    request_id: str
    operation: Literal["scan", "evaluate", "settings"]
    created_at: datetime
    expected_snapshot_id: str | None = None
    result_snapshot_id: str | None = None
    result_received_at: datetime | None = None
    result_analysis_version: str | None = None
    strategy: StrategySettings = Field(default_factory=StrategySettings)


class ScannerSettings(BaseModel):
    """Версия параметров мониторинга предмета."""

    monitoring: bool
    interval: int = Field(ge=5, le=86400)
    priority: int = 0


class SnapshotMetadata(BaseModel):
    """Маркер опубликованного снимка и качество исходной выборки."""

    snapshot_id: str
    started_at: datetime
    received_at: datetime
    published_at: datetime
    evaluated_at: datetime
    expires_at: datetime
    lots_loaded: int
    lots_reported_total: int
    lots_complete: bool
    lots_consistent: bool
    status: Literal["published"] = "published"
    error_type: str | None = None
    request_ids: tuple[str, ...] = ()


class ScannerState(BaseModel):
    """Последний результат сканирования и допустимое время повтора."""

    snapshot_id: str | None = None
    last_success_at: datetime | None = None
    next_at: datetime | None = None
    error_type: str | None = None
    failures: int = 0
    status: Literal["ready", "error"] = "ready"


class EngineStatus(BaseModel):
    """Подтверждение работы планировщика для HTTP-диагностики."""

    heartbeat: datetime
    online: bool = True
    active_operations: int = 0
    pending_commands: int = 0
    exbo_requests_last_minute: int = 0
    exbo_rate_limits: int = 0
    last_error: str | None = None
    last_publication: datetime | None = None
