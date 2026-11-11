"""Параметры HTTP-запроса анализа аукциона."""

from typing import Literal

from pydantic import Field

from common.enums.auction import AuctionMode, HistoryDays
from common.models.auction_strategy import StrategySettings


class AuctionAnalysisQuery(StrategySettings):
    """Предмет, источник, окно графика и параметры стратегии."""

    item_id: str = Field(min_length=1)
    region: Literal["RU"] = "RU"
    mode: AuctionMode = AuctionMode.DEMO
    days: HistoryDays = HistoryDays.TWO_WEEKS
