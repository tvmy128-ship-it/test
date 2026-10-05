// @ts-check
// After "Approve" at Gate 1 (APP_SPEC 9.2): when a colour taken from the approved picture moved more than ΔE 10 from the planned
// colour, a small dialog asks which one to use. The default is the colour from the picture, because that is what the user
// approved (CHK-G1-08). Looks for a follow-up gate of the project whose tile carries `facts.palette_confirm`
// ([{id, name, planned_hex, picture_hex, delta_e}]) and answers it with choice "picture" or "planned".
import { get } from "../api.js";
import { h, setChildren } from "../dom.js";
import { openDialog } from "./modal.js";
import { decide } from "./decisions.js";

const HEX = /^#[0-9a-f]{6}$/i;

/** @param {string} hex */
function chip(hex) {
  const el = h("span", { class: "chip", "aria-hidden": "true" });
  if (HEX.test(hex)) el.style.background = hex;
  return el;
}

/** Gate ids already shown, so one dialog is never opened twice. */
const shown = new Set();

/**
 * Look once for an open palette-confirm gate of this project and ask about it. Called when a gate opens (app.js).
 * @param {string} projectId
 * @returns {Promise<"answered" | "none">}
 */
export async function maybeConfirmPalette(projectId) {
  const gates = /** @type {any[]} */ (await get("/api/gates", { query: { project_id: projectId, state: "open" } }).catch(() => []));
  const gate = gates.find((g) => !shown.has(g.id) && Array.isArray(g.tiles?.[0]?.facts?.palette_confirm) && g.tiles[0].facts.palette_confirm.length) ?? null;
  if (!gate) return "none";
  shown.add(gate.id);
  const tile = gate.tiles[0];
  const rows = /** @type {any[]} */ (tile.facts.palette_confirm);
  const action = (tile.allowed_actions || []).find((/** @type {string} */ a) => ["confirm", "approve", "continue"].includes(a)) ?? "approve";
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  const group = h("fieldset", { class: "choice-group" }, h("legend", {}, "Which colours should the character use?"),
    h("label", { class: "choice" }, h("input", { type: "radio", name: "pal", value: "picture", checked: true }), h("span", {}, h("strong", {}, "Use the colours from the picture (recommended)"), h("br"), h("small", { class: "muted" }, "This is what you approved."))),
    h("label", { class: "choice" }, h("input", { type: "radio", name: "pal", value: "planned" }), h("span", {}, h("strong", {}, "Keep the planned colours"), h("br"), h("small", { class: "muted" }, "The picture's colours are replaced by the plan's."))));
  const list = h("ul", { class: "diff-list" }, rows.map((r) => h("li", {}, h("span", { class: "diff-path" }, String(r.name || r.id)),
    h("span", { class: "diff-change" }, "planned ", chip(String(r.planned_hex)), String(r.planned_hex).toUpperCase(), h("span", { class: "arrow" }, " → "), "in the picture ", chip(String(r.picture_hex)), String(r.picture_hex).toUpperCase()))));
  const go = h("button", { type: "button", class: "btn primary", onclick: async () => {
    go.disabled = true;
    const choice = /** @type {HTMLInputElement} */ (group.querySelector("input:checked")).value;
    const r = await decide(gate, tile, action, { choice });
    if (r.status === "done") dlg.close("answered"); else go.disabled = false;
  } }, "Continue");
  dlg = openDialog({ title: "A few colours moved", dismissible: false, body: [h("p", {}, "When the final picture was drawn, some colours came out noticeably different from the plan."), list, group], actions: go });
  go.focus();
  await dlg.closed;
  setChildren(list);
  return "answered";
}
