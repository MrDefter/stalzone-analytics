"""Автономные проверки чтения каталога RU и поиска."""

import json
from pathlib import Path

import pytest

from common.catalog import ItemCatalog
from common.dataclasses import CatalogItem
from common.errors import AmbiguousItemError, CatalogError, ItemNotFoundError


def _write_item(
    root: Path, relative_file: str, payload: dict[str, object]
) -> Path:
    """Записать тестовый предмет в дерево ru/items.

    Args:
        root: Временный корень каталога.
        relative_file: Путь к файлу внутри ru/items.
        payload: Поля предмета для сериализации в JSON.

    Returns:
        Путь к записанному файлу.
    """
    path = root / "ru" / "items" / relative_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def catalog(tmp_path: Path) -> ItemCatalog:
    """Подготовить каталог с обоими форматами имени и пересечениями.

    Args:
        tmp_path: Временный каталог pytest.

    Returns:
        Каталог с тремя предметами из вложенных папок.
    """
    _write_item(
        tmp_path,
        "z/medicine/MiX9.json",
        {
            "id": "MiX9",
            "category": "medicine/injector",
            "name": {
                "type": "translation",
                "lines": {"ru": "  Морфин  ", "en": "Morphine"},
            },
        },
    )
    _write_item(
        tmp_path,
        "a/medicine/boost.json",
        {
            "id": "boost",
            "category": "medicine/booster",
            "name": {"type": "text", "text": "Морфин  усиленный"},
        },
    )
    _write_item(
        tmp_path,
        "other/byname.json",
        {
            "id": "byname",
            "category": "other",
            "name": {"type": "text", "text": "MiX9"},
        },
    )
    return ItemCatalog.load(tmp_path)


def test_read_nested_catalog(catalog: ItemCatalog) -> None:
    """Проверить извлечение обоих форматов имени и категории.

    Args:
        catalog: Каталог предметов с исходными именами.
    """
    assert catalog.find("MiX9") == CatalogItem(
        "MiX9", "  Морфин  ", "medicine/injector"
    )
    assert catalog.find("boost") == CatalogItem(
        "boost", "Морфин  усиленный", "medicine/booster"
    )
    assert catalog.find("MiX9").label == "  Морфин  "


@pytest.mark.parametrize(
    ("query", "item_id"),
    [
        ("MiX9", "MiX9"),
        ("mix9", "byname"),
        ("  МОРФИН\t", "MiX9"),
        ("морфин \t усиленный", "boost"),
        (" усиленный ", "boost"),
    ],
)
def test_search_priority(
    catalog: ItemCatalog, query: str, item_id: str
) -> None:
    """Проверить приоритет ID, полного имени и частичного совпадения.

    Args:
        catalog: Каталог с пересекающимися именами и ID.
        query: Исходный запрос пользователя.
        item_id: Ожидаемый точный ID.
    """
    assert catalog.find(query).item_id == item_id


def test_partial_match_reports_sorted_candidates(
    catalog: ItemCatalog,
) -> None:
    """Проверить порядок и содержимое списка частичных совпадений.

    Args:
        catalog: Каталог с двумя подходящими названиями.
    """
    with pytest.raises(AmbiguousItemError, match="точным ID") as caught:
        catalog.find("морф")

    assert caught.value.matches == (
        CatalogItem("MiX9", "  Морфин  ", "medicine/injector"),
        CatalogItem("boost", "Морфин  усиленный", "medicine/booster"),
    )


def test_exact_ambiguity_excludes_partial_matches(tmp_path: Path) -> None:
    """Проверить приоритет нескольких полных совпадений над частью.

    Args:
        tmp_path: Временный каталог pytest.
    """
    for item_id, name in (
        ("b1", "Морфин"),
        ("a1", "морфин"),
        ("other", "Морфин усиленный"),
    ):
        _write_item(
            tmp_path,
            f"{item_id}.json",
            {
                "id": item_id,
                "category": "medicine",
                "name": {"type": "text", "text": name},
            },
        )

    with pytest.raises(AmbiguousItemError) as caught:
        ItemCatalog.load(tmp_path).find("МОРФИН")

    assert [item.item_id for item in caught.value.matches] == ["a1", "b1"]


