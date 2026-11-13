import assert from "node:assert/strict";
import test from "node:test";

import {
    DEFAULT_FILTERS,
    DEFAULT_HISTORY_PERIOD,
    DEFAULT_STRATEGY,
    STRATEGY_STORAGE_KEY,
    canPollVersion,
    checkAnalysisVersion,
    createAnalysisLoader,
    filterLots,
    historySeries,
    isEstimatedAnalysis,
    loadStrategy,
    versionPollState,
} from "../stalzone_analytics/frontend/static/js/auction-data.js";
import { formatMoney, renderLotRows, renderMetrics } from "../stalzone_analytics/frontend/static/js/ui.js";

function data(itemId) {
    return { item_id: itemId, mode: "production", lots: [], history: { sales: [] } };
}

function jsonResponse(payload, status = 200) {
    return new Response(JSON.stringify(payload), { status });
}

test("загрузка передаёт точный ID, окно графика и стратегию", async () => {
    const updates = [];
    const queries = [];
    const load = createAnalysisLoader((state) => updates.push(state), async (url) => {
        const params = new URL(url, "http://test").searchParams;
        queries.push(Object.fromEntries(params));
        return jsonResponse(data(params.get("item_id")));
    });
    await load("AbC9");
    await load("another", 30, { min_sales: 35, min_roi_bps: 1510 });
    assert.equal(DEFAULT_HISTORY_PERIOD, 1);
    assert.deepEqual(queries, [
        {
            item_id: "AbC9", region: "RU", mode: "production", days: "1",
            min_sales: "20", min_roi_bps: "1000",
        },
        {
            item_id: "another", region: "RU", mode: "production", days: "30",
            min_sales: "35", min_roi_bps: "1510",
        },
    ]);
    assert.deepEqual(updates.map(({ status }) => status), [
        "loading", "ready", "loading", "ready",
    ]);
});

test("восстанавливаются валидные настройки сохранённой версии", () => {
    const settings = { min_sales: 35, min_roi_bps: 1510 };
    const storage = new Map([[STRATEGY_STORAGE_KEY, JSON.stringify(settings)]]);
    const adapter = { getItem: (key) => storage.get(key) ?? null };
    assert.deepEqual(loadStrategy(adapter), settings);
    storage.set(STRATEGY_STORAGE_KEY, '{"min_sales":35,"min_roi_bps":1501}');
    assert.deepEqual(loadStrategy(adapter), DEFAULT_STRATEGY);
    storage.set(STRATEGY_STORAGE_KEY, "{");
    assert.deepEqual(loadStrategy(adapter), DEFAULT_STRATEGY);
});

test("запоздавший ответ не возвращает старые результаты", async () => {
    const updates = [];
    let completeFirst;
    let firstSignal;
    const load = createAnalysisLoader((state) => updates.push(state), (url, options) => {
        const params = new URL(url, "http://test").searchParams;
        if (params.get("item_id") === "first") {
            firstSignal = options.signal;
            return new Promise((resolve) => { completeFirst = resolve; });
        }
        return Promise.resolve(jsonResponse(data("second")));
    });
    const first = load("first");
    await load("second");
    assert.equal(firstSignal.aborted, true);
    completeFirst(jsonResponse(data("first")));
    await first;
    assert.deepEqual(updates.filter(({ status }) => status === "ready"), [
        { status: "ready", data: data("second") },
    ]);
});

test("ограничение квоты и сетевая ошибка допускают повтор", async () => {
    const states = [];
    let attempt = 0;
    const load = createAnalysisLoader((state) => states.push(state), async () => {
        attempt += 1;
        if (attempt === 1) {
            return jsonResponse({
                detail: { message: "Квота EXBO", retryable: true, retry_after_seconds: 3 },
            }, 429);
        }
        if (attempt === 2) {
            throw new Error("local network failure");
        }
        return jsonResponse(data("item"));
    });
    await load("item");
    assert.deepEqual(states.at(-1), {
        status: "error", message: "Квота EXBO", retryable: true, retryAfter: 3,
    });
    await load("item");
    assert.equal(states.at(-1).status, "error");
    assert.equal(states.at(-1).retryable, true);
    await load("item");
    assert.equal(states.at(-1).status, "ready");
});

