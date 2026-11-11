"""Проверки значений при декодировании JSON."""


def reject_json_constant(_: str) -> None:
    """Отклонить числовую константу, которой нет в стандарте JSON.

    Args:
        _: Значение NaN, Infinity или -Infinity.

    Raises:
        ValueError: Декодер встретил недопустимую константу.
    """
    raise ValueError("invalid JSON constant")
