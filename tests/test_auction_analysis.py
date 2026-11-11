"""Контрольные сценарии расчёта перепродажи без сети."""

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from auction_engine.backend.analytics import analyze_auction
from auction_engine.backend.analytics.types import (
    AnalysisCoverage,
    AuctionAnalysis,
)
from common.dataclasses import CatalogItem
from common.enums.auction import AuctionMode
from common.enums.auction_analysis import (
    AnalysisStatus,
    CommissionRounding,
    PriceBasis,
    Q25Quality,
)
from common.errors import AuctionConfigurationError, CatalogError
from common.models.analysis_rules import AnalysisRules
from common.models.auction import Lot, Sale
from common.models.auction_strategy import StrategySettings
from common.settings import ItemCatalogSettings
from common.settings import analysis as analysis_settings

T = datetime(2026, 11, 7, 12, tzinfo=UTC)
PILOT = CatalogItem(
    "l0og1", "Стандартные запчасти", sell_quantity=10, homogeneous=True
)
COMPLETE = AnalysisCoverage(True, True, True, history_complete=True)
RULES = AnalysisRules(
    buyout_price_basis=PriceBasis.LOT_TOTAL,
    history_price_basis=PriceBasis.LOT_TOTAL,
    sale_commission_bps=500,
    listing_cost_per_pack=0,
)


def _lot(
    amount: int,
    buyout: int | None,
    *,
    end_time: datetime = T + timedelta(hours=1),
    item_id: str = PILOT.item_id,
) -> Lot:
    """Создать лот с тестовыми количеством, выкупом и сроком.

    Args:
        amount: Количество предметов.
        buyout: Исходная цена выкупа или None.
        end_time: Время окончания лота.
        item_id: Точный ID предмета.

    Returns:
        Валидированная строка активных лотов.
    """
    return Lot.model_validate(
        {
            "itemId": item_id,
            "amount": amount,
            "startPrice": 1,
            "buyoutPrice": buyout,
            "startTime": (T - timedelta(hours=1)).isoformat(),
            "endTime": end_time.isoformat(),
        }
    )


def _sale(
    price: int,
    *,
    time: datetime = T - timedelta(hours=1),
    amount: int = 10,
) -> Sale:
    """Создать запись истории с известными ценой и временем.

    Args:
        price: Исходная цена продажи.
        time: Время продажи.
        amount: Количество предметов в проданной пачке.

    Returns:
        Валидированная строка истории.
    """
    return Sale.model_validate(
        {"amount": amount, "price": price, "time": time.isoformat()}
    )


def _history(price: int = 100_000, *, turnover: int = 5) -> list[Sale]:
    """Подготовить двадцать продаж с заданным суточным оборотом.

    Args:
        price: Исходная цена каждой строки.
        turnover: Число строк внутри окна 24 часа.

    Returns:
        История с одинаковыми строками в каждой группе времени.
    """
    return [
        _sale(price, time=T - timedelta(hours=1 if i < turnover else 25))
        for i in range(20)
    ]


def _analyze(
    lots: Sequence[Lot],
    history: Sequence[Sale],
    *,
    item: CatalogItem = PILOT,
    rules: AnalysisRules | None = RULES,
    coverage: AnalysisCoverage = COMPLETE,
    strategy: StrategySettings | None = None,
) -> AuctionAnalysis:
    """Запустить ядро с фиксированным временем пилота.

    Args:
        lots: Строки снимка лотов.
        history: Строки истории продаж.
        item: Настройки выбранного предмета.
        rules: Правила расчёта либо None до сверки.
        coverage: Признаки покрытия снимка.
        strategy: Порог выборки и ROI либо значения по умолчанию.

    Returns:
        Диагностика предмета и сценарии покупки.
    """
    return analyze_auction(
        item, lots, history, evaluated_at=T, coverage=coverage, rules=rules,
        strategy=strategy,
    )


