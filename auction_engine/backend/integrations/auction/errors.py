"""Ошибки HTTP-клиента EXBO."""


class AuctionError(Exception):
    """Базовая ошибка при работе с аукционом EXBO."""

    def __init__(self, method: str, path: str, reason: str) -> None:
        """Сформировать сообщение из безопасных данных запроса.

        Args:
            method: Метод HTTP.
            path: Путь запроса.
            reason: Краткая причина ошибки.
        """
        super().__init__(f"{method} {path} ({reason})")


class TransportError(AuctionError):
    """Сбой транспорта, включая ошибки соединения и таймауты."""


class ApiHTTPError(AuctionError):
    """Неожиданный HTTP-статус ответа EXBO."""

    def __init__(
        self, method: str, path: str, status_code: int, headers: dict[str, str]
    ) -> None:
        """Сохранить статус и безопасные заголовки ответа.

        Args:
            method: Метод HTTP.
            path: Путь запроса.
            status_code: Код ответа HTTP.
            headers: Отобранные заголовки ответа.
        """
        super().__init__(method, path, f"HTTP {status_code}")
        self.status_code = status_code
        self.headers = headers


class AuthenticationError(ApiHTTPError):
    """Сервер аукциона или OAuth отклонил авторизацию."""


class RateLimitError(ApiHTTPError):
    """Превышена квота запросов EXBO."""

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        headers: dict[str, str],
        retry_after_seconds: float | None,
    ) -> None:
        """Сохранить сведения об ограничении частоты запросов.

        Args:
            method: Метод HTTP.
            path: Путь запроса.
            status_code: Код ответа HTTP.
            headers: Отобранные заголовки ответа.
            retry_after_seconds: Задержка в секундах или None.
        """
        super().__init__(method, path, status_code, headers)
        self.retry_after_seconds = retry_after_seconds


class ResponseFormatError(AuctionError):
    """Ответ содержит повреждённый JSON или нарушает схему."""
