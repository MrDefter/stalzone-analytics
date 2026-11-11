"""HTTP-маршрут чтения настроенного каталога предметов."""

from fastapi import APIRouter, HTTPException, Request, Response

from common.errors import CatalogError
from common.settings.items import ItemCatalogSettings

from ..icon_files import ITEM_IMAGES_DIR, item_icon_filename
from ..models.responses.items import ItemResponse, ItemsResponse

router = APIRouter()


def _available_image_names() -> set[str]:
    """Собрать имена локальных изображений с точным регистром.

    Returns:
        Имена доступных файлов или пустое множество при ошибке чтения.
    """
    try:
        return {
            path.name
            for path in ITEM_IMAGES_DIR.iterdir()
            if (
                path.suffix == ".png"
                and path.is_file()
                and not path.is_symlink()
            )
        }
    except OSError:
        return set()


@router.get("/api/items")
def list_items(request: Request, response: Response) -> ItemsResponse:
    """Отдать актуальный локальный каталог и ссылки на доступные PNG.

    Args:
        request: Запрос для построения путей к изображениям.
        response: Ответ для установки заголовка кеширования.

    Returns:
        Предметы из конфигурации с сохранением их порядка.

    Raises:
        HTTPException: Конфигурация недоступна или некорректна.
    """
    try:
        settings = ItemCatalogSettings.load(allow_empty=True)
    except CatalogError:
        raise HTTPException(
            status_code=500,
            detail="Не удалось загрузить каталог предметов.",
        ) from None

    response.headers["Cache-Control"] = "no-store"
    image_names = _available_image_names()
    items = []
    for item in settings.items:
        filename = item_icon_filename(item.item_id)
        image_url = (
            request.url_for("item_images", path=filename).path
            if filename is not None and filename in image_names
            else None
        )
        items.append(
            ItemResponse(
                item_id=item.item_id,
                name_ru=item.label,
                sell_quantity=item.sell_quantity,
                homogeneous=item.homogeneous,
                image_url=image_url,
            )
        )
    return ItemsResponse(items=items)
