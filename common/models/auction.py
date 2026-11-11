"""Модели ответов аукциона и OAuth EXBO."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from common.dataclasses.auction import PageResult as PageResult
from common.dataclasses.auction import PageT as PageT
from common.utils.dates import parse_utc_datetime

_UtcDatetime = Annotated[datetime, BeforeValidator(parse_utc_datetime)]


class _ApiModel(BaseModel):
    """Строгая базовая модель ответа EXBO."""

    model_config = ConfigDict(strict=True, hide_input_in_errors=True)


class Lot(_ApiModel):
    """Активный лот с ценами, количеством и сроком действия."""

    item_id: str = Field(alias="itemId")
    amount: int
    start_price: int = Field(alias="startPrice")
    current_price: int | None = Field(default=None, alias="currentPrice")
    buyout_price: int | None = Field(default=None, alias="buyoutPrice")
    start_time: _UtcDatetime = Field(alias="startTime")
    end_time: _UtcDatetime = Field(alias="endTime")
    additional: dict[str, Any] | None = None


class LotPage(_ApiModel):
    """Страница активных лотов с общим числом записей."""

    total: int = Field(ge=0)
    lots: list[Lot]


class Sale(_ApiModel):
    """Запись истории с количеством предметов и ценой из EXBO."""

    amount: int
    price: int
    time: _UtcDatetime
    additional: dict[str, Any] | None = None


class HistoryPage(_ApiModel):
    """Страница истории продаж с общим числом записей."""

    total: int = Field(ge=0)
    prices: list[Sale]
