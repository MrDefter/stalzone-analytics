"""Настройка клиента и чтение первых страниц аукциона."""

from common.dataclasses import PageResult
from common.enums.auction import LotSort, SortOrder
from auction_engine.backend.integrations import AuctionClient
from common.models.auction import HistoryPage, LotPage
from .settings import AuctionSettings, DemoAuctionSettings


def create_auction_client(mode: str) -> AuctionClient:
    """Создать клиент с учётными данными выбранного режима.

    Args:
        mode: Режим доступа, demo или production.

    Returns:
        Клиент, готовый к входу в асинхронный контекст.

    Raises:
        ValueError: Режим или учётные данные некорректны.
    """
    if mode == "production":
        settings = AuctionSettings.from_env()
        return AuctionClient.production(
            client_id=settings.client_id,
            client_secret=settings.client_secret.get_secret_value(),
        )
    if mode == "demo":
        demo_settings = DemoAuctionSettings.from_env()
        return AuctionClient.demo(
            demo_settings.access_token.get_secret_value()
        )
    raise ValueError("Режим доступа должен быть demo или production.")


async def read_auction_pages(
    client: AuctionClient,
    region: str,
    item_id: str,
    *,
    limit: int,
) -> tuple[PageResult[LotPage], PageResult[HistoryPage]]:
    """Прочитать первые страницы лотов и истории с атрибутами.

    Args:
        client: Клиент под управлением вызывающего кода.
        region: Код региона.
        item_id: Точный ID предмета.
        limit: Максимальное число записей на каждой странице.

    Returns:
        Пара страниц с исходными ответами и метаданными.
    """
    lots = await client.get_lots(
        region,
        item_id,
        limit=limit,
        additional=True,
        sort=LotSort.TIME_CREATED,
        order=SortOrder.DESC,
    )
    history = await client.get_history(
        region, item_id, limit=limit, additional=True
    )
    return lots, history
