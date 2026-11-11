"""Сбор и проверка согласованности страниц аукциона."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from itertools import pairwise
from operator import attrgetter
from time import perf_counter
from typing import TypeVar
from uuid import uuid4

from common.dataclasses import PageResult
from common.dataclasses.auction import PageScan
from auction_engine.backend.scan_progress import ScanProgress
from common.enums.auction import LotSort, SortOrder
from auction_engine.backend.integrations.auction.errors import AuctionError
from auction_engine.backend.integrations import AuctionClient
from auction_engine.backend.integrations.auction.diagnostics import (
    CURRENT_PAGE,
    LOGGER,
    PageLog,
    completion_status,
    error_details,
)
from common.models.auction import HistoryPage, Lot, LotPage, Sale

_PAGE_LIMIT = 200
_MAX_LOT_PAGES = 5
_LOTS_PAGE_CONCURRENCY = 2
_HISTORY_PAGE_CONCURRENCY = 8
_OVERLAP = 2
_HISTORY_RECHECK_SECONDS = 10.0
_PageT = TypeVar("_PageT", LotPage, HistoryPage)
_RowT = TypeVar("_RowT")
SourceFailure = AuctionError | TimeoutError


def _valid_page(
    rows: list[_RowT],
    *,
    total: int,
    offset: int,
    time_of: Callable[[_RowT], datetime],
) -> bool:
    """Проверить длину и хронологический порядок страницы.

    Args:
        rows: Строки страницы.
        total: Общее число строк, заявленное EXBO.
        offset: Запрошенное смещение.
        time_of: Чтение времени из строки.

    Returns:
        Признак согласованности страницы с параметрами запроса.
    """
    return len(rows) == min(_PAGE_LIMIT, total - offset) and all(
        time_of(newer) >= time_of(older) for newer, older in pairwise(rows)
    )


async def _find_history_anchor(
    fetch: Callable[..., Awaitable[PageResult[_PageT]]],
    first: PageResult[_PageT],
    before: datetime,
    *,
    rows_of: Callable[[_PageT], list[_RowT]],
    time_of: Callable[[_RowT], datetime],
) -> tuple[int, PageResult[_PageT]] | None:
    """Найти страницу, с которой можно продолжить старую историю.

    Args:
        fetch: Запрос страницы со смещением и этапом.
        first: Проверенная первая страница с текущим общим числом строк.
        before: Нижняя граница уже подтверждённого покрытия.
        rows_of: Чтение строк из ответа.
        time_of: Чтение времени из строки.

    Returns:
        Смещение и страница с началом выше границы или None при сдвиге.
    """
    total = first.data.total
    step = _PAGE_LIMIT - _OVERLAP
    first_rows = rows_of(first.data)
    if not first_rows or time_of(first_rows[-1]) < before:
        return 0, first
    low = 0
    high = len(range(step, total - _OVERLAP, step)) + 1
    anchor = first
    while high - low > 1:
        index = (low + high) // 2
        offset = index * step
        page = await fetch(offset=offset, stage="history_seek")
        rows = rows_of(page.data)
        if page.data.total != total or not _valid_page(
            rows, total=total, offset=offset, time_of=time_of,
        ):
            return None
        if time_of(rows[0]) > before:
            low = index
            anchor = page
        else:
            high = index
    return low * step, anchor


async def _scan_pages(
    fetch: Callable[..., Awaitable[PageResult[_PageT]]],
    *,
    rows_of: Callable[[_PageT], list[_RowT]],
    time_of: Callable[[_RowT], datetime],
    since: datetime | None,
    before: datetime | None,
    pagination_deadline: float | None,
    progress: ScanProgress,
) -> PageScan[_RowT]:
    """Прочитать страницы параллельно и проверить по смещениям.

    Args:
        fetch: Запрос страницы со смещением и этапом для журнала.
        rows_of: Чтение строк из модели ответа.
        time_of: Чтение времени из строки.
        since: Нижняя граница истории или None при чтении лотов.
        before: Граница, от которой нужно продолжить чтение назад.
        pagination_deadline: Срок загрузки страниц до повторной сверки.
        progress: Текущий этап и счётчики загрузки источника.

    Returns:
        Прочитанный префикс с результатом проверки согласованности.
    """
    first = await fetch(offset=0, stage="initial_page")
    progress.stage = "page_merge"
    first_rows = rows_of(first.data)
    total = first.data.total
    progress.stage = "order_validation"
    consistent = _valid_page(
        first_rows, total=total, offset=0, time_of=time_of,
    )
    start_offset = 0
    if before is not None and consistent:
        progress.stage = "history_seek"
        found = await _find_history_anchor(
            fetch, first, before, rows_of=rows_of, time_of=time_of,
        )
        if found is None:
            consistent = False
        else:
            start_offset, first = found
            first_rows = rows_of(first.data)
    failed_check = None if consistent else progress.stage
    received_at = first.received_at
    rows = list(first_rows)
    previous = first_rows
    progress.stage = "pagination"
    step = _PAGE_LIMIT - _OVERLAP
    # Каждая следующая страница должна добавить хотя бы одну строку.
    offsets = range(start_offset + step, total - _OVERLAP, step)
    batch_size = _HISTORY_PAGE_CONCURRENCY
    if since is None:
        offsets = offsets[:_MAX_LOT_PAGES - 1]
        batch_size = _LOTS_PAGE_CONCURRENCY
    progress.stage = "history_window"
    past_cutoff = (
        since is not None and bool(rows) and time_of(rows[-1]) < since
    )

    paging_timed_out = False
    try:
        async with asyncio.timeout_at(pagination_deadline) as paging:
            for index in range(0, len(offsets), batch_size):
                if not consistent or past_cutoff:
                    break

                progress.stage = "pagination"
                progress.offset = offsets[index]
                batch = {
                    offset: asyncio.ensure_future(
                        fetch(offset=offset, stage="pagination")
                    )
                    for offset in offsets[index:index + batch_size]
                }
                try:
                    for offset, task in batch.items():
                        progress.stage = "pagination"
                        progress.offset = offset
                        page = await task
                        progress.stage = "page_merge"
                        received_at = max(received_at, page.received_at)
                        current = rows_of(page.data)
                        progress.stage = "overlap_validation"
                        if (
                            page.data.total != total
                            or len(current) <= _OVERLAP
                            or current == previous
                            or current[:_OVERLAP]
                            != rows[offset - start_offset:]
                        ):
                            consistent = False
                            failed_check = progress.stage
                            break
                        progress.stage = "order_validation"
                        if not _valid_page(
                            current, total=total, offset=offset,
                            time_of=time_of,
                        ):
                            consistent = False
                            failed_check = progress.stage
                            break
                        progress.stage = "page_merge"
                        rows.extend(current[_OVERLAP:])
                        previous = current
                        progress.stage = "history_window"
                        if since is not None and time_of(rows[-1]) < since:
                            past_cutoff = True
                            break
                finally:
                    # Дождаться остановки задач перед следующим запросом.
                    for task in batch.values():
                        task.cancel()
                    try:
                        await asyncio.gather(
                            *batch.values(), return_exceptions=True
                        )
                    except asyncio.CancelledError:
                        progress.stage = "pagination_cleanup"
                        raise
    except TimeoutError:
        if not paging.expired():
            raise
        paging_timed_out = True

    if consistent and (start_offset > 0 or len(rows) > len(first_rows)):
        progress.stage = "first_page_recheck"
        progress.offset = start_offset
        anchor = await fetch(offset=start_offset, stage=progress.stage)
        received_at = anchor.received_at
        consistent = (
            anchor.data.total == total and rows_of(anchor.data) == first_rows
        )
        if not consistent:
            failed_check = progress.stage
    progress.stage = "history_budget" if paging_timed_out else "finalization"
    scan = PageScan(tuple(rows), total, received_at, consistent, start_offset)
    if failed_check is not None:
        progress.stage = failed_check
    return scan


async def _scan(
    fetch: Callable[..., Awaitable[PageResult[_PageT]]],
    *,
    rows_of: Callable[[_PageT], list[_RowT]],
    time_of: Callable[[_RowT], datetime],
    since: datetime | None = None,
    before: datetime | None = None,
    deadline: float | None = None,
    resource: str = "auction",
    snapshot_id: str | None = None,
    progress: ScanProgress | None = None,
) -> PageScan[_RowT]:
    """Собрать один ресурс в пределах срока и записать замеры.

    Args:
        fetch: Запрос страницы с именованным параметром offset.
        rows_of: Чтение строк из модели ответа.
        time_of: Чтение времени из строки.
        since: Нижняя граница истории или None при чтении лотов.
        before: Граница для продолжения исторической загрузки.
        deadline: Момент окончания сбора по часам цикла событий.
        resource: Имя ресурса для журнала.
        snapshot_id: Идентификатор общего сбора лотов и истории.
        progress: Внешние счётчики фоновой операции при наличии.

    Returns:
        Прочитанный префикс с результатом проверки согласованности.
    """
    started = perf_counter()
    progress = progress if progress is not None else ScanProgress()
    scan = None
    error: BaseException | None = None

    async def read_page(offset: int, stage: str) -> PageResult[_PageT]:
        """Запросить страницу с отдельным контекстом журналирования.

        Args:
            offset: Смещение запрашиваемой страницы.
            stage: Этап, на котором началась загрузка.

        Returns:
            Страница со строками и временем получения.
        """
        progress.pages_attempted += 1
        page_log = PageLog(
            snapshot_id=snapshot_id, source=resource, stage=stage,
            offset=offset, limit=_PAGE_LIMIT,
        )
        context = CURRENT_PAGE.set(page_log)
        try:
            page = await fetch(offset=offset)
            count = len(rows_of(page.data))
        except BaseException as exc:
            page_log.write(error=exc)
            raise
        finally:
            CURRENT_PAGE.reset(context)
        progress.pages_succeeded += 1
        progress.rows_received += count
        page_log.write(rows_received=count)
        return page

    pagination_deadline = None
    if since is not None and deadline is not None:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        recheck_time = min(_HISTORY_RECHECK_SECONDS, remaining / 10)
        pagination_deadline = deadline - recheck_time
    try:
        async with asyncio.timeout_at(deadline):
            scan = await _scan_pages(
                read_page, rows_of=rows_of, time_of=time_of, since=since,
                before=before,
                pagination_deadline=pagination_deadline, progress=progress,
            )
        return scan
    except BaseException as exc:
        error = exc
        raise
    finally:
        status = completion_status(error)
        error_type, cause_type, message = error_details(error)
        LOGGER.log(
            logging.ERROR if status == "failed" else logging.INFO,
            "EXBO scan snapshot_id=%s source=%s status=%s stage=%s "
            "offset=%d elapsed_ms=%.1f pages_attempted=%d pages_succeeded=%d "
            "rows_received=%d complete=%s consistent=%s error_type=%s "
            "cause_type=%s error_message=%r",
            snapshot_id, resource, status, progress.stage, progress.offset,
            (perf_counter() - started) * 1000,
            progress.pages_attempted, progress.pages_succeeded,
            progress.rows_received,
            scan.complete if scan is not None else None,
            scan.consistent if scan is not None else None,
            error_type, cause_type, message,
        )


def _source_result(
    result: PageScan[_RowT] | BaseException,
) -> PageScan[_RowT] | SourceFailure:
    """Отделить ожидаемый сбой источника от внутренней ошибки.

    Args:
        result: Результат сбора или исключение из gather.

    Returns:
        Прочитанные страницы или ожидаемая ошибка загрузки.
    """
    if isinstance(result, (PageScan, AuctionError, TimeoutError)):
        return result
    raise result


async def collect_active_lots(
    client: AuctionClient, region: str, item_id: str, *,
    deadline: float | None = None, snapshot_id: str | None = None,
) -> PageScan[Lot]:
    """Собрать активные лоты отдельно от истории.

    Args:
        client: Открытый клиент EXBO.
        region: Регион аукциона.
        item_id: Точный ID предмета.
        deadline: Срок окончания по часам цикла событий.
        snapshot_id: Идентификатор для связывания строк журнала.

    Returns:
        Проверенный снимок прочитанных лотов.
    """
    return await _scan(
        partial(
            client.get_lots, region, item_id, limit=_PAGE_LIMIT,
            additional=True, sort=LotSort.TIME_CREATED, order=SortOrder.DESC,
        ),
        rows_of=attrgetter("lots"), time_of=attrgetter("start_time"),
        deadline=deadline, resource="lots", snapshot_id=snapshot_id,
    )


async def collect_sale_history(
    client: AuctionClient, region: str, item_id: str, *,
    since: datetime, before: datetime | None = None,
    deadline: float | None = None, snapshot_id: str | None = None,
    progress: ScanProgress | None = None,
) -> PageScan[Sale]:
    """Прочитать продажи от актуального списка или контрольной границы.

    Args:
        client: Открытый клиент EXBO.
        region: Регион аукциона.
        item_id: Точный ID предмета.
        since: Нижняя граница запрашиваемого периода.
        before: Граница уже накопленной истории при продолжении.
        deadline: Срок окончания по часам цикла событий.
        snapshot_id: Идентификатор для связывания строк журнала.
        progress: Счётчики страниц для журнала фоновой операции.

    Returns:
        Сырые продажи с проверкой перекрытий и согласованности.
    """
    return await _scan(
        partial(
            client.get_history, region, item_id, limit=_PAGE_LIMIT,
            additional=True,
        ),
        rows_of=attrgetter("prices"), time_of=attrgetter("time"),
        since=since, before=before, deadline=deadline, resource="history",
        snapshot_id=snapshot_id, progress=progress,
    )


async def collect_auction_snapshot(
    client: AuctionClient,
    region: str,
    item_id: str,
    *,
    history_since: datetime,
    deadline: float | None = None,
) -> tuple[PageScan[Lot] | SourceFailure, PageScan[Sale] | SourceFailure]:
    """Параллельно собрать лоты и историю через один открытый клиент.

    Args:
        client: Клиент под управлением вызывающего кода.
        region: Регион аукциона.
        item_id: Точный ID предмета.
        history_since: Нижняя граница интересующего окна продаж.
        deadline: Общий срок сбора по часам цикла событий.

    Returns:
        Результат или ошибка каждого источника, сначала лоты.
    """
    started = perf_counter()
    snapshot_id = uuid4().hex
    try:
        lots, history = await asyncio.gather(
            collect_active_lots(
                client, region, item_id, deadline=deadline,
                snapshot_id=snapshot_id,
            ),
            collect_sale_history(
                client, region, item_id, since=history_since,
                deadline=deadline,
                snapshot_id=snapshot_id,
            ),
            return_exceptions=True,
        )
        return _source_result(lots), _source_result(history)
    finally:
        LOGGER.info(
            "EXBO snapshot snapshot_id=%s elapsed_ms=%.1f",
            snapshot_id, (perf_counter() - started) * 1000,
        )
