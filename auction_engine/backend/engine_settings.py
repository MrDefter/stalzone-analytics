"""Параметры параллельности и хранения, которыми управляет ядро."""

from pydantic import Field

from common.settings.auction_storage import AuctionStorageSettings


class AuctionEngineSettings(AuctionStorageSettings):
    """Лимиты операций ядра и срок хранения снимков."""

    max_active_items: int = Field(
        default=2, ge=1, le=16,
        validation_alias="STALZONE_ENGINE_MAX_ACTIVE_ITEMS",
    )
    max_http_concurrency: int = Field(
        default=8, ge=1, le=8,
        validation_alias="STALZONE_ENGINE_MAX_HTTP_CONCURRENCY",
    )
    requests_per_minute: int = Field(
        default=60, ge=1,
        validation_alias="STALZONE_ENGINE_REQUESTS_PER_MINUTE",
    )
    snapshots_ttl_days: int = Field(
        default=3, ge=1, le=30,
        validation_alias="STALZONE_ENGINE_SNAPSHOTS_TTL_DAYS",
    )
