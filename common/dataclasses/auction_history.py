"""Ключ истории, её подтверждённое покрытие и сохранённые продажи."""

from dataclasses import dataclass
from datetime import datetime

from common.enums.auction import AuctionMode
from common.enums.auction_history import HistoryOperation, HistorySyncStatus
from common.models.auction import Sale


@dataclass(frozen=True, slots=True)
class HistoryKey:
    """Предмет, регион и источник с независимой историей продаж."""

    item_id: str
    region: str
    mode: AuctionMode


@dataclass(frozen=True, slots=True)
class HistoryCheckpoint:
    """Состояние синхронизации с границами подтверждённого участка."""

    updated_at: datetime
    covered_from: datetime | None = None
    covered_until: datetime | None = None
    pending_from: datetime | None = None
    pending_until: datetime | None = None
    last_success_at: datetime | None = None
    initial_since: datetime | None = None
    initial_complete: bool = False
    backfill_complete: bool = False
    operation: HistoryOperation | None = None
    status: HistorySyncStatus = HistorySyncStatus.PENDING
    last_error: str | None = None
    last_error_type: str | None = None
    last_error_at: datetime | None = None
    retry_at: datetime | None = None
    data_revision: datetime | None = None
    batch_revision: datetime | None = None


@dataclass(frozen=True, slots=True)
class StoredHistory:
    """Продажи из ClickHouse и их последняя контрольная точка."""

    rows: tuple[Sale, ...]
    checkpoint: HistoryCheckpoint | None
