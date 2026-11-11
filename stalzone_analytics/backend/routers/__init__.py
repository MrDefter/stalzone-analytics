"""Маршруты веб-приложения."""

from .auction import router as auction_router
from .dashboard import router as dashboard_router
from .items import router as items_router

__all__ = ["auction_router", "dashboard_router", "items_router"]
