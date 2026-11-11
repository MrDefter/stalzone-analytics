import { createChart } from "./charts.js";
import {
    DEFAULT_FILTERS,
    DEFAULT_HISTORY_PERIOD,
    DEFAULT_STRATEGY,
    STRATEGY_STORAGE_KEY,
    auctionKey,
    checkAnalysisVersion,
    createAnalysisLoader,
    fetchAnalysis,
    filterLots,
    historySeries,
    isEstimatedAnalysis,
    readRefresh,
    loadStrategy,
    statusLabel,
} from "./auction-data.js";
import {
    emptyContent,
    highlightLot,
    icon,
    packLabel,
    renderDetails,
    renderItemList,
    renderLotRows,
    renderMetrics,
} from "./ui.js";

const byId = (id) => document.getElementById(id);
const tabButtons = [...document.querySelectorAll("[data-tab]")];
const sortButtons = document.querySelectorAll("[data-sort]");
const periodButtons = document.querySelectorAll("[data-period]");
const periodSelect = byId("statistics-period");
const searchInputs = document.querySelectorAll("[data-item-search]");
const sidebarBreakpoint = matchMedia("(max-width: 1100px)");
const sidebarToggle = document.querySelector("[data-action='sidebar']");
const sidebarSearch = byId("item-sidebar").querySelector("input");
const defaultInterface = { density: "regular", reduceMotion: false };
const snapshotTime = new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
});
const HISTORY_POLL_INTERVAL_MS = 5000;
const REFRESH_POLL_INTERVAL_MS = 2000;
const REFRESH_TIMEOUT_MS = 15 * 60 * 1000;
const refreshes = new Map();
const failedSnapshots = new Map();
const refreshRegions = document.querySelectorAll("[data-refresh-region]");
const appState = {
    items: [],
    catalogStatus: "loading",
    selectedItemId: null,
    selectedModel: "statistics",
    selectedLotId: null,
    selectedPipeline: "conservative_q25",
    analysis: { status: "idle" },
    historySync: null,
    retryAt: 0,
    historyPeriod: DEFAULT_HISTORY_PERIOD,
    filters: { ...DEFAULT_FILTERS },
    strategy: loadStrategy(),
    sort: { key: null, direction: "ascending" },
    preferences: { ...defaultInterface },
    search: "",
    sidebarOpen: false,
};

const charts = [
    createChart(byId("price-chart"), "price"),
    createChart(byId("sales-chart"), "sales"),
    createChart(byId("history-price-chart"), "price"),
    createChart(byId("history-sales-chart"), "sales"),
];

let popoverTrigger = null;
let toastTimer;
let toastOwner = null;
let historyPollTimer;

function itemAndPack() {
    const item = appState.items.find(({ item_id }) => item_id === appState.selectedItemId);
    const dataset = appState.analysis.data;
    const quantity = dataset ? dataset.sell_quantity : item?.sell_quantity ?? null;
    return { item, quantity, dataset };
}

const loadAnalysis = createAnalysisLoader(applyAnalysis);

function applyAnalysis(state) {
    window.clearTimeout(historyPollTimer);
    const previousItem = appState.analysis.data?.item_id;
    const previousSnapshot = appState.analysis.data?.snapshot_id;
    appState.analysis = state;
    appState.historySync = state.data?.history.sync ?? null;
    const retryAfter = state.retryAfter ?? Math.max(
        0, ...Object.values(state.data?.source_errors ?? {})
            .map((error) => error.retry_after_seconds ?? 0),
    );
    appState.retryAt = retryAfter ? Date.now() + retryAfter * 1000 : 0;
    if (!state.data || state.data.item_id !== previousItem
        || state.data.snapshot_id !== previousSnapshot) {
        appState.selectedLotId = null;
    }
    updateStatistics();
    updateHistory();
    updateRefreshControls();
    if (["ready", "pending"].includes(state.status) || state.retryable) {
        pollAnalysis(state);
    }
}

