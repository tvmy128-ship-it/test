// @ts-check
// Tiny DOM helpers shared by every page and component. No framework: elements are built with h() and replaced in place.
// CSP note: the page forbids inline style="" attributes, so h() never writes one; use classes, or el.style.x (CSSOM).

/** @typedef {Node | string | number | boolean | null | undefined} Child */
/** @typedef {Child | Child[]} Children */

/**
 * Create an element. `props`: `class`, `dataset`, `on<event>` handlers, `text`, `aria-*`, plain attributes; a value of
 * `true` sets a boolean attribute, `false`/`null`/`undefined` skips it. Children may be nodes, strings, numbers or
 * (nested) arrays; `null`, `undefined` and `false` are ignored.
 * @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag
 * @param {Record<string, any> | null} [props]
 * @param {...Children} children
 * @returns {HTMLElementTagNameMap[K]}
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === false || value == null) continue;
    if (key === "class") el.className = String(value);
    else if (key === "text") el.textContent = String(value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "value" || key === "checked" || key === "selected" || key === "disabled" || key === "indeterminate") {
      /** @type {any} */ (el)[key] = value;
      if (key === "disabled" && value) el.setAttribute("disabled", "");
    } else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

/** @param {Node} el @param {Children} children */
export function append(el, children) {
  for (const child of Array.isArray(children) ? children : [children]) {
    if (Array.isArray(child)) append(el, child);
    else if (child == null || child === false || child === true) continue;
    else el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Replace all children of `el`. @param {Element} el @param {...Children} children */
export function setChildren(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

/** @param {Element} el */
export function clear(el) { el.replaceChildren(); return el; }

/** @param {string} sel @param {ParentNode} [root] @returns {HTMLElement | null} */
export const $ = (sel, root = document) => /** @type {HTMLElement | null} */ (root.querySelector(sel));

/** @param {number | null | undefined} n @param {number} [digits] */
export function money(n, digits = 2) { return "$" + Number(n || 0).toFixed(digits); }

/** "Boy and girl" style wording for a combo code such as "bg". @param {string} combo */
export function comboLabel(combo) {
  const w = { b: "Boy", g: "Girl" };
  const [a, b] = combo.split("");
  return `${w[/** @type {'b'|'g'} */ (a)] || "?"} + ${w[/** @type {'b'|'g'} */ (b)] || "?"}`;
}

/** @param {string} s */
export function humanize(s) {
  const t = String(s ?? "").replace(/[_\-.]+/g, " ").replace(/\s+/g, " ").trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "";
}

/** @param {number} n @param {string} one @param {string} [many] */
export function plural(n, one, many = one + "s") { return `${n} ${n === 1 ? one : many}`; }

/** "3 minutes ago" for an ISO timestamp. @param {string | null | undefined} iso */
export function timeAgo(iso) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 45) return "just now";
  if (s < 3600) return plural(Math.round(s / 60), "minute") + " ago";
  if (s < 86400) return plural(Math.round(s / 3600), "hour") + " ago";
  return plural(Math.round(s / 86400), "day") + " ago";
}

/** @param {string | null | undefined} iso */
export function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

/** @param {number} bytes */
export function fmtBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

/** @template {(...a: any[]) => void} F @param {F} fn @param {number} ms @returns {F & {cancel(): void}} */
export function debounce(fn, ms) {
  /** @type {any} */ let timer = null;
  const wrapped = /** @type {any} */ ((/** @type {any[]} */ ...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  });
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

let idCounter = 0;
/** A unique id for label/aria wiring. @param {string} [prefix] */
export function uid(prefix = "id") { idCounter += 1; return `${prefix}-${idCounter}`; }

/** A random id for idempotency keys (never security relevant). */
export function randomId() {
  try { return crypto.randomUUID(); } catch { return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`; }
}

/** Read a localStorage value; storage may be blocked, so every access is guarded. @param {string} key @param {string} [fallback] */
export function remembered(key, fallback = "") {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
/** @param {string} key @param {string} value */
export function remember(key, value) {
  try { localStorage.setItem(key, value); } catch { /* private window: the choice just is not remembered */ }
}
