// @ts-check
// Dialogs on the native <dialog> element: focus is trapped, Escape closes, and the page behind is inert.
import { h } from "../dom.js";

/**
 * @typedef {object} DialogHandle
 * @property {HTMLDialogElement} el
 * @property {HTMLElement} body
 * @property {HTMLElement} footer
 * @property {(result?: any) => void} close
 * @property {Promise<any>} closed resolves with the value given to close() (undefined when dismissed)
 */

/**
 * @param {{title: string, body?: import("../dom.js").Children, actions?: import("../dom.js").Children, wide?: boolean, drawer?: boolean, labelledBy?: string, dismissible?: boolean}} o
 * @returns {DialogHandle}
 */
export function openDialog(o) {
  const dialog = h("dialog", { class: "dialog" + (o.wide ? " wide" : "") + (o.drawer ? " drawer" : ""), "aria-labelledby": "dlg-title-" + Math.random().toString(36).slice(2, 8) });
  const titleId = dialog.getAttribute("aria-labelledby") || "";
  const body = h("div", { class: "dialog-body" }, o.body);
  const footer = h("div", { class: "dialog-foot" }, o.actions);
  /** @type {(v: any) => void} */
  let resolve = () => {};
  const closed = new Promise((r) => { resolve = r; });
  let result;
  /** @param {any} [value] */
  const close = (value) => { result = value; if (dialog.open) dialog.close(); else dialog.remove(); };
  const closeBtn = h("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: () => close(undefined) }, "×");
  dialog.append(h("div", { class: "dialog-head" }, h("h2", { id: titleId }, o.title), o.dismissible === false ? null : closeBtn), body, footer);
  if (!o.actions) footer.remove();
  dialog.addEventListener("close", () => { dialog.remove(); resolve(result); });
  dialog.addEventListener("cancel", (e) => { if (o.dismissible === false) e.preventDefault(); });
  // a click on the backdrop (outside the dialog box) closes it
  dialog.addEventListener("click", (e) => {
    if (o.dismissible === false) return;
    if (e.target === dialog) close(undefined);
  });
  document.body.append(dialog);
  dialog.showModal();
  return { el: dialog, body, footer, close, closed };
}

/**
 * A yes/no question. Resolves true when the user confirms.
 * @param {{title: string, message?: import("../dom.js").Children, confirmLabel?: string, cancelLabel?: string, tone?: "danger" | "primary"}} o
 */
export function confirmDialog(o) {
  /** @type {DialogHandle} */
  let dlg;
  const yes = h("button", { type: "button", class: "btn " + (o.tone === "danger" ? "danger" : "primary"), onclick: () => dlg.close(true) }, o.confirmLabel ?? "Yes");
  const no = h("button", { type: "button", class: "btn", onclick: () => dlg.close(false) }, o.cancelLabel ?? "Cancel");
  dlg = openDialog({ title: o.title, body: typeof o.message === "string" ? h("p", {}, o.message) : o.message, actions: [no, yes] });
  (o.tone === "danger" ? no : yes).focus();
  return dlg.closed.then((v) => v === true);
}
