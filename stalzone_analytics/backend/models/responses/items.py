"""Ответ HTTP-маршрута локального каталога предметов."""

from pydantic import BaseModel


class ItemResponse(BaseModel):
    """Поля каталога и адрес локального изображения предмета."""

    item_id: str
    name_ru: str
    sell_quantity: int | None
    homogeneous: bool
    image_url: str | None


class ItemsResponse(BaseModel):
    """Предметы в порядке, заданном конфигурацией."""

    items: list[ItemResponse]
