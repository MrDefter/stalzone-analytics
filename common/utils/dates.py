"""Разбор времени EXBO и задержек из HTTP-заголовков."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

# HTTP-date допускает IMF-fixdate и старые формы RFC 850 и asctime.
_HTTP_DATE_PATTERN = re.compile(
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} [A-Z][a-z]{2} [0-9]{4}"
    r" [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),"
    r" [0-9]{2}-[A-Z][a-z]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT"
    r"|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} (?:[0-9]{2}| [0-9])"
    r" [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}"
)


def parse_utc_datetime(value: object) -> datetime:
    """Разобрать строку ISO 8601 с часовым поясом.

    Args:
        value: Значение поля времени из ответа EXBO.

    Returns:
        Дата и время, приведённые к UTC.

    Raises:
        ValueError: Формат, часовой пояс или диапазон времени неверны.
    """
    if not isinstance(value, str):
        raise ValueError("expected an ISO 8601 string with a time zone")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("expected an explicit time zone")
    try:
        return parsed.astimezone(UTC)
    except OverflowError:
        raise ValueError("timestamp is out of range in UTC") from None


def retry_after_seconds(
    value: str | None, received_at: datetime
) -> float | None:
    """Получить задержку по заголовку Retry-After.

    Args:
        value: Число секунд, дата HTTP или None при отсутствии поля.
        received_at: Момент получения ответа для отсчёта задержки.

    Returns:
        Задержка в секундах, ноль для прошедшей даты или None при
        отсутствующем либо некорректном заголовке.
    """
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        seconds = float(value)
        return seconds if math.isfinite(seconds) else None
    if not _HTTP_DATE_PATTERN.fullmatch(value):
        return None
    try:
        retry_at = parsedate_to_datetime(value).replace(tzinfo=UTC)
        return max(0.0, (retry_at - received_at).total_seconds())
    except (ValueError, OverflowError):
        return None