function requestAnalysis(keepData = false) {
    const refresh = refreshes.get(refreshKey());
    if (refresh?.data) {
        loadAnalysis.cancel();
        applyAnalysis({ status: "ready", data: refresh.data });
        return;
    }
    loadAnalysis(appState.selectedItemId, appState.historyPeriod, appState.strategy,
        keepData ? appState.analysis.data : null);
}

function pollAnalysis(state) {
    historyPollTimer = window.setTimeout(async () => {
        if (appState.analysis !== state) {
            return;
        }
        if (!document.hidden && Date.now() >= appState.retryAt && !refreshes.has(refreshKey())) {
            let check;
            try {
                check = await checkAnalysisVersion(appState.selectedItemId, appState.strategy);
            } catch {
                // Временный сбой статуса допускает повтор опроса.
            }
            if (appState.analysis !== state) {
                return;
            }
            if (!refreshes.has(refreshKey()) && check) {
                const failedSnapshot = failedSnapshots.get(refreshKey());
                if (check.version && check.version !== state.data?.version
                    && (!failedSnapshot || check.snapshot_id !== failedSnapshot)) {
                    failedSnapshots.delete(refreshKey());
                    requestAnalysis(true);
                    return;
                }
                if (state.data && check.version && check.version === state.data.version) {
                    state.data.snapshot_stale = check.snapshot_stale;
                    state.data.history.sync = check.history;
                    updateLotsTime(state.data);
                }
                if (!state.data || check.version === state.data.version) {
                    appState.historySync = check.history;
                    updateHistoryCaption(state.data);
                }
                updateRefreshControls();
            }
        }
        pollAnalysis(state);
    }, HISTORY_POLL_INTERVAL_MS);
}

function refreshKey(itemId = appState.selectedItemId) {
    const { item_id, region, mode } = auctionKey(itemId);
    return `stalzone.refresh:${region}:${mode}:${item_id}`;
}

function updateRefreshControls() {
    const busy = refreshes.has(refreshKey());
    const retry = byId("analysis-refresh");
    retry.disabled = busy || !appState.selectedItemId;
    retry.querySelector("span").textContent = busy ? "Обновляем…" : "Обновить";
    byId("filter-button").disabled = busy;
    document.querySelector('[data-action="export"]').disabled = busy;
    periodSelect.disabled = busy;
    for (const region of refreshRegions) {
        region.setAttribute("aria-busy", String(busy));
        region.querySelector("[data-refresh-content]").inert = busy;
    }
    const { status, data, message } = appState.analysis;
    byId("analysis-caption").textContent = !data && !busy && ["error", "pending"].includes(status)
        ? (appState.historySync?.error ? "История пока недоступна. Ожидаем повторную загрузку." : message)
        : "";
}

function matchingItems() {
    const query = appState.search.trim();
    const nameQuery = query.toLocaleLowerCase("ru-RU");
    return appState.items.filter((item) => (
        item.item_id.includes(query)
        || item.name_ru.toLocaleLowerCase("ru-RU").includes(nameQuery)
    ));
}

function updateItems() {
    const emptyState = appState.catalogStatus === "ready"
        ? (appState.items.length ? "unmatched" : "empty")
        : appState.catalogStatus;
    renderItemList(byId("item-list"), matchingItems(), appState.selectedItemId, emptyState);
}

async function loadItems() {
    appState.catalogStatus = "loading";
    updateItems();
    try {
        const response = await fetch("/api/items", { cache: "no-store" });
        if (!response.ok) {
            throw new Error("Ошибка загрузки каталога");
        }
        const { items } = await response.json();
        if (!Array.isArray(items)) {
            throw new Error("Некорректный формат каталога");
        }
        appState.items = items;
        if (!items.some(({ item_id }) => item_id === appState.selectedItemId)) {
            appState.selectedItemId = items[0]?.item_id ?? null;
            appState.selectedLotId = null;
        }
        appState.catalogStatus = "ready";
    } catch {
        appState.items = [];
        appState.selectedItemId = null;
        appState.selectedLotId = null;
        appState.catalogStatus = "error";
    }
    updateItems();
    resumeRefresh();
    requestAnalysis();
}

