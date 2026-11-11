"""Проверки клиента аукциона на подставном транспорте."""

import asyncio
from datetime import UTC, datetime
from traceback import format_exception
from urllib.parse import parse_qs

import httpx
import pytest

from auction_engine.backend.integrations.auction.errors import (
    ApiHTTPError,
    AuthenticationError,
    RateLimitError,
    ResponseFormatError,
    TransportError,
)
from auction_engine.backend.integrations import AuctionClient
from auction_engine.backend.integrations.auction import (
    HistoryPage,
    LotPage,
    LotSort,
    SortOrder,
    auth,
    http,
)

pytestmark = pytest.mark.asyncio

BIG_PRICE = 2**53 + 1
API_TIME = "2026-11-07T12:34:56+03:00"
UTC_TIME = datetime(2026, 11, 7, 9, 34, 56, tzinfo=UTC)
LOT = {
    "itemId": "MiX /?#%é",
    "amount": 5,
    "startPrice": BIG_PRICE,
    "startTime": API_TIME,
    "endTime": "2026-11-07T12:34:56Z",
}
SALE = {"amount": 3, "price": BIG_PRICE, "time": API_TIME}


def empty_page(request: httpx.Request) -> httpx.Response:
    """Собрать пустую страницу для запрошенного ресурса.

    Args:
        request: Запрос лотов или истории продаж.

    Returns:
        Ответ HTTP 200 с подходящим пустым массивом.
    """
    collection = "prices" if request.url.path.endswith("/history") else "lots"
    return httpx.Response(200, json={"total": 0, collection: []})


def token_response(token: str, *, expires_in: int = 600) -> httpx.Response:
    """Собрать успешный ответ сервера OAuth.

    Args:
        token: Тестовое значение токена.
        expires_in: Срок действия токена в секундах.

    Returns:
        Ответ с токеном и сроком его действия.
    """
    return httpx.Response(
        200, json={"access_token": token, "expires_in": expires_in}
    )


