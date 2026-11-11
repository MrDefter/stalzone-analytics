"""Асинхронный клиент API аукциона EXBO."""

import re
from types import TracebackType
from typing import Self
from urllib.parse import quote

import httpx

from common.dataclasses import PageResult
from common.dataclasses.auction import PageT
from common.enums.auction import LotSort, SortOrder
from common.models.auction import HistoryPage, LotPage

from .oauth_types import BEARER_TOKEN_PATTERN

from .auth import AuctionAuth
from .diagnostics import CURRENT_PAGE
from .http import check_status, decode_response, reject_redirect, send_request

_DEMO_HOST = "https://dapi.stalzone.com"
_PRODUCTION_HOST = "https://eapi.stalzone.com"


def _page_params(limit: int, offset: int, additional: bool) -> dict[str, str]:
    """Проверить общие параметры страницы и подготовить их для URL.

    Args:
        limit: Число записей на странице от 0 до 200.
        offset: Неотрицательное смещение в записях.
        additional: Признак запроса дополнительных данных.

    Returns:
        Параметры запроса в строковом виде.

    Raises:
        ValueError: Тип или значение параметра недопустимы.
    """
    if type(limit) is not int or not 0 <= limit <= 200:
        raise ValueError("limit must be an integer between 0 and 200")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if not isinstance(additional, bool):
        raise ValueError("additional must be a boolean")
    return {
        "limit": str(limit),
        "offset": str(offset),
        "additional": "true" if additional else "false",
    }


def _auction_path(region: str, item_id: str, resource: str) -> str:
    """Собрать путь с отдельно закодированными регионом и предметом.

    Args:
        region: Код региона, приводимый к верхнему регистру.
        item_id: Регистрозависимый идентификатор предмета.
        resource: Имя ресурса аукциона, lots или history.

    Returns:
        Путь к ресурсу аукциона.

    Raises:
        ValueError: Регион или предмет нельзя передать как сегмент пути.
    """
    for name, value in (("region", region), ("item_id", item_id)):
        if (
            not isinstance(value, str)
            or not value.strip()
            or value in (".", "..")
        ):
            raise ValueError(
                f"{name} must be a nonempty path segment "
                "other than '.' or '..'"
            )
    region_segment = quote(region.upper(), safe="")
    item_segment = quote(item_id, safe="")
    return f"/{region_segment}/auction/{item_segment}/{resource}"


