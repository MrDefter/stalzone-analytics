"""Безопасные диагностические записи запросов страниц EXBO."""

import asyncio
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from socket import gaierror
from time import perf_counter

import httpx
from pydantic import ValidationError

from .errors import ApiHTTPError, ResponseFormatError, TransportError
from common.utils.dates import retry_after_seconds

LOGGER = logging.getLogger("uvicorn.error.auction_snapshot")


def completion_status(error: BaseException | None) -> str:
    """Определить состояние завершения операции для журнала.

    Args:
        error: Исключение операции или None при успешном возврате.

    Returns:
        Состояние завершения, сбоя или отмены.
    """
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    return "failed" if error is not None else "completed"


def _transport_message(cause: BaseException | None) -> str:
    """Описать сбой транспорта по типам исключений HTTPX и сокета.

    Args:
        cause: Исходное исключение, преобразованное в TransportError.

    Returns:
        Безопасное описание сбоя транспорта.
    """
    if isinstance(cause, httpx.ReadTimeout):
        return "HTTP read timed out"
    if isinstance(cause, httpx.ConnectTimeout):
        return "HTTP connection timed out"
    if isinstance(cause, httpx.ConnectError):
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, gaierror):
                return "DNS resolution failed"
            seen.add(id(cause))
            cause = cause.__cause__
        return "HTTP connection failed"
    return "HTTP transport failed"


def _error_message(
    error: BaseException, cause: BaseException | None
) -> str:
    """Выбрать описание ошибки по её типу и безопасным метаданным.

    Args:
        error: Исключение операции.
        cause: Первичная причина исключения.

    Returns:
        Краткое описание без исходного текста исключения.
    """
    if isinstance(error, ApiHTTPError):
        return f"HTTP {error.status_code}"
    if isinstance(error, TransportError):
        return _transport_message(cause)
    if isinstance(error, ResponseFormatError):
        if isinstance(cause, (ValueError, RecursionError)) and not isinstance(
            cause, ValidationError
        ):
            return "Invalid JSON response"
        return "Invalid response format"
    if isinstance(error, asyncio.CancelledError):
        return "Operation cancelled"
    if isinstance(error, TimeoutError):
        if isinstance(cause, asyncio.CancelledError):
            return "Scan deadline expired"
        return "Operation timed out"
    return "Operation failed"


def error_details(
    error: BaseException | None,
) -> tuple[str | None, str | None, str | None]:
    """Подготовить тип исключения, тип его причины и описание.

    Args:
        error: Исключение операции или None.

    Returns:
        Тип исключения, тип причины и безопасное описание.
    """
    if error is None:
        return None, None, None
    cause = error.__cause__ or error.__context__
    return (
        type(error).__name__,
        type(cause).__name__ if cause is not None else None,
        _error_message(error, cause),
    )


class PageLog:
    """Метаданные одной загрузки страницы и её текущей попытки HTTP."""

    def __init__(
        self,
        *,
        snapshot_id: str | None,
        source: str,
        stage: str,
        offset: int,
        limit: int,
    ) -> None:
        """Привязать журнал запроса к странице конкретного снимка.

        Args:
            snapshot_id: Идентификатор сбора лотов и истории.
            source: Ресурс аукциона.
            stage: Этап, инициировавший загрузку.
            offset: Смещение запрошенной страницы.
            limit: Запрошенное число строк.
        """
        self.snapshot_id = snapshot_id
        self.source = source
        self.stage = stage
        self.offset = offset
        self.limit = limit
        self.begin("prepare")

    def begin(self, operation: str) -> None:
        """Начать замер подготовки, запроса страницы или обмена OAuth.

        Args:
            operation: Название текущей операции.
        """
        self.operation = operation
        self.started = perf_counter()
        self.http_status: int | None = None
        self.retry_after: float | None = None

    def record_response(self, response: httpx.Response) -> None:
        """Запомнить полученный статус и разобрать паузу при HTTP 429.

        Args:
            response: Ответ HTTPX на этапе получения заголовков.
        """
        self.http_status = response.status_code
        if self.http_status == 429:
            self.retry_after = retry_after_seconds(
                response.headers.get("retry-after"), datetime.now(UTC)
            )

    def write(
        self,
        *,
        rows_received: int = 0,
        error: BaseException | None = None,
        retry: bool = False,
    ) -> None:
        """Записать итог попытки загрузки в журнал EXBO.

        Args:
            rows_received: Число строк, прошедших разбор модели ответа.
            error: Исключение завершившейся операции.
            retry: HTTP 401 запустил штатное обновление токена.
        """
        error_type, cause_type, message = error_details(error)
        status = "retry" if retry else completion_status(error)
        LOGGER.log(
            logging.ERROR if status == "failed" else logging.INFO,
            "EXBO page snapshot_id=%s source=%s stage=%s offset=%d limit=%d "
            "operation=%s status=%s elapsed_ms=%.1f rows_received=%d "
            "http_status=%s retry_after_seconds=%s error_type=%s "
            "cause_type=%s error_message=%r",
            self.snapshot_id, self.source, self.stage, self.offset, self.limit,
            self.operation, status, (perf_counter() - self.started) * 1000,
            rows_received, self.http_status, self.retry_after, error_type,
            cause_type, "Refreshing OAuth" if retry else message,
        )


CURRENT_PAGE: ContextVar[PageLog | None] = ContextVar(
    "exbo_current_page", default=None
)
