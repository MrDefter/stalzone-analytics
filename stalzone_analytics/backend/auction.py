"""Чтение готового анализа и отправка команд аукционному ядру."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from common.database.clickhouse.auction_storage import AuctionStorage
from common.database.clickhouse.repository import AuctionHistoryRepository
from common.database.clickhouse.scanner_repository import (
    MAX_TEMPORARY_PROFILES,
    ScannerRepository,
)
from common.dataclasses import CatalogItem
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.errors import ItemNotFoundError
from common.errors.auction_engine import EngineCapacityError
from common.models.auction_engine import (
    MonitoringUpdate,
    ScannerCommand,
    ScannerSettings,
    ScannerState,
    SnapshotMetadata,
)
from common.models.auction_strategy import StrategySettings
from common.settings import ItemCatalogSettings
from common.settings.auction_storage import AuctionStorageSettings
from common.engine_analysis import (
    analysis_key,
    history_summary,
)

from .history import build_history
from .models.requests.auction_analysis import AuctionAnalysisQuery
from .models.responses.auction_analysis import (
    AuctionAnalysisResponse,
    AuctionVersionResponse,
)

_COMMAND_LOCK = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class _Selection:
    """Опубликованный снимок и зафиксированная ревизия истории."""

    key: HistoryKey
    item: CatalogItem
    meta: SnapshotMetadata
    point: HistoryCheckpoint | None
    profile: str
    age: int
    stale: bool


def catalog_item(item_id: str) -> CatalogItem:
    """Найти предмет по точному ключу.

    Args:
        item_id: Идентификатор из запроса.

    Returns:
        Настройки предмета.
    """
    for item in ItemCatalogSettings.load(allow_empty=True).items:
        if item.item_id == item_id:
            return item
    raise ItemNotFoundError("Предмет не найден в каталоге.")


async def enqueue(
    repository: ScannerRepository, key: HistoryKey,
    query: AuctionAnalysisQuery, operation: str, identity: str = "",
    *, manual: bool = False, settings_update: MonitoringUpdate | None = None,
) -> ScannerCommand:
    """Поставить команду с объединением повторов и пределом очереди.

    Args:
        repository: Хранилище управляющих команд.
        key: Предмет, регион и режим.
        query: Параметры требуемой стратегии.
        operation: Сканирование или пересчёт сохранённого снимка.
        identity: Версия снимка и входных данных для профиля.
        manual: Пользователь запросил новый проход после предыдущего.
        settings_update: Изменения параметров фонового мониторинга.

    Returns:
        Принятая или уже ожидающая команда.
    """
    async with _COMMAND_LOCK:
        pending = [
            command for command_key, command in await repository.commands()
            if command_key == key and command.operation == operation
        ]
        changes = settings_update.model_dump() if settings_update else {}
        for command in pending:
            if settings_update is not None:
                if command.model_dump(include=set(changes)) == changes:
                    return command
            elif operation == "scan" or (
                command.strategy.min_sales == query.min_sales
                and command.strategy.min_roi_bps == query.min_roi_bps
            ):
                return command
        now = datetime.now(UTC)
        window = now if manual else int(now.timestamp()) // 15
        signature = (key, operation, identity, changes, window)
        command = ScannerCommand(
            request_id=sha256(repr(signature).encode()).hexdigest(),
            operation=operation, created_at=now, strategy=query,
            expected_snapshot_id=(identity or None) if operation == "scan"
            else None,
            **changes,
        )
        if len(pending) >= 8:
            raise EngineCapacityError("Очередь расчётов предмета заполнена.")
        if await repository.command_status(key, command.request_id) is None:
            await repository.record_command(key, command)
        return command


async def read_analysis(
    storage: AuctionStorage, query: AuctionAnalysisQuery,
) -> AuctionAnalysisResponse | None:
    """Получить опубликованные результаты и выбранный период графика.

    Args:
        storage: Общее хранилище веб-процесса.
        query: Предмет, режим, период и профиль.

    Returns:
        Готовый профиль или None до его публикации.
    """
    async with storage.session() as (history, db):
        selection = await _select(history, db, storage.settings, query)
        if selection is None:
            return None
        result = await db.analysis(
            selection.key, selection.meta, selection.profile,
        )
        if result is None:
            await _queue_profile(db, selection, query)
            return None
        now = datetime.now(UTC)
        sync = history_summary(selection.point, storage.settings)
        end = sync.covered_until or now
        start = end - timedelta(days=query.days)
        stored = await history.read_at(
            selection.key, start, end, selection.point,
        )
        result.coverage.history_window_complete = (
            sync.covered_from is not None and sync.covered_until is not None
            and sync.covered_from <= start
        )
        return AuctionAnalysisResponse(
            **result.model_dump(),
            snapshot_id=selection.meta.snapshot_id,
            snapshot_age_seconds=selection.age,
            snapshot_stale=selection.stale,
            version=result.analysis_version,
            history=build_history(
                selection.item, stored.rows, query.days, now, sync=sync,
            ),
        )


async def read_version(
    storage: AuctionStorage, query: AuctionAnalysisQuery,
) -> AuctionVersionResponse:
    """Проверить готовность профиля без чтения лотов и сделок.

    Args:
        storage: Подключение веб-процесса.
        query: Предмет и параметры стратегии.

    Returns:
        Идентификатор оценки и метаданные актуальности.
    """
    async with storage.session() as (history, db):
        selection = await _select(history, db, storage.settings, query)
        if selection is None:
            key = HistoryKey(query.item_id, query.region, query.mode)
            point = await history.get_checkpoint(key)
            return AuctionVersionResponse(
                history=history_summary(point, storage.settings),
            )
        version = await db.analysis_version(
            selection.key, selection.meta, selection.profile,
        )
        if version is None:
            await _queue_profile(db, selection, query)
        return AuctionVersionResponse(
            version=version, snapshot_id=selection.meta.snapshot_id,
            snapshot_age_seconds=selection.age,
            snapshot_stale=selection.stale,
            history=history_summary(selection.point, storage.settings),
        )


async def _select(
    history: AuctionHistoryRepository, db: ScannerRepository,
    settings: AuctionStorageSettings, query: AuctionAnalysisQuery,
) -> _Selection | None:
    """Выбрать исходную версию и запросить нужное обновление.

    Args:
        history: Репозиторий контрольных точек.
        db: Репозиторий снимков и команд.
        settings: Интервал мониторинга по умолчанию.
        query: Параметры запрошенной оценки.

    Returns:
        Версия для чтения или None до первой публикации.
    """
    item = catalog_item(query.item_id)
    key = HistoryKey(query.item_id, query.region, query.mode)
    meta = await db.latest(key)
    if meta is None:
        await enqueue(db, key, query, "scan")
        return None
    now = datetime.now(UTC)
    age = max(0, int((now - meta.received_at).total_seconds()))
    policy = await db.latest_state("scanner_settings", key)
    interval = (
        ScannerSettings.model_validate_json(policy).interval
        if policy else settings.lots_interval
    )
    state = await db.latest_state("scanner_state", key)
    scanner = ScannerState.model_validate_json(state) if state else (
        ScannerState()
    )
    stale = age >= interval or scanner.status == "error"
    if stale and (
        not scanner.failures
        or scanner.next_at is None
        or now >= scanner.next_at
    ):
        await enqueue(db, key, query, "scan", meta.snapshot_id)
    point = await history.get_checkpoint(key)
    return _Selection(
        key, item, meta, point, analysis_key(key, item, query, point),
        age, stale,
    )


async def _queue_profile(
    db: ScannerRepository, selection: _Selection, query: AuctionAnalysisQuery,
) -> None:
    """Запросить расчёт выбранного профиля с ограничением очереди.

    Args:
        db: Хранилище команд.
        selection: Выбранные входные данные.
        query: Параметры запрошенной стратегии.
    """
    defaults = StrategySettings()
    custom = (
        query.min_sales != defaults.min_sales
        or query.min_roi_bps != defaults.min_roi_bps
    )
    if custom and await db.temporary_profiles(
        selection.key, selection.meta,
    ) >= MAX_TEMPORARY_PROFILES:
        raise EngineCapacityError("Достигнут предел временных профилей.")
    await enqueue(
        db, selection.key, query, "evaluate",
        selection.meta.snapshot_id + selection.profile,
    )
