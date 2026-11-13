"""Подготовка сохранённой оценки существующей моделью аукциона."""

import logging
from datetime import UTC, datetime, timedelta
from time import perf_counter

from common.database.clickhouse.repository import AuctionHistoryRepository
from common.database.clickhouse.scanner_repository import (
    MAX_TEMPORARY_PROFILES,
)
from common.dataclasses import CatalogItem
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import HistoryKey
from common.errors.auction_engine import EngineCapacityError
from common.models.analysis_result import AnalysisResult
from common.models.auction import Lot
from common.models.auction_engine import SnapshotMetadata
from common.models.auction_strategy import StrategySettings
from common.engine_analysis import analysis_key, history_summary
from common.settings.analysis import load_analysis_rules

from .analysis import evaluate_lots
from .engine_settings import AuctionEngineSettings
from .snapshot_writer import SnapshotWriter

_LOGGER = logging.getLogger(__name__)


async def evaluate(
    history: AuctionHistoryRepository, snapshots: SnapshotWriter,
    settings: AuctionEngineSettings, key: HistoryKey, item: CatalogItem,
    meta: SnapshotMetadata, scan: PageScan[Lot], strategy: StrategySettings,
) -> AnalysisResult:
    """Рассчитать и записать профиль для данной версии лотов.

    Args:
        history: Репозиторий завершённых продаж.
        snapshots: Репозиторий снимков и аналитики.
        settings: Срок хранения и период обновления.
        key: Предмет, регион и режим.
        item: Параметры стандартной партии.
        meta: Версия исходного снимка.
        scan: Сохранённые или только что собранные лоты.
        strategy: Минимальная выборка и порог ROI.

    Returns:
        Готовая оценка для публикации.
    """
    now = datetime.now(UTC)
    started = perf_counter()
    stored = await history.read_history(
        key, now - timedelta(days=settings.ttl_days), now,
    )
    loaded = perf_counter()
    profile = analysis_key(key, item, strategy, stored.checkpoint)
    existing = await snapshots.analysis(key, meta, profile)
    if existing is not None:
        return existing
    default = StrategySettings()
    temporary = (
        strategy.min_sales != default.min_sales
        or strategy.min_roi_bps != default.min_roi_bps
    )
    if temporary and (
        await snapshots.temporary_profiles(key, meta) >= MAX_TEMPORARY_PROFILES
    ):
        raise EngineCapacityError("Достигнут предел временных профилей.")
    result = evaluate_lots(
        key, strategy, item, scan, stored, load_analysis_rules(key.mode),
        history_summary(stored.checkpoint, settings),
    )
    calculated = perf_counter()
    result.received_at = meta.received_at
    result.analysis_version = await snapshots.store_analysis(
        key, meta, profile, result, temporary=temporary,
    )
    _LOGGER.info(
        "Evaluated snapshot=%s item=%s region=%s mode=%s sales=%d lots=%d "
        "history_ms=%.1f analysis_ms=%.1f store_ms=%.1f",
        meta.snapshot_id, key.item_id, key.region, key.mode,
        len(stored.rows), len(result.lots), (loaded - started) * 1000,
        (calculated - loaded) * 1000, (perf_counter() - calculated) * 1000,
    )
    return result
