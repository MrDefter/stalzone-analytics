"""Подключение ядра с доступом к публикации истории и снимков."""

from common.database.clickhouse.auction_storage import AuctionStorage

from .history_writer import HistoryWriter
from .snapshot_writer import SnapshotWriter


class EngineStorage(AuctionStorage):
    """Создать репозитории записи только в процессе ядра."""

    history_repository = HistoryWriter
    scanner_repository = SnapshotWriter