class AuctionClient:
    """Клиент чтения аукциона с доступом через demo или production."""

    def __init__(
        self,
        *,
        access_token: str | None,
        oauth_form: dict[str, str] | None,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        """Подготовить авторизацию и создать HTTP-клиент.

        Args:
            access_token: Готовый токен demo или None для production.
            oauth_form: Данные OAuth для production или None для demo.
            timeout: Таймаут сетевых операций в секундах.
            transport: Транспорт HTTPX или None для стандартного.
        """
        self._host = _DEMO_HOST if oauth_form is None else _PRODUCTION_HOST
        self._auth = AuctionAuth(access_token, oauth_form)
        self._http = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            event_hooks={"response": [reject_redirect]},
        )

    @classmethod
    def demo(
        cls,
        access_token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        """Создать клиент с готовым demo-токеном.

        Args:
            access_token: Токен приложения для демонстрационного API.
            timeout: Таймаут сетевых операций в секундах.
            transport: Транспорт HTTPX или None для стандартного.

        Returns:
            Клиент, настроенный для демонстрационного хоста.

        Raises:
            ValueError: Токен пуст или содержит недопустимые символы.
        """
        if not isinstance(access_token, str) or not re.fullmatch(
            BEARER_TOKEN_PATTERN, access_token
        ):
            raise ValueError("access_token must be a nonempty Bearer token")
        return cls(
            access_token=access_token,
            oauth_form=None,
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def production(
        cls,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        """Создать клиент с авторизацией по данным приложения EXBO.

        Args:
            client_id: Идентификатор приложения.
            client_secret: Секрет приложения для обмена OAuth.
            timeout: Таймаут сетевых операций в секундах.
            transport: Транспорт HTTPX или None для стандартного.

        Returns:
            Клиент production, получающий токен при первом запросе.
        """
        return cls(
            access_token=None,
            oauth_form={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "",
            },
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        """Войти в асинхронный контекст HTTP-клиента.

        Returns:
            Текущий экземпляр клиента аукциона.
        """
        await self._http.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Закрыть HTTP-клиент при выходе из контекста.

        Args:
            exc_type: Тип исключения из тела контекста или None.
            exc: Исключение из тела контекста или None.
            traceback: Стек вызовов для исключения или None.
        """
        await self._http.__aexit__(exc_type, exc, traceback)

    async def aclose(self) -> None:
        """Закрыть HTTP-клиент и его транспорт."""
        await self._http.aclose()

    async def get_lots(
        self,
        region: str,
        item_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        additional: bool = False,
        sort: LotSort | None = None,
        order: SortOrder | None = None,
    ) -> PageResult[LotPage]:
        """Загрузить одну страницу активных лотов предмета.

        Args:
            region: Код региона, приводимый к верхнему регистру.
            item_id: Регистрозависимый идентификатор предмета.
            limit: Число записей на странице от 0 до 200.
            offset: Число записей, пропускаемых с начала выборки.
            additional: Признак запроса дополнительных данных.
            sort: Поле сортировки или None для выбора API.
            order: Направление сортировки или None для выбора API.

        Returns:
            Страница лотов с исходным JSON и метаданными ответа.

        Raises:
            ValueError: Некорректный аргумент запроса.
            AuctionError: Ошибка транспорта, HTTP или формата ответа.
        """
        params = _page_params(limit, offset, additional)
        if sort is not None:
            if not isinstance(sort, LotSort):
                raise ValueError("sort must be a LotSort")
            params["sort"] = sort.value
        if order is not None:
            if not isinstance(order, SortOrder):
                raise ValueError("order must be a SortOrder")
            params["order"] = order.value
        path = _auction_path(region, item_id, "lots")
        return await self._read_page(path, params, LotPage)

    async def get_history(
        self,
        region: str,
        item_id: str,
        *,
        limit: int = 20,
        offset: int = 0,
        additional: bool = False,
    ) -> PageResult[HistoryPage]:
        """Загрузить одну страницу истории продаж предмета.

        Args:
            region: Код региона, приводимый к верхнему регистру.
            item_id: Регистрозависимый идентификатор предмета.
            limit: Число записей на странице от 0 до 200.
            offset: Число записей, пропускаемых с начала выборки.
            additional: Признак запроса дополнительных данных.

        Returns:
            Страница истории с исходным JSON и метаданными ответа.

        Raises:
            ValueError: Некорректный аргумент запроса.
            AuctionError: Ошибка транспорта, HTTP или формата ответа.
        """
        params = _page_params(limit, offset, additional)
        path = _auction_path(region, item_id, "history")
        return await self._read_page(path, params, HistoryPage)

    async def _read_page(
        self, path: str, params: dict[str, str], model: type[PageT]
    ) -> PageResult[PageT]:
        """Отправить GET и разобрать страницу EXBO.

        При HTTP 401 в production обновить токен и повторить GET один
        раз.

        Args:
            path: Закодированный путь к ресурсу.
            params: Строковые параметры запроса.
            model: Модель Pydantic для проверки страницы.

        Returns:
            Проверенная страница с исходными данными и метаданными.
        """
        url = httpx.URL(self._host + path, params=params)
        token = await self._auth.get_token(self._http)
        response, received_at = await send_request(
            self._http, "GET", url, token=token.value
        )
        if response.status_code == 401 and self._auth.can_refresh:
            page_log = CURRENT_PAGE.get()
            if page_log is not None:
                page_log.write(retry=True)
                page_log.begin("oauth")
            self._auth.discard_token(token)
            token = await self._auth.get_token(self._http)
            response, received_at = await send_request(
                self._http, "GET", url, token=token.value
            )
            if response.status_code == 401:
                self._auth.discard_token(token)
        headers = check_status(response, received_at)
        data, raw = decode_response(response, model)
        return PageResult(
            data=data, raw=raw, received_at=received_at, headers=headers
        )
