"""Чтение локального каталога RU и поиск предметов."""

import json
from pathlib import Path
from typing import Self

from common.dataclasses import CatalogItem
from common.errors import AmbiguousItemError, CatalogError, ItemNotFoundError
from common.utils.json import reject_json_constant


def _name_from_catalog(value: object) -> str | None:
    """Получить имя предмета из текстового объекта EXBO.

    Args:
        value: Значение поля name из файла каталога.

    Returns:
        Исходное русское имя или None при отсутствии читаемого имени.
    """
    if not isinstance(value, dict):
        return None
    name_type = value.get("type")
    if name_type == "translation":
        lines = value.get("lines")
        name = lines.get("ru") if isinstance(lines, dict) else None
    elif name_type == "text":
        name = value.get("text")
    else:
        return None
    return name if isinstance(name, str) and name.strip() else None


def _read_item(path: Path) -> CatalogItem:
    """Прочитать и проверить один файл предмета.

    Args:
        path: Путь к JSON-файлу, имя которого задаёт ожидаемый ID.

    Returns:
        Предмет с данными из файла.

    Raises:
        CatalogError: Файл недоступен или содержит неверную запись.
    """
    try:
        with path.open(encoding="utf-8-sig") as file:
            data = json.load(file, parse_constant=reject_json_constant)
    except OSError:
        raise CatalogError(f"Не удалось прочитать файл {path}.") from None
    except (ValueError, RecursionError):
        raise CatalogError(f"Некорректный JSON в файле {path}.") from None

    if not isinstance(data, dict):
        raise CatalogError(f"Файл {path} должен содержать JSON-объект.")
    item_id = data.get("id")
    if not isinstance(item_id, str) or item_id != path.stem:
        raise CatalogError(
            f"Поле id в файле {path} должно совпадать с именем без .json."
        )
    category = data.get("category")
    if not isinstance(category, str):
        raise CatalogError(
            f"Поле category в файле {path} должно быть строкой."
        )
    return CatalogItem(
        item_id=item_id,
        name_ru=_name_from_catalog(data.get("name")),
        category=category,
    )


def _normalize_name(name: str) -> str:
    """Привести имя к форме для сравнения.

    Args:
        name: Название предмета или запрос по имени.

    Returns:
        Имя после casefold() с одиночными пробелами между словами.
    """
    return " ".join(name.casefold().split())


class ItemCatalog:
    """Загруженные предметы RU с поиском по ID и имени."""

    def __init__(self, items: dict[str, CatalogItem]) -> None:
        """Сохранить набор предметов для поиска.

        Args:
            items: Предметы, сгруппированные по точному ID.
        """
        self._items = items.copy()

    @classmethod
    def load(cls, root: Path) -> Self:
        """Загрузить JSON-файлы из дерева ru/items.

        Args:
            root: Локальный корень каталога EXBO.

        Returns:
            Каталог всех предметов из дерева ru/items.

        Raises:
            CatalogError: Каталог отсутствует, пуст или повреждён.
        """
        items_dir = root.absolute() / "ru" / "items"
        try:
            if not items_dir.is_dir():
                raise CatalogError(
                    f"Каталог RU не найден по пути {items_dir}."
                )
            paths = sorted(
                path
                for path in items_dir.rglob("*.json")
                if path.suffix == ".json" and path.is_file()
            )
        except OSError:
            raise CatalogError(
                f"Не удалось прочитать каталог {items_dir}."
            ) from None
        if not paths:
            raise CatalogError(f"В каталоге {items_dir} нет JSON-файлов.")

        items = {}
        sources = {}
        for path in paths:
            item = _read_item(path)
            previous = sources.get(item.item_id)
            if previous is not None:
                raise CatalogError(
                    f"Повтор ID {item.item_id!r} в файлах {previous} и {path}."
                )
            items[item.item_id] = item
            sources[item.item_id] = path
        return cls(items)

    def find(self, query: str) -> CatalogItem:
        """Найти предмет по ID, полному имени или части имени.

        Args:
            query: Точный ID или фрагмент имени предмета.

        Returns:
            Единственный предмет на первом уровне совпадений.

        Raises:
            ItemNotFoundError: Совпадений нет.
            AmbiguousItemError: Найдено несколько подходящих предметов.
        """
        by_id = self._items.get(query)
        if by_id is not None:
            return by_id

        name_query = _normalize_name(query)
        if not name_query:
            raise ItemNotFoundError("Укажите имя или точный ID предмета.")

        exact = []
        partial = []
        for item in self._items.values():
            if item.name_ru is None:
                continue
            name = _normalize_name(item.name_ru)
            if name == name_query:
                exact.append(item)
            elif name_query in name:
                partial.append(item)

        matches = exact or partial
        if not matches:
            raise ItemNotFoundError(f"Предмет по запросу {query!r} не найден.")
        if len(matches) > 1:
            matches.sort(
                key=lambda item: (_normalize_name(item.label), item.item_id)
            )
            raise AmbiguousItemError(query, tuple(matches))
        return matches[0]
