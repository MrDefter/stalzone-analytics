"""Состояние и актуальность истории из локального хранилища."""

from datetime import datetime

from pydantic import BaseModel

from common.enums.auction_history import HistoryOperation, HistorySyncStatus


class HistoryStatus(BaseModel):
    """Сведения о сохранённом покрытии и фоновой операции."""

    status: HistorySyncStatus
    operation: HistoryOperation | None = None
    last_success_at: datetime | None = None
    covered_from: datetime | None = None
    covered_until: datetime | None = None
    initial_complete: bool = False
    backfill_complete: bool = False
    stale: bool = False
    error: str | None = None
