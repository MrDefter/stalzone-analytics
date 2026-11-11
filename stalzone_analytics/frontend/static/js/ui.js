import { isEstimatedAnalysis, reasonLabel, statusLabel } from "./auction-data.js";

const integerFormat = new Intl.NumberFormat("ru-RU");
const decimalFormat = new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
});
const percentFormat = new Intl.NumberFormat("ru-RU", {
    style: "percent",
    maximumFractionDigits: 2,
});

export const formatInteger = (value) => value == null ? "—" : integerFormat.format(value);

export function formatMoney(value, signed = false) {
    if (value == null) {
        return "—";
    }
    const amount = typeof value === "string" ? BigInt(value) : value;
    return `${withSign(integerFormat, amount, signed)} ₽`;
}

function formatUnitMoney(total, quantity) {
    if (total == null || quantity == null) {
        return "—";
    }
    const numerator = BigInt(total);
    const denominator = BigInt(quantity);
    if (numerator % denominator === 0n) {
        return formatMoney(numerator / denominator);
    }
    const negative = numerator < 0n;
    const hundredths = (negative ? -numerator : numerator) * 100n;
    const rounded = (2n * hundredths + denominator) / (2n * denominator);
    const approximation = hundredths % denominator === 0n ? "" : "≈ ";
    const sign = negative ? "−" : "";
    const fraction = String(rounded % 100n).padStart(2, "0");
    return `${approximation}${sign}${formatInteger(rounded / 100n)},${fraction} ₽`;
}

function formatRoi(value, signed = true) {
    return value == null ? "—" : `${withSign(decimalFormat, value, signed)}%`;
}

function withSign(format, value, signed) {
    if (value < 0) {
        return `−${format.format(-value)}`;
    }
    const prefix = signed && value > 0 ? "+" : "";
    return prefix + format.format(value);
}

function valueTone(value) {
    if (value < 0) {
        return "negative";
    }
    return value > 0 ? "positive" : "";
}

export function icon(name) {
    return `<svg class="icon" aria-hidden="true" focusable="false">
        <use href="/static/images/icons.svg#${name}"></use>
    </svg>`;
}

const ITEM_PLACEHOLDER = "/static/images/item-placeholder.svg";

function itemImage(item) {
    const image = document.createElement("img");
    image.className = "item-art";
    image.alt = "";
    image.decoding = "async";
    image.addEventListener("error", () => {
        image.src = ITEM_PLACEHOLDER;
    }, { once: true });
    const url = item.image_url;
    image.src = url?.startsWith("/item-images/") ? url : ITEM_PLACEHOLDER;
    return image;
}

function itemCopy(item) {
    const copy = document.createElement("span");
    copy.className = "item-copy";
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.name_ru;
    const id = document.createElement("span");
    id.className = "item-category";
    id.textContent = `ID: ${item.item_id}`;
    copy.append(name, id);
    return copy;
}

export function emptyContent(title, description = "") {
    return `${icon("inbox")}<strong>${title}</strong>${description ? `<span>${description}</span>` : ""}`;
}

export function packLabel(quantity) {
    return `${formatInteger(quantity)} шт.`;
}

export function renderItemList(container, items, selectedId, emptyState) {
    container.setAttribute("aria-busy", String(emptyState === "loading"));
    if (!items.length) {
        const messages = {
            loading: ["Загрузка каталога…"],
            error: ["Не удалось загрузить каталог"],
            empty: ["В каталоге пока нет предметов", "Добавьте предметы в items.json."],
            unmatched: ["Предметы не найдены", "Попробуйте изменить запрос."],
        };
        const retry = emptyState === "error"
            ? '<button class="button" type="button" data-action="retry-items">Повторить</button>'
            : "";
        container.innerHTML = `<div class="empty">${emptyContent(...messages[emptyState])}${retry}</div>`;
        return;
    }
    container.replaceChildren(...items.map((item) => {
        const option = document.createElement("button");
        option.className = "item-option";
        option.type = "button";
        option.dataset.itemId = item.item_id;
        option.setAttribute("aria-pressed", String(item.item_id === selectedId));
        option.append(itemImage(item), itemCopy(item));
        return option;
    }));
}

function formatPriceWindow(hours) {
    const labels = { 3: "3 часа", 24: "24 часа", 168: "7 дней", 336: "14 дней" };
    return labels[hours] ?? "—";
}

const PRELIMINARY_Q25_LABEL = "Предварительная оценка";

function q25Display(dataset) {
    const preliminary = dataset?.q25_quality === "preliminary";
    const estimate = preliminary || dataset?.q25_quality === "verified" ? dataset : null;
    return {
        preliminary,
        price: formatMoney(estimate?.q25_per_pack),
        window: formatPriceWindow(estimate?.model.price_window_hours),
        sampleCount: formatInteger(estimate?.sample_count),
    };
}

