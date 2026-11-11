"""Проверки веб-анализа с локальным каталогом и MockTransport."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from stalzone_analytics.backend.app import app
from common.dataclasses.auction import PageScan
from common.dataclasses.auction_history import (
    HistoryCheckpoint,
    HistoryKey,
    StoredHistory,
)
from common.enums.auction import AuctionMode
from common.enums.auction_analysis import CommissionRounding, PriceBasis
from common.enums.auction_history import HistorySyncStatus
from auction_engine.backend.integrations.auction.errors import RateLimitError
from common.errors.clickhouse import HistoryStorageError
from auction_engine.backend.integrations import AuctionClient
from common.models.analysis_rules import AnalysisRules
from common.models.auction import Lot, Sale
from common.models.auction_engine import ScannerCommand, SnapshotMetadata
from stalzone_analytics.backend.models.requests.auction_analysis import (
    AuctionAnalysisQuery,
)
from common.models.analysis_result import AnalysisResult
from auction_engine.backend import analysis as auction_analysis
from auction_engine.backend import auction_snapshot
from common.settings.auction_storage import AuctionStorageSettings
from common.engine_analysis import history_summary
from common.settings.items import items as catalog_settings
from stalzone_analytics.backend import auction as web_auction

pytestmark = pytest.mark.asyncio

_ENDPOINT = "/api/auction/analysis"
_ITEM_ID = "test-item"
_RULES = AnalysisRules(
    buyout_price_basis=PriceBasis.LOT_TOTAL,
    history_price_basis=PriceBasis.LOT_TOTAL,
    sale_commission_bps=333,
    listing_cost_per_pack=5,
    commission_rounding=CommissionRounding.FLOOR,
)


class _Market:
    """Снимок торгов и имитация ответов EXBO по offset и limit."""

    def __init__(self) -> None:
        """Подготовить продажи партий разных размеров."""
        now = datetime.now(UTC)
        start = (now - timedelta(hours=1)).isoformat()
        end = (now + timedelta(days=1)).isoformat()
        self.lots = [
            {
                "amount": 25, "startPrice": 100, "buyoutPrice": 100,
                "startTime": start, "endTime": end,
            },
            {
                "amount": 10, "startPrice": 1, "buyoutPrice": None,
                "startTime": start, "endTime": end,
            },
        ]
        self.sales = [
            {
                "amount": 10, "price": 500 + index * 10,
                "time": (now - timedelta(minutes=index + 2)).isoformat(),
            }
            for index in range(20)
        ]
        self.sales[:0] = [
            {"amount": 1, "price": 1, "time": now.isoformat()},
            {
                "amount": 5, "price": 1,
                "time": (now - timedelta(minutes=1)).isoformat(),
            },
        ]
        self.rules: dict[AuctionMode, AnalysisRules | None] = dict.fromkeys(
            AuctionMode, _RULES
        )
        self.calls: list[httpx.Request] = []
        self.failure: int | str | None = None
        self.failure_resource: str | None = None
        self.failure_offset: int | None = None
        self.drift = False
        self.history_hours = 30 * 24
        self.history_unavailable = False
        self.history_stale = False
        self.history_reads: list[HistoryKey] = []

    def respond(self, request: httpx.Request) -> httpx.Response:
        """Вернуть токен, страницу или заданную ошибку EXBO.

        Args:
            request: Запрос клиента аукциона.

        Returns:
            Ответ, построенный по параметрам запроса.
        """
        self.calls.append(request)
        if request.url.host == "exbo.net":
            return httpx.Response(
                200, json={"access_token": "fake-oauth", "expires_in": 3600}
            )
        item, resource = request.url.path.rsplit("/", 2)[-2:]
        offset = int(request.url.params["offset"])
        if (
            self.failure_resource in (None, resource)
            and self.failure_offset in (None, offset)
        ):
            if self.failure == "timeout":
                raise httpx.ReadTimeout("private fixture", request=request)
            if isinstance(self.failure, int):
                return httpx.Response(
                    self.failure,
                    headers={"Retry-After": "3"},
                    json={"secret": "private fixture"},
                )
        limit = int(request.url.params["limit"])
        if resource == "lots":
            records = [dict(lot, itemId=item) for lot in self.lots]
            key = "lots"
        else:
            records = self.sales
            key = "prices"
        page = records[offset:offset + limit]
        if self.drift and key == "prices" and offset and page:
            page[0] = dict(page[0], price=page[0]["price"] + 1)
        return httpx.Response(200, json={"total": len(records), key: page})


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Market:
    """Подключить мокированные клиенты и изолированную конфигурацию.

    Args:
        monkeypatch: Фикстура подмены источников и клиента.
        tmp_path: Папка временного каталога.

    Returns:
        Набор данных для обоих режимов.
    """
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps({"items": [
            {
                "item_id": _ITEM_ID, "name_ru": "Запчасти",
                "sell_quantity": 10, "homogeneous": True,
            },
            {
                "item_id": "variant", "name_ru": "Вариант",
                "sell_quantity": 10,
            },
            {
                "item_id": "missing", "name_ru": "Без размера графика",
                "homogeneous": True,
            },
        ]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_settings, "_ITEMS_FILE", path)
    source = _Market()
    cfg = AuctionStorageSettings(_env_file=None)
    source.pending = []
    source.snapshot_missing = False
    source.profile_pending = False
    source.snapshot_age = 0

    def stored() -> StoredHistory:
        """Подготовить сохранённый набор для ответов репозитория.

        Returns:
            Сделки и контрольная точка тестового предмета.
        """
        if source.history_unavailable:
            raise HistoryStorageError("История в ClickHouse недоступна.")
        now = datetime.now(UTC)
        until = now - timedelta(minutes=10) if source.history_stale else now
        checkpoint = HistoryCheckpoint(
            now, covered_from=until - timedelta(hours=source.history_hours),
            covered_until=until, last_success_at=until,
            initial_complete=True, backfill_complete=True,
            status=HistorySyncStatus.READY,
        )
        rows = tuple(Sale.model_validate(sale) for sale in source.sales)
        return StoredHistory(rows, checkpoint)

    async def read_at(
        key: HistoryKey, since: datetime, until: datetime,
        point: HistoryCheckpoint | None,
    ) -> StoredHistory:
        """Имитировать запрос продаж за период.

        Args:
            key: Ключ предмета.
            since: Начало периода.
            until: Конец периода.
            point: Выбранная контрольная точка.

        Returns:
            Сохранённые продажи за выбранный период.
        """
        source.history_reads.append(key)
        history = stored()
        return StoredHistory(
            tuple(row for row in history.rows if since <= row.time <= until),
            point,
        )

    async def checkpoint(key: HistoryKey) -> HistoryCheckpoint | None:
        """Прочитать контрольную точку предмета.

        Args:
            key: Ключ предмета.

        Returns:
            Подставная контрольная точка.
        """
        return stored().checkpoint

    async def latest(key: HistoryKey) -> SnapshotMetadata | None:
        """Вернуть маркер готовой версии.

        Args:
            key: Ключ предмета.

        Returns:
            Маркер или None до публикации.
        """
        if source.snapshot_missing:
            return None
        now = datetime.now(UTC)
        received = now - timedelta(seconds=source.snapshot_age)
        return SnapshotMetadata(
            snapshot_id="fixture", started_at=received, received_at=received,
            evaluated_at=now, published_at=now,
            expires_at=now + timedelta(days=3),
            lots_loaded=len(source.lots), lots_reported_total=len(source.lots),
            lots_complete=True, lots_consistent=True,
        )

    def profile(
        key: HistoryKey, item: object, query: AuctionAnalysisQuery,
        point: HistoryCheckpoint | None,
    ) -> str:
        """Кодировать параметры профиля для тестового репозитория.

        Args:
            key: Ключ предмета.
            item: Настройки каталога.
            query: Параметры анализа.
            point: Контрольная точка истории.

        Returns:
            Ключ тестовой оценки.
        """
        return f"{query.min_sales}:{query.min_roi_bps}"

    async def analysis(
        key: HistoryKey, meta: SnapshotMetadata, strategy: str,
    ) -> AnalysisResult | None:
        """Смоделировать готовую оценку точного профиля.

        Args:
            key: Ключ предмета.
            meta: Маркер опубликованной версии.
            strategy: Параметры готового расчёта.

        Returns:
            Оценка по фикстурам или None до её публикации.
        """
        if source.profile_pending:
            return None
        minimum, roi = map(int, strategy.split(":"))
        query = AuctionAnalysisQuery(
            item_id=key.item_id, mode=key.mode,
            min_sales=minimum, min_roi_bps=roi,
        )
        loaded = source.lots
        page_size = auction_snapshot._PAGE_LIMIT
        max_pages = auction_snapshot._MAX_LOT_PAGES
        loaded = loaded[:page_size + (page_size - 2) * (max_pages - 1)]
        scan = PageScan(
            tuple(Lot.model_validate(dict(row, itemId=key.item_id))
                  for row in loaded),
            len(source.lots), meta.received_at, True,
        )
        history = stored()
        result = auction_analysis.evaluate_lots(
            key, query, web_auction.catalog_item(key.item_id), scan, history,
            source.rules[key.mode],
            history_summary(history.checkpoint, cfg),
        )
        result.analysis_version = "revision-1"
        return result

    async def record(key: HistoryKey, command: ScannerCommand) -> None:
        """Сохранить поступившую команду.

        Args:
            key: Ключ предмета.
            command: Команда из веб-процесса.
        """
        source.pending.append((key, command))

    history = SimpleNamespace(
        read_at=read_at, get_checkpoint=checkpoint,
    )
    db = SimpleNamespace(
        latest=latest, analysis=analysis, record_command=record,
        temporary_profiles=AsyncMock(return_value=0),
        commands=AsyncMock(side_effect=lambda: list(source.pending)),
        command_status=AsyncMock(return_value=None),
        latest_state=AsyncMock(return_value=None),
        analysis_version=AsyncMock(return_value="revision-1"),
    )

    @asynccontextmanager
    async def session():
        """Предоставить подставные репозитории для запроса.

        Yields:
            История и опубликованные снимки.
        """
        yield history, db

    monkeypatch.setattr(web_auction, "analysis_key", profile)
    monkeypatch.setattr(
        app.state, "auction_storage",
        SimpleNamespace(settings=cfg, session=session),
        raising=False,
    )
    return source


async def _get(
    item_id: str = _ITEM_ID,
    *,
    mode: str = "demo",
    days: int = 14,
    endpoint: str = _ENDPOINT,
    **strategy: int | str,
) -> httpx.Response:
    """Запросить анализ через ASGI без локального HTTP-сервера.

    Args:
        item_id: Точный ID из временного каталога.
        mode: Режим доступа.
        days: Период графика.
        endpoint: Маршрут полного ответа или проверки версии.
        strategy: Параметры стратегии в строке запроса.

    Returns:
        Ответ маршрута приложения.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(
            endpoint,
            params={
                "item_id": item_id, "mode": mode, "days": days, **strategy,
            },
        )


