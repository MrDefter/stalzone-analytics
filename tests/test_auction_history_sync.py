"""Сценарии сохранения границ и кратности истории без внешней сети."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from auction_engine.backend.history_writer import _sale_rows
from auction_engine.backend.history_sync import (
    HistoryReader,
    synchronize_history,
)
from common.dataclasses.auction import PageScan
from auction_engine.backend.scan_progress import ScanProgress
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.enums.auction import AuctionMode
from common.enums.auction_history import HistoryOperation, HistorySyncStatus
from auction_engine.backend.integrations.auction.errors import RateLimitError
from common.errors.clickhouse import HistoryStorageError
from auction_engine.backend.integrations import AuctionClient
from common.models.auction import Sale
from auction_engine.backend import auction_snapshot
from common.settings.clickhouse import ClickHouseSettings

pytestmark = pytest.mark.asyncio
_KEY = HistoryKey("fixture", "RU", AuctionMode.DEMO)
_SETTINGS = ClickHouseSettings(_env_file=None, overlap_seconds=120)


class _Repository:
    """Локальное хранилище с записью границы после вставки продаж."""

    def __init__(self) -> None:
        """Подготовить независимые продажи и границы по ключам."""
        self.rows: dict[HistoryKey, dict[tuple[bytes, int], Sale]] = {}
        self.points: dict[HistoryKey, HistoryCheckpoint] = {}
        self.fail_checkpoint = False

    async def ensure_item(self, key: HistoryKey) -> None:
        """Подготовить хранилище одного предмета.

        Args:
            key: Независимый источник продаж.
        """
        self.rows.setdefault(key, {})

    async def append_sales(self, key: HistoryKey, sales: list[Sale]) -> int:
        """Сохранить каждое новое вхождение канонической сигнатуры.

        Args:
            key: Независимый источник продаж.
            sales: Сделки подтверждённого временного участка.

        Returns:
            Число добавленных событий с учётом кратности.
        """
        current = self.rows[key]
        before = len(current)
        for sale, row in zip(sales, _sale_rows(key, sales)):
            current.setdefault((row[-2], row[-1]), sale)
        return len(current) - before

    async def set_checkpoint(
        self, key: HistoryKey, point: HistoryCheckpoint
    ) -> None:
        """Записать границу или имитировать обрыв после вставки продаж.

        Args:
            key: Независимый источник продаж.
            point: Записываемое состояние операции.
        """
        if self.fail_checkpoint and point.status is HistorySyncStatus.READY:
            self.fail_checkpoint = False
            raise HistoryStorageError("Прерванная запись контрольной точки.")
        self.points[key] = point

    async def commit_sales(
        self, key: HistoryKey, sales: list[Sale], point: HistoryCheckpoint,
    ) -> tuple[int, HistoryCheckpoint]:
        """Сохранить продажи и границу для проверки сборщика.

        Args:
            key: Независимый источник продаж.
            sales: Прочитанная порция.
            point: Состояние после загрузки.

        Returns:
            Число новых продаж и сохранённое состояние.
        """
        added = await self.append_sales(key, sales)
        await self.set_checkpoint(key, point)
        return added, point


def _sale(at: datetime, *, amount: int = 10) -> Sale:
    """Создать сделку с точной ценой и временем.

    Args:
        at: Время продажи с часовым поясом.
        amount: Число проданных предметов.

    Returns:
        Запись завершённой продажи EXBO.
    """
    return Sale(
        amount=amount, price=2**53 + 1, time=at.isoformat(),
        additional={"a": 1, "b": 2},
    )


async def test_initial_backfill_and_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Заполнить сутки, расширить период и повторить короткий проход.

    Args:
        monkeypatch: Подмена размера страниц локального EXBO.
    """
    monkeypatch.setattr(auction_snapshot, "_PAGE_LIMIT", 8)
    now = datetime.now(UTC)
    equal = _sale(now - timedelta(seconds=20))
    sales = [equal, equal.model_copy(update={"additional": {"b": 2, "a": 1}})]
    sales += [
        _sale(now - timedelta(minutes=i), amount=7) for i in range(4, 15)
    ]
    sales += [_sale(now - timedelta(days=i)) for i in range(2, 33)]
    offsets = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Отдать срез продаж с текущим общим числом строк.

        Args:
            request: Запрос клиента на страницу истории.

        Returns:
            Подставная страница EXBO.
        """
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        offsets.append(offset)
        return httpx.Response(200, json={
            "total": len(sales),
            "prices": [
                sale.model_dump(mode="json")
                for sale in sales[offset:offset + limit]
            ],
        })

    repository = _Repository()
    async with AuctionClient.demo(
        "fake-demo", transport=httpx.MockTransport(respond),
    ) as client:
        async def read_exbo(
            since: datetime, before: datetime | None, progress: ScanProgress
        ) -> PageScan[Sale]:
            """Собрать порцию через обычную пагинацию EXBO.

            Args:
                since: Нижняя временная граница.
                before: Контрольная точка для продолжения назад.
                progress: Счётчики фоновой операции.

            Returns:
                Продажи с результатом проверок.
            """
            return await auction_snapshot.collect_sale_history(
                client, _KEY.region, _KEY.item_id, since=since, before=before,
                progress=progress,
                deadline=asyncio.get_running_loop().time() + 2,
            )

        point = await synchronize_history(
            repository, _KEY, HistoryCheckpoint(now), HistoryOperation.INITIAL,
            _SETTINGS, read_exbo,
        )
        assert point.initial_complete and not point.backfill_complete
        assert len(repository.rows[_KEY]) == 13
        assert list(repository.rows[_KEY].values()).count(equal) == 2
        point = await synchronize_history(
            repository, _KEY, point, HistoryOperation.BACKFILL,
            _SETTINGS, read_exbo,
        )
        assert point.backfill_complete
        assert len(repository.rows[_KEY]) == 41
        assert point.covered_until - point.covered_from >= timedelta(days=29)

        for extra in (0, 1, 0):
            if extra:
                sales.insert(0, equal)
            count = len(repository.rows[_KEY])
            offsets.clear()
            point = await synchronize_history(
                repository, _KEY, point, HistoryOperation.INCREMENTAL,
                _SETTINGS, read_exbo,
            )
            assert offsets == [0]
            assert len(repository.rows[_KEY]) == count + extra
        assert list(repository.rows[_KEY].values()).count(equal) == 3

    other = HistoryKey("other", "RU", AuctionMode.DEMO)
    await repository.ensure_item(other)
    assert not repository.rows[other]


@pytest.mark.parametrize("failure", ["drift", "rate_limit", "empty"])
async def test_sync_keeps_checkpoint_on_source_error(failure: str) -> None:
    """Сохранить границы, когда EXBO не подтвердил продолжение.

    Args:
        failure: Имитируемый сбой источника EXBO.
    """
    repository = _Repository()
    now = datetime.now(UTC)
    point = HistoryCheckpoint(
        now, covered_from=now - timedelta(days=10),
        covered_until=now - timedelta(minutes=5), initial_complete=True,
        last_success_at=now - timedelta(minutes=5),
    )

    async def read_exbo(
        since: datetime, before: datetime | None, progress: ScanProgress
    ) -> PageScan[Sale]:
        """Имитировать сбой или пустой ответ EXBO.

        Args:
            since: Нижняя временная граница.
            before: Граница продолжения.
            progress: Счётчики попыток.

        Returns:
            Пустой список или продажи с неподтверждённым порядком.
        """
        progress.pages_attempted = 2
        if failure == "rate_limit":
            raise RateLimitError("GET", "/history", 429, {}, 150)
        if failure == "empty":
            return PageScan((), 0, now, True)
        return PageScan((_sale(since),), 4, now, False)

    result = await synchronize_history(
        repository, _KEY, point, HistoryOperation.INCREMENTAL,
        _SETTINGS, read_exbo,
    )
    assert result.status is HistorySyncStatus.ERROR
    assert result.covered_from == point.covered_from
    assert result.covered_until == point.covered_until
    assert not repository.rows[_KEY]
    if failure == "rate_limit":
        assert result.retry_at >= now + timedelta(seconds=150)


async def test_resume_after_write_before_checkpoint() -> None:
    """Повторить запись после обрыва и сохранить кратность границы."""
    repository = _Repository()
    repository.fail_checkpoint = True
    now = datetime.now(UTC)
    incomplete_time = now - timedelta(hours=2)
    tail = _sale(incomplete_time)
    newest = _sale(now - timedelta(hours=1))

    async def read_exbo(
        since: datetime, before: datetime | None, progress: ScanProgress
    ) -> PageScan[Sale]:
        """Сначала обрезать последнюю отметку, затем вернуть её целиком.

        Args:
            since: Нижняя временная граница.
            before: Граница сохранённого участка.
            progress: Счётчики попыток.

        Returns:
            Подтверждённая часть истории или полный список.
        """
        if before is None:
            return PageScan((newest, tail), 3, now, True)
        assert before == incomplete_time + timedelta(microseconds=1)
        return PageScan((newest, tail, tail), 3, now, True)

    read: HistoryReader = read_exbo
    with pytest.raises(HistoryStorageError):
        await synchronize_history(
            repository, _KEY, HistoryCheckpoint(now), HistoryOperation.INITIAL,
            _SETTINGS, read,
        )
    assert repository.points[_KEY].covered_until is None
    assert list(repository.rows[_KEY].values()) == [newest]
    partial = await synchronize_history(
        repository, _KEY, repository.points[_KEY], HistoryOperation.INITIAL,
        _SETTINGS, read,
    )
    assert not partial.initial_complete
    assert len(repository.rows[_KEY]) == 1
    result = await synchronize_history(
        repository, _KEY, partial, HistoryOperation.INITIAL, _SETTINGS, read,
    )
    assert result.initial_complete and result.backfill_complete
    assert result.covered_from == incomplete_time
    assert list(repository.rows[_KEY].values()).count(tail) == 2
