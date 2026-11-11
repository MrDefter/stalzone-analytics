"""Синхронизация локальных PNG с каталогом предметов EXBO."""

import argparse
import json
import os
import struct
import sys
import zlib
from collections.abc import Collection, Sequence
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from urllib.request import urlopen

from common.errors import CatalogError
from common.settings.items import ItemCatalogSettings
from stalzone_analytics.backend.icon_files import (
    ITEM_IMAGES_DIR,
    item_icon_filename,
)

_RU_ROOT = (
    "https://raw.githubusercontent.com/EXBO-Studio/stalzone-database/main/ru"
)
_LISTING_LIMIT = 10 * 1024 * 1024
_ICON_LIMIT = 2 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DOWNLOAD_ERRORS = (OSError, HTTPException, ValueError)


def _download(url: str, limit: int) -> bytes:
    """Прочитать успешный HTTP-ответ в пределах заданного размера.

    Args:
        url: Адрес файла в публичном репозитории EXBO.
        limit: Максимальный размер ответа в байтах.

    Returns:
        Байты ответа.

    Raises:
        OSError: Не удалось получить файл по сети.
        ValueError: Ответ неуспешен или превышает ограничение.
    """
    with urlopen(url, timeout=15) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"превышен лимит в {limit} байт")
    return content


def _icon_paths(content: bytes, ids: Collection[str]) -> dict[str, str]:
    """Найти пути изображений нужных предметов в индексе RU.

    Args:
        content: Содержимое listing.json.
        ids: Точные ID предметов, для которых нужны иконки.

    Returns:
        Пути PNG из индекса по точным идентификаторам.

    Raises:
        ValueError: Индекс не содержит корректный JSON-массив.
    """
    listing = json.loads(content)
    if not isinstance(listing, list):
        raise ValueError("индекс EXBO должен содержать массив записей")

    paths = {}
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        data_path, icon_path = entry.get("data"), entry.get("icon")
        if not isinstance(data_path, str) or not isinstance(icon_path, str):
            continue
        data = PurePosixPath(data_path)
        if data.suffix != ".json" or data.stem not in ids:
            continue
        icon = PurePosixPath(icon_path)
        if (
            icon_path.startswith("/icons/")
            and icon.suffix == ".png"
            and ".." not in icon.parts
        ):
            paths[data.stem] = icon.as_posix()
    return paths


def _validate_png(content: bytes) -> None:
    """Проверить заголовок, размеры, чанки и контрольные суммы PNG.

    Args:
        content: Скачанные байты изображения.

    Raises:
        ValueError: Изображение не является целым PNG.
    """
    header = _PNG_SIGNATURE + b"\x00\x00\x00\rIHDR"
    if len(content) < 33 or not content.startswith(header):
        raise ValueError("ожидалось изображение PNG")
    width, height = struct.unpack_from(">II", content, 16)
    if not width or not height:
        raise ValueError("PNG содержит недопустимые размеры")

    offset = len(_PNG_SIGNATURE)
    has_pixels = False
    while offset + 12 <= len(content):
        size, chunk = struct.unpack_from(">I4s", content, offset)
        end = offset + 8 + size
        if end + 4 > len(content):
            break
        checksum = struct.unpack_from(">I", content, end)[0]
        if zlib.crc32(content[offset + 4:end]) != checksum:
            break
        if chunk == b"IDAT" and size:
            has_pixels = True
        if chunk == b"IEND":
            if has_pixels and size == 0 and end + 4 == len(content):
                return
            break
        offset = end + 4
    raise ValueError("PNG повреждён или загружен не полностью")


def _write_icon(path: Path, content: bytes) -> None:
    """Атомарно сохранить проверенный PNG в каталоге изображений.

    Args:
        path: Путь к итоговому файлу изображения.
        content: Проверенные байты PNG.
    """
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _pending_icons(
    ids: Sequence[str], force: bool
) -> tuple[dict[str, Path], int, int]:
    """Подготовить пути для загрузки с учётом существующих файлов.

    Args:
        ids: ID настроенных предметов в порядке конфигурации.
        force: Обновлять ранее сохранённые PNG.

    Returns:
        Пути для загрузки, число пропусков и число ошибок.
    """
    pending = {}
    skipped = failed = 0
    occupied = {
        os.path.normcase(path.name): path
        for path in ITEM_IMAGES_DIR.iterdir()
    }
    for item_id in ids:
        filename = item_icon_filename(item_id)
        if filename is None:
            print(f"{item_id!r}: недопустимое имя PNG.", file=sys.stderr)
            failed += 1
            continue

        key = os.path.normcase(filename)
        existing = occupied.get(key)
        if existing is not None:
            if (
                existing.name != filename
                or existing.is_symlink()
                or not existing.is_file()
            ):
                print(f"{filename}: конфликт имён файлов.", file=sys.stderr)
                failed += 1
                continue
            if not force:
                skipped += 1
                continue

        path = ITEM_IMAGES_DIR / filename
        occupied[key] = path
        pending[item_id] = path
    return pending, skipped, failed


def main(argv: Sequence[str] | None = None) -> int:
    """Загрузить иконки настроенных предметов и вывести итог.

    Args:
        argv: Аргументы утилиты без имени команды или None для sys.argv.

    Returns:
        Код 0 при успехе или 1 при ошибке синхронизации.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="Обновить существующие PNG"
    )
    args = parser.parse_args(argv)
    try:
        settings = ItemCatalogSettings.load(allow_empty=True)
        ITEM_IMAGES_DIR.mkdir(exist_ok=True)
        if ITEM_IMAGES_DIR.resolve() != ITEM_IMAGES_DIR:
            raise OSError("Каталог иконок должен быть рядом с items.json.")
        pending, skipped, failed = _pending_icons(
            [item.item_id for item in settings.items], args.force
        )
    except (CatalogError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1

    paths = {}
    if pending:
        try:
            content = _download(f"{_RU_ROOT}/listing.json", _LISTING_LIMIT)
            paths = _icon_paths(content, pending)
        except _DOWNLOAD_ERRORS as exc:
            print(f"Не удалось получить индекс EXBO. {exc}", file=sys.stderr)
            return 1

    downloaded = 0
    for item_id, target in pending.items():
        path = paths.get(item_id)
        if path is None:
            print(
                f"{item_id}: иконка не найдена в индексе.",
                file=sys.stderr,
            )
            failed += 1
            continue

        try:
            url = f"{_RU_ROOT}{quote(path)}"
            content = _download(url, _ICON_LIMIT)
            _validate_png(content)
            _write_icon(target, content)
        except _DOWNLOAD_ERRORS as exc:
            print(f"{item_id}: {exc}", file=sys.stderr)
            failed += 1
        else:
            print(f"{target.name} {url}")
            downloaded += 1

    print(f"Загружено {downloaded}. Пропущено {skipped}. Ошибок {failed}.")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
