"""Чтение продаж и контрольных точек ClickHouse."""

import json
from dataclasses import fields
from datetime import UTC, datetime

from clickhouse_connect.driver.asyncclient import AsyncClient

from common.dataclasses.auction_history import (
    HistoryCheckpoint,
    HistoryKey,
    StoredHistory,
)
from common.enums.auction_history import HistoryOperation, HistorySyncStatus
from common.models.auction import Sale

from .schema import BATCH_COMMITS, STATE_TABLE, sales_table

_STATE_COLUMNS = tuple(field.name for field in fields(HistoryCheckpoint))
_TIME_FILTER = """
    mode = {mode:String}
    AND sale_time >= toDateTime64({since:String}, 6, 'UTC')
    AND sale_time <= toDateTime64({until:String}, 6, 'UTC')
"""


def _period(
    key: HistoryKey, since: datetime, until: datetime
) -> dict[str, str]:
    """Подготовить параметры запроса с точностью до микросекунд.

    Args:
        key: Предмет, регион и режим EXBO.
        since: Нижняя включённая граница.
        until: Верхняя включённая граница.

    Returns:
        Связанные параметры SQL в UTC.
    """
    return {
        "mode": key.mode.value,
        "since": since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
        "until": until.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
    }


class AuctionHistoryRepository:
    """Чтение истории по предмету, региону и режиму EXBO."""

    def __init__(
        self, client: AsyncClient, database: str, ttl_days: int
    ) -> None:
        """Сохранить открытое подключение и имя базы.

        Args:
            client: Асинхронный клиент под управлением веб-сервиса.
            database: Проверенное имя базы из настроек.
            ttl_days: Срок хранения завершённых сделок.
        """
        self._client = client
        self._database = database
        self._ttl_days = ttl_days

    async def get_checkpoint(
        self, key: HistoryKey
    ) -> HistoryCheckpoint | None:
        """Загрузить последнюю контрольную точку указанного источника.

        Args:
            key: Предмет, регион и режим EXBO.

        Returns:
            Контрольная точка или None до первой записи.
        """
        result = await self._client.query(
            f"""
            SELECT {", ".join(_STATE_COLUMNS)}
            FROM `{self._database}`.{STATE_TABLE} FINAL
            WHERE item_id = {{item_id:String}}
                AND region = {{region:String}}
                AND mode = {{mode:String}}
            """,
            parameters={
                "item_id": key.item_id,
                "region": key.region,
                "mode": key.mode.value,
            },
        )
        if not result.result_rows:
            return None
        values = {
            name: value.replace(tzinfo=UTC)
            if isinstance(value, datetime) else value
            for name, value in zip(_STATE_COLUMNS, result.result_rows[0])
        }
        values["status"] = HistorySyncStatus(values["status"])
        if values["operation"] is not None:
            values["operation"] = HistoryOperation(values["operation"])
        return HistoryCheckpoint(**values)

    async def read_history(
        self, key: HistoryKey, since: datetime, until: datetime
    ) -> StoredHistory:
        """Прочитать сохранённые продажи и границы покрытия периода.

        Args:
            key: Предмет, регион и режим EXBO.
            since: Нижняя включённая граница.
            until: Верхняя включённая граница.

        Returns:
            Продажи от новых к старым и сохранённая контрольная точка.
        """
        checkpoint = await self.get_checkpoint(key)
        return await self.read_at(key, since, until, checkpoint)

    async def read_at(
        self, key: HistoryKey, since: datetime, until: datetime,
        checkpoint: HistoryCheckpoint | None,
    ) -> StoredHistory:
        """Прочитать сделки, видимые на переданной контрольной точке.

        Args:
            key: Предмет, регион и режим.
            since: Нижняя граница.
            until: Верхняя граница.
            checkpoint: Фиксированная ревизия для графика и расчёта.

        Returns:
            Набор сделок для указанной ревизии.
        """
        if checkpoint is None:
            return StoredHistory((), None)
        rows = await self._visible_rows(
            key, since, until, "sale_time, amount, price, additional",
            checkpoint.batch_revision, order="sale_time DESC",
        )
        return StoredHistory(tuple(
            Sale.model_construct(
                time=at.replace(tzinfo=UTC), amount=amount, price=price,
                additional=json.loads(additional),
            )
            for at, amount, price, additional in rows
        ), checkpoint)

    async def _visible_rows(
        self, key: HistoryKey, since: datetime, until: datetime,
        columns: str, revision: datetime | None, *, order: str = "",
    ) -> list[tuple]:
        """Читать исходные записи и опубликованные порции до ревизии.

        Args:
            key: Предмет, регион и режим.
            since: Нижняя граница.
            until: Верхняя граница.
            columns: Внутренний список полей SQL.
            revision: Верхняя граница подтверждённых порций.
            order: Внутренний порядок строк, если он нужен.

        Returns:
            Строки SQL без незавершённых порций.
        """
        table = sales_table(key.region, key.item_id)
        parameters = _period(key, since, until)
        query = f"""
            SELECT {columns}
            FROM `{self._database}`.{table} FINAL
            WHERE {_TIME_FILTER}
        """
        if revision is not None:
            parameters.update(
                item=key.item_id, region=key.region, revision=(
                    revision.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
                ),
            )
            query += f"""
                UNION ALL
                SELECT {columns}
                FROM `{self._database}`.{table}_batches FINAL
                WHERE {_TIME_FILTER}
                    AND batch_id IN (
                        SELECT batch_id
                        FROM `{self._database}`.{BATCH_COMMITS}
                        WHERE item_id = {{item:String}}
                          AND region = {{region:String}}
                          AND mode = {{mode:String}}
                          AND committed_at <=
                              toDateTime64({{revision:String}}, 6, 'UTC')
                    )
            """
        if order:
            query = f"SELECT * FROM ({query}) ORDER BY {order}"
        result = await self._client.query(query, parameters=parameters)
        return result.result_rows
