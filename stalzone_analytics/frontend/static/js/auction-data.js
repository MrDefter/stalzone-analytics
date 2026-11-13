export const DEFAULT_FILTERS = Object.freeze({
    minRoi: null,
    maxPurchasePrice: "",
    status: "all",
});
export const DEFAULT_HISTORY_PERIOD = 1;
export const DEFAULT_STRATEGY = Object.freeze({ min_sales: 20, min_roi_bps: 1000 });
export const STRATEGY_STORAGE_KEY = "stalzone.strategy.conservative_q25.v1";

const AUCTION_MODE = "production";
const DAY_MS = 86_400_000;
const timeLabel = new Intl.DateTimeFormat("ru-RU", {
    hour: "2-digit",
    minute: "2-digit",
});
const dayLabel = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
});
const dateTimeLabel = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
});

const statuses = {
    candidate: "Потенциально выгодно",
    below_roi: "Ниже порога ROI",
    low_liquidity: "Низкая ликвидность",
    buyout_unavailable: "Нет цены выкупа",
    sell_quantity_missing: "Не задан размер партии",
    insufficient_history: "Недостаточно истории",
    history_unavailable: "История продаж недоступна",
    price_rules_unverified: "Правила не подтверждены",
    variant_unverified: "Вариант не подтверждён",
    sharp_price_drop: "Резкое падение цены",
};
const reasons = {
    sell_quantity_missing: "Для предмета не задан размер партии.",
    variant_unverified: "Однородность предмета не подтверждена.",
    price_rules_unverified: "Единица цены выкупа и расходы аукциона не подтверждены.",
    history_unavailable: "Нет загруженной истории продаж для оценки лотов.",
    insufficient_history: "В загруженной истории за последние 14 дней недостаточно сопоставимых продаж для Q25.",
    buyout_unavailable: "У лота нет цены выкупа.",
    low_liquidity: "Суточного оборота недостаточно с учётом объёма покупки и конкурентов.",
    below_roi: "Прибыль или ROI ниже порога модели.",
    sharp_price_drop: "Резкое снижение рыночной цены",
};
const moneyFields = new Set([
    "buyout_total", "exit_price_per_pack", "commission_total", "profit_total",
]);

export function hasUnconfirmedHistory(analysis) {
    const coverage = analysis?.coverage;
    return analysis?.q25_quality === "preliminary"
        || coverage?.history_prefix_consistent === false
        || (analysis?.turnover_24h != null && (
            coverage?.history_prefix_consistent !== true
            || coverage?.history_24h_complete !== true
        ));
}

export function isEstimatedAnalysis(analysis) {
    return analysis?.snapshot_stale === true || analysis?.coverage?.lots_complete === false
        || analysis?.history?.sync?.stale === true || hasUnconfirmedHistory(analysis);
}

export function statusLabel(status, estimated = false) {
    if (status === "candidate" && estimated) {
        return "Оценочно выгодно";
    }
    return Object.hasOwn(statuses, status) ? statuses[status] : "Без оценки";
}

export function reasonLabel(reason) {
    return Object.hasOwn(reasons, reason) ? reasons[reason] : "Оценка ограничена.";
}

export function filterLots(lots, filters, sort, estimated = false) {
    const maxPrice = filters.maxPurchasePrice ? BigInt(filters.maxPurchasePrice) : null;
    const filtered = lots.filter((lot) => (
        (filters.minRoi === null || (lot.roi_percent !== null && lot.roi_percent >= filters.minRoi))
        && (maxPrice === null || (lot.buyout_total !== null && BigInt(lot.buyout_total) <= maxPrice))
        && (filters.status === "all" || (lot.status ?? "unassessed") === filters.status)
    ));
    if (!sort.key) {
        return filtered;
    }
    const { key, direction } = sort;
    const sign = direction === "ascending" ? 1 : -1;
    return filtered.sort((first, second) => {
        const left = first[key];
        const right = second[key];
        if (left === null || right === null) {
            return Number(left === null) - Number(right === null);
        }
        if (key === "status") {
            return sign * statusLabel(left, estimated).localeCompare(statusLabel(right, estimated), "ru");
        }
        const a = moneyFields.has(key) ? BigInt(left) : left;
        const b = moneyFields.has(key) ? BigInt(right) : right;
        return sign * (Number(a > b) - Number(a < b));
    });
}

function medianPrice(prices) {
    if (prices.length === 0) {
        return null;
    }
    prices.sort((left, right) => Number(left > right) - Number(left < right));
    const middle = Math.floor(prices.length / 2);
    if (prices.length % 2 === 1) {
        return String(prices[middle]);
    }
    const sum = prices[middle - 1] + prices[middle];
    return sum % 2n === 0n ? String(sum / 2n) : `${sum / 2n}.5`;
}

