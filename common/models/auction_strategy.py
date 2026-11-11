"""Пользовательские параметры стратегии перепродажи."""

from pydantic import BaseModel, ConfigDict, Field

from common.models.analysis_rules import BASIS_POINTS


class StrategySettings(BaseModel):
    """Минимальная выборка продаж и порог ROI консервативной модели."""

    model_config = ConfigDict(frozen=True)

    min_sales: int = Field(default=20, ge=5, le=200)
    min_roi_bps: int = Field(
        default=1000, ge=0, le=BASIS_POINTS, multiple_of=10
    )