function updateLotsTime(dataset) {
    const time = byId("lots-time");
    const date = dataset?.received_at;
    byId("lots-updated").hidden = !date;
    time.dateTime = date ?? "";
    time.textContent = date ? snapshotTime.format(new Date(date)) : "";
    const quality = byId("lots-quality");
    const labels = [
        dataset?.snapshot_stale && "Устарел",
        dataset?.coverage?.lots_complete === false && "Неполный снимок",
    ].filter(Boolean);
    quality.textContent = labels.join(" · ");
    quality.hidden = !labels.length;
    quality.title = labels.length ? "Показаны доступные лоты из сохранённого снимка EXBO." : "";
}

function updateStatistics() {
    const { item, dataset } = itemAndPack();
    const estimated = isEstimatedAnalysis(dataset);
    const lots = filterLots(dataset?.lots ?? [], appState.filters, appState.sort, estimated);
    const selected = lots.find(({ id }) => id === appState.selectedLotId) ?? lots[0];
    appState.selectedLotId = selected?.id ?? null;

    byId("lots-title").textContent = item ? `Активные лоты: ${item.name_ru}` : "Активные лоты";
    updateLotsTime(dataset);
    renderMetrics(byId("metrics"), dataset);
    renderLotRows(byId("lots-body"), item, lots, appState.selectedLotId, estimated);
    byId("filters-panel").querySelector('option[value="candidate"]').textContent = statusLabel("candidate", estimated);
    renderDetails(byId("lot-details"), item, dataset, selected);
    byId("table-scroll").hidden = !lots.length;

    const empty = byId("table-empty");
    empty.hidden = Boolean(lots.length);
    if (!lots.length) {
        if (dataset?.source_errors?.lots) {
            empty.innerHTML = emptyContent("Не удалось загрузить активные лоты");
        } else if (dataset?.lots.length) {
            empty.innerHTML = emptyContent("Подходящих лотов нет", "Попробуйте изменить фильтры.");
        } else {
            const title = dataset
                ? dataset.coverage.lots_complete ? "Активных лотов нет" : "В загруженной части лотов нет"
                : item ? "Лоты пока недоступны" : "Предмет не выбран";
            empty.innerHTML = emptyContent(title);
        }
    }
    for (const button of sortButtons) {
        const active = appState.sort.key === button.dataset.sort;
        const direction = active ? appState.sort.direction : "none";
        button.closest("th").setAttribute("aria-sort", direction);
        const arrow = { ascending: "arrow-up", descending: "arrow-down", none: "sort" };
        button.querySelector(".icon").outerHTML = icon(arrow[direction]);
    }
    const activeFilters = Object.entries(DEFAULT_FILTERS).some(
        ([key, defaultValue]) => appState.filters[key] !== defaultValue,
    );
    byId("filter-button").querySelector(".filter-dot").hidden = !activeFilters;
}

function updateHistoryCaption(dataset) {
    const caption = byId("price-caption");
    const { covered_from: from, covered_until: until } = dataset?.history ?? {};
    const sync = appState.historySync;
    const consistent = dataset?.coverage?.history_prefix_consistent === true;
    caption.title = [
        dataset && !consistent && "Согласованность истории не подтверждена. Возможны пропущенные или повторяющиеся сделки",
        sync?.error ?? dataset?.source_errors?.history?.message,
        sync?.stale && "Показана история до последней успешной синхронизации",
    ].filter(Boolean).join(". ");
    caption.tabIndex = caption.title ? 0 : -1;
    if (from && until) {
        const end = new Date(until);
        const requested = end.getTime() - appState.historyPeriod * 86_400_000;
        const start = new Date(Math.max(Date.parse(from), requested));
        const partial = consistent && Date.parse(from) <= requested ? "" : " · Частичное покрытие";
        caption.textContent = `Период истории: ${snapshotTime.format(start)} — ${snapshotTime.format(end)}${partial}`;
    } else {
        const loading = !sync?.last_success_at && ["pending", "running"].includes(sync?.status);
        caption.textContent = loading ? "Загрузка истории продаж…" : "История пока недоступна";
    }
}

