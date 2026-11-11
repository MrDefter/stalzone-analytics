"""Идемпотентная схема хранилища истории и точек синхронизации."""

import re
from hashlib import sha256

from clickhouse_connect.driver.asyncclient import AsyncClient

STATE_TABLE = "history_sync_state"
BATCH_COMMITS = "history_batch_commits"


def sales_table(region: str, item_id: str) -> str:
    """Получить безопасное имя таблицы для региона и точного ID.

    Args:
        region: Код региона аукциона.
        item_id: Регистрозависимый идентификатор предмета EXBO.

    Returns:
        Стабильное имя таблицы из ASCII-символов.

    Raises:
        ValueError: Код региона не соответствует формату EXBO.
    """
    if not re.fullmatch(r"[A-Z]{2,8}", region):
        raise ValueError("Недопустимый код региона аукциона.")
    suffix = item_id
    if not re.fullmatch(r"[A-Za-z0-9]{1,64}", item_id):
        suffix = f"id_{sha256(item_id.encode()).hexdigest()}"
    return f"auction_history_{region.lower()}_{suffix}"


async def initialize_schema(client: AsyncClient, database: str) -> None:
    """Создать базу и общую таблицу контрольных точек.

    Args:
        client: Открытый клиент ClickHouse.
        database: Проверенное имя базы из настроек.
    """
    await client.command(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    await client.command(f"""
        CREATE TABLE IF NOT EXISTS `{database}`.{STATE_TABLE}
        (
            item_id String,
            region LowCardinality(String),
            mode LowCardinality(String),
            updated_at DateTime64(6, 'UTC'),
            covered_from Nullable(DateTime64(6, 'UTC')),
            covered_until Nullable(DateTime64(6, 'UTC')),
            pending_from Nullable(DateTime64(6, 'UTC')),
            pending_until Nullable(DateTime64(6, 'UTC')),
            last_success_at Nullable(DateTime64(6, 'UTC')),
            initial_since Nullable(DateTime64(6, 'UTC')),
            initial_complete Bool,
            backfill_complete Bool,
            operation Nullable(String),
            status LowCardinality(String),
            last_error Nullable(String),
            last_error_type Nullable(String),
            last_error_at Nullable(DateTime64(6, 'UTC')),
            retry_at Nullable(DateTime64(6, 'UTC')),
            data_revision Nullable(DateTime64(6, 'UTC')),
            batch_revision Nullable(DateTime64(6, 'UTC'))
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (item_id, region, mode)
    """)
    await client.command(f"""
        ALTER TABLE `{database}`.{STATE_TABLE}
        ADD COLUMN IF NOT EXISTS data_revision Nullable(DateTime64(6, 'UTC'))
    """)
    await client.command(f"""
        ALTER TABLE `{database}`.{STATE_TABLE}
        ADD COLUMN IF NOT EXISTS batch_revision Nullable(DateTime64(6, 'UTC'))
    """)
    await client.command(f"""
        CREATE TABLE IF NOT EXISTS `{database}`.{BATCH_COMMITS}
        (
            item_id String,
            region LowCardinality(String),
            mode LowCardinality(String),
            batch_id String,
            committed_at DateTime64(6, 'UTC')
        )
        ENGINE = ReplacingMergeTree
        ORDER BY (item_id, region, mode, committed_at, batch_id)
        TTL toDateTime(committed_at, 'UTC') + INTERVAL 31 DAY DELETE
    """)


async def ensure_sales_table(
    client: AsyncClient, database: str, region: str, item_id: str,
    ttl_days: int, *, staged: bool = False,
) -> None:
    """Создать таблицу продаж предмета при его первой синхронизации.

    Args:
        client: Открытый клиент ClickHouse.
        database: Проверенное имя базы из настроек.
        region: Регион аукциона.
        item_id: Точный ID предмета EXBO.
        ttl_days: Срок хранения по времени завершения продажи.
        staged: Хранить порции отдельно до подтверждения записи.
    """
    suffix = "_batches" if staged else ""
    table = f"`{database}`.{sales_table(region, item_id)}{suffix}"
    ttl = f"toDateTime(sale_time, 'UTC') + toIntervalDay({ttl_days})"
    batch_column = "batch_id String," if staged else ""
    batch_key = ", batch_id" if staged else ""
    await client.command(f"""
        CREATE TABLE IF NOT EXISTS {table}
        (
            mode LowCardinality(String),
            sale_time DateTime64(6, 'UTC'),
            amount UInt64,
            price UInt64,
            additional String,
            loaded_at DateTime64(6, 'UTC'),
            event_signature FixedString(64),
            {batch_column}
            occurrence_index UInt32
        )
        ENGINE = ReplacingMergeTree
        PARTITION BY toYYYYMM(sale_time)
        ORDER BY (
            mode, sale_time, event_signature, occurrence_index{batch_key}
        )
        TTL {ttl} DELETE
    """)
    definition = await client.command(f"SHOW CREATE TABLE {table}")
    if f"toIntervalDay({ttl_days})" not in definition:
        await client.command(f"ALTER TABLE {table} MODIFY TTL {ttl}")
