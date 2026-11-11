"""Счётчики и состояние текущего чтения страниц EXBO."""

from dataclasses import dataclass


@dataclass(slots=True)
class ScanProgress:
    """Текущий этап и счётчики загрузки одного ресурса аукциона."""

    stage: str = "initial_page"
    offset: int = 0
    pages_attempted: int = 0
    pages_succeeded: int = 0
    rows_received: int = 0
