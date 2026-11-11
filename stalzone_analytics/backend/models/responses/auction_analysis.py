"""HTTP-ответ анализа и истории продаж выбранного предмета."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from common.enums.auction import HistoryDays
from common.models.analysis_result import AnalysisResult, Money
from common.models.history_status import HistoryStatus

AuctionSource = Literal["lots", "history"]


class AuctionSaleResponse(BaseModel):
    """Завершённая продажа одной сопоставимой партии."""

    time: datetime
    amount: int
    price_per_pack: Money | None


class AuctionHistoryResponse(BaseModel):
    """Продажи и граница подтверждённого покрытия окна графиков."""

    days: HistoryDays
    covered_from: datetime | None
    covered_until: datetime | None = None
    sales: list[AuctionSaleResponse]
    sync: HistoryStatus | None = None


class AuctionSourceErrorResponse(BaseModel):
    """Безопасное сообщение о сбое отдельного источника EXBO."""

    message: str
    retry_after_seconds: int | None = None


class AuctionAnalysisResponse(AnalysisResult):
    """Опубликованная оценка с историей для выбранного графика."""

    snapshot_id: str | None = None
    snapshot_age_seconds: int | None = None
    snapshot_stale: bool = False
    version: str | None = None
    history: AuctionHistoryResponse
    source_errors: dict[AuctionSource, AuctionSourceErrorResponse] = Field(
        default_factory=dict,
    )


class AuctionVersionResponse(BaseModel):
    """Версия готового результата и текущие признаки устаревания."""

    version: str | None = None
    snapshot_age_seconds: int | None = None
    snapshot_stale: bool = False
    history: HistoryStatus | None = None
