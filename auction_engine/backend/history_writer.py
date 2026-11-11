"""Запись продаж и контрольных точек истории."""

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import astuple, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from clickhouse_connect.driver.asyncclient import AsyncClient

from common.database.clickhouse.repository import (
    _STATE_COLUMNS,
    AuctionHistoryRepository,
)
from common.database.clickhouse.schema import (
    BATCH_COMMITS,
    STATE_TABLE,
    ensure_sales_table,
    sales_table,
)
from common.dataclasses.auction_history import HistoryCheckpoint, HistoryKey
from common.models.auction import Sale

_INSERT_SETTINGS = {"async_insert": 0}


def _sale_rows(
    key: HistoryKey, sales: Sequence[Sale]
) -> list[tuple[str, datetime, int, int, str, datetime, bytes, int]]:
    """Собрать строки с устойчивым ключом для повторных вставок.

    Args:
        key: Источник продаж.
        sales: Полученные завершённые продажи одного предмета.

    Returns:
        Строки с отдельным номером для каждой одинаковой продажи.
    """
    rows = []
    occurrences: Counter[bytes] = Counter()
    loaded_at = datetime.now(UTC)
    for sale in sales:
        additional = json.dumps(
            sale.additional, sort_keys=True, separators=(",", ":")
        )
        at = sale.time.astimezone(UTC).isoformat(timespec="microseconds")
        content = f"{at}:{sale.amount}:{sale.price}:{additional}"
        signature = sha256(content.encode()).hexdigest().encode("ascii")
        occurrences[signature] += 1
        rows.append((
            key.mode.value, sale.time, sale.amount, sale.price, additional,
            loaded_at, signature, occurrences[signature],
        ))
    return rows


class HistoryWriter(AuctionHistoryRepository):
    """Хранилище истории со стороны фонового ядра."""

    def __init__(
        self, client: AsyncClient, database: str, ttl_days: int,
    ) -> None:
        """Подготовить кеш схемы таблиц предметов.

        Args:
            client: Подключение фонового ядра.
            database: Проверенное имя базы истории.
            ttl_days: Срок хранения продаж.
        """
        super().__init__(client, database, ttl_days)
        self._tables: set[str] = set()

    async def ensure_item(self, key: HistoryKey) -> None:
        """Подготовить таблицу предмета и применить срок хранения.

        Args:
            key: Предмет, регион и режим EXBO.
        """
        table = sales_table(key.region, key.item_id)
        if table not in self._tables:
            await ensure_sales_table(
                self._client, self._database, key.region, key.item_id,
                self._ttl_days,
            )
            await ensure_sales_table(
                self._client, self._database, key.region, key.item_id,
                self._ttl_days, staged=True,
            )
            self._tables.add(table)

    async def _latest_commit(self, key: HistoryKey) -> datetime | None:
        """Найти последний подтверждённый маркер порции.

        Args:
            key: Предмет, регион и режим.

        Returns:
            Ревизия последней записанной порции, если она существует.
        """
        result = await self._client.query(f"""
            SELECT maxOrNull(committed_at)
            FROM `{self._database}`.{BATCH_COMMITS}
            WHERE item_id = {{item:String}} AND region = {{region:String}}
              AND mode = {{mode:String}}
        """, parameters={
            "item": key.item_id, "region": key.region, "mode": key.mode.value,
        })
        latest = result.result_rows[0][0]
        return latest.replace(tzinfo=UTC) if latest is not None else None

    async def stage_sales(
        self, key: HistoryKey, sales: Sequence[Sale], batch_id: str,
        latest: datetime | None,
    ) -> int:
        """Записать новые вхождения, ожидающие маркера порции.

        Args:
            key: Предмет, регион и режим.
            sales: Подтверждённые полные группы сделок.
            batch_id: Идентификатор попытки записи.
            latest: Ревизия для проверки уже подтверждённых вхождений.

        Returns:
            Число новых строк в порции.
        """
        if not sales:
            return 0
        seen = set(await self._visible_rows(
            key, min(sale.time for sale in sales),
            max(sale.time for sale in sales),
            "event_signature, occurrence_index", latest,
        ))
        rows = [
            row + (batch_id,) for row in _sale_rows(key, sales)
            if row[-2:] not in seen
        ]
        if rows:
            table = sales_table(key.region, key.item_id) + "_batches"
            await self._client.insert(
                table, rows, database=self._database,
                settings=_INSERT_SETTINGS,
                column_names=(
                    "mode", "sale_time", "amount", "price", "additional",
                    "loaded_at", "event_signature", "occurrence_index",
                    "batch_id",
                ),
            )
        return len(rows)

    async def commit_sales(
        self, key: HistoryKey, sales: Sequence[Sale], point: HistoryCheckpoint,
    ) -> tuple[int, HistoryCheckpoint]:
        """Подтвердить порцию и продвинуть видимую ревизию истории.

        Args:
            key: Предмет, регион и режим.
            sales: Сделки успешной операции синхронизации.
            point: Следующее состояние с проверенными границами.

        Returns:
            Число добавленных строк и опубликованная контрольная точка.
        """
        latest = await self._latest_commit(key)
        batch_id = uuid4().hex
        added = await self.stage_sales(key, sales, batch_id, latest)
        if added:
            revision = datetime.now(UTC)
            if latest is not None and revision <= latest:
                revision = latest + timedelta(microseconds=1)
            await self._client.insert(
                BATCH_COMMITS, [(
                    key.item_id, key.region, key.mode.value,
                    batch_id, revision,
                )], database=self._database, settings=_INSERT_SETTINGS,
                column_names=(
                    "item_id", "region", "mode", "batch_id", "committed_at",
                ),
            )
            latest = revision
        if latest is not None:
            point = replace(point, data_revision=latest, batch_revision=latest)
        await self.set_checkpoint(key, point)
        return added, point

    async def set_checkpoint(
        self, key: HistoryKey, point: HistoryCheckpoint
    ) -> None:
        """Записать состояние после подтверждения соответствующих сделок.

        Args:
            key: Предмет, регион и режим EXBO.
            point: Последняя проверенная граница и состояние операции.
        """
        await self._client.insert(
            STATE_TABLE,
            [(key.item_id, key.region, key.mode.value, *astuple(point))],
            column_names=("item_id", "region", "mode", *_STATE_COLUMNS),
            database=self._database, settings=_INSERT_SETTINGS,
        )