async def test_confirmed_analysis_preserves_math_and_units(
    market: _Market,
) -> None:
    """Сверить цену партии и пропорциональные суммы для малого лота.

    Args:
        market: Снимок с правилами, отличающими комиссию от выставления.
    """
    market.lots[0]["amount"] = 6
    market.lots[1]["buyoutPrice"] = 2**53 + 1
    market.lots.append(dict(market.lots[0], buyoutPrice=None))
    response = await _get(days=1)
    assert response.status_code == 200
    payload = response.json()
    assert payload["item_id"] == _ITEM_ID
    assert payload["sample_count"] == payload["turnover_24h"] == 20
    assert payload["q25_per_pack"] == "540"
    assert payload["q25_quality"] == "verified"
    assert payload["model"]["price_window_hours"] == 3
    assert len(payload["history"]["sales"]) == 20
    assert {sale["amount"] for sale in payload["history"]["sales"]} == {10}
    assert payload["history"]["sales"][0]["price_per_pack"] == "500"
    candidate, expensive, bid = payload["lots"]
    assert candidate["status"] == "candidate"
    assert candidate["amount"] == 6
    assert candidate["buyout_total"] == "100"
    assert candidate["exit_price_per_pack"] == "540"
    assert candidate["net_revenue_per_pack"] == "518"
    assert candidate["gross_revenue_total"] == "324"
    assert candidate["commission_total"] == "11"
    assert candidate["listing_cost_total"] == "3"
    assert candidate["net_revenue_total"] == "310"
    assert candidate["profit_total"] == "210"
    assert candidate["roi_percent"] == 210
    assert candidate["max_buyout"] == "281"
    assert candidate["active_competing_packs"] == 1
    assert candidate["competition_to_turnover"] == 0.05
    assert not {"full_packs", "remainder", "exit_total"} & candidate.keys()
    assert expensive["buyout_total"] == str(2**53 + 1)
    assert expensive["active_competing_packs"] == 0
    assert bid["buyout_total"] is None
    assert bid["status"] == "buyout_unavailable"
    assert bid["reasons"] == ["buyout_unavailable"]
    assert len({lot["id"] for lot in payload["lots"]}) == 3
    longer = await _get(days=30)
    assert longer.json()["q25_per_pack"] == payload["q25_per_pack"]


