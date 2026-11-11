"""Проверка публикации порций истории на локальном ClickHouse."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from auction_engine.backend.storage import EngineStorage
from common.database.clickhouse.client import connect_clickhouse
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.enums.auction import AuctionMode
from common.models.auction import Sale
from auction_engine.backend.engine_settings import AuctionEngineSettings


async def main() -> None:
    """Проверить чтение до и после публикации на временных базах."""
    local = AuctionEngineSettings()
    if local.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Проверка предназначена для локального ClickHouse.")
    suffix = uuid4().hex
    settings = local.model_copy(update={
        "history_database": "smoke_history_" + suffix,
        "snapshots_database": "smoke_snapshots_" + suffix,
    })
    storage = EngineStorage(settings)
    key = HistoryKey("smoke", "RU", AuctionMode.DEMO)
    now = datetime.now(UTC)
    start = now - timedelta(hours=1)
    sale = Sale(
        amount=10, price=2**53 + 1,
        time=(now - timedelta(minutes=5)).isoformat(),
    )
    orphan = sale.model_copy(update={"price": sale.price + 1})
    new = sale.model_copy(update={"price": sale.price + 2})
    point = HistoryCheckpoint(now, covered_from=start, covered_until=now)

    try:
        async with storage.session() as (history, _):
            await history.ensure_item(key)
            added, first = await history.commit_sales(key, (sale, sale), point)
            assert added == 2
            added = await history.stage_sales(
                key, (orphan,), uuid4().hex, first.batch_revision,
            )
            assert added == 1
            before = await history.read_at(key, start, now, first)
            assert before.rows == (sale, sale)

            added, second = await history.commit_sales(
                key, (sale, sale, new),
                replace(first, updated_at=datetime.now(UTC)),
            )
            assert added == 1 and second.batch_revision != first.batch_revision
            pinned = await history.read_at(key, start, now, first)
            assert pinned.rows == before.rows
            latest = await history.read_history(key, start, now)
            assert latest.rows.count(sale) == 2 and new in latest.rows
            assert orphan not in latest.rows

            added, third = await history.commit_sales(
                key, (sale, sale, new, orphan),
                replace(second, updated_at=datetime.now(UTC)),
            )
            assert added == 1
            replayed, _ = await history.commit_sales(
                key, (sale, sale, new, orphan),
                replace(third, updated_at=datetime.now(UTC)),
            )
            assert replayed == 0
            final = await history.read_history(key, start, now)
            assert final.rows.count(sale) == 2
            assert final.rows.count(orphan) == final.rows.count(new) == 1
        print("Ревизии истории, незавершённая запись и повтор проверены.")
    finally:
        await storage.close()
        client = await connect_clickhouse(local)
        try:
            for database in (
                settings.history_database, settings.snapshots_database,
            ):
                await client.command(
                    f"DROP DATABASE IF EXISTS {database} SYNC"
                )
        finally:
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
