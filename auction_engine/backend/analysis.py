"""Подготовка оценки по уже полученным лотам и истории."""

from datetime import UTC, datetime, timedelta

from auction_engine.backend.analytics.auction_analysis import (
    PRICE_WINDOWS,
    TURNOVER_WINDOW,
    analyze_auction,
)
from common.dataclasses import CatalogItem
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import HistoryKey, StoredHistory
from common.enums.auction_analysis import AnalysisStatus
from common.models.analysis_rules import AnalysisRules
from common.models.auction import Lot
from common.models.auction_strategy import StrategySettings
from common.models.analysis_result import (
    AnalysisResult,
    EvaluatedLot,
    EvaluationCoverage,
    EvaluationModel,
)
from common.models.history_status import HistoryStatus

from .analytics.types import AnalysisCoverage, AuctionAnalysis, LotAnalysis


def _active_for_item(lot: Lot, item: CatalogItem, at: datetime) -> bool:
    """Проверить принадлежность и срок действия лота.

    Args:
        lot: Строка снимка EXBO.
        item: Предмет локального каталога.
        at: Время оценки.

    Returns:
        Признак действующего лота с положительным количеством.
    """
    return (
        lot.item_id == item.item_id and lot.amount > 0 and lot.end_time > at
    )


def _competition_ratio(
    competing_packs: int | None, turnover_24h: int | None
) -> float | None:
    """Сопоставить активное предложение с полученными продажами.

    Args:
        competing_packs: Число выставленных партий без покупки.
        turnover_24h: Число продаж стандартных партий за сутки.

    Returns:
        Отношение предложения к обороту или None без делителя.
    """
    if competing_packs is None or not turnover_24h:
        return None
    return competing_packs / turnover_24h


def _lot_result(
    result: LotAnalysis,
    analysis: AuctionAnalysis,
) -> EvaluatedLot:
    """Собрать сохраняемые показатели лота.

    Args:
        result: Сценарий покупки из аналитического ядра.
        analysis: Сведения о полученных продажах и конкурентах.

    Returns:
        Значения лота с явными единицами и причинами ограничений.
    """
    plan = result.plan
    competing = analysis.active_competing_packs
    if competing is not None and plan.amount == analysis.sell_quantity:
        competing -= 1
    return EvaluatedLot(
        id=plan.source_index + 1,
        amount=plan.amount,
        end_time=plan.end_time,
        buyout_total=plan.buyout_total,
        exit_price_per_pack=result.exit_price_per_pack,
        net_revenue_per_pack=result.net_revenue_per_pack,
        gross_revenue_total=result.gross_revenue,
        commission_total=result.commission,
        listing_cost_total=result.listing_cost,
        net_revenue_total=result.net_revenue,
        profit_total=result.profit,
        max_buyout=result.max_buyout,
        roi_percent=(
            float(result.roi * 100) if result.roi is not None else None
        ),
        active_competing_packs=competing,
        competition_to_turnover=_competition_ratio(
            competing, analysis.turnover_24h
        ),
        status=result.status,
        reasons=result.reasons,
    )


def _unassessed_lot(
    index: int,
    lot: Lot,
    analysis: AuctionAnalysis,
) -> EvaluatedLot:
    """Представить активный лот без действующей цены выкупа.

    Args:
        index: Позиция строки в снимке.
        lot: Активный лот выбранного предмета.
        analysis: Показатели и общие ограничения анализа предмета.

    Returns:
        Наблюдаемые данные лота и причины отсутствия оценки.
    """
    status = AnalysisStatus.BUYOUT_UNAVAILABLE
    return EvaluatedLot(
        id=index + 1,
        amount=lot.amount,
        end_time=lot.end_time,
        active_competing_packs=analysis.active_competing_packs,
        competition_to_turnover=_competition_ratio(
            analysis.active_competing_packs, analysis.turnover_24h
        ),
        status=status,
        reasons=(status, *analysis.reasons),
    )


