"""Виды и состояния фоновой загрузки истории."""

from enum import StrEnum


class HistoryOperation(StrEnum):
    """Часть истории, которую получает фоновый сборщик."""

    INITIAL = "initial"
    INCREMENTAL = "incremental"
    BACKFILL = "backfill"


class HistorySyncStatus(StrEnum):
    """Сохранённое состояние фоновой операции."""

    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    ERROR = "error"
