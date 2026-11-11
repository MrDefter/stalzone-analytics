"""Управление подключением к двум базам аукциона."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from clickhouse_connect.driver.asyncclient import AsyncClient

from common.errors.clickhouse import HistoryStorageError
from common.settings.auction_storage import AuctionStorageSettings

from .client import CLICKHOUSE_FAILURES, connect_clickhouse
from .repository import AuctionHistoryRepository
from .scanner_repository import ScannerRepository
from .scanner_schema import initialize_scanner_schema
from .schema import initialize_schema

_LOGGER = logging.getLogger(__name__)


class _Connection:
    """Один клиент с репозиториями и счётчиком активных операций."""

    def __init__(
        self, client: AsyncClient, history: AuctionHistoryRepository,
        scanner: ScannerRepository,
    ) -> None:
        """Связать репозитории с одним открытым клиентом.

        Args:
            client: Подключение к ClickHouse.
            history: Репозиторий истории этого подключения.
            scanner: Репозиторий снимков этого подключения.
        """
        self.client = client
        self.repositories = (history, scanner)
        self.users = 0
        self.idle = asyncio.Event()
        self.idle.set()


class AuctionStorage:
    """Подключение с повторной инициализацией после сетевого сбоя."""

    history_repository = AuctionHistoryRepository
    scanner_repository = ScannerRepository

    def __init__(self, settings: AuctionStorageSettings) -> None:
        """Подготовить ленивое подключение.

        Args:
            settings: Учётные данные и имена баз.
        """
        self.settings = settings
        self._lock = asyncio.Lock()
        self._current: _Connection | None = None
        self._retired: set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        """Дождаться завершения сессий и закрыть все их подключения."""
        async with self._lock:
            if self._current is not None:
                self._retire(self._current)
            pending = tuple(self._retired)
        if pending:
            await asyncio.shield(asyncio.gather(*pending))

    def _retire(self, connection: _Connection) -> None:
        """Убрать сбойное подключение из выдачи новым сессиям.

        Args:
            connection: Подключение, сообщившее о сбое.
        """
        if connection is self._current:
            self._current = None
            task = asyncio.create_task(self._close_when_idle(connection))
            self._retired.add(task)
            task.add_done_callback(self._retired.discard)

    @staticmethod
    async def _close_when_idle(connection: _Connection) -> None:
        """Закрыть клиент после выхода всех использующих его сессий.

        Args:
            connection: Подключение, больше не выдаваемое новым сессиям.
        """
        await connection.idle.wait()
        await AuctionStorage._close_client(connection.client)

    @staticmethod
    async def _close_client(client: AsyncClient) -> None:
        """Освободить клиент и записать безопасные сведения о сбое.

        Args:
            client: Клиент без активных операций.
        """
        try:
            await client.close()
        except Exception as error:
            _LOGGER.warning("ClickHouse close error=%s", type(error).__name__)

    async def _open(self) -> _Connection:
        """Подключиться и создать репозитории после проверки схем.

        Returns:
            Готовое поколение подключения.

        Raises:
            HistoryStorageError: Учётные данные ClickHouse отсутствуют.
        """
        cfg = self.settings
        if not cfg.password.get_secret_value():
            raise HistoryStorageError("ClickHouse не настроен.")
        client = await connect_clickhouse(cfg)
        try:
            await initialize_schema(client, cfg.history_database)
            await initialize_scanner_schema(client, cfg.snapshots_database)
            return _Connection(
                client,
                self.history_repository(
                    client, cfg.history_database, cfg.ttl_days,
                ),
                self.scanner_repository(client, cfg.snapshots_database),
            )
        except BaseException:
            await self._close_client(client)
            raise

    @asynccontextmanager
    async def session(
        self,
    ) -> AsyncIterator[tuple[AuctionHistoryRepository, ScannerRepository]]:
        """Предоставить историю и снимки для одной операции.

        Yields:
            Репозитории истории и снимков.

        Raises:
            HistoryStorageError: Подключение или запрос завершились сбоем.
        """
        connection = None
        try:
            async with self._lock:
                if self._current is None:
                    self._current = await self._open()
                connection = self._current
                connection.users += 1
                connection.idle.clear()
            yield connection.repositories
        except (*CLICKHOUSE_FAILURES, HistoryStorageError):
            if connection is not None:
                self._retire(connection)
            raise HistoryStorageError("ClickHouse недоступен.") from None
        finally:
            if connection is not None:
                connection.users -= 1
                if not connection.users:
                    connection.idle.set()