async def test_lots_request_and_page():
    """Проверить запрос лотов, разбор страницы и метаданные."""
    first = {
        **LOT,
        "currentPrice": BIG_PRICE + 1,
        "buyoutPrice": 0,
        "additional": {},
        "unknownField": ["preserved", BIG_PRICE],
    }
    omitted = {**LOT, "amount": 2}
    nullable = {
        **LOT,
        "amount": 1,
        "currentPrice": None,
        "buyoutPrice": None,
        "additional": None,
    }
    payload = {
        "total": 12,
        "lots": [first, omitted, nullable, first],
        "future": {"flag": True},
    }
    response_headers = {
        "Date": "Sat, 07 Nov 2026 09:35:00 GMT",
        "Retry-After": "2",
        "RateLimit-Remaining": "17",
        "X-RateLimit-Reset": "102",
        "X-Rate-Limit-Limit": "20",
        "Authorization": "ignored",
        "Set-Cookie": "ignored",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Сверить параметры запроса и вернуть страницу лотов.

        Args:
            request: Запрос клиента к демонстрационному аукциону.

        Returns:
            Страница с пропусками полей, дубликатом и метаданными.
        """
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "dapi.stalzone.com"
        path = request.url.raw_path.partition(b"?")[0]
        assert path == b"/EU/auction/MiX%20%2F%3F%23%25%C3%A9/lots"
        assert request.headers["authorization"] == "Bearer demo-token"
        assert request.extensions["timeout"] == {
            "connect": 1.25,
            "read": 1.25,
            "write": 1.25,
            "pool": 1.25,
        }
        assert dict(request.url.params) == {
            "limit": "200",
            "offset": "7",
            "additional": "true",
            "sort": "buyout_price",
            "order": "asc",
        }
        return httpx.Response(200, json=payload, headers=response_headers)

    async with AuctionClient.demo(
        "demo-token", timeout=1.25, transport=httpx.MockTransport(handler)
    ) as client:
        before = datetime.now(UTC)
        page = await client.get_lots(
            "eU",
            LOT["itemId"],
            limit=200,
            offset=7,
            additional=True,
            sort=LotSort.BUYOUT_PRICE,
            order=SortOrder.ASC,
        )

    assert isinstance(page.data, LotPage)
    assert page.raw == payload
    assert before <= page.received_at <= datetime.now(UTC)
    assert page.received_at.tzinfo is UTC
    assert page.headers == {
        "date": response_headers["Date"],
        "retry-after": "2",
        "ratelimit-remaining": "17",
        "x-ratelimit-reset": "102",
        "x-rate-limit-limit": "20",
    }
    assert page.data.total == 12
    assert [lot.amount for lot in page.data.lots] == [5, 2, 1, 5]
    assert page.data.lots[0] == page.data.lots[3]
    lot = page.data.lots[0]
    assert lot.item_id == LOT["itemId"]
    assert type(lot.start_price) is int and lot.start_price == BIG_PRICE
    assert lot.current_price == BIG_PRICE + 1
    assert lot.buyout_price == 0
    assert lot.additional == {}
    assert lot.start_time == UTC_TIME and lot.start_time.tzinfo is UTC
    assert lot.end_time == datetime(2026, 11, 7, 12, 34, 56, tzinfo=UTC)
    for lot in page.data.lots[1:3]:
        assert lot.current_price is None
        assert lot.buyout_price is None
        assert lot.additional is None


async def test_history_request_and_page():
    """Проверить запрос истории и точность больших целых цен."""
    additional = {"future": [1, "text", {"flag": True}]}
    payload = {
        "total": 2,
        "prices": [
            SALE,
            {**SALE, "price": BIG_PRICE + 1, "additional": additional},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """Сверить кодирование пути и вернуть историю продаж.

        Args:
            request: Запрос истории с нулевым лимитом.

        Returns:
            Две тестовые продажи с точными целыми ценами.
        """
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "dapi.stalzone.com"
        path = request.url.raw_path.partition(b"?")[0]
        assert path == b"/EU%2FX%3FX/auction/aBc/history"
        assert request.headers["authorization"] == "Bearer demo-token"
        assert dict(request.url.params) == {
            "limit": "0",
            "offset": "0",
            "additional": "false",
        }
        return httpx.Response(200, json=payload)

    async with AuctionClient.demo(
        "demo-token", transport=httpx.MockTransport(handler)
    ) as client:
        page = await client.get_history("eU/x?x", "aBc", limit=0)

    assert isinstance(page.data, HistoryPage)
    assert page.data.total == 2
    assert page.raw == payload
    assert page.headers == {}
    first, second = page.data.prices
    assert type(first.price) is int
    assert first.price == BIG_PRICE
    assert second.price == BIG_PRICE + 1
    assert first.amount == 3
    assert first.time == UTC_TIME and first.time.tzinfo is UTC
    assert first.additional is None
    assert second.additional == additional


@pytest.mark.parametrize(
    ("resource", "collection"), [("lots", "lots"), ("history", "prices")]
)
async def test_empty_page_and_defaults(resource, collection):
    """Проверить параметры по умолчанию и приём пустой страницы.

    Args:
        resource: Имя ресурса, lots или history.
        collection: Имя массива записей в ответе.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Проверить строковые параметры и таймаут по умолчанию.

        Args:
            request: Запрос, сформированный с параметрами по умолчанию.

        Returns:
            Пустая страница запрошенного ресурса.
        """
        calls.append(request)
        assert dict(request.url.params) == {
            "limit": "20",
            "offset": "0",
            "additional": "false",
        }
        assert request.extensions["timeout"]["read"] == 10.0
        return empty_page(request)

    async with AuctionClient.demo(
        "demo-token", transport=httpx.MockTransport(handler)
    ) as client:
        page = await getattr(client, f"get_{resource}")("EU", "item")

    assert len(calls) == 1
    assert page.data.total == 0
    assert getattr(page.data, collection) == []
    assert page.raw == {"total": 0, collection: []}


@pytest.mark.parametrize("resource", ["lots", "history"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": -1},
        {"limit": 201},
        {"limit": True},
        {"limit": 1.0},
        {"limit": "20"},
        {"offset": -1},
        {"offset": False},
        {"offset": 1.0},
        {"offset": "0"},
        {"additional": "false"},
        {"additional": 1},
        {"region": ""},
        {"region": "."},
        {"region": ".."},
        {"region": None},
        {"item_id": ""},
        {"item_id": "."},
        {"item_id": ".."},
        {"item_id": 123},
    ],
)
async def test_invalid_page_arguments_do_not_send_requests(
    resource, overrides
):
    """Проверить валидацию параметров до обращения к транспорту.

    Args:
        resource: Имя ресурса, lots или history.
        overrides: Недопустимые параметры, подставляемые в вызов.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Провалить проверку при обращении к транспорту.

        Args:
            request: Неожиданный запрос при некорректных параметрах.
        """
        pytest.fail("invalid arguments reached the transport")

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        arguments = {"region": "EU", "item_id": "item", **overrides}
        with pytest.raises(ValueError):
            await getattr(client, f"get_{resource}")(**arguments)


@pytest.mark.parametrize(
    "overrides", [{"sort": "current_price"}, {"order": "desc"}]
)
async def test_invalid_sort_arguments_do_not_send_requests(overrides):
    """Проверить валидацию сортировки до обращения к транспорту.

    Args:
        overrides: Параметр сортировки с недопустимым типом.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Провалить проверку при обращении к транспорту.

        Args:
            request: Неожиданный запрос при неверной сортировке.
        """
        pytest.fail("invalid arguments reached the transport")

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError):
            await client.get_lots("EU", "item", **overrides)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ([], "$"),
        ({"total": -1, "prices": []}, "$.total"),
        ({"total": True, "prices": []}, "$.total"),
        ({"total": 0}, "$.prices"),
        (
            {"total": 1, "prices": [{**SALE, "price": 5.0}]},
            "$.prices[0].price",
        ),
        (
            {"total": 1, "prices": [{**SALE, "price": "private-price"}]},
            "$.prices[0].price",
        ),
        (
            {"total": 1, "prices": [{**SALE, "time": "2026-11-07T12:34:56"}]},
            "$.prices[0].time",
        ),
        (
            {"total": 1, "prices": [{**SALE, "time": 1700000000}]},
            "$.prices[0].time",
        ),
        (
            {
                "total": 1,
                "prices": [{**SALE, "time": "0001-01-01T00:00:00+01:00"}],
            },
            "$.prices[0].time",
        ),
        (
            {"total": 1, "prices": [{**SALE, "additional": []}]},
            "$.prices[0].additional",
        ),
    ],
)
async def test_invalid_schema_reports_field_without_input(payload, field):
    """Проверить путь поля в безопасном сообщении об ошибке.

    Args:
        payload: Тело ответа, нарушающее схему.
        field: Ожидаемый путь к ошибочному полю в JSON.
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)
    )
    async with AuctionClient.demo("demo-token", transport=transport) as client:
        with pytest.raises(ResponseFormatError) as caught:
            await client.get_history("EU", "item")

    expected = f"GET /EU/auction/item/history (invalid response field {field})"
    assert str(caught.value) == expected
    assert "private-price" not in "".join(format_exception(caught.value))


