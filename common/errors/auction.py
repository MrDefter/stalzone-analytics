"""Исключения клиента аукциона EXBO."""


class AuctionConfigurationError(Exception):
    """Серверные настройки доступа или правил аукциона некорректны."""
