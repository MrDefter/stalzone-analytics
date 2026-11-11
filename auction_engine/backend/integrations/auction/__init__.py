"""Публичный интерфейс клиента аукциона EXBO."""

from common.dataclasses import PageResult
from common.enums.auction import LotSort, SortOrder
from auction_engine.backend.integrations.auction.errors import (
    ApiHTTPError,
    AuctionError,
    AuthenticationError,
    RateLimitError,
    ResponseFormatError,
    TransportError,
)
from common.models.auction import HistoryPage, Lot, LotPage, Sale

from .client import AuctionClient

__all__ = [
    "ApiHTTPError",
    "AuctionClient",
    "AuctionError",
    "AuthenticationError",
    "HistoryPage",
    "Lot",
    "LotPage",
    "LotSort",
    "PageResult",
    "RateLimitError",
    "ResponseFormatError",
    "Sale",
    "SortOrder",
    "TransportError",
]
