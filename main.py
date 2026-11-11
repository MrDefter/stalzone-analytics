"""Точка входа для веб-сервиса и просмотра аукциона STALZONE."""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

from common.catalog import ItemCatalog
from common.database.clickhouse.auction_storage import AuctionStorage
from common.dataclasses import PageResult
from common.dataclasses.auction_history import HistoryKey
from common.enums.auction import AuctionMode
from common.errors import AmbiguousItemError, CatalogError
from common.errors.clickhouse import HistoryStorageError
from common.models.auction import HistoryPage, LotPage
from common.settings import ItemCatalogSettings
from common.settings.auction_storage import AuctionStorageSettings
from stalzone_analytics.backend.cli.auction_output import (
    format_json,
    format_text,
)
from stalzone_analytics.backend.supervisor import supervise_engine

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def _limit_value(value: str) -> int:
    """Разобрать лимит страницы из аргумента команды.

    Args:
        value: Значение, переданное в --limit.

    Returns:
        Целое число от 0 до 200.

    Raises:
        argparse.ArgumentTypeError: Значение не входит в диапазон.
    """
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ожидается целое число") from None
    if not 0 <= limit <= 200:
        raise argparse.ArgumentTypeError("лимит должен быть от 0 до 200")
    return limit


def _port_value(value: str) -> int:
    """Разобрать порт веб-сервиса из аргумента команды.

    Args:
        value: Значение, переданное в --port.
    Returns:
        Целое число от 0 до 65535.
    Raises:
        argparse.ArgumentTypeError: Недопустимый номер порта.
    """
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ожидается целое число") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("порт должен быть от 0 до 65535")
    return port


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Разобрать параметры веб-сервиса или просмотра аукциона.

    Args:
        argv: Аргументы без имени команды или None для sys.argv.

    Returns:
        Проверенные параметры запуска.
    """
    parser = argparse.ArgumentParser(
        description="Веб-интерфейс и просмотр аукциона STALZONE Analytics.",
        epilog=(
            "Без ITEM запускается веб-интерфейс. "
            "Пример просмотра аукциона — "
            'python main.py "Стандартные запчасти" '
            "--mode production. Для demo нужен STALZONE_DEMO_TOKEN. "
            "Для production нужны STALZONE_CLIENT_ID и "
            "STALZONE_CLIENT_SECRET."
        ),
    )
    web = parser.add_argument_group("Веб-сервис")
    web.add_argument(
        "--host", metavar="HOST", help=f"Адрес (по умолчанию {_DEFAULT_HOST})"
    )
    web.add_argument(
        "--port",
        type=_port_value,
        metavar="PORT",
        help=f"Порт (по умолчанию {_DEFAULT_PORT})",
    )
    auction = parser.add_argument_group("Просмотр аукциона")
    auction.add_argument(
        "item",
        nargs="?",
        metavar="ITEM",
        help="Русское имя, часть имени или точный ID",
    )
    auction.add_argument(
        "--catalog",
        type=Path,
        metavar="PATH",
        help="Свой JSON со списком предметов (по умолчанию файл проекта)",
    )
    auction.add_argument(
        "--region",
        type=str.upper,
        choices=("RU",),
        help="Регион аукциона",
    )
    auction.add_argument(
        "--mode",
        choices=("demo", "production"),
        help="Режим доступа (по умолчанию demo)",
    )
    auction.add_argument(
        "--limit",
        type=_limit_value,
        help="Записей на странице от 0 до 200 (по умолчанию 10)",
    )
    auction.add_argument(
        "--json", dest="as_json", action="store_true", help="Вывод в JSON"
    )
    args = parser.parse_args(argv)
    if args.item is None:
        auction_values = (args.catalog, args.region, args.mode, args.limit)
        if args.as_json or any(value is not None for value in auction_values):
            parser.error("Для параметров аукциона укажите ITEM.")
        args.host = args.host or _DEFAULT_HOST
        if args.port is None:
            args.port = _DEFAULT_PORT
    else:
        if args.host is not None or args.port is not None:
            parser.error("Для запуска веб-сервиса уберите ITEM.")
        args.region = args.region or "RU"
        args.mode = args.mode or "demo"
        if args.limit is None:
            args.limit = 10
    return args


async def _inspect_auction(
    mode: str, region: str, item_id: str, limit: int
) -> tuple[PageResult[LotPage], PageResult[HistoryPage]]:
    """Прочитать опубликованные данные для команды CLI.

    Args:
        mode: Режим доступа, demo или production.
        region: Код региона.
        item_id: Точный ID выбранного предмета.
        limit: Максимальное число записей на каждой странице.

    Returns:
        Пара из страницы лотов и страницы истории.
    """
    key = HistoryKey(item_id, region, AuctionMode(mode))
    settings = AuctionStorageSettings()
    storage = AuctionStorage(settings)
    try:
        async with storage.session() as (history, db):
            meta = await db.latest(key)
            if meta is None:
                raise HistoryStorageError(
                    "Опубликованный снимок предмета ещё отсутствует."
                )
            scan = await db.lots(key, meta)
            now = datetime.now(UTC)
            stored = await history.read_history(
                key, now - timedelta(days=settings.ttl_days), now,
            )
        lots = LotPage(total=scan.reported_total, lots=list(scan.rows[:limit]))
        sales = HistoryPage(
            total=len(stored.rows), prices=list(stored.rows[:limit]),
        )
        end = stored.checkpoint.covered_until if stored.checkpoint else None
        return (
            PageResult(lots, lots.model_dump(mode="json", by_alias=True),
                       meta.received_at, {}),
            PageResult(sales, sales.model_dump(mode="json"), end or now, {}),
        )
    finally:
        await storage.close()


def main(argv: Sequence[str] | None = None) -> int:
    """Запустить веб-сервис или вывести обе страницы аукциона.

    Args:
        argv: Аргументы команды или None для чтения sys.argv.

    Returns:
        Код завершения 0, 1 для ошибки API или 2 для локальной ошибки.
    """
    args = _parse_args(argv)
    if args.item is None:
        with supervise_engine():
            uvicorn.run(
                "stalzone_analytics.app:app", host=args.host, port=args.port,
                workers=1,
            )
        return 0

    try:
        settings = ItemCatalogSettings.load(args.catalog)
        catalog = ItemCatalog({item.item_id: item for item in settings.items})
        item = catalog.find(args.item)
        lots, history = asyncio.run(
            _inspect_auction(args.mode, args.region, item.item_id, args.limit)
        )
    except AmbiguousItemError as exc:
        print(exc, file=sys.stderr)
        for match in exc.matches:
            category = match.category if match.category is not None else "—"
            print(
                f"  {match.label} | {match.item_id} | {category}",
                file=sys.stderr,
            )
        return 2
    except (CatalogError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    except HistoryStorageError as exc:
        print(exc, file=sys.stderr)
        return 1

    formatter = format_json if args.as_json else format_text
    try:
        output = formatter(args.mode, args.region, item, lots, history)
    except (TypeError, ValueError):
        print("Не удалось вывести данные аукциона.", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