test("сортировка и фильтры различают неизвестные значения и точные суммы", () => {
    const lots = [
        { id: 1, buyout_total: "9007199254740993", roi_percent: 22, status: "candidate" },
        { id: 2, buyout_total: null, roi_percent: null, status: null },
        { id: 3, buyout_total: "9007199254740992", roi_percent: 12, status: "candidate" },
    ];
    const asc = { key: "buyout_total", direction: "ascending" };
    const desc = { ...asc, direction: "descending" };
    assert.deepEqual(filterLots(lots, DEFAULT_FILTERS, asc).map((lot) => lot.id), [3, 1, 2]);
    assert.deepEqual(filterLots(lots, DEFAULT_FILTERS, desc).map((lot) => lot.id), [1, 3, 2]);
    assert.deepEqual(filterLots(lots, {
        minRoi: 10, maxPurchasePrice: "9007199254740992", status: "candidate",
    }, asc).map((lot) => lot.id), [3]);
    assert.deepEqual(filterLots(lots, {
        ...DEFAULT_FILTERS, status: "unassessed",
    }, asc).map((lot) => lot.id), [2]);
});

test("метрики показывают окно оценки и считают только оценённых кандидатов", () => {
    const panel = { innerHTML: "" };
    assert.equal(formatMoney(null), "—");
    assert.equal(formatMoney("9007199254740993").replace(/\s/g, ""), "9007199254740993₽");
    renderMetrics(panel, null);
    assert.equal((panel.innerHTML.match(/>—<\/dd>/g) ?? []).length, 6);
    renderMetrics(panel, {
        q25_per_pack: "5000", q25_quality: "verified", sample_count: 21,
        turnover_24h: 23, reasons: [],
        model: { price_window_hours: 24 },
        coverage: {
            lots_complete: true, history_prefix_consistent: true, history_24h_complete: true,
        },
        lots: [
            { status: "candidate", roi_percent: 20 },
            { status: "candidate", roi_percent: null },
            { status: "below_roi", roi_percent: 5 },
        ],
    });
    const values = [...panel.innerHTML.matchAll(/<dd>(.*?)<\/dd>/g)]
        .map((match) => match[1].replace(/\s/g, ""));
    assert.deepEqual(values, ["3", "1", "5000₽", "24часа", "21", "23"]);
    assert.doesNotMatch(panel.innerHTML, /Предварительная оценка/);
});

test("KPI и таблица отмечают оценочный результат", () => {
    const panel = { innerHTML: "" };
    const candidate = {
        id: 1, amount: 6, status: "candidate", exit_price_per_pack: "130000", roi_percent: 12,
    };
    const dataset = {
        q25_per_pack: "130000", q25_quality: "preliminary", sample_count: 42,
        turnover_24h: 42, reasons: [],
        model: { price_window_hours: 24 },
        coverage: {
            lots_complete: true, history_prefix_consistent: false, history_24h_complete: false,
        },
        lots: [candidate],
    };
    renderMetrics(panel, dataset);
    assert.match(panel.innerHTML, /Q25 за партию/);
    assert.match(panel.innerHTML, /130\s000 ₽.*Предварительная оценка/);
    assert.match(panel.innerHTML, /<dd>24 часа<\/dd>/);
    assert.match(panel.innerHTML, /<dd>42<small[^>]+>Наблюдения в выборке/);
    assert.match(panel.innerHTML, /Оценочно выгодных<\/dt>\s*<dd>1<\/dd>/);
    assert.match(panel.innerHTML, /Продаж партий в Q25/);
    assert.match(panel.innerHTML, /Наблюдаемые продажи партий за 24 часа<\/dt>\s*<dd>42<\/dd>/);
    assert.doesNotMatch(panel.innerHTML, /q25-notice/);

    const rows = { innerHTML: "", querySelectorAll: () => [] };
    renderLotRows(rows, null, [candidate], 1, isEstimatedAnalysis(dataset));
    assert.match(rows.innerHTML, /130\s000 ₽/);
    assert.match(rows.innerHTML, /Оценочно выгодно/);
    assert.doesNotMatch(rows.innerHTML, /Потенциально выгодно/);

    dataset.q25_quality = "verified";
    dataset.coverage.history_prefix_consistent = true;
    assert.equal(isEstimatedAnalysis(dataset), true);
    dataset.coverage.history_24h_complete = true;
    assert.equal(isEstimatedAnalysis(dataset), false);
});