@pytest.mark.parametrize(
    "body",
    [b'{"private-response"', b'{"total":0,"prices":[],"future":NaN}', b"\xff"],
)
async def test_invalid_json_reports_format_error(body):
    """Проверить обработку повреждённого JSON.

    Args:
        body: Байты ответа с недопустимым JSON.
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body)
    )
    async with AuctionClient.demo("demo-token", transport=transport) as client:
        with pytest.raises(
            ResponseFormatError, match="invalid JSON response"
        ) as caught:
            await client.get_history("EU", "item")
    assert "private-response" not in "".join(format_exception(caught.value))


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (302, ApiHTTPError),
        (401, AuthenticationError),
        (403, ApiHTTPError),
        (404, ApiHTTPError),
        (500, ApiHTTPError),
    ],
)
async def test_demo_http_errors_do_not_retry_or_follow_redirects(
    status, error_type
):
    """Проверить обработку статусов demo за один вызов транспорта.

    Args:
        status: HTTP-статус подставного ответа.
        error_type: Ожидаемый класс исключения клиента.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Зафиксировать запрос и сформировать ответ с ошибкой.

        Args:
            request: Запрос клиента к аукциону.

        Returns:
            Ответ с заданным статусом и заголовками для проверки.
        """
        calls.append(request)
        return httpx.Response(
            status,
            text="private-response",
            headers={
                "Location": "https://example.invalid:bad/",
                "X-RateLimit-Remaining": "0",
            },
        )

    async with AuctionClient.demo(
        "demo-token", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(error_type) as caught:
            await client.get_lots("EU", "item")
    assert type(caught.value) is error_type
    assert caught.value.status_code == status
    assert caught.value.headers == {"x-ratelimit-remaining": "0"}
    assert str(caught.value) == f"GET /EU/auction/item/lots (HTTP {status})"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_transport_errors_are_sanitized_and_do_not_retry(
    failure, method
):
    """Проверить, что транспортный сбой даёт безопасное исключение.

    Args:
        failure: Тип исключения HTTPX для имитации сбоя.
        method: Метод запроса, в котором имитируется сбой.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Зафиксировать попытку запроса и воспроизвести сбой.

        Args:
            request: Запрос клиента, привязанный к исключению HTTPX.
        """
        calls.append(request)
        raise failure("private-request-details", request=request)

    transport = httpx.MockTransport(handler)
    client = (
        AuctionClient.demo("demo-token", transport=transport)
        if method == "GET"
        else AuctionClient.production("id", "secret", transport=transport)
    )
    async with client:
        with pytest.raises(TransportError) as caught:
            await client.get_lots("EU", "item")
    path = "/EU/auction/item/lots" if method == "GET" else "/oauth/token"
    assert str(caught.value).startswith(f"{method} {path} ")
    traceback = "".join(format_exception(caught.value))
    assert "private-request-details" not in traceback
    assert [request.method for request in calls] == [method]


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("5", 5),
        ("0", 0),
        (" 2.5 ", 2.5),
        (None, None),
        ("-3", None),
        ("unknown", None),
        ("NaN", None),
        ("Sat, 07 Nov 2026 09:35:06 GMT", 10),
        ("Saturday, 07-Nov-26 09:35:06 GMT", 10),
        ("Sat Nov  7 09:35:06 2026", 10),
        ("Sat, 07 Nov 2026 09:34:55 GMT", 0),
        ("Sat, 07 Nov 2026 09:35:06 INVALID", None),
        ("Sat, 07 Nov 2026 09:35:06 GMT, extra", None),
    ],
)
async def test_retry_after(header, seconds, monkeypatch):
    """Проверить расчёт задержки после ответа HTTP 429.

    Args:
        header: Значение Retry-After или None при его отсутствии.
        seconds: Ожидаемая задержка в секундах или None.
        monkeypatch: Фикстура для подмены часов клиента.
    """

    class FixedClock(datetime):
        """Часы с фиксированным временем получения ответа."""

        @classmethod
        def now(cls, tz=None):
            """Вернуть фиксированный момент времени для сценария.

            Args:
                tz: Часовой пояс из вызова datetime.now.

            Returns:
                Момент получения ответа в UTC.
            """
            return UTC_TIME

    monkeypatch.setattr(http, "datetime", FixedClock)
    headers = {} if header is None else {"Retry-After": header}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Зафиксировать запрос и имитировать ограничение квоты.

        Args:
            request: Запрос истории продаж.

        Returns:
            Ответ HTTP 429 с заголовками сценария.
        """
        calls.append(request)
        return httpx.Response(429, headers=headers)

    async with AuctionClient.demo(
        "demo-token", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RateLimitError) as caught:
            await client.get_history("EU", "item")
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds == seconds
    assert caught.value.headers == {
        name.lower(): value for name, value in headers.items()
    }
    assert len(calls) == 1


