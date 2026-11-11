"""Проверки команды аукциона с временным JSON и снимком."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main as cli
from common.errors import CatalogError
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import StoredHistory
from common.errors.clickhouse import HistoryStorageError
from common.models.auction import LotPage, HistoryPage
from common.settings import ItemCatalogSettings


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    """Записать свой список предметов во временный каталог.

    Args:
        tmp_path: Каталог pytest для временных файлов.

    Returns:
        Путь к JSON с двумя похожими названиями.
    """
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {"item_id": "Med1", "name_ru": "Морфин"},
                    {"item_id": "med2", "name_ru": "Морфин усиленный"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _mock_storage(
    monkeypatch: pytest.MonkeyPatch, lots: dict, history: dict, *,
    unavailable: bool = False,
) -> AsyncMock:
    """Подставить сохранённые страницы для команды CLI.

    Args:
        monkeypatch: Фикстура для подмены хранилища.
        lots: Исходные активные лоты.
        history: Сохранённые сделки.
        unavailable: Имитировать ошибку подключения к базе.

    Returns:
        Подставной запрос последнего опубликованного снимка.
    """
    at = datetime(2026, 11, 7, 8, tzinfo=UTC)
    lot_page = LotPage.model_validate(lots)
    sale_page = HistoryPage.model_validate(history)
    latest = AsyncMock(return_value=SimpleNamespace(received_at=at))
    scanner = SimpleNamespace(latest=latest, lots=AsyncMock(return_value=(
        PageScan(tuple(lot_page.lots), lot_page.total, at, True)
    )))
    sales = SimpleNamespace(read_history=AsyncMock(return_value=(
        StoredHistory(tuple(sale_page.prices), None)
    )))

    @asynccontextmanager
    async def session():
        """Выдать подставные репозитории.

        Yields:
            История и снимки.
        """
        if unavailable:
            raise HistoryStorageError("ClickHouse недоступен.")
        yield sales, scanner

    monkeypatch.setattr(cli, "AuctionStorage", lambda settings: (
        SimpleNamespace(session=session, close=AsyncMock())
    ))
    return latest


@pytest.mark.parametrize("as_json", [False, True])
def test_inspect_reads_two_pages(
    catalog_path: Path,
    as_json: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Проверить выбор предмета, запросы и точность вывода.

    Args:
        catalog_path: Временный JSON со списком предметов.
        as_json: Проверить JSON вместо текста.
        monkeypatch: Фикстура для подмены фабрики клиента.
        capsys: Фикстура для чтения stdout и stderr.
    """
    large_price = 2**53 + 1
    lots = {
        "total": 1,
        "lots": [
            {
                "itemId": "Med1",
                "amount": 2,
                "startPrice": large_price,
                "buyoutPrice": 0,
                "startTime": "2026-11-07T08:00:00Z",
                "endTime": "2026-11-07T09:00:00Z",
                "additional": {},
            }
        ],
        "unknownField": True,
    }
    history = {
        "total": 1,
        "prices": [
            {
                "amount": 2,
                "price": large_price,
                "time": "2026-11-07T08:00:00Z",
            }
        ],
    }
    latest = _mock_storage(monkeypatch, lots, history)
    args = [
        "  МОРФИН ",
        "--catalog",
        str(catalog_path),
        "--region",
        "ru",
        "--limit",
        "1",
    ]
    if as_json:
        args.append("--json")

    assert cli.main(args) == 0
    output = capsys.readouterr()

    key = latest.await_args.args[0]
    assert (key.item_id, key.region, key.mode.value) == ("Med1", "RU", "demo")
    assert output.err == ""
    if as_json:
        data = json.loads(output.out)
        assert data["mode"] == "demo"
        assert data["region"] == "RU"
        assert data["item"] == {
            "item_id": "Med1",
            "name_ru": "Морфин",
            "category": None,
        }
        assert data["lots"]["raw"]["lots"][0]["startPrice"] == large_price
        assert data["history"]["raw"]["prices"][0]["price"] == large_price
        assert data["history"]["received_at"].endswith("+00:00")
    else:
        assert "Активные лоты" in output.out
        assert "История продаж" in output.out
        assert f"2 | {large_price} | — | 0 |" in output.out
        assert " | {}" in output.out


@pytest.mark.parametrize(
    ("query", "diagnostic"),
    [("морф", "Морфин усиленный | med2"), ("неизвестный", "не найден")],
)
def test_local_error_prevents_storage_open(
    catalog_path: Path,
    query: str,
    diagnostic: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Проверить код 2 до открытия ClickHouse.

    Args:
        catalog_path: Временный JSON со списком предметов.
        query: Неоднозначный или отсутствующий предмет.
        diagnostic: Ожидаемый текст в stderr.
        monkeypatch: Фикстура для подмены фабрики клиента.
        capsys: Фикстура для чтения stdout и stderr.
    """
    monkeypatch.setattr(
        cli,
        "AuctionStorage",
        lambda settings: pytest.fail("storage opened before item selection"),
    )

    code = cli.main([query, "--catalog", str(catalog_path)])
    output = capsys.readouterr()

    assert code == 2
    assert output.out == ""
    assert diagnostic in output.err


@pytest.mark.parametrize("unavailable", [False, True])
def test_empty_pages_and_storage_error(
    catalog_path: Path, unavailable: bool, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Проверить пустые страницы и ошибку базы.

    Args:
        catalog_path: Временный JSON со списком предметов.
        unavailable: Имитировать отсутствие доступа к ClickHouse.
        monkeypatch: Фикстура для подмены хранилища.
        capsys: Фикстура для чтения stdout и stderr.
    """
    _mock_storage(
        monkeypatch, {"total": 0, "lots": []}, {"total": 0, "prices": []},
        unavailable=unavailable,
    )
    code = cli.main(["Med1", "--catalog", str(catalog_path), "--limit", "0"])
    output = capsys.readouterr()
    if unavailable:
        assert code == 1 and output.out == ""
        assert "ClickHouse недоступен" in output.err
    else:
        assert code == 0 and output.err == ""
        assert output.out.count("Пустая страница.") == 2


def test_config_rejects_duplicate_ids(catalog_path: Path) -> None:
    """Проверить дубли в пользовательских настройках.

    Args:
        catalog_path: Временный JSON со списком предметов.
    """
    config = json.loads(catalog_path.read_text(encoding="utf-8"))
    config["items"][1]["item_id"] = config["items"][0]["item_id"]
    catalog_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(CatalogError, match="повтор ID") as caught:
        ItemCatalogSettings.load(catalog_path)

    assert str(catalog_path) in str(caught.value)
