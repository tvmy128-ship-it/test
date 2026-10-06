// @ts-check
// Soft warnings (APP_SPEC 9.9). The server withholds them until the user's first choice on a gate and then releases at most
// two. They are shown as an "Approve anyway?" step on the decision the user just made, and as a calm "Heads-up" list on the
// tiles afterwards. They never block anything and never use alarming colours.
import { h } from "../dom.js";
import { openDialog } from "./modal.js";
import { severityLabel } from "../text.js";

/**
 * @typedef {object} Warning
 * @property {string} id
 * @property {string} [text]
 * @property {string} [message]
 * @property {string | number} [severity]
 * @property {string} [tile_id]
 */

/** @param {Warning} w */
export function warningText(w) { return String(w.text || w.message || "Something here may be worth a second look."); }

/**
 * A "Heads-up" list for a tile. Pass `visible=false` before the gate's first choice: nothing is drawn then.
 * @param {Warning[] | undefined} warnings @param {boolean} visible
 */
export function warningList(warnings, visible) {
  if (!visible || !warnings?.length) return null;
  return h("div", { class: "heads-up", role: "note" },
    h("p", { class: "heads-up-title" }, warnings.length === 1 ? "Heads-up" : "Heads-ups"),
    h("ul", {}, warnings.slice(0, 2).map((w) => h("li", {}, h("span", { class: "badge info" }, severityLabel(w.severity)), " ", warningText(w)))));
}

/**
 * The "Approve anyway?" step. Resolves true to approve anyway, false for "Go back".
 * @param {Warning[]} warnings @param {{what?: string}} [o]
 * @returns {Promise<boolean>}
 */
export function approveAnywayDialog(warnings, o = {}) {
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  const back = h("button", { type: "button", class: "btn", onclick: () => dlg.close(false) }, "Go back");
  const go = h("button", { type: "button", class: "btn primary", onclick: () => dlg.close(true) }, "Approve anyway");
  dlg = openDialog({
    title: "Approve anyway?",
    dismissible: false,
    body: [
      h("p", {}, `Before you approve${o.what ? " " + o.what : ""}, the checks noticed ${warnings.length === 1 ? "one thing" : "two things"}. These are only suggestions: you can approve anyway, or go back and change it.`),
      h("ul", { class: "warning-dialog-list" }, warnings.map((w) => h("li", {}, h("strong", {}, severityLabel(w.severity) + ": "), warningText(w)))),
      h("p", { class: "muted" }, "If you approve anyway, your choice helps DuoSkin learn which suggestions are useful. Nothing is charged by this step."),
    ],
    actions: [back, go],
  });
  go.focus();
  return dlg.closed.then((v) => v === true);
}