test("частичная история показывает медиану, ликвидность считает покрытые интервалы", () => {
    const analysis = {
        evaluated_at: "2026-06-11T00:00:00Z",
        sell_quantity: 10,
        homogeneous: true,
        coverage: { history_prefix_consistent: true },
        model: { sale_commission_bps: null },
        history: {
            covered_from: "2026-06-10T16:00:00Z",
            sales: [
                { time: "2026-06-10T20:01:00Z", amount: 5, price_per_pack: "50" },
                { time: "2026-06-10T19:00:00Z", amount: 10, price_per_pack: "200" },
                { time: "2026-06-10T18:00:00Z", amount: 10, price_per_pack: "100" },
                { time: "2026-06-10T12:00:00Z", amount: 10, price_per_pack: "60" },
            ],
        },
    };
    const series = historySeries(analysis, 1);
    assert.deepEqual(series.sales, [null, null, null, null, 2, 0]);
    assert.deepEqual(series.prices, [null, null, null, "60", "150", null]);
    assert.deepEqual(series.priceCounts, [0, 0, 0, 1, 2, 0]);
    assert.deepEqual(series.coveredIntervals, [
        false, false, false, false, true, true,
    ]);
    assert.equal(series.complete, false);
    assert.equal(series.pricePartial, true);
    analysis.homogeneous = false;
    assert.ok(historySeries(analysis, 1).sales.every((value) => value === null));
});

test("медиана учитывает только подходящие партии и точно хранит половину рубля", () => {
    const sales = [
        ["01:05", 10, "140000"],
        ["01:21", 10, "148000"],
        ["01:48", 10, "155000"],
        ["02:00", 1, "15000"],
        ["02:30", 5, "72000"],
        ["03:00", 20, "270000"],
        ["04:00", 10, "140000"],
        ["05:00", 10, "145000"],
        ["06:00", 10, "150000"],
        ["07:00", 10, "160000"],
        ["08:00", 10, "150000"],
        ["16:00", 10, "9007199254740993"],
        ["17:00", 10, "9007199254740994"],
    ].map(([time, amount, price]) => ({
        time: `2026-06-10T${time}:00Z`, amount, price_per_pack: price,
    }));
    const analysis = {
        evaluated_at: "2026-06-11T00:00:00Z",
        sell_quantity: 10,
        homogeneous: true,
        coverage: { history_prefix_consistent: true },
        history: { covered_from: "2026-06-10T00:00:00Z", sales },
    };
    const series = historySeries(analysis, 1);
    assert.deepEqual(series.prices, [
        "148000", "147500", "150000", null, "9007199254740993.5", null,
    ]);
    assert.deepEqual(series.priceCounts, [3, 4, 1, 0, 2, 0]);
    assert.equal(series.pricePartial, false);
    assert.deepEqual(historySeries({ ...analysis, sell_quantity: 20 }, 1).prices,
        ["270000", null, null, null, null, null]);
    assert.ok(historySeries({ ...analysis, sell_quantity: 11 }, 1).prices
        .every((price) => price === null));
});

