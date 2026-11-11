"""Ошибки подключения к хранилищу истории."""


class HistoryStorageError(Exception):
    """ClickHouse не смог предоставить или сохранить историю продаж."""
