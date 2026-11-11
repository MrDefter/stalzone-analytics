"""Имена и расположение локальных иконок предметов."""

import re
from pathlib import Path

ITEM_IMAGES_DIR = (
    Path(__file__).resolve().parents[1] / "frontend" / "item_images"
)
_WINDOWS_DEVICE = re.compile(r"con|prn|aux|nul|(?:com|lpt)[0-9]", re.I)


def item_icon_filename(item_id: str) -> str | None:
    """Получить безопасное имя PNG с сохранением регистра ID.

    Args:
        item_id: Точный идентификатор предмета.

    Returns:
        Имя файла или None для ID, который нельзя использовать как имя.
    """
    if re.fullmatch(r"[A-Za-z0-9_-]{1,251}", item_id) is None:
        return None
    if _WINDOWS_DEVICE.fullmatch(item_id):
        return None
    return f"{item_id}.png"