@pytest.mark.parametrize("amount", [1, 6, 10, 20, 27, 100])
def test_control_example_uses_a_share_of_standard_packs(amount: int) -> None:
    """Проверить прибыль пропорционально всему объёму покупки.

    Args:
        amount: Количество предметов в покупке и перепродаже.
    """
    buyout = 10_000 * amount
    analysis = _analyze(
        [_lot(amount, buyout)], _history(130_000, turnover=20),
    )
    (result,) = analysis.lots
    assert analysis.q25_per_pack == 130_000
    assert analysis.turnover_24h == 20
    assert analysis.sample_count == 20
    assert result.plan.buyout_total == buyout
    assert result.competing_packs == 0
    assert result.exit_price_per_pack == 130_000
    assert result.net_revenue_per_pack == 123_500
    assert result.gross_revenue == 13_000 * amount
    assert result.commission == 650 * amount
    assert result.listing_cost == 0
    assert result.net_revenue == 12_350 * amount
    assert result.profit == 2_350 * amount
    assert result.roi == Fraction(47, 200)
    assert result.max_buyout == 123_500 * amount // 11
    assert result.status is AnalysisStatus.CANDIDATE


def test_q25_uses_only_standard_pack_sales() -> None:
    """Считать только продажи стандартных партий и сохранить повторы."""
    history = [
        *[_sale(100 + i, time=T - timedelta(minutes=i)) for i in range(20)],
        *[_sale(1, time=T - timedelta(hours=2))] * 5,
        *[_sale(1, time=T, amount=1)] * 12,
        _sale(1, time=T, amount=20),
        _sale(2, time=T - timedelta(hours=24)),
        _sale(1, time=T + timedelta(seconds=1)),
        _sale(1, time=T - timedelta(days=15)),
        _sale(1, time=T, amount=0),
        _sale(0, time=T),
    ]

    analysis = _analyze([], history)

    assert analysis.price_window == timedelta(hours=3)
    assert analysis.q25_per_pack == 101
    assert analysis.q25_quality is Q25Quality.VERIFIED
    assert analysis.sample_count == 25
    assert analysis.sample_newest_at == T
    assert analysis.sample_oldest_at == T - timedelta(hours=2)
    assert analysis.turnover_24h == 26


@pytest.mark.parametrize(
    ("counts", "min_sales", "hours", "sample_count"),
    [
        ((25, 0, 0, 0), 20, 3, 25),
        ((10, 40, 0, 0), 20, 24, 50),
        ((5, 3, 32, 0), 20, 168, 40),
        ((2, 1, 2, 25), 20, 336, 30),
        ((25, 25, 0, 0), 30, 24, 50),
    ],
)
def test_shortest_covered_price_window(
    counts: tuple[int, int, int, int],
    min_sales: int,
    hours: int,
    sample_count: int,
) -> None:
    """Найти минимальное покрытое окно по порогу стратегии.

    Args:
        counts: Продажи в четырёх непересекающихся возрастах.
        min_sales: Минимум продаж для выбора окна.
        hours: Ожидаемая длительность выбранного окна.
        sample_count: Число цен в выбранной выборке.
    """
    history = [
        _sale(2**53 + 1, time=T - timedelta(hours=age))
        for age, count in zip((1, 12, 48, 192), counts, strict=True)
        for _ in range(count)
    ]
    analysis = _analyze(
        [], history, rules=None, strategy=StrategySettings(min_sales=min_sales)
    )
    assert analysis.price_window == timedelta(hours=hours)
    assert analysis.sample_count == sample_count
    assert analysis.q25_per_pack == 2**53 + 1


def test_price_window_includes_its_start() -> None:
    """Учесть продажу ровно за 14 дней до оценки."""
    oldest = T - timedelta(days=14)
    history = [
        *[_sale(100)] * 19,
        _sale(1, time=oldest),
        _sale(1, time=oldest - timedelta(seconds=1)),
    ]

    analysis = _analyze([], history)

    assert analysis.sample_count == 20
    assert analysis.sample_oldest_at == oldest
    assert analysis.q25_per_pack == 100


def test_small_lot_is_evaluated_and_expired_offers_are_skipped() -> None:
    """Оценить семь предметов и пропустить недоступные лоты."""
    lots = [
        _lot(7, 20_000),
        _lot(10, 10_000, end_time=T),
        _lot(10, None),
        _lot(10, 0),
        _lot(10, 10_000, item_id="other"),
    ]
    (result,) = _analyze(lots, _history()).lots

    assert result.status is AnalysisStatus.CANDIDATE
    assert result.plan.buyout_total == 20_000
    assert result.plan.amount == 7
    assert result.exit_price_per_pack == 100_000
    assert result.gross_revenue == 70_000


