"""Подготовка продаж для графиков веб-интерфейса."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from common.dataclasses import CatalogItem
from common.enums.auction import HistoryDays
from common.models.auction import Sale
from common.models.history_status import HistoryStatus

from .models.responses.auction_analysis import (
    AuctionHistoryResponse,
    AuctionSaleResponse,
)


def build_history(
    item: CatalogItem,
    sales: Sequence[Sale],
    days: HistoryDays,
    evaluated_at: datetime,
    *,
    sync: HistoryStatus | None,
) -> AuctionHistoryResponse:
    """Ограничить историю для графиков сопоставимыми продажами.

    Args:
        item: Предмет с настройками пачки и однородности.
        sales: Завершённые продажи из ClickHouse.
        days: Окно графика в сутках.
        evaluated_at: Конец окна.
        sync: Границы и состояние фоновой синхронизации.

    Returns:
        Продажи нужных пачек и начало подтверждённого покрытия.
    """
    start = sync.covered_from if sync is not None else None
    end = sync.covered_until if sync is not None else None
    history_at = end or evaluated_at
    cutoff = history_at - timedelta(days=days)
    points = []
    quantity = item.sell_quantity
    if quantity is not None and item.homogeneous:
        for sale in sales:
            if (
                sale.amount == quantity and sale.price > 0
                and cutoff <= sale.time <= history_at
            ):
                points.append(
                    AuctionSaleResponse(
                        time=sale.time,
                        amount=sale.amount,
                        price_per_pack=sale.price,
                    )
                )
    return AuctionHistoryResponse(
        days=days, covered_from=max(start, cutoff) if start else None,
        covered_until=end, sales=points, sync=sync,
    )
