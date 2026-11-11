"""Публичные датаклассы серверной части."""

from .auction import PageResult
from .catalog import CatalogItem

__all__ = [
    "CatalogItem",
    "PageResult",
]
