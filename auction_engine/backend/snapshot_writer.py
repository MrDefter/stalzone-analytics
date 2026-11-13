"""Подтверждённая запись лотов, профилей и снимков."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel

from common.database.clickhouse.scanner_repository import (
    _KEY,
    _PROFILE,
    _SNAPSHOT,
    ScannerRepository,
)
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import HistoryKey
from common.errors.clickhouse import HistoryStorageError
from common.models.analysis_result import AnalysisResult
from common.models.auction import Lot
from common.models.auction_engine import SnapshotMetadata


class SnapshotWriter(ScannerRepository):
    """Публикация версии отдельным финальным маркером."""

    async def store_lots(
        self, key: HistoryKey, meta: SnapshotMetadata, scan: PageScan[Lot],
    ) -> None:
        """Записать исходные строки и проверить количество перед публикацией.

        Args:
            key: Предмет, регион и режим.
            meta: Метаданные подготавливаемой версии.
            scan: Полученные страницы активных лотов.
        """
        await self._insert("snapshot_lots", key, (
            "snapshot_id", "expires_at", "source_index", "amount",
            "buyout_price", "start_price", "end_time", "payload",
        ), [
            (meta.snapshot_id, meta.expires_at, index, lot.amount,
             lot.buyout_price, lot.start_price, lot.end_time,
             lot.model_dump_json(by_alias=True))
            for index, lot in enumerate(scan.rows)
        ])
        await self.lots(key, meta)

    async def publish(
        self, key: HistoryKey, meta: SnapshotMetadata,
    ) -> None:
        """Разрешить чтение версии после записи исходных данных и расчёта.

        Args:
            key: Предмет, регион и режим.
            meta: Проверенные метаданные версии.
        """
        await self._insert("auction_snapshots", key, (
            "snapshot_id", "version", "expires_at", "started_at",
            "received_at", "evaluated_at", "published_at", "lots_loaded",
            "lots_reported_total", "lots_complete", "lots_consistent",
            "status", "error_type", "payload",
        ), [(
            meta.snapshot_id, meta.published_at, meta.expires_at,
            meta.started_at, meta.received_at, meta.evaluated_at,
            meta.published_at, meta.lots_loaded, meta.lots_reported_total,
            meta.lots_complete, meta.lots_consistent, meta.status,
            meta.error_type, meta.model_dump_json(),
        )])

    async def store_analysis(
        self, key: HistoryKey, meta: SnapshotMetadata, strategy: str,
        result: AnalysisResult, *, temporary: bool = False,
    ) -> str:
        """Сохранить расчёт и подтвердить его отдельной строкой агрегатов.

        Args:
            key: Предмет, регион и режим.
            meta: Версия исходных лотов.
            strategy: Ключ входов расчёта.
            result: Оценка лотов и общие показатели.
            temporary: Ограничить срок хранения пользовательского профиля.

        Returns:
            Идентификатор опубликованной оценки.
        """
        revision = uuid4().hex
        stamp = datetime.now(UTC)
        expires = meta.expires_at
        if temporary:
            expires = min(expires, stamp + timedelta(minutes=30))
        prefix = (meta.snapshot_id, strategy, revision)
        columns = (
            "snapshot_id", "strategy_key", "analysis_version", "position",
            "source_index", "version", "expires_at", "temporary",
            "evaluated_at",
            "status", "reasons", "profit_total", "roi_percent",
            "exit_price_per_pack", "net_revenue_total", "commission_total",
            "max_buyout", "payload",
        )
        await self._insert("snapshot_analysis", key, columns, [
            prefix + (
                position, lot.id - 1, stamp, expires, temporary,
                result.evaluated_at,
                lot.status, list(lot.reasons), lot.profit_total,
                lot.roi_percent, lot.exit_price_per_pack,
                lot.net_revenue_total,
                lot.commission_total, lot.max_buyout, lot.model_dump_json(),
            )
            for position, lot in enumerate(result.lots)
        ])
        counts = await self._select(f"""
            SELECT count() FROM @db@.snapshot_analysis FINAL
            WHERE {_KEY} AND {_SNAPSHOT} AND {_PROFILE}
              AND analysis_version = {{revision:String}} AND position >= 0
        """, key, snapshot=meta.snapshot_id, strategy=strategy,
            revision=revision)
        if counts[0][0] != len(result.lots):
            raise HistoryStorageError("Записана неполная оценка снимка.")
        aggregate = result.model_dump(mode="json", exclude={"lots"})
        aggregate["result_count"] = len(result.lots)
        await self._insert("snapshot_analysis", key, columns, [
            prefix + (
                -1, -1, stamp, expires, temporary, result.evaluated_at,
                None, [],
                None, None, None, None, None, None, json.dumps(aggregate),
            ),
        ])

        return revision

    async def store_state(
        self, table: str, key: HistoryKey, value: BaseModel,
    ) -> None:
        """Добавить версию настроек, состояния предмета или ядра.

        Args:
            table: Внутреннее имя служебной таблицы.
            key: Ключ записи.
            value: Типизированное значение.
        """
        await self._insert(table, key, ("version", "payload"), [
            (datetime.now(UTC), value.model_dump_json()),
        ])
