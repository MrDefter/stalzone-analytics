"""Сборка веб-приложения STALZONE Analytics."""

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from common.database.clickhouse.auction_storage import AuctionStorage
from common.settings.auction_storage import AuctionStorageSettings

from .icon_files import ITEM_IMAGES_DIR
from .routers import auction_router, dashboard_router, items_router
from .web_templates import FRONTEND_ROOT

mimetypes.add_type("image/webp", ".webp")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Управлять подключением веб-процесса к ClickHouse.

    Args:
        app: Веб-приложение с хранилищем состояния.

    Yields:
        Время работы HTTP-сервера.
    """
    try:
        storage = AuctionStorage(AuctionStorageSettings())
    except ValidationError:
        app.state.auction_storage = None
        yield
        return
    app.state.auction_storage = storage
    try:
        yield
    finally:
        await storage.close()


app = FastAPI(
    title="STALZONE Analytics",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)
app.mount(
    "/static",
    StaticFiles(directory=FRONTEND_ROOT / "static"),
    name="static",
)
app.mount(
    "/item-images",
    StaticFiles(directory=ITEM_IMAGES_DIR, check_dir=False),
    name="item_images",
)
app.include_router(dashboard_router)
app.include_router(items_router)
app.include_router(auction_router)