def test_excludes_only_the_selected_lot_row() -> None:
    """Сохранить конкурента, совпадающего с покупкой по всем полям."""
    duplicate = _lot(10, 90_000)
    analysis = _analyze([duplicate, duplicate], _history(turnover=2))

    assert len(analysis.lots) == 2
    for result in analysis.lots:
        assert result.exit_price_per_pack == 100_000
        assert result.competing_packs == 1


@pytest.mark.parametrize(
    ("quantity", "amount", "exit_price"),
    [(10, 6, 130_000), (10, 20, 127_999), (10, 27, None), (5, 6, 127_999)],
)
def test_competition_uses_standard_packs_and_rounded_up_load(
    quantity: int, amount: int, exit_price: int | None
) -> None:
    """Учесть конкурентов того же размера и нагрузку целых партий.

    Args:
        quantity: Размер стандартной партии из каталога.
        amount: Число предметов в оцениваемой покупке.
        exit_price: Ожидаемая цена продажи партии или None.
    """
    lots = [
        _lot(amount, 1_000 * amount),
        _lot(quantity, 128_000),
        _lot(1, 1),
        _lot(quantity + 1, 1),
    ]
    history = [
        _sale(
            130_000, amount=quantity,
            time=T - timedelta(hours=1 if i < 2 else 25),
        )
        for i in range(20)
    ]
    analysis = _analyze(
        lots, history, item=replace(PILOT, sell_quantity=quantity),
    )
    result = next(lot for lot in analysis.lots if lot.plan.source_index == 0)
    assert analysis.turnover_24h == 2
    assert analysis.active_competing_packs == 1
    assert result.exit_price_per_pack == exit_price
    if exit_price is None:
        assert result.status is AnalysisStatus.LOW_LIQUIDITY


@pytest.mark.parametrize(
    ("lots", "turnover"),
    [
        ([_lot(25, 160_000)], 1),
        ([_lot(10, 1), _lot(10, 1)], 1),
    ],
)
def test_low_liquidity(lots: list[Lot], turnover: int) -> None:
    """Отклонить покупку без допустимой положительной цены.

    Args:
        lots: Лоты с избыточным объёмом или конкурентом по рублю.
        turnover: Число сделок из тестового суточного окна.
    """
    analysis = _analyze(lots, _history(turnover=turnover))

    for result in analysis.lots:
        assert result.status is AnalysisStatus.LOW_LIQUIDITY
        assert result.exit_price_per_pack is None
        assert result.max_buyout is None


def test_normalizes_buyout_but_uses_full_history_prices() -> None:
    """Учесть единицу выкупа и полные цены исторических продаж."""
    buyout_rules = RULES.model_copy(
        update={"buyout_price_basis": PriceBasis.PER_UNIT}
    )
    history_rules = RULES.model_copy(
        update={"history_price_basis": PriceBasis.PER_UNIT}
    )
    buyout = _analyze(
        [_lot(25, 6_400)], _history(), rules=buyout_rules
    )
    history = _analyze(
        [_lot(25, 160_000)], _history(), rules=history_rules
    )

    assert buyout.q25_per_pack == history.q25_per_pack == 100_000
    assert buyout.lots == history.lots
    assert buyout.lots[0].plan.buyout_total == 160_000


@pytest.mark.parametrize(
    ("rounding", "net_pack", "commission", "listing", "net_revenue"),
    [
        (None, 93, 16, 5, 251),
        (CommissionRounding.CEIL, 93, 16, 5, 251),
        (CommissionRounding.FLOOR, 94, 13, 6, 253),
    ],
)
def test_rounded_pack_costs_balance_the_proportional_profit(
    rounding: CommissionRounding | None, net_pack: int, commission: int,
    listing: int, net_revenue: int,
) -> None:
    """Округлить прибыль вниз и согласовать финансовые компоненты.

    Args:
        rounding: Подтверждённое округление или None.
        net_pack: Чистая выручка одной стандартной партии.
        commission: Целая доля комиссии на купленное количество.
        listing: Целая доля расходов на выставление.
        net_revenue: Ожидаемая чистая выручка.
    """
    rules = RULES.model_copy(
        update={"commission_rounding": rounding, "listing_cost_per_pack": 2}
    )
    analysis = _analyze([_lot(27, 252)], _history(101), rules=rules)

    (result,) = analysis.lots
    assert result.exit_price_per_pack == 101
    assert result.net_revenue_per_pack == net_pack
    assert result.gross_revenue == 272
    assert result.commission == commission
    assert result.listing_cost == listing
    assert result.net_revenue == net_revenue
    assert result.profit == 272 - commission - listing - 252
    assert result.roi == Fraction(net_revenue - 252, 252)
    assert analysis.commission_estimated is (rounding is None)


