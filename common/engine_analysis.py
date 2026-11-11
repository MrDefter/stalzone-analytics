"""Общие параметры расчёта и представление покрытия из ClickHouse."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256

from common.dataclasses import CatalogItem
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.enums.auction_history import HistorySyncStatus
from common.models.auction_strategy import StrategySettings
from common.models.history_status import HistoryStatus
from common.settings.analysis import load_analysis_rules
from common.settings.auction_storage import AuctionStorageSettings


def analysis_key(
    key: HistoryKey, item: CatalogItem, strategy: StrategySettings,
    point: HistoryCheckpoint | None,
) -> str:
    """Связать профиль с версией правил и входной истории.

    Args:
        key: Предмет, регион и режим.
        item: Размер партии и однородность предмета.
        strategy: Пользовательские параметры модели.
        point: Границы накопленных сделок.

    Returns:
        Стабильный ключ расчёта без параметров графика.
    """
    rules = load_analysis_rules(key.mode)
    inputs = (
        "v1", strategy.min_sales, strategy.min_roi_bps, item.sell_quantity,
        item.homogeneous, rules.model_dump_json() if rules else None,
        point.covered_from if point else None,
        point.covered_until if point else None,
        point.data_revision if point else None,
    )
    return sha256(repr(inputs).encode()).hexdigest()


def history_summary(
    point: HistoryCheckpoint | None, settings: AuctionStorageSettings,
) -> HistoryStatus:
    """Восстановить состояние истории по сохранённой контрольной точке.

    Args:
        point: Последняя сохранённая запись сборщика.
        settings: Срок хранения и интервал обновления.

    Returns:
        Границы покрытия и возраст подтверждённых данных.
    """
    if point is None:
        return HistoryStatus(status=HistorySyncStatus.PENDING)
    now = datetime.now(UTC)
    start = point.covered_from
    end = point.covered_until
    if start is not None:
        start = max(start, now - timedelta(days=settings.ttl_days))
    if end is not None and start is not None and start > end:
        start = end = None
    error = (
        point.last_error if point.status is HistorySyncStatus.ERROR else None
    )
    return HistoryStatus(
        status=point.status, operation=point.operation,
        last_success_at=point.last_success_at, covered_from=start,
        covered_until=end, initial_complete=point.initial_complete,
        backfill_complete=point.backfill_complete,
        stale=point.covered_until is not None and (
            now - point.covered_until >= timedelta(
                seconds=settings.sync_interval_seconds,
            ) or error is not None
        ),
        error=error,
    )
