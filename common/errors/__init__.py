"""Общие ошибки каталога и конфигурации."""

from .auction import AuctionConfigurationError
from .catalog import AmbiguousItemError, CatalogError, ItemNotFoundError

__all__ = [
    "AmbiguousItemError", "AuctionConfigurationError",
    "CatalogError", "ItemNotFoundError",
]