@pytest.mark.parametrize("limited_method", ["POST", "GET"])
async def test_production_rate_limits_propagate_without_retry(limited_method):
    """Проверить, что HTTP 429 завершает вызов production.

    Args:
        limited_method: Запрос OAuth или аукциона с исчерпанной квотой.
    """
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Выдать ограничение на выбранном этапе запроса.

        Args:
            request: Запрос OAuth или аукциона.

        Returns:
            Ответ HTTP 429 для выбранного метода или токен OAuth.
        """
        methods.append(request.method)
        if request.method == limited_method:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return token_response("token")

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RateLimitError) as caught:
            await client.get_lots("RU", "item")

    assert caught.value.retry_after_seconds == 1
    expected = ["POST"] if limited_method == "POST" else ["POST", "GET"]
    assert methods == expected


async def test_oauth_is_lazy_shared_and_cached():
    """Проверить один ленивый обмен OAuth для нескольких запросов."""
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """Сверить форму OAuth и использование общего токена.

        Args:
            request: Запрос токена или страницы аукциона.

        Returns:
            Тестовый токен для POST или пустая страница для GET.
        """
        calls.append(request.method)
        if request.method == "POST":
            assert str(request.url) == "https://exbo.net/oauth/token"
            assert "authorization" not in request.headers
            content_type = request.headers["content-type"]
            assert content_type == "application/x-www-form-urlencoded"
            form = parse_qs(
                request.content.decode("ascii"), keep_blank_values=True
            )
            assert form == {
                "grant_type": ["client_credentials"],
                "client_id": ["app+/"],
                "client_secret": ["local-test secret+&"],
                "scope": [""],
            }
            await asyncio.sleep(0)
            return token_response("token")
        assert request.url.scheme == "https"
        assert request.url.host == "eapi.stalzone.com"
        assert request.headers["authorization"] == "Bearer token"
        assert "client_secret" not in request.url.params
        assert not request.content
        return empty_page(request)

    async with AuctionClient.production(
        "app+/", "local-test secret+&", transport=httpx.MockTransport(handler)
    ) as client:
        assert calls == []
        await asyncio.gather(
            client.get_lots("RU", "item"),
            client.get_history("RU", "item"),
        )
        await client.get_lots("RU", "item")
    assert calls == ["POST", "GET", "GET", "GET"]


async def test_oauth_refreshes_before_expiry(monkeypatch):
    """Проверить обновление токена с запасом до истечения срока.

    Args:
        monkeypatch: Фикстура для подмены монотонного времени.
    """
    now_ns = 0
    monkeypatch.setattr(auth, "monotonic_ns", lambda: now_ns)
    issued = 0
    used = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Выдать очередной токен или записать использованный токен.

        Args:
            request: Запрос токена или страницы истории.

        Returns:
            Токен со сроком 100 секунд или пустая страница.
        """
        nonlocal issued
        if request.method == "POST":
            issued += 1
            return token_response(f"token-{issued}", expires_in=100)
        used.append(request.headers["authorization"])
        return empty_page(request)

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        await client.get_history("RU", "item")
        now_ns = 89_000_000_000
        await client.get_history("RU", "item")
        now_ns = 90_000_000_000
        await client.get_history("RU", "item")
    assert issued == 2
    assert used == ["Bearer token-1", "Bearer token-1", "Bearer token-2"]


