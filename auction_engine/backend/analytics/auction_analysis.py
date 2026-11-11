"""Расчёт перепродажи одного предмета по снимку аукциона."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction

from common.dataclasses import CatalogItem
from common.enums.auction_analysis import AnalysisStatus, Q25Quality
from common.models.analysis_rules import AnalysisRules
from common.models.auction import Lot, Sale
from common.models.auction_strategy import StrategySettings

from .auction_pricing import evaluate_resale, total_price
from .types import AnalysisCoverage, AuctionAnalysis, LotAnalysis, LotPlan

TURNOVER_WINDOW = timedelta(hours=24)
PRICE_WINDOWS = (
    timedelta(hours=3), TURNOVER_WINDOW, timedelta(days=7), timedelta(days=14)
)
_SHARP_PRICE_DROP = Fraction(-1, 5)


def _comparable_sales(
    history: Sequence[Sale], sell_quantity: int, evaluated_at: datetime
) -> list[Sale]:
    """Отобрать продажи стандартных партий в пределах ценовых окон.

    Args:
        history: История выбранного предмета.
        sell_quantity: Размер стандартной партии.
        evaluated_at: Время оценки в UTC.

    Returns:
        Продажи от новых к старым с исходной кратностью строк.
    """
    cutoff = evaluated_at - 2 * PRICE_WINDOWS[-1]
    return sorted(
        (
            sale
            for sale in history
            if sale.amount == sell_quantity
            and sale.price > 0
            and cutoff <= sale.time <= evaluated_at
        ),
        key=lambda sale: sale.time,
        reverse=True,
    )


def _sample_q25(sample: Sequence[Sale]) -> int:
    """Получить Q25 цены стандартной партии методом ближайшего ранга.

    Args:
        sample: Непустая выборка сопоставимых продаж.

    Returns:
        Полная цена стандартной партии на нижнем квартиле.
    """
    prices = sorted(sale.price for sale in sample)
    rank = (len(prices) + 3) // 4
    return prices[rank - 1]


def _covered_from(
    history: Sequence[Sale],
    evaluated_at: datetime,
    coverage: AnalysisCoverage,
) -> datetime | None:
    """Определить включённую границу проверенной истории.

    Args:
        history: Исходные строки истории выбранного предмета.
        evaluated_at: Конец окна анализа.
        coverage: Подтверждённые признаки покрытия.

    Returns:
        Начало покрытия или None, если оно не подтверждено.
    """
    if not coverage.history_prefix_consistent:
        return None
    if coverage.history_covered_from is not None:
        return coverage.history_covered_from
    if coverage.history_complete:
        return evaluated_at - 2 * PRICE_WINDOWS[-1]
    oldest = min((sale.time for sale in history), default=evaluated_at)
    # В момент старейшей строки могут быть непрочитанные продажи.
    start = (
        oldest + timedelta(microseconds=1) if oldest < evaluated_at else None
    )
    if coverage.history_24h_complete:
        day_start = evaluated_at - TURNOVER_WINDOW
        return min(start or day_start, day_start)
    return start


def _select_sample(
    comparable: Sequence[Sale],
    evaluated_at: datetime,
    covered_from: datetime | None,
    min_sales: int,
    *,
    require_coverage: bool = True,
) -> tuple[timedelta | None, list[Sale] | None]:
    """Найти кратчайшее допустимое окно с достаточной выборкой.

    Args:
        comparable: Подходящие продажи в пределах двух максимальных окон.
        evaluated_at: Конец окна анализа.
        covered_from: Начало подтверждённого покрытия или None.
        min_sales: Минимальное число продаж в окне.
        require_coverage: Требовать подтверждённое покрытие окна.

    Returns:
        Выбранное окно и продажи либо последняя допустимая выборка.
    """
    sample = None
    for window in PRICE_WINDOWS:
        cutoff = evaluated_at - window
        if require_coverage and (
            covered_from is None or cutoff < covered_from
        ):
            break
        sample = [sale for sale in comparable if sale.time >= cutoff]
        if len(sample) >= min_sales:
            return window, sample
    return None, sample


def _price_change(
    comparable: Sequence[Sale],
    evaluated_at: datetime,
    window: timedelta,
    covered_from: datetime,
    *,
    q25_per_pack: int,
    min_sales: int,
) -> Fraction | None:
    """Сравнить Q25 с предыдущим окном такой же длительности.

    Args:
        comparable: Подходящие продажи в обоих окнах.
        evaluated_at: Конец текущего окна.
        window: Длительность выбранного окна.
        covered_from: Включённая граница подтверждённой истории.
        q25_per_pack: Нижний квартиль цены партии в текущем окне.
        min_sales: Минимальное число продаж для сравнения.

    Returns:
        Точная доля изменения либо None без проверенной выборки.
    """
    boundary = evaluated_at - window
    start = boundary - window
    if start < covered_from:
        return None
    previous = [
        sale for sale in comparable if start <= sale.time < boundary
    ]
    if len(previous) < min_sales:
        return None
    previous_q25 = _sample_q25(previous)
    return Fraction(q25_per_pack - previous_q25, previous_q25)


def _restrictions(
    variant_verified: bool,
    sell_quantity: int | None,
    price_window: timedelta | None,
    history_available: bool,
    rules: AnalysisRules | None,
) -> tuple[AnalysisStatus, ...]:
    """Собрать все ограничения, общие для предмета.

    Args:
        variant_verified: Признак подтверждённой однородности.
        sell_quantity: Размер стандартной партии из каталога.
        price_window: Выбранное окно Q25 или None без оценки.
        history_available: Есть продажи или подтверждённое пустое окно.
        rules: Правила цен и расходов для текущего режима.

    Returns:
        Причины в порядке выбора основного статуса.
    """
    reasons = []
    if sell_quantity is None:
        reasons.append(AnalysisStatus.SELL_QUANTITY_MISSING)
    if not variant_verified:
        reasons.append(AnalysisStatus.VARIANT_UNVERIFIED)
    if rules is None:
        reasons.append(AnalysisStatus.PRICE_RULES_UNVERIFIED)
    if not history_available:
        reasons.append(AnalysisStatus.HISTORY_UNAVAILABLE)
    elif (
        variant_verified
        and sell_quantity is not None
        and price_window is None
    ):
        reasons.append(AnalysisStatus.INSUFFICIENT_HISTORY)
    return tuple(reasons)


def _plan_lots(
    lots: Sequence[Lot],
    item_id: str,
    evaluated_at: datetime,
    rules: AnalysisRules | None,
) -> list[LotPlan]:
    """Подготовить планы покупки по действующим лотам.

    Args:
        lots: Строки снимка активных лотов.
        item_id: Точный ID выбранного предмета.
        evaluated_at: Время оценки в UTC.
        rules: Правила цен и расходов или None.

    Returns:
        Планы с позициями исходных строк и полными ценами.
    """
    plans = []
    for index, lot in enumerate(lots):
        buyout = lot.buyout_price
        if (
            lot.item_id != item_id
            or lot.amount <= 0
            or buyout is None
            or buyout <= 0
            or lot.end_time <= evaluated_at
        ):
            continue
        buyout_total = (
            total_price(buyout, lot.amount, rules.buyout_price_basis)
            if rules is not None
            else None
        )
        plans.append(
            LotPlan(
                source_index=index,
                amount=lot.amount,
                end_time=lot.end_time,
                buyout_total=buyout_total,
            )
        )
    return plans


def _evaluate_plans(
    plans: Sequence[LotPlan],
    reasons: tuple[AnalysisStatus, ...],
    *,
    rules: AnalysisRules | None,
    sell_quantity: int | None,
    q25_per_pack: int | None,
    turnover_24h: int | None,
    min_roi_bps: int,
    sharp_price_drop: bool,
) -> tuple[LotAnalysis, ...]:
    """Оценить покупки отдельно и упорядочить кандидатов по прибыли.

    Args:
        plans: Планы покупки по полученным строкам снимка.
        reasons: Ограничения, общие для предмета.
        rules: Правила цен и расходов для текущего режима.
        sell_quantity: Размер стандартной партии из каталога.
        q25_per_pack: Верхний ориентир цены стандартной партии.
        turnover_24h: Число продаж стандартных партий за сутки.
        min_roi_bps: Пользовательский порог ROI в базисных пунктах.
        sharp_price_drop: Подтверждено существенное снижение Q25.

    Returns:
        Кандидаты по убыванию прибыли, затем остальные оценки.
    """
    competitor_levels = sorted(
        (plan.buyout_total, plan.source_index)
        for plan in plans
        if plan.amount == sell_quantity and plan.buyout_total is not None
    )
    candidates = []
    others = []
    for plan in plans:
        if reasons:
            result = LotAnalysis(plan, reasons[0], reasons=reasons)
        else:
            assert rules is not None and sell_quantity is not None
            assert q25_per_pack is not None and turnover_24h is not None
            result = evaluate_resale(
                plan,
                rules,
                sell_quantity=sell_quantity,
                q25_per_pack=q25_per_pack,
                turnover_24h=turnover_24h,
                competitor_levels=competitor_levels,
                min_roi_bps=min_roi_bps,
            )
        if sharp_price_drop:
            risk = AnalysisStatus.SHARP_PRICE_DROP
            result = replace(
                result,
                status=(
                    risk if result.status is AnalysisStatus.CANDIDATE
                    else result.status
                ),
                reasons=(*result.reasons, risk),
            )
        if result.status is AnalysisStatus.CANDIDATE:
            candidates.append(result)
        else:
            others.append(result)

    candidates.sort(key=lambda result: result.profit or 0, reverse=True)
    return (*candidates, *others)


def analyze_auction(
    item: CatalogItem,
    lots: Sequence[Lot],
    history: Sequence[Sale],
    *,
    evaluated_at: datetime,
    coverage: AnalysisCoverage,
    rules: AnalysisRules | None = None,
    strategy: StrategySettings | None = None,
) -> AuctionAnalysis:
    """Рассчитать оценки перепродажи на переданном снимке.

    Args:
        item: Предмет из каталога с признаком однородности.
        lots: Лоты выбранного предмета.
        history: Продажи выбранного предмета.
        evaluated_at: Фиксированный момент расчёта с часовым поясом.
        coverage: Признаки покрытия, проверенные при сборе страниц.
        rules: Правила текущего режима или None до сверки.
        strategy: Параметры стратегии или None для значений по умолчанию.

    Returns:
        Диагностика предмета и отдельные сценарии покупки.

    Raises:
        ValueError: Недопустимый размер партии или время без пояса.
    """
    if evaluated_at.utcoffset() is None:
        raise ValueError("Укажите время оценки с часовым поясом.")
    evaluated_at = evaluated_at.astimezone(UTC)
    history_at = coverage.history_covered_until or evaluated_at
    strategy = strategy if strategy is not None else StrategySettings()

    sell_quantity = item.sell_quantity
    if sell_quantity is not None and (
        type(sell_quantity) is not int or sell_quantity <= 0
    ):
        raise ValueError("sell_quantity должен быть положительным целым.")

    variant_verified = item.homogeneous is True
    comparable = []
    covered_from = None
    if variant_verified and sell_quantity is not None:
        comparable = _comparable_sales(history, sell_quantity, history_at)
        covered_from = _covered_from(history, history_at, coverage)
    price_window, sample = _select_sample(
        comparable, history_at, covered_from,
        strategy.min_sales,
    )
    q25_per_pack = None
    q25_quality = Q25Quality.UNAVAILABLE
    price_change = None
    if price_window is not None:
        assert sample and covered_from is not None
        q25_per_pack = _sample_q25(sample)
        q25_quality = Q25Quality.VERIFIED
        price_change = _price_change(
            comparable, history_at, price_window, covered_from,
            q25_per_pack=q25_per_pack, min_sales=strategy.min_sales,
        )
    sharp_price_drop = (
        price_change is not None and price_change <= _SHARP_PRICE_DROP
    )

    history_available = bool(history) or (
        coverage.history_prefix_consistent and coverage.history_24h_complete
    )
    turnover_24h = None
    if variant_verified and sell_quantity is not None and history_available:
        cutoff = history_at - TURNOVER_WINDOW
        turnover_24h = sum(sale.time >= cutoff for sale in comparable)

    complete_price_history = (
        covered_from is not None
        and covered_from <= history_at - PRICE_WINDOWS[-1]
    )
    if comparable and price_window is None and not complete_price_history:
        observed_window, observed_sales = _select_sample(
            comparable, history_at, None, strategy.min_sales,
            require_coverage=False,
        )
        if observed_window is not None:
            assert observed_sales
            price_window, sample = observed_window, observed_sales
            q25_per_pack = _sample_q25(sample)
            q25_quality = Q25Quality.PRELIMINARY

    reasons = _restrictions(
        variant_verified, sell_quantity, price_window, history_available,
        rules,
    )
    plans = _plan_lots(lots, item.item_id, evaluated_at, rules)
    return AuctionAnalysis(
        item_id=item.item_id,
        evaluated_at=evaluated_at,
        sell_quantity=sell_quantity,
        coverage=coverage,
        sample_count=len(sample) if sample is not None else None,
        q25_per_pack=q25_per_pack,
        q25_quality=q25_quality,
        sample_oldest_at=sample[-1].time if sample else None,
        sample_newest_at=sample[0].time if sample else None,
        turnover_24h=turnover_24h,
        commission_estimated=(
            rules.commission_rounding is None if rules is not None else None
        ),
        reasons=(
            (*reasons, AnalysisStatus.SHARP_PRICE_DROP)
            if sharp_price_drop else reasons
        ),
        lots=_evaluate_plans(
            plans,
            reasons,
            rules=rules,
            sell_quantity=sell_quantity,
            q25_per_pack=q25_per_pack,
            turnover_24h=turnover_24h,
            min_roi_bps=strategy.min_roi_bps,
            sharp_price_drop=sharp_price_drop,
        ),
        price_window=price_window,
        price_change=price_change,
        active_competing_packs=(
            sum(plan.amount == sell_quantity for plan in plans)
            if variant_verified and sell_quantity is not None else None
        ),
    )