@pytest.mark.parametrize(
    ("revenue", "buyout", "threshold", "status", "max_buyout"),
    [
        (11_000, 10_000, 1000, AnalysisStatus.CANDIDATE, 10_000),
        (11_001, 10_001, 1000, AnalysisStatus.BELOW_ROI, 10_000),
        (100, 100, 0, AnalysisStatus.BELOW_ROI, 99),
        (100, 99, 0, AnalysisStatus.CANDIDATE, 99),
    ],
)
def test_exact_roi_boundary(
    revenue: int,
    buyout: int,
    threshold: int,
    status: AnalysisStatus,
    max_buyout: int,
) -> None:
    """Проверить точный порог ROI и строго положительную прибыль.

    Args:
        revenue: Выручка за лот без комиссии.
        buyout: Стоимость покупки.
        threshold: Минимальный ROI в базисных пунктах.
        status: Ожидаемое решение.
        max_buyout: Ожидаемая предельная цена покупки.
    """
    rules = RULES.model_copy(update={"sale_commission_bps": 0})
    analysis = _analyze(
        [_lot(10, buyout)], _history(revenue), rules=rules,
        strategy=StrategySettings(min_roi_bps=threshold),
    )
    (result,) = analysis.lots

    assert result.status is status
    assert result.roi == Fraction(revenue - buyout, buyout)
    assert result.max_buyout == max_buyout


@pytest.mark.parametrize(
    ("coverage", "quality"),
    [
        (AnalysisCoverage(False, True, True), Q25Quality.VERIFIED),
        (AnalysisCoverage(True, False, True), Q25Quality.VERIFIED),
        (AnalysisCoverage(True, False, False), Q25Quality.PRELIMINARY),
        (AnalysisCoverage(False, False, False), Q25Quality.PRELIMINARY),
    ],
)
def test_incomplete_coverage_does_not_block_observed_lots(
    coverage: AnalysisCoverage, quality: Q25Quality
) -> None:
    """Оценить лот и конкуренцию по доступным снимку и истории.

    Args:
        coverage: Состояние снимка, суток и ценовой выборки.
        quality: Ожидаемое качество ценовой выборки.
    """
    history = [
        *[_sale(100_000)] * 20,
        _sale(1, time=T - timedelta(hours=4), amount=1),
    ]
    analysis = _analyze(
        [_lot(25, 160_000), _lot(10, 90_000)], history, coverage=coverage,
    )
    result = analysis.lots[0]

    assert analysis.q25_per_pack == 100_000
    assert analysis.q25_quality is quality
    assert analysis.coverage == coverage
    assert analysis.turnover_24h == 20
    assert analysis.active_competing_packs == result.competing_packs == 1
    assert analysis.reasons == ()
    assert result.status is AnalysisStatus.CANDIDATE
    assert result.plan.amount == 25
    assert result.exit_price_per_pack == 100_000
    assert result.profit == 77_500
    assert result.roi == Fraction(31, 64)


def test_candidate_with_confirmed_short_window_and_daily_turnover() -> None:
    """Использовать проверенное короткое окно при неполных 14 днях."""
    analysis = _analyze(
        [_lot(27, 160_000)], [_sale(100_000)] * 20,
        coverage=AnalysisCoverage(True, True, True),
    )
    assert analysis.price_window == timedelta(hours=3)
    assert analysis.price_change is None
    assert analysis.lots[0].status is AnalysisStatus.CANDIDATE