@pytest.mark.parametrize("retry_status", [200, 401])
async def test_production_retries_original_request_once_after_401(
    retry_status,
):
    """Проверить один повтор исходного запроса после HTTP 401.

    Args:
        retry_status: Статус повторного GET, 200 или 401.
    """
    requests = []
    issued = 0
    gets = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Имитировать отклонение первого токена и повтор запроса.

        Args:
            request: Запрос OAuth или активных лотов.

        Returns:
            Очередной токен, ответ HTTP 401 или пустая страница.
        """
        nonlocal issued
        requests.append(request.method)
        if request.method == "POST":
            issued += 1
            return token_response(f"token-{issued}")
        gets.append(request)
        if len(gets) == 1:
            return httpx.Response(401)
        if retry_status == 200:
            return empty_page(request)
        return httpx.Response(401)

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        if retry_status == 200:
            result = await client.get_lots(
                "RU", "MiX", limit=4, order=SortOrder.DESC
            )
            assert result.data.total == 0
        else:
            with pytest.raises(AuthenticationError) as caught:
                await client.get_lots(
                    "RU", "MiX", limit=4, order=SortOrder.DESC
                )
            assert caught.value.status_code == 401

    assert requests == ["POST", "GET", "POST", "GET"]
    first, second = gets
    assert first.url == second.url
    assert "sort" not in second.url.params
    assert second.url.params["order"] == "desc"
    assert first.headers["authorization"] == "Bearer token-1"
    assert second.headers["authorization"] == "Bearer token-2"


async def test_parallel_401_reuses_token_refreshed_by_another_request():
    """Проверить, что параллельные запросы делят обновлённый токен."""
    both_started = asyncio.Event()
    first_retried = asyncio.Event()
    issued = 0
    attempts = {"first": 0, "second": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        """Согласовать порядок отказов и повторных запросов.

        Args:
            request: Запрос OAuth или один из параллельных запросов.

        Returns:
            Токен, ответ HTTP 401 или страница после повтора.
        """
        nonlocal issued
        if request.method == "POST":
            issued += 1
            return token_response("same-token-value")
        item = request.url.path.split("/")[-2]
        attempts[item] += 1
        if attempts[item] == 2:
            if item == "first":
                first_retried.set()
            return empty_page(request)
        if item == "first":
            await both_started.wait()
        else:
            both_started.set()
            await first_retried.wait()
        return httpx.Response(401)

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        await asyncio.wait_for(
            asyncio.gather(
                client.get_lots("RU", "first"),
                client.get_history("RU", "second"),
            ),
            timeout=3,
        )
    assert issued == 2
    assert attempts == {"first": 2, "second": 2}


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (400, AuthenticationError),
        (401, AuthenticationError),
        (403, AuthenticationError),
        (302, ApiHTTPError),
        (500, ApiHTTPError),
    ],
)
async def test_oauth_http_errors_do_not_expose_body_or_credentials(
    status, error_type
):
    """Проверить безопасные сообщения об ошибках OAuth.

    Args:
        status: HTTP-статус сервера обмена токена.
        error_type: Ожидаемый класс исключения.
    """
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Имитировать ответ OAuth с чувствительными деталями.

        Args:
            request: Запрос на обмен учётных данных.

        Returns:
            Ответ с заданным статусом, телом и заголовками.
        """
        calls.append(request)
        return httpx.Response(
            status,
            text="private-response",
            headers={
                "Location": "https://example.invalid/",
                "Authorization": "private-response",
            },
        )

    async with AuctionClient.production(
        "private-id", "private-secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(error_type) as caught:
            await client.get_lots("RU", "item")
    assert type(caught.value) is error_type
    assert caught.value.status_code == status
    assert caught.value.headers == {}
    assert str(caught.value) == f"POST /oauth/token (HTTP {status})"
    assert "private-" not in "".join(format_exception(caught.value))
    assert [request.method for request in calls] == ["POST"]


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"access_token": "", "expires_in": 10}, "$.access_token"),
        (
            {"access_token": "private-unsafe\n", "expires_in": 10},
            "$.access_token",
        ),
        ({"access_token": "private-token"}, "$.expires_in"),
        (
            {"access_token": "private-token", "expires_in": "10"},
            "$.expires_in",
        ),
        (
            {"access_token": "private-token", "expires_in": True},
            "$.expires_in",
        ),
        ({"access_token": "private-token", "expires_in": 0}, "$.expires_in"),
    ],
)
async def test_oauth_validates_response_without_exposing_token(payload, field):
    """Проверить схему OAuth и сокрытие токена в ошибках.

    Args:
        payload: Ответ OAuth, нарушающий схему.
        field: Ожидаемый путь к ошибочному полю.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Вернуть успешный HTTP-статус с некорректным телом OAuth.

        Args:
            request: Запрос на получение токена.

        Returns:
            Ответ HTTP 200 с тестовым JSON.
        """
        assert request.method == "POST"
        return httpx.Response(200, json=payload)

    async with AuctionClient.production(
        "id", "secret", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ResponseFormatError) as caught:
            await client.get_lots("RU", "item")
    expected = f"POST /oauth/token (invalid response field {field})"
    assert str(caught.value) == expected
    assert "private-" not in "".join(format_exception(caught.value))


@pytest.mark.parametrize("use_context", [True, False])
async def test_client_closes_transport(use_context):
    """Проверить освобождение транспорта клиентом.

    Args:
        use_context: Закрывать через async with или явный aclose.
    """

    class ClosingTransport(httpx.MockTransport):
        """Подставной транспорт с отметкой о закрытии."""

        closed = False

        async def aclose(self):
            """Отметить закрытие подставного транспорта."""
            self.closed = True

    transport = ClosingTransport(empty_page)
    client = AuctionClient.demo("demo-token", transport=transport)
    if use_context:
        async with client as entered:
            assert entered is client
            await client.get_lots("RU", "item")
    else:
        await client.get_lots("RU", "item")
        await client.aclose()
    assert transport.closed
