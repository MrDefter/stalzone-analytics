"""Параметры сортировки лотов EXBO."""

from enum import IntEnum, StrEnum


class AuctionMode(StrEnum):
    """Режим обращения к серверу EXBO."""

    DEMO = "demo"
    PRODUCTION = "production"


class HistoryDays(IntEnum):
    """Длительность окна графиков в сутках."""

    DAY = 1
    WEEK = 7
    TWO_WEEKS = 14
    MONTH = 30


class LotSort(StrEnum):
    """Поле сортировки активных лотов."""

    TIME_CREATED = "time_created"
    TIME_LEFT = "time_left"
    CURRENT_PRICE = "current_price"
    BUYOUT_PRICE = "buyout_price"


class SortOrder(StrEnum):
    """Направление сортировки лотов."""

    ASC = "asc"
    DESC = "desc"
