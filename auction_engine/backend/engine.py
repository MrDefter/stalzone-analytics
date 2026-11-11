"""Планирование, сбор и публикация аукционных данных."""

import asyncio
import logging
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from time import perf_counter
from uuid import uuid4

from common.dataclasses import CatalogItem
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.enums.auction import AuctionMode
from common.enums.auction_history import HistoryOperation, HistorySyncStatus
from common.errors import AuctionConfigurationError
from common.errors.auction_engine import EngineCapacityError
from common.errors.clickhouse import HistoryStorageError
from common.models.auction import Sale
from common.models.auction_engine import (
    EngineStatus,
    ScannerCommand,
    ScannerSettings,
    ScannerState,
    SnapshotMetadata,
)
from common.models.auction_strategy import StrategySettings
from common.settings import ItemCatalogSettings

from .auction_pages import create_auction_client
from .auction_snapshot import collect_active_lots, collect_sale_history
from .engine_settings import AuctionEngineSettings
from .evaluator import evaluate
from .history_sync import synchronize_history
from .history_writer import HistoryWriter
from .integrations import AuctionClient
from .integrations.auction.errors import AuctionError, RateLimitError
from .integrations.auction.http import RATE_LIMIT_FEEDBACK, REQUEST_SLOT
from .rate_limiter import RateLimiter
from .scan_progress import ScanProgress
from .snapshot_writer import SnapshotWriter
from .storage import EngineStorage

ENGINE_KEY = HistoryKey("", "RU", AuctionMode.PRODUCTION)
DEFAULT_ITEMS = frozenset(("l0og1", "qjqw9", "y3nmw", "4q7pl"))
_LOGGER = logging.getLogger(__name__)


def _history_due(
    point: HistoryCheckpoint | None, now: datetime, interval: int,
) -> HistoryOperation | None:
    """Выбрать очередной участок завершённых продаж.

    Args:
        point: Сохранённая контрольная точка.
        now: Время текущего цикла.
        interval: Интервал обновления свежих продаж.

    Returns:
        Вид операции или None, если загрузка пока не нужна.
    """
    if point is None:
        return HistoryOperation.INITIAL
    waiting = point.retry_at is not None and now < point.retry_at
    if waiting and (
        point.covered_until is None
        or point.operation is HistoryOperation.INCREMENTAL
        or point.last_error_type == RateLimitError.__name__
    ):
        return None
    if point.covered_until is None:
        return HistoryOperation.INITIAL
    if not point.initial_complete and point.pending_from is None:
        return None if waiting else HistoryOperation.INITIAL
    if (
        point.pending_from is not None or point.last_success_at is None
        or now - point.last_success_at >= timedelta(seconds=interval)
    ):
        return HistoryOperation.INCREMENTAL
    if waiting:
        return None
    return None if point.backfill_complete else HistoryOperation.BACKFILL


