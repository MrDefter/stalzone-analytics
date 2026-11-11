"""Синхронизация подтверждённых временных участков истории EXBO."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from time import perf_counter

from common.dataclasses.auction import PageScan
from auction_engine.backend.scan_progress import ScanProgress
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.enums.auction_history import HistoryOperation, HistorySyncStatus
from common.errors import AuctionConfigurationError
from auction_engine.backend.integrations.auction.errors import (
    AuctionError,
    RateLimitError,
)
from common.models.auction import Sale
from common.settings.clickhouse import ClickHouseSettings

from .errors import HistorySyncError
from .history_writer import HistoryWriter

HistoryReader = Callable[
    [datetime, datetime | None, ScanProgress], Awaitable[PageScan[Sale]]
]
_LOGGER = logging.getLogger("uvicorn.error")
_INITIAL_WINDOW = timedelta(hours=24)
_NEXT_MICROSECOND = timedelta(microseconds=1)
_UINT64_MAX = (1 << 64) - 1
_MAX_RETRY_AT = datetime(2299, 1, 1, tzinfo=UTC)


def _confirmed_start(
    scan: PageScan[Sale], since: datetime, checked_at: datetime
) -> datetime:
    """Найти начало участка с полностью прочитанными отметками времени.

    Args:
        scan: Проверенный ответ сборщика страниц.
        since: Запрошенная нижняя граница.
        checked_at: Время старта чтения.

    Returns:
        Нижняя включённая граница доступной подтверждённой истории.

    Raises:
        HistorySyncError: Страницы или поля продаж не прошли проверку.
    """
    if not scan.consistent or any(
        not 0 < sale.amount <= _UINT64_MAX
        or not 0 < sale.price <= _UINT64_MAX
        or sale.time > scan.received_at
        for sale in scan.rows
    ):
        raise HistorySyncError("EXBO не подтвердил согласованность истории.")
    if not scan.rows:
        return checked_at
    oldest = scan.rows[-1].time
    if not scan.reached_end:
        # Последняя отметка может продолжаться на непрочитанной странице.
        oldest += _NEXT_MICROSECOND
    return max(since, oldest)


def _advance(
    point: HistoryCheckpoint,
    *,
    start: datetime,
    until: datetime,
    before: datetime | None,
    retention: datetime,
    exhausted: bool,
) -> HistoryCheckpoint:
    """Продвинуть границы только по связному подтверждённому участку.

    Args:
        point: Состояние до записи порции.
        start: Подтверждённое начало новой порции.
        until: Подтверждённый конец новой порции.
        before: Нижняя граница старой истории для догрузки назад.
        retention: Нижняя граница срока хранения.
        exhausted: Источник дошёл до конца доступного списка.

    Returns:
        Следующее состояние после успешной записи продаж.

    """
    previous_start = point.covered_from
    previous_end = point.covered_until
    if before is not None and previous_end is not None:
        # Исчерпанный источник может больше не содержать старую границу.
        start = min(before, start)
        until = previous_end
    connected = (
        previous_start is not None and previous_end is not None
        and start <= previous_end
    )
    if connected:
        start = min(start, previous_start)
    finished = datetime.now(UTC)
    initial_complete = (
        point.initial_since is not None
        and start <= max(retention, point.initial_since)
    ) or exhausted or (connected and point.initial_complete)
    return replace(
        point, covered_from=max(retention, start), covered_until=until,
        pending_from=None, pending_until=None,
        last_success_at=finished if before is None else point.last_success_at,
        initial_complete=initial_complete,
        backfill_complete=(
            start <= retention or exhausted
            or (connected and point.backfill_complete)
        ),
        updated_at=finished, status=HistorySyncStatus.READY, retry_at=None,
    )


async def synchronize_history(
    repository: HistoryWriter,
    key: HistoryKey,
    checkpoint: HistoryCheckpoint,
    operation: HistoryOperation,
    settings: ClickHouseSettings,
    read_exbo: HistoryReader,
) -> HistoryCheckpoint:
    """Записать одну ограниченную порцию и зафиксировать её результат.

    Args:
        repository: Открытое хранилище истории.
        key: Предмет, регион и режим EXBO.
        checkpoint: Последнее сохранённое состояние предмета.
        operation: Приоритетная задача для данного запуска.
        settings: Интервал, срок хранения и размер перекрытия.
        read_exbo: Существующий сборщик с тайм-аутом порции.

    Returns:
        Сохранённое состояние, в том числе при ожидаемой ошибке EXBO.
    """
    started = perf_counter()
    checked_at = datetime.now(UTC)
    retention = checked_at - timedelta(days=settings.ttl_days)
    initial_since = checkpoint.initial_since or checked_at - _INITIAL_WINDOW
    before = None
    if operation is HistoryOperation.INITIAL:
        since = initial_since
        before = checkpoint.covered_from
    elif operation is HistoryOperation.BACKFILL:
        since = retention
        before = checkpoint.covered_from
    else:
        assert checkpoint.covered_until is not None
        since = checkpoint.covered_until - timedelta(
            seconds=settings.overlap_seconds,
        )
        before = checkpoint.pending_from
    since = max(since, retention)
    point = replace(
        checkpoint, updated_at=checked_at, initial_since=initial_since,
        operation=operation, status=HistorySyncStatus.RUNNING,
    )
    progress = ScanProgress()
    added = received = observed = 0
    result = checkpoint
    error_type = None
    status = "cancelled"
    reading_exbo = False
    try:
        await repository.ensure_item(key)
        await repository.set_checkpoint(key, point)
        reading_exbo = True
        scan = await read_exbo(since, before, progress)
        reading_exbo = False
        received = len(scan.rows)
        start = _confirmed_start(scan, since, checked_at)
        until = checked_at if before is None else before
        exhausted = scan.reached_end and (
            not scan.rows or scan.rows[-1].time >= since
        )
        if start >= until and not exhausted:
            raise HistorySyncError("EXBO не подтвердил продолжение истории.")
        segment_start = min(start, until)
        if operation is HistoryOperation.INCREMENTAL:
            assert point.covered_until is not None
            if (
                not scan.rows and point.covered_from is not None
                and point.covered_from < point.covered_until
            ):
                raise HistorySyncError(
                    "Пустой ответ EXBO не подтверждает продолжение истории."
                )
            segment_end = point.pending_until or checked_at
            if segment_start > point.covered_until and not exhausted:
                # Догрузим разрыв с сохранённой границы после перезапуска.
                next_point = replace(
                    point, pending_from=segment_start,
                    pending_until=segment_end, updated_at=datetime.now(UTC),
                    status=HistorySyncStatus.READY, retry_at=None,
                )
            else:
                next_point = _advance(
                    point, start=segment_start, until=segment_end, before=None,
                    retention=retention, exhausted=exhausted,
                )
        else:
            next_point = _advance(
                point, start=segment_start, until=until, before=before,
                retention=retention, exhausted=exhausted,
            )
        sales = [sale for sale in scan.rows if start <= sale.time <= until]
        observed = len(sales)
        added, next_point = await repository.commit_sales(
            key, sales, next_point,
        )
        result = next_point
        status = "partial" if result.pending_from is not None else "ready"
        return result
    except (
        AuctionError, AuctionConfigurationError, HistorySyncError,
        TimeoutError,
    ) as error:
        error_type = type(error).__name__
        status = "error"
        if isinstance(error, TimeoutError) and not reading_exbo:
            raise
        finished = datetime.now(UTC)
        wait = settings.sync_interval_seconds
        message = "Не удалось обновить историю EXBO."
        if isinstance(error, RateLimitError):
            wait = max(wait, error.retry_after_seconds or 0)
            message = "EXBO ограничил частоту обновления истории."
        elif isinstance(error, HistorySyncError):
            message = str(error)
        result = replace(
            point, updated_at=finished, status=HistorySyncStatus.ERROR,
            last_error=message, last_error_type=error_type,
            last_error_at=finished, retry_at=finished + timedelta(
                seconds=min(wait, (_MAX_RETRY_AT - finished).total_seconds()),
            ),
        )
        await repository.set_checkpoint(key, result)
        return result
    except Exception as error:
        error_type = type(error).__name__
        status = "error"
        raise
    finally:
        _LOGGER.info(
            "History sync item_id=%s region=%s mode=%s operation=%s "
            "elapsed_ms=%.1f exbo_page_requests=%d received=%d added=%d "
            "duplicates=%d old_from=%s old_until=%s new_from=%s "
            "new_until=%s status=%s error_type=%s",
            key.item_id, key.region, key.mode, operation,
            (perf_counter() - started) * 1000, progress.pages_attempted,
            received, added, observed - added,
            checkpoint.covered_from, checkpoint.covered_until,
            result.covered_from, result.covered_until, status, error_type,
        )
