"""Создание хранилища опубликованных снимков и команд."""

from clickhouse_connect.driver.asyncclient import AsyncClient

_KEY = (
    "item_id String, region LowCardinality(String), "
    "mode LowCardinality(String)"
)
_ORDER = "item_id, region, mode"


async def initialize_scanner_schema(
    client: AsyncClient, database: str,
) -> None:
    """Создать таблицы ядра с независимыми сроками хранения.

    Args:
        client: Подключение к ClickHouse.
        database: Проверенное имя базы снимков.
    """
    await client.command(f"CREATE DATABASE IF NOT EXISTS `{database}`")
    exp = "expires_at DateTime('UTC')"
    stamp = "version DateTime64(6, 'UTC')"
    tables = {
        "auction_snapshots": (
            f"{_KEY}, snapshot_id String, {stamp}, {exp}, "
            "started_at DateTime64(6, 'UTC'), "
            "received_at DateTime64(6, 'UTC'), "
            "evaluated_at DateTime64(6, 'UTC'), "
            "published_at DateTime64(6, 'UTC'), "
            "lots_loaded UInt32, lots_reported_total UInt64, "
            "lots_complete Bool, "
            "lots_consistent Bool, status LowCardinality(String), "
            "error_type Nullable(String), payload String",
            "snapshot_id", "version", True,
        ),
        "snapshot_lots": (
            f"{_KEY}, snapshot_id String, source_index UInt32, "
            f"{exp}, amount Int64, buyout_price Nullable(Int128), "
            "start_price Int128, end_time DateTime64(6, 'UTC'), "
            "payload String",
            "snapshot_id, source_index", "", True,
        ),
        "snapshot_analysis": (
            f"{_KEY}, snapshot_id String, strategy_key String, "
            f"analysis_version String, position Int32, source_index Int32, "
            f"{stamp}, {exp}, temporary Bool, "
            "evaluated_at DateTime64(6, 'UTC'), "
            "status Nullable(String), reasons Array(String), "
            "profit_total Nullable(Int128), roi_percent Nullable(Float64), "
            "exit_price_per_pack Nullable(Int128), "
            "net_revenue_total Nullable(Int128), "
            "commission_total Nullable(Int128), max_buyout Nullable(Int128), "
            "payload String",
            "snapshot_id, strategy_key, analysis_version, position", "version",
            True,
        ),
        "scanner_state": (
            f"{_KEY}, {stamp}, payload String", "", "version", False,
        ),
        "scanner_settings": (
            f"{_KEY}, {stamp}, payload String", "", "version", False,
        ),
        "scanner_commands": (
            f"{_KEY}, request_id String, {stamp}, {exp}, status String, "
            "payload String, error_type String", "request_id", "version", True,
        ),
    }
    for table, (columns, suffix, version, ttl) in tables.items():
        order = f"{_ORDER}, {suffix}" if suffix else _ORDER
        expiry = "TTL expires_at DELETE" if ttl else ""
        await client.command(f"""
            CREATE TABLE IF NOT EXISTS `{database}`.{table} ({columns})
            ENGINE = ReplacingMergeTree({version})
            ORDER BY ({order})
            {expiry}
        """)
