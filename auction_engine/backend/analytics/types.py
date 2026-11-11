"""Входные признаки покрытия и результаты расчёта перепродажи."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from fractions import Fraction

from common.enums.auction_analysis import AnalysisStatus, Q25Quality


@dataclass(frozen=True, slots=True)
class AnalysisCoverage:
    """Покрытие, проверенное при чтении страниц.

    Attributes:
        lots_complete: Получен согласованный полный снимок лотов.
        history_24h_complete: Подтверждено покрытие суточного окна.
        history_prefix_consistent: Прочитанный префикс согласован.
        history_complete: Все заявленные строки истории прочитаны.
        history_covered_from: Нижняя граница подтверждённого кэша.
        history_covered_until: Момент проверки истории в ClickHouse.
    """

    lots_complete: bool
    history_24h_complete: bool
    history_prefix_consistent: bool
    history_complete: bool = False
    history_covered_from: datetime | None = None
    history_covered_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class LotPlan:
    """Покупка лота произвольного размера.

    Attributes:
        source_index: Позиция строки в исходном снимке.
        amount: Объём всей покупки.
        end_time: Время окончания лота.
        buyout_total: Полная цена покупки или None без правил цен.
    """

    source_index: int
    amount: int
    end_time: datetime
    buyout_total: int | None


@dataclass(frozen=True, slots=True)
class LotAnalysis:
    """Решение по отдельной покупке и доступные показатели.

    Attributes:
        plan: Объём покупки и её полная стоимость.
        status: Основной статус лота.
        reasons: Все ограничения, применившиеся к лоту.
        exit_price_per_pack: Валовая цена продажи стандартной партии.
        net_revenue_per_pack: Чистая выручка стандартной партии.
        competing_packs: Партии конкурентов по цене выхода или ниже.
        gross_revenue: Валовая выручка, отнесённая к покупке.
        commission: Доля комиссии, отнесённая к покупке.
        listing_cost: Доля расходов на выставление.
        net_revenue: Чистая расчётная выручка на купленный объём.
        profit: Выручка за вычетом полного выкупа.
        roi: Точная доля прибыли от полной стоимости выкупа.
        max_buyout: Верхний порог покупки всего лота.
    """

    plan: LotPlan
    status: AnalysisStatus
    reasons: tuple[AnalysisStatus, ...] = ()
    exit_price_per_pack: int | None = None
    net_revenue_per_pack: int | None = None
    competing_packs: int | None = None
    gross_revenue: int | None = None
    commission: int | None = None
    listing_cost: int | None = None
    net_revenue: int | None = None
    profit: int | None = None
    roi: Fraction | None = None
    max_buyout: int | None = None


@dataclass(frozen=True, slots=True)
class AuctionAnalysis:
    """Оценка предмета и отдельных покупок в одном снимке.

    Attributes:
        item_id: Точный ID оцениваемого предмета.
        evaluated_at: Момент расчёта в UTC.
        sell_quantity: Размер стандартной партии из каталога.
        coverage: Проверенные признаки покрытия данных.
        sample_count: Число продаж в выбранном или проверенном окне.
        q25_per_pack: Нижний квартиль полной цены стандартной партии.
        q25_quality: Степень подтверждения ценовой выборки.
        sample_oldest_at: Время старейшей продажи в выборке.
        sample_newest_at: Время самой новой продажи в выборке.
        turnover_24h: Число продаж стандартных партий за сутки.
        commission_estimated: Признак оценочного округления комиссии.
        reasons: Ограничения, общие для всего предмета.
        lots: Кандидаты по прибыли, затем остальные лоты.
        price_window: Длительность выбранного окна Q25.
        price_change: Доля изменения Q25 к предыдущему окну.
        active_competing_packs: Число стандартных партий в снимке.
    """

    item_id: str
    evaluated_at: datetime
    sell_quantity: int | None
    coverage: AnalysisCoverage
    sample_count: int | None
    q25_per_pack: int | None
    sample_oldest_at: datetime | None
    sample_newest_at: datetime | None
    turnover_24h: int | None
    commission_estimated: bool | None
    reasons: tuple[AnalysisStatus, ...]
    lots: tuple[LotAnalysis, ...]
    price_window: timedelta | None = None
    price_change: Fraction | None = None
    active_competing_packs: int | None = None
    q25_quality: Q25Quality = Q25Quality.UNAVAILABLE
