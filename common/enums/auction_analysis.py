"""Единицы цен, округление и статусы оценки аукциона."""

from enum import StrEnum


class PriceBasis(StrEnum):
    """Единица исходной цены EXBO."""

    LOT_TOTAL = "lot_total"
    PER_UNIT = "per_unit"


class CommissionRounding(StrEnum):
    """Способ округления удержания за один выставляемый лот."""

    CEIL = "ceil"
    FLOOR = "floor"
    HALF_UP = "half_up"


class Q25Quality(StrEnum):
    """Степень подтверждения ценовой выборки."""

    VERIFIED = "verified"
    PRELIMINARY = "preliminary"
    UNAVAILABLE = "unavailable"


class AnalysisStatus(StrEnum):
    """Решение или причина ограничения оценки."""

    SELL_QUANTITY_MISSING = "sell_quantity_missing"
    VARIANT_UNVERIFIED = "variant_unverified"
    PRICE_RULES_UNVERIFIED = "price_rules_unverified"
    HISTORY_UNAVAILABLE = "history_unavailable"
    INSUFFICIENT_HISTORY = "insufficient_history"
    BUYOUT_UNAVAILABLE = "buyout_unavailable"
    LOW_LIQUIDITY = "low_liquidity"
    BELOW_ROI = "below_roi"
    SHARP_PRICE_DROP = "sharp_price_drop"
    CANDIDATE = "candidate"
