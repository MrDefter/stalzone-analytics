"""Датаклассы результата запроса и кэша токенов EXBO."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

PageT = TypeVar("PageT", bound=BaseModel)
RowT = TypeVar("RowT")


@dataclass(slots=True)
class PageResult(Generic[PageT]):
    """Типизированная страница и метаданные ответа EXBO.

    Attributes:
        data: Страница, проверенная по модели Pydantic.
        raw: Декодированный JSON со всеми исходными полями.
        received_at: Момент получения ответа в UTC.
        headers: Заголовки даты, паузы и квоты ответа.
    """

    data: PageT
    raw: dict[str, Any]
    received_at: datetime
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class PageScan(Generic[RowT]):
    """Прочитанные строки и проверка согласованности страниц."""

    rows: tuple[RowT, ...]
    reported_total: int
    received_at: datetime
    consistent: bool
    start_offset: int = 0

    @property
    def reached_end(self) -> bool:
        """Проверить, что чтение дошло до конца согласованного списка.

        Returns:
            Признак окончания списка, в том числе при чтении с середины.
        """
        return (
            self.consistent
            and self.start_offset + len(self.rows) == self.reported_total
        )

    @property
    def complete(self) -> bool:
        """Проверить, что прочитаны все заявленные строки.

        Returns:
            Признак согласованного полного набора.
        """
        return self.start_offset == 0 and self.reached_end
