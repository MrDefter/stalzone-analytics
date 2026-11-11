"""Подключение к ClickHouse через нативный асинхронный драйвер."""

import clickhouse_connect
from aiohttp import ClientError
from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.exceptions import ClickHouseError

from common.errors.clickhouse import HistoryStorageError
from common.settings.clickhouse import ClickHouseSettings

CLICKHOUSE_FAILURES = (ClickHouseError, ClientError, OSError, TimeoutError)


async def connect_clickhouse(settings: ClickHouseSettings) -> AsyncClient:
    """Открыть HTTP-подключение и проверить выполнение простого запроса.

    Args:
        settings: Адрес и учётные данные локального сервера.

    Returns:
        Проверенное подключение под управлением вызывающего кода.

    Raises:
        HistoryStorageError: Не удалось подключиться к ClickHouse.
    """
    client = None
    try:
        client = await clickhouse_connect.get_async_client(
            host=settings.host,
            port=settings.port,
            interface="http",
            username=settings.user,
            password=settings.password.get_secret_value(),
            database="default",
            connect_timeout=2,
            send_receive_timeout=10,
            query_retries=0,
            autogenerate_session_id=False,
        )
        if await client.command("SELECT 1") != 1:
            raise HistoryStorageError("ClickHouse не прошёл проверку.")
    except BaseException as error:
        if client is not None:
            await client.close()
        if isinstance(error, CLICKHOUSE_FAILURES):
            raise HistoryStorageError("ClickHouse недоступен.") from None
        raise
    return client