@pytest.mark.parametrize(
    "name_fields",
    [
        {},
        {"name": None},
        {"name": {"type": "translation", "lines": {"en": "Item"}}},
        {"name": {"type": "text", "text": " \t "}},
    ],
)
def test_unnamed_item_is_found_by_exact_id(
    tmp_path: Path, name_fields: dict[str, object]
) -> None:
    """Проверить подпись и точный поиск для предмета без имени.

    Args:
        tmp_path: Временный каталог pytest.
        name_fields: Поле name без читаемого русского имени.
    """
    _write_item(
        tmp_path,
        "OpaqueID.json",
        {"id": "OpaqueID", "category": "other", **name_fields},
    )
    catalog = ItemCatalog.load(tmp_path)

    item = catalog.find("OpaqueID")
    assert item.name_ru is None
    assert item.label == "OpaqueID"
    with pytest.raises(ItemNotFoundError, match="не найден"):
        catalog.find("opaqueid")
    with pytest.raises(ItemNotFoundError, match="не найден"):
        catalog.find("Opaque")


@pytest.mark.parametrize("query", ["Несуществующий предмет", " \t "])
def test_missing_item(catalog: ItemCatalog, query: str) -> None:
    """Проверить ошибку для отсутствующего или пустого запроса.

    Args:
        catalog: Каталог предметов для поиска.
        query: Запрос без совпадений.
    """
    with pytest.raises(ItemNotFoundError):
        catalog.find(query)


@pytest.mark.parametrize("create_directory", [False, True])
def test_missing_or_empty_item_directory(
    tmp_path: Path, create_directory: bool
) -> None:
    """Проверить диагностику пути к каталогу без JSON-файлов.

    Args:
        tmp_path: Временный каталог pytest.
        create_directory: Создать ru/items с посторонним файлом.
    """
    items_dir = tmp_path / "ru" / "items"
    if create_directory:
        items_dir.mkdir(parents=True)
        (items_dir / "notes.txt").write_text("ignored", encoding="utf-8")

    with pytest.raises(CatalogError) as caught:
        ItemCatalog.load(tmp_path)

    assert str(items_dir) in str(caught.value)


@pytest.mark.parametrize(
    ("content", "diagnostic"),
    [
        (b'{"id":', "Некорректный JSON"),
        (b"\xff", "Некорректный JSON"),
        (b"[]", "JSON-объект"),
        (
            b'{"id":"bad","category":"other","extra":NaN}',
            "Некорректный JSON",
        ),
        (b'{"id":"Bad","category":"other"}', "Поле id"),
        (b'{"id":"bad","category":0}', "Поле category"),
    ],
)
def test_invalid_item_file(
    tmp_path: Path, content: bytes, diagnostic: str
) -> None:
    """Проверить локальную ошибку с путём повреждённого файла.

    Args:
        tmp_path: Временный каталог pytest.
        content: Содержимое невалидной записи.
        diagnostic: Ожидаемый фрагмент сообщения.
    """
    path = tmp_path / "ru" / "items" / "nested" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    with pytest.raises(CatalogError, match=diagnostic) as caught:
        ItemCatalog.load(tmp_path)

    assert str(path) in str(caught.value)


def test_duplicate_id_reports_both_files(tmp_path: Path) -> None:
    """Проверить сообщение об одном ID в двух разных файлах.

    Args:
        tmp_path: Временный каталог pytest.
    """
    payload = {"id": "same", "category": "other"}
    first = _write_item(tmp_path, "a/same.json", payload)
    second = _write_item(tmp_path, "b/same.json", payload)

    with pytest.raises(CatalogError, match="Повтор ID") as caught:
        ItemCatalog.load(tmp_path)

    assert str(first) in str(caught.value)
    assert str(second) in str(caught.value)