export function historySeries(analysis, days) {
    if (!analysis) {
        return null;
    }
    if (analysis.source_errors?.history) {
        const message = analysis.source_errors.history.message ?? "Не удалось загрузить историю продаж";
        return {
            labels: [], prices: [], sales: [], complete: false,
            boundaryLabels: [], latestSaleAt: null,
            priceIntervals: [], priceCounts: [], coveredIntervals: [], pricePartial: false,
            priceEmpty: message, salesEmpty: message, priceWarning: "",
        };
    }
    if (analysis.history.covered_until === null) return null;
    const until = Date.parse(analysis.history.covered_until ?? analysis.evaluated_at);
    const from = until - days * DAY_MS;
    if (!Number.isFinite(until)) return null;
    const step = days === 1 ? DAY_MS / 6 : DAY_MS;
    const length = (until - from) / step;
    const coveredFrom = Date.parse(analysis.history.covered_from);
    const sync = analysis.history.sync;
    const coveredUntil = sync ? Date.parse(analysis.history.covered_until) : until;
    const comparable = analysis.homogeneous && analysis.sell_quantity !== null;
    const consistent = analysis.coverage?.history_prefix_consistent === true;
    const complete = consistent && coveredFrom <= from && coveredUntil >= until;
    const formatBoundary = days === 1 ? timeLabel : dayLabel;
    const boundaryLabels = Array.from({ length: length + 1 },
        (_, index) => formatBoundary.format(from + index * step));
    const labels = boundaryLabels.slice(0, -1);
    const priceIntervals = [];
    const coveredIntervals = [];
    const priceGroups = Array.from({ length }, () => []);
    const sales = [];
    let latestSaleAt = null;
    for (let index = 0; index < length; index += 1) {
        const start = from + index * step;
        const end = start + step;
        const covered = consistent && start >= coveredFrom && end <= coveredUntil;
        priceIntervals.push(`${dateTimeLabel.format(start)}–${dateTimeLabel.format(end)}`);
        coveredIntervals.push(covered);
        sales.push(comparable && covered ? 0 : null);
    }
    if (comparable) {
        for (const sale of analysis.history.sales) {
            const at = Date.parse(sale?.time);
            if (!(at >= from && at <= until) || sale.amount !== analysis.sell_quantity) {
                continue;
            }
            const index = Math.min(length - 1, Math.floor((at - from) / step));
            if (sales[index] !== null) {
                sales[index] += 1;
            }
            const price = sale.price_per_pack;
            if (
                (typeof price === "string" && /^[1-9]\d*$/.test(price))
                || (Number.isSafeInteger(price) && price > 0)
            ) {
                priceGroups[index].push(BigInt(price));
                latestSaleAt = Math.max(latestSaleAt ?? at, at);
            }
        }
    }
    const unavailable = analysis.sell_quantity === null
        ? reasonLabel("sell_quantity_missing")
        : reasonLabel("variant_unverified");
    const waiting = sync && !sync.last_success_at && ["pending", "running"].includes(sync.status);
    const priceEmpty = waiting ? "История загружается" : comparable
        ? "В загруженной истории нет сопоставимых продаж за период." : unavailable;
    const salesEmpty = waiting ? "История загружается" : comparable
        ? "Ни один интервал выбранного периода не покрыт полностью."
        : unavailable;
    const pricePartial = comparable && !complete;
    const priceWarning = waiting ? "" : !consistent
        ? "Согласованность истории не подтверждена. Возможны пропущенные или повторяющиеся сделки"
        : pricePartial ? "История загружена частично. Показаны доступные продажи." : "";
    return {
        labels, boundaryLabels, latestSaleAt, sales, complete, priceEmpty, salesEmpty,
        priceIntervals, coveredIntervals,
        prices: priceGroups.map(medianPrice),
        priceCounts: priceGroups.map((group) => group.length),
        pricePartial, priceWarning,
    };
}

export function loadStrategy(storage) {
    try {
        const saved = JSON.parse((storage ?? localStorage).getItem(STRATEGY_STORAGE_KEY));
        const { min_sales, min_roi_bps } = saved ?? {};
        if (
            Number.isInteger(min_sales) && min_sales >= 5 && min_sales <= 200
            && Number.isInteger(min_roi_bps) && min_roi_bps >= 0
            && min_roi_bps <= 10_000 && min_roi_bps % 10 === 0
        ) {
            return { min_sales, min_roi_bps };
        }
    } catch {
        return DEFAULT_STRATEGY;
    }
    return DEFAULT_STRATEGY;
}

