import { emptyContent, formatInteger, formatMoney, packLabel } from "./ui.js";

const PARTIAL_INTERVAL = "Интервал покрыт не полностью";

function axisScale(values, isPrice) {
    const max = Math.max(...values);
    const min = isPrice ? Math.min(...values) : 0;
    const range = Math.max(max - min, Math.abs(max) * Number.EPSILON * 16, 1);
    const padding = isPrice ? range * 0.07 : 0;
    const stepTarget = (range + 2 * padding) / 5;
    const magnitude = 10 ** Math.floor(Math.log10(stepTarget));
    const factor = [1, 2, 5, 10].find((value) => value * magnitude >= stepTarget);
    const step = Math.max(1, factor * magnitude);
    const start = Math.max(0, Math.floor((min - padding) / step) * step);
    const end = Math.max(start + step, Math.ceil((max + padding) / step) * step);
    const ticks = Array.from(
        { length: Math.round((end - start) / step) + 1 },
        (_, index) => start + index * step,
    );
    return { start, end, ticks };
}

function linePath(points) {
    let path = `M ${points[0].x} ${points[0].y}`;
    for (let index = 1; index < points.length; index += 1) {
        const previous = points[index - 1];
        const point = points[index];
        const midX = (previous.x + point.x) / 2;
        path += ` C ${midX} ${previous.y}, ${midX} ${point.y}, ${point.x} ${point.y}`;
    }
    return path;
}

function formatMedianPrice(value) {
    const [whole, fraction] = value.split(".");
    const decimal = fraction === undefined ? "" : `,${fraction}`;
    return `${formatInteger(BigInt(whole))}${decimal} ₽`;
}