test("неподтверждённая история показывает цены с предупреждением", () => {
    const analysis = {
        evaluated_at: "2026-06-11T00:00:00Z",
        sell_quantity: 10,
        homogeneous: true,
        coverage: { history_prefix_consistent: false },
        history: {
            covered_from: "2026-06-10T00:00:00Z",
            sales: [
                { time: "2026-06-10T18:00:00Z", amount: 10, price_per_pack: "149999" },
                { time: "2026-06-10T19:00:00Z", amount: 10, price_per_pack: "150000" },
                { time: "2026-06-10T19:05:00Z", amount: 10, price_per_pack: "0" },
                { time: "2026-06-10T19:10:00Z", amount: 10, price_per_pack: "invalid" },
            ],
        },
    };
    const series = historySeries(analysis, 1);
    assert.deepEqual(series.prices, [null, null, null, null, "149999.5", null]);
    assert.equal(series.pricePartial, true);
    assert.equal(series.complete, false);
    assert.ok(series.sales.every((value) => value === null));
    assert.equal(
        series.priceWarning,
        "Согласованность истории не подтверждена. Возможны пропущенные или повторяющиеся сделки",
    );
    analysis.source_errors = { history: { message: "EXBO недоступен" } };
    const unavailable = historySeries(analysis, 1);
    assert.deepEqual(unavailable.prices, []);
    assert.equal(unavailable.priceEmpty, "EXBO недоступен");
    assert.equal(unavailable.priceWarning, "");
});

test("отказ ручной операции блокирует только её версию на том же снимке", () => {
    const version = (value) => ({ snapshot_id: "S", version: value });
    assert.equal(versionPollState(version("v1"), "v1", "failed", 0).status, "unchanged");
    assert.equal(versionPollState(version("failed"), "v1", "failed", 0).status, "failed");
    assert.equal(versionPollState(version(null), "v1", "failed", 0).status, "pending");
    assert.equal(versionPollState(version("v2"), "v1", "failed", 0).status, "update");
});

test("ответы 429 и 503 сохраняют причину и уважают паузу опроса", async () => {
    const rate = await checkAnalysisVersion("item", DEFAULT_STRATEGY, async () =>
        new Response("", { status: 429, headers: { "Retry-After": "30" } }));
    const unavailable = await checkAnalysisVersion("item", DEFAULT_STRATEGY, async () =>
        new Response("", { status: 503 }));
    assert.deepEqual(rate, { error: 429, retryMs: 30000 });
    assert.deepEqual(unavailable, { error: 503, retryMs: 0 });
    assert.deepEqual(versionPollState(rate, "v1", undefined, 0), {
        status: "error", failures: 1, retryMs: 30000,
    });
    assert.equal(versionPollState(unavailable, "v1", undefined, 1).retryMs, 10000);
});

test("скрытая вкладка, активный запрос и ручное обновление не запускают опрос", () => {
    const state = { hidden: false, inFlight: false, refreshing: false, retryAt: 10 };
    for (const key of ["hidden", "inFlight", "refreshing"]) {
        assert.equal(canPollVersion({ ...state, [key]: true }, 10), false);
    }
    assert.equal(canPollVersion(state, 9), false);
    assert.equal(canPollVersion(state, 10), true);
});

test("границы обоих графиков включают конец окна, в том числе через Новый год", () => {
    const end = "2027-01-01T00:29:00+03:00";
    const sale = { time: "2027-01-01T00:27:00+03:00", amount: 10, price_per_pack: "750" };
    const analysis = {
        sell_quantity: 10, homogeneous: true,
        coverage: { history_prefix_consistent: true },
        history: { covered_from: "2026-11-30T00:29:00+03:00", covered_until: end, sales: [sale] },
    };
    for (const days of [1, 7, 14, 30]) {
        const series = historySeries(analysis, days);
        assert.equal(series.boundaryLabels.length, (days === 1 ? 6 : days) + 1);
        assert.equal(series.priceIntervals.length + 1, series.boundaryLabels.length);
        assert.equal(series.latestSaleAt, Date.parse(sale.time));
        assert.equal(series.prices.at(-1), "750");
        assert.equal(series.sales.at(-1), 1);
    }
    const daily = historySeries(analysis, 1);
    assert.equal(daily.boundaryLabels[0], daily.boundaryLabels.at(-1));
    assert.notEqual(daily.boundaryLabels.at(-2), daily.boundaryLabels.at(-1));
    analysis.history.sales = [{ ...sale, amount: 5 }];
    assert.equal(historySeries(analysis, 1).latestSaleAt, null);
});
