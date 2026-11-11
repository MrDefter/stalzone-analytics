"""Контракт сохранённого результата оценки аукциона."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

from common.enums.auction import AuctionMode
from common.enums.auction_analysis import (
    AnalysisStatus,
    PriceBasis,
    Q25Quality,
)
from common.models.auction_strategy import StrategySettings

# Десятичные строки сохраняют целые суммы при чтении JSON браузером.
Money = Annotated[int, PlainSerializer(str, return_type=str, when_used="json")]


class EvaluationCoverage(BaseModel):
    """Проверка полноты снимка и число загруженных строк."""

    lots_complete: bool
    history_24h_complete: bool
    history_prefix_consistent: bool
    history_window_complete: bool
    lots_loaded: int
    lots_reported_total: int | None
    history_loaded: int


class EvaluationModel(StrategySettings):
    """Параметры расчёта для просмотра в интерфейсе."""

    price_window_days: int = Field(description="Максимум дней анализа")
    sample_size: int = Field(description="Минимум продаж для выбора окна")
    window_mode: Literal["automatic"] = "automatic"
    price_window_hours: int | None = None
    sale_commission_bps: int | None
    listing_cost_per_pack: Money | None = None
    buyout_price_basis: PriceBasis | None = None
    commission_estimated: bool | None


class EvaluatedLot(BaseModel):
    """Активный лот и оценка перепродажи в игровых рублях.

    Суммы с суффиксом total отнесены ко всей покупке.
    Поля per_pack относятся к одной стандартной партии.
    ID уникален в пределах снимка. ROI выражен в процентах.
    """

    id: int
    amount: int
    end_time: datetime
    buyout_total: Money | None = None
    exit_price_per_pack: Money | None = None
    net_revenue_per_pack: Money | None = None
    gross_revenue_total: Money | None = None
    commission_total: Money | None = None
    listing_cost_total: Money | None = None
    net_revenue_total: Money | None = None
    profit_total: Money | None = None
    max_buyout: Money | None = None
    roi_percent: float | None = None
    active_competing_packs: int | None = None
    competition_to_turnover: float | None = None
    status: AnalysisStatus | None = None
    reasons: tuple[AnalysisStatus, ...]


class AnalysisResult(BaseModel):
    """Сохранённая оценка предмета и всех полученных активных лотов."""

    item_id: str
    analysis_version: str | None = None
    name_ru: str
    homogeneous: bool
    sell_quantity: int | None
    mode: AuctionMode
    region: Literal["RU"]
    received_at: datetime
    evaluated_at: datetime
    sample_count: int | None
    q25_per_pack: Money | None
    q25_quality: Q25Quality
    turnover_24h: int | None
    price_change_percent: float | None = None
    coverage: EvaluationCoverage
    model: EvaluationModel
    reasons: tuple[AnalysisStatus, ...]
    lots: list[EvaluatedLot]
