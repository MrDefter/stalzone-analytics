"""Ошибки загрузки локального каталога и поиска предметов."""

from common.dataclasses import CatalogItem


class CatalogError(Exception):
    """Ошибка при чтении каталога или выборе предмета."""


class ItemNotFoundError(CatalogError):
    """Каталог не содержит совпадений по запросу."""


class AmbiguousItemError(CatalogError):
    """Запросу соответствует несколько предметов."""

    def __init__(self, query: str, matches: tuple[CatalogItem, ...]) -> None:
        """Сохранить найденные варианты для вывода пользователю.

        Args:
            query: Исходный запрос пользователя.
            matches: Отсортированные совпадения с именем и категорией.
        """
        super().__init__(
            f"По запросу {query!r} найдено несколько предметов. "
            "Повторите поиск с точным ID."
        )
        self.matches = matches