function updateHistory() {
    const { item, quantity, dataset } = itemAndPack();
    const period = appState.historyPeriod;
    const history = historySeries(dataset, period);
    const priceCaption = quantity === null ? "Медианная цена продажи партии"
        : `Медианная цена продажи партии по ${packLabel(quantity)}`;
    const coverage = dataset?.source_errors?.history ? " · история недоступна"
        : history && !history.complete ? " · неполное окно" : "";
    const salesCaption = (quantity === null ? "Продажи партий" : `Продажи партий по ${packLabel(quantity)}`) + coverage;
    byId("history-item").textContent = item ? `История аукциона: ${item.name_ru}` : "История аукциона";
    byId("history-caption").textContent = dataset
        ? `Снимок ${snapshotTime.format(new Date(dataset.history.covered_until ?? dataset.evaluated_at))}${coverage}`
        : "История продаж EXBO";
    updateHistoryCaption(dataset);
    byId("history-price-caption").textContent = priceCaption;
    const priceWarning = history?.priceWarning ?? "";
    const warning = byId("history-price-warning");
    warning.textContent = priceWarning;
    warning.hidden = !priceWarning;
    byId("sales-caption").textContent = salesCaption;
    byId("history-sales-caption").textContent = salesCaption;
    periodSelect.value = String(period);
    for (const button of periodButtons) {
        button.setAttribute("aria-pressed", String(Number(button.dataset.period) === period));
    }
    for (const chart of charts) {
        chart(history, quantity);
    }
}

function selectHistoryPeriod(value) {
    appState.historyPeriod = Number(value);
    const { data, days = 0, status } = appState.analysis;
    if (
        (status === "ready" || status === "loading")
        && appState.historyPeriod > (data?.history.days ?? days)
        && Date.now() >= appState.retryAt
    ) {
        requestAnalysis();
    } else {
        updateHistory();
    }
}

function selectLot(id) {
    const { item, dataset } = itemAndPack();
    const lot = dataset?.lots.find((entry) => entry.id === id);
    if (!lot) {
        return;
    }
    appState.selectedLotId = id;
    highlightLot(byId("lots-body"), id);
    renderDetails(byId("lot-details"), item, dataset, lot);
}

function selectItem(id) {
    if (appState.selectedItemId !== id) {
        if (toastOwner) hideToast();
        appState.selectedItemId = id;
        appState.selectedLotId = null;
        for (const button of byId("item-list").querySelectorAll("[data-item-id]")) {
            button.setAttribute("aria-pressed", String(button.dataset.itemId === id));
        }
        resumeRefresh();
        requestAnalysis();
    }
    setSidebar(false, byId("item-sidebar").contains(document.activeElement));
}

function selectModel(model) {
    appState.selectedModel = model;
    for (const button of tabButtons) {
        const active = button.dataset.tab === model;
        button.setAttribute("aria-selected", String(active));
        button.tabIndex = active ? 0 : -1;
        byId(button.dataset.tab).hidden = !active;
    }
    updateRefreshControls();
}

function setSidebar(open, focus = false) {
    const wasOpen = appState.sidebarOpen;
    appState.sidebarOpen = open && sidebarBreakpoint.matches;
    byId("item-sidebar").classList.toggle("is-open", appState.sidebarOpen);
    document.querySelector(".sidebar-scrim").hidden = !appState.sidebarOpen;
    sidebarToggle.setAttribute("aria-expanded", String(appState.sidebarOpen));
    if (focus && appState.sidebarOpen) {
        sidebarSearch.focus();
    } else if (focus && wasOpen && sidebarBreakpoint.matches) {
        sidebarToggle.focus();
    }
}

function closePopover(restoreFocus = false) {
    if (!popoverTrigger) {
        return;
    }
    byId(popoverTrigger.dataset.popover).hidden = true;
    popoverTrigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) {
        popoverTrigger.focus();
    }
    popoverTrigger = null;
}