export function createChart(plot, kind) {
    const isPrice = kind === "price";
    const gradient = `${plot.id}-fill`;
    let history;
    let quantity;
    let points = [];
    let width = 0;
    let height = 0;

    function render() {
        width = plot.clientWidth;
        height = plot.clientHeight;
        if (!width || !height) {
            return;
        }
        const values = isPrice ? history?.prices : history?.sales;
        if (!values?.length) {
            points = [];
            const explanation = isPrice ? history?.priceEmpty : history?.salesEmpty;
            plot.innerHTML = `<div class="empty">${emptyContent(
                "Нет данных",
                explanation ?? "История продаж пока недоступна.",
            )}</div>`;
            return;
        }

        const observed = values.filter((value) => value !== null).map(Number);
        const left = 77;
        const right = Math.max(left + 1, width - 28);
        const top = 8;
        const bottom = height - 24;
        const interval = (right - left) / values.length;
        const { start, end, ticks } = observed.length
            ? axisScale(observed, isPrice) : { start: 0, end: 1, ticks: [] };
        const yAt = (value) => bottom - (value - start) / (end - start) * (bottom - top);
        points = values.map((value, index) => {
            const covered = history.coveredIntervals[index];
            let description;
            if (value === null) {
                description = covered ? "Нет сопоставимых продаж" : PARTIAL_INTERVAL;
                if (!isPrice && !covered && history.priceCounts[index]) {
                    description += ` · Наблюдаемых продаж: ${formatInteger(history.priceCounts[index])}`;
                }
            } else if (isPrice) {
                description = `Медианная цена: ${formatMedianPrice(value)}`;
            } else {
                description = `${formatInteger(value)} · продано партий`;
            }
            return {
                x: left + (index + 0.5) * interval,
                y: value === null ? null : yAt(Number(value)),
                tooltipLabel: history.priceIntervals[index],
                priceCount: isPrice ? history.priceCounts[index] : 0,
                partial: isPrice && value !== null && !covered,
                value: description,
            };
        });

        const gridY = ticks.map((value) => {
            const y = yAt(value);
            const label = isPrice ? formatMoney(value) : formatInteger(value);
            return `
                <line class="chart-gridline" x1="${left}" x2="${right}" y1="${y}" y2="${y}"/>
                <text class="chart-label" text-anchor="end" x="${left - 9}" y="${y + 4}">${label}</text>`;
        }).join("");

        const labelStep = Math.max(1, Math.ceil(55 / interval));
        const lastIndex = values.length;
        const gridX = history.boundaryLabels.map((label, index) => {
            if (index !== lastIndex && (index % labelStep !== 0
                || (index > 0 && lastIndex - index < labelStep))) return "";
            const x = left + index * interval;
            return `
                <line class="chart-gridline" x1="${x}" x2="${x}" y1="${top}" y2="${bottom}"/>
                <text class="chart-label" text-anchor="middle" x="${x}" y="${height - 7}">${label}</text>`;
        }).join("");

        const color = isPrice ? "#ffc344" : "#79cf49";
        const fill = `url(#${gradient})`;
        const plotArea = isPrice ? drawPrice(points, fill, bottom) : drawBars(points, fill, interval, bottom);
        const hits = points.map((point, index) => {
            const x = Math.max(left, point.x - interval / 2);
            const hitWidth = Math.min(right, point.x + interval / 2) - x;
            const count = point.priceCount ? `, продано партий: ${formatInteger(point.priceCount)}` : "";
            const partial = point.partial ? `, ${PARTIAL_INTERVAL}` : "";
            return `
                <rect class="chart-hit" x="${x}" y="${top}" width="${hitWidth}" height="${bottom - top}"
                      tabindex="${index === 0 ? 0 : -1}" data-point="${index}" role="img"
                      aria-label="${point.tooltipLabel}, ${point.value}${count}${partial}"/>`;
        }).join("");

        const title = isPrice ? "Медианная цена продажи партии" : "Количество проданных партий";
        plot.innerHTML = `
                <svg class="chart-svg" viewBox="0 0 ${width} ${height}"
                 role="group" aria-label="${title} по ${packLabel(quantity)}">
                <defs><linearGradient id="${gradient}" x2="0" y2="1">
                    <stop stop-color="${color}" stop-opacity="${isPrice ? 0.29 : 0.86}"/>
                    <stop offset="100%" stop-color="${color}" stop-opacity="${isPrice ? 0 : 0.25}"/>
                </linearGradient></defs>
                <g aria-hidden="true">${gridY}${gridX}${plotArea}
                    ${observed.length ? "" : `<text class="chart-label" text-anchor="middle"
                        x="${(left + right) / 2}" y="${(top + bottom) / 2}">Нет данных</text>`}
                </g>
                <g class="chart-highlight" aria-hidden="true" visibility="hidden">
                    <line class="chart-guide" y1="${top}" y2="${bottom}"/>
                    ${isPrice ? '<circle class="chart-marker" r="4"/>' : ""}
                </g>
                ${hits}
            </svg>
            <div class="chart-tooltip" aria-hidden="true" hidden></div>`;
    }

    function showPoint(index) {
        const point = points[index];
        const tooltip = plot.querySelector(".chart-tooltip");
        const highlight = plot.querySelector(".chart-highlight");
        highlight.setAttribute("transform", `translate(${point.x} 0)`);
        highlight.setAttribute("visibility", "visible");
        const marker = highlight.querySelector("circle");
        if (marker) {
            marker.setAttribute("visibility", point.y === null ? "hidden" : "visible");
            if (point.y !== null) {
                marker.setAttribute("cy", point.y);
            }
        }
        tooltip.innerHTML = `<span>${point.tooltipLabel}</span><strong>${point.value}</strong>`;
        if (point.priceCount) {
            tooltip.innerHTML += `
                <span>Продано партий: ${formatInteger(point.priceCount)}</span>
                <span>Размер партии: ${packLabel(quantity)}</span>`;
        }
        if (point.partial) {
            tooltip.innerHTML += `<span>${PARTIAL_INTERVAL}</span>`;
        }
        tooltip.hidden = false;
        const halfWidth = tooltip.offsetWidth / 2;
        tooltip.style.left = `${Math.max(halfWidth, Math.min(width - halfWidth, point.x))}px`;
    }

    function hidePoint() {
        const tooltip = plot.querySelector(".chart-tooltip");
        if (tooltip) {
            tooltip.hidden = true;
            plot.querySelector(".chart-highlight").setAttribute("visibility", "hidden");
        }
    }

    function showFromEvent(event) {
        if (event.target.hasAttribute("data-point")) {
            showPoint(Number(event.target.dataset.point));
        } else {
            hidePoint();
        }
    }

    plot.addEventListener("pointerover", showFromEvent);
    plot.addEventListener("pointerleave", hidePoint);
    plot.addEventListener("focusin", showFromEvent);
    plot.addEventListener("focusout", hidePoint);
    plot.addEventListener("keydown", (event) => {
        const direction = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
        if (!direction || !event.target.hasAttribute("data-point")) {
            return;
        }
        event.preventDefault();
        const next = Number(event.target.dataset.point) + direction;
        const target = plot.querySelector(`[data-point="${next}"]`);
        if (target) {
            event.target.setAttribute("tabindex", "-1");
            target.setAttribute("tabindex", "0");
            target.focus();
        }
    });
    new ResizeObserver(() => {
        if (quantity !== undefined && (plot.clientWidth !== width || plot.clientHeight !== height)) {
            render();
        }
    }).observe(plot);

    return (nextHistory, nextQuantity) => {
        if (history !== nextHistory || quantity !== nextQuantity) {
            history = nextHistory;
            quantity = nextQuantity;
            render();
        }
    };
}

function drawPrice(points, fill, bottom) {
    const runs = [];
    let run = [];
    for (const point of points) {
        if (point.y === null) {
            if (run.length) {
                runs.push(run);
                run = [];
            }
        } else {
            run.push(point);
        }
    }
    if (run.length) {
        runs.push(run);
    }
    return runs.map((series) => {
        if (series.length === 1) {
            const { x, y } = series[0];
            return `<circle class="chart-marker" cx="${x}" cy="${y}" r="4"/>`;
        }
        const curve = linePath(series);
        return `
            <path d="${curve} L ${series.at(-1).x} ${bottom} H ${series[0].x} Z" fill="${fill}"/>
            <path d="${curve}" class="chart-curve"/>`;
    }).join("");
}

function drawBars(points, fill, interval, bottom) {
    const barWidth = Math.min(44, interval * 0.65);
    return points.filter(({ y }) => y !== null).map(({ x, y }) => `
        <rect x="${x - barWidth / 2}" y="${y}" width="${barWidth}"
              height="${bottom - y}" rx="1" fill="${fill}"/>`
    ).join("");
}
