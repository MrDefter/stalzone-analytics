"""Датакласс предмета из локального каталога."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field


@dataclass(frozen=True, slots=True)
class CatalogItem:
    """Предмет с исходными именем и категорией каталога.

    Attributes:
        item_id: Точный регистрозависимый ID предмета.
        name_ru: Имя из каталога или None при его отсутствии.
        category: Исходная категория или None при её отсутствии.
        sell_quantity: Точный размер пачки для оценки перепродажи.
        homogeneous: Подтверждена однородность для расчёта.
    """

    item_id: str
    name_ru: str | None
    category: str | None = None
    sell_quantity: Annotated[int, Field(gt=0, strict=True)] | None = None
    homogeneous: Annotated[bool, Field(strict=True)] = False

    @property
    def label(self) -> str:
        """Получить подпись предмета для вывода.

        Returns:
            Имя из каталога или точный ID при отсутствии имени.
        """
        return self.name_ru if self.name_ru is not None else self.item_id
