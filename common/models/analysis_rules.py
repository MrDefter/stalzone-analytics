"""Правила расчёта валовой цены и расходов."""

from pydantic import BaseModel, ConfigDict, Field

from common.enums.auction_analysis import CommissionRounding, PriceBasis

BASIS_POINTS = 10_000


class AnalysisRules(BaseModel):
    """Явные правила цен и затрат для одного режима доступа.

    Attributes:
        buyout_price_basis: Единица цены выкупа в лотах.
        history_price_basis: Устаревший параметр единиц цены истории.
        sale_commission_bps: Ставка комиссии в базисных пунктах.
        listing_cost_per_pack: Расход за выставление одного лота.
        min_roi_bps: Устаревший параметр для чтения старых конфигураций.
        commission_rounding: Подтверждённое округление или None.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )

    buyout_price_basis: PriceBasis
    history_price_basis: PriceBasis = PriceBasis.LOT_TOTAL
    sale_commission_bps: int = Field(ge=0, le=BASIS_POINTS, strict=True)
    listing_cost_per_pack: int = Field(ge=0, strict=True)
    min_roi_bps: int = Field(default=1000, ge=0, strict=True)
    commission_rounding: CommissionRounding | None = None
