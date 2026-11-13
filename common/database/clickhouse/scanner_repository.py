"""Чтение версий снимков и запись состояния аукционного ядра."""

import json
from datetime import UTC, datetime, timedelta

from clickhouse_connect.driver.asyncclient import AsyncClient
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import HistoryKey
from common.enums.auction import AuctionMode
from common.errors.clickhouse import HistoryStorageError
from common.models.auction import Lot
from common.models.auction_engine import ScannerCommand, SnapshotMetadata
from common.models.analysis_result import AnalysisResult

_KEY = (
    "item_id = {item:String} AND region = {region:String} "
    "AND mode = {mode:String}"
)
_SNAPSHOT = "snapshot_id = {snapshot:String} AND expires_at > now()"
_PROFILE = "strategy_key = {strategy:String}"
_WRITE = {"async_insert": 0}
_COMMAND_TTL = timedelta(minutes=15)
MAX_TEMPORARY_PROFILES = 32


class ScannerRepository:
    """Чтение снимков и обмен командами через ClickHouse."""

    def __init__(self, client: AsyncClient, database: str) -> None:
        """Привязать репозиторий к подключению.

        Args:
            client: Клиент ClickHouse.
            database: Проверенное имя базы снимков.
        """
        self._client = client
        self._database = database

    async def _select(
        self, sql: str, key: HistoryKey | None = None, **parameters: str,
    ) -> list[tuple]:
        """Прочитать строки с привязкой параметров.

        Args:
            sql: Внутренний шаблон запроса с именем базы.
            key: Ключ предмета, если запрос ограничен предметом.
            **parameters: Дополнительные SQL-параметры.

        Returns:
            Строки результата.
        """
        if key is not None:
            parameters.update(item=key.item_id, region=key.region,
                              mode=key.mode.value)
        result = await self._client.query(
            sql.replace("@db@", f"`{self._database}`"),
            parameters=parameters,
        )
        return result.result_rows

    async def _insert(
        self, table: str, key: HistoryKey, columns: tuple[str, ...],
        rows: list[tuple],
    ) -> None:
        """Подтвердить вставку порции для одного ключа.

        Args:
            table: Внутреннее имя таблицы.
            key: Предмет, регион и режим.
            columns: Поля без общего ключа.
            rows: Данные для записи.
        """
        if rows:
            prefix = (key.item_id, key.region, key.mode.value)
            await self._client.insert(
                table, [prefix + row for row in rows],
                column_names=("item_id", "region", "mode", *columns),
                database=self._database, settings=_WRITE,
            )

    async def latest(self, key: HistoryKey) -> SnapshotMetadata | None:
        """Найти последнюю доступную опубликованную версию.

        Args:
            key: Предмет, регион и режим.

        Returns:
            Маркер актуального по TTL снимка или None.
        """
        rows = await self._select(f"""
            SELECT payload FROM @db@.auction_snapshots FINAL
            WHERE {_KEY} AND expires_at > now()
            ORDER BY version DESC LIMIT 1
        """, key)
        if rows:
            return SnapshotMetadata.model_validate_json(rows[0][0])
        return None

    async def lots(
        self, key: HistoryKey, meta: SnapshotMetadata,
    ) -> PageScan[Lot]:
        """Восстановить исходные лоты в порядке их позиций.

        Args:
            key: Предмет, регион и режим.
            meta: Снимок, к которому относятся лоты.

        Returns:
            Лоты и признаки качества сборщика.
        """
        rows = await self._select(f"""
            SELECT payload FROM @db@.snapshot_lots FINAL
            WHERE {_KEY} AND {_SNAPSHOT} ORDER BY source_index
        """, key, snapshot=meta.snapshot_id)
        if len(rows) != meta.lots_loaded:
            raise HistoryStorageError("Снимок лотов недоступен целиком.")
        return PageScan(
            tuple(Lot.model_validate_json(row[0]) for row in rows),
            meta.lots_reported_total, meta.received_at, meta.lots_consistent,
        )

    async def analysis(
        self, key: HistoryKey, meta: SnapshotMetadata, strategy: str,
    ) -> AnalysisResult | None:
        """Прочитать последнюю готовую оценку точного профиля.

        Args:
            key: Предмет, регион и режим.
            meta: Версия исходных лотов.
            strategy: Ключ профиля и версии входной истории.

        Returns:
            Аналитика или None до записи финальной строки профиля.
        """
        rows = await self._select(f"""
            SELECT analysis_version, payload FROM @db@.snapshot_analysis FINAL
            WHERE {_KEY} AND {_SNAPSHOT} AND {_PROFILE} AND position = -1
            ORDER BY version DESC LIMIT 1
        """, key, snapshot=meta.snapshot_id, strategy=strategy)
        if not rows:
            return None
        revision, payload = rows[0]
        aggregates = json.loads(payload)
        lots = await self._select(f"""
            SELECT payload FROM @db@.snapshot_analysis FINAL
            WHERE {_KEY} AND {_SNAPSHOT} AND {_PROFILE}
              AND analysis_version = {{revision:String}} AND position >= 0
            ORDER BY position
        """, key, snapshot=meta.snapshot_id, strategy=strategy,
            revision=revision)
        if len(lots) != aggregates.pop("result_count"):
            return None
        aggregates["lots"] = [json.loads(row[0]) for row in lots]
        aggregates["analysis_version"] = revision
        return AnalysisResult.model_validate(aggregates)

    async def analysis_version(
        self, key: HistoryKey, meta: SnapshotMetadata, strategy: str,
    ) -> str | None:
        """Прочитать только идентификатор готового профиля.

        Args:
            key: Предмет, регион и режим.
            meta: Версия исходных лотов.
            strategy: Ключ профиля и истории.

        Returns:
            Идентификатор опубликованного расчёта или None.
        """
        rows = await self._select(f"""
            SELECT analysis_version FROM @db@.snapshot_analysis FINAL
            WHERE {_KEY} AND {_SNAPSHOT} AND {_PROFILE} AND position = -1
            ORDER BY version DESC LIMIT 1
        """, key, snapshot=meta.snapshot_id, strategy=strategy)
        return rows[0][0] if rows else None

    async def temporary_profiles(
        self, key: HistoryKey, meta: SnapshotMetadata,
    ) -> int:
        """Посчитать доступные временные профили исходной версии.

        Args:
            key: Предмет, регион и режим.
            meta: Исходный опубликованный снимок.

        Returns:
            Число готовых пользовательских профилей до их истечения.
        """
        rows = await self._select(f"""
            SELECT uniqExact(strategy_key) FROM @db@.snapshot_analysis FINAL
            WHERE {_KEY} AND {_SNAPSHOT} AND temporary AND position = -1
        """, key, snapshot=meta.snapshot_id)
        return rows[0][0]

    async def latest_state(self, table: str, key: HistoryKey) -> str | None:
        """Прочитать текущую запись состояния или настроек.

        Args:
            table: Внутреннее имя служебной таблицы.
            key: Ключ записи.

        Returns:
            Сериализованное значение или None.
        """
        rows = await self._select(
            f"SELECT payload FROM @db@.{table} FINAL WHERE {_KEY}", key,
        )
        return rows[0][0] if rows else None

    async def commands(self) -> list[tuple[HistoryKey, ScannerCommand]]:
        """Прочитать незавершённые команды, доступные планировщику.

        Returns:
            Очередь команд вместе с ключами предметов.
        """
        rows = await self._select("""
            SELECT item_id, region, mode, payload
            FROM @db@.scanner_commands FINAL
            WHERE status = 'pending' AND expires_at > now()
            ORDER BY version
        """)
        return [
            (HistoryKey(item, region, AuctionMode(mode)),
             ScannerCommand.model_validate_json(payload))
            for item, region, mode, payload in rows
        ]

    async def command_status(
        self, key: HistoryKey, request_id: str,
    ) -> str | None:
        """Найти последнее состояние конкретной команды.

        Args:
            key: Предмет, регион и режим.
            request_id: Идентификатор команды.

        Returns:
            Статус команды или None.
        """
        result = await self.command_result(key, request_id)
        return result["status"] if result else None

    async def command_result(
        self, key: HistoryKey, request_id: str,
    ) -> dict[str, str | None] | None:
        """Прочитать состояние и безопасный код ошибки.

        Args:
            key: Предмет, регион и режим.
            request_id: Идентификатор команды.

        Returns:
            Результат или None, если команда отсутствует.
        """
        rows = await self._select(f"""
            SELECT status, error_type, payload FROM @db@.scanner_commands FINAL
            WHERE {_KEY} AND request_id = {{request:String}}
                AND expires_at > now()
        """, key, request=request_id)
        if rows:
            status, error, payload = rows[0]
            result = {"status": status, "error": error or None}
            command = json.loads(payload)
            if command.get("result_snapshot_id"):
                result["snapshot_id"] = command["result_snapshot_id"]
                result["received_at"] = command["result_received_at"]
            if command.get("result_analysis_version"):
                result["analysis_version"] = command["result_analysis_version"]
            return result
        return None

    async def record_command(
        self, key: HistoryKey, command: ScannerCommand,
        status: str = "pending", error: str = "",
    ) -> None:
        """Добавить состояние команды с ограниченным сроком жизни.

        Args:
            key: Предмет, регион и режим.
            command: Исходный запрос.
            status: Текущее состояние обработки.
            error: Безопасный код ошибки при отказе.
        """
        await self._insert("scanner_commands", key, (
            "request_id", "version", "expires_at", "status", "payload",
            "error_type",
        ), [(command.request_id, datetime.now(UTC),
             command.created_at + _COMMAND_TTL, status,
             command.model_dump_json(), error)])