@pytest.mark.parametrize("mode", ["demo", "production"])
async def test_mode_selects_published_rules(
    market: _Market, mode: str,
) -> None:
    """Читать результаты только для указанного режима.

    Args:
        market: Подставное хранилище результатов для обоих режимов.
        mode: Значение HTTP-параметра.
    """
    market.rules[AuctionMode.PRODUCTION] = _RULES.model_copy(
        update={"history_price_basis": PriceBasis.PER_UNIT}
    )
    market.rules[AuctionMode.DEMO] = None
    market.sales[-1]["price"] = 2**53 + 1
    response = await _get(mode=mode)
    assert response.status_code == 200
    assert not market.calls
    assert market.history_reads == [
        HistoryKey(_ITEM_ID, "RU", AuctionMode(mode)),
    ]
    payload = response.json()
    assert payload["mode"] == mode
    history = payload["history"]["sales"]
    assert [sale["price_per_pack"] for sale in history] == [
        str(sale["price"]) for sale in market.sales if sale["amount"] == 10
    ]
    if mode == "demo":
        assert "price_rules_unverified" in payload["reasons"]
        assert payload["model"]["sale_commission_bps"] is None
        for key in (
            "buyout_total", "exit_price_per_pack", "commission_total",
            "profit_total", "roi_percent",
        ):
            assert payload["lots"][0][key] is None
    assert payload["q25_per_pack"] == "540"
    assert payload["turnover_24h"] == 20


