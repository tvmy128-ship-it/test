// @ts-check
// Small reusable pieces: page header, panel, badge, progress bar, tabs, switch, "not available yet" and empty states.
import { h, uid } from "../dom.js";

/**
 * @param {{title: string, lead?: string, actions?: import("../dom.js").Children, back?: {href: string, label: string}}} o
 */
export function pageHeader(o) {
  return h("header", { class: "page-head" },
    o.back ? h("a", { class: "back-link", href: o.back.href }, "‹ " + o.back.label) : null,
    h("div", { class: "page-head-row" },
      h("div", {}, h("h1", { tabindex: "-1" }, o.title), o.lead ? h("p", { class: "lead" }, o.lead) : null),
      o.actions ? h("div", { class: "page-actions" }, o.actions) : null));
}

/** @param {{class?: string, title?: string, titleLevel?: 2 | 3, lead?: string, actions?: import("../dom.js").Children, id?: string}} o @param {...import("../dom.js").Children} body */
export function panel(o, ...body) {
  const level = /** @type {"h2" | "h3"} */ (o.titleLevel === 3 ? "h3" : "h2");
  return h("section", { class: "panel " + (o.class || ""), id: o.id },
    o.title || o.actions ? h("div", { class: "panel-head" }, o.title ? h(level, {}, o.title) : null, o.actions ? h("div", { class: "panel-actions" }, o.actions) : null) : null,
    o.lead ? h("p", { class: "muted" }, o.lead) : null,
    body);
}

/** A text badge (colour is never the only signal). @param {string} text @param {"ok" | "warn" | "bad" | "info" | "busy" | "muted" | "wild"} [tone] */
export function badge(text, tone = "info") { return h("span", { class: `badge ${tone}` }, text); }

/** @param {number} fraction 0..1 @param {string} [label] */
export function progress(fraction, label = "") {
  const pct = Math.max(0, Math.min(1, Number.isFinite(fraction) ? fraction : 0));
  const bar = h("div", { class: "progress-bar" });
  bar.style.setProperty("--p", `${Math.round(pct * 100)}%`);
  return h("div", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(pct * 100)), "aria-label": label || "Progress" }, bar);
}

export function spinner() { return h("span", { class: "spinner", "aria-hidden": "true" }); }

/** A friendly "this part is not built yet" state (a 404 or 501 from the API). @param {string} what @param {string} [detail] */
export function notAvailable(what, detail = "") {
  return h("div", { class: "empty not-available", role: "status" },
    h("h3", {}, "Not available yet"),
    h("p", {}, `${what} is not part of this build yet.`),
    detail ? h("p", { class: "muted" }, detail) : null);
}

/** @param {string} title @param {string} [text] @param {import("../dom.js").Children} [action] */
export function emptyState(title, text = "", action = null) {
  return h("div", { class: "empty" }, h("h3", {}, title), text ? h("p", { class: "muted" }, text) : null, action);
}

/** An error line with role=alert. @param {string} text */
export function errorNote(text) { return h("p", { class: "note bad", role: "alert" }, text); }

/** @param {string} text @param {"info" | "warn" | "ok" | "bad"} [tone] */
export function note(text, tone = "info") { return h("p", { class: `note ${tone}`, role: tone === "bad" ? "alert" : "status" }, text); }

/**
 * A key/value list. @param {[string, import("../dom.js").Children][]} rows
 */
export function keyValues(rows) {
  return h("dl", { class: "kv" }, rows.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

/**
 * An on/off switch built from a real checkbox (keyboard and screen-reader friendly).
 * @param {{label: string, hint?: string, checked?: boolean, onChange?: (v: boolean) => void, name?: string, disabled?: boolean}} o
 */
export function switchField(o) {
  const id = uid("sw");
  const input = h("input", { type: "checkbox", id, name: o.name, checked: !!o.checked, disabled: o.disabled, role: "switch" });
  input.addEventListener("change", () => o.onChange?.(input.checked));
  const el = h("div", { class: "switch-field" }, input,
    h("label", { for: id }, h("span", { class: "switch-label" }, o.label), o.hint ? h("span", { class: "hint" }, o.hint) : null));
  return Object.assign(el, { input });
}

/**
 * Tabs with arrow-key navigation. Returns the element; `select(id)` switches programmatically.
 * @param {{id: string, label: string}[]} tabs @param {string} active @param {(id: string) => void} onSelect @param {string} [label]
 */
export function tabList(tabs, active, onSelect, label = "Sections") {
  const list = h("div", { class: "tabs", role: "tablist", "aria-label": label });
  /** @type {HTMLButtonElement[]} */
  const buttons = [];
  /** @param {string} id */
  const select = (id) => {
    buttons.forEach((b) => {
      const on = b.dataset.tab === id;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    onSelect(id);
  };
  tabs.forEach((t, i) => {
    const b = h("button", { type: "button", class: "tab", role: "tab", dataset: { tab: t.id }, id: `tab-${t.id}`, "aria-selected": String(t.id === active), tabindex: t.id === active ? "0" : "-1",
      onclick: () => select(t.id),
      onkeydown: (/** @type {KeyboardEvent} */ e) => {
        let next = -1;
        if (e.key === "ArrowRight") next = (i + 1) % tabs.length;
        else if (e.key === "ArrowLeft") next = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") next = 0;
        else if (e.key === "End") next = tabs.length - 1;
        if (next >= 0) { e.preventDefault(); buttons[next].focus(); select(tabs[next].id); }
      } }, t.label);
    buttons.push(b);
    list.append(b);
  });
  return Object.assign(list, { select });
}

/** A labelled form row. @param {string} label @param {HTMLElement} control @param {string} [hint] */
export function field(label, control, hint = "") {
  if (!control.id) control.id = uid("f");
  const hintId = hint ? `${control.id}-hint` : "";
  if (hintId) control.setAttribute("aria-describedby", hintId);
  return h("div", { class: "field" }, h("label", { for: control.id }, label), control, hint ? h("p", { class: "hint", id: hintId }, hint) : null);
}

/** Disable a button while an async action runs; re-enable after. @param {HTMLButtonElement} btn @param {() => Promise<any>} fn */
export async function busy(btn, fn) {
  const text = btn.textContent;
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  try { return await fn(); } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); btn.textContent = text; }
}