@pytest.mark.parametrize(
    ("coverage", "ages", "hours", "turnover"),
    [
        (AnalysisCoverage(True, False, True), [1] * 25, 3, 25),
        (AnalysisCoverage(True, False, False), [1] * 8 + [6] * 34, 24, 42),
        (AnalysisCoverage(True, True, True), [1] * 8 + [48] * 34, 168, 8),
    ],
)
def test_preliminary_q25_uses_shortest_observed_window(
    coverage: AnalysisCoverage, ages: list[int], hours: int,
    turnover: int,
) -> None:
    """Рассчитать предварительный Q25 по доступным сопоставимым продажам.

    Args:
        coverage: Подтверждённое покрытие снимка и истории.
        ages: Возраст сопоставимых продаж в часах.
        hours: Длительность окна предварительной оценки.
        turnover: Число завершённых продаж партий за сутки.
    """
    price = 2**53 + 1
    history = [
        *[_sale(price, time=T - timedelta(hours=age)) for age in ages],
        _sale(1, amount=0),
        _sale(0),
        _sale(1, time=T + timedelta(seconds=1)),
    ]
    analysis = _analyze(
        [_lot(10, 10_000)], history, coverage=coverage,
    )
    (lot,) = analysis.lots
    assert analysis.price_window == timedelta(hours=hours)
    assert analysis.q25_per_pack == price
    assert analysis.q25_quality is Q25Quality.PRELIMINARY
    assert analysis.sample_count == len(ages)
    assert analysis.price_change is None
    assert analysis.turnover_24h == turnover
    assert analysis.reasons == ()
    assert lot.status is AnalysisStatus.CANDIDATE
    assert lot.exit_price_per_pack == price
    assert lot.profit is not None and lot.profit > 0


def test_observed_turnover_does_not_include_older_sales() -> None:
    """Сохранить низкую ликвидность при отсутствии суточных наблюдений."""
    analysis = _analyze(
        [_lot(10, 10_000)],
        [_sale(100_000, time=T - timedelta(hours=25))] * 25,
        coverage=AnalysisCoverage(True, False, False),
    )
    (lot,) = analysis.lots
    assert analysis.q25_quality is Q25Quality.PRELIMINARY
    assert analysis.turnover_24h == 0
    assert lot.status is AnalysisStatus.LOW_LIQUIDITY
    assert lot.exit_price_per_pack is lot.profit is None


@pytest.mark.parametrize(
    ("previous_price", "previous_count", "status"),
    [
        (100_000, 20, AnalysisStatus.SHARP_PRICE_DROP),
        (99_999, 20, AnalysisStatus.CANDIDATE),
        (100_000, 19, AnalysisStatus.CANDIDATE),
    ],
)
def test_price_drop_gate_preserves_calculated_finances(
    previous_price: int, previous_count: int, status: AnalysisStatus
) -> None:
    """Сравнить непересекающиеся окна и сохранить оценку прибыли.

    Args:
        previous_price: Цена всех продаж предыдущего окна.
        previous_count: Число продаж предыдущего окна.
        status: Ожидаемый статус после проверки риска.
    """
    history = [
        *[_sale(80_000)] * 20,
        _sale(80_000, time=T - timedelta(hours=3)),
        *[
            _sale(previous_price, time=T - timedelta(hours=4))
        ] * previous_count,
    ]
    analysis = _analyze([_lot(10, 65_000)], history)
    (result,) = analysis.lots
    assert analysis.sample_count == 21
    assert analysis.price_change == (
        Fraction(80_000 - previous_price, previous_price)
        if previous_count == 20 else None
    )
    assert result.status is status
    assert result.profit == 11_000


def test_unverified_variant_has_no_comparable_history() -> None:
    """Сохранить объём покупки без ценовой оценки варианта."""
    item = replace(PILOT, homogeneous=False)
    analysis = _analyze([_lot(25, 160_000)], _history(), item=item)
    (result,) = analysis.lots

    assert result.status is AnalysisStatus.VARIANT_UNVERIFIED
    assert result.plan.amount == 25
    assert analysis.sample_count is None
    assert analysis.q25_per_pack is None
    assert analysis.turnover_24h is None


@pytest.mark.parametrize(
    ("coverage", "sample_count"),
    [(COMPLETE, 19), (AnalysisCoverage(True, False, False), None)],
)
def test_insufficient_history_remains_blocking(
    coverage: AnalysisCoverage, sample_count: int | None
) -> None:
    """Ограничить оценку при недостатке продаж для Q25.

    Args:
        coverage: Покрытие снимка и истории.
        sample_count: Число продаж в покрытом окне или None.
    """
    analysis = _analyze(
        [_lot(10, 20_000)], [_sale(100_000)] * 19, coverage=coverage,
    )
    (result,) = analysis.lots

    assert analysis.sample_count == sample_count
    assert analysis.turnover_24h == 19
    assert result.status is AnalysisStatus.INSUFFICIENT_HISTORY
    assert analysis.q25_per_pack is None
    assert analysis.q25_quality is Q25Quality.UNAVAILABLE
    assert result.max_buyout is None