@pytest.mark.parametrize("amount", [20, 27])
async def test_strategy_settings_change_window_and_candidate(
    market: _Market, amount: int
) -> None:
    """Передать выборку и порог ROI из HTTP-запроса в расчёт.

    Args:
        market: Подставные лоты и история продаж.
        amount: Число предметов в покупаемом лоте.
    """
    buyout = 46 * amount
    market.lots[0].update(amount=amount, buyoutPrice=buyout)
    market.sales.append({
        "amount": 10, "price": 530,
        "time": (datetime.now(UTC) - timedelta(hours=4)).isoformat(),
    })
    market.rules[AuctionMode.DEMO] = _RULES.model_copy(
        update={"min_roi_bps": 9999}
    )
    default_response = await _get()
    strict_response = await _get(min_sales=21, min_roi_bps=1500)
    assert default_response.status_code == strict_response.status_code == 200
    baseline = default_response.json()
    changed = strict_response.json()
    assert baseline["model"]["price_window_hours"] == 3
    assert changed["model"]["price_window_hours"] == 24
    assert changed["model"]["min_sales"] == changed["sample_count"] == 21
    assert changed["model"]["min_roi_bps"] == 1500
    assert changed["sell_quantity"] == baseline["sell_quantity"] == 10
    first, second = baseline["lots"][0], changed["lots"][0]
    assert first["status"] == "candidate"
    assert second["status"] == "below_roi"
    assert first["roi_percent"] == second["roi_percent"]
    assert 10 < second["roi_percent"] < 15
    assert second["amount"] == amount
    assert second["buyout_total"] == str(buyout)
    assert second["exit_price_per_pack"] == "540"
    assert second["net_revenue_total"] == str(518 * amount // 10)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("min_sales", 4), ("min_sales", 201), ("min_sales", "5.5"),
        ("min_roi_bps", -10), ("min_roi_bps", 10010), ("min_roi_bps", 1501),
    ],
)
async def test_invalid_strategy_does_not_call_exbo(
    market: _Market, field: str, value: int | str
) -> None:
    """Отклонить недопустимый порог до обращения к аукциону.

    Args:
        market: Источник, который не должен получить запрос.
        field: Проверяемый параметр стратегии.
        value: Недопустимое значение параметра.
    """
    response = await _get(**{field: value})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", field]
    assert market.calls == []