function openPopover(trigger) {
    const wasOpen = popoverTrigger === trigger;
    closePopover();
    if (wasOpen) {
        return;
    }
    const panel = byId(trigger.dataset.popover);
    if (panel.id === "filters-panel") {
        fillForm(panel, appState.filters);
    }
    popoverTrigger = trigger;
    trigger.setAttribute("aria-expanded", "true");
    panel.hidden = false;
    panel.querySelector("input, button")?.focus({ preventScroll: true });
}

function fillForm(form, values) {
    for (const [name, value] of Object.entries(values)) {
        const input = form.elements.namedItem(name);
        if (input.type === "checkbox") {
            input.checked = value;
        } else {
            input.value = value ?? "";
        }
    }
}

function readForm(form, defaults) {
    const values = new FormData(form);
    return Object.fromEntries(Object.entries(defaults).map(([name, defaultValue]) => {
        const value = values.get(name);
        if (typeof defaultValue === "boolean") {
            return [name, values.has(name)];
        }
        if (typeof defaultValue === "number" || defaultValue === null) {
            return [name, value === "" ? null : Number(value)];
        }
        return [name, value];
    }));
}

function fillStrategyForm(strategy) {
    fillForm(byId("model-form"), {
        min_sales: strategy.min_sales,
        min_roi_percent: strategy.min_roi_bps / 100,
    });
}

function openDialog(button) {
    const dialog = byId(button.dataset.dialog);
    const opener = popoverTrigger ?? button;
    closePopover();
    if (dialog.id === "model-dialog") {
        fillStrategyForm(appState.strategy);
    } else if (dialog.id === "interface-dialog") {
        fillForm(byId("interface-form"), appState.preferences);
    }
    dialog.addEventListener("close", () => opener.focus(), { once: true });
    dialog.showModal();
}

function saveFilters(filters) {
    appState.filters = filters;
    updateStatistics();
    closePopover(true);
}

function hideToast() {
    window.clearTimeout(toastTimer);
    byId("toast").hidden = true;
    toastOwner = null;
}

function toast(message, { kind = "info", title = "", owner = null } = {}) {
    if (!owner && toastOwner && refreshes.has(toastOwner)) return;
    window.clearTimeout(toastTimer);
    toastOwner = owner;
    const notification = byId("toast");
    notification.dataset.kind = kind;
    byId("toast-title").textContent = title;
    byId("toast-message").textContent = message;
    byId("toast-icon").innerHTML = kind === "loading" ? '<span class="refresh-spinner"></span>'
        : icon({ success: "check", error: "x", info: "info" }[kind]);
    notification.hidden = false;
    if (kind === "success" || kind === "info") {
        toastTimer = window.setTimeout(hideToast, 5000);
    }
}

function showRefresh(key) {
    if (!refreshes.get(key).dismissed) {
        toast("Загружаем актуальные данные аукциона…", {
            kind: "loading", title: "Обновление данных", owner: key,
        });
    }
}

function saveRefresh(key, value) {
    try {
        if (value) sessionStorage.setItem(key, JSON.stringify(value));
        else sessionStorage.removeItem(key);
    } catch {
        // Команда продолжает отслеживаться в памяти текущей страницы.
    }
}

function resumeRefresh() {
    if (!appState.selectedItemId) return;
    const key = refreshKey();
    if (refreshes.has(key)) {
        showRefresh(key);
        return;
    }
    try {
        const saved = JSON.parse(sessionStorage.getItem(key));
        if (typeof saved?.id === "string" && saved.until > Date.now()) {
            refreshes.set(key, saved);
            showRefresh(key);
            trackRefresh(appState.selectedItemId, saved);
        } else if (saved) {
            saveRefresh(key, null);
        }
    } catch {
        // Хранилище недоступно или содержит повреждённую запись.
    }
}

