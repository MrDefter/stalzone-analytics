"""Отправка запросов EXBO и обработка ответов HTTP."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from auction_engine.backend.integrations.auction.errors import (
    ApiHTTPError,
    AuthenticationError,
    RateLimitError,
    ResponseFormatError,
    TransportError,
)
from common.utils.dates import retry_after_seconds
from common.utils.json import reject_json_constant

from .diagnostics import CURRENT_PAGE

_RATE_LIMIT_PREFIXES = ("ratelimit-", "x-ratelimit-", "x-rate-limit-")
ModelT = TypeVar("ModelT", bound=BaseModel)
REQUEST_SLOT: ContextVar[
    Callable[[], AbstractAsyncContextManager[None]] | None
] = ContextVar("exbo_request_slot", default=None)
RATE_LIMIT_FEEDBACK: ContextVar[Callable[[float | None], None] | None] = (
    ContextVar("exbo_rate_limit_feedback", default=None)
)


def _request_path(url: httpx.URL) -> str:
    """Выделить путь для безопасного сообщения об ошибке.

    Args:
        url: URL запроса HTTPX.

    Returns:
        Закодированный путь без параметров запроса.
    """
    return url.raw_path.partition(b"?")[0].decode("ascii")


async def send_request(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL,
    *,
    token: str | None = None,
    form: dict[str, str] | None = None,
) -> tuple[httpx.Response, datetime]:
    """Отправить HTTP-запрос и зафиксировать время получения ответа.

    Args:
        client: HTTP-клиент для отправки запроса.
        method: Метод HTTP.
        url: URL запроса с параметрами.
        token: Токен Bearer или None для запроса без заголовка.
        form: Поля формы OAuth или None при отсутствии формы.

    Returns:
        Пара из ответа HTTPX и времени получения в UTC.

    Raises:
        TransportError: Сбой транспорта HTTPX.
    """
    page = CURRENT_PAGE.get()
    if page is not None:
        page.begin("page" if method == "GET" else "oauth")
    headers = None if token is None else {"Authorization": f"Bearer {token}"}
    try:
        slot = REQUEST_SLOT.get()
        async with slot() if slot is not None else nullcontext():
            response = await client.request(
                method, url, headers=headers, data=form
            )
            if response.status_code == 429 and (
                feedback := RATE_LIMIT_FEEDBACK.get()
            ) is not None:
                feedback(retry_after_seconds(
                    response.headers.get("retry-after"), datetime.now(UTC),
                ))
    except httpx.RequestError as exc:
        reason = f"transport failure ({type(exc).__name__})"
        raise TransportError(method, _request_path(url), reason) from None
    return response, datetime.now(UTC)


def check_status(
    response: httpx.Response, received_at: datetime, *, oauth: bool = False
) -> dict[str, str]:
    """Проверить статус и отобрать безопасные заголовки ответа.

    Args:
        response: Ответ, связанный с запросом HTTPX.
        received_at: Момент получения ответа для расчёта Retry-After.
        oauth: Признак ответа от сервера обмена OAuth.

    Returns:
        Заголовки даты, паузы и квоты с именами в нижнем регистре.

    Raises:
        ApiHTTPError: Неожиданный HTTP-статус, включая ошибки
            авторизации и превышение квоты.
    """
    headers = {
        name: value
        for name, value in response.headers.items()
        if (
            name in ("date", "retry-after")
            or name.startswith(_RATE_LIMIT_PREFIXES)
        )
    }
    if response.is_success:
        return headers

    method = response.request.method
    path = _request_path(response.request.url)
    status = response.status_code
    if status == 429:
        retry_after = retry_after_seconds(
            headers.get("retry-after"), received_at
        )
        raise RateLimitError(method, path, status, headers, retry_after)
    if status == 401 or (oauth and status in (400, 403)):
        raise AuthenticationError(method, path, status, headers)
    raise ApiHTTPError(method, path, status, headers)


async def reject_redirect(response: httpx.Response) -> None:
    """Зафиксировать статус и отклонить редирект до разбора Location.

    Args:
        response: Ответ HTTPX, переданный обработчику ответа.

    Raises:
        ApiHTTPError: Ответ имеет статус перенаправления.
    """
    page = CURRENT_PAGE.get()
    if page is not None:
        page.record_response(response)
    if response.is_redirect:
        check_status(response, datetime.now(UTC))


def decode_response(
    response: httpx.Response, model: type[ModelT]
) -> tuple[ModelT, dict[str, Any]]:
    """Декодировать JSON-объект и проверить его по модели.

    Args:
        response: Ответ HTTPX с телом JSON.
        model: Модель Pydantic для проверки декодированного объекта.

    Returns:
        Пара из модели и исходного словаря со всеми полями JSON.

    Raises:
        ResponseFormatError: Повреждённый JSON или нарушение схемы.
    """
    method = response.request.method
    path = _request_path(response.request.url)
    try:
        raw = response.json(parse_constant=reject_json_constant)
    except (ValueError, RecursionError):
        raise ResponseFormatError(
            method, path, "invalid JSON response"
        ) from None
    if not isinstance(raw, dict):
        raise ResponseFormatError(method, path, "invalid response field $")
    try:
        return model.model_validate(raw), raw
    except ValidationError as exc:
        first_error = exc.errors(
            include_input=False, include_context=False, include_url=False
        )[0]
        location = "$"
        for part in first_error["loc"]:
            location += f"[{part}]" if isinstance(part, int) else f".{part}"
        raise ResponseFormatError(
            method, path, f"invalid response field {location}"
        ) from None
