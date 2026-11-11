"""Подтверждённые правила аукциона по режимам доступа."""

from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from common.enums.auction import AuctionMode
from common.enums.auction_analysis import CommissionRounding, PriceBasis
from common.errors import AuctionConfigurationError
from common.models.analysis_rules import AnalysisRules


class _RulesSettings(BaseSettings):
    """Общие источники настроек для серверных правил цены."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8-sig",
        case_sensitive=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    rules: AnalysisRules | None


class _DemoRulesSettings(_RulesSettings):
    """Правила расчёта для Demo API."""

    rules: AnalysisRules | None = Field(
        default=None, validation_alias="STALZONE_DEMO_ANALYSIS_RULES"
    )


class _ProductionRulesSettings(_RulesSettings):
    """Правила расчёта для Production API."""

    rules: AnalysisRules = Field(
        default=AnalysisRules(
            buyout_price_basis=PriceBasis.LOT_TOTAL,
            history_price_basis=PriceBasis.LOT_TOTAL,
            sale_commission_bps=500,
            listing_cost_per_pack=0,
            commission_rounding=CommissionRounding.CEIL,
        ),
        validation_alias="STALZONE_PRODUCTION_ANALYSIS_RULES",
    )


def load_analysis_rules(mode: AuctionMode) -> AnalysisRules | None:
    """Прочитать подтверждённые правила только выбранного режима.

    Args:
        mode: Режим обращения к EXBO.

    Returns:
        Правила режима или None для Demo без заданных правил.

    Raises:
        AuctionConfigurationError: Правила заданы некорректно.
    """
    settings = (
        _DemoRulesSettings if mode is AuctionMode.DEMO
        else _ProductionRulesSettings
    )
    try:
        return settings().rules
    except (OSError, SettingsError, ValidationError, ValueError):
        raise AuctionConfigurationError(
            "Проверьте серверные настройки ценовых правил для этого режима."
        ) from None