function refreshError(code) {
    return {
        RateLimitError: "EXBO ограничил частоту запросов. Повторите попытку позже.",
        HistoryRefreshFailed: "Не удалось обновить историю продаж. Повторите попытку.",
        SnapshotInconsistent: "Не удалось подтвердить согласованность лотов. Повторите попытку.",
        CommandExpired: "Команда больше недоступна. Запросите обновление снова.",
        Timeout: "Ядро не подтвердило обновление вовремя. Проверьте его состояние и повторите попытку.",
        RequestFailed: "Не удалось поставить обновление в очередь. Проверьте соединение и повторите попытку.",
    }[code] ?? "Не удалось получить данные аукциона. Повторите попытку.";
}

function matchesPublication(data, result) {
    return Boolean(result.snapshot_id && data.snapshot_id === result.snapshot_id)
        || Date.parse(data.received_at) > Date.parse(result.received_at);
}

async function trackRefresh(itemId, operation) {
    const key = refreshKey(itemId);
    const source = auctionKey(itemId);
    let error = "Timeout";
    try {
        if (!operation.id) {
            const command = await readRefresh(source);
            if (!command.request_id) throw new Error("Missing command");
            operation.id = command.request_id;
            saveRefresh(key, { id: operation.id, until: operation.until });
        }
        let result;
        while (Date.now() < operation.until) {
            if (!result) {
                const status = await readRefresh(source, operation.id).catch(() => null);
                if (status?.status === "error") {
                    if (status.snapshot_id) failedSnapshots.set(key, status.snapshot_id);
                    error = status.error ?? "SourceError";
                    break;
                }
                if (status?.status === "done") result = status;
            }
            if (result) {
                if (refreshKey() !== key) return;
                const days = appState.historyPeriod;
                const strategy = appState.strategy;
                const state = await fetchAnalysis(itemId, days, strategy, AbortSignal.timeout(8000));
                if (refreshKey() !== key) return;
                if (state.status === "ready" && matchesPublication(state.data, result)
                    && days === appState.historyPeriod && strategy === appState.strategy) {
                    failedSnapshots.delete(key);
                    loadAnalysis.cancel();
                    applyAnalysis(state);
                    error = null;
                    break;
                }
            }
            await new Promise((resolve) => window.setTimeout(resolve, REFRESH_POLL_INTERVAL_MS));
        }
    } catch {
        error = "RequestFailed";
    } finally {
        refreshes.delete(key);
        saveRefresh(key, null);
        if (refreshKey() === key) {
            updateRefreshControls();
            if (error) {
                if (appState.analysis.status === "loading") {
                    applyAnalysis({ status: "error", message: refreshError(error), retryable: true });
                }
                toast(refreshError(error), { kind: "error", title: "Ошибка обновления", owner: key });
            } else {
                toast("Актуальные данные аукциона успешно загружены", {
                    kind: "success", title: "Данные обновлены", owner: key,
                });
            }
        }
    }
}

function startRefresh() {
    const itemId = appState.selectedItemId;
    const key = refreshKey(itemId);
    if (!itemId || refreshes.has(key)) return;
    const operation = { until: Date.now() + REFRESH_TIMEOUT_MS, data: appState.analysis.data };
    refreshes.set(key, operation);
    closePopover();
    loadAnalysis.cancel();
    updateRefreshControls();
    showRefresh(key);
    trackRefresh(itemId, operation);
}

function selectPipeline(button) {
    const id = button.dataset.pipeline;
    closePopover(true);
    if (id === appState.selectedPipeline) {
        toast("Активна консервативная оценка Q25.");
    } else {
        toast(`«${button.textContent.trim()}» пока не реализована.`);
    }
}

const actions = {
    export: () => toast("Экспорт пока не реализован"),
    sidebar: () => setSidebar(!appState.sidebarOpen, true),
    "close-sidebar": () => setSidebar(false, true),
    "retry-items": loadItems,
    "refresh-analysis": startRefresh,
    "reset-filters": () => saveFilters({ ...DEFAULT_FILTERS }),
    "reset-strategy": () => fillStrategyForm(DEFAULT_STRATEGY),
    "dismiss-toast": () => {
        const operation = refreshes.get(toastOwner);
        if (operation) operation.dismissed = true;
        hideToast();
    },
    statistics: () => {
        selectModel("statistics");
        byId("tab-statistics").focus();
    },
};