class AuctionEngine:
    """Один планировщик и один общий бюджет для всех предметов."""

    def __init__(self, settings: AuctionEngineSettings) -> None:
        """Подготовить ресурсы процесса.

        Args:
            settings: Интервалы, лимиты и параметры ClickHouse.
        """
        self.settings = settings
        self.storage = EngineStorage(settings)
        self.limiter = RateLimiter(
            settings.max_http_concurrency, settings.requests_per_minute,
        )
        self._clients: dict[AuctionMode, AuctionClient] = {}
        self._client_lock = asyncio.Lock()
        self._stack = AsyncExitStack()
        self._active: dict[HistoryKey, asyncio.Task[None]] = {}
        self._last_run: dict[HistoryKey, datetime] = {}
        self._error: str | None = None
        self._published: datetime | None = None

    async def run(self) -> None:
        """Обрабатывать циклы до отмены и закрыть все клиенты."""
        slot = REQUEST_SLOT.set(self.limiter.slot)
        feedback = RATE_LIMIT_FEEDBACK.set(self.limiter.throttle)
        try:
            async with self._stack:
                try:
                    while True:
                        try:
                            async with self.storage.session() as (history, db):
                                await self._tick(history, db)
                        except Exception as error:
                            self._error = type(error).__name__
                            _LOGGER.warning("Scheduler error=%s", self._error)
                        await asyncio.sleep(self.settings.scheduler_interval)
                finally:
                    for task in self._active.values():
                        task.cancel()
                    await asyncio.gather(
                        *self._active.values(), return_exceptions=True,
                    )
        finally:
            RATE_LIMIT_FEEDBACK.reset(feedback)
            REQUEST_SLOT.reset(slot)
            try:
                async with (
                    asyncio.timeout(2),
                    self.storage.session() as (_, db),
                ):
                    await self._heartbeat(db, 0, online=False)
            except (HistoryStorageError, TimeoutError):
                pass
            await self.storage.close()

    async def _tick(
        self, history: HistoryWriter, db: SnapshotWriter,
    ) -> None:
        """Назначить допустимые задачи и записать подтверждение активности.

        Args:
            history: Репозиторий истории.
            db: Репозиторий снимков, команд и состояния.
        """
        for key, task in tuple(self._active.items()):
            if task.done():
                del self._active[key]
                if not task.cancelled() and (error := task.exception()):
                    self._error = type(error).__name__
        items = {
            item.item_id: item
            for item in ItemCatalogSettings.load(allow_empty=True).items
        }
        pending = await db.commands()
        by_key: dict[HistoryKey, list[ScannerCommand]] = {}
        for key, command in pending:
            if key.item_id not in items:
                await db.record_command(
                    key, command, "error", "item_not_found",
                )
                continue
            by_key.setdefault(key, []).append(command)
        keys = {
            HistoryKey(item, "RU", AuctionMode.PRODUCTION) for item in items
        } | by_key.keys()
        now = datetime.now(UTC)
        candidates = []
        for key in keys - self._active.keys():
            raw = await db.latest_state("scanner_settings", key)
            policy = ScannerSettings.model_validate_json(raw) if raw else (
                ScannerSettings(
                    monitoring=key.mode is AuctionMode.PRODUCTION
                    and key.item_id in DEFAULT_ITEMS,
                    interval=self.settings.lots_interval,
                )
            )
            commands = by_key.get(key, [])
            if not policy.monitoring and not commands:
                continue
            meta = await db.latest(key)
            raw_state = await db.latest_state("scanner_state", key)
            state = (
                ScannerState.model_validate_json(raw_state)
                if raw_state else ScannerState()
            )
            allowed = not state.failures or state.next_at is None or (
                state.next_at <= now
            )
            point = await history.get_checkpoint(key)
            needs_initial_history = point is None or not point.initial_complete
            due = _history_due(
                point, now, self.settings.sync_interval_seconds,
            )
            command = commands[0] if commands else None
            history_checked = point.updated_at if point else (
                meta.received_at if meta else now
            )
            history_waited = (
                policy.monitoring and due is not None and meta is not None
                and now - history_checked >= timedelta(seconds=120)
            )
            if command is not None and command.operation == "settings":
                operation, rank = "settings", 0
            elif meta is None and needs_initial_history:
                if due is None:
                    continue
                operation, rank = due, 0 if command is not None else 1
            elif history_waited:
                operation, rank = due, -1
            elif command and command.operation == "evaluate" and meta:
                operation, rank = "evaluate", 0
            elif command is not None and allowed:
                operation, rank = "scan", 0
            elif policy.monitoring and allowed and (
                meta is None or now - meta.received_at >= timedelta(
                    seconds=policy.interval,
                )
            ):
                operation, rank = "scan", 1
            elif policy.monitoring and due is not None:
                operation = due
                rank = 2 if due is HistoryOperation.BACKFILL else 1
            else:
                continue
            last_run = self._last_run.get(
                key, datetime.min.replace(tzinfo=UTC),
            )
            if rank == 2 and now - last_run >= timedelta(seconds=60):
                rank = -1
            priority = (
                rank, -policy.priority, last_run, key.item_id, key.mode.value,
            )
            candidates.append((priority, key, operation, command, policy))
        slots = self.settings.max_active_items - len(self._active)
        for _, key, operation, command, policy in sorted(
            candidates, key=lambda row: row[0],
        )[:slots]:
            self._last_run[key] = now
            self._active[key] = asyncio.create_task(
                self._execute(
                    key, items[key.item_id], operation, command, policy,
                ),
            )
        await self._heartbeat(db, len(pending))

    async def _heartbeat(
        self, db: SnapshotWriter, pending: int, *, online: bool = True,
    ) -> None:
        """Обновить отметку доступности и счётчики планировщика.

        Args:
            db: Хранилище состояния.
            pending: Число ожидающих команд.
            online: Ядро продолжает обслуживать очередь.
        """
        await db.store_state("scanner_state", ENGINE_KEY, EngineStatus(
            heartbeat=datetime.now(UTC), online=online,
            active_operations=len(self._active) if online else 0,
            pending_commands=pending, last_error=self._error,
            last_publication=self._published,
            exbo_requests_last_minute=self.limiter.requests_last_minute(),
            exbo_rate_limits=self.limiter.rate_limits,
        ))

    async def _client(self, mode: AuctionMode) -> AuctionClient:
        """Открыть один клиент EXBO для режима.

        Args:
            mode: Режим аукциона.

        Returns:
            Клиент до завершения процесса ядра.
        """
        async with self._client_lock:
            if mode not in self._clients:
                self._clients[mode] = await self._stack.enter_async_context(
                    create_auction_client(mode),
                )
            return self._clients[mode]

    async def _execute(
        self, key: HistoryKey, item: CatalogItem,
        operation: str | HistoryOperation, command: ScannerCommand | None,
        policy: ScannerSettings,
    ) -> None:
        """Выполнить одну задачу и подтвердить команду после записи.

        Args:
            key: Предмет, регион и режим.
            item: Настройки каталога.
            operation: Выбранный вид работы.
            command: Команда пользователя, если она ожидает обработки.
            policy: Действующие настройки мониторинга.
        """
        started = perf_counter()
        try:
            async with self.storage.session() as (history, db):
                await self._process(history, db, key, item, operation,
                                    command, policy)
        except HistoryStorageError:
            self._error = "HistoryStorageError"
            raise
        finally:
            _LOGGER.info(
                "Auction item=%s region=%s mode=%s "
                "operation=%s elapsed_ms=%.1f",
                key.item_id, key.region, key.mode, operation,
                (perf_counter() - started) * 1000,
            )

    async def _process(
        self, history: HistoryWriter, db: SnapshotWriter,
        key: HistoryKey, item: CatalogItem, operation: str | HistoryOperation,
        command: ScannerCommand | None, policy: ScannerSettings,
    ) -> None:
        """Обновить снимок, профиль, настройки или участок истории.

        Args:
            history: Хранилище продаж.
            db: Хранилище лотов и состояния.
            key: Ключ предмета.
            item: Настройки стандартной партии.
            operation: Операция планировщика.
            command: Исходная команда при наличии.
            policy: Текущие параметры мониторинга.
        """
        meta = await db.latest(key)
        refresh_error = None
        try:
            if isinstance(operation, HistoryOperation):
                await self._sync(history, key, operation)
                if meta is not None:
                    await evaluate(history, db, self.settings, key, item,
                                   meta, await db.lots(key, meta),
                                   StrategySettings())
                return
            if operation == "settings" and command is not None:
                updates = command.model_dump(
                    include={"monitoring", "interval", "priority"},
                    exclude_none=True,
                )
                await db.store_state("scanner_settings", key,
                                     policy.model_copy(update=updates))
            elif operation == "evaluate" and command is not None and meta:
                await evaluate(
                    history, db, self.settings, key, item,
                    meta, await db.lots(key, meta), command.strategy,
                )
            elif (
                command is None or meta is None
                or (
                    meta.received_at < command.created_at
                    and command.expected_snapshot_id in (
                        None, meta.snapshot_id,
                    )
                )
            ):
                meta, refresh_error = await self._scan(
                    history, db, key, item, command, policy,
                )
                if command is not None and command.operation == "evaluate":
                    published = await db.latest(key)
                    if published is not None:
                        await evaluate(
                            history, db, self.settings, key, item, published,
                            await db.lots(key, published), command.strategy,
                        )
            if command is not None:
                if command.operation == "scan" and meta is not None:
                    command = command.model_copy(update={
                        "result_snapshot_id": meta.snapshot_id,
                        "result_received_at": meta.received_at,
                    })
                    if not meta.lots_consistent:
                        refresh_error = "SnapshotInconsistent"
                if refresh_error and command.operation == "scan":
                    await db.record_command(
                        key, command, "error", refresh_error,
                    )
                else:
                    await db.record_command(key, command, "done")
        except EngineCapacityError as error:
            if command is not None:
                await db.record_command(key, command, "error",
                                        type(error).__name__)
        except (
            AuctionConfigurationError, AuctionError, TimeoutError, ValueError,
        ) as error:
            await self._record_failure(db, key, error, command)

    async def _record_failure(
        self, db: SnapshotWriter, key: HistoryKey, error: Exception,
        command: ScannerCommand | None,
    ) -> None:
        """Сохранить ошибку и время следующей попытки.

        Args:
            db: Хранилище состояния.
            key: Предмет, регион и режим.
            error: Исключение источника или конфигурации.
            command: Команда, для которой известен итог.
        """
        self._error = type(error).__name__
        raw = await db.latest_state("scanner_state", key)
        state = (
            ScannerState.model_validate_json(raw) if raw else ScannerState()
        )
        delay = min(300, 5 * 2 ** min(state.failures, 6))
        if isinstance(error, RateLimitError):
            delay = max(delay, self.settings.sync_interval_seconds,
                        min(error.retry_after_seconds or 0, 86400))
        await db.store_state("scanner_state", key, state.model_copy(update={
            "failures": state.failures + 1, "error_type": self._error,
            "next_at": datetime.now(UTC) + timedelta(seconds=delay),
            "status": "error",
        }))
        if command is not None:
            await db.record_command(key, command, "error", self._error)

    async def _scan(
        self, history: HistoryWriter, db: SnapshotWriter,
        key: HistoryKey, item: CatalogItem, command: ScannerCommand | None,
        policy: ScannerSettings,
    ) -> tuple[SnapshotMetadata, str | None]:
        """Собрать лоты и опубликовать версию после подтверждения записи.

        Args:
            history: Источник сохранённых продаж.
            db: Хранилище снимков.
            key: Ключ аукциона.
            item: Настройки каталога.
            command: Команда, вызвавшая сканирование.
            policy: Действующий интервал мониторинга.

        Returns:
            Опубликованный снимок и код ошибки сопутствующей истории.
        """
        started = datetime.now(UTC)
        snapshot_id = uuid4().hex
        loop = asyncio.get_running_loop()
        scan = await collect_active_lots(
            await self._client(key.mode), key.region, key.item_id,
            deadline=loop.time() + self.settings.chunk_timeout_seconds,
            snapshot_id=snapshot_id,
        )
        if not scan.rows and not scan.complete:
            raise ValueError("Пустой снимок с неподтверждённым покрытием.")
        now = datetime.now(UTC)
        meta = SnapshotMetadata(
            snapshot_id=snapshot_id, started_at=started,
            received_at=scan.received_at, evaluated_at=now, published_at=now,
            expires_at=now + timedelta(days=self.settings.snapshots_ttl_days),
            lots_loaded=len(scan.rows),
            lots_reported_total=scan.reported_total,
            lots_complete=scan.complete, lots_consistent=scan.consistent,
            request_ids=(command.request_id,) if command else (),
        )
        await db.store_lots(key, meta, scan)
        result = await evaluate(history, db, self.settings, key, item,
                                meta, scan, StrategySettings())
        meta.evaluated_at = result.evaluated_at
        meta.published_at = datetime.now(UTC)
        await db.publish(key, meta)
        self._published = meta.published_at
        await db.store_state("scanner_state", key, ScannerState(
            snapshot_id=meta.snapshot_id, last_success_at=meta.received_at,
            next_at=meta.received_at + timedelta(seconds=policy.interval),
        ))
        _LOGGER.info("Published snapshot=%s item=%s lots=%d evaluated=%d",
                     meta.snapshot_id, key.item_id, len(scan.rows),
                     len(result.lots))
        if not policy.monitoring:
            point = await history.get_checkpoint(key)
            due = _history_due(point, datetime.now(UTC),
                               self.settings.sync_interval_seconds)
            if due in (HistoryOperation.INITIAL, HistoryOperation.INCREMENTAL):
                updated = await self._sync(history, key, due)
                await evaluate(history, db, self.settings, key, item,
                               meta, scan, StrategySettings())
                if updated.status is HistorySyncStatus.ERROR:
                    return meta, "HistoryRefreshFailed"
        return meta, None

    async def _sync(
        self, history: HistoryWriter, key: HistoryKey,
        operation: HistoryOperation,
    ) -> HistoryCheckpoint:
        """Продвинуть контрольную точку через существующий сборщик.

        Args:
            history: Хранилище продаж.
            key: Предмет, регион и режим.
            operation: Начальная, свежая или историческая загрузка.

        Returns:
            Сохранённая контрольная точка с результатом синхронизации.
        """
        point = await history.get_checkpoint(key)

        async def read(
            since: datetime, before: datetime | None, progress: ScanProgress,
        ) -> PageScan[Sale]:
            """Получить порцию страниц с общим HTTP-бюджетом.

            Args:
                since: Нижняя граница чтения.
                before: Опорная граница для продолжения.
                progress: Счётчики страниц.

            Returns:
                Полученные сделки и качество проверки.
            """
            return await collect_sale_history(
                await self._client(key.mode), key.region, key.item_id,
                since=since, before=before, progress=progress,
                deadline=(asyncio.get_running_loop().time()
                          + self.settings.chunk_timeout_seconds),
            )

        updated = await synchronize_history(
            history, key, point or HistoryCheckpoint(datetime.now(UTC)),
            operation, self.settings, read,
        )
        if updated.status is HistorySyncStatus.ERROR:
            self._error = updated.last_error_type
        return updated
