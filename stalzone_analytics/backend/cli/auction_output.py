"""Текстовый и JSON-вывод страниц аукциона."""

import json
from datetime import datetime
from typing import Any

from common.dataclasses import CatalogItem, PageResult
from common.models.auction import HistoryPage, LotPage


def _row(*values: int | str | dict[str, Any] | None) -> str:
    """Собрать строку таблицы с исходными значениями.

    Args:
        *values: Значения ячеек, включая цену и атрибуты.

    Returns:
        Строка с разделителями и прочерком на месте None.
    """
    cells = []
    for value in values:
        if value is None:
            cells.append("—")
        elif isinstance(value, dict):
            cells.append(
                json.dumps(value, ensure_ascii=False, allow_nan=False)
            )
        else:
            cells.append(str(value))
    return " | ".join(cells)


def _section(
    title: str,
    columns: str,
    rows: list[str],
    *,
    total: int,
    received_at: datetime,
) -> str:
    """Собрать секцию со сведениями о странице.

    Args:
        title: Название секции.
        columns: Заголовки столбцов.
        rows: Готовые строки таблицы.
        total: Общее число записей по данным API.
        received_at: Время получения страницы в UTC.

    Returns:
        Секция с таблицей или подписью пустой страницы.
    """
    summary = (
        f"{title} — {len(rows)} на странице, всего {total}, "
        f"получено {received_at.isoformat()}"
    )
    if not rows:
        return f"{summary}\nПустая страница."
    return "\n".join((summary, columns, *rows))


def format_text(
    mode: str,
    region: str,
    item: CatalogItem,
    lots: PageResult[LotPage],
    history: PageResult[HistoryPage],
) -> str:
    """Сформировать две секции для просмотра в терминале.

    Args:
        mode: Режим доступа, demo или production.
        region: Код региона.
        item: Выбранный предмет.
        lots: Страница активных лотов.
        history: Страница истории продаж.

    Returns:
        Текст с заголовком предмета и обеими страницами.
    """
    lot_rows = [
        _row(
            lot.amount,
            lot.start_price,
            lot.current_price,
            lot.buyout_price,
            lot.start_time.isoformat(),
            lot.end_time.isoformat(),
            lot.additional,
        )
        for lot in lots.data.lots
    ]
    sale_rows = [
        _row(sale.amount, sale.price, sale.time.isoformat(), sale.additional)
        for sale in history.data.prices
    ]
    return "\n\n".join(
        (
            f"Режим {mode}, регион {region}\n"
            f"{item.label} ({item.item_id}), категория {_row(item.category)}",
            _section(
                "Активные лоты",
                "Количество | Стартовая цена | Текущая ставка | Выкуп | "
                "Выставление | Окончание | Атрибуты",
                lot_rows,
                total=lots.data.total,
                received_at=lots.received_at,
            ),
            _section(
                "История продаж",
                "Количество | Цена | Время продажи | Атрибуты",
                sale_rows,
                total=history.data.total,
                received_at=history.received_at,
            ),
        )
    )


def format_json(
    mode: str,
    region: str,
    item: CatalogItem,
    lots: PageResult[LotPage],
    history: PageResult[HistoryPage],
) -> str:
    """Сериализовать исходные ответы в один JSON-объект.

    Args:
        mode: Режим доступа, demo или production.
        region: Код региона.
        item: Выбранный предмет.
        lots: Страница активных лотов.
        history: Страница истории продаж.

    Returns:
        JSON с предметом, метаданными и исходными страницами.
    """
    return json.dumps(
        {
            "mode": mode,
            "region": region,
            "item": {
                "item_id": item.item_id,
                "name_ru": item.label,
                "category": item.category,
            },
            "lots": {
                "received_at": lots.received_at.isoformat(),
                "raw": lots.raw,
            },
            "history": {
                "received_at": history.received_at.isoformat(),
                "raw": history.raw,
            },
        },
        allow_nan=False,
    )
