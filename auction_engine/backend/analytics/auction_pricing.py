"""Целочисленный расчёт цены выхода, комиссии и выручки."""

from bisect import bisect_right
from collections.abc import Sequence
from fractions import Fraction
from math import ceil, floor

from .types import LotAnalysis, LotPlan
from common.enums.auction_analysis import (
    AnalysisStatus,
    CommissionRounding,
    PriceBasis,
)
from common.models.analysis_rules import BASIS_POINTS, AnalysisRules


def total_price(price: int, amount: int, basis: PriceBasis) -> int:
    """Перевести цену EXBO в полную стоимость.

    Args:
        price: Исходное значение цены.
        amount: Количество предметов в строке.
        basis: Единица исходной цены.

    Returns:
        Полная стоимость строки в игровых рублях.
    """
    return price * amount if basis is PriceBasis.PER_UNIT else price


def _highest_exit(
    q25_per_pack: int, prices: Sequence[int], capacity: int
) -> int | None:
    """Выбрать максимальную цену партии по глубине конкуренции.

    Args:
        q25_per_pack: Верхний ориентир цены стандартной партии.
        prices: Полные цены партий конкурентов по возрастанию.
        capacity: Сколько конкурирующих партий помещается в оборот.

    Returns:
        Допустимая положительная цена или None.
    """
    if capacity < 0:
        return None
    price = q25_per_pack
    if capacity < len(prices):
        # Следующая партия конкурента превысит доступный оборот.
        price = min(price, prices[capacity] - 1)
    return price if price > 0 else None


def _commission(price: int, rules: AnalysisRules) -> int:
    """Рассчитать удержание с продажи стандартной партии.

    Args:
        price: Валовая цена перепродажи партии.
        rules: Ставка и, если известно, правило округления.

    Returns:
        Целое удержание с партии.
    """
    numerator = price * rules.sale_commission_bps
    if rules.commission_rounding is CommissionRounding.FLOOR:
        return numerator // BASIS_POINTS
    if rules.commission_rounding is CommissionRounding.HALF_UP:
        return (numerator + BASIS_POINTS // 2) // BASIS_POINTS
    return (numerator + BASIS_POINTS - 1) // BASIS_POINTS


def evaluate_resale(
    plan: LotPlan,
    rules: AnalysisRules,
    *,
    sell_quantity: int,
    q25_per_pack: int,
    turnover_24h: int,
    competitor_levels: Sequence[tuple[int, int]],
    min_roi_bps: int,
) -> LotAnalysis:
    """Оценить долю стандартных партий на весь объём покупки.

    Args:
        plan: Покупка с положительным объёмом и полной ценой.
        rules: Правила цен и затрат.
        sell_quantity: Размер стандартной партии.
        q25_per_pack: Верхний ориентир валовой цены партии.
        turnover_24h: Число продаж стандартных партий за сутки.
        competitor_levels: Отсортированные цены партий и позиции лотов.
        min_roi_bps: Пользовательский порог ROI в базисных пунктах.

    Returns:
        Оценка со статусом ликвидности, прибыли и порога покупки.
    """
    buyout = plan.buyout_total
    assert buyout is not None and buyout > 0
    prices = [
        price
        for price, index in competitor_levels
        if index != plan.source_index
    ]
    share = Fraction(plan.amount, sell_quantity)
    required_packs = ceil(share)
    exit_price = _highest_exit(
        q25_per_pack, prices, turnover_24h - required_packs
    )
    if exit_price is None:
        status = AnalysisStatus.LOW_LIQUIDITY
        return LotAnalysis(plan, status, reasons=(status,))

    after_commission = exit_price - _commission(exit_price, rules)
    net_pack = after_commission - rules.listing_cost_per_pack
    gross = floor(exit_price * share)
    after_commission_total = floor(after_commission * share)
    revenue = floor(net_pack * share)
    # Разности округлённых сумм сохраняют баланс выручки и расходов.
    commission = gross - after_commission_total
    listing = after_commission_total - revenue
    profit = revenue - buyout
    max_at_roi = revenue * BASIS_POINTS // (BASIS_POINTS + min_roi_bps)
    max_buyout = max(0, min(max_at_roi, revenue - 1))
    if profit > 0 and profit * BASIS_POINTS >= buyout * min_roi_bps:
        status = AnalysisStatus.CANDIDATE
    else:
        status = AnalysisStatus.BELOW_ROI

    return LotAnalysis(
        plan=plan,
        status=status,
        reasons=() if status is AnalysisStatus.CANDIDATE else (status,),
        exit_price_per_pack=exit_price,
        net_revenue_per_pack=net_pack,
        competing_packs=bisect_right(prices, exit_price),
        gross_revenue=gross,
        commission=commission,
        listing_cost=listing,
        net_revenue=revenue,
        profit=profit,
        roi=Fraction(profit, buyout),
        max_buyout=max_buyout,
    )