function turnoverLabel(dataset) {
    return dataset?.coverage.history_24h_complete === false
        ? "Наблюдаемые продажи партий за 24 часа" : "Продаж партий за 24 часа";
}

export function renderMetrics(container, dataset) {
    const q25 = q25Display(dataset);
    const estimated = isEstimatedAnalysis(dataset);
    const lots = dataset?.source_errors?.lots ? null : dataset?.lots;
    const candidateCount = (q25.preliminary || dataset?.q25_quality === "verified")
        && dataset.reasons.every((reason) => reason === "sharp_price_drop")
        ? lots?.filter((lot) => lot.status === "candidate" && lot.roi_percent != null).length
        : null;
    const cards = [
        {
            iconName: "count",
            label: dataset && !dataset.coverage.lots_complete ? "Лотов в снимке" : "Активных лотов",
            value: formatInteger(lots?.length),
            help: "Число активных предложений в загруженных данных.",
        },
        {
            iconName: "layers",
            label: estimated ? "Оценочно выгодных" : "Потенциально выгодных",
            value: formatInteger(candidateCount),
            help: "Лоты с рассчитанным ROI, прошедшие проверки модели.",
        },
        {
            iconName: "tag",
            label: "Ориентир Q25 за партию",
            value: q25.price,
            note: q25.preliminary ? PRELIMINARY_Q25_LABEL : "",
            help: "Нижний квартиль полной цены стандартной партии в выбранном окне.",
        },
        {
            iconName: "trend",
            label: "Окно оценки Q25",
            value: q25.window,
            help: q25.preliminary ? "Кратчайший период с достаточным числом загруженных продаж."
                : "Кратчайший подтверждённый период с достаточным числом продаж.",
        },
        {
            iconName: "count",
            label: "Продаж партий в Q25",
            value: q25.sampleCount,
            note: q25.preliminary ? "Наблюдения в выборке" : "",
            help: "Число сопоставимых продаж, использованных в Q25.",
        },
        {
            iconName: "bars",
            label: turnoverLabel(dataset),
            value: formatInteger(dataset?.turnover_24h),
            help: "Число завершённых продаж стандартных партий за последние 24 часа.",
        },
    ];
    container.innerHTML = cards.map((card, index) => `
        <div class="metric" tabindex="0" aria-describedby="metric-help-${index}">
            ${icon(card.iconName)}
            <dl>
                <dt>${card.label}</dt>
                <dd>${card.value}${card.note ? `<small class="metric-note">${card.note}</small>` : ""}</dd>
            </dl>
            <span class="metric-tooltip" role="tooltip" id="metric-help-${index}">
                ${card.help}
            </span>
        </div>
    `).join("");
}

export function renderLotRows(container, item, lots, selectedId, estimated = false) {
    container.innerHTML = lots.map((lot) => {
        const selected = lot.id === selectedId;
        return `
            <tr data-lot-id="${lot.id}" class="${selected ? "is-selected" : ""}">
                <td><button class="lot-link" type="button" aria-pressed="${selected}">
                    Лот #${lot.id}
                </button></td>
                <td>${formatMoney(lot.buyout_total)}</td>
                <td>${formatMoney(lot.exit_price_per_pack)}</td>
                <td>${formatMoney(lot.commission_total)}</td>
                <td>${formatInteger(lot.amount)}</td>
                <td class="lot-value ${valueTone(lot.profit_total)}">${formatMoney(lot.profit_total, true)}</td>
                <td class="lot-value ${valueTone(lot.roi_percent)}">${formatRoi(lot.roi_percent)}</td>
                <td><span class="lot-status ${lot.status === "candidate" && !estimated ? "is-candidate" : ""}">
                    <span class="status-dot" aria-hidden="true"></span>${statusLabel(lot.status, estimated)}
                </span></td>
            </tr>`;
    }).join("");
    for (const button of container.querySelectorAll(".lot-link")) {
        button.prepend(itemImage(item));
    }
}

export function highlightLot(container, selectedId) {
    for (const row of container.rows) {
        const selected = Number(row.dataset.lotId) === selectedId;
        row.classList.toggle("is-selected", selected);
        row.querySelector(".lot-link").setAttribute("aria-pressed", String(selected));
    }
}

function detailRow(label, value, tone = "") {
    return `<div><dt>${label}</dt><dd class="${tone}">${value}</dd></div>`;
}