async def test_unknown_id_and_invalid_days_do_not_call_exbo(
    market: _Market,
) -> None:
    """Проверить точный ID и допустимые периоды до запуска клиента.

    Args:
        market: Источник, который не должен получить запросы.
    """
    assert (await _get("TEST-ITEM")).status_code == 404
    assert (await _get(days=2)).status_code == 422
    assert market.calls == []


@pytest.mark.parametrize(
    ("item_id", "reason"),
    [("variant", "variant_unverified"), ("missing", "sell_quantity_missing")],
)
async def test_trading_requires_a_configured_standard_pack(
    market: _Market, item_id: str, reason: str
) -> None:
    """Ограничить расчёт без однородности или размера партии.

    Args:
        market: Источник с активными лотами.
        item_id: Предмет с ограничением.
        reason: Ожидаемая причина ограничения оценки.
    """
    response = await _get(item_id)
    assert response.status_code == 200
    payload = response.json()
    assert payload["history"]["sales"] == []
    assert len(payload["lots"]) == len(market.lots)
    assert reason in payload["reasons"]
    assert payload["q25_per_pack"] is payload["turnover_24h"] is None
    assert payload["q25_quality"] == "unavailable"
    assert all(lot["profit_total"] is None for lot in payload["lots"])


async def test_confirmed_empty_snapshot(market: _Market) -> None:
    """Признать нулевые продажи только для проверенного пустого окна.

    Args:
        market: Источник с нулевым числом строк в обоих ресурсах.
    """
    market.lots.clear()
    market.sales.clear()
    response = await _get()
    payload = response.json()
    assert response.status_code == 200
    assert payload["lots"] == payload["history"]["sales"] == []
    assert payload["coverage"]["lots_complete"]
    assert payload["coverage"]["history_24h_complete"]
    assert payload["turnover_24h"] == 0
    assert payload["q25_per_pack"] is None
    assert payload["reasons"] == ["insufficient_history"]


