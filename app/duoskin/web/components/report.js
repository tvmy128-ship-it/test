// @ts-check
// Show any JSON-shaped report (library summary, weekly learning report, calibration counts) as readable text, never as
// raw JSON: objects become labelled lists, lists of objects become tables, numbers get units by their field name.
import { h, humanize, money } from "../dom.js";
import { badge } from "./ui.js";

/** @param {string} key @param {number} v */
function formatNumber(key, v) {
  if (/usd|cost|spent|price/.test(key)) return money(v);
  if (/rate|share|ratio|fraction|pct|percent|approval|coverage/.test(key) && v >= 0 && v <= 1) return `${Math.round(v * 100)}%`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

/**
 * @param {any} value
 * @param {{key?: string, labels?: Record<string, string>, depth?: number}} [o]
 * @returns {Node}
 */
export function renderReport(value, o = {}) {
  const key = o.key ?? "";
  const depth = o.depth ?? 0;
  const labels = o.labels ?? {};
  if (value === null || value === undefined || value === "") return document.createTextNode("—");
  if (typeof value === "boolean") return badge(value ? "Yes" : "No", value ? "ok" : "muted");
  if (typeof value === "number") return document.createTextNode(formatNumber(key, value));
  if (typeof value === "string") return document.createTextNode(/^[a-z0-9]+(_[a-z0-9]+)+$/.test(value) ? humanize(value) : value);
  if (Array.isArray(value)) {
    if (!value.length) return h("span", { class: "muted" }, "None");
    if (value.every((v) => v === null || typeof v !== "object")) return document.createTextNode(value.map((v) => (typeof v === "number" ? formatNumber(key, v) : String(v))).join(", "));
    if (value.every((v) => v && typeof v === "object" && !Array.isArray(v))) {
      const cols = [...new Set(value.flatMap((v) => Object.keys(v)))].filter((c) => value.every((v) => v[c] === null || v[c] === undefined || typeof v[c] !== "object")).slice(0, 8);
      if (cols.length) {
        return h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
          h("thead", {}, h("tr", {}, cols.map((c) => h("th", {}, labels[c] ?? humanize(c))))),
          h("tbody", {}, value.slice(0, 100).map((row) => h("tr", {}, cols.map((c) => h("td", {}, renderReport(row[c], { key: c, labels, depth: depth + 1 }))))))));
      }
    }
    return h("ul", { class: "plain-list" }, value.map((v) => h("li", {}, renderReport(v, { key, labels, depth: depth + 1 }))));
  }
  const entries = Object.entries(value);
  if (!entries.length) return h("span", { class: "muted" }, "Nothing");
  const simple = entries.filter(([, v]) => v === null || typeof v !== "object");
  const nested = entries.filter(([, v]) => v !== null && typeof v === "object");
  return h("div", { class: "report" + (depth ? " nested" : "") },
    simple.length ? h("dl", { class: "kv" }, simple.map(([k, v]) => [h("dt", {}, labels[k] ?? humanize(k)), h("dd", {}, renderReport(v, { key: k, labels, depth: depth + 1 }))])) : null,
    nested.map(([k, v]) => h("section", { class: "report-section" }, h(depth ? "h4" : "h3", {}, labels[k] ?? humanize(k)), renderReport(v, { key: k, labels, depth: depth + 1 }))));
}
