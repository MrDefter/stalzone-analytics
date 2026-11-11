import { createChart } from "./charts.js";
import {
    DEFAULT_FILTERS,
    DEFAULT_HISTORY_PERIOD,
    DEFAULT_STRATEGY,
    STRATEGY_STORAGE_KEY,
    checkAnalysisVersion,
    createAnalysisLoader,
    filterLots,
    historySeries,
    isEstimatedAnalysis,
    refreshAuction,
    loadStrategy,
    reasonLabel,
    statusLabel,
} from "./auction-data.js";
import {
    emptyContent,
    formatInteger,
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
let retryTimer;
let historyPollTimer;

function itemAndPack() {
    const item = appState.items.find(({ item_id }) => item_id === appState.selectedItemId);
    const dataset = appState.analysis.data;
    const quantity = dataset ? dataset.sell_quantity : item?.sell_quantity ?? null;
    return { item, quantity, dataset };
}

const loadAnalysis = createAnalysisLoader((state) => {
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
    updateAnalysisNotice();
    if (["ready", "pending"].includes(state.status) || state.retryable) {
        pollAnalysis(state);
    }
});

function requestAnalysis(keepData = false) {
    loadAnalysis(appState.selectedItemId, appState.historyPeriod, appState.strategy,
        keepData ? appState.analysis.data : null);
}

function pollAnalysis(state) {
    historyPollTimer = window.setTimeout(async () => {
        if (appState.analysis !== state) {
            return;
        }
        if (!document.hidden && Date.now() >= appState.retryAt) {
            let check;
            try {
                check = await checkAnalysisVersion(appState.selectedItemId, appState.strategy);
            } catch {
                // Временный сбой статуса допускает повтор опроса.
            }
            if (appState.analysis !== state) {
                return;
            }
            if (check?.version && check.version !== state.data?.version) {
                requestAnalysis(true);
                return;
            }
            if (check?.version && state.data) {
                const staleChanged = state.data.snapshot_stale !== check.snapshot_stale
                    || appState.historySync?.stale !== check.history?.stale;
                state.data.snapshot_age_seconds = check.snapshot_age_seconds;
                state.data.snapshot_stale = check.snapshot_stale;
                state.data.history.sync = check.history;
                appState.historySync = check.history;
                if (staleChanged) {
                    updateStatistics();
                    updateHistory();
                }
                updateAnalysisNotice();
            } else if (check?.history && !state.data) {
                appState.historySync = check.history;
                updateAnalysisNotice();
            }
        }
        pollAnalysis(state);
    }, HISTORY_POLL_INTERVAL_MS);
}

function historyMessages(sync) {
    if (!sync) {
        return [];
    }
    const busy = ["pending", "running"].includes(sync.status);
    const operations = {
        initial: "Первичная загрузка истории",
        incremental: "Обновление свежих продаж",
        backfill: "Догрузка более старой истории",
    };
    return [
        busy && operations[sync.operation],
        sync.last_success_at && `Последнее обновление ${snapshotTime.format(new Date(sync.last_success_at))}`,
        sync.covered_from && sync.covered_until && `Покрытие с ${snapshotTime.format(new Date(sync.covered_from))} по ${snapshotTime.format(new Date(sync.covered_until))}`,
        sync.stale && "Используется сохранённая история. Свежие продажи могут отсутствовать.",
        sync.error,
    ].filter(Boolean);
}

function updateAnalysisNotice() {
    window.clearTimeout(retryTimer);
    const { status, data, message, retryable } = appState.analysis;
    const notice = byId("analysis-notice");
    notice.hidden = status === "idle" || !["statistics", "history"].includes(appState.selectedModel);
    if (notice.hidden) {
        return;
    }
    const retry = byId("analysis-refresh");
    const wait = Math.max(0, appState.retryAt - Date.now());
    const issues = byId("analysis-reasons");
    const sourceErrors = Object.values(data?.source_errors ?? {});
    const warnings = [
        ...sourceErrors.map((error) => error.message),
        ...(data?.reasons ?? []).map(reasonLabel),
    ];
    const sync = appState.historySync;
    const messages = [...warnings, ...historyMessages(sync)];
    if (data?.snapshot_stale) {
        const age = formatInteger(data.snapshot_age_seconds);
        messages.push(`Снимок лотов устарел. Возраст ${age} с.`);
    }
    issues.replaceChildren(...messages.map((warning) => {
        const item = document.createElement("li");
        item.textContent = warning;
        return item;
    }));
    issues.hidden = messages.length === 0;
    notice.dataset.state = sourceErrors.length || sync?.error ? "partial" : status;
    retry.hidden = status !== "ready" && !retryable;
    retry.disabled = wait > 0;
    retry.textContent = status === "ready" ? "Обновить" : "Повторить";
    const waiting = sync && !sync.last_success_at && ["pending", "running"].includes(sync.status);
    const readyHeading = waiting ? "История загружается"
        : warnings.length ? "Анализ ограничен" : "Снимок аукциона получен";
    const headings = {
        loading: "Читаем опубликованный снимок…",
        pending: message,
        ready: sourceErrors.length ? "Данные загружены частично" : readyHeading,
        error: message,
    };
    const retryHint = wait ? ` · Повтор доступен через ${Math.ceil(wait / 1000)} с.` : "";
    byId("analysis-message").textContent = headings[status] + retryHint;
    if (wait > 0) {
        retryTimer = window.setTimeout(updateAnalysisNotice, Math.min(wait, 1000));
    }
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
    requestAnalysis();
}

function updateStatistics() {
    const { item, dataset } = itemAndPack();
    const estimated = isEstimatedAnalysis(dataset);
    const lots = filterLots(dataset?.lots ?? [], appState.filters, appState.sort, estimated);
    const selected = lots.find(({ id }) => id === appState.selectedLotId) ?? lots[0];
    appState.selectedLotId = selected?.id ?? null;

    byId("lots-title").textContent = item ? `Активные лоты: ${item.name_ru}` : "Активные лоты";
    byId("lots-subtitle").textContent = dataset
        ? [
            `Снимок ${snapshotTime.format(new Date(dataset.evaluated_at))}`,
            `Минимум продаж для Q25 ${formatInteger(dataset.model.min_sales)}`,
        ].join(" · ")
        : item ? "Результаты по выбранному предмету" : "Выберите предмет из каталога";
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
    byId("price-caption").textContent = priceCaption;
    byId("history-price-caption").textContent = priceCaption;
    const priceWarning = history?.priceWarning ?? "";
    for (const id of ["price-warning", "history-price-warning"]) {
        const warning = byId(id);
        warning.textContent = priceWarning;
        warning.hidden = !priceWarning;
    }
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
        appState.selectedItemId = id;
        appState.selectedLotId = null;
        for (const button of byId("item-list").querySelectorAll("[data-item-id]")) {
            button.setAttribute("aria-pressed", String(button.dataset.itemId === id));
        }
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
    updateAnalysisNotice();
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

function toast(message) {
    window.clearTimeout(toastTimer);
    byId("toast-message").textContent = message;
    byId("toast").hidden = false;
    toastTimer = window.setTimeout(() => { byId("toast").hidden = true; }, 4500);
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
    "refresh-analysis": async () => {
        if (appState.analysis.status !== "loading" && Date.now() >= appState.retryAt) {
            try {
                if (await refreshAuction(appState.selectedItemId)) {
                    toast("Обновление поставлено в очередь.");
                    return;
                }
            } catch {
                // Следующий опрос продолжит чтение опубликованных данных.
            }
            toast("Не удалось запросить обновление.");
        }
    },
    "reset-filters": () => saveFilters({ ...DEFAULT_FILTERS }),
    "reset-strategy": () => fillStrategyForm(DEFAULT_STRATEGY),
    "dismiss-toast": () => { byId("toast").hidden = true; },
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