export function renderDetails(container, item, dataset, lot) {
    if (!item) {
        container.innerHTML = `<div class="empty">${emptyContent("Предмет не выбран")}</div>`;
        return;
    }
    const identity = document.createElement("div");
    identity.className = "lot-identity";
    identity.append(itemImage(item), itemCopy(item));
    container.replaceChildren(identity);
    if (lot) {
        const number = document.createElement("span");
        number.className = "lot-number";
        number.textContent = `#${lot.id}`;
        identity.append(number);
    }
    if (!lot) {
        const message = dataset?.lots.length ? "Измените фильтры, чтобы увидеть лоты." : "";
        const title = dataset ? "Нет выбранного лота" : "Нет данных о лотах";
        container.insertAdjacentHTML("beforeend", `<div class="empty">${emptyContent(title, message)}</div>`);
        return;
    }
    const commissionLabel = dataset.model.commission_estimated
        ? "Доля комиссии (оценка)" : "Доля комиссии";
    const quantity = dataset.sell_quantity;
    const needsCompletion = lot.profit_total != null
        && quantity != null && lot.amount % quantity !== 0;
    const competition = lot.competition_to_turnover == null
        ? "—" : percentFormat.format(lot.competition_to_turnover);
    const priceChange = dataset.price_change_percent;
    const q25 = q25Display(dataset);
    const estimated = isEstimatedAnalysis(dataset);
    const risks = lot.reasons.map(reasonLabel);
    if (priceChange == null) {
        risks.push("Изменение цены не проверено");
    }
    container.insertAdjacentHTML("beforeend", `
        <section class="lot-detail-section">
            <h3>Покупка</h3>
            <dl class="detail-list">
                ${detailRow("Статус", statusLabel(lot.status, estimated))}
                ${detailRow("Цена выкупа за лот", formatMoney(lot.buyout_total))}
                ${detailRow("Предметов в лоте", formatInteger(lot.amount))}
                ${detailRow("Цена покупки за предмет", formatUnitMoney(lot.buyout_total, lot.amount))}
            </dl>
        </section>
        <section class="lot-detail-section">
            <h3>Рыночная оценка</h3>
            ${q25.preliminary ? `<p class="notice q25-notice">${icon("info")}<span>${PRELIMINARY_Q25_LABEL}</span></p>` : ""}
            <dl class="detail-list">
                ${detailRow("Окно Q25", q25.window)}
                ${detailRow("Продаж партий в Q25", q25.sampleCount)}
                ${detailRow("Размер стандартной партии", quantity == null ? "—" : packLabel(quantity))}
                ${detailRow("Q25 за партию", q25.price)}
                ${detailRow("Q25 за предмет", formatUnitMoney(dataset.q25_per_pack, quantity))}
                ${detailRow("Цена выхода за партию", formatMoney(lot.exit_price_per_pack))}
                ${detailRow("Чистая стоимость за предмет", formatUnitMoney(lot.net_revenue_per_pack, quantity))}
                ${detailRow("Конкурирующих партий в снимке", formatInteger(lot.active_competing_packs))}
                ${detailRow(turnoverLabel(dataset), formatInteger(dataset.turnover_24h))}
                ${detailRow("Предложение к обороту", competition)}
            </dl>
        </section>
        <section class="lot-detail-section">
            <h3>Финансы</h3>
            <dl class="detail-list">
                ${detailRow("Валовая доля выручки", formatMoney(lot.gross_revenue_total))}
                ${detailRow(commissionLabel, formatMoney(lot.commission_total))}
                ${detailRow("Доля расходов на выставление", formatMoney(lot.listing_cost_total))}
                ${detailRow("Чистая расчётная выручка", formatMoney(lot.net_revenue_total))}
                ${detailRow("Расчётный предел цены покупки", formatMoney(lot.max_buyout))}
            </dl>
            <dl class="detail-list detail-totals">
                ${detailRow("Потенциальная прибыль", formatMoney(lot.profit_total, true), valueTone(lot.profit_total))}
                ${detailRow("ROI", formatRoi(lot.roi_percent), valueTone(lot.roi_percent))}
            </dl>
            ${needsCompletion ? `<p class="notice">${icon("info")}<span>Прибыль рассчитана пропорционально будущей продаже стандартных партий. Для реализации потребуется дополнить количество предметов.</span></p>` : ""}
        </section>
        <section class="lot-detail-section">
            <h3>Риски</h3>
            ${priceChange == null ? "" : `
                <dl class="detail-list">
                    ${detailRow("Изменение Q25", formatRoi(priceChange), valueTone(priceChange))}
                </dl>`}
            ${risks.length ? `<ul class="lot-risks">${risks.map((risk) => `<li>${risk}</li>`).join("")}</ul>` : ""}
        </section>`);
}