def evaluate_lots(
    key: HistoryKey,
    strategy: StrategySettings,
    item: CatalogItem,
    lots: PageScan[Lot] | None,
    history: StoredHistory | None,
    rules: AnalysisRules | None,
    sync: HistoryStatus | None,
) -> AnalysisResult:
    """Проверить покрытие и собрать оценку и историю в один ответ.

    Args:
        key: Предмет, регион и режим.
        strategy: Настройки расчёта.
        item: Предмет каталога.
        lots: Снимок активных лотов или None при сбое.
        history: Накопленные продажи или None при сбое ClickHouse.
        rules: Подтверждённые правила текущего режима или None.
        sync: Границы и состояние фоновой синхронизации.

    Returns:
        Результаты существующего анализа и наблюдаемые данные.
    """
    evaluated_at = datetime.now(UTC)
    lot_rows = lots.rows if lots is not None else ()
    history_rows = history.rows if history is not None else ()
    start = sync.covered_from if sync is not None else None
    end = sync.covered_until if sync is not None else None
    consistent = start is not None and end is not None
    coverage = AnalysisCoverage(
        lots_complete=lots is not None and lots.complete and all(
            lot.item_id == item.item_id and lot.amount > 0 for lot in lot_rows
        ),
        history_24h_complete=(
            start is not None and end is not None
            and start <= end - TURNOVER_WINDOW
        ),
        history_prefix_consistent=consistent,
        history_covered_from=start,
        history_covered_until=end,
    )
    analysis = analyze_auction(
        item, lot_rows, history_rows, evaluated_at=evaluated_at,
        coverage=coverage, rules=rules, strategy=strategy,
    )
    result_lots = [_lot_result(row, analysis) for row in analysis.lots]
    analyzed_indexes = {row.plan.source_index for row in analysis.lots}
    result_lots.extend(
        _unassessed_lot(index, lot, analysis)
        for index, lot in enumerate(lot_rows)
        if index not in analyzed_indexes
        and _active_for_item(lot, item, evaluated_at)
    )
    return AnalysisResult(
        item_id=item.item_id,
        name_ru=item.label,
        homogeneous=item.homogeneous,
        sell_quantity=item.sell_quantity,
        mode=key.mode,
        region=key.region,
        received_at=max((
            at for at in (lots.received_at if lots else None, end)
            if at is not None
        ), default=evaluated_at),
        evaluated_at=evaluated_at,
        sample_count=analysis.sample_count if history is not None else None,
        q25_per_pack=analysis.q25_per_pack,
        q25_quality=analysis.q25_quality,
        turnover_24h=analysis.turnover_24h,
        price_change_percent=(
            float(analysis.price_change * 100)
            if analysis.price_change is not None else None
        ),
        coverage=EvaluationCoverage(
            lots_complete=coverage.lots_complete,
            history_24h_complete=coverage.history_24h_complete,
            history_prefix_consistent=consistent,
            history_window_complete=False,
            lots_loaded=len(lot_rows),
            lots_reported_total=(
                lots.reported_total
                if lots is not None and lots.consistent else None
            ),
            history_loaded=len(history_rows),
        ),
        model=EvaluationModel(
            price_window_days=PRICE_WINDOWS[-1].days,
            sample_size=strategy.min_sales,
            min_sales=strategy.min_sales,
            price_window_hours=(
                analysis.price_window // timedelta(hours=1)
                if analysis.price_window is not None else None
            ),
            sale_commission_bps=(
                rules.sale_commission_bps if rules is not None else None
            ),
            listing_cost_per_pack=(
                rules.listing_cost_per_pack if rules is not None else None
            ),
            buyout_price_basis=(
                rules.buyout_price_basis if rules is not None else None
            ),
            min_roi_bps=strategy.min_roi_bps,
            commission_estimated=(
                rules.commission_rounding is None if rules is not None
                else None
            ),
        ),
        reasons=analysis.reasons,
        lots=result_lots,
    )