document.addEventListener("click", (event) => {
    const target = event.target;
    if (popoverTrigger && !popoverTrigger.parentElement.contains(target)) {
        closePopover();
    }
    const button = target.closest("button");
    if (!button) {
        return;
    }
    const { action, dialog, popover, itemId, tab, period, sort, pipeline } = button.dataset;
    if (action) {
        actions[action]();
    } else if (dialog) {
        openDialog(button);
    } else if (button.hasAttribute("data-close-dialog")) {
        button.closest("dialog").close();
    } else if (popover) {
        openPopover(button);
    } else if (itemId) {
        selectItem(itemId);
    } else if (tab) {
        selectModel(tab);
    } else if (period) {
        selectHistoryPeriod(period);
    } else if (sort) {
        const descending = appState.sort.key === sort && appState.sort.direction === "ascending";
        appState.sort = { key: sort, direction: descending ? "descending" : "ascending" };
        updateStatistics();
    } else if (pipeline) {
        selectPipeline(button);
    }
});

periodSelect.addEventListener("change", () => selectHistoryPeriod(periodSelect.value));

byId("lots-body").addEventListener("click", (event) => {
    const row = event.target.closest("[data-lot-id]");
    if (row) {
        selectLot(Number(row.dataset.lotId));
    }
});

byId("filters-panel").addEventListener("submit", (event) => {
    event.preventDefault();
    saveFilters(readForm(event.currentTarget, DEFAULT_FILTERS));
});

byId("model-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) {
        return;
    }
    if (Date.now() < appState.retryAt) {
        toast("Дождитесь окончания паузы EXBO, чтобы применить настройки.");
        return;
    }
    const strategy = {
        min_sales: form.elements.namedItem("min_sales").valueAsNumber,
        min_roi_bps: Math.round(form.elements.namedItem("min_roi_percent").valueAsNumber * 100),
    };
    appState.strategy = strategy;
    try {
        localStorage.setItem(STRATEGY_STORAGE_KEY, JSON.stringify(strategy));
    } catch {
        toast("Настройки применены на текущей странице. Хранилище браузера недоступно.");
    }
    byId("model-dialog").close();
    requestAnalysis();
});

byId("interface-form").addEventListener("submit", (event) => {
    event.preventDefault();
    appState.preferences = readForm(event.currentTarget, defaultInterface);
    document.body.dataset.density = appState.preferences.density;
    document.body.dataset.motion = appState.preferences.reduceMotion ? "reduced" : "regular";
    byId("interface-dialog").close();
    toast("Настройки интерфейса применены.");
});

for (const input of searchInputs) {
    input.addEventListener("input", () => {
        appState.search = input.value;
        for (const other of searchInputs) {
            if (other !== input) {
                other.value = appState.search;
            }
        }
        updateItems();
        if (input !== sidebarSearch) {
            setSidebar(true);
        }
    });
    input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            const matches = matchingItems();
            const item = matches.find(({ item_id }) => item_id === appState.search.trim()) ?? matches[0];
            if (item) {
                selectItem(item.item_id);
            }
        }
    });
}

sidebarBreakpoint.addEventListener("change", () => setSidebar(false));
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !document.querySelector("dialog[open]")) {
        if (popoverTrigger) {
            closePopover(true);
        } else if (appState.sidebarOpen) {
            setSidebar(false, true);
        }
    }

    const index = tabButtons.indexOf(event.target);
    if (index === -1) {
        return;
    }
    const last = tabButtons.length - 1;
    const targets = {
        ArrowLeft: index === 0 ? last : index - 1,
        ArrowRight: index === last ? 0 : index + 1,
        Home: 0,
        End: last,
    };
    if (Object.hasOwn(targets, event.key)) {
        event.preventDefault();
        const next = tabButtons[targets[event.key]];
        selectModel(next.dataset.tab);
        next.focus();
    }
});

document.querySelector(`[data-pipeline="${appState.selectedPipeline}"]`)
    .setAttribute("aria-current", "true");
updateStatistics();
updateHistory();
loadItems();
