"""Проверка смены подключения ClickHouse при пересечении сессий."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from common.database.clickhouse import auction_storage
from common.database.clickhouse.auction_storage import AuctionStorage
from common.errors.clickhouse import HistoryStorageError
from common.settings.auction_storage import AuctionStorageSettings


@pytest.mark.asyncio
async def test_failed_generation_drains_active_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Закрыть старый клиент после последней сессии и сохранить новый.

    Args:
        monkeypatch: Подмена подключения и инициализации схем ClickHouse.
    """
    first, second = AsyncMock(), AsyncMock()
    connect = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(auction_storage, "connect_clickhouse", connect)
    monkeypatch.setattr(auction_storage, "initialize_schema", AsyncMock())
    monkeypatch.setattr(
        auction_storage, "initialize_scanner_schema", AsyncMock(),
    )
    storage = AuctionStorage(AuctionStorageSettings(
        _env_file=None, password="fixture",
    ))
    try:
        with pytest.raises(HistoryStorageError):
            async with storage.session() as original:
                with pytest.raises(HistoryStorageError):
                    async with storage.session() as failed:
                        assert failed is original
                        raise OSError("temporary outage")
                first.close.assert_not_awaited()
                async with storage.session() as replacement:
                    assert replacement is not original
                first.close.assert_not_awaited()
                raise OSError("old connection")
        async with storage.session() as current:
            assert current is replacement
            closing = asyncio.create_task(storage.close())
            await asyncio.sleep(0)
            assert not closing.done()
            second.close.assert_not_awaited()
        await closing
        assert connect.await_count == 2
    finally:
        await storage.close()
    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