async def test_partial_lot_snapshot_uses_cached_history(
    market: _Market, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Оценить частичный снимок лотов по сохранённой истории.

    Args:
        market: Источник с большим числом строк.
        monkeypatch: Подмена лимитов для коротких сценариев пагинации.
    """
    monkeypatch.setattr(auction_snapshot, "_PAGE_LIMIT", 8)
    monkeypatch.setattr(auction_snapshot, "_MAX_LOT_PAGES", 2)
    market.lots = [dict(market.lots[0], amount=10 + i) for i in range(25)]
    now = datetime.now(UTC)
    market.sales = [
        {
            "amount": 10, "price": 500,
            "time": (now - timedelta(hours=1 + 8 * index)).isoformat(),
        }
        for index in range(80)
    ]
    response = await _get()
    payload = response.json()
    assert response.status_code == 200
    assert payload["coverage"]["lots_complete"] is False
    assert payload["coverage"]["lots_loaded"] == 14
    assert payload["coverage"]["history_prefix_consistent"]
    assert payload["coverage"]["history_24h_complete"]
    assert payload["coverage"]["history_window_complete"]
    assert payload["history"]["covered_from"] is not None
    assert payload["turnover_24h"] == 3
    by_amount = {lot["amount"]: lot for lot in payload["lots"]}
    assert by_amount[10]["active_competing_packs"] == 0
    assert by_amount[11]["active_competing_packs"] == 1
    assert payload["reasons"] == []
    assert by_amount[11]["status"] == "candidate"
    assert by_amount[11]["roi_percent"] > 0
    assert payload["coverage"]["history_loaded"] == 80
    assert len(payload["history"]["sales"]) == 42
    assert not any(call.url.path.endswith("/history") for call in market.calls)


@pytest.mark.parametrize("has_rules", [True, False])
async def test_preliminary_history_uses_observed_sales_for_lots(
    market: _Market, has_rules: bool
) -> None:
    """Оценить лоты по короткому подтверждённому участку истории.

    Args:
        market: Подставные сделки и активные лоты.
        has_rules: Финансовые правила доступны для расчёта.
    """
    price = 2**53 + 1
    for sale in market.sales:
        sale["price"] = price
    market.history_hours = 1
    if not has_rules:
        market.rules[AuctionMode.DEMO] = None

    response = await _get()
    assert response.status_code == 200
    data = response.json()
    assert data["q25_per_pack"] == str(price)
    assert data["q25_quality"] == "preliminary"
    assert data["sample_count"] == 20
    assert data["model"]["price_window_hours"] == 3
    assert data["coverage"]["lots_complete"]
    assert data["coverage"]["history_prefix_consistent"]
    assert not data["coverage"]["history_24h_complete"]
    assert not data["coverage"]["history_window_complete"]
    assert data["turnover_24h"] == 20
    assert data["price_change_percent"] is None
    lot = data["lots"][0]
    if has_rules:
        assert data["reasons"] == []
        assert lot["status"] == "candidate"
        assert lot["exit_price_per_pack"] == str(price)
        assert int(lot["profit_total"]) > 0 and lot["roi_percent"] > 0
    else:
        assert data["reasons"] == ["price_rules_unverified"]
        assert lot["status"] == "price_rules_unverified"
        for key in ("exit_price_per_pack", "profit_total", "roi_percent"):
            assert lot[key] is None


@pytest.mark.parametrize("drift", [False, True])
async def test_multipage_history_checks_overlap_and_keeps_duplicates(
    market: _Market, monkeypatch: pytest.MonkeyPatch, drift: bool,
) -> None:
    """Не потерять реальные одинаковые строки при склейке страниц.

    Args:
        market: Источник с повторяющимися завершёнными продажами.
        monkeypatch: Подмена размера страницы.
        drift: Имитировать сдвиг страницы истории.
    """
    monkeypatch.setattr(auction_snapshot, "_PAGE_LIMIT", 8)
    market.sales.insert(4, dict(market.sales[3]))
    market.drift = drift
    async with AuctionClient.demo(
        "fake-demo", transport=httpx.MockTransport(market.respond),
    ) as client:
        history = await auction_snapshot.collect_sale_history(
            client, "RU", _ITEM_ID,
            since=datetime.now(UTC) - timedelta(days=1),
        )
    if drift:
        assert not history.consistent and len(history.rows) == 8
        return
    assert history.complete
    assert sum(sale.amount == 10 for sale in history.rows) == 21
    history_calls = [
        call for call in market.calls if call.url.path.endswith("/history")
    ]
    assert [call.url.params["offset"] for call in history_calls] == [
        "0", "6", "12", "18", "0",
    ]


@pytest.mark.parametrize("cutoff_page", [None, 1, 7, 8])
async def test_concurrent_history_preserves_prefix(
    market: _Market, monkeypatch: pytest.MonkeyPatch, cutoff_page: int | None
) -> None:
    """Склеить страницы по порядку при разных моментах остановки.

    Args:
        market: Подставной снимок активных лотов и продаж.
        monkeypatch: Подмена размера страницы.
        cutoff_page: Номер страницы в пачке, пересекающей границу.
    """
    monkeypatch.setattr(auction_snapshot, "_PAGE_LIMIT", 8)
    now = datetime.now(UTC)
    market.sales = [
        dict(
            market.sales[2], price=500 + index,
            time=(now - timedelta(minutes=index)).isoformat(),
        )
        for index in range(68)
    ]
    market.lots = [
        dict(market.lots[0], amount=index + 1) for index in range(40)
    ]
    first_batch_ready = asyncio.Event()
    later_page_done = asyncio.Event()
    started_offsets: list[int] = []
    finished_offsets: list[int] = []
    cancelled_offsets: set[int] = set()
    in_flight = {"lots": 0, "history": 0}
    peak = dict(in_flight)
    stop_offset = cutoff_page * 6 if cutoff_page is not None else None
    since = (
        datetime.fromisoformat(market.sales[stop_offset + 1]["time"])
        if stop_offset is not None else now - timedelta(days=30)
    )

    async def respond(request: httpx.Request) -> httpx.Response:
        """Отпустить первую пачку после старта восьми запросов.

        Args:
            request: Запрос клиента аукциона.

        Returns:
            Страница из снимка.
        """
        resource = request.url.path.rsplit("/", 1)[-1]
        offset = int(request.url.params["offset"])
        in_flight[resource] += 1
        peak[resource] = max(peak[resource], in_flight[resource])
        try:
            if resource == "lots":
                if offset:
                    await asyncio.sleep(0)
                return market.respond(request)
            if offset:
                started_offsets.append(offset)
                if in_flight["history"] == 8:
                    first_batch_ready.set()
                await first_batch_ready.wait()
                if stop_offset is not None and offset > stop_offset:
                    await asyncio.Future()
                if offset == 6 and stop_offset != 6:
                    await later_page_done.wait()
            else:
                assert in_flight["history"] == 1
            response = market.respond(request)
            finished_offsets.append(offset)
            if offset == 12:
                later_page_done.set()
            return response
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cancelled_offsets.add(offset)
            raise
        finally:
            in_flight[resource] -= 1

    async with AuctionClient.demo(
        "fake-demo", transport=httpx.MockTransport(respond)
    ) as client:
        lots, history = await auction_snapshot.collect_auction_snapshot(
            client, "RU", _ITEM_ID, history_since=since,
            deadline=asyncio.get_running_loop().time() + 2,
        )

    assert isinstance(lots, PageScan)
    assert lots.consistent and len(lots.rows) == 32
    assert isinstance(history, PageScan)
    assert history.consistent
    assert history.complete is (stop_offset is None)
    expected = (
        market.sales[:8 + stop_offset]
        if stop_offset is not None else market.sales
    )
    assert [sale.price for sale in history.rows] == [
        sale["price"] for sale in expected
    ]
    assert peak == {"lots": 2, "history": 8}
    assert in_flight == {"lots": 0, "history": 0}
    assert started_offsets == list(
        range(6, 61 if stop_offset is None else 49, 6)
    )
    assert cancelled_offsets == {
        offset for offset in started_offsets
        if stop_offset is not None and offset > stop_offset
    }
    assert finished_offsets[0] == finished_offsets[-1] == 0
    if stop_offset != 6:
        assert finished_offsets.index(12) < finished_offsets.index(6)


async def test_history_rate_limit_cancels_pending_pages(
    market: _Market, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Дождаться отмены пачки и вернуть ограничение с Retry-After.

    Args:
        market: Подставной источник лотов и истории.
        monkeypatch: Подмена размера страницы.
    """
    monkeypatch.setattr(auction_snapshot, "_PAGE_LIMIT", 8)
    market.sales = sorted(
        market.sales * 3, key=lambda sale: sale["time"], reverse=True
    )
    market.failure = 429
    market.failure_resource = "history"
    market.failure_offset = 6
    batch_ready = asyncio.Event()
    started_offsets: list[int] = []
    cancelled_offsets: set[int] = set()

    async def respond(request: httpx.Request) -> httpx.Response:
        """Вернуть 429 при восьми начатых запросах истории.

        Args:
            request: Запрос клиента аукциона.

        Returns:
            Страница или ответ EXBO об ограничении частоты запросов.
        """
        offset = int(request.url.params["offset"])
        if not request.url.path.endswith("/history") or offset == 0:
            return market.respond(request)
        started_offsets.append(offset)
        if len(started_offsets) == 8:
            batch_ready.set()
        try:
            await batch_ready.wait()
            if offset != 6:
                await asyncio.Future()
            return market.respond(request)
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            cancelled_offsets.add(offset)
            raise

    async with AuctionClient.demo(
        "fake-demo", transport=httpx.MockTransport(respond)
    ) as client:
        lots, history = await auction_snapshot.collect_auction_snapshot(
            client, "RU", _ITEM_ID,
            history_since=datetime.now(UTC) - timedelta(days=30),
            deadline=asyncio.get_running_loop().time() + 2,
        )

    assert isinstance(lots, PageScan) and lots.complete
    assert isinstance(history, RateLimitError)
    assert history.retry_after_seconds == 3
    assert started_offsets == list(range(6, 49, 6))
    assert cancelled_offsets == set(started_offsets[1:])
    assert [
        request.url.params["offset"] for request in market.calls
        if request.url.path.endswith("/history")
    ] == ["0", "6"]


async def test_history_deadline_keeps_rechecked_prefix(
    market: _Market, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Отменить зависшую страницу и сверить прочитанную историю.

    Args:
        market: Источник со стабильным началом истории.
        monkeypatch: Подмена размера страницы.
    """
    monkeypatch.setattr(auction_snapshot, "_PAGE_LIMIT", 8)
    cancelled = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        """Задержать третью страницу до отмены.

        Args:
            request: Запрос клиента аукциона.

        Returns:
            Готовая страница при доступном смещении.
        """
        if (
            request.url.path.endswith("/history")
            and request.url.params["offset"] == "12"
        ):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        return market.respond(request)

    async with AuctionClient.demo(
        "fake-demo", transport=httpx.MockTransport(respond)
    ) as client:
        _, history = await auction_snapshot.collect_auction_snapshot(
            client, "RU", _ITEM_ID,
            history_since=datetime.now(UTC) - timedelta(days=30),
            deadline=asyncio.get_running_loop().time() + 2,
        )

    assert cancelled.is_set()
    assert isinstance(history, PageScan)
    assert history.consistent and not history.complete
    assert len(history.rows) == 14
    history_offsets = [
        call.url.params["offset"] for call in market.calls
        if call.url.path.endswith("/history")
    ]
    assert history_offsets == ["0", "6", "18", "0"]


async def test_stale_history_keeps_prices_when_exbo_history_is_down(
    market: _Market,
) -> None:
    """Сохранить вычисления и показать время устаревшего снимка.

    Args:
        market: Подставные лоты и ранее сохранённые сделки.
    """
    market.history_stale = True
    market.failure = 429
    market.failure_resource = "history"
    for sale in market.sales:
        at = datetime.fromisoformat(sale["time"]) - timedelta(minutes=15)
        sale["time"] = at.isoformat()
    response = await _get()
    data = response.json()
    assert response.status_code == 200
    assert data["history"]["sync"]["stale"]
    assert data["history"]["sync"]["last_success_at"]
    assert data["q25_per_pack"] == "540"
    assert data["turnover_24h"] == 20
    assert data["source_errors"] == {}
    assert not any(call.url.path.endswith("/history") for call in market.calls)


async def test_empty_snapshot_enqueues_one_scan(market: _Market) -> None:
    """Повторно читать HTTP 202, пока ядро не опубликовало снимок.

    Args:
        market: Подставное хранилище до первой публикации.
    """
    market.snapshot_missing = True
    assert (await _get()).status_code == 202
    assert (await _get(days=7)).status_code == 202
    assert len(market.pending) == 1
    assert market.pending[0][1].operation == "scan"
    assert not market.calls


async def test_missing_profile_waits_for_requested_result(
    market: _Market,
) -> None:
    """Запросить пересчёт без подстановки стандартной стратегии.

    Args:
        market: Подставное хранилище без пользовательского профиля.
    """
    market.profile_pending = True
    first = await _get(min_sales=23, min_roi_bps=1500)
    assert first.status_code == 202
    second = await _get(days=7, min_sales=23, min_roi_bps=1500)
    assert second.status_code == 202
    assert len(market.pending) == 1
    command = market.pending[0][1]
    assert command.operation == "evaluate"
    assert command.strategy.min_sales == 23
    assert command.strategy.min_roi_bps == 1500
    assert not market.calls


async def test_clickhouse_failure_is_reported_without_exbo(
    market: _Market,
) -> None:
    """Вернуть диагностируемую ошибку при недоступной истории.

    Args:
        market: Подставное хранилище с ошибкой чтения.
    """
    market.history_unavailable = True
    response = await _get()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "storage_unavailable"
    assert not market.calls


async def test_version_poll_queues_one_refresh_for_stale_snapshot(
    market: _Market,
) -> None:
    """Запросить один проход для открытого предмета без чтения сделок.

    Args:
        market: Хранилище старого снимка в режиме без мониторинга.
    """
    market.snapshot_age = 360
    first = await _get(endpoint="/api/auction/version")
    second = await _get(endpoint="/api/auction/version")
    assert first.status_code == second.status_code == 200
    assert first.json()["version"] == "revision-1"
    assert second.json()["snapshot_stale"]
    assert len(market.pending) == 1
    assert market.pending[0][1].operation == "scan"
    assert market.pending[0][1].expected_snapshot_id == "fixture"
    assert not market.history_reads and not market.calls
    full = await _get()
    assert full.status_code == 200 and full.json()["snapshot_stale"]
    assert full.json()["version"] == "revision-1"
    assert len(market.pending) == 1
