// @ts-check
// Short messages in the corner (aria-live=polite). Errors stay until dismissed; others fade after a few seconds.
import { h } from "../dom.js";

/** @param {string} message @param {{kind?: "info" | "ok" | "warn" | "bad", ms?: number}} [o] */
export function toast(message, o = {}) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const kind = o.kind ?? "info";
  const el = h("div", { class: `toast ${kind}`, role: kind === "bad" ? "alert" : "status" },
    h("span", {}, message),
    h("button", { type: "button", class: "icon-btn", "aria-label": "Dismiss", onclick: () => el.remove() }, "×"));
  host.append(el);
  while (host.children.length > 4) host.firstElementChild?.remove();
  const ms = o.ms ?? (kind === "bad" ? 0 : 6000);
  if (ms > 0) setTimeout(() => el.remove(), ms);
}
