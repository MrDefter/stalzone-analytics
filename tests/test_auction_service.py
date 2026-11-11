"""Проверки сценария чтения аукциона для будущего сборщика."""

import httpx
import pytest

from auction_engine.backend.integrations import AuctionClient
from auction_engine.backend.auction_pages import read_auction_pages


@pytest.mark.asyncio
async def test_read_pages_reuses_one_open_client() -> None:
    """Проверить два предмета на одном клиенте и с одним токеном."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Записать запрос и вернуть подходящий ответ EXBO.

        Args:
            request: Запрос токена или страницы.

        Returns:
            Ответ обмена OAuth либо пустая страница аукциона.
        """
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                200, json={"access_token": "token", "expires_in": 600}
            )
        assert request.headers["authorization"] == "Bearer token"
        rows = "lots" if request.url.path.endswith("/lots") else "prices"
        return httpx.Response(200, json={"total": 0, rows: []})

    async with AuctionClient.production(
        "test-id", "test-secret", transport=httpx.MockTransport(handler)
    ) as client:
        first_lots, first_history = await read_auction_pages(
            client, "RU", "first", limit=10
        )
        second_lots, second_history = await read_auction_pages(
            client, "RU", "second", limit=20
        )

    assert first_lots.data.lots == second_lots.data.lots == []
    assert first_history.data.prices == second_history.data.prices == []
    assert calls == [
        ("POST", "/oauth/token"),
        ("GET", "/RU/auction/first/lots"),
        ("GET", "/RU/auction/first/history"),
        ("GET", "/RU/auction/second/lots"),
        ("GET", "/RU/auction/second/history"),
    ]