def test_reports_all_restrictions_without_unverified_prices() -> None:
    """Перечислить причины и отложить расчёт до сверки правил."""
    analysis = _analyze(
        [_lot(25, 160_000)],
        [_sale(100_000)],
        coverage=AnalysisCoverage(False, True, True, history_complete=True),
        rules=None,
    )
    (result,) = analysis.lots

    assert result.status is AnalysisStatus.PRICE_RULES_UNVERIFIED
    assert result.reasons == analysis.reasons == (
        AnalysisStatus.PRICE_RULES_UNVERIFIED,
        AnalysisStatus.INSUFFICIENT_HISTORY,
    )
    assert result.plan.buyout_total is None
    assert result.plan.amount == 25
    assert analysis.q25_per_pack is None
    assert result.profit is None


def test_candidates_are_ranked_by_profit() -> None:
    """Сортировать кандидатов отдельно от диагностических лотов."""
    analysis = _analyze(
        [_lot(25, 170_000), _lot(7, 1_000), _lot(25, 150_000)],
        _history(turnover=20),
    )

    assert [result.plan.source_index for result in analysis.lots] == [2, 0, 1]
    profits = [result.profit for result in analysis.lots]
    assert profits == [87_500, 67_500, 65_500]


def test_resale_requires_a_standard_pack_size_from_catalog() -> None:
    """Показать причину отсутствия оценки без размера партии."""
    analysis = _analyze(
        [_lot(6, 60_000)], _history(130_000),
        item=replace(PILOT, sell_quantity=None),
    )
    (lot,) = analysis.lots
    assert lot.status is AnalysisStatus.SELL_QUANTITY_MISSING
    assert lot.exit_price_per_pack is lot.profit is None


@pytest.mark.parametrize("quantity", [0, -1, 1.5, True])
def test_json_config_rejects_invalid_pack_size(
    tmp_path: Path, quantity: int | float
) -> None:
    """Проверить строгую валидацию размера пачки из JSON.

    Args:
        tmp_path: Временная папка для файла настроек.
        quantity: Недопустимое значение sell_quantity.
    """
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "item_id": "l0og1",
                        "name_ru": "Запчасти",
                        "sell_quantity": quantity,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CatalogError, match="sell_quantity"):
        ItemCatalogSettings.load(path)


def test_production_rules_default_and_validated_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Загрузить правила Production без файла и проверить переопределение.

    Args:
        monkeypatch: Изоляция окружения и пути к локальному файлу.
        tmp_path: Каталог без файла с финансовыми правилами.
    """
    variable = "STALZONE_PRODUCTION_ANALYSIS_RULES"
    monkeypatch.delenv(variable, raising=False)
    monkeypatch.setitem(
        analysis_settings._ProductionRulesSettings.model_config,
        "env_file", tmp_path / ".env",
    )
    rules = analysis_settings.load_analysis_rules(AuctionMode.PRODUCTION)
    assert rules is not None
    assert rules.buyout_price_basis is PriceBasis.LOT_TOTAL
    assert rules.history_price_basis is PriceBasis.LOT_TOTAL
    assert rules.sale_commission_bps == 500
    assert rules.listing_cost_per_pack == 0
    assert rules.commission_rounding is CommissionRounding.CEIL

    override = rules.model_copy(update={"sale_commission_bps": 333})
    monkeypatch.setenv(variable, override.model_dump_json())
    assert analysis_settings.load_analysis_rules(AuctionMode.PRODUCTION) == (
        override
    )

    invalid = rules.model_dump(mode="json")
    invalid["sale_commission_bps"] = 10_001
    monkeypatch.setenv(variable, json.dumps(invalid))
    with pytest.raises(AuctionConfigurationError):
        analysis_settings.load_analysis_rules(AuctionMode.PRODUCTION)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sale_commission_bps", -1),
        ("sale_commission_bps", 10_001),
        ("sale_commission_bps", True),
        ("listing_cost_per_pack", -1),
        ("listing_cost_per_pack", 1.5),
        ("min_roi_bps", -1),
        ("min_roi_bps", True),
    ],
)
def test_price_rules_require_valid_integers(
    field: str, value: int | float
) -> None:
    """Проверить ограничения на комиссию, расходы и порог ROI.

    Args:
        field: Изменяемое поле правил.
        value: Недопустимое числовое значение.
    """
    payload = RULES.model_dump()
    payload[field] = value

    with pytest.raises(ValidationError, match=field):
        AnalysisRules.model_validate(payload)