export function auctionKey(itemId) {
    return { item_id: itemId, region: "RU", mode: AUCTION_MODE };
}

export async function readRefresh(key, requestId = null) {
    const route = requestId ? `commands/${encodeURIComponent(requestId)}` : "refresh";
    const response = await fetch(`/api/auction/${route}?${new URLSearchParams(key)}`, {
        method: requestId ? "GET" : "POST", cache: "no-store",
        signal: AbortSignal.timeout(5000),
    });
    if (requestId && response.status === 404) {
        return { status: "error", error: "CommandExpired" };
    }
    if (!response.ok) throw new Error("Refresh request failed");
    return response.json();
}

export function versionPollState(check, currentVersion, failedVersion, failures) {
    if (check.error) {
        return { status: "error", failures: failures + 1,
            retryMs: Math.max(check.retryMs, 5000 * 2 ** Math.min(failures, 4)) };
    }
    if (check.version === null) return { status: "pending", failures: 0 };
    if (check.version === failedVersion) return { status: "failed", failures: 0 };
    return { status: check.version === currentVersion ? "unchanged" : "update", failures: 0 };
}

export function canPollVersion({ hidden, inFlight, refreshing, retryAt }, now) {
    return !hidden && !inFlight && !refreshing && now >= retryAt;
}

function retryDelay(response) {
    const header = response.headers.get("Retry-After");
    const delay = /^\d+$/.test(header)
        ? Number(header) * 1000 : Date.parse(header) - Date.now();
    return Math.max(0, delay || 0);
}

export async function checkAnalysisVersion(itemId, strategy, fetcher = fetch) {
    const params = new URLSearchParams({
        item_id: itemId, region: "RU", mode: AUCTION_MODE,
        min_sales: String(strategy.min_sales), min_roi_bps: String(strategy.min_roi_bps),
    });
    try {
        const response = await fetcher(`/api/auction/version?${params}`, {
            cache: "no-store", signal: AbortSignal.timeout(4500),
        });
        if (response.ok) {
            const result = await response.json();
            if (result?.version === null || typeof result?.version === "string") return result;
            return { error: "invalid", retryMs: 0 };
        }
        return { error: response.status, retryMs: retryDelay(response) };
    } catch {
        return { error: "network", retryMs: 0 };
    }
}

export function createAnalysisLoader(update, fetcher = fetch) {
    let current = null;
    const load = async (itemId, period = DEFAULT_HISTORY_PERIOD, strategy = DEFAULT_STRATEGY, previousData = null) => {
        current?.abort();
        if (itemId === null) {
            current = null;
            update({ status: "idle" });
            return;
        }
        const controller = new AbortController();
        current = controller;
        if (!previousData) update({ status: "loading", days: period });
        const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(8000)]);
        const result = await fetchAnalysis(itemId, period, strategy, signal, fetcher);
        if (current === controller) {
            current = null;
            if (previousData && !result.data) result.data = previousData;
            update(result);
        }
    };
    load.cancel = () => {
        current?.abort();
        current = null;
    };
    load.pending = () => current !== null;
    return load;
}

export async function fetchAnalysis(itemId, days, strategy, signal, fetcher = fetch) {
    const params = new URLSearchParams({
        ...auctionKey(itemId), days: String(days),
        min_sales: String(strategy.min_sales), min_roi_bps: String(strategy.min_roi_bps),
    });
    try {
        const response = await fetcher(`/api/auction/analysis?${params}`, { cache: "no-store", signal });
        const data = await response.json().catch(() => null);
        if (response.status === 202 && data?.status === "pending") {
            return { status: "pending", message: data.message, retryable: true };
        }
        if (response.ok && data?.item_id === itemId && data.mode === AUCTION_MODE
            && Array.isArray(data.lots) && Array.isArray(data.history?.sales)) {
            return { status: "ready", data };
        }
        const detail = data?.detail;
        const seconds = detail?.retry_after_seconds;
        return {
            status: "error",
            message: typeof detail?.message === "string" ? detail.message : (
                response.status === 422 ? "Сервер отклонил параметры анализа. Проверьте настройки модели."
                    : "Сервер не предоставил данные аукциона."
            ),
            retryable: detail?.retryable ?? (response.ok || response.status >= 500),
            retryAfter: Math.max(
                Number.isSafeInteger(seconds) && seconds > 0 ? seconds : 0,
                retryDelay(response) / 1000,
            ),
        };
    } catch {
        return {
            status: "error",
            message: "Не удалось связаться с сервером STALZONE Analytics.",
            retryable: true,
            retryAfter: 0,
        };
    }
}
